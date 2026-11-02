"""Seed a scratch factory Tares for factory-docs.e2e.cjs (TR-737): the Docs and memory page as a cell
with some history shows it. Invented names only.
  shared rules and shared memory with lines;
  project-x (open): spec, plan, rules and a memory with five dated lines from four authors;
  project-y (finished): a spec and a memory of 130 dated lines (more than the page shows at once);
  project-w (not a factory project): a spec that must not appear.
Starts a daemon on the scratch data dir with TARES_EDITION=factory, creates the projects and their
documents through its API, stops it, then writes the dated memory bodies and the times straight into
the database. Prints a JSON map of names to ids on the last line.
usage: python seed-factory-docs.py <data-dir> <port> <mcp-port> <ui-dist> <tares-bin>"""
import json, os, subprocess, sys, time, urllib.error, urllib.request
from datetime import date, timedelta

home, port, mcp, dist, tares = sys.argv[1:6]
env = {**os.environ, "PYTHONPATH": os.getcwd(), "TARES_HOME": home, "TARES_PORT": port, "TARES_MCP_PORT": mcp,
       "TARES_UI_DIST": dist, "TARES_EDITION": "factory", "TARES_OTLP_GRPC_PORT": "off"}
for k in ("TARES_AUTH_TOKEN", "TARES_DB", "TARES_CATALOG", "TARESD_URL"):
    env.pop(k, None)
base = f"http://127.0.0.1:{port}"


def call(path, body=None):
    r = urllib.request.Request(base + path, json.dumps(body).encode() if body is not None else None,
                               {"content-type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(r))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{path} -> {e.code} {e.read().decode()[:300]}")


# project-x memory, oldest first: (days ago, who, line)
X_MEMORY = [
    (12, "crew-builder-1", "Articles over 600 words fail the length check; split them."),
    (9, "crew-reviewer", "Expand the questions before the checks, not after."),
    (6, "console", "The first wave needs a yes on each pull request the day it opens."),
    (3, "crew-builder-1", "Answer articles keep one fact per sentence so an assistant can quote them."),
    (1, "crew-orchestrator", "Tell the reviewer which ticket a pull request closes."),
]
BIG = 130   # lines in project-y's memory

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
    for name, factory in (("project-x", True), ("project-y", True), ("project-w", False)):
        body = {"template": "custom", "name": name, "objects": [], "goal": "A project for the docs page."}
        if factory:
            body["kind"] = "software_factory"
        ids[name] = call("/api/projects", body)["id"]
    px, py, pw = ids["project-x"], ids["project-y"], ids["project-w"]
    docs = (
        (px, "spec", "Spec", "# Spec\n\nBuild the answers site for service-a.\n\n## Goal\n\nA reader finds an answer in two clicks.\n\n"
                             "- Every article answers one question\n- Every article names its source of truth\n"),
        (px, "plan", "Plan: project-x", "# Plan\n\nThree milestones, in order.\n\n## M1: Sitemap and index live\n\n"
                                        "Goal: the site has a sitemap and an empty index page.\n- T1 Add a sitemap file\n- T2 Add the index page\n\n"
                                        "## M2: Checks gate CI\n\nGoal: the checks exist and are tested.\n- T3 Scaffold the tooling\n"),
        (px, "agents", "Rules: project-x", "# Rules\n\nRules for project-x only.\n\n- Keep each pull request to one ticket.\n"
                                           "- Run the checks before asking for review.\n"),
        (px, "memory", "Memory: project-x", "# Memory: project-x\n\nWhat the builders learned about this project.\n"),
        (py, "spec", "Spec", "# Spec\n\nMove the blog to the new site.\n"),
        (py, "memory", "Memory: project-y", "# Memory: project-y\n\nWhat the builders learned about this project.\n"),
        (pw, "spec", "Spec", "# Spec\n\nNot a factory project, so the page never lists it.\n"),
    )
    for uid, kind, title, body in docs:
        call(f"/api/projects/{uid}/docs", {"kind": kind, "title": title, "body": body})
    for line, who in (("Stop only the processes you started yourself.", "crew-reviewer"),
                      ("Keep each pull request to one ticket, and name the ticket in the title.", "crew-orchestrator")):
        call("/api/docs/global/agents/lines", {"text": line, "by": who})
    for line, who in (("Alex prefers short pull requests.", "console"), ("Kim reviews on Tuesdays.", "crew-orchestrator")):
        call("/api/docs/global/memory/lines", {"text": line, "by": who})
    page = call("/api/factory/docs")
    ids["shared-rules"], ids["shared-memory"] = (s["id"] for s in page["shared"])
    for p in page["projects"] + page["finished"]:
        for d in p["docs"]:
            ids[f"{p['name']}-{d['kind']}"] = d["id"]
finally:
    daemon.terminate()
    daemon.wait()

sys.path.insert(0, os.getcwd())
from tares.store import Store  # noqa: E402

store = Store(os.path.join(home, "tares.duckdb"))
con = store.con
today = date.today()
stamp = lambda days, who: f"({(today - timedelta(days=days)).isoformat()}, {who})"


def memory_body(name, lines):
    return (f"# Memory: {name}\n\nWhat the builders learned about this project.\n\n"
            + "".join(f"- {text} {stamp(days, who)}\n" for days, who, text in lines))


authors = ["crew-builder-1", "crew-reviewer", "console", "crew-orchestrator"]
big = [(max(2, (BIG - i) // 4 + 2), authors[i % 4], f"Memory line {i + 1}: a fact kept for the crew.") for i in range(BIG)]
store.update_doc(ids["project-x-memory"], body=memory_body("project-x", X_MEMORY), by="crew-builder-1")
store.update_doc(ids["project-y-memory"], body=memory_body("project-y", big), by="crew-reviewer")
con.execute("UPDATE usecases SET status = 'done' WHERE id = ?", [ids["project-y"]])
# the whole documents were last changed a while ago, by different people
for key, days, who in (("project-x-spec", 20, "crew-orchestrator"), ("project-x-plan", 14, "console"),
                       ("project-x-agents", 8, "crew-reviewer"), ("project-x-memory", 1, "crew-builder-1"),
                       ("project-y-memory", 2, "crew-reviewer"), ("project-y-spec", 30, "crew-orchestrator")):
    con.execute("UPDATE docs SET updated_at = now() - to_days(?), updated_by = ? WHERE id = ?", [days, who, ids[key]])
con.close()
print(json.dumps(ids))
