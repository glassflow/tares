"""`crew up` asks only what neither the team nor the person has set (TR-1721).

The real command runs as a subprocess against a scratch cell with auth on, served on a local port from this process,
with the Python fake of Claude Code and a throwaway home (tests/factory/parity.py, tests/_crew_guard.py). The MCP
address is the stand-in Tares of tests/factory. The box a terminal shows (tares/factory_setup.py) runs here too, its
key presses scripted. What is checked:

  - a team that set everything: a member is asked nothing and saves nothing;
  - a team that set some: one line says what the team chose, only the rest is asked, and only what was asked is
    saved, so a team's value is never copied into the member's own settings;
  - an answer looser than a value the team saved meanwhile is refused with Tares' own message and asked again, and
    the command stops when nobody is there to answer;
  - the owner of a cell with nothing set is asked everything, as before;
  - the questions as data (`crew setup --questions` / `--answers`) and the box follow the same rules.

Run: python tests/test_crew_settings_missing.py
"""
import contextlib
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "factory"))
import _crew_guard  # noqa: E402  (read before HOME changes: it remembers the real home)
from _env import scrub_os_environ  # noqa: E402

scrub_os_environ()
_crew_guard.scratch_home()   # the box runs in this process: not on the real home (TR-1164)
_crew_guard.install()

TMP = Path(tempfile.mkdtemp(prefix="tares-settings-missing-")).resolve()
ROOT_KEY = "root-token-settings-missing"
os.environ.update(TARES_HOME=str(TMP / "cell"), TARES_CATALOG=str(TMP / "none.yaml"),
                  TARES_AUTH_TOKEN=ROOT_KEY, TARES_OTLP_GRPC_PORT="off", TARES_EDITION="factory")

import hashlib  # noqa: E402

import httpx  # noqa: E402
import uvicorn  # noqa: E402

import parity  # noqa: E402  (the harness: crew_env)
from fake_up_tares import FakeTares  # noqa: E402

from tares import factory, factory_home  # noqa: E402
from tares import factory_setup as box  # noqa: E402
from tares import words  # noqa: E402
from tares.crew import up  # noqa: E402
from tares.crew.client import SETTING_KEYS, looser_than_team, settings_missing, team_settings  # noqa: E402
from tares.crew.state import trust_keys  # noqa: E402
from tares.daemon import make_app  # noqa: E402

P = F = 0
KEYS = SETTING_KEYS.split()
TIMEOUT = 120
SETTINGS = "/api/crew/settings"
LINE = "Using your team's choices:"
ASKING = "Setting up your crew"
GRANT_ASKED = "Anything you always allow the crew"
GRANT = "the releaser may restart the staging database"
PATTERN = "^kubectl .*prod-of-the-team"
TEAM_ALL = {"autonomy": "L3-review", "releaser": True, "release_profile": "github-release", "prod_pattern": PATTERN,
            "challenger": True, "builders_max": 3}
# the server's two refusals, word for word
LOOSE_AUTONOMY = ("autonomy L4-ship is looser than the team's L3-review; a personal value can only be as strict "
                  "or stricter. Clear yours to use the team's.")
LOOSE_CHALLENGER = ("challenger off is looser than the team's value, on; a personal value can only be as strict "
                    "or stricter. Clear yours to use the team's.")


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail!s}"[:900]))


# ---------- the functions ----------

print("== which settings are missing")
ck("every setting set: nothing is missing", settings_missing(TEAM_ALL) == [], settings_missing(TEAM_ALL))
ck("nothing set, or every value null: the four questions, no release key without a releaser",
   settings_missing({}) == settings_missing({k: None for k in KEYS})
   == ["autonomy", "releaser", "challenger", "builders_max"], settings_missing({}))
ck("an empty autonomy is not a choice", settings_missing({**TEAM_ALL, "autonomy": ""}) == ["autonomy"])
ck("no releaser: how to ship is not missing",
   settings_missing({"autonomy": "L4-ship", "releaser": False, "challenger": False, "builders_max": 1}) == [])

