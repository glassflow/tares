"""Seed a scratch factory Tares for factory-crew.e2e.cjs (TR-736): the Crew page as a cell with a crew at
work shows it. Invented names only.
  project-x (open): 4 tickets; builder m1 works on one (and has a helper), builder m2 waits for the review
  of another, an old builder has ended;
  project-y (open): one builder not heard from for two hours;
  project-d (finished): its builder has ended, so the page leaves it out;
  the one you talk to works, the reviewer is idle, the releaser idle for two hours;
  the Claude plan is at 1% and 11%; two rule proposals are open (one with markup in its text).
Starts a daemon on the scratch data dir with TARES_EDITION=factory, creates the projects and tickets and sends the
crew's activity through its API, stops it, then writes the rule proposals straight into the database.
Prints a JSON map of names to ids on the last line.
usage: python seed-factory-crew.py <data-dir> <port> <mcp-port> <ui-dist> <tares-bin>"""
import json, os, subprocess, sys, time, urllib.error, urllib.request
from datetime import datetime, timedelta, timezone

home, port, mcp, dist, tares = sys.argv[1:6]
env = {**os.environ, "PYTHONPATH": os.getcwd(), "TARES_HOME": home, "TARES_PORT": port, "TARES_MCP_PORT": mcp,
       "TARES_UI_DIST": dist, "TARES_EDITION": "factory", "TARES_OTLP_GRPC_PORT": "off"}
for k in ("TARES_AUTH_TOKEN", "TARES_DB", "TARES_CATALOG", "TARESD_URL"):
    env.pop(k, None)
base = f"http://127.0.0.1:{port}"


def call(path, body=None, method=None, raw=None, ctype="application/json"):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    r = urllib.request.Request(base + path, data, {"content-type": ctype}, method=method)
    try:
        return json.load(urllib.request.urlopen(r))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{path} -> {e.code} {e.read().decode()[:300]}")


def ts(seconds_ago):
    t = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def st_line(sid, station, role, parent="", project="", ago=3600):
    o = {"type": "session_station", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago),
         "station": station, "role": role, "parent": parent, "factory_station": station}
    if project:
        o["tares_project"] = project
    return o


def state(sid, s, ago):
    return {"type": "session_state", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago), "state": s, "reason": ""}


def context(sid, pct, ago=5):
    return {"type": "session_status", "sessionId": sid, "timestamp": ts(ago),
            "status": {"session_id": sid, "context_window": {
                "context_window_size": 200000, "used_percentage": pct,
                "current_usage": {"input_tokens": 2, "output_tokens": 40,
                                  "cache_creation_input_tokens": 300, "cache_read_input_tokens": 90000}}}}


