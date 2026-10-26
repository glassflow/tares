"""Seed a scratch Tares for project-idle.e2e.cjs (TR-693): two software factory projects, each with
a ticket in progress held by a builder station, whose session is driven by the plugin's real hook
script (ship.py) against the running daemon.
  fb-90a1: the builder ends its turn while its own Monitor runs: Stop, then Claude Code's idle notice.
  Billing export: the builder is stopped at a permission prompt.
  Slow build: the builder has been idle for 30 minutes (past the 10-minute working window, inside
  the 90-minute one): still no banner.
usage: python seed-project-idle.py <port> <ship.py> <scratch-dir>
Prints a JSON map of project name to id on the last line."""
import json, os, subprocess, sys, urllib.request
from datetime import datetime, timedelta, timezone

port, ship, scratch = sys.argv[1:4]
base = f"http://127.0.0.1:{port}"


def post(path, body):
    r = urllib.request.Request(base + path, json.dumps(body).encode(), {"content-type": "application/json"})
    return json.load(urllib.request.urlopen(r))


def stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def line(role, sid, text):
    return json.dumps({"type": role, "sessionId": sid, "timestamp": stamp(), "cwd": "/w/repo",
                       "message": {"role": role, "content": [{"type": "text", "text": text}]}})


def hooks(station, uid, sid, said, events):
    """Run ship.py once per hook event, as Claude Code would, appending to the transcript."""
    data = os.path.join(scratch, station)
    os.makedirs(data, exist_ok=True)
    transcript = os.path.join(data, "transcript.jsonl")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TARES_", "FACTORY_", "CLAUDE_PLUGIN"))}
    env.update(TARESD_URL=base, CLAUDE_PLUGIN_DATA=data, FACTORY_STATION=station, FACTORY_ROLE="builder",
               FACTORY_PARENT="crew-orchestrator", TARES_PROJECT=uid, HOME=data)
    open(transcript, "w").write(line("user", sid, "Take the ticket and build it.") + "\n")
    for ev, extra in events:
        if ev == "Stop":
            open(transcript, "a").write(line("assistant", sid, said) + "\n")
        hook = {"hook_event_name": ev, "session_id": sid, "cwd": "/w/repo", "transcript_path": transcript, **extra}
        subprocess.run([sys.executable, ship], input=json.dumps(hook), text=True, env=env, check=True,
                       stdout=subprocess.DEVNULL)


def project(name, station, title):
    uid = post("/api/projects", {"template": "custom", "name": name, "objects": [], "kind": "software_factory"})["id"]
    post(f"/api/projects/{uid}/handover", {"repos": [{"path": "/w/repo", "name": "acme/repo"}]})
    t = post(f"/api/projects/{uid}/tickets", {"title": title, "status": "in_progress"})
    post(f"/api/projects/{uid}/tickets/{t['id']}/assign", {"holder": station})
    post(f"/api/projects/{uid}/tickets/{t['id']}/stage", {"stage": "doing"})
    return uid


ids = {}
a = "fb-90a1-build-m1-argus-core"
ids["fb-90a1"] = project("fb-90a1", a, "T3 Unsubscribe from alert emails")
hooks(a, ids["fb-90a1"], "sess-idle",
      "Waiting on the T3 e2e result; the Monitor will notify me when it lands.",
      [("SessionStart", {}), ("UserPromptSubmit", {}), ("PostToolUse", {"tool_name": "Monitor"}), ("Stop", {}),
       ("Notification", {"notification_type": "idle_prompt", "message": "Claude is waiting for your input"})])
b = "billing-export-build-t4-api"
ids["Billing export"] = project("Billing export", b, "T4 CSV export endpoint")
hooks(b, ids["Billing export"], "sess-perm",
      "I need to run the migration against the scratch database.",
      [("SessionStart", {}), ("UserPromptSubmit", {}), ("PostToolUse", {"tool_name": "Read"}),
       ("PermissionRequest", {"tool_name": "Bash"})])   # a background station may get no Notification
c = "slow-build-t5-api"
ids["Slow build"] = project("Slow build", c, "T5 Retry the upload")
t = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
old = [{"type": "session_station", "sessionId": "sess-slow", "cwd": "/w/repo", "timestamp": t, "station": c,
        "role": "builder", "parent": "crew-orchestrator", "tares_project": ids["Slow build"]},
       {"type": "session_project", "sessionId": "sess-slow", "cwd": "/w/repo", "timestamp": t,
        "tares_project": ids["Slow build"]},
       {"type": "session_state", "sessionId": "sess-slow", "cwd": "/w/repo", "timestamp": t,
        "state": "idle", "reason": "the turn ended", "tares_project": ids["Slow build"]}]
r = urllib.request.Request(base + "/ingest/claude_code", "\n".join(json.dumps(o) for o in old).encode() + b"\n",
                           {"content-type": "application/x-ndjson"})
urllib.request.urlopen(r).read()
print(json.dumps(ids))