print("== the line about the team's choices")
two = {"autonomy": "L4-ship", "builders_max": 2}
ck("autonomy and builders",
   words.team_choices(two, two) == "Using your team's choices: the crew merges on its own, 2 builders.",
   words.team_choices(two, two))
one = {"autonomy": "L3-review", "builders_max": 1}
ck("one builder is singular",
   words.team_choices(one, one) == "Using your team's choices: you approve each merge, 1 builder.",
   words.team_choices(one, one))
ck("autonomy alone: nothing about builders",
   words.team_choices({"autonomy": "L3-review", "builders_max": None}, {"autonomy": "L3-review", "builders_max": 2})
   == "Using your team's choices: you approve each merge.")
ck("builders alone: nothing about autonomy",
   words.team_choices({"autonomy": None, "builders_max": 4}, {"autonomy": "L4-ship", "builders_max": 4})
   == "Using your team's choices: 4 builders.")
ck("nothing from the team, no line",
   words.team_choices({}, two) == words.team_choices({"autonomy": None, "builders_max": None}, two) == "")
ck("a value of one's own that is not the team's is not called the team's",
   words.team_choices(two, {"autonomy": "L3-review", "builders_max": 1}) == "")
ck("an autonomy with no words says nothing",
   words.team_choices({"autonomy": "L9", "builders_max": None}, {"autonomy": "L9"}) == "")
ck("an autonomy that is not a text says nothing",
   all(words.team_choices({"autonomy": a}, {"autonomy": a}) == "" for a in (["L4-ship"], {"level": "L4-ship"}, 4, "")))
ck("the line keeps to the words list, no em dash",
   all(words.offenders(words.team_choices(t, t)) == [] for t in (one, two)))

print("== what Tares says")
ck("the team's settings come from the home's joiner block",
   team_settings({"joiner": {"team_settings": one}}) == one)
ck("no block, a null one, or no document: nothing from the team",
   [team_settings(x) for x in (None, [], "x", {}, {"joiner": None}, {"joiner": {}},
                               {"joiner": {"team_settings": None}})] == [{}] * 7)


def server_refusal(team, mine):
    try:
        factory.personal_ceiling_ok(team, mine)
    except Exception as e:   # noqa: BLE001 (its DocError; the message is what is read)
        return str(e)
    return ""


got = (server_refusal({"autonomy": "L3-review"}, {"autonomy": "L4-ship"}),
       server_refusal({"challenger": True}, {"challenger": False}))
ck("the server's refusals read as written here", got == (LOOSE_AUTONOMY, LOOSE_CHALLENGER), got)
ck("each is recognised by the setting it names",
   (looser_than_team(400, got[0]), looser_than_team(400, got[1])) == ("autonomy", "challenger"))
ck("another status, another message or no message is not one",
   [looser_than_team(*a) for a in ((403, LOOSE_AUTONOMY), (400, ""), (None, ""),
                                   (400, "autonomy is L3-review or L4-ship"),
                                   (400, "releaser is looser than the team's"))] == [""] * 5)


# ---------- the scratch cell ----------

class Spy:
    """The scratch cell, every request noted, and two things a case can arrange: a team setting saved the moment
    before the next save of settings arrives (the team deciding while a person answers), and a cell whose home
    cannot be read."""

    def __init__(self, app):
        self.app, self.seen, self.team_first, self.no_home = app, [], None, False

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        method, path = scope["method"], scope["path"]
        chunks = []
        while True:
            message = await receive()
            chunks.append(message.get("body", b""))
            if not message.get("more_body"):
                break
        body, given = b"".join(chunks), False
        if method == "PUT" and path == SETTINGS and self.team_first:
            first, self.team_first = self.team_first, None
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://team") as cx:
                r = await cx.put(SETTINGS, json=first, headers={"Authorization": f"Bearer {ROOT_KEY}"})
                assert r.status_code == 200, r.text
        row = {"method": method, "path": path, "body": body, "status": 0}
        self.seen.append(row)

        async def replay():
            nonlocal given
            if given:
                return await receive()
            given = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def note(message):
            if message["type"] == "http.response.start":
                row["status"] = message["status"]
            await send(message)

        if path == "/api/home" and self.no_home:
            row["status"] = 404
            await send({"type": "http.response.start", "status": 404, "headers": []})
            return await send({"type": "http.response.body", "body": b""})
        await self.app(scope, replay, note)


