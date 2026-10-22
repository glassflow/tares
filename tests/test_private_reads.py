"""A private project's events and cell-wide lists stay out of reach of anyone not on it (TR-629).

Three parts:

1. Timing, on the store alone: 100k events with 10 private projects, one cell-wide read with and
   without the filter. Both timings are printed; the filtered one must stay within 5x, and
   with no private project the filter adds no SQL at all.
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
from tares.store import Store, _exclude_sql

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
    # the ratio is a report, not a gate: it swings with the machine. The cost that matters is
    # fixed by construction: no private project, no SQL added; and a loose bound catches a blow-up.
    print(f"  ratio         {filtered / plain:8.2f}x")
    ck("the filtered read stays within 5x of the plain one", filtered < 5 * plain,
       f"{filtered * 1000:.1f} ms against {plain * 1000:.1f} ms")
    ck("with no private project the filter adds no SQL", _exclude_sql(None) == ("", [])
       and _exclude_sql({"ids": [], "names": []}) == ("", []), str(_exclude_sql(None)))
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
        r = await cx.post(f"{B}/api/projects/{team}/handover", headers=ROOT, json={
            "repos": [{"path": TMP, "name": "acme/open-repo"}]})
        ck("set up: the team project handed to the crew", r.status_code == 200, f"{r.status_code} {r.text[:160]}")


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
    seed_objects(store, ids, now)
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


def seed_objects(store, ids, now):
    """Things p1 owns besides events, and their open counterparts in the team project."""
    p1, team = ids["p1"], ids["team"]
    cond = {"aggregate": "count", "predicate": "> 0", "window": "1m"}
    for uid, trig, agent in ((p1, "t-secret", "secret-agent"), (team, "t-open", "open-agent")):
        store.upsert_catalog_trigger(trig, ["p1-src" if uid == p1 else "evts"], cond, {}, "5m")
        store.set_owned_by("trigger", trig, uid)
        store.upsert_project_object(uid, "trigger", trig, trig)
        store.mark_customized("trigger", trig)
        store.upsert_catalog_agent(agent, trig, "Look.")
        store.set_owned_by("agent", agent, uid)
        store.upsert_project_object(uid, "agent", agent, agent)
        store.mark_customized("agent", agent)
    # an agent both projects use: Bob may know it and the team run, never the newer p1 run
    store.upsert_catalog_agent("shared-agent", "t-open", "Look.")
    store.set_owned_by("agent", "shared-agent", team)
    for uid in (p1, team):
        store.upsert_project_object(uid, "agent", "shared-agent", "shared-agent")
    store.mark_customized("agent", "shared-agent")
    for rid, uid in (("run-team", team), ("run-p1", p1)):
        store.start_agent_run(rid, "shared-agent", "t-open", "d-" + rid, "checkout", "h", project=uid)
        store.finish_agent_run(rid, "done", finding=f"finding of {rid}")
        time.sleep(0.01)
    store.start_agent_run("run-secret", "secret-agent", "t-secret", "d-secret", "checkout", "h", project=p1)
    store.finish_agent_run("run-secret", "done", finding="SECRET run finding")
    for did, trig, uid, text in (("d-secret", "t-secret", p1, "SECRET dispatch payload"),
                                 ("d-open", "t-open", team, "OPEN dispatch payload")):
        store.log_dispatch(did, trig, "checkout", "incident", 1, 1, text, project=uid)
        store.add_dispatch_projects(did, [uid])
    store.add_subscription("sub-secret", "t-secret", "http://127.0.0.1:9/secret-hook")
    store.add_subscription("sub-open", "t-open", "http://127.0.0.1:9/open-hook")
    store.log_query("q-secret", "(read)", "tares_project=Secret", "1h", 1, "mcp")
    store.log_query("q-open", "(read)", "service=checkout", "1h", 1, "mcp")
    store.upsert_skill(p1, "secret-skill", "SECRET skill", "body")
    store.upsert_skill(team, "open-skill", "OPEN skill", "body")
    store.create_doc(p1, "note", "SECRET doc", "body")
    store.create_doc(team, "note", "OPEN doc", "body")
    for name, uid in (("secret-mcp", p1), ("open-mcp", team)):
        store.upsert_mcp_server(name, "http://127.0.0.1:9/mcp")
        store.set_owned_by("mcp_server", name, uid)
        store.upsert_project_object(uid, "mcp_server", name, name)
    store.set_session_station("s-secret", "secret-station", "builder", "crew-orchestrator", p1, now)
    store.set_session_station("s-open", "open-station", "builder", "crew-orchestrator", team, now)
    # tickets in review, and one finding kind that came back in three of them (a recurring one)
    for uid, repo, label in ((p1, "acme/secret-repo", "SECRET"), (team, "acme/open-repo", "OPEN")):
        for n in range(3):
            tid = store.create_ticket(uid, "ann", f"{label} ticket {n}", repo=repo)
            store.update_ticket(tid, stage="review", pr={"url": f"https://github.com/{repo}/pull/{n}",
                                                         "repo": repo, "head": "abc"})
            store.add_review(uid, tid, f"https://github.com/{repo}/pull/{n}", "abc", "changes", "ran it",
                             "", "crew-reviewer", [{"severity": "P2", "file": "a.py", "line": 1,
                                                     "blocking": True, "text": f"{label} missing check"}],
                             {}, lambda t: f"{label.lower()}-kind", now)
    store.upsert_rule_proposal("acme/secret-repo", "secret-kind", p1, "project",
                               "SECRET rule text", ["Secret T1"], now)
    store.upsert_rule_proposal("acme/open-repo", "open-kind", team, "project",
                               "OPEN rule text", ["Open T1"], now)


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


async def list_checks(B, ids):
    """Every cell-wide list and by-name read that leaves out what only p1 owns (TR-629)."""
    A_, B_ = H(ids["ann"]), H(ids["bob"])
    p1 = ids["p1"]
    async with httpx.AsyncClient(timeout=30) as cx:
        async def both(path, **kw):
            return ((await cx.get(f"{B}{path}", headers=B_, **kw)),
                    (await cx.get(f"{B}{path}", headers=A_, **kw)))

        async def listed(label, path, secret, openw=None, **kw):
            b, a = await both(path, **kw)
            ck(f"{label}: Bob's list has no {secret}", b.status_code == 200 and secret not in b.text,
               f"{b.status_code} {b.text[:200]}")
            ck(f"{label}: Ann's has it", secret in a.text, f"{a.status_code} {a.text[:200]}")
            if openw:
                ck(f"{label}: Bob still sees {openw}", openw in b.text, b.text[:200])

        async def by_name(label, path):
            b, a = await both(path)
            gone = await cx.get(f"{B}{path.rsplit('/', 1)[0]}/no-such-thing", headers=B_)
            ck(f"{label}: Bob gets a 404", b.status_code == 404, f"{b.status_code} {b.text[:160]}")
            ck(f"{label}: Ann gets it", a.status_code == 200, f"{a.status_code} {a.text[:160]}")
            return gone

        print("== by name: a hidden object is as unknown as one that does not exist ==")
        await by_name("source", "/api/sources/p1-src")
        await by_name("source fields", "/api/sources/p1-src/fields")
        await by_name("catalog describe, source", "/catalog/source:p1-src")
        await by_name("catalog describe, trigger", "/catalog/trigger:t-secret")
        await by_name("agent runs", "/api/agents/builtin/secret-agent/runs")
        await by_name("dispatch detail", "/api/activity/dispatches/d-secret")
        for src, who, want in (("evts", B_, False), ("evts", A_, True), ("p1-src", B_, False),
                               ("p1-src", A_, True)):
            r = await cx.post(f"{B}/api/labels/preview", headers=who,
                              json={"source": src, "label": {"name": "msg", "field": "text"}})
            if src == "p1-src" and not want:
                ck("label preview: Bob on p1's source is a 404", r.status_code == 404, f"{r.status_code} {r.text[:160]}")
            elif src == "p1-src":
                ck("label preview: Ann on p1's source works", r.status_code == 200 and "SECRET own-source event" in r.text,
                   f"{r.status_code} {r.text[:200]}")
            elif want:
                ck("label preview: Ann sees p1's event text on a shared source", "SECRET label event" in r.text,
                   f"{r.status_code} {r.text[:200]}")
            else:
                ck("label preview: Bob sees none of p1's event text", r.status_code == 200 and has_none(r.text, SECRETS)
                   and "OPEN no-label event" in r.text, f"{r.status_code} {r.text[:200]}")

        print("== lists ==")
        for ag in ("secret-agent", "open-agent"):   # the roster lists what is switched on
            er = await cx.post(f"{B}/api/agents/builtin/{ag}/enable", headers=H(AUTH))
            ck(f"set up: {ag} switched on", er.status_code == 200, er.text[:160])
        b, a = await both("/catalog")
        ck("catalog: Bob has no p1 trigger and no p1 project", "t-secret" not in b.text and p1 not in b.text, b.text[b.text.find(p1) - 150:][:400] if p1 in b.text else b.text[:200])
        ck("catalog: Ann has both", "t-secret" in a.text and p1 in a.text, a.text[:300])
        await listed("triggers", "/api/triggers", "t-secret", "t-open")
        await listed("built-in agents", "/api/agents/builtin", "secret-agent", "open-agent")
        await listed("MCP servers", "/api/mcp-servers", "secret-mcp", "open-mcp")
        await listed("skills", "/api/skills", "secret-skill", "open-skill")
        await listed("docs", "/api/docs", "SECRET doc", "OPEN doc")
        await listed("roster", "/api/agents", "secret-agent", "open-agent")
        await listed("subscriptions", "/api/subscriptions", "sub-secret", "sub-open")
        await listed("activity, dispatches", "/api/activity/dispatches", "d-secret", "d-open")
        await listed("activity, queries", "/api/activity/queries", "tares_project=Secret", "service=checkout")
        await listed("resources", "/api/resources", "secret-skill", "open-skill")
        b, _ = await both("/api/resources")
        ck("resources: nor the p1 agent, trigger or MCP server", has_none(b.text, ("secret-agent", "t-secret", "secret-mcp")),
           b.text[:200])
        await listed("standup", "/api/crew/standup", "SECRET ticket", "OPEN ticket")
        b, _ = await both("/api/crew/standup")
        ck("standup: Bob's report names no p1", p1 not in b.text and "Secret" not in b.text, b.text[:300])
        await listed("crew stations", "/api/crew", "secret-station", "open-station")
        await listed("review queue", "/api/review-queue", "SECRET ticket", "OPEN ticket")
        await listed("recurring findings", "/api/recurring-findings", "secret-kind", params={"repo": "acme/secret-repo"})
        await listed("rule proposals", "/api/rule-proposals", "SECRET rule text", "OPEN rule text")

        print("== an agent two projects use: the newest run Bob may see ==")
        b, a = await both("/api/agents/builtin")
        row = lambda r: next((x for x in r.json()["agents"] if x["name"] == "shared-agent"), {})
        ck("Bob: last_run is the team's run, not the newer p1 one",
           (row(b).get("last_run") or {}).get("id") == "run-team", str(row(b).get("last_run"))[:200])
        ck("Bob: the agent's projects do not name p1", p1 not in json.dumps(row(b).get("projects")), str(row(b).get("projects")))
        ck("Ann: last_run is the p1 run, the newest", (row(a).get("last_run") or {}).get("id") == "run-p1",
           str(row(a).get("last_run"))[:200])
        ck("Ann: the agent's projects name both", p1 in json.dumps(row(a).get("projects")), str(row(a).get("projects")))
        b, a = await both("/api/agents/builtin/shared-agent/runs")
        ck("agent runs: Bob has the team's, not the p1 run", "run-team" in b.text and "run-p1" not in b.text, b.text[:200])
        ck("agent runs: Ann has both", "run-team" in a.text and "run-p1" in a.text, a.text[:200])


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
        await list_checks(B, ids)
        await ask_checks(B, ids)
    finally:
        stop(proc)
        stub.shutdown()
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    asyncio.run(main())
