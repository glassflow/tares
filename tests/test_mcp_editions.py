"""MCP tools per edition (TR-757): a factory cell lists its outcome tools first and hides the
building-block tools, an AI SRE cell keeps only `read` of them and lists its own four outcome tools
first (TR-766), a crew station sees everything, a cell with no edition lists everything, and an
unreachable daemon fails open to every tool.
The filter is a tidy list, not a permission boundary: the daemon still checks every call.
"""
import asyncio, json, os, signal, subprocess, sys, tempfile
from datetime import datetime, timedelta, timezone
import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import clean_env

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

DPORT, MPORT, TOKEN = "8856", "8857", "root-mcp"
URL = f"http://127.0.0.1:{MPORT}/mcp"
D = f"http://127.0.0.1:{DPORT}"
ROOT = {"Authorization": f"Bearer {TOKEN}"}
OBJECTS = ("read", "stats", "catalog_list", "catalog_describe", "list_connectors", "discover_source",
           "discover_docker", "test_source", "create_source", "list_sources", "source_fields",
           "create_trigger", "update_trigger", "subscribe")
OUTCOMES = ("what_needs_me", "how_is_project", "start_project", "hand_project_to_crew")
SRE_OUTCOMES = ("what_is_broken", "how_is_service", "what_happened", "mark_incident")


async def _wait(url, ok=lambda r: r.status_code == 200):
    for _ in range(120):
        try:
            async with httpx.AsyncClient() as cx:
                if ok(await cx.get(url, timeout=1)):
                    return True
        except Exception:
            pass
        await asyncio.sleep(0.25)
    return False


