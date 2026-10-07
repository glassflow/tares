"""Seed a local Tares with a crew-built demo project ("Bakery orders"), through the real API only,
the way stations would: a plan with milestones, a crew (reviewer revived once, a builder), the
ledger, checks (one not in the recording), assumptions, a review round with changes then a pass,
the desk with open questions and one answered decision. For looking at the factory page, the Crew
page and a ticket. Run: scripts/factory-dev.sh seed (re-running replaces the project). Needs an
open (no auth) Tares; the URL is the first argument, default http://127.0.0.1:8787."""
import json
import sys
from datetime import datetime, timedelta, timezone

import httpx

T = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787").rstrip("/")
NAME = "Bakery orders"


def ts(minutes_ago=0.0):
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def call(method, path, station="", role="", parent="", **kw):
    h = {}
    if station:
        h = {"X-Tares-Station": station, "X-Tares-Role": role}
        if parent:
            h["X-Tares-Parent"] = parent
    r = httpx.request(method, T + path, headers=h, timeout=30, **kw)
    if r.status_code >= 400:
        print("ERR", method, path, r.status_code, r.text[:300])
    return r.json() if r.content else {}


def ingest(lines):
    r = httpx.post(T + "/ingest/claude_code", content="\n".join(json.dumps(x) for x in lines) + "\n",
                   headers={"Content-Type": "application/x-ndjson"}, timeout=30)
    if r.status_code >= 300:
        print("ERR ingest", r.status_code, r.text[:200])


existing = [p for p in call("GET", "/api/projects")["projects"] if p["name"] == NAME]
for p in existing:
    call("DELETE", f"/api/projects/{p['id']}")
p = call("POST", "/api/projects", json={"template": "custom", "kind": "software_factory", "name": NAME,
                                        "goal": "Customers order cakes online; the bakery sees orders by pickup day",
                                        "objects": []})
uid = p["id"]
call("POST", "/api/sources", json={"name": "claude_code", "connector": "claude_code", "poll": "10s",
                                   "config": {"push": True}, "project": uid})

# the plan
call("POST", f"/api/projects/{uid}/docs", json={"kind": "spec", "title": "Spec",
     "body": "# Bakery orders\n\nCustomers order cakes online and pick a pickup day. The bakery sees "
             "orders grouped by day.\n\nOut of scope: payments online (cash at pickup)."})
call("POST", f"/api/projects/{uid}/docs", json={"kind": "agents", "title": "AGENTS.md",
     "body": "# AGENTS.md\n\nRepo: ~/code/bakery (branch main). `make test`, `make lint`.\n\n## Release\n"
             "`make deploy-kind`, check `curl localhost:8080/health`."})
call("POST", f"/api/projects/{uid}/docs", json={"kind": "start", "title": "Start here",
     "body": "Join the crew: tares-factory join snippet goes here."})
call("POST", f"/api/projects/{uid}/milestones", json={
    "name": "Take orders", "goal": "A customer can order a cake for a pickup day",
    "checks": [{"check": "curl -s localhost:8080/orders -d @order.json", "expect": "201 and an order id"},
               {"check": "open /order in a browser", "expect": "the form lists the next 14 days"}]})
call("POST", f"/api/projects/{uid}/milestones", json={
    "name": "Bakery view", "goal": "The bakery sees orders by pickup day",
    "checks": [{"check": "open /admin/orders", "expect": "orders grouped by day, oldest first"}]})
tickets = [("Order model and migration", "Take orders", []),
           ("Order form with pickup day", "Take orders", ["T1"]),
           ("Order confirmation email", "Take orders", ["T2"]),
           ("Orders by day page", "Bakery view", ["T1"]),
           ("Mark an order picked up", "Bakery view", ["T4"])]
for title, ms, deps in tickets:
    t = call("POST", f"/api/projects/{uid}/tickets", json={"title": title, "milestone": ms, "depends_on": deps})
    call("POST", f"/api/projects/{uid}/docs", json={"kind": "working", "title": f"Working: {title}",
         "body": f"## Goal\n{title}.\n\n## Definition of done\n- tests pass\n\n## Progress\n"})
