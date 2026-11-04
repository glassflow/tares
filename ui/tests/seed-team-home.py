"""Seed a scratch Tares for team-home.e2e.cjs (TR-741): a factory cell of three people with a
thing of every kind waiting for Alex, written through the running daemon's API only.
  People:     Alex, Kim and Lee, each with a key. Kim owns acme/api, Alex owns acme/web.
  Shop:       a project Kim leads, a ticket on acme/web and one on acme/api (merged).
  Waiting for Alex:
    question   Kim's spec asks about acme/web (the Answer box, free text)
    sign-off   the web ticket needs Alex's sign-off before Shop goes to a crew
    grants     two that Kim gave over acme/web: one to agree to, one to decline
    desk       a permission request from a builder (Allow, Answer, "stand for next time"),
               a question with two options, and a long question for the long-text state
  Lee:        has nothing waiting; the page shows "Nothing waits for you".
  Team (TR-761): Alex leads Portal (acme/api, Kim has not signed off; acme/docs, no owner) and Notes (acme/web, his
              own; acme/api, Kim has not signed off), so he waits on Kim for two repos. Shop gets a ticket on
              acme/docs. Kim leads Billing, Lee leads Search, Mobile and Vault; Vault is private and Alex is not on
              it. Alex sees Shop, Portal and Notes under Mine and six under Team, plus one hidden stub.
  Crews:      Alex's crew (an orchestrator and a builder on Notes) and Kim's were heard a few minutes ago, Lee's two days ago (so it is "not seen in a day").
usage: python seed-team-home.py <port> <root-token>
Prints a JSON map on the last line: keys and people by first name, project, question, grants and desk labels."""
import json, sys, urllib.error, urllib.request
from datetime import datetime, timedelta, timezone

port, root = sys.argv[1], sys.argv[2]
base = f"http://127.0.0.1:{port}"


def call(method, path, body=None, token=root):
    data = None if body is None else json.dumps(body).encode()
    r = urllib.request.Request(base + path, data, {"content-type": "application/json", "authorization": f"Bearer {token}"},
                               method=method)
    try:
        return json.load(urllib.request.urlopen(r))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {path} -> {e.code} {e.read().decode()[:300]}")


keys, people = {}, {}
for who in ("alex", "kim", "lee"):
    k = call("POST", "/api/keys", {"name": f"{who}-laptop", "scopes": ["admin"],
                                   "person": {"email": f"{who}-seed@example.com", "name": who.capitalize()}})
    keys[who], people[who] = k["secret"], k["person"]["id"]
for area, who in (("acme/api", "kim"), ("acme/web", "alex")):
    call("PUT", f"/api/areas/{area}", {"person_id": people[who]}, keys["kim"])

shop = call("POST", "/api/projects", {"template": "custom", "name": "Shop", "objects": [],
                                      "kind": "software_factory"}, keys["kim"])["id"]
call("POST", f"/api/projects/{shop}/tickets", {"title": "Web page", "repo": "acme/web"}, keys["kim"])
done = call("POST", f"/api/projects/{shop}/tickets", {"title": "Api route", "repo": "acme/api"}, keys["kim"])
call("POST", f"/api/projects/{shop}/tickets/{done['id']}/stage", {"stage": "merged", "reason": "seeded"}, keys["kim"])

question = call("POST", f"/api/projects/{shop}/questions",
                {"repo": "acme/web", "text": "Which page does the checkout live on?"}, keys["kim"])["id"]

grants = []
for words in ("merge to acme/web", "read the logs of acme/web"):
    g = call("POST", "/api/grants", {"kind": "project", "project": shop, "words": words, "areas": ["acme/web"]}, keys["kim"])
    grants.append(g["grant"]["id"])

LONG = ("Should the checkout keep the cart in the browser, so a customer who closes the tab loses it, or in the "
        "account, so it follows them to another device? If it lives in the browser the page needs no sign-in and "
        "the work is one ticket. If it lives in the account the page needs a sign-in first, the cart needs a table, "
        "and an anonymous visitor needs a way to merge their cart into the account when they sign in later. "
        "Please name the one you want and say why, so the tickets can be written once.")