def _spawn(code, env):
    return subprocess.Popen([sys.executable, "-c", code], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _stop(*procs):
    for p in procs:
        if p is None:
            continue
        p.send_signal(signal.SIGTERM)
        try: p.wait(timeout=5)
        except Exception: p.kill()


def seed_sre(db):
    """The shared AI SRE cell with one open incident on edge-proxy, written before the daemon starts."""
    from datetime import datetime, timedelta, timezone
    from fixtures.sre_cell import build
    from tares.store import Store
    now = datetime.now(timezone.utc)
    store = Store(db)
    try:
        uid = build(store, now, watcher=True)["project"]
        at = now - timedelta(minutes=10)
        store.start_agent_run("w-1", "watcher", "watch-prod", "", "edge-proxy", "h", project=uid)
        store.finish_agent_run("w-1", "ok", finding="502s on edge-proxy rose to 12x.",
                               outcome="finding", verdict="investigate")
        inc = store.create_incident("edge-proxy", uid, at)
        store.add_incident_note(inc["id"], "w-1", at)
    finally:
        store.con.close()


def _start(edition, daemon=True, seed=None):
    tmp = tempfile.mkdtemp(prefix="mcp-editions-")
    if seed:
        seed(os.path.join(tmp, "t.duckdb"))
    base = clean_env(TARES_OTLP_GRPC_PORT="off", TARES_AUTH_TOKEN=TOKEN, TARES_EDITION=edition)
    d = None
    if daemon:
        d = _spawn("from tares.cli import run_daemon; run_daemon()",
                   {**base, "TARES_DB": os.path.join(tmp, "t.duckdb"), "TARES_CATALOG": "/tmp/none_me.yaml",
                    "TARES_PORT": DPORT})
    m = _spawn("from tares.cli import run_mcp; run_mcp()",
               {**base, "TARES_MCP_TRANSPORT": "streamable-http", "TARES_MCP_HOST": "127.0.0.1",
                "TARES_MCP_PORT": MPORT, "TARES_MCP_SURFACE_TTL": "0",
                "TARESD_URL": f"http://127.0.0.1:{DPORT}"})
    return d, m


async def _session(headers, fn):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    async with streamablehttp_client(URL, headers=headers) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            return await fn(s)


async def names(headers=ROOT):
    async def f(s):
        return [t.name for t in (await s.list_tools()).tools]
    return await _session(headers, f)


async def call(name, args, headers=ROOT):
    async def f(s):
        return (await s.call_tool(name, args)).content[0].text
    return await _session(headers, f)


async def up(daemon=True):
    if daemon and not await _wait(f"{D}/health"):
        return False
    return await _wait(URL, ok=lambda r: r.status_code < 500)


async def factory():
    print("== a factory cell ==")
    d, m = _start("factory")
    try:
        if not await up():
            ck("daemon and mcp up", False); return
        n = await names()
        ck("the outcome tools come first", n[:4] == list(OUTCOMES), str(n[:6]))
        ck("no building-block tool", not set(OBJECTS) & set(n), str(sorted(set(OBJECTS) & set(n))))
        ck("no AI SRE outcome tool", not set(SRE_OUTCOMES) & set(n), str(sorted(set(SRE_OUTCOMES) & set(n))))
        ck("answer_question is a crew tool and stays", "answer_question" in n)
        ck("the crew's tools stay", "list_tickets" in n and "desk_list" in n and "hand_over" in n, str(n))
        ck("a hidden tool answers in a sentence",
           await call("read", {"selector": {"a": "b"}}) == "This tool is off on this Tares. An admin "
           "can turn on the building-block tools under Settings, Access and API keys, Agent tools.")
        ck("create_source is off too", "off on this Tares" in await call("create_source", {"name": "x"}))
        st = {"X-Tares-Role": "builder"}
        ck("a station sees the building blocks", "read" in await names({**ROOT, **st}))
        # The server keeps one tool cache for every caller: once a station has listed `read`, the
        # person's call to it must still get the sentence, not an output check error.
        ck("a hidden tool answers after a station listed it",
           "off on this Tares" in await call("read", {"selector": {"a": "b"}}))
        ck("a station can call a hidden tool", "off on this Tares" not in
           await call("list_connectors", {}, {**ROOT, **st}))

        started = await call("start_project", {"name": "alpha", "goal": "ship the thing"})
        ck("start_project makes a project", '"name": "alpha"' in started, started)
        async with httpx.AsyncClient(base_url=D, headers=ROOT, timeout=10) as cx:
            r = await cx.post("/api/desk", json={"question": "Which colour for the links?",
                                                 "recommendation": "blue", "why": "it reads well",
                                                 "project": "alpha"})
            ck("a question on the desk", r.status_code == 201, r.text)
            w = (await cx.get("/api/whoami")).json()
            ck("whoami says object tools are off", w["mcp_object_tools"] is False, str(w))
            r = await cx.put("/api/settings/mcp", json={"object_tools": True},
                             headers={"Authorization": "Bearer nope"})
            ck("a bad key cannot change it", r.status_code in (401, 403), r.text)
            k = (await cx.post("/api/keys", json={"name": "r", "scopes": ["read"]})).json()
            r = await cx.put("/api/settings/mcp", json={"object_tools": True},
                             headers={"Authorization": f"Bearer {k['secret']}"})
            ck("a read key cannot change it", r.status_code == 403, r.text)
        async with httpx.AsyncClient(base_url=D, headers=ROOT, timeout=10) as cx:
            home = (await cx.get("/api/home")).json()
        waits = json.loads(await call("what_needs_me", {}))
        ck("what_needs_me keeps the home headline", waits["headline"] == home["headline"] and home["headline"],
           str(waits.get("headline")))
        ck("what_needs_me returns the home items", isinstance(waits.get("waiting_for_you"), list)
           and "headline" in waits and any("colour" in json.dumps(i) for i in waits["waiting_for_you"]),
           str(waits)[:300])
        row = json.loads(await call("how_is_project", {"project": "alpha"}))
        ck("how_is_project returns the project's row", row.get("name") == "alpha" and "tickets" in row, str(row))
        byid = json.loads(await call("how_is_project", {"project": row.get("id", "x")}))
        ck("how_is_project finds a project by id", byid.get("name") == "alpha" and row.get("id"), str(byid))
        miss = await call("how_is_project", {"project": "nope"})
        ck("an unknown project names the ones there are", miss == "No project called nope. Projects: alpha.", miss)
        sent = await call("hand_project_to_crew", {"project": "alpha", "repos": [
            {"path": "/tmp/alpha", "name": "acme/alpha"}]})
        ck("hand_project_to_crew records the hand-over", sent.startswith("Recorded") and "/tmp/alpha" in sent, sent)

        async with httpx.AsyncClient(base_url=D, headers=ROOT, timeout=10) as cx:
            r = await cx.put("/api/settings/mcp", json={"object_tools": True})
            ck("an admin turns object tools on", r.status_code == 200 and r.json()["object_tools"] is True, r.text)
        n = await names()
        ck("the building blocks come back", set(OBJECTS) <= set(n), str(sorted(set(OBJECTS) - set(n))))
        ck("and a building block runs", "off on this Tares" not in await call("list_connectors", {}))
        ck("and the outcome tools still lead", n[:4] == list(OUTCOMES), str(n[:6]))
        ck("a hidden tool runs again", "off on this Tares" not in await call("list_connectors", {}))
        async with httpx.AsyncClient(base_url=D, headers=ROOT, timeout=10) as cx:
            r = await cx.put("/api/settings/mcp", json={"object_tools": None})
            ck("null clears the choice", r.status_code == 200 and r.json()["object_tools"] is False, r.text)
        ck("and they are hidden again", "read" not in await names())
        ck("a call after they were listed and turned off gets the sentence",
           "off on this Tares" in await call("read", {"selector": {"a": "b"}}))
        ck("a bad key fails open to every tool and the call says 401",
           set(OBJECTS) <= set(await names({"Authorization": "Bearer nope"})))
        ck("what_needs_me reports an error from the daemon",
           (await call("what_needs_me", {}, {"Authorization": "Bearer nope"})).startswith("error 401"))
    finally:
        _stop(m, d)


async def sre():
    print("== an AI SRE cell ==")
    d, m = _start("sre", seed=seed_sre)
    try:
        if not await up():
            ck("daemon and mcp up", False); return
        n = await names()
        ck("the four outcome tools come first", n[:4] == list(SRE_OUTCOMES), str(n[:6]))
        ck("read stays", "read" in n, str(n))
        ck("the other building blocks are hidden", not (set(OBJECTS) - {"read"}) & set(n),
           str(sorted((set(OBJECTS) - {"read"}) & set(n))))
        ck("no factory outcome tool", not set(OUTCOMES) & set(n), str(sorted(set(OUTCOMES) & set(n))))
        ck("the crew's tools stay", "list_tickets" in n and "desk_list" in n)
        await sre_outcomes()
        ck("create_source answers in a sentence", "off on this Tares" in await call("create_source", {"name": "x"}))
        ck("an outcome tool says it is not part of this edition",
           "not part of the sre edition" in await call("what_needs_me", {}))
        async with httpx.AsyncClient(base_url=D, headers=ROOT, timeout=10) as cx:
            r = await cx.put("/api/settings/mcp", json={"object_tools": True})
        n = await names()
        ck("object tools on, the factory outcome tools stay hidden on sre",
           set(OBJECTS) <= set(n) and not set(OUTCOMES) & set(n), str(sorted(set(OUTCOMES) & set(n))))
    finally:
        _stop(m, d)


async def sre_unit():
    print("== the AI SRE tools, in process ==")

    class _Fake(_Resp):
        @property
        def text(self):
            return json.dumps(self._body)
    import time
    from tares import mcp_outcomes as mo
    seen, answer = [], {"r": _Fake(200, {})}

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None):
            seen.append((url, params)); return answer["r"]
        async def post(self, url, **kw):
            seen.append((url, None)); return answer["r"]

    real = mo._cx
    mo._cx = lambda timeout=10: Client()
    old_tz = os.environ.get("TZ")
    try:
        around = "2026-11-02T14:00:00Z"
        for window, minutes in (("30m", 30), ("1h", 60), ("6h", 360), ("24h", 1440)):
            seen.clear()
            await mo.what_happened(window=window, around=around)
            p = seen[0][1]
            span = datetime.fromisoformat(p["to"]) - datetime.fromisoformat(p["from"])
            ck(f"the {window} window reaches back {minutes} minutes", span == timedelta(minutes=minutes), str(p))
        seen.clear()
        await mo.what_happened(around="2026-11-02T14:00:00+02:00")
        ck("an around with an offset is turned to UTC", seen[0][1]["to"] == "2026-11-02T12:00:00+00:00", str(seen))
        os.environ["TZ"] = "Asia/Kolkata"; time.tzset()
        seen.clear()
        await mo.what_happened(around="2026-11-02T14:00:00")
        ck("an around with no zone is UTC whatever the local zone is",
           seen[0][1]["to"] == "2026-11-02T14:00:00+00:00", str(seen))
        for bad in ("0001-01-01", "0001-01-01T00:00:00+05:00"):
            seen.clear()
            said = await mo.what_happened(around=bad)
            ck(f"an around of {bad} gets a sentence, not a crash",
               said.startswith("around must be an ISO time") and not seen, said)

        answer["r"] = _Fake(200, {"range_words": "Last hour", "items": [], "next_before": "2026-11-02T13:00:00Z"})
        ck("more is true when the daemon has a next page",
           json.loads(await mo.what_happened())["more"] is True)
        answer["r"] = _Fake(200, {"range_words": "Last hour", "items": [], "next_before": None})
        ck("and false when it does not", json.loads(await mo.what_happened())["more"] is False)

        answer["r"] = _Fake(403, {"detail": "This key lacks the admin scope."})
        for name, call_ in (("what_is_broken", mo.what_is_broken()), ("how_is_service", mo.how_is_service("a")),
                            ("what_happened", mo.what_happened()), ("mark_incident", mo.mark_incident("i", "open"))):
            ck(f"{name} gives a refusal as the daemon's sentence", await call_ == "This key lacks the admin scope.")
        answer["r"] = _Fake(409, {"detail": "This incident is already open."})
        ck("a 409 is a sentence", await mo.mark_incident("i", "open") == "This incident is already open.")
        answer["r"] = _Fake(500, {"detail": "boom"})
        ck("a server error keeps its status", await mo.what_is_broken() == "error 500: boom")
        answer["r"] = _Fake(422, {"detail": [{"msg": "bad"}]})
        ck("a detail that is not a sentence keeps its status",
           (await mo.what_is_broken()).startswith("error 422: "))
        seen.clear(); answer["r"] = _Fake(200, {"id": "i"})
        await mo.mark_incident("inc 1/x", "resolved")
        ck("mark_incident posts to the AI SRE route, escaped",
           seen[0][0].endswith("/api/sre/incidents/inc%201%2Fx/resolve"), str(seen))
    finally:
        mo._cx = real
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()