docs = {d["title"]: d["id"] for d in call("GET", f"/api/projects/{uid}/docs")}
for i, (title, _, _) in enumerate(tickets, 1):
    call("PUT", f"/api/projects/{uid}/tickets/T{i}", json={"working_doc": docs[f"Working: {title}"]})

# the crew settings and grants, as the first `factory crew up` saves them
call("PUT", "/api/crew/settings", json={"autonomy": "L4-ship", "release_profile": "kind",
                                        "prod_pattern": "prod-*", "challenger": True, "builders_max": 2})
call("POST", "/api/grants", json={"words": "Merging after the reviewer passes it is fine", "scope": "all"})

# the crew reports in: orchestrator, reviewer (revived once), releaser, a builder for this project
ingest([
    {"type": "session_station", "sessionId": "seed-o1", "cwd": "/Users/me/factory", "timestamp": ts(90),
     "station": "crew-orchestrator", "role": "orchestrator", "factory_station": "crew-orchestrator"},
    {"type": "assistant", "sessionId": "seed-o1", "cwd": "/Users/me/factory", "timestamp": ts(2),
     "factory_station": "crew-orchestrator", "message": {"role": "assistant", "content": "D2 is waiting for you."}},
    {"type": "session_state", "sessionId": "seed-o1", "cwd": "/Users/me/factory", "timestamp": ts(2),
     "state": "waiting", "reason": "the turn ended; the session waits for input"},
    {"type": "session_station", "sessionId": "seed-r1", "cwd": "/Users/me/factory", "timestamp": ts(85),
     "station": "crew-reviewer", "role": "reviewer"},
    {"type": "session_state", "sessionId": "seed-r1", "cwd": "/Users/me/factory", "timestamp": ts(60), "state": "ended"},
    {"type": "session_station", "sessionId": "seed-r2", "cwd": "/Users/me/factory", "timestamp": ts(40),
     "station": "crew-reviewer", "role": "reviewer"},
    {"type": "assistant", "sessionId": "seed-r2", "cwd": "/Users/me/factory", "timestamp": ts(1),
     "message": {"role": "assistant", "content": "Reviewing T2"}},
    {"type": "session_state", "sessionId": "seed-r2", "cwd": "/Users/me/factory", "timestamp": ts(1), "state": "working"},
    {"type": "session_station", "sessionId": "seed-x1", "cwd": "/Users/me/factory", "timestamp": ts(80),
     "station": "crew-releaser", "role": "releaser"},
    {"type": "session_state", "sessionId": "seed-x1", "cwd": "/Users/me/factory", "timestamp": ts(50), "state": "waiting"},
    {"type": "session_station", "sessionId": "seed-b1", "cwd": "/Users/me/code/bakery", "timestamp": ts(70),
     "station": "bakery-orders-build-m1", "role": "builder", "parent": "crew-orchestrator",
     "tares_project": NAME},
    {"type": "session_project", "sessionId": "seed-b1", "cwd": "/Users/me/code/bakery", "timestamp": ts(70),
     "tares_project": NAME, "from_env": True},
])
call("POST", f"/api/projects/{uid}/handover", json={"repo": "/Users/me/code/bakery", "by": "spec session"})

O = ("crew-orchestrator", "orchestrator")
B = ("bakery-orders-build-m1", "builder", "crew-orchestrator")
R = ("crew-reviewer", "reviewer")
for ref in ("T1", "T2", "T3"):
    call("POST", f"/api/projects/{uid}/tickets/{ref}/assign", *O, json={"holder": B[0]})
