"""The crew's machine interface (TR-1067): `--json` on status, roster and desk, one error shape, and `events`.

What this checks, against the real commands in a throwaway crew (tests/_crew_fake.py: the fake Claude Code,
a stub Tares on a local port):

  - every document `status --json`, `roster --json`, `desk --json` and `events --json` prints validates
    against its schema in tares/crew/schemas/ (JSON Schema draft 2020-12, checked with jsonschema, a
    test-only dependency), carries schema_version 1 and is the only thing on stdout;
  - a failure with --json is the error shape (schemas/error.json) on stdout with exit 1, whichever command
    fails and whatever the cause; without --json it is the text line on stderr, as before;
  - `events`: three events written, a read with --since after the second gives only the third; the file
    truncated, then rotated, is followed on; a half-written line waits for its end; a cursor of another file
    or past the end starts at 0; and one run of the real command over a pipe, reconnected with its cursor;
  - the samples under tests/fixtures/crew_contract/ are what the commands print (ids and ports aside) and
    validate too. TARES_UPDATE_FIXTURES=1 rewrites them.

The text of status, roster and desk is not compared here: tests/test_crew_views.py and the parity harness do.
Untested on real Windows (TR-946): the file id of a rotated events file is what os.stat reports there.

Run: python tests/test_crew_contract.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "factory"))

import fake_claude  # noqa: E402
from _crew_fake import fake_crew_env  # noqa: E402

from tares.crew import contract, events  # noqa: E402

try:
    from jsonschema import Draft202012Validator, FormatChecker  # noqa: E402
except ImportError:
    print("FAIL  jsonschema is not installed (a test-only dependency: pip install jsonschema)")
    print("\n0 passed, 1 failed")
    sys.exit(1)

SCHEMAS = ROOT / "tares" / "crew" / "schemas"
FIXTURES = ROOT / "tests" / "fixtures" / "crew_contract"
UPDATE = os.environ.get("TARES_UPDATE_FIXTURES") == "1"
P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail!s}"[:600]))


# ---------- schemas ----------

VALIDATORS = {}
for _f in sorted(SCHEMAS.glob("*.json")):
    if _f.name == "schema_version.json":
        continue
    _schema = json.loads(_f.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(_schema)
    VALIDATORS[_f.stem] = Draft202012Validator(_schema, format_checker=FormatChecker())


def problems(name, doc):
    """What is wrong with doc against schemas/<name>.json; [] when it validates."""
    return [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in VALIDATORS[name].iter_errors(doc)]


# ---------- a stub Tares ----------

WORLD = {"desk": [], "projects": [], "settings": {"autonomy": "L4-ship"}}


class Stub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        body = {"/health": {"status": "ok"},
                "/api/crew/settings": {"settings": WORLD["settings"]},
                "/api/crew/projects": {"projects": WORLD["projects"]},
                "/api/desk": {"desk": WORLD["desk"]}}.get(path)
        raw = json.dumps(body if body is not None else {}).encode()
        self.send_response(200 if body is not None else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_POST = do_GET  # noqa: N815

    def log_message(self, *a):
        pass


SRV = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
PORT = SRV.server_address[1]
threading.Thread(target=SRV.serve_forever, daemon=True).start()
UP = f"http://127.0.0.1:{PORT}"
DOWN = "http://127.0.0.1:9"

# ---------- a crew, and a run in it ----------

TOP = Path(tempfile.mkdtemp(prefix="tares crew contract "))
N = [0]
OID, RID, BID, SID = "aaaa0001", "bbbb0002", "cccc0003", "dddd0004"


def station(role, sid, project="", status="active"):
    return {"role": role, "status": status, "project": project, "repo": "", "cwd": "", "model": "opus",
            "effort": "high", "id": sid}


def world(stations, agents, url=UP):
    N[0] += 1
    tmp = TOP / f"w{N[0]}"
    tmp.mkdir()
    env = fake_crew_env(tmp)
    env["TARESD_URL"] = url
    crew = Path(env["FACTORY_CREW_HOME"])
    if stations is None:
        (crew / "stations.json").unlink()
    else:
        (crew / "stations.json").write_text(json.dumps(stations, indent=2) + "\n", encoding="utf-8")
    fake_claude.write_agents(Path(env["FAKE_CLAUDE_STATE"]), agents)
    (Path(env["FAKE_CLAUDE_STATE"]) / "version").write_text("2.1.310\n", encoding="utf-8")
    return env, tmp


def crew_of():
    return {"crew-orchestrator": station("orchestrator", OID), "crew-reviewer": station("reviewer", RID),
            "shop-build-m1": station("builder", BID, project="shop"),
            "shop-build-m2": station("builder", SID, project="shop"),
            "old-station": station("helper", "eeee0005", status="stopped")}


def sessions():
    return [fake_claude.agent_row("working", OID, "crew-orchestrator", "/x", version="2.1.310"),
            fake_claude.agent_row("idle", RID, "crew-reviewer", "/x", version="2.1.310"),
            fake_claude.agent_row("waiting", BID, "shop-build-m1", "/x", version="2.1.100")]  # m2: gone


def tares(env, argv, extra=None):
    """(exit code, stdout, stderr) of `python -m tares.crew <argv>` with the Python engine."""
    e = dict(env, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", TARES_FACTORY_ENGINE="python", **(extra or {}))
    e.pop("PYTHONSTARTUP", None)
    p = subprocess.run([sys.executable, "-m", "tares.crew"] + argv, env=e, cwd=str(ROOT), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120)
    return p.returncode, p.stdout, p.stderr


def one_document(label, code, out, err, name, want_code=0):
    """Check exit code, one line on stdout, valid JSON that validates against schemas/<name>.json, nothing on
    stderr; return the document."""
    ck(f"{label}: exit {want_code}", code == want_code, f"{code} {err[-200:]}")
    lines = out.splitlines()
    ck(f"{label}: one line on stdout, LF ended", len(lines) == 1 and out.endswith("\n") and "\r" not in out, repr(out[:200]))
    try:
        doc = json.loads(out)
    except ValueError as e:
        ck(f"{label}: stdout is JSON", False, f"{e}: {out[:200]!r}")
        return {}
    bad = problems(name, doc)
    ck(f"{label}: validates against {name}.json", not bad, bad)
    ck(f"{label}: schema_version 1 first", doc.get("schema_version") == 1 and next(iter(doc)) == "schema_version")
    ck(f"{label}: nothing on stderr", err == "", err[:200])
    return doc


def fixture(name, text):
    """Compare text (ids, paths and the port made stable) with the committed sample; rewrite it on request."""
    path = FIXTURES / name
    if UPDATE:
        FIXTURES.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    have = path.read_text(encoding="utf-8") if path.is_file() else None
    ck(f"sample {name} is what the command prints", have == text, f"differs; run with TARES_UPDATE_FIXTURES=1\n{text}")


def stable(text, tmp):
    text = text.replace(str(tmp), "<T>").replace(str(tmp.resolve()), "<T>").replace(f":{PORT}", ":<PORT>")
    return re.sub(r"/private<T>", "<T>", text)


def pretty(doc):
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


# ---------- schemas themselves ----------

print("== the schemas")
ck("a schema for status, roster, desk, events and error",
   {"status", "roster", "desk", "events", "error"} <= set(VALIDATORS), sorted(VALIDATORS))
version = json.loads((SCHEMAS / "schema_version.json").read_text(encoding="utf-8"))["schema_version"]
ck("every schema pins schema_version to the number in schema_version.json",
   all(v.schema["properties"]["schema_version"] == {"const": version} for v in VALIDATORS.values()))
readme = (SCHEMAS / "README.md").read_text(encoding="utf-8") if (SCHEMAS / "README.md").is_file() else ""
ck("the README documents the exit codes", all(re.search(rf"^\|\s*{c}\s*\|", readme, re.M) for c in "012"), readme[:200])
ck("a document that is not the shape fails",
   problems("roster", {"schema_version": 1, "crew": "x", "orchestrator": "y"}) != []
   and problems("error", {"schema_version": 1, "error": {"code": "Bad Code", "message": "m"}}) != []
   and problems("events", {"schema_version": 1, "cursor": "abc", "event": {}}) != [])

# ---------- roster ----------

print("== roster --json")
env, tmp = world(crew_of(), sessions())
code, out, err = tares(env, ["roster", "--json"], {"TARESD_URL": DOWN})
doc = one_document("roster", code, out, err, "roster")
rows = {r["station"]: r for r in doc.get("stations", [])}
ck("roster: the crew and its orchestrator", doc.get("crew") == "crew" and doc.get("orchestrator") == "crew-orchestrator")
ck("roster: the active stations, not the stopped one", sorted(rows) == ["crew-orchestrator", "crew-reviewer", "shop-build-m1", "shop-build-m2"], sorted(rows))
ck("roster: a working orchestrator", rows.get("crew-orchestrator") == {
    "station": "crew-orchestrator", "role": "orchestrator", "project": "", "turn": "working", "claude_code": "2.1.310"}, rows)
ck("roster: a station without a session is gone", rows.get("shop-build-m2", {}).get("turn") == "gone", rows)
ck("roster: an older Claude Code is flagged", rows.get("shop-build-m1", {}).get("claude_code") == "2.1.100 (older than 2.1.310)", rows)
fixture("roster.json", stable(out, tmp))
c2, o2, e2 = tares(env, ["roster"], {"TARESD_URL": DOWN})
ck("roster without --json is still the text", c2 == 0 and o2.startswith("crew: crew (orchestrator: crew-orchestrator)\n"), o2[:80])
env, tmp = world({}, [])
doc = one_document("roster, no stations", *tares(env, ["roster", "--json"]), "roster")
ck("roster, no stations: an empty list", doc.get("stations") == [], doc)
env, tmp = world(None, [])
doc = one_document("roster, no stations file", *tares(env, ["roster", "--json"]), "roster")
ck("roster, no stations file: an empty list", doc.get("stations") == [], doc)
env, tmp = world(crew_of(), sessions())
c3, o3, e3 = tares(env, ["roster", "--json"], {"FACTORY_CREW_NAME": "ops"})
ck("roster, a named crew: the warning for a person stays on stderr", "Orchestrator session title is crew-orchestrator" in e3, e3)
doc = one_document("roster, a named crew", c3, o3, e3.split("\n", 1)[1] if e3 else e3, "roster")
ck("roster, a named crew: ops", doc.get("crew") == "ops" and doc.get("orchestrator") == "ops-orchestrator", doc)

# ---------- status ----------

print("== status --json")
WORLD["desk"] = [{"id": "d1", "label": "D1", "question": "ship it?", "options": ["yes", "no"], "recommendation": "yes",
                  "why": "tests pass", "blocking": True, "project_name": "shop"}]
WORLD["projects"] = [{"id": "p1", "name": "shop", "tickets_total": 4, "tickets_done": 1,
                      "milestone": {"name": "M1", "done": 1, "total": 3}},
                     {"id": "p2", "name": "docs", "tickets_total": 2, "tickets_done": 2},
                     {"id": "p3", "name": "new", "tickets_total": 0, "tickets_done": 0}]
env, tmp = world(crew_of(), sessions())
code, out, err = tares(env, ["status", "--json"])
doc = one_document("status", code, out, err, "status")
st = {r["station"]: r for r in doc.get("stations", [])}
ck("status: the running stations", doc.get("running") == 4 and len(st) == 4, doc.get("running"))
ck("status: a working orchestrator", st.get("crew-orchestrator", {}).get("state") == "working", st)
ck("status: a gone builder", st.get("shop-build-m2", {}).get("state") == "gone", st)
ck("status: model and effort apart", (st.get("shop-build-m1", {}).get("model"), st.get("shop-build-m1", {}).get("effort")) == ("opus", "high"), st)
ck("status: claude code behind", doc.get("claude_code") == {"target": "2.1.310", "older": 1, "retrying": 0}, doc.get("claude_code"))
ck("status: not halted, not frozen", doc.get("halted") is None and doc.get("frozen") is None, doc)
ck("status: watch not running", doc.get("watch") == {"running": False, "pid": None}, doc.get("watch"))
ck("status: one question waiting, in full", doc.get("desk", {}).get("waiting") == 1
   and doc["desk"]["next"]["question"] == "ship it?", doc.get("desk"))
ck("status: projects with and without a milestone",
   [(p["name"], p["milestone"] is not None) for p in doc.get("projects", [])] == [("shop", True), ("docs", False), ("new", False)],
   doc.get("projects"))
ck("status: the crew page", doc.get("crew_page") == f"{UP}/crew", doc.get("crew_page"))
fixture("status.json", stable(out, tmp))
c2, o2, e2 = tares(env, ["crew", "status", "--json"])
ck("crew status --json is status --json", c2 == 0 and o2 == out, o2[:100])
c2, o2, e2 = tares(env, ["status"])
ck("status without --json is still the text", c2 == 0 and o2.startswith("STATION") and not o2.lstrip().startswith("{"), o2[:80])

two = sessions() + [fake_claude.agent_row("working", "ffff0006", "shop-build-m1", "/y", version="2.1.310")]
env, tmp = world(crew_of(), two)
doc = one_document("status, two sessions", *tares(env, ["status", "--json"]), "status")
ck("status, two sessions: named", [t["name"] for t in doc.get("two_sessions", [])] == ["shop-build-m1"], doc.get("two_sessions"))
env, tmp = world(crew_of(), sessions())
(Path(env["FACTORY_CREW_HOME"]) / "HALT").write_text('{"at":"2026-11-09T10:00:00Z","reason":"maintenance"}\n', encoding="utf-8")
doc = one_document("status, halted", *tares(env, ["status", "--json"]), "status")
ck("status, halted: the reason", doc.get("halted") == "maintenance", doc.get("halted"))
env, tmp = world({}, [])
doc = one_document("status, no stations running", *tares(env, ["status", "--json"]), "status")
ck("status, no stations running: an empty list", doc.get("stations") == [] and doc.get("running") == 0, doc)
env, tmp = world(None, [])
code, out, err = tares(env, ["status", "--json"])
doc = one_document("status, no stations file", code, out, err.replace("tares factory: no stations yet; run tares factory crew up\n", ""), "status")
ck("status, no stations file: the note stays on stderr", "no stations yet" in err, err)

# ---------- desk ----------

print("== desk --json")
env, tmp = world(crew_of(), sessions())
code, out, err = tares(env, ["desk", "--json"])
doc = one_document("desk", code, out, err, "desk")
ck("desk: the item as Tares gives it", [i["label"] for i in doc.get("items", [])] == ["D1"], doc)
fixture("desk.json", stable(out, tmp))
WORLD["desk"] = []
doc = one_document("desk, nothing waiting", *tares(env, ["desk", "--json"]), "desk")
ck("desk, nothing waiting: an empty list", doc.get("items") == [], doc)
WORLD["projects"] = []

# ---------- the error shape ----------

print("== the error shape")
env, tmp = world(crew_of(), sessions(), url=DOWN)
code, out, err = tares(env, ["status", "--json"])
doc = one_document("status, no Tares", code, out, err, "error", want_code=1)
ck("status, no Tares: the message", "no Tares answers at http://127.0.0.1:9" in doc.get("error", {}).get("message", ""), doc)
fixture("error.json", stable(out, tmp))
c2, o2, e2 = tares(env, ["status"])
ck("...and without --json it is the text line on stderr, exit 1",
   c2 == 1 and o2 == "" and e2.startswith("tares factory: no Tares answers at"), (c2, o2, e2))
for argv, why in ((["desk", "--json"], "desk"), (["crew", "desk", "--json"], "crew desk")):
    code, out, err = tares(env, argv)
    doc = one_document(f"{why}, no Tares", code, out, err, "error", want_code=1)
env, tmp = world(crew_of(), sessions())
code, out, err = tares(env, ["roster", "--json"], {"FAKE_CLAUDE_AGENTS_FAIL": "1"})
doc = one_document("roster, sessions unreadable", code, out, err, "error", want_code=1)
ck("roster, sessions unreadable: its code", doc.get("error", {}).get("code") == "sessions_unreadable", doc)
code, out, err = tares(env, ["roster", "--json"], {"CLAUDE_BIN": str(TOP / "no such claude")})
doc = one_document("roster, no claude", code, out, err, "error", want_code=1)
ck("roster, no claude: some failure code", re.match(r"^[a-z][a-z0-9_]*$", doc.get("error", {}).get("code", "")) is not None, doc)
code, out, err = tares(env, ["tidy", "--json", str(TOP)])
doc = one_document("tidy, a refusal", code, out, err, "error", want_code=1)
ck("tidy, a refusal: outside the crew", "outside the crew" in doc.get("error", {}).get("message", ""), doc)
c2, o2, e2 = tares(env, ["tidy", str(TOP)])
ck("...and without --json it is the text line on stderr", c2 == 1 and o2 == "" and "outside the crew" in e2, (c2, o2, e2))
code, out, err = tares(env, ["events", "--json", "--since", "not-a-cursor"])
doc = one_document("events, a bad cursor", code, out, err, "error", want_code=2)
ck("events, a bad cursor: its code", doc.get("error", {}).get("code") == "bad_cursor", doc)
code, out, err = tares(env, ["events"])
ck("events without --json: a usage line on stderr, exit 2", code == 2 and out == "" and "usage: tares factory events" in err, (code, out, err))
code, out, err = tares(env, ["events", "--json", "--nope"])
doc = one_document("events, an unknown argument", code, out, err, "error", want_code=2)
ck("events, an unknown argument: usage", doc.get("error", {}).get("code") == "usage", doc)
try:
    contract.fail("custom_code", "it broke", 7)
    ck("fail() leaves the process", False)
except SystemExit as e:
    ck("fail() raises SystemExit with the code it was given", e.code == 7, e.code)

# ---------- events ----------

print("== events, in process")
EV = TOP / "events.jsonl"


def ev(n, station="shop-build-m1"):
    return {"at": f"2026-11-09T10:00:0{n}Z", "station": station, "event": f"step{n}", "detail": f"detail {n}"}


def append(path, *objs, raw=None):
    with open(path, "ab") as f:
        for o in objs:
            f.write((json.dumps(o, separators=(",", ":")) + "\n").encode("utf-8"))
        if raw is not None:
            f.write(raw)


def names(items):
    return [e["event"] for _c, e in items]


append(EV, ev(1), ev(2), ev(3))
got = list(events.follow(EV, tail=False))
ck("three events written, three read", names(got) == ["step1", "step2", "step3"], got)
ck("every cursor is <file id>:<offset>", all(re.fullmatch(r"\d+:\d+", c) for c, _e in got), got)
ck("the last cursor is the end of the file", got[-1][0].split(":")[1] == str(EV.stat().st_size) and got[-1][0].split(":")[0] == str(EV.stat().st_ino), got[-1])
after_second = list(events.follow(EV, got[1][0], tail=False))
ck("--since after the 2nd gives only the 3rd", names(after_second) == ["step3"], after_second)
ck("--since the last cursor gives nothing", list(events.follow(EV, got[2][0], tail=False)) == [])
ck("a cursor is not interpreted: the same one twice gives the same", list(events.follow(EV, got[1][0], tail=False)) == after_second)
ck("a cursor of another file starts at 0", names(events.follow(EV, f"{EV.stat().st_ino + 12345}:{got[1][0].split(':')[1]}", tail=False)) == ["step1", "step2", "step3"])
ck("a cursor past the end starts at 0", names(events.follow(EV, f"{EV.stat().st_ino}:99999", tail=False)) == ["step1", "step2", "step3"])
for bad in ("abc", "1:", ":2", "1:2:3", "1:-2"):
    try:
        list(events.follow(EV, bad, tail=False))
        ck(f"a malformed cursor {bad!r} is refused", False)
    except events.BadCursorError:
        ck(f"a malformed cursor {bad!r} is refused", True)
ck("a missing file is no events", list(events.follow(TOP / "none.jsonl", tail=False)) == [])
garbage = TOP / "garbage.jsonl"
append(garbage, ev(1), raw=b"not json\n[1,2]\n\n\xff\xfe\n")
append(garbage, ev(2))
ck("lines that are not an event object are skipped", names(events.follow(garbage, tail=False)) == ["step1", "step2"])


class Script:
    """Drives follow() without waiting: at each poll the next step runs, then follow() looks at the file."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.done = False

    def sleep(self, _s):
        if self.steps:
            self.steps.pop(0)()
        else:
            self.done = True

    def stop(self):
        return self.done