async def sre_outcomes():
    async with httpx.AsyncClient(base_url=D, headers=ROOT, timeout=10) as cx:
        home = (await cx.get("/api/home")).json()
        listed = (await cx.get("/api/incidents")).json()["incidents"]
        sre_names = [s["name"] for s in (await cx.get("/api/sre/services")).json()["services"]]
        k = (await cx.post("/api/keys", json={"name": "r", "scopes": ["read"]})).json()
    ck("the cell has one open incident", len(listed) == 1 and listed[0]["state"] == "open", str(listed))
    iid = listed[0]["id"]
    read_key = {"Authorization": f"Bearer {k['secret']}"}

    async def state():
        async with httpx.AsyncClient(base_url=D, headers=ROOT, timeout=10) as cx:
            return (await cx.get(f"/api/incidents/{iid}")).json()["state"]

    broken = json.loads(await call("what_is_broken", {}))
    ck("what_is_broken keeps the home headline",
       broken["headline"] == home["headline"] and home["headline"]["text"], str(broken.get("headline")))
    ck("what_is_broken says whether it is watching", "on" in broken["watch"] and "reason_words" in broken["watch"],
       str(broken.get("watch")))
    row = next((i for i in broken["incidents"] if i["id"] == iid), None)
    ck("what_is_broken lists the open incident", row is not None and row["service"] == "edge-proxy"
       and row["title"] and row["state"] == "open" and row["age_words"] and row["who_words"], str(broken))

    svc = json.loads(await call("how_is_service", {"service": "edge-proxy"}))
    ck("how_is_service returns the state", svc["name"] == "edge-proxy" and svc["state"] == "incident"
       and svc["state_words"] and svc["signals_words"] and "blind_spots" in svc, str(svc)[:300])
    miss = await call("how_is_service", {"service": "nope"})
    ck("an unknown service names the ones there are",
       miss == f"No service called nope. Services: {', '.join(sre_names)}." and "edge-proxy" in miss, miss)

    seen = json.loads(await call("what_happened", {"service": "edge-proxy", "window": "1h"}))
    ck("what_happened returns the items of the hour", seen["items"] and all(
        i["time_words"] and i["text"] and i["service"] == "edge-proxy" for i in seen["items"]), str(seen)[:300])
    old = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    past = json.loads(await call("what_happened", {"service": "edge-proxy", "window": "1h", "around": old}))
    ck("the window ends at around", not past["items"] and past["nothing"], str(past)[:300])
    def at(minutes, zone=""):
        return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S") + zone
    # edge-proxy has lines 180 and 240 minutes back; the window is the 30 minutes before around
    hit = json.loads(await call("what_happened", {"service": "edge-proxy", "around": at(170)}))
    ck("a window ending after a line holds it (around with no zone is UTC)", hit["items"], str(hit)[:300])
    miss_before = json.loads(await call("what_happened", {"service": "edge-proxy", "around": at(190, "Z")}))
    ck("a window ending before a line does not", not miss_before["items"] and miss_before["nothing"],
       str(miss_before)[:300])
    ck("a window that is not offered gets a plain sentence",
       await call("what_happened", {"window": "2h"}) == "window must be one of 30m, 1h, 6h, 24h. You sent 2h.")
    ck("an around that is not a time gets a plain sentence",
       (await call("what_happened", {"around": "lunch"})).startswith("around must be an ISO time"))

    ck("a state that is not offered gets a plain sentence",
       await call("mark_incident", {"incident": iid, "state": "fixed"})
       == "state must be one of on_it, resolved, open. You sent fixed." and await state() == "open")
    refused = await call("mark_incident", {"incident": iid, "state": "on_it"}, read_key)
    ck("a read key is refused in the daemon's own words",
       "lacks the" in refused and "scope" in refused and not refused.startswith("error"), refused)
    ck("and the incident is unchanged", await state() == "open")
    got = json.loads(await call("mark_incident", {"incident": iid, "state": "on_it"}))
    ck("on_it takes the incident", got["state"] == "on_it" and got["id"] == iid and await state() == "on_it", str(got))
    got = json.loads(await call("mark_incident", {"incident": iid, "state": "resolved"}))
    ck("resolved closes it", got["state"] == "resolved" and await state() == "resolved", str(got))
    got = json.loads(await call("mark_incident", {"incident": iid, "state": "open"}))
    ck("open reopens it", got["state"] == "open" and await state() == "open", str(got))
    ck("an unknown incident answers in the daemon's words",
       await call("mark_incident", {"incident": "inc_nope", "state": "on_it"}) == "No incident with that id")
    ck("a change the incident's state does not allow is refused in the daemon's words",
       await call("mark_incident", {"incident": iid, "state": "open"}) == "This incident is already open."
       and await state() == "open")
    await call("mark_incident", {"incident": iid, "state": "resolved"})
    refused = await call("mark_incident", {"incident": iid, "state": "on_it"})
    ck("on_it on a resolved incident is refused, not moved",
       refused == "This incident is resolved now; reopen it first." and await state() == "resolved", refused)
    ck("resolving it twice is refused", await call("mark_incident", {"incident": iid, "state": "resolved"})
       == "This incident is already resolved.")
    await call("mark_incident", {"incident": iid, "state": "open"})
    got = json.loads(await call("mark_incident", {"incident": iid, "state": "on_it"}))
    ck("the answer carries the incident's title, service, words and who",
       got["title"] == row["title"] and got["service"] == "edge-proxy" and got["state_words"]
       and got["who"] and got["who"]["name"], str(got))
    after = json.loads(await call("what_is_broken", {}))
    mine = next((i for i in after["incidents"] if i["id"] == iid), None)
    ck("what_is_broken read right after a mark shows the new state",
       mine is not None and mine["state"] == "on_it" and mine["who_words"] != row["who_words"], str(after)[:400])
    await call("mark_incident", {"incident": iid, "state": "resolved"})
    after = json.loads(await call("what_is_broken", {}))
    ck("and a resolved incident leaves the list at once",
       not any(i["id"] == iid for i in after["incidents"]), str(after)[:400])
    await call("mark_incident", {"incident": iid, "state": "open"})

    async with httpx.AsyncClient(base_url=D, headers=ROOT, timeout=10) as cx:
        page = (await cx.get("/api/sre/services/edge-proxy")).json()
    for key in ("name", "state", "state_words", "watch_words", "signals_words", "feeds_words",
                "last_words", "events_24h_words", "incidents_open", "blind_spots_words"):
        ck(f"how_is_service keeps {key} as the page has it", svc.get(key) == page.get(key) and page.get(key) is not None,
           f"{svc.get(key)!r} vs {page.get(key)!r}")
    ck("how_is_service lists the incidents and the blind spots with their words",
       svc["incidents"] and svc["incidents"][0]["id"] == iid and svc["blind_spots"]
       and all(b["title"] and b["detail"] for b in svc["blind_spots"])
       and [b["title"] for b in svc["blind_spots"]] == [b["title"] for b in page["blind_spots"]], str(svc)[:400])

    ck("the window is as long as it says: 6 hours", json.loads(await call(
        "what_happened", {"service": "edge-proxy", "window": "6h"}))["range"] == "Last 6 hours")
    ck("and 24 hours", json.loads(await call("what_happened", {"window": "24h"}))["range"] == "Last 24 hours")
    ck("and 30 minutes is the default", json.loads(await call("what_happened", {}))["range"] == "Last 30 minutes")
    day = json.loads(await call("what_happened", {"window": "24h"}))["items"]
    half = json.loads(await call("what_happened", {}))["items"]
    ck("a day holds more than the last half hour", len(day) > len(half) > 0, f"{len(day)} vs {len(half)}")
    ck("a bad key gets the daemon's status", (await call("what_is_broken", {}, {"Authorization": "Bearer nope"}))
       .startswith("error 401"))


