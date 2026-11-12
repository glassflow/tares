"""The parity harness (tests/factory/parity.py): the normaliser, the scenario environment, a scenario run on
Python alone against its recorded result, the recorded labels and the coverage count.

Checks: what the normaliser removes (temp folder, timestamps, pids, ids, the checkout's folder, the kit fingerprint,
the plugin version, the `tares` binary, key order) and what it keeps; `parity.py crew_basic` exits 0 and matches
its recorded result; a scenario whose command the Python engine has not moved over (a made-up word) fails
with exit 1 (not ported is a failure, not a skip); a failing check exits 1; `--record` stores what a run printed and wrote, and a later run
that prints the same matches while one that prints something else, writes another crew file, other contents or makes
another Claude Code call exits 1 and names the file (stub commands through PARITY_CMD_PYTHON); a scenario with no
recorded result fails; every recorded result belongs to a scenario and every scenario has one; `--coverage` counts
the labels of parity_labels.json (pinned against a fixture), a `covers` label missing from its suite is an ERROR,
`--require-full` exits 1 below 100%; an unknown scenario exits 2; the scenario environment drops the person's
CLAUDE_CONFIG_DIR, XDG_* and GH_* settings.

Run: .venv/bin/python tests/test_parity.py
"""
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
FACTORY = ROOT / "tests" / "factory"
PARITY = FACTORY / "parity.py"
sys.path.insert(0, str(FACTORY))