APP = make_app()
SPY = Spy(APP)
SOCK = socket.socket()
SOCK.bind(("127.0.0.1", 0))
CELL = f"http://127.0.0.1:{SOCK.getsockname()[1]}"
SERVER = uvicorn.Server(uvicorn.Config(SPY, log_level="critical"))
THREAD = threading.Thread(target=SERVER.run, kwargs={"sockets": [SOCK]}, daemon=True)
THREAD.start()
for _ in range(600):
    if SERVER.started or not THREAD.is_alive():
        break
    time.sleep(0.05)
if not SERVER.started:
    print(f"FAIL  the scratch cell did not start\n\n{P} passed, {F + 1} failed")
    sys.exit(1)
MCP = FakeTares(str(TMP / "mcp")).start()

# one member, with a person's key; the owner is the cell's own token
PERSON = APP.state.store.create_person("Ana Example", "ana@example.test")["id"]
MEMBER_KEY = "nvf_member_member-secret-for-the-test"
APP.state.store.insert_api_key("member", "member", "nvf_member", hashlib.sha256(MEMBER_KEY.encode()).hexdigest(),
                               ["read", "ingest"], person_id=PERSON)
HTTP = httpx.Client(base_url=CELL, timeout=30, trust_env=False)
OWNER = {"Authorization": f"Bearer {ROOT_KEY}"}
MEMBER = {"Authorization": f"Bearer {MEMBER_KEY}"}


def put(who, body):
    r = HTTP.put(SETTINGS, json=body, headers=who)
    assert r.status_code == 200, r.text


def effective(who=MEMBER):
    return HTTP.get(SETTINGS, headers=who).json()


def reset(team=None, mine=None):
    """A cell with these team settings and these of the member's own, and nothing noted yet."""
    for who in (OWNER, MEMBER):
        put(who, {"clear": KEYS})
    if team:
        put(OWNER, team)
    if mine:
        put(MEMBER, mine)
    factory_home.clear_cache()
    SPY.seen.clear()
    SPY.team_first, SPY.no_home = None, False


def saves():
    """Each save of settings the cell was asked for: (status, body)."""
    return [(r["status"], json.loads(r["body"] or b"{}")) for r in SPY.seen
            if (r["method"], r["path"]) == ("PUT", SETTINGS)]


def grants_saved():
    return [json.loads(r["body"])["words"] for r in SPY.seen if (r["method"], r["path"]) == ("POST", "/api/grants")]


def home_reads():
    return sum(r["path"] == "/api/home" for r in SPY.seen)


def own():
    """The member's own settings: what is left for them once the team's are cleared."""
    put(OWNER, {"clear": KEYS})
    got = effective()
    return {k: got[k] for k in KEYS if got.get(k) is not None}


class Machine:
    """One throwaway machine with a trusted crew home and the fake Claude Code."""

    def __init__(self, key=MEMBER_KEY):
        self.tmp = Path(tempfile.mkdtemp(prefix="machine-", dir=TMP))
        self.env = parity.crew_env(self.tmp)
        self.env.update(TARESD_URL=CELL, TARES_AUTH_TOKEN=key, TARES_FACTORY_MCP_URL=MCP.mcp_url)
        crew = self.tmp / "crew"
        crew.mkdir(exist_ok=True)
        (self.tmp / "home" / ".claude.json").write_text(
            json.dumps({"projects": {str(crew): {"hasTrustDialogAccepted": True}}}), encoding="utf-8")

    def run(self, *argv, stdin=""):
        try:
            return subprocess.run([sys.executable, "-m", "tares.crew", "crew", *argv], cwd=str(ROOT), env=self.env,
                                  input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=TIMEOUT)
        except subprocess.TimeoutExpired:   # a question asked for ever: a failed check, not a stuck test
            return subprocess.CompletedProcess(argv, -1, "", f"still running after {TIMEOUT} s")

    def up(self, *extra, stdin=""):
        return self.run("up", "--no-watch", *extra, stdin=stdin)


