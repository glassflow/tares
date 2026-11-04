"""Seed a scratch Tares for factory-home.e2e.cjs (TR-732): a factory cell with something on every
row of the live home page, written through the running daemon's API only.
  Billing export:   building (one ticket merged, one in progress); two calls the crew made that wait
                    to be kept; a permission request from its builder for a command.
  Docs search:      a spec and two tickets, not handed to the crew: "Ready to start".
  Link counter:     every ticket merged: ready to close (the page can close it).
  Retry uploads:    one ticket merged, one still open: closing it is refused with a sentence.
  Old report:       a project finished already, so "All projects" counts one.
  Two more permission requests on the desk: one from the reviewer for every project, and one to
  withdraw while its dialog is open (the refusal the page shows).
usage: python seed-factory-home.py <port>
Prints a JSON map of names (projects, and desk items: allow-crew, allow-withdraw, allow-builder)
to ids and labels on the last line."""
import json, sys, urllib.error, urllib.request

port = sys.argv[1]
base = f"http://127.0.0.1:{port}"


def post(path, body):
    r = urllib.request.Request(base + path, json.dumps(body).encode(), {"content-type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(r))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{path} -> {e.code} {e.read().decode()[:300]}")


def project(name, goal):
    return post("/api/projects", {"template": "custom", "name": name, "goal": goal, "objects": [],
                                  "kind": "software_factory"})["id"]


def ticket(uid, title, stage=None, holder=None):
    t = post(f"/api/projects/{uid}/tickets", {"title": title})
    if holder:
        post(f"/api/projects/{uid}/tickets/{t['id']}/assign", {"holder": holder})
    if stage:
        post(f"/api/projects/{uid}/tickets/{t['id']}/stage", {"stage": stage, "reason": "seeded"})
    return t


ids = {}
station = "billing-export-build-t2-api"
uid = ids["Billing export"] = project("Billing export", "Customers download their invoices as a CSV file")
ticket(uid, "Write the export query", "merged")
ticket(uid, "Add the download button", "doing", station)
for q, choice, why in [
    ("Which columns does the CSV hold?", "Invoice number, date, amount and status", "The invoice page shows those four"),
    ("How big may an export be?", "Twelve months at most", "A longer one would time out"),
]:
    post(f"/api/projects/{uid}/assumptions", {"question": q, "choice": choice, "why": why})
d = post("/api/desk", {"question": "May the builder run the export tests?", "recommendation": "Allow it",
                       "why": "It only reads the scratch database", "project": uid, "station": station,
                       "command": "npm run test -- --watch=false"})
ids["allow-builder"] = d["label"]

uid = ids["Docs search"] = project("Docs search", "Readers find a page by typing a few words of it")
post(f"/api/projects/{uid}/docs", {"kind": "spec", "title": "Docs search spec", "body": "# Docs search\n\nA search box on every page."})
ticket(uid, "Index the pages")
ticket(uid, "Add the search box")

uid = ids["Link counter"] = project("Link counter", "Count every visit to a short link")
ticket(uid, "Count a visit", "merged")
ticket(uid, "Show the count", "merged")

uid = ids["Retry uploads"] = project("Retry uploads", "A failed upload tries again before it gives up")
ticket(uid, "Retry three times", "merged")
ticket(uid, "Tell the user after the last try")

uid = project("Old report", "A one-off report that shipped last month")
ticket(uid, "Write the report", "merged")
post(f"/api/projects/{uid}/finish", {"words": "Seeded as finished"})

d = post("/api/desk", {"question": "May the reviewer read pull requests?", "recommendation": "Allow it",
                       "why": "Reviewing is its job", "station": "crew-reviewer",
                       "command": "gh pr view 12 --json state"})
ids["allow-crew"] = d["label"]
d = post("/api/desk", {"question": "May the builder run the seed script?", "recommendation": "Allow it",
                       "why": "It only fills the scratch database", "station": "other-build-t1-api",
                       "command": "make seed"})
ids["allow-withdraw"] = d["label"]
print(json.dumps(ids))