async def platform():
    print("== a cell with no edition ==")
    d, m = _start("")
    try:
        if not await up():
            ck("daemon and mcp up", False); return
        n = await names()
        want = set(OBJECTS) | {"start_project", "hand_project_to_crew"}
        ck("every tool but the two home readers is listed", want <= set(n) and "list_tickets" in n,
           str(sorted(want - set(n))))
        ck("the two tools that read the factory home stay off", not {"what_needs_me", "how_is_project"} & set(n))
        ck("the four AI SRE outcome tools are listed", set(SRE_OUTCOMES) <= set(n), str(sorted(set(SRE_OUTCOMES) - set(n))))
        async with httpx.AsyncClient(base_url=D, headers=ROOT, timeout=10) as cx:
            async def whoami():
                return (await cx.get("/api/whoami")).json()["mcp_object_tools"]
            ck("platform has object tools by default", await whoami() is True)
            await cx.put("/api/settings/mcp", json={"object_tools": False})
            ck("an admin can turn them off", await whoami() is False and "read" not in await names())
            await cx.put("/api/settings/mcp", json={"object_tools": None})
            ck("null brings the default back", await whoami() is True)
    finally:
        _stop(m, d)


async def unreachable():
    print("== the daemon is unreachable ==")
    d, m = _start("factory", daemon=False)
    try:
        if not await up(daemon=False):
            ck("mcp up", False); return
        n = await names()
        ck("it fails open to every tool", set(OBJECTS) | set(OUTCOMES) <= set(n), str(n))
        ck("a call runs rather than answering the off sentence",
           "off on this Tares" not in await call("create_source", {"name": "x"}))
    finally:
        _stop(m, d)