def questions_in(text):
    """The settings questions a run printed, in the order of the settings."""
    return [k for k in KEYS if k in up.QUESTIONS and up.QUESTIONS[k]["text"] in text]


def started(r):
    return r.returncode == 0 and "The crew takes a few minutes to check in" in r.stderr


def tail(r):
    return (r.returncode, r.stderr[-700:])


# ---------- crew up ----------

print("== a member of a team that set everything")
reset(team=TEAM_ALL)
r = Machine().up()
ck("crew up starts the crew", started(r), tail(r))
ck("no question, no grant question, no heading and no line",
   questions_in(r.stderr) == [] and not any(t in r.stderr for t in (ASKING, GRANT_ASKED, LINE, "Choice [")), tail(r))
ck("nothing is saved", saves() == [] and grants_saved() == [], (saves(), grants_saved()))
ck("the member has no settings of their own", own() == {})

print("== the team set autonomy only")
reset(team={"autonomy": "L3-review"})
r = Machine().up(stdin="no\nno\n2\n\n")
ck("crew up starts the crew", started(r), tail(r))
ck("one line says the team's choice, before the first question",
   r.stderr.count(f"{LINE} you approve each merge.\n") == 1
   and -1 < r.stderr.find(LINE) < r.stderr.find("1/3  "), tail(r))
ck("the rest is asked, autonomy is not",
   questions_in(r.stderr) == ["releaser", "challenger", "builders_max"] and re.search(r"\b4/\d+  ", r.stderr) is None, questions_in(r.stderr))
ck("no None in what is printed", "None" not in r.stderr, tail(r))
ck("only what was asked is saved",
   saves() == [(200, {"releaser": False, "challenger": False, "builders_max": 2})], saves())
put(OWNER, {"autonomy": "L4-ship"})
ck("the member still follows the team's autonomy when it changes", effective()["autonomy"] == "L4-ship", effective())
ck("their own settings hold what they were asked",
   own() == {"releaser": False, "challenger": False, "builders_max": 2})

print("== the team set autonomy and one builder")
reset(team={"autonomy": "L4-ship", "builders_max": 1})
r = Machine().up(stdin="no\nno\n\n")
ck("the line names both, one builder in the singular",
   f"{LINE} the crew merges on its own, 1 builder.\n" in r.stderr, tail(r))
ck("only releaser and challenger are asked and saved",
   started(r) and questions_in(r.stderr) == ["releaser", "challenger"]
   and saves() == [(200, {"releaser": False, "challenger": False})], (questions_in(r.stderr), saves()))

print("== the team set the number of builders only")
reset(team={"builders_max": 2})
r = Machine().up(stdin="L4-ship\nno\nno\n\n")
ck("Tares names no team settings without an autonomy: no line, and none half filled",
   LINE not in r.stderr and "None" not in r.stderr, tail(r))
ck("the builders are not asked and not saved",
   started(r) and questions_in(r.stderr) == ["autonomy", "releaser", "challenger"]
   and saves() == [(200, {"autonomy": "L4-ship", "releaser": False, "challenger": False})],
   (questions_in(r.stderr), saves()))

print("== a member who is already stricter than the team")
reset(team={"autonomy": "L4-ship", "builders_max": 3}, mine={"autonomy": "L3-review"})
r = Machine().up(stdin="no\nno\n\n")
ck("the line leaves out the autonomy that is not the team's",
   f"{LINE} 3 builders.\n" in r.stderr and "autonomy" not in questions_in(r.stderr), tail(r))

print("== the team set how to ship, not whether there is a releaser")
reset(team={"release_profile": "github-release", "prod_pattern": PATTERN})
r = Machine().up(stdin="L4-ship\nyes\nno\n2\n\n")
ck("a yes to the releaser does not ask how it ships",
   started(r) and questions_in(r.stderr) == ["autonomy", "releaser", "challenger", "builders_max"],
   (questions_in(r.stderr), r.stderr[-300:]))