def run_script(path, steps, since=None):
    s = Script(steps)
    return list(events.follow(path, since, poll=0, stop=s.stop, sleep=s.sleep))


# truncation: the file is emptied and written again; the reader starts over, sends the new lines once
fol = TOP / "follow.jsonl"
append(fol, ev(1), ev(2), ev(3))


def truncate_and_write():
    with open(fol, "wb") as f:  # same file id, size 0
        f.write(b"")
    append(fol, ev(4))


got = run_script(fol, [truncate_and_write, lambda: append(fol, ev(5))])
ck("truncated: what was there, then the new lines", names(got) == ["step1", "step2", "step3", "step4", "step5"], names(got))
ck("truncated: the file id did not change", len({c.split(":")[0] for c, _e in got}) == 1)

# rotation: the file is renamed and a new one starts at its place; the new file is read from 0
rot = TOP / "rotate.jsonl"
append(rot, ev(1), ev(2))


def rotate():
    os.replace(rot, TOP / "rotate.jsonl.1")
    append(rot, ev(3))


got = run_script(rot, [rotate, lambda: append(rot, ev(4))])
ck("rotated: the new file is followed from its start", names(got) == ["step1", "step2", "step3", "step4"], names(got))
ck("rotated: the file id changed", len({c.split(":")[0] for c, _e in got}) == 2, [c for c, _e in got])
ck("rotated: nothing from the old file is sent twice", len(names(got)) == len(set(names(got))))
# a reader that was behind the rotation (its cursor names the old file) gets the new one from 0
old_cursor = got[1][0]
got2 = list(events.follow(rot, old_cursor, tail=False))
ck("reconnecting with a cursor of the rotated-away file reads the new file from 0", names(got2) == ["step3", "step4"], names(got2))

