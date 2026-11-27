"""Seed a scratch Tares for factory-home-joiner.e2e.cjs (TR-1723): a factory cell one person has set up
and a second person about to join it, written through the running daemon's API only.
  People:    Robin, the cell's first person (`local`, claimed), with an admin key. Sam, with a key that
             reads and writes but is no admin.
  Team:      the crew's choices saved for everyone: a person approves each merge, 3 builders, no
             releases, second check on.
  Crews:     Robin's orchestrator and reviewer, heard just now. Sam's machine has reported nothing.
  Projects:  Checkout and Search, team projects Robin leads, and Payroll, a private one Robin leads.
             Sam is on none of them.
usage: python seed-factory-home-joiner.py <port> <root-token>
Prints a JSON map on the last line: keys and people by first name, and the projects."""
import json, sys, urllib.error, urllib.request
from datetime import datetime, timezone

port, root = sys.argv[1], sys.argv[2]
base = f"http://127.0.0.1:{port}"


def call(method, path, body=None, token=root, raw=None):
    data = raw.encode() if raw is not None else None if body is None else json.dumps(body).encode()
    kind = "application/x-ndjson" if raw is not None else "application/json"
    r = urllib.request.Request(base + path, data, {"content-type": kind, "authorization": f"Bearer {token}"},
                               method=method)
    try:
        return json.load(urllib.request.urlopen(r))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {path} -> {e.code} {e.read().decode()[:300]}")


owner = call("POST", "/api/people", {"owner": True, "email": "robin-seed@example.com", "name": "Robin Owner"})
if owner["id"] != "local":
    raise SystemExit(f"the first person was not claimed: {owner['id']}")
robin = call("POST", "/api/keys", {"name": "robin-laptop", "scopes": ["admin"], "person_id": "local"})
sam = call("POST", "/api/keys", {"name": "sam-laptop", "scopes": ["read", "ingest"],
                                 "person": {"email": "sam-seed@example.com", "name": "Sam Member"}})
keys = {"robin": robin["secret"], "sam": sam["secret"]}
people = {"robin": "local", "sam": sam["person"]["id"]}

call("PUT", "/api/crew/settings", {"autonomy": "L3-review", "releaser": False, "builders_max": 3, "challenger": True})

call("POST", "/api/sources", {"name": "claude_code", "connector": "claude_code", "poll": "10s", "config": {"push": True}})
stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
lines = []
for role in ("orchestrator", "reviewer"):
    sid, name = f"s-robin-{role}", f"robin-{role}"
    lines.append({"type": "session_station", "sessionId": sid, "cwd": "/w/crew", "timestamp": stamp,
                  "station": name, "role": role, "parent": "", "factory_station": name,
                  "factory_crew": "robin", "crew": "robin", "factory_machine": "robin-laptop", "machine": "robin-laptop"})
    lines.append({"type": "session_state", "sessionId": sid, "cwd": "/w/crew", "timestamp": stamp,
                  "state": "idle", "reason": ""})
call("POST", "/ingest/claude_code", raw="".join(json.dumps(x) + "\n" for x in lines), token=keys["robin"])


def project(name, goal):
    return call("POST", "/api/projects", {"template": "custom", "name": name, "goal": goal, "objects": [],
                                          "kind": "software_factory"}, keys["robin"])["id"]


projects = {"checkout": project("Checkout", "A checkout page that keeps the cart."),
            "search": project("Search", "Find an order by its number."),
            "payroll": project("Payroll", "Pay slips by the first of the month.")}
call("PATCH", f"/api/projects/{projects['payroll']}", {"scope": "private"}, keys["robin"])

print(json.dumps({"keys": keys, "people": people, "projects": projects}))