ck("neither the team's profile nor its pattern is copied",
   saves() == [(200, {"autonomy": "L4-ship", "releaser": True, "challenger": False, "builders_max": 2})], saves())

print("== the team set the prod pattern only")
reset(team={"prod_pattern": PATTERN})
r = Machine().up(stdin="L4-ship\nyes\n\nno\n2\n\n")
ck("how to ship is asked and saved, the team's pattern is not written over",
   started(r) and "release_profile" in questions_in(r.stderr)
   and saves() == [(200, {"autonomy": "L4-ship", "releaser": True, "release_profile": "kind",
                          "challenger": False, "builders_max": 2})], saves())

print("== the team chose a releaser, not how it ships")
reset(team={"autonomy": "L3-review", "releaser": True, "challenger": False, "builders_max": 2})
r = Machine().up(stdin="\n\n")
ck("only how to ship is asked, and saved with the default pattern",
   started(r) and questions_in(r.stderr) == ["release_profile"]
   and saves() == [(200, {"release_profile": "kind", "prod_pattern": box.PROD_PATTERN})],
   (questions_in(r.stderr), saves()))

print("== a cell whose home cannot be read")
reset(team={"autonomy": "L3-review"})
SPY.no_home = True
r = Machine().up(stdin="no\nno\n2\n\n")
ck("no line, and the questions go on as usual",
   started(r) and home_reads() == 1 and LINE not in r.stderr
   and saves() == [(200, {"releaser": False, "challenger": False, "builders_max": 2})], (home_reads(), tail(r)))

print("== the team decides while the member answers")
reset()
SPY.team_first = {"autonomy": "L3-review", "challenger": True}
r = Machine().up(stdin="L4-ship\nno\nno\n2\nL3-review\nyes\n\n")
ck("each looser answer is refused with Tares' message, as it is",
   f"\n{LOOSE_AUTONOMY}\n" in r.stderr and f"\n{LOOSE_CHALLENGER}\n" in r.stderr, tail(r))
text = up.QUESTIONS
ck("and only that question is asked again",
   [r.stderr.count(text[k]["text"]) for k in ("autonomy", "releaser", "challenger", "builders_max")] == [2, 1, 2, 1],
   r.stderr)
ck("the saves: two refused, then the one that holds, with only the asked keys",
   [s for s, _ in saves()] == [400, 400, 200]
   and saves()[-1][1] == {"autonomy": "L3-review", "releaser": False, "challenger": True, "builders_max": 2}, saves())
ck("crew up goes on to the grant and starts", started(r) and GRANT_ASKED in r.stderr, tail(r))

print("== refused, and nobody left to answer")
reset()
SPY.team_first = {"autonomy": "L3-review"}
r = Machine().up(stdin="L4-ship\nno\nno\n2\n")
ck("it stops with Tares' message instead of asking for ever",
   r.returncode == 1 and r.stderr.rstrip().endswith(LOOSE_AUTONOMY), tail(r))
ck("one more try at the end of the input, then nothing", [s for s, _ in saves()] == [400, 400], saves())
ck("nothing was saved", own() == {})

reset()
SPY.team_first = {"autonomy": "L3-review"}
r = Machine().up("--yes")
ck("--yes: the refusal ends the run at once, with Tares' message",
   r.returncode == 1 and LOOSE_AUTONOMY in r.stderr and [s for s, _ in saves()] == [400], (saves(), tail(r)))

print("== the owner of a cell with nothing set")
reset()
r = Machine(ROOT_KEY).up(stdin=f"L3-review\nyes\n\nno\n3\n{GRANT}\n\n")
ck("every question is asked, then the grant",
   started(r) and questions_in(r.stderr) == ["autonomy", "releaser", "release_profile", "challenger", "builders_max"]
   and GRANT_ASKED in r.stderr, tail(r))
