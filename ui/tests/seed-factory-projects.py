"""Seed a scratch factory Tares for factory-projects.e2e.cjs (TR-733): the Projects list as a cell with
some history shows it. Invented names only.
  project-w (waiting on you): a question on the desk, one ticket merged, one not;
  project-b1 and project-b2 (building): tickets in flight, one blocked on b1, repos, milestones;
  project-y (not started): tickets written, none begun; its goal carries markup;
  project-z and project-q (ready to close): every ticket merged;
  project-d and project-e (done): finished, one handed to the crew, one closed by hand;
  project-n (not a factory project, made with no kind): must not appear, as the cell's Default does not.
Starts a daemon on the scratch data dir with TARES_EDITION=factory, creates the projects through its
API, stops it, then backdates the projects' times straight in the database so the rows read as a cell
with history. Prints a JSON map of names to ids on the last line.
usage: python seed-factory-projects.py <data-dir> <port> <mcp-port> <ui-dist> <tares-bin>"""
import json, os, subprocess, sys, time, urllib.error, urllib.request

home, port, mcp, dist, tares = sys.argv[1:6]
env = {**os.environ, "PYTHONPATH": os.getcwd(), "TARES_HOME": home, "TARES_PORT": port, "TARES_MCP_PORT": mcp,
       "TARES_UI_DIST": dist, "TARES_EDITION": "factory", "TARES_OTLP_GRPC_PORT": "off"}
for k in ("TARES_AUTH_TOKEN", "TARES_DB", "TARES_CATALOG", "TARESD_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
    env.pop(k, None)
base = f"http://127.0.0.1:{port}"


def call(path, body=None, headers=None):
    r = urllib.request.Request(base + path, json.dumps(body).encode() if body is not None else None,
                               {"content-type": "application/json", **(headers or {})})
    try:
        return json.load(urllib.request.urlopen(r))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{path} -> {e.code} {e.read().decode()[:300]}")


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

    def make(name, goal, factory=True):
        body = {"template": "custom", "name": name, "objects": [], "goal": goal}
        if factory:
            body["kind"] = "software_factory"
        ids[name] = call("/api/projects", body)["id"]
        time.sleep(0.05)
        return ids[name]

    def tickets(uid, titles, milestone=None):
        for t in titles:
            call(f"/api/projects/{uid}/tickets", {"title": t, **({"milestone": milestone} if milestone else {})})

    def stage(uid, n, stg, reason=None):
        call(f"/api/projects/{uid}/tickets/T{n}/stage", {"stage": stg, **({"reason": reason} if reason else {})})

    def hand(uid, *repos):
        call(f"/api/projects/{uid}/handover", {"repos": [{"path": "/w/" + r.split("/")[1], "name": r} for r in repos]})

    # oldest first, so the newest activity is the project made last
    make("project-n", "Not a factory project", factory=False)
    tickets(ids["project-n"], ["Task 1", "Task 2"])

    e = make("project-e", "Closed by hand, never handed to the crew.")
    tickets(e, ["Task 1"])
    stage(e, 1, "merged")
    call(f"/api/projects/{e}/finish", {"words": "done"})

    d = make("project-d", "Move the blog to the new site.")
    tickets(d, ["Task 1", "Task 2"])
    hand(d, "example-org/service-a")
    stage(d, 1, "merged")
    stage(d, 2, "merged")
    call(f"/api/projects/{d}/finish", {"words": "done"})

    # the goal carries markup on purpose: the page must show it as text
    y = make("project-y", "Let customers <b>connect</b> their own tools <img src=x onerror=\"window.__pwned=1\">.")
    for m in ("M1: Foundations", "M2: Setup pages"):
        call(f"/api/projects/{y}/milestones", {"name": m, "goal": "A step."})
    tickets(y, ["Write the connector contract", "Add the setup page"], "M1: Foundations")
    tickets(y, ["Add the second setup page"], "M2: Setup pages")
    hand(y, "example-org/service-a")

    b1 = make("project-b1", "Give the console an edition for each kind of team.")
    for m in ("M1: Foundations", "M2: Home page"):
        call(f"/api/projects/{b1}/milestones", {"name": m, "goal": "A step."})
    tickets(b1, ["Add the shell", "Add the menus"], "M1: Foundations")
    tickets(b1, ["Add the home page", "Add the list page"], "M2: Home page")
    hand(b1, "example-org/service-a", "example-org/docs-site")
    stage(b1, 1, "merged")
    stage(b1, 2, "doing")
    stage(b1, 3, "blocked", "Waiting for a decision")

    b2 = make("project-b2", "Tidy the styles across the console.")
    tickets(b2, ["Move the colours to tokens", "Remove the old classes"])
    hand(b2, "example-org/service-a")
    stage(b2, 1, "merged")
    stage(b2, 2, "doing")

    w = make("project-w", "Publish the answer articles on the docs site, once the gateway is chosen.")
    tickets(w, ["Pick the gateway", "Write the first articles"])
    hand(w, "example-org/service-b")
    stage(w, 1, "merged")
    call("/api/desk", {"question": "Which gateway?", "recommendation": "the first", "why": "It is the cheaper one.",
                       "project": "project-w"},
         {"X-Tares-Station": "crew-orchestrator", "X-Tares-Role": "orchestrator"})

    for name, goal, repos in (("project-q", "Let a team share one crew.", ("example-org/service-a", "example-org/service-b")),
                              ("project-z", "Prepare the workshop.", ("example-org/service-a",))):
        z = make(name, goal)
        tickets(z, ["Task 1", "Task 2", "Task 3"])
        hand(z, *repos)
        for n in (1, 2, 3):
            stage(z, n, "merged")
finally:
    daemon.terminate()
    daemon.wait()

sys.path.insert(0, os.getcwd())
from tares.store import Store  # noqa: E402

store = Store(os.path.join(home, "tares.duckdb"))
con = store.con
# how long ago each project last moved (hours): the project and its tickets
for name, hours in (("project-b1", 0.05), ("project-b2", 2), ("project-w", 26), ("project-y", 120), ("project-q", 72),
                    ("project-z", 240)):
    con.execute("UPDATE usecases SET updated_at = now() - to_seconds(?) WHERE id = ?", [int(hours * 3600), ids[name]])
    con.execute("UPDATE tickets SET updated_at = now() - to_seconds(?) WHERE project = ?", [int(hours * 3600), ids[name]])
con.close()
print(json.dumps(ids))