def reply(sid, mid, ago, project="", text="ok"):
    o = {"type": "assistant", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago),
         "message": {"id": mid, "role": "assistant", "model": "claude-opus-5-5", "content": text,
                     "usage": {"input_tokens": 2, "output_tokens": 10, "cache_creation_input_tokens": 1000,
                               "cache_read_input_tokens": 50000,
                               "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 1000}}}}
    if project:
        o["tares_project"] = project
    return o


def plan_line(session_pct, weekly_pct):
    now = datetime.now(timezone.utc)
    return {"type": "claude_plan", "timestamp": ts(1), "usage_report": {
        "session": {"total_cost_usd": 0},
        "rate_limits": {"limits": [
            {"kind": "session", "group": "session", "percent": session_pct, "scope": None,
             "resets_at": (now + timedelta(hours=2)).isoformat(), "severity": "normal", "is_active": True},
            {"kind": "weekly_all", "group": "weekly", "percent": weekly_pct, "scope": None,
             "resets_at": (now + timedelta(days=2)).isoformat(), "severity": "normal", "is_active": False}],
            "extra_usage": {"is_enabled": False, "monthly_limit": None}}}}


def ingest(lines):
    call("/ingest/claude_code", raw=("\n".join(json.dumps(x) for x in lines) + "\n").encode(),
         ctype="application/x-ndjson")


daemon = subprocess.Popen([tares, "up", "--port", port, "--data-dir", home], env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
ids = {}
try:
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/health", timeout=1)
            break
        except Exception:
            time.sleep(0.3)
    else:
        raise SystemExit("daemon did not start")
    for name in ("project-x", "project-y", "project-d"):
        ids[name] = call("/api/projects", {"template": "custom", "name": name, "objects": [], "kind": "software_factory",
                                           "goal": f"A project for the crew page ({name})."})["id"]
    px, py, pd = ids["project-x"], ids["project-y"], ids["project-d"]
    for uid, titles in ((px, ("Home page", "Footer", "Search box", "Search index")), (py, ("Export",)), (pd, ("Old",))):
        for t in titles:
            call(f"/api/projects/{uid}/tickets", {"title": t, "repo": "example-org/docs-site"})
    for uid in (px, py, pd):
        call(f"/api/projects/{uid}/handover", {"repos": [{"path": "/w/docs-site", "name": "example-org/docs-site"}]})
    call(f"/api/projects/{pd}/tickets/T1/stage", {"stage": "merged"})
    call(f"/api/projects/{pd}/finish", {"words": "done"})
    call(f"/api/projects/{px}/tickets/T1/stage", {"stage": "merged"})
    call(f"/api/projects/{px}/tickets/T2/assign", {"holder": "project-x-build-m1"})
    call(f"/api/projects/{px}/tickets/T2/stage", {"stage": "doing"})
    call(f"/api/projects/{px}/tickets/T3/assign", {"holder": "project-x-build-m2"})
    call(f"/api/projects/{px}/tickets/T3/stage", {"stage": "doing"})
    call(f"/api/projects/{px}/tickets/T3/stage", {"stage": "review"})
    ingest([
        st_line("o1", "crew-orchestrator", "orchestrator"), state("o1", "working", 12), context("o1", 82), reply("o1", "o-1", 30),
        st_line("r1", "crew-reviewer", "reviewer"), state("r1", "idle", 70), context("r1", 41),
        st_line("l1", "crew-releaser", "releaser"), state("l1", "idle", 7200),
        st_line("b1", "project-x-build-m1", "builder", "crew-orchestrator", project=px),
        state("b1", "working", 20), context("b1", 58), reply("b1", "m1", 30, px),
        reply("b1", "m2", 25, px, "Reading <script>alert(1)</script> & more"),
        st_line("h1", "project-x-build-m1-lint", "helper", "project-x-build-m1", project=px), state("h1", "working", 10),
        st_line("b2", "project-x-build-m2", "builder", "crew-orchestrator", project=px),
        state("b2", "idle", 480), context("b2", 63),
        st_line("b3", "project-x-build-old", "builder", "crew-orchestrator", project=px), state("b3", "ended", 86400),
        st_line("b4", "project-d-build-m1", "builder", "crew-orchestrator", project=pd), state("b4", "ended", 172800),
        st_line("b5", "project-y-build-m1", "builder", "crew-orchestrator", project=py, ago=7200),
        state("b5", "working", 7200), context("b5", 88),
        plan_line(1, 11),
    ])
finally:
    daemon.terminate()
    daemon.wait()

sys.path.insert(0, os.getcwd())
from tares.store import Store  # noqa: E402

store = Store(os.path.join(home, "tares.duckdb"))
now = datetime.now(timezone.utc)
store.upsert_rule_proposal("example-org/docs-site", "missing tests", py, "project",
                           "In example-org/docs-site, check for missing tests before asking for review: it came back in 3 tickets.",
                           ["T1", "T2"], now - timedelta(days=2))
store.upsert_rule_proposal("example-org/service-a", "markup", None, "all", "Never merge <i>on a red check</i>.",
                           ["T9"], now - timedelta(hours=6))
store.con.close()
print(json.dumps(ids))