# a missing file, then it appears; a file removed and made again
late = TOP / "late.jsonl"
got = run_script(late, [lambda: None, lambda: append(late, ev(1)), lambda: None])
ck("a file that appears later is read", names(got) == ["step1"], names(got))
gone = TOP / "gone.jsonl"
append(gone, ev(1))


def remove_and_recreate():
    os.remove(gone)
    append(gone, ev(2))


got = run_script(gone, [remove_and_recreate, lambda: append(gone, ev(3))])
ck("a file removed and made again is read from its start", names(got) == ["step1", "step2", "step3"], names(got))

# a line still being written is waited for, and sent once
half = TOP / "half.jsonl"
line = (json.dumps(ev(2), separators=(",", ":")) + "\n").encode("utf-8")
append(half, ev(1), raw=line[:20])
gen_steps = [lambda: append(half, raw=b""), lambda: append(half, raw=line[20:])]
got = run_script(half, gen_steps)
ck("a half-written line is sent whole, after its end, once", names(got) == ["step1", "step2"], names(got))
ck("...and its cursor is the end of the line", got[-1][0].endswith(f":{half.stat().st_size}"), got[-1])

# a reconnect in the middle of a follow loses nothing and repeats nothing
mid = TOP / "mid.jsonl"
append(mid, ev(1), ev(2))
first = run_script(mid, [])
second = run_script(mid, [lambda: append(mid, ev(3))], since=first[-1][0])
ck("follow, reconnect with the last cursor: only what is new", names(first) == ["step1", "step2"] and names(second) == ["step3"], (names(first), names(second)))

