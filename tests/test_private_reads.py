"""A private project's events and cell-wide lists stay out of reach of anyone not on it (TR-629).

Three parts:

1. Timing, on the store alone: 100k events with 10 private projects, one cell-wide read with and
   without the filter. The filtered read must cost less than twice the plain one.
2. A daemon with auth on: private project p1 has an event labelled for it, one that is only
   linked by its session, one whose payload names it, a source of its own, a desk item, a grant
   and a hand-over. Bob (not on p1) sees none of it in any read or list, and the desk routes that
   take an item by reference answer 404 for its item. Ann (leads p1) sees all of it. An event
   with no project label is visible to both.
3. Ask and Slack: the Ask agent runs with the asker's own credential, so its tool calls return
   nothing of p1 to Bob; Ann's return it. Slack's /tares ask knows no person and sees no private
   project at all.

Run: .venv/bin/python tests/test_private_reads.py
"""
import asyncio, hashlib, hmac, json, os, shutil, signal, socket, subprocess, sys, tempfile, threading, time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import clean_env
from tares.envelope import Envelope, now_utc
from tares.store import Store

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

AUTH = "root-token"
SLACK_SECRET = "private-reads-signing-secret-0123456789"   # gitleaks:allow, a test fixture
TMP = tempfile.mkdtemp(prefix="private-reads-")
DB = os.path.join(TMP, "cell.duckdb")


def free_port() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return str(s.getsockname()[1])


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


# ── part 1: what the filter costs ───────────────────────────────────────────────

