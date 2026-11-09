"""`tares factory projects|finish|context|plan` in Python (tares/crew/person.py, TR-967).

The word-for-word match with bash is the parity scenarios (projects_*, usage_*). This checks what they
cannot: `projects --json` (bash has none), the usage errors, a refusal Tares words its own way, the key
going to Tares and never into a message, the small helpers, and that `tares factory roster` still does not
import the module (the dispatcher loads it only when one of its commands runs).

Run: .venv/bin/python tests/test_crew_person.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "factory"))

os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"

from _crew_fake import fake_crew_env  # noqa: E402
from parity import FakeTares  # noqa: E402
from tares.crew import person  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


PROJECTS = [
    {"id": "p1", "name": "Links App", "tickets_done": 1, "tickets_total": 2,
     "milestone": {"name": "Links", "done": 1, "total": 2}, "repos": [{"name": "acme/links", "path": "/w/links"}]},
    {"id": "p2", "name": "shop", "tickets_done": 0, "tickets_total": 0, "milestone": None, "repos": [{"path": "/w/shop"}]},
]
STATIONS = {
    "links-app-build-m1": {"role": "builder", "project": "Links App", "status": "active"},
    "links-app-build-m2": {"role": "builder", "project": "Links App", "status": "retired"},
    "crew-reviewer": {"role": "reviewer", "status": "active"},
}

T = Path(tempfile.mkdtemp(prefix="tares crew person "))


def run(env, *argv, fake=None, **extra):
    e = {**env, "TARES_FACTORY_ENGINE": "python", "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}
    if fake is not None:
        e["TARESD_URL"] = fake.url
    e.update(extra)
    return subprocess.run([sys.executable, "-m", "tares.crew", *argv], cwd=str(ROOT), env=e, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=120)


try:
    # --- the small functions
    ck("local_only: the commands that work while Tares is down",
       all(person.local_only(c) for c in ("tidy", "roster", "doctor", "down", "halt", "help")))
    ck("local_only: the ones that need Tares do not",
       not any(person.local_only(c) for c in ("projects", "finish", "context", "plan", "spawn", "")))
    ck("local_only: crew halt/resume/down/attach/logs are local, crew up and status are not",
       all(person.local_only("crew", s) for s in ("halt", "resume", "down", "attach", "logs"))
       and not person.local_only("crew", "up") and not person.local_only("crew", ""))
    ck("milestone: its name and progress", person._milestone({"milestone": {"name": "M1", "done": 2, "total": 5}}) == "M1 2/5")
    ck("milestone: none and tickets means all done", person._milestone({"milestone": None, "tickets_total": 3}) == "all done")
    ck("milestone: none and no tickets is a dash", person._milestone({"milestone": None, "tickets_total": 0}) == "-")
    ck("repos: the name, else the path", person._repos({"repos": [{"name": "a/b", "path": "/x"}, {"path": "/y"}]}) == "a/b, /y")
    ck("detail: a JSON detail, else the body", person._detail('{"detail": "no"}') == "no" and person._detail("boom") == "boom")
    lines = ['{"x": 1}', "not json", '{"context_usage": {"total_tokens": 5}}', '{"context_usage": null}', "[1]"]
    ck("last_field: the last object that has the key, skipping null",
       person.last_field(lines, "context_usage") == {"total_tokens": 5} and person.last_field(lines, "zz") is None)
    ck("reading settings: hooks off, no status line, the rest kept",
       person._reading_settings("") == {"disableAllHooks": True})
    sf = T / "s.json"
    sf.write_text(json.dumps({"statusLine": {"type": "command"}, "env": {"A": "1"}}), encoding="utf-8")
    ck("reading settings of a station", person._reading_settings(str(sf)) == {"env": {"A": "1"}, "disableAllHooks": True})

    # --- builders_of reads stations.json
    env = fake_crew_env(T / "a")
    crew = Path(env["FACTORY_CREW_HOME"])
    (crew / "stations.json").write_text(json.dumps(STATIONS), encoding="utf-8")
    paths = CrewPaths(str(crew))
    ck("builders_of: active builders of the project only", person.builders_of("Links App", paths) == 1
       and person.builders_of("shop", paths) == 0)
    (crew / "stations.json").write_text("not json", encoding="utf-8")
    ck("builders_of: an unreadable stations.json is 0", person.builders_of("Links App", paths) == 0)
    (crew / "stations.json").write_text(json.dumps(STATIONS), encoding="utf-8")

    # --- projects --json
    fake = FakeTares({"GET /api/crew/projects": (200, {"projects": PROJECTS})})
    try:
        r = run(env, "projects", "--json", fake=fake)
        rows = json.loads(r.stdout) if r.returncode == 0 else []
        ck("projects --json: each project with its builders", r.returncode == 0
           and [(p["name"], p["builders"]) for p in rows] == [("Links App", 1), ("shop", 0)], r.stdout + r.stderr)
        ck("projects --json: Tares's own fields are kept", rows and rows[0]["milestone"]["name"] == "Links"
           and rows[0]["repos"][0]["path"] == "/w/links")
    finally:
        fake.stop()
    fake = FakeTares({"GET /api/crew/projects": (200, {"projects": []})})
    try:
        r = run(env, "projects", "--json", fake=fake)
        ck("projects --json: no project is an empty list", r.returncode == 0 and json.loads(r.stdout) == [], r.stdout + r.stderr)
    finally:
        fake.stop()

    # --- finish
    fake = FakeTares({"GET /api/crew/projects": (200, {"projects": PROJECTS}),
                      "POST /api/projects/p1/finish": (409, {"detail": "Links App still has open tickets: T1"}),
                      "POST /api/projects/p2/finish": (200, {"open_projects": 0})})
    try:
        r = run(env, "finish", fake=fake)
        ck("finish: no project is a usage error", r.returncode == 1 and "usage: " in r.stderr
           and "finish <project>" in r.stderr, r.stderr)
        r = run(env, "finish", "Links App", "all done", fake=fake)
        ck("finish: Tares's refusal is shown, exit 1", r.returncode == 1
           and "Tares did not finish Links App: Links App still has open tickets: T1" in r.stderr, r.stderr)
        r = run(env, "finish", "shop", fake=fake, TARES_AUTH_TOKEN="s3cret-token")
        sent = fake.asked("POST /api/projects/p2/finish")
        ck("finish: the key goes to Tares as a bearer header", r.returncode == 0 and sent and sent[0]["auth"], r.stdout + r.stderr)
        ck("finish: the key is in no output", "s3cret-token" not in r.stdout + r.stderr)
        r = run(env, "finish", "shop", fake=fake, FACTORY_STATION="crew-orchestrator")
        ck("finish: refused for a station before Tares is asked", r.returncode == 1 and "only the person runs this" in r.stderr
           and len(fake.asked("POST /api/projects/p2/finish")) == 1, r.stderr)
    finally:
        fake.stop()

    # --- context and plan
    fake = FakeTares({"POST /ingest/claude_code": (500, {"detail": "no"})})
    try:
        r = run(env, "plan", fake=fake)
        ck("plan: a reading Tares does not take is said plainly", r.returncode == 1
           and "could not send the plan's limits to Tares" in r.stderr, r.stdout + r.stderr)
        r = run(env, "context", fake=fake)
        ck("context: no station is a usage error", r.returncode == 1 and "usage: " in r.stderr
           and "context <role|station>" in r.stderr, r.stderr)
        (crew / "stations.json").write_text(json.dumps({"crew-orchestrator": {"role": "orchestrator", "id": "deadbeef"}}),
                                            encoding="utf-8")
        r = run(env, "context", "orchestrator", fake=fake)
        ck("context: a station with no session says so", r.returncode == 1
           and "crew-orchestrator has no conversation to read yet" in r.stderr, r.stderr)
    finally:
        fake.stop()

    # --- plan on Windows hands claude its settings as a file (cmd.exe refuses JSON with quotes), elsewhere as text
    fake = FakeTares({})
    seen = []
    real_read = person.read_quiet

    def spy(cwd, settings, args):
        seen.append((settings, os.path.isfile(settings) and json.loads(Path(settings).read_text(encoding="utf-8"))))
        return []

    try:
        saved = {k: os.environ.get(k) for k in [*env, "TARESD_URL"]}
        os.environ.update(env)
        os.environ["TARESD_URL"] = fake.url
        person.read_quiet = spy
        for plat in ("linux", "win32"):
            seen.clear()
            real_plat = person.sys.platform
            person.sys.platform = plat
            try:
                person.cmd_plan(["plan"], dict(os.environ))
            finally:
                person.sys.platform = real_plat
            ck(f"plan on {plat}: settings as " + ("a file" if plat == "win32" else "JSON text"),
               len(seen) == 1 and (seen[0][1] == {"disableAllHooks": True} if plat == "win32"
                                   else seen[0][0] == '{"disableAllHooks": true}'), str(seen))
    finally:
        person.read_quiet = real_read
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        fake.stop()

    # --- the dispatcher loads the module only when it is needed
    probe = ("import sys; from tares.crew import dispatch; print('person' in ' '.join(m for m in sys.modules "
             "if m.startswith('tares.crew')))")
    r = subprocess.run([sys.executable, "-c", probe], cwd=str(ROOT), capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": str(ROOT)})
    ck("importing the dispatcher does not import person.py", r.stdout.strip() == "False", r.stdout + r.stderr)
    ck("the dispatcher routes the four commands to person.py",
       all(("projects",) in __import__("tares.crew.dispatch", fromlist=["x"]).PORTED for _ in [0])
       and all(k in __import__("tares.crew.dispatch", fromlist=["x"]).PORTED
               for k in (("projects",), ("finish",), ("context",), ("plan",))))
finally:
    shutil.rmtree(T, ignore_errors=True)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