def words():
    print("== the outcome tools follow the words list ==")
    from tares import mcp_server, words as w
    tools = {t.name: t for t in mcp_server.mcp._tool_manager.list_tools()}
    ck("all four are registered", set(OUTCOMES) == set(mcp_server.FACTORY_TOOLS) <= set(tools))
    bad = [(n, w.offenders(n) + w.offenders(tools[n].description or "")) for n in mcp_server.FACTORY_TOOLS]
    ck("no forbidden word or em dash in a name or description", not any(b for _, b in bad), str(bad))
    ck("the four AI SRE tools are registered", set(SRE_OUTCOMES) == set(mcp_server.SRE_TOOLS) <= set(tools))
    bad = [(n, w.offenders(n) + w.offenders(tools[n].description or "")) for n in mcp_server.SRE_TOOLS]
    ck("no forbidden word or em dash in an AI SRE tool name or description", not any(b for _, b in bad), str(bad))


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)
    def json(self):
        return self._body


async def unit():
    print("== the surface cache and the settings order, in process ==")
    from tares import mcp_server as ms, edition as ed

    class Store:
        def __init__(self, v=""): self.v = v
        def get_setting(self, k): return self.v

    saved = os.environ.pop(ed.OBJECT_TOOLS_ENV, None)
    try:
        ck("platform defaults on", ed.object_tools(Store(), "platform") is True)
        ck("factory defaults off", ed.object_tools(Store(), "factory") is False)
        ck("a stored 0 beats the default", ed.object_tools(Store("0"), "platform") is False)
        ck("a stored 1 wins", ed.object_tools(Store("1"), "factory") is True)
        os.environ[ed.OBJECT_TOOLS_ENV] = "1"
        ck("the env turns them on when nothing is stored", ed.object_tools(Store(), "factory") is True)
        ck("a stored 0 beats the env", ed.object_tools(Store("0"), "factory") is False)
        os.environ[ed.OBJECT_TOOLS_ENV] = "0"
        ck("the env turns them off on platform", ed.object_tools(Store(), "platform") is False)
    finally:
        os.environ.pop(ed.OBJECT_TOOLS_ENV, None)
        if saved is not None:
            os.environ[ed.OBJECT_TOOLS_ENV] = saved

    calls = []
    answer = {"r": _Resp(200, {"edition": "factory", "mcp_object_tools": False})}

    class FakeCx:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url):
            calls.append(url)
            return answer["r"]

    real = (ms._cx, ms.AUTH_TOKEN, ms._HTTP)
    ms._cx = lambda *a, **k: FakeCx()
    ms._HTTP = False
    old_ttl = os.environ.get("TARES_MCP_SURFACE_TTL")
    try:
        os.environ["TARES_MCP_SURFACE_TTL"] = "30"
        ms._SURFACES.clear(); ms.AUTH_TOKEN = "t1"
        a = await ms._surface(); b = await ms._surface()
        ck("a second look within the TTL reuses the answer", a == b == ("factory", False) and len(calls) == 1, str(calls))
        ms.AUTH_TOKEN = "t2"
        await ms._surface()
        ck("each token has its own answer", len(calls) == 2, str(calls))
        os.environ["TARES_MCP_SURFACE_TTL"] = "0"
        await ms._surface()
        ck("a TTL of 0 asks every time", len(calls) == 3, str(calls))
        os.environ["TARES_MCP_SURFACE_TTL"] = "junk"
        await ms._surface()
        ck("a TTL that is not a number falls back to 30 s", len(calls) == 3, str(calls))
        os.environ["TARES_MCP_SURFACE_TTL"] = "30"
        ms._SURFACES.clear()
        for i in range(260):
            ms.AUTH_TOKEN = f"k{i}"
            await ms._surface()
        ck("the cache is emptied past 256 tokens", len(ms._SURFACES) < 260, str(len(ms._SURFACES)))
        ms._SURFACES.clear(); ms.AUTH_TOKEN = "t9"
        answer["r"] = _Resp(401, {})
        ck("a 401 gives the unknown edition and every tool", await ms._surface() == ("", True))
        ms._SURFACES.clear()
        answer["r"] = _Resp(200, {"edition": "factory"})
        ck("a body with no mcp_object_tools fails open", await ms._surface() == ("", True))
        ck("an unknown edition shows the factory tools and the building blocks",
           ms._visible("what_needs_me", "", True, False) and ms._visible("read", "", True, False))
    finally:
        ms._cx, ms.AUTH_TOKEN, ms._HTTP = real
        ms._SURFACES.clear()
        if old_ttl is None:
            os.environ.pop("TARES_MCP_SURFACE_TTL", None)
        else:
            os.environ["TARES_MCP_SURFACE_TTL"] = old_ttl


async def main():
    words()
    await unit()
    await sre_unit()
    await factory()
    await sre()
    await platform()
    await unreachable()
    print(f"\n{P} passed, {F} failed")

asyncio.run(main())
sys.exit(1 if F else 0)