def timing():
    print("== 100k events, 10 private projects: the filtered read against the plain one ==")
    store = Store(os.path.join(TMP, "timing.duckdb"))
    now = now_utc()
    projects = [f"proj-{i}" for i in range(10)]
    for i, uid in enumerate(projects):
        store.create_project(uid, "custom", f"Private {i}", {})
    for i, uid in enumerate(projects):
        store.link_session(uid, f"session-{i}")
    sources = ["logs", "deploys", "metrics", "claude_code"]
    batch = []
    for n in range(100_000):
        src = sources[n % 4]
        labels = {"env": "prod", "service": f"svc-{n % 50}"}
        key = f"svc-{n % 50}"
        if n % 10 == 0:                       # a tenth of the rows belong to a private project
            labels["tares_project"] = projects[(n // 10) % 10]
        elif n % 25 == 0:                     # and some are linked by their session
            key = f"session-{(n // 25) % 10}"
        batch.append(Envelope(src, "event_stream", key, "evt", f"event {n}",
                              now - timedelta(seconds=n % 3000), {"n": n}, labels))
        if len(batch) == 5000:
            store.append(batch); batch = []
    if batch:
        store.append(batch)
    # "shared" is what the daemon computes: the finding and memory sources, the only ones whose
    # payload can name another project. None of the four sources above is one.
    exclude = {"ids": projects, "names": [f"Private {i}" for i in range(10)], "shared": ["findings"]}
    since = now - timedelta(hours=2)

    def best(ex, runs=9):
        out = []
        for _ in range(runs):
            t = time.perf_counter()
            rows = store.read_window(sources, None, since, where={"env": "prod"}, exclude=ex)
            out.append(time.perf_counter() - t)
        return min(out), rows

    plain, rows_all = best(None)
    filtered, rows_kept = best(exclude)
    print(f"  plain read    {plain * 1000:8.1f} ms  ({len(rows_all)} rows)")
    print(f"  filtered read {filtered * 1000:8.1f} ms  ({len(rows_kept)} rows)")
    ck("the filtered read costs less than 2x the plain one", filtered < 2 * plain,
       f"{filtered * 1000:.1f} ms against {plain * 1000:.1f} ms")
    leaked = [r for r in rows_kept if "tares_project" in json.dumps(r[3]) and "proj-" in json.dumps(r[3])]
    ck("... and it still drops the private rows", not leaked and len(rows_kept) > 0, str(leaked[:1]))
    store.con.close()


# ── part 2 and 3: a daemon ──────────────────────────────────────────────────────

MODEL_CALLS: list = []
REPLIES: list = []


def _sse(blocks, stop) -> bytes:
    ev = [("message_start", {"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "stub", "content": [],
        "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 1}}})]
    for i, b in enumerate(blocks):
        if b["type"] == "text":
            ev.append(("content_block_start", {"type": "content_block_start", "index": i,
                                               "content_block": {"type": "text", "text": ""}}))
            ev.append(("content_block_delta", {"type": "content_block_delta", "index": i,
                                               "delta": {"type": "text_delta", "text": b["text"]}}))
        else:
            ev.append(("content_block_start", {"type": "content_block_start", "index": i,
                                               "content_block": {"type": "tool_use", "id": b["id"],
                                                                 "name": b["name"], "input": {}}}))
            ev.append(("content_block_delta", {"type": "content_block_delta", "index": i,
                                               "delta": {"type": "input_json_delta",
                                                         "partial_json": json.dumps(b["input"])}}))
        ev.append(("content_block_stop", {"type": "content_block_stop", "index": i}))
    ev.append(("message_delta", {"type": "message_delta",
                                 "delta": {"stop_reason": stop, "stop_sequence": None},
                                 "usage": {"output_tokens": 20}}))
    ev.append(("message_stop", {"type": "message_stop"}))
    return "".join(f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in ev).encode()


class Stub(BaseHTTPRequestHandler):
    """/v1/messages: the model. Its first turn asks for three reads (the projects, a read of the
    service, the recent events of the shared source); its second ends the turn. What the tools
    returned is in the second request's last message, which the test inspects. /response:
    Slack's response_url."""

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        if self.path.startswith("/response"):
            REPLIES.append(json.loads(raw))
            return self._send(b'{"ok":true}', "application/json")
        body = json.loads(raw or b"{}")
        MODEL_CALLS.append(body)
        last = (body.get("messages") or [{}])[-1].get("content")
        if isinstance(last, list) and any(b.get("type") == "tool_result" for b in last):
            out = _sse([{"type": "text", "text": "done"}], "end_turn")
        else:
            out = _sse([
                {"type": "tool_use", "id": "t1", "name": "list_projects", "input": {}},
                {"type": "tool_use", "id": "t2", "name": "read",
                 "input": {"selector": {"service": "checkout"}, "window": "1h"}},
                {"type": "tool_use", "id": "t3", "name": "recent_events",
                 "input": {"name": "evts", "limit": 20}},
            ], "tool_use")
        self._send(out, "text/event-stream")

    def _send(self, raw: bytes, ctype: str):
        self.send_response(200)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


def tool_results(call) -> str:
    """Everything the tools returned in one model request, as one string."""
    last = call["messages"][-1]["content"]
    out = []
    for b in last:
        if b.get("type") == "tool_result":
            c = b.get("content")
            out.append(c if isinstance(c, str) else json.dumps(c))
    return "\n".join(out)


async def _wait(url, tries=80):
    for _ in range(tries):
        try:
            async with httpx.AsyncClient() as cx:
                if (await cx.get(url, timeout=1)).status_code == 200:
                    return True
        except Exception:
            pass
        await asyncio.sleep(0.25)
    return False


async def _until(fn, tries=60):
    for _ in range(tries):
        if fn():
            return True
        await asyncio.sleep(0.5)
    return False


def spawn(port, extra=None):
    env = {**clean_env(), "TARES_DB": DB, "TARES_PORT": port, "TARES_OTLP_GRPC_PORT": "off",
           "TARES_AUTH_TOKEN": AUTH, **(extra or {})}
    return subprocess.Popen([sys.executable, "-c", "from tares.cli import run_daemon; run_daemon()"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop(proc):
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
        proc.wait()


def sign(ts, body):
    return "v0=" + hmac.new(SLACK_SECRET.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()


async def ask(cx, B, hd, text="what is happening?"):
    """The console's Ask as `hd`; returns what the tools returned to the model."""
    before = len(MODEL_CALLS)
    r = await cx.post(f"{B}/api/agent/chat", headers=hd,
                      json={"messages": [{"role": "user", "content": text}]})
    await _until(lambda: len(MODEL_CALLS) >= before + 2, 20)
    if len(MODEL_CALLS) < before + 2:
        return None
    return tool_results(MODEL_CALLS[before + 1])


async def setup(B, ids):
    """Phase 1: people, projects, sources, a desk item, a grant, a hand-over."""
    ROOT = H(AUTH)
    async with httpx.AsyncClient(timeout=20) as cx:
        async def person(name, email):
            return (await cx.post(f"{B}/api/people", headers=ROOT,
                                  json={"name": name, "email": email})).json()["id"]

        async def key(pid):
            r = await cx.post(f"{B}/api/keys", headers=ROOT,
                              json={"name": f"k-{pid}", "scopes": ["admin"], "person_id": pid})
            return r.json()["secret"]

        pa, pb = await person("Ann", "ann@x.dev"), await person("Bob", "bob@x.dev")
        ids["ann"], ids["bob"] = await key(pa), await key(pb)
        p1 = (await cx.post(f"{B}/api/projects", headers=H(ids["ann"]), json={
            "template": "custom", "name": "Secret", "objects": []})).json()["id"]
        team = (await cx.post(f"{B}/api/projects", headers=H(ids["ann"]), json={
            "template": "custom", "name": "Open", "objects": []})).json()["id"]
        r = await cx.patch(f"{B}/api/projects/{p1}", headers=H(ids["ann"]), json={"scope": "private"})
        ck("set up: Ann leads a private project, Bob is not on it", r.status_code == 200
           and r.json()["scope"] == "private", r.text[:200])
        ids["p1"], ids["team"] = p1, team
        lbl = [{"name": "service", "field": "service", "primary": True},
               {"name": "tares_project", "field": "tares_project"}]
        r = await cx.post(f"{B}/api/sources", headers=ROOT, json={
            "name": "findings", "connector": "finding", "config": {}})
        ck("set up: the shared findings source", r.status_code == 201, f"{r.status_code} {r.text[:160]}")
        for name, proj in (("evts", ""), ("sess", ""), ("p1-src", p1)):
            r = await cx.post(f"{B}/api/sources", headers=ROOT, json={
                "name": name, "connector": "webhook", "config": {"labels": lbl}, "project": proj})
            ck(f"set up: source {name}", r.status_code == 201, f"{r.status_code} {r.text[:160]}")
        r = await cx.post(f"{B}/api/desk", headers=ROOT, json={
            "question": "Ship the secret thing?", "recommendation": "yes", "why": "it is ready",
            "project": p1})
        ck("set up: a desk item on p1", r.status_code == 201, f"{r.status_code} {r.text[:160]}")
        ids["desk"] = r.json()["id"]
        r = await cx.post(f"{B}/api/desk", headers=ROOT, json={
            "question": "Ship the open thing?", "recommendation": "yes", "why": "it is ready",
            "project": team})
        ids["desk_open"] = r.json()["id"]
        r = await cx.post(f"{B}/api/grants", headers=ROOT, json={
            "words": "ship the secret thing whenever it is green", "scope": "project", "project": p1})
        ck("set up: a grant on p1", r.status_code == 201, f"{r.status_code} {r.text[:160]}")
        r = await cx.post(f"{B}/api/projects/{p1}/handover", headers=ROOT, json={
            "repos": [{"path": TMP, "name": "acme/secret-repo"}]})
        ck("set up: p1 handed to the crew", r.status_code == 200, f"{r.status_code} {r.text[:160]}")


def seed(ids):
    """Phase 2: the events, written to the store with the daemon stopped."""
    store = Store(DB)
    now = now_utc()
    p1, team = ids["p1"], ids["team"]

    def ev(src, key, text, labels, payload=None):
        return Envelope(src, "event_stream", key, "evt", text, now, payload or {"text": text}, labels)

    store.link_session(p1, "sess-secret")
    # the default fallbacks (F6): an agent nobody owns, on a cell whose default project is p1
    store.upsert_catalog_agent("orphan", "no-such-trigger", "Take a first look.")
    store.upsert_catalog_trigger("t-orphan", ["evts"], {"aggregate": "count", "predicate": "> 0",
                                                       "window": "1m"}, {}, "5m")
    store.mark_customized("trigger", "t-orphan")   # a catalog sync at boot would otherwise prune it
    store.set_setting("default_project", p1)
    store.append([
        ev("evts", "checkout", "SECRET label event", {"service": "checkout", "tares_project": p1}),
        ev("findings", "checkout", "SECRET payload event", {"service": "checkout"}, {"project": p1}),
        ev("findings", "checkout", "OPEN finding event", {"service": "checkout"}, {"project": team}),
        ev("evts", "checkout", "OPEN no-label event", {"service": "checkout"}),
        ev("evts", "checkout", "OPEN team event", {"service": "checkout", "tares_project": team}),
        ev("sess", "sess-secret", "SECRET session event", {"service": "checkout"}),
        ev("sess", "sess-other", "OPEN other-session event", {"service": "checkout"}),
        ev("p1-src", "checkout", "SECRET own-source event", {"service": "checkout"}),
    ])
    store.con.close()


SECRETS = ("SECRET label event", "SECRET payload event", "SECRET session event", "SECRET own-source event")
OPENS = ("OPEN no-label event", "OPEN team event", "OPEN other-session event", "OPEN finding event")


def has_all(text, words):
    return all(w in text for w in words)


def has_none(text, words):
    return not any(w in text for w in words)


async def reads(cx, B, hd):
    """Every cell-wide way to read events and lists, as one text per route."""
    out = {}
    r = await cx.post(f"{B}/read", headers=hd, json={"selector": {"service": "checkout"}, "window": "1h"})
    out["/read"] = r.text
    r = await cx.post(f"{B}/read", headers=hd, json={"selector": {"service": "checkout"}, "window": "1h",
                                                     "include_payload": True})
    out["/read raw"] = r.text
    for src in ("evts", "sess", "findings"):
        out[f"events {src}"] = (await cx.get(f"{B}/api/sources/{src}/events", headers=hd,
                                             params={"limit": 50})).text
    out["sources"] = (await cx.get(f"{B}/api/sources", headers=hd)).text
    out["entities"] = (await cx.get(f"{B}/api/entities", headers=hd)).text
    out["catalog"] = (await cx.get(f"{B}/catalog", headers=hd)).text
    return out


async def daemon_checks(B, ids):
    ROOT, A_, B_ = H(AUTH), H(ids["ann"]), H(ids["bob"])
    p1 = ids["p1"]
    async with httpx.AsyncClient(timeout=30) as cx:
        a, b = await reads(cx, B, A_), await reads(cx, B, B_)

        print("== events: Bob sees none of p1's, Ann sees all ==")
        for route in ("/read", "/read raw", "events evts", "events sess", "events findings"):
            ck(f"Bob, {route}: nothing of p1", has_none(b[route], SECRETS), b[route][:200])
        ck("Bob, /read: the events with no p1 label are there", has_all(b["/read"], OPENS), b["/read"][:300])
        ck("Bob, events evts: the unlabelled one is there", "OPEN no-label event" in b["events evts"], b["events evts"][:200])
        ck("Ann, /read: all of p1's and the others", has_all(a["/read"], SECRETS + OPENS), a["/read"][:400])
        ck("Ann, events evts: the label one", "SECRET label event" in a["events evts"], a["events evts"][:300])
        ck("Ann, events findings: the one recorded in p1", "SECRET payload event" in a["events findings"],
           a["events findings"][:300])
        ck("Bob, events findings: the one recorded in the open project", "OPEN finding event" in b["events findings"],
           b["events findings"][:300])
        ck("Ann, events sess: the session one", "SECRET session event" in a["events sess"], a["events sess"][:200])
        ck("the system credential reads all of it too",
           has_all((await cx.post(f"{B}/read", headers=ROOT, json={
               "selector": {"service": "checkout"}, "window": "1h"})).text, SECRETS + OPENS))

        print("== lists: p1's source and its entity values ==")
        ck("Bob: its own source is not listed", "p1-src" not in b["sources"], b["sources"][:200])
        ck("Ann: it is", "p1-src" in a["sources"], a["sources"][:200])
        r = await cx.get(f"{B}/api/sources/p1-src/events", headers=B_)
        ck("Bob: its events by name are as unknown as a name that does not exist",
           r.status_code == 404 == (await cx.get(f"{B}/api/sources/no-such/events", headers=B_)).status_code,
           f"{r.status_code} {r.text[:120]}")
        ck("Ann: its events by name", (await cx.get(f"{B}/api/sources/p1-src/events", headers=A_)).status_code == 200)
        ck("Bob: the catalog does not list it", "p1-src" not in b["catalog"], b["catalog"][:200])
        ck("Bob: the entity counts do not carry p1's id", p1 not in b["entities"], b["entities"][:300])
        ck("Ann: they do", p1 in a["entities"], a["entities"][:300])

        print("== the desk ==")
        lst = (await cx.get(f"{B}/api/desk", headers=B_)).text
        ck("Bob: the list has the open item, not p1's", ids["desk_open"] in lst and ids["desk"] not in lst
           and "secret thing" not in lst, lst[:300])
        lst = (await cx.get(f"{B}/api/desk", headers=A_)).text
        ck("Ann: the list has both", ids["desk_open"] in lst and ids["desk"] in lst, lst[:300])
        for route, body in (("withdraw", {}), ("answer", {"words": "yes, ship it"}), ("allow", {"words": "yes"})):
            r = await cx.post(f"{B}/api/desk/{ids['desk']}/{route}", headers=B_, json=body)
            gone = await cx.post(f"{B}/api/desk/D9999/{route}", headers=B_, json=body)
            ck(f"Bob: {route} on p1's item is as unknown as an item that does not exist",
               r.status_code == gone.status_code == 404, f"{r.status_code} {r.text[:120]} / {gone.status_code}")
        still = next((d for d in (await cx.get(f"{B}/api/desk", headers=A_)).json()["desk"]
                      if d["id"] == ids["desk"]), None)
        ck("... and the item is still open", still is not None and still["state"] == "open", str(still))

        print("== the default project is private to Bob: no default for him ==")
        r = await cx.post(f"{B}/api/agents/builtin/orphan/enable", headers=B_)
        ck("Bob: turning on an agent nobody owns asks him to name a project",
           r.status_code == 400 and "name the project" in r.text and p1 not in r.text, f"{r.status_code} {r.text[:200]}")
        r = await cx.post(f"{B}/api/agents/builtin/orphan/enable", headers=A_)
        ck("Ann: the default is hers, so she is not asked", "name the project" not in r.text, f"{r.status_code} {r.text[:200]}")
        ck("... and Bob's refusal wrote nothing into p1",
           "orphan" not in (await cx.get(f"{B}/api/projects/{p1}", headers=B_)).text)

        trig = {"name": "t-orphan", "sources": ["evts"], "cooldown": "5m",
                "condition": {"aggregate": "count", "predicate": "> 0", "window": "1m"}}
        r = await cx.put(f"{B}/api/triggers/t-orphan", headers=B_, json=trig)
        ck("Bob: a trigger nobody owns sits in the private default, so editing it is as unknown as a missing one",
           r.status_code == 404 and p1 not in r.text, f"{r.status_code} {r.text[:200]}")
        r = await cx.put(f"{B}/api/agents/builtin/orphan", headers=B_, json={"prompt": "Look again."})
        ck("Bob: the same for an agent nobody owns",
           r.status_code == 404 and p1 not in r.text, f"{r.status_code} {r.text[:200]}")
        r = await cx.put(f"{B}/api/triggers/t-orphan", headers=A_, json=trig)
        ck("Ann: the default is hers, so editing the trigger works", r.status_code == 200, f"{r.status_code} {r.text[:200]}")

        print("== grants and the hand-over ==")
        r = await cx.get(f"{B}/api/grants", headers=B_, params={"project": p1})
        ck("Bob: p1's grants are unknown", r.status_code == 404, f"{r.status_code} {r.text[:120]}")
        ck("Bob: the cell-wide grants do not carry p1's", "secret thing" not in (await cx.get(f"{B}/api/grants", headers=B_)).text)
        ck("Ann: p1's grants", "secret thing" in (await cx.get(f"{B}/api/grants", headers=A_, params={"project": p1})).text)
        crew = (await cx.get(f"{B}/api/crew/projects", headers=B_)).text
        ck("Bob: the crew's project list does not carry p1 or its repo", p1 not in crew and "secret-repo" not in crew, crew[:300])
        crew = (await cx.get(f"{B}/api/crew/projects", headers=A_)).text
        ck("Ann: it does", p1 in crew and "secret-repo" in crew, crew[:300])
        r = await cx.get(f"{B}/api/crew", headers=B_)
        ck("Bob: the crew view does not carry p1", p1 not in r.text and "Secret" not in r.text, r.text[:300])


async def ask_checks(B, ids):
    A_, B_ = H(ids["ann"]), H(ids["bob"])
    print("== Ask runs with the asker's own credential ==")
    async with httpx.AsyncClient(timeout=60) as cx:
        got_b = await ask(cx, B, B_)
        ck("Bob: the model got its tool results", bool(got_b), str(got_b)[:100])
        got_b = got_b or ""
        ck("Bob: list_projects names no p1", "Secret" not in got_b and ids["p1"] not in got_b, got_b[:300])
        ck("Bob: the open project is listed", "Open" in got_b, got_b[:300])
        ck("Bob: read and recent_events return no p1 event", has_none(got_b, SECRETS), got_b[:400])
        ck("Bob: ... but the events with no project label", has_all(got_b, OPENS[:2]), got_b[:400])
        got_a = await ask(cx, B, A_) or ""
        ck("Ann: list_projects names p1", "Secret" in got_a, got_a[:300])
        ck("Ann: read returns the p1 events", has_all(got_a, ("SECRET label event", "SECRET payload event", "SECRET session event")), got_a[:500])

    print("== Slack's /tares ask sees no private project ==")
    before = len(MODEL_CALLS)
    form = urlencode({"token": "x", "team_id": "T1", "user_id": "U1", "channel_id": "C1",
                      "command": "/tares", "text": "ask what is happening",
                      "response_url": f"http://127.0.0.1:{ids['stub']}/response"}).encode()
    ts = str(int(time.time()))
    async with httpx.AsyncClient(timeout=30) as cx:
        r = await cx.post(f"{B}/api/slack/events", content=form, headers={
            "content-type": "application/x-www-form-urlencoded", "x-slack-request-timestamp": ts,
            "x-slack-signature": sign(ts, form)})
        ck("Slack: the command is accepted", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
    await _until(lambda: len(MODEL_CALLS) >= before + 2, 30)
    ck("Slack: the model got its tool results", len(MODEL_CALLS) >= before + 2, str(len(MODEL_CALLS) - before))
    if len(MODEL_CALLS) >= before + 2:
        got = tool_results(MODEL_CALLS[before + 1])
        ck("Slack: no p1 in the project list", "Secret" not in got and ids["p1"] not in got, got[:300])
        ck("Slack: no p1 event", has_none(got, SECRETS), got[:400])
        ck("Slack: the open project and events are there", "Open" in got and has_all(got, OPENS[:2]), got[:400])


async def main():
    timing()
    ids = {}
    port = free_port()
    B = f"http://127.0.0.1:{port}"
    proc = spawn(port)
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up", False); return
        await setup(B, ids)
    finally:
        stop(proc)
    seed(ids)

    stub = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    ids["stub"] = stub.server_address[1]
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    proc = spawn(port, {"ANTHROPIC_API_KEY": "sk-test", "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{ids['stub']}",
                        "TARES_SLACK_SIGNING_SECRET": SLACK_SECRET})
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up (second start)", False); return
        await daemon_checks(B, ids)
        await ask_checks(B, ids)
    finally:
        stop(proc)
        stub.shutdown()
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    asyncio.run(main())