# ---------- the real command, over a pipe ----------

print("== events --follow --json, the real command")
env, tmp = world(crew_of(), sessions())
chk = subprocess.run([sys.executable, "-c",
                      "from tares.crew.paths import CrewPaths; print(CrewPaths.from_env().events)"],
                     env=dict(env, PYTHONPATH=str(ROOT)), capture_output=True, text=True, cwd=str(ROOT))
crew_events = Path(chk.stdout.strip())
ck("the events file is in the crew home", crew_events.parent.is_dir(), chk.stdout + chk.stderr)
append(crew_events, ev(1), ev(2))


def spawn(extra):
    e = dict(env, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", TARES_FACTORY_ENGINE="python")
    e.pop("PYTHONSTARTUP", None)
    return subprocess.Popen([sys.executable, "-m", "tares.crew", "events", "--follow", "--json"] + extra, env=e,
                            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def read_lines(proc, n, timeout=20):
    """n lines from the process, or what arrived by the timeout."""
    lines = []

    def reader():
        while len(lines) < n:
            raw = proc.stdout.readline()
            if not raw:
                return
            lines.append(raw.decode("utf-8"))

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    t.join(timeout)
    return lines


def stop(proc):
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(10)
    for s in (proc.stdout, proc.stderr):
        s.close()


p1 = spawn([])
try:
    first = read_lines(p1, 2)
    docs = [json.loads(x) for x in first]
    ck("the stream: two lines for the two events", [d["event"]["event"] for d in docs] == ["step1", "step2"], first)
    ck("the stream: every line validates against events.json", all(not problems("events", d) for d in docs), [problems("events", d) for d in docs])
    append(crew_events, ev(3))
    third = read_lines(p1, 1)
    ck("the stream: an event written later arrives", third and json.loads(third[0])["event"]["event"] == "step3", third)
    fixture("events.jsonl", stable("".join(
        json.dumps(dict(json.loads(x), cursor=re.sub(r"^\d+", "1", json.loads(x)["cursor"])), separators=(",", ":"),
                   ensure_ascii=False) + "\n" for x in first + third), tmp))
    last_cursor = json.loads(third[0])["cursor"] if third else ""
finally:
    stop(p1)
append(crew_events, ev(4), ev(5))
p2 = spawn(["--since", last_cursor])
try:
    again = read_lines(p2, 2)
    ck("a reconnect with --since replays nothing older", [json.loads(x)["event"]["event"] for x in again] == ["step4", "step5"], again)
    # rotate the file under the running command
    os.replace(crew_events, crew_events.with_name("events.jsonl.1"))
    append(crew_events, ev(6))
    rotated = read_lines(p2, 1)
    ck("the running command follows a rotated file", rotated and json.loads(rotated[0])["event"]["event"] == "step6", rotated)
    with open(crew_events, "wb"):
        pass  # truncate
    time.sleep(1.0)  # a poll sees the empty file; a truncation and a write inside one poll are not seen
    append(crew_events, ev(7))
    truncated = read_lines(p2, 1)
    ck("...and a truncated one", truncated and json.loads(truncated[0])["event"]["event"] == "step7", truncated)
finally:
    stop(p2)
p3 = spawn([])
try:
    p3.stdout.close()  # the reader goes away: the command must leave quietly, not with a traceback
    code = p3.wait(20)
    err = p3.stderr.read().decode("utf-8", errors="replace")
    ck("a reader that goes away is not a crash", "Traceback" not in err, err[-300:])
except subprocess.TimeoutExpired:
    # nothing was written after the close, so the command has no reason to notice yet: that is fine, stop it
    ck("a command that has nothing to write waits for its reader", True)
finally:
    try:
        p3.kill()
    except OSError:
        pass
    p3.wait(10)

# ---------- the samples ----------

print("== the samples")
for name in ("status.json", "roster.json", "desk.json", "error.json"):
    path = FIXTURES / name
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        ck(f"sample {name} is JSON", False, e)
        continue
    bad = problems(path.stem, doc)
    ck(f"sample {name} validates against {path.stem}.json", not bad, bad)
sample_lines = (FIXTURES / "events.jsonl").read_text(encoding="utf-8").splitlines() if (FIXTURES / "events.jsonl").is_file() else []
ck("sample events.jsonl validates line by line against events.json",
   len(sample_lines) == 3 and all(not problems("events", json.loads(x)) for x in sample_lines), sample_lines)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