ck("no line about a team, and the home is not read", LINE not in r.stderr and home_reads() == 0, (home_reads(), tail(r)))
ck("everything is saved, the default pattern with the profile",
   saves() == [(200, {"autonomy": "L3-review", "releaser": True, "release_profile": "kind",
                      "prod_pattern": box.PROD_PATTERN, "challenger": False, "builders_max": 3})]
   and grants_saved() == [GRANT], (saves(), grants_saved()))


# ---------- the questions as data ----------

def asked_as_data(machine):
    r = machine.run("setup", "--questions", "--json")
    try:
        return [q["id"] for q in json.loads(r.stdout)["questions"]]
    except (ValueError, KeyError):
        return tail(r)


def answer(machine, answers):
    f = machine.tmp / "answers.json"
    f.write_text(json.dumps(answers), encoding="utf-8")
    return machine.run("setup", "--answers", str(f), "--json")


print("== crew setup, as data")
reset(team=TEAM_ALL)
m = Machine()
ck("a team that set everything: no question", asked_as_data(m) == [], asked_as_data(m))

reset(team={"autonomy": "L3-review", "release_profile": "github-release", "prod_pattern": PATTERN})
got = asked_as_data(m)
ck("what the team set is no question, how to ship included",
   got == ["releaser", "challenger", "builders_max", "grant"], got)
r = answer(m, {"releaser": True, "challenger": False, "builders_max": 2})
ck("the answers save only what was asked",
   r.returncode == 0 and saves() == [(200, {"releaser": True, "challenger": False, "builders_max": 2})],
   (saves(), tail(r)))

reset(team={"prod_pattern": PATTERN})
r = answer(m, {"autonomy": "L4-ship", "releaser": True, "release_profile": "github-release", "challenger": False,
               "builders_max": 2})
ck("the team's prod pattern is not written over",
   r.returncode == 0 and len(saves()) == 1 and "prod_pattern" not in saves()[0][1]
   and saves()[0][1]["release_profile"] == "github-release", (saves(), tail(r)))

reset()
SPY.team_first = {"autonomy": "L3-review"}
r = answer(m, {"autonomy": "L4-ship", "releaser": False, "challenger": False, "builders_max": 2})
try:
    doc = json.loads(r.stdout)
except ValueError:
    doc = {}
ck("a refused answer: the error document carries Tares' message as it is",
   r.returncode == 1 and doc.get("error", {}).get("message") == LOOSE_AUTONOMY, (r.stdout[:600], tail(r)))
ck("and nothing was saved", [s for s, _ in saves()] == [400] and own() == {}, saves())


# ---------- the box ----------

HOME = TMP / "box-crew"
HOME.mkdir()
(Path.home() / ".claude.json").write_text(
    json.dumps({"projects": {k: {"hasTrustDialogAccepted": True} for k in trust_keys(str(HOME))}}), encoding="utf-8")


def run_box(*shown, key=MEMBER_KEY):
    """box.setup() with its key presses scripted. `shown` is one dict per box that opens: question key to the value
    picked (Enter, the box's own default, for a question it does not name), or None to press Esc.
    Returns (what setup returned, the question keys of each box, what was printed)."""
    todo, asked, real = list(shown), [], box.run_form

    def press(form):
        picks = todo.pop(0) if todo else None
        keys = []
        asked.append(keys)
        if picks is None:
            keys.extend(q.key for q in form.questions)
            form.key(box.ESC)
            return form
        for _ in range(40):
            if form.result:
                break
            if form.on_review:
                form.key(box.ENTER)
                continue
            q = form.questions[form.at]
            keys.append(q.key)
            if q.key in picks:
                form.key(str([o.value for o in q.options].index(picks[q.key]) + 1))
            else:
                form.key(box.ENTER)
        return form

    out = io.StringIO()
    box.run_form = press
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            done = box.setup(CELL, key, HOME, "claude")
    finally:
        box.run_form = real
    return done, asked, out.getvalue()


print("== the box")
reset(team=TEAM_ALL)
done, asked, text = run_box()
ck("a team that set everything: no box, nothing saved",
   done is True and asked == [] and text == "" and saves() == [], (done, asked, text, saves()))