call("POST", f"/api/projects/{uid}/tickets/T1/stage", *B, json={"stage": "doing"})
ingest([
    {"type": "assistant", "sessionId": "seed-b1", "cwd": "/Users/me/code/bakery", "timestamp": ts(0.5),
     "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "sb1", "name": "Bash",
                                                    "input": {"command": "make test"}}]}},
    {"type": "user", "sessionId": "seed-b1", "cwd": "/Users/me/code/bakery", "timestamp": ts(0.4),
     "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "sb1",
                                               "content": "===== 9 passed in 0.8s ====="}]}},
    {"type": "assistant", "sessionId": "seed-b1", "cwd": "/Users/me/code/bakery", "timestamp": ts(0.3),
     "message": {"role": "assistant", "content": "T1 done, sending it for review"}},
    {"type": "session_state", "sessionId": "seed-b1", "cwd": "/Users/me/code/bakery", "timestamp": ts(0.3),
     "state": "working"},
])
call("POST", f"/api/projects/{uid}/tickets/T1/checks", *B, json={"command": "make test", "result": "9 passed",
                                                                  "commit": "3f2a9c1d"})
call("POST", f"/api/projects/{uid}/tickets/T1/checks", *B, json={"command": "make e2e", "result": "all green",
                                                                  "commit": "3f2a9c1d"})
call("POST", f"/api/projects/{uid}/assumptions", *B, json={"question": "How far ahead can a customer order?",
     "choice": "14 days", "why": "the spec says 'the next two weeks' in passing", "tickets": ["T2"]})
call("POST", f"/api/projects/{uid}/assumptions", *B, json={"question": "Time zone for pickup days?",
     "choice": "Europe/Berlin", "why": "the bakery is in Berlin", "tickets": ["T1", "T4"]})
call("POST", f"/api/projects/{uid}/memory/lines", *B, json={"text": "make test needs Postgres from docker compose up db"})
call("POST", f"/api/projects/{uid}/tickets/T1/stage", *B, json={"stage": "review",
     "pr": "https://github.com/acme/bakery/pull/12"})
call("POST", f"/api/projects/{uid}/tickets/T1/reviews", *R, json={
    "head": "3f2a9c1d", "verdict": "changes",
    "verified": "make test: 9 passed. Broke Order.pickup_day validation: test_pickup_day failed, then restored.",
    "not_verified": "No load test.",
    "findings": [{"severity": "P2", "file": "db/migrations/001_orders.sql", "line": 4,
                  "text": "the migration has no index on pickup_day, the bakery view sorts by it"},
                 {"severity": "P3", "text": "order ids are sequential and guessable", "blocking": False}]})
call("POST", f"/api/projects/{uid}/tickets/T1/stage", *B, json={"stage": "changes"})
call("POST", f"/api/projects/{uid}/tickets/T1/reviews", *R, json={
    "head": "77ab01e2", "verdict": "pass", "verified": "make test: 10 passed. Index present.",
    "resolved": {"F1": "fixed"}})
call("POST", f"/api/projects/{uid}/tickets/T1/stage", *B, json={"stage": "merged"})
call("POST", f"/api/projects/{uid}/tickets/T2/stage", *B, json={"stage": "doing"})

# the desk: a blocking question, a batch to confirm, one answered
d1 = call("POST", "/api/desk", *O, json={"question": "Confirmation email: send from the bakery's own address or ours?",
          "recommendation": "the bakery's address", "why": "customers reply to it with questions",
          "options": ["bakery's address", "noreply"], "blocks": "bakery-orders-build-m1 on T3",
          "tickets": ["T3"], "blocking": True, "project": uid})
d2 = call("POST", "/api/desk", *O, json={"question": "Keep these assumptions?", "recommendation": "keep both",
          "why": "both are safe defaults", "assumptions": ["A1", "A2"], "project": uid})
d3 = call("POST", "/api/desk", *O, json={"question": "Staging deploys: ask each time?",
          "recommendation": "no, a standing yes", "why": "staging is cheap to undo", "project": uid})
ingest([{"type": "user", "sessionId": "seed-o1", "cwd": "/Users/me/factory", "timestamp": ts(-0.05),
         "message": {"role": "user", "content": "Always fine to deploy to staging"}}])
print(call("POST", f"/api/desk/{d3['label']}/answer", *O, json={"words": "Always fine to deploy to staging",
                                                                   "standing": True}).get("matched"))
print(json.dumps({"project": uid, "url": f"{T}/projects/{uid}"}))