desk = {}
for name, body in {
    "allow": {"question": "shop-build-m1 was refused a command. Allow it?", "recommendation": "shop-build-m1 may read the status of a commit",
              "why": "it needs it to wait for CI", "project": shop, "station": "shop-build-m1",
              "command": "gh api repos/acme/web/statuses/abc", "to": people["alex"]},
    "options": {"question": "Which colour for the links?", "options": ["blue", "green", "b!"], "recommendation": "blue",
                "why": "it reads best", "project": shop, "to": people["alex"]},
    "long": {"question": LONG, "recommendation": "the account", "why": "it follows the customer",
             "project": shop, "to": people["alex"]},
}.items():
    desk[name] = call("POST", "/api/desk", body)["label"]

# ── TR-761: the team home ──
call("POST", f"/api/projects/{shop}/tickets", {"title": "Docs page", "repo": "acme/docs"}, keys["kim"])


def project(who, name, **extra):
    return call("POST", "/api/projects", {"template": "custom", "name": name, "objects": [], "kind": "software_factory",
                                          **extra}, keys[who])["id"]


portal = project("alex", "Portal")
call("POST", f"/api/projects/{portal}/tickets", {"title": "Portal api", "repo": "acme/api"}, keys["alex"])
call("POST", f"/api/projects/{portal}/tickets", {"title": "Portal docs", "repo": "acme/docs"}, keys["alex"])
notes = project("alex", "Notes")
call("POST", f"/api/projects/{notes}/tickets", {"title": "Notes page", "repo": "acme/web"}, keys["alex"])
call("POST", f"/api/projects/{notes}/tickets", {"title": "Notes api", "repo": "acme/api"}, keys["alex"])
billing = project("kim", "Billing")
search = project("lee", "Search")
mobile = project("lee", "Mobile")
PRIVATE = {"name": "Vault", "goal": "Rotate the launch keys"}
vault = project("lee", PRIVATE["name"], goal=PRIVATE["goal"])
call("PATCH", f"/api/projects/{vault}", {"scope": "private"}, keys["lee"])

# the crews: each person's lines go in with their own key; Lee's last line is two days old
call("POST", "/api/sources", {"name": "claude_code", "connector": "claude_code", "poll": "10s", "config": {"push": True}})


def stamp(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def push(who, lines):
    body = ("\n".join(json.dumps(x) for x in lines) + "\n").encode()
    r = urllib.request.Request(base + "/ingest/claude_code", body, {"content-type": "application/x-ndjson",
                               "authorization": f"Bearer {keys[who]}"}, method="POST")
    try:
        urllib.request.urlopen(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"ingest for {who} -> {e.code} {e.read().decode()[:300]}")


def crew(who, tag, machine, minutes_ago, state, role="orchestrator", project_id=""):
    sid = f"{tag}-{role}"
    line = {"type": "session_station", "sessionId": sid, "cwd": "/w/crew", "timestamp": stamp(minutes_ago),
            "station": f"{tag}-{role}", "role": role, "parent": "", "factory_station": f"{tag}-{role}",
            "factory_crew": tag, "crew": tag, "factory_machine": machine, "machine": machine}
    if project_id:
        line["tares_project"] = project_id
    push(who, [line, {"type": "session_state", "sessionId": sid, "cwd": "/w/crew", "state": state, "reason": "",
                      "timestamp": stamp(minutes_ago)}])


crew("alex", "a", "alex-mbp", 4, "idle")
crew("alex", "a", "alex-mbp", 4, "working", "builder", notes)
crew("kim", "k", "kim-mbp", 6, "idle")
crew("lee", "l", "lee-mbp", 2 * 24 * 60, "idle")

print(json.dumps({"keys": keys, "people": people, "project": shop, "question": question, "grants": grants, "desk": desk,
                  "projects": {"shop": shop, "portal": portal, "notes": notes, "billing": billing, "search": search,
                               "mobile": mobile, "vault": vault}, "private": PRIVATE}))