reset(team={"autonomy": "L3-review"})
done, asked, text = run_box({"releaser": "no", "challenger": "no", "builders_max": "2"})
ck("the box says the team's choice and asks the rest",
   done is True and f"{LINE} you approve each merge." in text
   and asked == [["releaser", "challenger", "builders_max", "grant"]], (done, asked, text))
ck("it saves only what it asked",
   saves() == [(200, {"releaser": False, "challenger": False, "builders_max": 2})] and grants_saved() == [],
   (saves(), grants_saved()))

reset(team={"autonomy": "L3-review", "releaser": True, "challenger": False, "builders_max": 2})
done, asked, text = run_box({"release_profile": "github-release"})
ck("a releaser the team chose, with no way to ship: the box asks it",
   done is True and asked == [["release_profile", "grant"]]
   and saves() == [(200, {"release_profile": "github-release", "prod_pattern": box.PROD_PATTERN})], (done, asked, saves(), text))

reset(team={"release_profile": "github-release", "prod_pattern": PATTERN})
done, asked, text = run_box({"autonomy": "L4-ship", "releaser": "yes", "challenger": "no", "builders_max": "2"})
ck("how to ship already set: a yes to the releaser does not bring that question",
   done is True and asked == [["autonomy", "releaser", "challenger", "builders_max", "grant"]]
   and saves() == [(200, {"autonomy": "L4-ship", "releaser": True, "challenger": False, "builders_max": 2})],
   (done, asked, saves(), text))

reset(team={"prod_pattern": PATTERN})
done, asked, text = run_box({"autonomy": "L4-ship", "releaser": "yes", "release_profile": "custom",
                             "challenger": "no", "builders_max": "2"})
ck("the team's prod pattern only: the box asks how to ship and does not write the pattern",
   done is True and "release_profile" in asked[0]
   and saves() == [(200, {"autonomy": "L4-ship", "releaser": True, "release_profile": "custom",
                          "challenger": False, "builders_max": 2})], (done, asked, saves(), text))

reset()
SPY.team_first = {"autonomy": "L3-review", "challenger": True}
done, asked, text = run_box({"autonomy": "L4-ship", "releaser": "no", "challenger": "no", "builders_max": "2"},
                            {"autonomy": "L3-review"}, {"challenger": "yes"})
ck("a refused answer: Tares' message, then a box with that question alone",
   done is True and LOOSE_AUTONOMY in text and LOOSE_CHALLENGER in text
   and asked[1:] == [["autonomy"], ["challenger"]], (done, asked, text))
ck("the box saves once all of it holds, only the asked keys",
   [s for s, _ in saves()] == [400, 400, 200]
   and saves()[-1][1] == {"autonomy": "L3-review", "releaser": False, "challenger": True, "builders_max": 2}, saves())
ck("what it says it saved is the second answer",
   "You, each one" in text.split("Saved on Tares")[-1], text)

reset()
SPY.team_first = {"autonomy": "L3-review"}
done, asked, text = run_box({"autonomy": "L4-ship", "releaser": "no", "challenger": "no", "builders_max": "2"}, None)
ck("Esc on the question asked again: cancelled, nothing saved",
   done is False and "Cancelled: nothing was saved" in text and [s for s, _ in saves()] == [400] and own() == {},
   (done, asked, saves(), text))

reset()
done, asked, text = run_box({"autonomy": "L3-review", "releaser": "yes", "challenger": "no", "builders_max": "3"},
                            key=ROOT_KEY)
ck("the owner of a cell with nothing set: every question, no line",
   done is True and LINE not in text and home_reads() == 0
   and asked == [["autonomy", "releaser", "release_profile", "challenger", "builders_max", "grant"]],
   (done, asked, text))
ck("everything is saved, the default pattern with the profile",
   saves() == [(200, {"autonomy": "L3-review", "releaser": True, "release_profile": "kind",
                      "prod_pattern": box.PROD_PATTERN, "challenger": False, "builders_max": 3})], saves())

MCP.stop()
SERVER.should_exit = True
THREAD.join(30)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
