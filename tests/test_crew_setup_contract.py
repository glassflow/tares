"""The setup contract of `crew up` (TR-956): the same questions in a terminal and as data.

  tares factory crew setup --questions --json    every outstanding question, in one document ({schema_version, questions})
  tares factory crew setup --answers <file>      validate the answers and save them on Tares (--json: what was saved)
  tares factory crew up --yes                    use what is saved, ask nothing

Checks, against a stand-in Tares (tests/factory/fake_up_tares.py) and the Python fake of Claude Code, all in
throwaway folders: the shape of every question; which ones are outstanding (none when everything is saved;
the crew's name first when a second person has joined); that the contract path and the terminal path save
the same settings, the same grant and the same requests, in the same order; that bad answers (an unknown
id, a value that is not a choice, an invalid crew name, a release profile with no releaser, text that is
not JSON) save nothing and say why; that `crew up --yes` with saved answers asks nothing and writes
nothing; that the Python crew up starts the watch (TR-1155) and never asks Claude Code for the language
server list; that crew up on the default engine is still bash.

Run: python tests/test_crew_setup_contract.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import _crew_guard  # noqa: E402  (read before HOME changes: it remembers the real home)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent / "factory"))
_crew_guard.scratch_home()   # the probes below inherit this process's HOME: not the real one (TR-1164)
_crew_guard.install()   # an exec or spawn that would reach the real ~/factory fails the run (TR-1164)

import parity  # noqa: E402  (the harness: crew_env and the stand-in Tares)
from fake_up_tares import FakeTares  # noqa: E402

from tares.crew import dispatch, up, watch  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402

try:
    from jsonschema import Draft202012Validator, FormatChecker  # noqa: E402  (a test-only dependency, as in test_crew_contract)
except ImportError:
    print("FAIL  jsonschema is not installed (a test-only dependency: pip install jsonschema)")
    sys.exit(1)

SCHEMAS = ROOT / "tares" / "crew" / "schemas"


def schema_problems(name, doc):
    """What is wrong with doc against schemas/<name>.json; [] when it validates."""
    v = Draft202012Validator(json.loads((SCHEMAS / f"{name}.json").read_text(encoding="utf-8")), format_checker=FormatChecker())
    return [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in v.iter_errors(doc)]

P = F = 0
TIMEOUT = 120
HAVE_BASH = bool(shutil.which("bash")) and os.name != "nt"
KEYS = {"id", "text", "kind", "choices", "default", "recommendation", "why", "pattern"}
GRANT = "the releaser may restart the staging database"
BASH_NAME_PATTERN = "^[a-z0-9][a-z0-9-]{0,30}$"   # the pattern bash passes to ask for the crew's name


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


class Case:
    """One throwaway crew with a stand-in Tares."""

    def __init__(self, **tares):
        self.tmp = Path(tempfile.mkdtemp(prefix="setup-contract-")).resolve()
        self.env = parity.crew_env(self.tmp, "python")
        self.tares = FakeTares(self.tmp / "tares", **tares).start()
        self.env.update(TARESD_URL=self.tares.url, TARES_FACTORY_MCP_URL=self.tares.mcp_url)
        self.crew = self.tmp / "crew"
        home = self.tmp / "home" / ".claude.json"
        home.write_text(json.dumps({"projects": {str(self.crew): {"hasTrustDialogAccepted": True}}}), encoding="utf-8")
        self.crew.mkdir(parents=True, exist_ok=True)

    def run(self, argv, stdin="", env=None):
        e = {**self.env, **(env or {})}
        return subprocess.run([sys.executable, "-m", "tares.crew", *argv], cwd=str(ROOT), env=e, input=stdin,
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT)

    def answers(self, doc) -> str:
        f = self.tmp / "answers.json"
        f.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")
        return str(f)

    def calls(self) -> list:
        f = self.tmp / "claude" / "calls.jsonl"
        try:
            return [json.loads(x)["args"] for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
        except OSError:
            return []

    def close(self):
        self.tares.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


def questions(c, extra=()):
    r = c.run(["crew", "setup", "--questions", "--json", *extra])
    try:
        doc = json.loads(r.stdout)
    except ValueError:
        return r, None
    # the document is {"schema_version": 1, "questions": [...]}; the checks below read the list
    return r, doc["questions"] if isinstance(doc, dict) and doc.get("schema_version") == 1 else doc


ALL_IDS = ["autonomy", "releaser", "release_profile", "challenger", "builders_max", "grant"]

# ====================================================================================
print("the questions as data")
c = Case()
r, qs = questions(c)
def as_doc(text):
    try:
        return json.loads(text)
    except ValueError:
        return None


qdoc = as_doc(r.stdout)
ck("--questions --json exits 0 and prints one document: schema_version 1 and the questions list",
   r.returncode == 0 and isinstance(qs, list) and isinstance(qdoc, dict) and set(qdoc) == {"schema_version", "questions"}
   and len(r.stdout.strip().splitlines()) == 1, (r.returncode, r.stdout[:120], r.stderr[-300:]))
qs = qs or []
ck("the document validates against setup_questions.json", schema_problems("setup_questions", qdoc) == [], schema_problems("setup_questions", qdoc))
ck("a fresh Tares: every setting is outstanding, in the order crew up asks them, then the grant",
   [q["id"] for q in qs] == ALL_IDS, [q.get("id") for q in qs])
ck("every question has exactly id, text, kind, choices, default, recommendation, why, pattern (and when)",
   all(KEYS <= set(q) and set(q) <= KEYS | {"when"} for q in qs), [sorted(q) for q in qs][:1])
ck("kind is text, choice or bool", all(q["kind"] in ("text", "choice", "bool") for q in qs))
ck("a choice or bool question lists its choices, each with value and label",
   all(q["choices"] and all({"value", "label"} <= set(x) for x in q["choices"]) for q in qs if q["kind"] != "text"))
ck("the recommendation is one of the choices", all(q["recommendation"] in [x["value"] for x in q["choices"]]
                                                   for q in qs if q["kind"] != "text"))
ck("the default is the recommendation", all(q["default"] == q["recommendation"] for q in qs if q["id"] != "grant"))
ck("every question says why", all(q["why"] and q["text"] for q in qs))
by = {q["id"]: q for q in qs}
ck("the autonomy choices are L4-ship and L3-review, the recommendation L4-ship",
   [x["value"] for x in by["autonomy"]["choices"]] == ["L4-ship", "L3-review"] and by["autonomy"]["recommendation"] == "L4-ship")
ck("releaser is a bool, recommended false", by["releaser"]["kind"] == "bool" and by["releaser"]["recommendation"] is False)
ck("builders_max offers 1 to 4, recommended 2",
   [x["value"] for x in by["builders_max"]["choices"]] == ["1", "2", "3", "4"] and by["builders_max"]["recommendation"] == "2")
ck("the release profile is only asked after a yes to the releaser", by["release_profile"].get("when") == {"releaser": True})
ck("the grant is free text with no pattern", by["grant"]["kind"] == "text" and by["grant"]["pattern"] == "")
ck("asking for the questions saves nothing", c.tares.written() == [])
r2 = c.run(["crew", "setup", "--questions"])
ck("--questions without --json prints the same document", r2.returncode == 0 and r2.stdout == r.stdout, (r2.stdout[:80], r.stdout[:80]))
c.close()

c = Case(settings={"autonomy": "L4-ship", "releaser": False, "release_profile": "kind",
                   "prod_pattern": "x", "challenger": False, "builders_max": 2})
r, qs = questions(c)
ck("everything saved: no question is outstanding", r.returncode == 0 and qs == [], (r.stdout, r.stderr[-300:]))
c.close()

c = Case(settings={"autonomy": "L4-ship", "builders_max": 2})
r, qs = questions(c)
ck("some saved: only the missing settings are outstanding (and the grant)",
   [q["id"] for q in (qs or [])] == ["releaser", "release_profile", "challenger", "grant"], [q.get("id") for q in (qs or [])])
c.close()

c = Case(people=[{"id": "local", "name": "Local"}, {"id": "p2", "name": "Two"}, {"id": "p3", "name": "Three"}])
r, qs = questions(c)
first = (qs or [{}])[0]
ck("two other people on Tares: the crew's name is the first question", first.get("id") == "crew_name", first)
ck("...a text question with bash's pattern, a suggested default and a reason",
   first.get("kind") == "text" and first.get("pattern") == BASH_NAME_PATTERN == up.NAME_PATTERN
   and first.get("default") and first.get("why"), first)
c.close()

# ====================================================================================
print("the terminal path and the contract path save the same")
TERMINAL = f"L3-review\nyes\n\nno\n3\n{GRANT}\n\n"
ANSWERS = {"autonomy": "L3-review", "releaser": True, "release_profile": "kind", "challenger": False,
           "builders_max": "3", "grant": GRANT}

c1 = Case()
t = c1.run(["crew", "up"], stdin=TERMINAL)          # the stations part needs bash; the answers are saved before it
c2 = Case()
a = c2.run(["crew", "setup", "--answers", c2.answers(ANSWERS), "--json"])
ck("--answers exits 0 and says what it saved", a.returncode == 0 and json.loads(a.stdout)["grants"] == 1, (a.returncode, a.stdout, a.stderr[-300:]))
ck("the --answers --json document validates against setup_answers.json", schema_problems("setup_answers", json.loads(a.stdout)) == [],
   schema_problems("setup_answers", json.loads(a.stdout)))
ck("--answers --json is one document: schema_version 1, then saved and grants",
   list(json.loads(a.stdout)) == ["schema_version", "saved", "grants"] and json.loads(a.stdout)["schema_version"] == 1
   and "autonomy" in json.loads(a.stdout)["saved"] and len(a.stdout.strip().splitlines()) == 1, a.stdout[:200])
ck("the terminal path saved the settings", c1.tares.settings.get("autonomy") == "L3-review", (t.returncode, t.stderr[-400:]))
ck("same settings on Tares (all keys, prod_pattern with the release profile)", c1.tares.settings == c2.tares.settings,
   (c1.tares.settings, c2.tares.settings))
ck("the settings are the answers", [c2.tares.settings.get(k) for k in ("autonomy", "releaser", "release_profile", "challenger", "builders_max")]
   == ["L3-review", True, "kind", False, 3] and "prod_pattern" in c2.tares.settings)
ck("same grant on Tares", c1.tares.grants == c2.tares.grants and len(c2.tares.grants) == 1 and GRANT in c2.tares.grants[0],
   (c1.tares.grants, c2.tares.grants))
ck("the same requests, in the same order", c1.tares.written() == c2.tares.written(), (c1.tares.written(), c2.tares.written()))
ck("the terminal path asked the questions the contract lists",
   all(q in t.stdout + t.stderr for q in ("Who merges pull requests?", "How many builders may work at the same time?")))
ck("a second --questions after --answers: nothing outstanding", questions(c2)[1] == [])
c1.close()
c2.close()

# a releaser answered no: no release profile is asked or saved, on both paths
c1 = Case()
c1.run(["crew", "up"], stdin="L4-ship\nno\nno\n2\n\n")
c2 = Case()
a = c2.run(["crew", "setup", "--answers", c2.answers({"autonomy": "L4-ship", "releaser": False, "challenger": False, "builders_max": 2})])
ck("no releaser: same settings on both paths", a.returncode == 0 and c1.tares.settings == c2.tares.settings and c2.tares.settings.get("releaser") is False,
   (c1.tares.settings, c2.tares.settings))
ck("no releaser: no release profile is saved", "release_profile" not in c2.tares.settings)
ck("no grant said: none saved", c1.tares.grants == [] and c2.tares.grants == [])
ck("the answers may be words (yes, no) as the terminal takes them",
   json.loads(c2.run(["crew", "setup", "--questions"]).stdout) == {"schema_version": 1, "questions": []})
c1.close()
c2.close()

# the crew's name on both paths
# bash counts the people other than the built-in local one and asks for a name when there are more than one
people = [{"id": "local", "name": "Local"}, {"id": "p2", "name": "Two"}, {"id": "p3", "name": "Three"}]
full = {"autonomy": "L4-ship", "releaser": False, "challenger": False, "builders_max": 2}
c1 = Case(people=people)
c1.run(["crew", "up"], stdin="shared\nL4-ship\nno\nno\n2\n\n")
c2 = Case(people=people)
a = c2.run(["crew", "setup", "--answers", c2.answers({"crew_name": "shared", **full})])
n1 = json.loads((c1.crew / "crew.json").read_text(encoding="utf-8")).get("crew_name")
n2 = json.loads((c2.crew / "crew.json").read_text(encoding="utf-8")).get("crew_name")
ck("the crew's name is stored in crew.json by both paths", a.returncode == 0 and n1 == n2 == "shared", (a.stderr[-300:], n1, n2))
ck("...and the settings match", c1.tares.settings == c2.tares.settings, (c1.tares.settings, c2.tares.settings))
c1.close()
c2.close()

# ====================================================================================
print("answers that are not valid save nothing")
BAD = [
    ("an unknown id", {**ANSWERS, "colour": "blue"}, "colour"),
    ("a value that is not a choice (autonomy)", {**ANSWERS, "autonomy": "L9"}, "autonomy"),
    ("a value that is not a choice (builders_max)", {**ANSWERS, "builders_max": "7"}, "builders_max"),
    ("a number out of range given as a number", {**ANSWERS, "builders_max": 0}, "builders_max"),
    ("a bool that is neither yes nor no", {**ANSWERS, "challenger": "maybe"}, "challenger"),
    ("a missing answer", {k: v for k, v in ANSWERS.items() if k != "autonomy"}, "autonomy"),
    ("a release profile with no releaser", {**ANSWERS, "releaser": False}, "release_profile"),
    ("a grant that is not text", {**ANSWERS, "grant": 5}, "grant"),
    ("answers that are not an object", ["L3-review"], "object"),
]
for label, doc, word in BAD:
    c = Case()
    r = c.run(["crew", "setup", "--answers", c.answers(doc)])
    ck(f"{label}: refused, names the problem, saves nothing",
       r.returncode != 0 and word in r.stderr and c.tares.written() == [] and c.tares.settings == {} and c.tares.grants == [],
       (r.returncode, r.stderr[-300:], c.tares.written()))
    c.close()
c = Case(people=people)
for bad in ("Bad_Name", "-x", "UPPER", "a b", "x-build-y", "build", "a" * 32):
    r = c.run(["crew", "setup", "--answers", c.answers({"crew_name": bad, **full})])
    ck(f"crew name {bad!r} is refused as the terminal refuses it",
       r.returncode != 0 and "is not a valid crew name" in r.stderr and c.tares.written() == [], (r.returncode, r.stderr[-200:]))
r = c.run(["crew", "setup", "--answers", c.answers({"crew_name": "a" * 31, **full})])
ck("a 31 character crew name is accepted", r.returncode == 0, r.stderr[-300:])
c.close()
c = Case()
r = c.run(["crew", "setup", "--answers", c.answers("{not json")])
ck("a file that is not JSON: refused, saves nothing", r.returncode != 0 and r.stderr.strip() != "" and c.tares.written() == [], (r.returncode, r.stderr[-200:]))
r = c.run(["crew", "setup", "--answers", str(c.tmp / "missing.json")])
ck("a file that is not there: refused, says so", r.returncode != 0 and "missing.json" in r.stderr and c.tares.written() == [], r.stderr[-200:])
r = c.run(["crew", "setup"])
ck("crew setup with no flag: a usage line, exit 1", r.returncode == 1 and "usage" in r.stderr, (r.returncode, r.stderr[-200:]))
r = c.run(["crew", "setup", "--bogus", "--json"])
err = json.loads(r.stdout) if r.stdout.strip().startswith("{") else {}
ck("a usage error with --json: error code usage, exit 2, nothing on stderr (as doctor does)",
   r.returncode == 2 and err.get("error", {}).get("code") == "usage" and "usage:" in err.get("error", {}).get("message", "")
   and r.stderr.strip() == "" and schema_problems("error", err) == [], (r.returncode, r.stdout[:200], r.stderr[-200:]))
r = c.run(["crew", "setup", "--json"])
err = json.loads(r.stdout) if r.stdout.strip().startswith("{") else {}
ck("no flag with --json: also the usage error, exit 2", r.returncode == 2 and err.get("error", {}).get("code") == "usage", (r.returncode, r.stdout[:200]))
c.close()
c = Case()
r = c.run(["crew", "setup", "--questions"], env={"TARESD_URL": "http://127.0.0.1:9"})
ck("no Tares: says so plainly", r.returncode != 0 and "no Tares answers at http://127.0.0.1:9" in r.stderr, (r.returncode, r.stderr[-200:]))
r = c.run(["crew", "setup", "--questions", "--json"], env={"TARESD_URL": "http://127.0.0.1:9"})
err = json.loads(r.stdout) if r.stdout.strip().startswith("{") else {}
ck("the error document validates against error.json", schema_problems("error", err) == [], schema_problems("error", err))
ck("no Tares with --json: the error document on stdout, exit 1, nothing on stderr",
   r.returncode == 1 and err.get("schema_version") == 1 and "no Tares answers at http://127.0.0.1:9" in err.get("error", {}).get("message", "")
   and set(err.get("error", {})) == {"code", "message"} and r.stderr.strip() == "", (r.returncode, r.stdout[:200], r.stderr[-200:]))
c.close()
c = Case()
r = c.run(["crew", "setup", "--answers", c.answers({"autonomy": "nope", "releaser": False, "challenger": False, "builders_max": 2}), "--json"])
err = json.loads(r.stdout) if r.stdout.strip().startswith("{") else {}
ck("bad answers with --json: the problems are in the error document, nothing saved",
   r.returncode == 1 and "autonomy" in err.get("error", {}).get("message", "") and "the answers were not saved" in err.get("error", {}).get("message", "")
   and r.stderr.strip() == "" and c.tares.written() == [], (r.returncode, r.stdout[:300], r.stderr[-200:]))
r = c.run(["crew", "setup", "--answers", c.answers({"autonomy": "nope", "releaser": False, "challenger": False, "builders_max": 2})])
ck("bad answers without --json: the problems stay on stderr, stdout empty",
   r.returncode == 1 and "autonomy" in r.stderr and r.stdout.strip() == "", (r.returncode, r.stdout[:100], r.stderr[-200:]))
c.close()

# ====================================================================================
print("crew up --yes uses the saved answers")
SAVED = {"autonomy": "L3-review", "releaser": True, "release_profile": "kind",
         "prod_pattern": up.settings_defaults()["prod_pattern"], "challenger": False, "builders_max": 3}
c = Case(settings=SAVED, grants=['- "%s" (2026-01-01)' % GRANT])
env_watch = {"FACTORY_NO_WATCH": ""}          # the watch asked for: the harness switches it off by default
r = c.run(["crew", "up", "--yes"], env=env_watch)
out = r.stdout + r.stderr
ck("asks nothing: no prompt, no question in the output", "Choice [" not in out and "recommended:" not in out and "> " not in out.replace("-> ", ""), out[-300:])
ck("writes nothing to Tares: every answer was saved already", c.tares.written() == [], c.tares.written())
if HAVE_BASH:
    ck("says it started the watch", "started the watch, which revives any station that stops" in out, out[-400:])
    ck("starts the Python watch: the pid file names it and the log is there",
       (c.crew / "watch.pid").exists() and (c.crew / "watch.log").exists() and watch.running_pid(CrewPaths(str(c.crew))) is not None,
       out[-300:])
    meta = (c.crew / watch.META_FILE).read_text(encoding="utf-8").split()
    ck("...and it is the Python watch (TARES_FACTORY_ENGINE=python)", meta[-1:] == ["python"], meta)
    watch.stop(CrewPaths(str(c.crew)), lambda m: None, 20, c.env)   # a watch left running would outlive the test
    ck("the test stopped it", watch.running_pid(CrewPaths(str(c.crew))) is None)
else:   # no bash (Windows): the stations cannot be started yet (TR-953), so it stops before the watch line
    ck("without bash it says the stations cannot be started here and exits 2",
       r.returncode == 2 and "cannot start the stations here" in out and "started the watch" not in out, (r.returncode, out[-400:]))
ck("asks Claude Code for the language server list, as bash does, and offers nothing when no project has a repo",
   ["plugin", "list", "--json"] in c.calls() and "Code intelligence" not in out, c.calls())
if HAVE_BASH:
    ck("exits 0 and starts the three stations", r.returncode == 0 and sum(1 for a in c.calls() if a[:1] == ["--bg"]) == 3,
       (r.returncode, out[-400:]))
else:
    ck("no bash here: says plainly that starting the stations is not yet ported, exit 2",
       r.returncode == 2 and "not yet ported" in r.stderr, (r.returncode, r.stderr[-300:]))
r = c.run(["crew", "up", "--yes", "--no-watch"], env=env_watch)
ck("--no-watch: no watch started", "started the watch" not in r.stdout + r.stderr and not (c.crew / "watch.pid").exists())
r = c.run(["crew", "up", "--yes"])           # FACTORY_NO_WATCH=1 as the harness sets it
ck("FACTORY_NO_WATCH set: no watch started", "started the watch" not in r.stdout + r.stderr and not (c.crew / "watch.pid").exists())
c.close()

c = Case()
r = c.run(["crew", "up", "--yes"])
ck("--yes with nothing saved: the recommended answers are used and saved, nothing is asked",
   "Choice [" not in r.stdout + r.stderr and c.tares.settings.get("autonomy") == "L4-ship" and c.tares.settings.get("releaser") is False,
   (r.returncode, c.tares.settings, (r.stdout + r.stderr)[-300:]))
c.close()

# ====================================================================================
print("routing: crew up runs in Python by default (TR-1069)")
ck("crew up and crew setup are in the ported table", dispatch.find(["crew", "up"]) is not None and dispatch.find(["crew", "setup"]) is not None)
calls = []


def fake_execve(path, args, env):
    _crew_guard.check_exec([path, *args], env)   # even a refused exec must not carry the real home
    calls.append((path, args))
    raise AssertionError("crew up must not exec bash from the Python entry")


with mock.patch.object(os, "execve", fake_execve), \
        mock.patch.object(up, "cmd_crew_up", lambda argv, env: calls.append(("python", argv)) or 0):
    scratch = Path(tempfile.mkdtemp(prefix="setup-contract-route-"))   # a scratch home, not this process's own (TR-1164)
    base = {k: v for k, v in parity.crew_env(scratch, "python").items() if not k.startswith("TARES_FACTORY_ENGINE")}
    up.entry_crew_up(["crew", "up", "--yes"], dict(base))
    ck("engine unset: the Python crew up runs, with its arguments", calls == [("python", ["crew", "up", "--yes"])], calls)
    calls.clear()
    up.entry_crew_up(["crew", "up"], {**base, "TARES_FACTORY_ENGINE": "python"})
    ck("engine=python: the Python crew up runs", calls == [("python", ["crew", "up"])], calls)

# ====================================================================================
print("an older Tares (need_capability)")
c = Case(capabilities=[])
r = c.run(["crew", "up", "--yes"])
ck("a Tares that does not list integrations: crew up stops with the upgrade message",
   r.returncode != 0 and "This needs a newer Tares on the cell (missing integrations). Upgrade Tares, then run this again." in r.stderr,
   (r.returncode, r.stderr[-300:]))
ck("...before it saved or started anything", c.tares.written() == [] and c.calls() == [], (c.tares.written(), c.calls()))
r = c.run(["crew", "setup", "--questions", "--json"])
ck("...while the read-only questions still answer (only commands that start something need it)", r.returncode == 0, r.stderr[-200:])
# the capability check comes before the MCP check: an old Tares with no MCP server up says "upgrade", not "start MCP"
r = c.run(["crew", "up", "--yes"], env={"TARES_FACTORY_MCP_URL": "http://127.0.0.1:9/mcp"})
ck("an old Tares with no MCP server up: the upgrade message wins over the MCP one",
   "missing integrations" in r.stderr and "no MCP server answers" not in r.stderr, (r.returncode, r.stderr[-300:]))
c.close()
c = Case(capabilities=["integrations", "other"])
r = c.run(["crew", "up", "--yes", "--no-watch"])
ck("a Tares that lists integrations among others is not refused", "needs a newer Tares" not in r.stderr, r.stderr[-300:])
c.close()

# ====================================================================================
print("the module")
src = (ROOT / "tares" / "crew" / "up.py").read_text(encoding="utf-8")
ck("up.py has no em dash", "\u2014" not in src)
ck("up.py does not import termios (tares.factory_setup) or the daemon",
   not re.search(r"^\s*(import|from)\s+(termios|tares\.factory_setup|tares\.daemon|tares\.store|duckdb|fastapi)\b", src, re.M))
r = subprocess.run([sys.executable, "-c", "import sys; import tares.crew.dispatch as d; d.find(['crew','up']); "
                    "print(sorted(m for m in ('duckdb','fastapi','tares.daemon','tares.store','tares.factory_setup','tares.cli') if m in sys.modules))"],
                   cwd=str(ROOT), capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(ROOT)})
ck("looking up crew up in the dispatcher imports no heavy module", r.stdout.strip() == "[]", (r.stdout, r.stderr[-200:]))

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