import parity  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def run(*args, env=None):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("PARITY_", "TARES_FACTORY_"))}
    e.update(env or {})
    e["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run([sys.executable, str(PARITY), *args], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=e, cwd=str(ROOT), timeout=1500)
    return p.returncode, p.stdout + p.stderr


# --- the normaliser -------------------------------------------------------------------------------
tmp = Path(tempfile.gettempdir()) / "parity-x-1"
n = parity.norm_text(f"in {tmp}/crew at 2026-11-07T10:11:12Z and 2026-11-07 10:11:12.123+01:00", tmp)
ck("the temp folder and timestamps are replaced", "<TMP>/crew" in n and n.count("<TS>") == 2 and str(tmp) not in n, n)
ck("an epoch time is replaced where it is a time", parity.norm_text("at 1762510272 and since 1762510272123, started_at=1762510272")
   == "at <TS> and since <TS>, started_at=<TS>")
ck("...also as a quoted JSON number in text", parity.norm_text('{"updated_at": 1762510272}') == '{"updated_at": <TS>}')
ck("a ten-digit number that is not a time is kept",
   parity.norm_text("port 1762510272, order 1234567890 shipped, id 1762510272") == "port 1762510272, order 1234567890 shipped, id 1762510272")
ck("an epoch string under a time key in JSON is replaced, under another key it is kept",
   parity.norm_file('{"started_at": "1762510272", "ref": "1762510272"}', "s.json") == parity.render_json({"ref": "1762510272", "started_at": "<TS>"}))
ck("a pid in text is replaced", parity.norm_text("another factory command (pid 4242) is starting")
   == "another factory command (pid <PID>) is starting")
ck("a session id and a job id are replaced",
   parity.norm_text("backgrounded · a1b2c3d4 · x and session-7") == "backgrounded · <ID> · x and <ID>")
ck("an eight-digit number or an eight-letter hex word is kept",
   parity.norm_text("total 87654321 bytes, port 12345678, the word deadbeef") == "total 87654321 bytes, port 12345678, the word deadbeef")
ck("a job id after stop, rm, job or id is replaced", parity.norm_text("stop 0000000a, rm 0000000b, job 0000000c, id=0000000d") == "stop <ID>, rm <ID>, job <ID>, id=<ID>")
ck("a string under an id key in JSON is replaced, under another key it is kept",
   parity.norm_output('{"id": "0000000a", "name": "deadbeef"}') == parity.render_json({"id": "<ID>", "name": "deadbeef"}))
_me = __import__("getpass").getuser()
ck("the login of whoever froze the kit is a placeholder, in text and in JSON",
   parity.norm_text(f"frozen since 2026-11-07T10:11:12Z by {_me}; ok") == "frozen since <TS> by <USER>; ok"
   and parity.norm_text(f'{{"by": "{_me}"}}') == '{"by": "<USER>"}'
   and parity.norm_json({"by": _me}) == {"by": "<USER>"})
ck("...but the same word elsewhere, or a longer login, is data",
   parity.norm_text(f"{_me} went by {_me}x and bye {_me}") == f"{_me} went by {_me}x and bye {_me}")
with mock.patch.object(parity, "is_windows", lambda: True):
    _wp = parity.norm_text('"cwd": "<TMP>\\\\crew\\\\a", and <REPO>\\sub\\f.py, plain <TMP>/x')
ck("on Windows a path under a placeholder has its backslashes (single or doubled) read as slashes",
   _wp == '"cwd": "<TMP>/crew/a", and <REPO>/sub/f.py, plain <TMP>/x')
with mock.patch.object(parity, "is_windows", lambda: False):
    _np = parity.norm_text("a\\b <TMP>\\c")
ck("...and elsewhere a backslash is kept", _np == "a\\b <TMP>\\c", _np)
_today = __import__("time").strftime("%Y-%m-%d", __import__("time").gmtime())
ck("today's date is a placeholder (a grant says when it was made), another day is data, a timestamp is a timestamp",
   parity.norm_text(f"granted on {_today} ({_today}) at {_today}T10:11:12Z, not 2001-02-03 or x{_today}0")
   == "granted on <DATE> (<DATE>) at <TS>, not 2001-02-03 or x" + _today + "0")
ck("a grants fingerprint (it holds the day a grant was made) is a placeholder", parity.norm_json({"grants": "925418911"}) == {"grants": "<GRANTS>"})
ck("a kit fingerprint is a placeholder wherever a kit is named: its stations, a freeze's version, a kit-newer event, the text",
   parity.norm_json({"kit": {"crew-reviewer": "2299798679"}, "frozen": {"version": "2299798679"},
                     "event": "kit-newer", "detail": "3804381130", "n": "12345678"})
   == {"kit": {"crew-reviewer": "<KIT>"}, "frozen": {"version": "<KIT>"}, "event": "kit-newer", "detail": "<KIT>", "n": "12345678"}
   and parity.norm_text("kit 3804381130 is newer than the frozen kit 2299798679; x")
   == "kit <KIT> is newer than the frozen kit <KIT>; x")
ck("a uuid is replaced", parity.norm_text("id 123e4567-e89b-12d3-a456-426614174000") == "id <ID>")
ck("words and small numbers are kept", parity.norm_text("started 3 stations, port 8788") == "started 3 stations, port 8788")
ck("CRLF becomes LF", parity.norm_text("a\r\nb") == "a\nb")
# A Windows temp folder (TR-1066): its backslashes are doubled inside JSON text, its drive letter can change case,
# and a name can hold a space and a letter outside ASCII. A Path with backslashes in its name is enough here.
wtmp = Path("C:\\Users\\Jane Do\u00e9\\parity-x")
nw = parity.norm_text('{"cwd": "C:\\\\Users\\\\Jane Do\u00e9\\\\parity-x\\\\crew"} and C:\\Users\\Jane Do\u00e9\\parity-x\\home', wtmp)
ck("a Windows temp folder is replaced as a plain path and with its backslashes doubled in JSON text",
   nw == '{"cwd": "<TMP>\\\\crew"} and <TMP>\\home', nw)
with mock.patch.object(parity, "is_windows", lambda: True):
    nc = parity.norm_text("in c:\\users\\jane do\u00e9\\PARITY-X\\crew", wtmp)
ck("...and on Windows the drive letter and folder case do not matter", nc == "in <TMP>/crew", nc)
with mock.patch.object(parity, "is_windows", lambda: False):
    nc = parity.norm_text("in c:\\users\\jane do\u00e9\\PARITY-X\\crew", wtmp)
ck("...elsewhere case still matters", "<TMP>" not in nc, nc)
# What Windows spells differently (TR-1172): a quoted kit path, claude.cmd, a settings file, the order of files, a junction
with mock.patch.object(parity, "is_windows", lambda: True):
    q1 = parity.norm_text('tares hook statusline --plugin-root "<TMP>/kit"')
    q2 = parity.norm_text('tares hook statusline --plugin-root "<TMP>\\kit" and <TMP>\\bin\\claude.cmd (2.1.0)')
ck("on Windows a quoted kit path and claude.cmd read as bash printed them",
   q1 == "tares hook statusline --plugin-root <TMP>/kit" and q2 == "tares hook statusline --plugin-root <TMP>/kit and <TMP>/bin/claude (2.1.0)", (q1, q2))
with mock.patch.object(parity, "is_windows", lambda: False):
    q3 = parity.norm_text('--plugin-root "<TMP>/kit"')
ck("...and elsewhere the quotes stay", q3 == '--plugin-root "<TMP>/kit"', q3)
_use = {"args": ["-p", "--no-session-persistence", "--settings", "C:/t/x.json", "--verbose", "/usage"]}
_ctx = {"args": ["-p", "--no-session-persistence", "--settings", "C:/t/x.json", "--verbose", "/context"]}
_bg = {"args": ["--bg", "--settings", "C:/t/x.json"]}
with mock.patch.object(parity, "is_windows", lambda: True):
    _u, _b = parity._settings_arg(json.loads(json.dumps(_use))), parity._settings_arg(json.loads(json.dumps(_bg)))
    _c = parity._settings_arg(json.loads(json.dumps(_ctx)))
ck("on Windows a plan read's settings file reads as the JSON text bash passed; a context read's and a launch's file stay a placeholder",
   _u["args"][3] == '{"disableAllHooks": true}' and _b["args"][2] == "<SETTINGS-FILE>" and _c["args"][3] == "<SETTINGS-FILE>", (_u, _b, _c))
_ft = Path(tempfile.mkdtemp(prefix="parity-files-"))
for _n in ("crew.json", "HALT", "a-b", "a/b"):
    (_ft / _n).parent.mkdir(parents=True, exist_ok=True)
    (_ft / _n).write_text("x")
ck("files are listed in the order bash lists them: by text, upper case first, a folder's files together",
   [p.relative_to(_ft).as_posix() for p in parity._files_under(_ft)] == ["HALT", "a/b", "a-b", "crew.json"],
   str([p.relative_to(_ft).as_posix() for p in parity._files_under(_ft)]))
if hasattr(os, "symlink"):
    _out = Path(tempfile.mkdtemp(prefix="parity-out-"))
    (_out / "keep").write_text("x")
    try:
        os.symlink(_out, _ft / "door", target_is_directory=True)
        ck("a link to a folder is not entered", not any("door" in p.parts for p in parity._files_under(_ft)))
    except OSError:
        pass

j1 = parity.norm_file('{"b": 1, "pid": 4242, "started_at": 1762510272, "a": "x"}', "s.json")
j2 = parity.norm_file('{\n  "a": "x",\n  "started_at": 1762510999,\n  "pid": 17,\n  "b": 1\n}', "s.json")
ck("JSON files compare by content, not key order, pid or time", j1 == j2, f"{j1!r} vs {j2!r}")
ck("a different value still differs", j1 != parity.norm_file('{"b": 2, "pid": 1, "started_at": 1, "a": "x"}', "s.json"))
l1 = parity.norm_file('{"cwd":"/a","args":["--bg"]}\n{"args":["stop","1"],"cwd":"/a"}\n', "calls.jsonl")
l2 = parity.norm_file('{"args":["--bg"],"cwd":"/a"}\n{"cwd":"/a","args":["stop","1"]}', "calls.jsonl")
ck("JSON lines compare by content, not key order", l1 == l2)
ck("the order of JSON lines still counts", l1 != parity.norm_file(
    '{"args":["stop","1"],"cwd":"/a"}\n{"args":["--bg"],"cwd":"/a"}', "calls.jsonl"))
o1 = parity.norm_output('{"b": [1], "a": {"id": "a1b2c3d4"}}')
o2 = parity.norm_output('{\n"a": {"id": "ffeeddcc"},\n"b": [1]}\n')
ck("printed JSON compares by content", o1 == o2, f"{o1!r} vs {o2!r}")
ck("printed text keeps its words", "no Tares answers" in parity.norm_output("no Tares answers at http://x\n"))
# What differs between machines is not in an expected result.
ck("the checkout's folder is replaced", parity.norm_text(f"cwd {parity.ROOT}/tests and {parity.ROOT.as_posix()}") == "cwd <REPO>/tests and <REPO>")
ver = parity.plugin_version()
ck("the plugin's version is replaced after the word version, kept elsewhere",
   not ver or parity.norm_text(f"plugin version {ver} (port {ver})") == f"plugin version <PLUGIN-VERSION> (port {ver})")
ck("the kit fingerprint of a station is replaced under the key kit and in freeze and upgraded events",
   parity.norm_json({"kit": "1234567890", "x": {"event": "freeze", "detail": "42"}, "y": {"event": "other", "detail": "42"}})
   == {"kit": "<KIT>", "x": {"event": "freeze", "detail": "<KIT>"}, "y": {"event": "other", "detail": "42"}})
note = "  \u2713 once a day the watch also removes Docker's leftovers (docker is on this machine)\n"
ck("the Docker onboarding line of crew up is left out", parity.norm_text("a\n" + note + "b\n") == "a\nb\n")
tbin = parity.shutil.which("tares")
ck("the path of the tares command is replaced", not tbin or parity.norm_text(f"FACTORY_TARES={tbin}") == "FACTORY_TARES=<TARES-BIN>")

work = Path(tempfile.mkdtemp(prefix="test-parity-"))

# --- the scenario environment ---------------------------------------------------------------------
lent = {"CLAUDE_CONFIG_DIR": "/people/claude", "CLAUDE_JOB_DIR": "/people/jobs", "XDG_CONFIG_HOME": "/people/xdg",
        "XDG_DATA_HOME": "/people/xdg-data", "GH_TOKEN": "ghp_not_real", "GH_HOST": "example.com", "TARES_X": "1",
        "FACTORY_X": "1", "PARITY_CMD_PYTHON": "x", "FAKE_CLAUDE_BG_DELAY": "9", "PARITY_KEEP_NOT": "1", "KEEP_ME": "yes"}
(work / "env").mkdir()
with mock.patch.dict(os.environ, lent):
    env_b = parity.crew_env(work / "env")
ck("the scenario environment drops the person's Claude, XDG, GH, crew, Tares and fake settings",
   not [k for k in lent if k != "KEEP_ME" and k in env_b], str([k for k in lent if k in env_b and k != "KEEP_ME"]))
ck("...keeps the rest and points HOME at the scenario and pins the Python engine",
   env_b.get("KEEP_ME") == "yes" and env_b["HOME"] == str(work / "env" / "home") and env_b.get("TARES_FACTORY_ENGINE") == "python", str(env_b.get("HOME")))

# TR-1066: the fake Claude Code is a launcher named `claude`, first on PATH and also CLAUDE_BIN.
launcher = Path(env_b["CLAUDE_BIN"])
ck("the fake Claude Code is named claude (claude.exe on Windows) and is CLAUDE_BIN", launcher.stem == "claude" and launcher.is_file(), str(launcher))
ck("...in the first folder of PATH", env_b["PATH"].split(os.pathsep)[0] == str(launcher.parent), env_b["PATH"][:200])
found_claude = shutil.which("claude", path=env_b["PATH"])
ck("...so `claude` by name finds it, not a real one", found_claude is not None and Path(found_claude).resolve() == launcher.resolve(), str(found_claude))
r = subprocess.run([found_claude or str(launcher), "--version"], env=env_b, capture_output=True, text=True, encoding="utf-8", errors="replace")
ck("...and it runs the Python fake (the version it prints, exit 0)", r.returncode == 0 and r.stdout.strip() != "", f"{r.returncode} {r.stdout!r} {r.stderr!r}")
r = subprocess.run([found_claude or str(launcher), "agents"], env={**env_b, "FAKE_CLAUDE_AGENTS_FAIL": "1"}, capture_output=True,
                   text=True, encoding="utf-8", errors="replace")
ck("...and the fake's exit code reaches the caller (a failing `claude agents` exits 1)", r.returncode == 1, f"{r.returncode} {r.stderr!r}")
if sys.platform != "win32":
    ck("on POSIX the launcher is an executable script", os.access(launcher, os.X_OK) and launcher.read_text(encoding="utf-8").startswith("#!/bin/sh"))
else:
    raw = launcher.read_bytes()
    ck("on Windows the launcher is CRLF, passes the exit code on, and holds no non-ASCII path",
       b"\r\n" in raw and b"exit /b %ERRORLEVEL%" in raw and all(c < 128 for c in raw), raw[:200])

# --- the recorded labels and results ---------------------------------------------------------------
labels = parity.recorded_labels()
ck("the crew suite lists its checks", "without Tares crew up stops plainly" in labels["crew"])
ck("a suite that was one script counts as one scenario", labels.get("parse") == ["(the whole parse suite)"], labels.get("parse"))
ck("there is no wrapper suite any more", not [s for s in labels if s.endswith("_python")], str(sorted(labels)))
ck("each scenario covers checks that exist", all(label in labels[sc.suite] for sc in parity.SCENARIOS.values() for label in sc.covers))
recorded = {p.stem for p in parity.EXPECTED.glob("*.txt")}
ck("every scenario has a recorded result", not (set(parity.SCENARIOS) - recorded), str(sorted(set(parity.SCENARIOS) - recorded)[:5]))
ck("every recorded result belongs to a scenario", not (recorded - set(parity.SCENARIOS)), str(sorted(recorded - set(parity.SCENARIOS))[:5]))
ck("no recorded result holds this machine's checkout, temp folder or the plugin version",
   not [n for n in recorded if str(parity.ROOT) in (parity.EXPECTED / f"{n}.txt").read_text(encoding="utf-8")
        or (parity.plugin_version() and f"version {parity.plugin_version()}" in (parity.EXPECTED / f"{n}.txt").read_text(encoding="utf-8"))])

fx = work / "fx"
fx.mkdir()
(fx / "labels.json").write_text(json.dumps({"a": ["first", "in a loop $x", 'say "hi"'], "b": ["(the whole b suite)"]}))


class Fixture:
    """parity reads the fixture's labels and records into the fixture's folder for the length of the with block."""

    def __enter__(self):
        self.saved = (parity.LABELS, parity.EXPECTED, dict(parity.SCENARIOS))
        parity.LABELS, parity.EXPECTED = fx / "labels.json", fx / "expected"
        return self

    def __exit__(self, *a):
        parity.LABELS, parity.EXPECTED = self.saved[:2]
        parity.SCENARIOS.clear()
        parity.SCENARIOS.update(self.saved[2])


stub = work / "stub.py"
stub.write_text(
    "import os, subprocess, sys\n"
    "mode = sys.argv[1]\n"
    "print('no Tares answers at http://127.0.0.1:9. Start it with tares up.')\n"
    "crew = os.environ['FACTORY_CREW_HOME']\n"
    "def write(name, text):\n"
    "    os.makedirs(crew, exist_ok=True)\n"
    "    open(os.path.join(crew, name), 'w').write(text)\n"
    "if mode == 'file1': write('x.json', '{\"a\": 1}')\n"
    "if mode == 'file2': write('x.json', '{\"a\": 2}')\n"
    "if mode == 'file3': write('y.txt', 'hello')\n"
    "if mode == 'unported':\n"
    "    sys.stderr.write('\"x\" is not yet ported to the Python engine\\n'); sys.exit(2)\n"
    "if mode == 'callv': subprocess.run([os.environ['CLAUDE_BIN'], '--version'], capture_output=True)\n"
    "if mode == 'calla': subprocess.run([os.environ['CLAUDE_BIN'], 'agents', '--json'], capture_output=True)\n"
    "sys.exit(1)\n")


def stubbed(mode):
    return {"PARITY_CMD_PYTHON": shlex.join([sys.executable, str(stub), mode])}


def recorded_with(mode):
    """Record crew_basic from the stub in `mode` into the fixture folder."""
    with mock.patch.dict(os.environ, stubbed(mode)):
        return parity.record_scenario(parity.SCENARIOS["crew_basic"], out=lambda _l: None)


def checked_with(mode):
    """(exit code, output) of crew_basic run from the stub in `mode` against the fixture's recorded result."""
    lines = []
    with mock.patch.dict(os.environ, stubbed(mode)):
        code, _ok = parity.evaluate_python(parity.SCENARIOS["crew_basic"], out=lines.append)
    return code, "\n".join(lines)


# --- what the engine writes -----------------------------------------------------------------------
# Every stub prints the same line and exits 1; only what it writes differs.
with Fixture():
    ck("a run is recorded", recorded_with("plain") == 0 and (fx / "expected" / "crew_basic.txt").is_file())
    code, text = checked_with("plain")
    ck("a stub that prints and writes the same matches (exit 0)", code == 0 and "MATCH" in text, f"{code}\n{text}")
    recorded_with("file1")
    code, text = checked_with("file1")
    ck("...also when both write the same crew file", code == 0 and "MATCH" in text, f"{code}\n{text}")
    recorded_with("callv")
    code, text = checked_with("callv")
    ck("...and when both make the same Claude Code call", code == 0 and "MATCH" in text, f"{code}\n{text}")
    recorded_with("plain")
    code, text = checked_with("file1")
    ck("the same output but a crew file that was not recorded: exit 1, the diff names the file",
       code == 1 and "differs from the result recorded" in text and "== file crew/x.json ==" in text, f"{code}\n{text}")
    recorded_with("file1")
    code, text = checked_with("plain")
    ck("...a recorded crew file that is not written: exit 1, the diff names the file", code == 1 and "crew/x.json" in text, f"{code}\n{text}")
    code, text = checked_with("file2")
    ck("...a crew file with other contents: exit 1, the diff shows both",
       code == 1 and "crew/x.json" in text and '-  "a": 1' in text and '+  "a": 2' in text, f"{code}\n{text}")
    code, text = checked_with("file3")
    ck("...another file name: exit 1, both names in the diff", code == 1 and "crew/x.json" in text and "crew/y.txt" in text, f"{code}\n{text}")
    recorded_with("callv")
    code, text = checked_with("calla")
    ck("the same output but another call to Claude Code: exit 1, the diff names the call record",
       code == 1 and "== file claude/calls.jsonl ==" in text and "agents" in text and "--version" in text, f"{code}\n{text}")
    recorded_with("plain")
    code, text = checked_with("callv")
    ck("...a call that was not recorded: exit 1", code == 1 and "claude/calls.jsonl" in text, f"{code}\n{text}")
    (fx / "expected" / "crew_basic.txt").unlink()
    code, text = checked_with("plain")
    ck("a scenario with no recorded result fails and says how to record it", code == 1 and "no recorded result for crew_basic" in text
       and "--record crew_basic" in text, f"{code}\n{text}")
    with mock.patch.dict(os.environ, {**stubbed("plain"), "PARITY_FAIL": "python"}):
        lines = []
        rc = parity.record_scenario(parity.SCENARIOS["crew_basic"], out=lines.append)
    ck("a scenario that fails its checks is not recorded", rc == 1 and not (fx / "expected" / "crew_basic.txt").exists(), "\n".join(lines))

# --- the coverage count and its error path --------------------------------------------------------
crew_basic = parity.SCENARIOS["crew_basic"]


def fx_scenario(name, label):
    return parity.Scenario(name=name, suite="a", steps=crew_basic.steps, checks=[("it ran", lambda r: True)], covers=[label], about="fixture")


def fx_record(name):
    (fx / "expected").mkdir(exist_ok=True)
    with mock.patch.dict(os.environ, stubbed("plain")):
        parity.record_scenario(parity.SCENARIOS[name], out=lambda _l: None)


# PARITY_JOBS=1: the fixture scenarios exist only in this process, a worker process would not have them
with Fixture(), mock.patch.dict(os.environ, {**stubbed("plain"), "PARITY_JOBS": "1"}):
    parity.SCENARIOS.clear()
    parity.SCENARIOS["fx_ok"] = fx_scenario("fx_ok", "first")
    fx_record("fx_ok")
    lines = []
    rc = parity.coverage(False, out=lines.append)
    text = "\n".join(lines)
    ck("coverage counts one scenario of a three-label suite: a: 1/3, b: 0/1, 1 of 4 (25.0%)",
       rc == 0 and "a: 1/3" in text and "b: 0/1" in text and "coverage: 1 of 4 scenarios pass against Python (25.0%) in 2 suites" in text, text)
    ck("...and lists the covered check as passing", "PASS python  (fx_ok)" in text and "first" in text, text)
    lines = []
    rc = parity.coverage(True, out=lines.append)
    ck("--require-full exits 1 below 100% and says how many are missing", rc == 1 and "wants every scenario ported, 3 are not" in "\n".join(lines), "\n".join(lines))
    (fx / "expected" / "fx_ok.txt").write_text("not what it prints\n")
    lines = []
    rc = parity.coverage(False, out=lines.append)
    ck("a scenario that differs from its recorded result does not count", "coverage: 0 of 4 scenarios" in "\n".join(lines), "\n".join(lines))
    parity.SCENARIOS.clear()
    parity.SCENARIOS["fx_bad"] = fx_scenario("fx_bad", "a label the suite does not have")
    fx_record("fx_bad")
    lines = []
    rc = parity.coverage(False, out=lines.append)
    text = "\n".join(lines)
    ck("a covers label missing from its suite is an ERROR: exit 1, it names scenario, suite and label",
       rc == 1 and "ERROR scenario fx_bad covers a check that is not in parity_labels.json under a: a label the suite does not have" in text, text)
    ck("...and it counts for nothing", "coverage: 0 of 4 scenarios" in text, text)

# --- the command line -----------------------------------------------------------------------------
code, text = run("--list")
ck("--list names crew_basic", code == 0 and "crew_basic" in text.split(), text)
code, text = run("no_such_scenario")
ck("an unknown scenario exits 2", code == 2 and "no scenario" in text, f"{code} {text}")
code, text = run()
ck("no scenario exits 2", code == 2, f"{code} {text}")
code, text = run("--require-full")
ck("--require-full alone exits 2", code == 2, f"{code} {text}")
code, text = run("--python-only", "crew_basic")
ck("a flag it no longer has exits 2", code == 2, f"{code} {text}")

code, text = run("crew_basic")
ck("crew_basic exits 0", code == 0, f"{code}\n{text}")
ck("crew_basic: python passes its checks and matches the recorded result", "PASS python: 1 checks" in text and "MATCH" in text, text)
code, text = run("crew_basic", env={"PARITY_FAIL": "python"})
ck("a failing check exits 1", code == 1 and "FAIL python" in text, f"{code}\n{text}")

# A command the Python engine has not ported (a made-up word, so a later port cannot turn it ported): the run skips.
unported = parity.Scenario(name="fx_unported", suite="a", steps=[parity.Step(["no-such-command"])],
                           checks=[("it ran", lambda r: True)], covers=["x"], about="fixture")
lines = []
with mock.patch.dict(os.environ, stubbed("unported")):
    code, passes = parity.evaluate_python(unported, out=lines.append)
ck("an unported command fails: exit 1, named, not counted as passing (a SKIP would let a dropped PORTED entry through)",
   any("not yet ported" in x and x.startswith("FAIL python") for x in lines) and code == 1 and passes is False, "\n".join(lines))

# The strict coverage count, without running the scenarios (--static: each counts as passing). CI runs every scenario in
# the `parity` job's shards (ci.yml), so this checks only that each recorded label has a scenario that names it.
code, text = run("--coverage", "--require-full", "--static")
last = [ln for ln in text.strip().splitlines() if ln.startswith("coverage: ")]
last = last[-1] if last else ""
full = last.startswith("coverage: ") and " of " in last and last.split()[1] == last.split()[3]
ck("--coverage --require-full --static prints a percentage and exits 0 at 100%",
   last.startswith("coverage: ") and "%" in last and "scenarios pass against Python" in last and code == (0 if full else 1),
   f"{code}\n{text[-400:]}")
ck("--coverage lists the crew suite", "crew: " in text and "without Tares crew up stops plainly" in text, text[-400:])
ck("every recorded check label has a scenario, so the count is full", full, last)
code, text = run("--static")
ck("--static alone exits 2", code == 2, f"{code} {text}")

# --shard=I/N: every Nth scenario of the sorted list, so N shards run each scenario once (main, with the run stubbed).
def shard_run(*argv):
    seen = []
    with mock.patch.object(parity, "evaluate_python", lambda sc, out=print: (seen.append(sc.name), (0, True))[1]):
        rc = parity.main(list(argv))
    return rc, seen


parts = [shard_run(f"--shard={i}/4") for i in range(1, 5)]
every = sorted(parity.SCENARIOS)
ck("four shards are disjoint and together every scenario", all(rc == 0 for rc, _ in parts)
   and sorted(x for _, p in parts for x in p) == every, str([len(p) for _, p in parts]))
ck("a shard is every Nth scenario of the sorted list", parts[1][1] == every[1::4], str(parts[1][1][:3]))
ck("a shard and a pattern run the union once each", sorted(shard_run("--shard=1/4", every[1])[1]) == sorted(every[0::4] + [every[1]]))
for bad in ("--shard=0/4", "--shard=5/4", "--shard=a/b", "--shard=1"):
    code, text = run(bad)
    ck(f"{bad} exits 2", code == 2 and "--shard=I/N" in text, f"{code} {text}")
code, text = run("--shard=1/%d" % len(every), "--list")
ck("--list wins over a shard", code == 0 and "crew_basic" in text.split(), text)

shutil.rmtree(work, ignore_errors=True)
print(f"{P} passed, {F} failed")
sys.exit(1 if F else 0)
