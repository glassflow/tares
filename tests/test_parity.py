"""The parity harness (tests/factory/parity.py): the normaliser, the crew_basic scenario, the exit codes,
and the scenario count gate.

Checks: what the normaliser removes (temp folder, timestamps, pids, ids, key order) and what it keeps;
`parity.py crew_basic` exits 0 and its Python side matches bash now that `crew up` is ported; a scenario
whose command the Python engine has not moved over (a made-up word) prints `SKIP python: not ported` and exits 0; a failing bash side exits 1; a Python side that prints the same as bash
matches, and one that prints something else exits 1; `--coverage` prints a percentage and exits 0;
`--require-full` exits 1 below 100%; an unknown scenario exits 2.

A difference in what the engines WRITE fails parity as surely as one in what they print: stub commands
(PARITY_CMD_BASH, PARITY_CMD_PYTHON) that print the same but write another crew file, other contents, or
make another Claude Code call must exit 1 and name the file; the same writes must match. Also: the
`--coverage` count is the number of `check "` lines of each suite (pinned against a fixture), a
`covers` label missing from its suite is an ERROR, a skipped bash side is an error off Windows and
fine on it, and the scenario environment drops the person's CLAUDE_CONFIG_DIR, XDG_* and GH_* settings.

The runs that need the real bash engine are skipped where bash and jq are not there (Windows). The
stub runs need neither.

Run: .venv/bin/python tests/test_parity.py
"""
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
ck("...and on Windows the drive letter and folder case do not matter", nc == "in <TMP>\\crew", nc)
with mock.patch.object(parity, "is_windows", lambda: False):
    nc = parity.norm_text("in c:\\users\\jane do\u00e9\\PARITY-X\\crew", wtmp)
ck("...elsewhere case still matters", "<TMP>" not in nc, nc)

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

work = Path(tempfile.mkdtemp(prefix="test-parity-"))

# --- the scenario environment ---------------------------------------------------------------------
lent = {"CLAUDE_CONFIG_DIR": "/people/claude", "CLAUDE_JOB_DIR": "/people/jobs", "XDG_CONFIG_HOME": "/people/xdg",
        "XDG_DATA_HOME": "/people/xdg-data", "GH_TOKEN": "ghp_not_real", "GH_HOST": "example.com", "TARES_X": "1",
        "FACTORY_X": "1", "PARITY_CMD_BASH": "x", "FAKE_CLAUDE_BG_DELAY": "9", "PARITY_KEEP_NOT": "1", "KEEP_ME": "yes"}
(work / "env").mkdir()
with mock.patch.dict(os.environ, lent):
    env_b = parity.crew_env(work / "env", "bash")
ck("the scenario environment drops the person's Claude, XDG, GH, crew, Tares and fake settings",
   not [k for k in lent if k != "KEEP_ME" and k in env_b], str([k for k in lent if k in env_b and k != "KEEP_ME"]))
ck("...keeps the rest and points HOME and the engine at the scenario",
   env_b.get("KEEP_ME") == "yes" and env_b["HOME"] == str(work / "env" / "home") and env_b["TARES_FACTORY_ENGINE"] == "bash", str(env_b.get("HOME")))

# TR-1066: the fake Claude Code is a launcher named `claude`, first on PATH and also CLAUDE_BIN.
launcher = Path(env_b["CLAUDE_BIN"])
ck("the fake Claude Code is named claude (claude.cmd on Windows) and is CLAUDE_BIN", launcher.stem == "claude" and launcher.is_file(), str(launcher))
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

# --- the suites and the count ---------------------------------------------------------------------
found = parity.suite_scenarios()
ck("every suite of tests/factory/ci_list.txt is counted", set(found) == set(parity.suites()) and len(found) > 0)
ck("the crew suite lists its checks", "without Tares crew up stops plainly" in found["crew"])
ck("a suite with no check line counts as one scenario", found.get("parse") == ["(the whole parse suite)"], found.get("parse"))
ck("each scenario covers checks that exist", all(label in found[sc.suite] for sc in parity.SCENARIOS.values() for label in sc.covers))

# The count is one scenario per `check "` line, in every suite, not one per suite or the first of each.
for suite, labels in found.items():
    lines = (FACTORY / f"{suite}_test.sh").read_text(encoding="utf-8").splitlines()
    n = sum(1 for ln in lines if ln.lstrip().startswith('check "'))
    ck(f"{suite}: the count is the number of check lines", len(labels) == (n or 1), f"{len(labels)} vs {n}")
ck("the crew suite has many checks, so the count is not one per suite", len(found["crew"]) > 50, len(found["crew"]))

fx = work / "fx"
fx.mkdir()
(fx / "ci_list.txt").write_text("a\n# a comment\nb\n")
(fx / "a_test.sh").write_text('#!/bin/bash\n# check "in a comment"\ncheck "first" \'true\'\nfor x in 1 2; do\n  check "in a loop $x" \'true\'\ndone\n'
                              'checkx "not a check"\necho check "not at line start"\ncheck "say \\"hi\\"" \'true\'\n')
(fx / "b_test.sh").write_text("#!/bin/bash\necho no checks here\n")


class Fixture:
    """parity looks at the fixture's suites for the length of the with block."""

    def __enter__(self):
        self.saved = (parity.HERE, parity.CI_LIST, dict(parity.SCENARIOS))
        parity.HERE, parity.CI_LIST = fx, fx / "ci_list.txt"
        return self

    def __exit__(self, *a):
        parity.HERE, parity.CI_LIST = self.saved[:2]
        parity.SCENARIOS.clear()
        parity.SCENARIOS.update(self.saved[2])


with Fixture():
    fixture_found = parity.suite_scenarios()
ck("a fixture suite counts every check line, indented ones too, and not comments, other words or echo lines",
   fixture_found.get("a") == ["first", "in a loop $x", 'say \\"hi\\"'], str(fixture_found))
ck("...and a fixture suite with no check line counts once", fixture_found.get("b") == ["(the whole b suite)"], str(fixture_found))

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
    "if mode == 'callv': subprocess.run([os.environ['CLAUDE_BIN'], '--version'], capture_output=True)\n"
    "if mode == 'calla': subprocess.run([os.environ['CLAUDE_BIN'], 'agents', '--json'], capture_output=True)\n"
    "sys.exit(1)\n")


def stubbed(bash_mode, python_mode):
    return {"PARITY_CMD_BASH": shlex.join([sys.executable, str(stub), bash_mode]),
            "PARITY_CMD_PYTHON": shlex.join([sys.executable, str(stub), python_mode])}


# --- what the engines write -----------------------------------------------------------------------
# Both sides print the same line and fail the same check-free way; only what they write differs.
code, text = run("crew_basic", env=stubbed("plain", "plain"))
ck("stubs that print and write the same match (exit 0)", code == 0 and "MATCH" in text, f"{code}\n{text}")
code, text = run("crew_basic", env=stubbed("file1", "file1"))
ck("...also when both write the same crew file", code == 0 and "MATCH" in text, f"{code}\n{text}")
code, text = run("crew_basic", env=stubbed("callv", "callv"))
ck("...and when both make the same Claude Code call", code == 0 and "MATCH" in text, f"{code}\n{text}")
code, text = run("crew_basic", env=stubbed("plain", "file1"))
ck("the same output but a crew file only Python writes: exit 1, the diff names the file",
   code == 1 and "output differs from bash" in text and "== file crew/x.json ==" in text, f"{code}\n{text}")
code, text = run("crew_basic", env=stubbed("file1", "plain"))
ck("...a crew file only bash writes: exit 1, the diff names the file",
   code == 1 and "output differs from bash" in text and "crew/x.json" in text, f"{code}\n{text}")
code, text = run("crew_basic", env=stubbed("file1", "file2"))
ck("...a crew file with other contents: exit 1, the diff shows both",
   code == 1 and "crew/x.json" in text and '-  "a": 1' in text and '+  "a": 2' in text, f"{code}\n{text}")
code, text = run("crew_basic", env=stubbed("file1", "file3"))
ck("...another file name: exit 1, both names in the diff", code == 1 and "crew/x.json" in text and "crew/y.txt" in text, f"{code}\n{text}")
code, text = run("crew_basic", env=stubbed("callv", "calla"))
ck("the same output but another call to Claude Code: exit 1, the diff names the call record",
   code == 1 and "== file claude/calls.jsonl ==" in text and "agents" in text and "--version" in text, f"{code}\n{text}")
code, text = run("crew_basic", env=stubbed("plain", "callv"))
ck("...a call only Python makes: exit 1", code == 1 and "claude/calls.jsonl" in text, f"{code}\n{text}")

# --- a skipped bash side --------------------------------------------------------------------------
saved_patch = (parity.bash_available, parity.is_windows)
parity.bash_available = lambda: "needs bash and jq on PATH"
with mock.patch.dict(os.environ, {"PARITY_CMD_PYTHON": stubbed("plain", "plain")["PARITY_CMD_PYTHON"]}):
    os.environ.pop("PARITY_CMD_BASH", None)
    lines = []
    parity.is_windows = lambda: False
    code, passes = parity.evaluate(parity.SCENARIOS["crew_basic"], out=lines.append)
    ck("off Windows a skipped bash side is an error: exit 1 and it says why", code == 1 and any(x.startswith("ERROR bash:") for x in lines), "\n".join(lines))
    ck("...and the scenario does not count as passing against Python", passes is False, passes)
    lines = []
    parity.is_windows = lambda: True
    code, passes = parity.evaluate(parity.SCENARIOS["crew_basic"], out=lines.append)
    ck("on Windows a skipped bash side is fine: the Python side stands alone", code == 0 and passes is True and not any("ERROR" in x for x in lines), "\n".join(lines))
parity.bash_available, parity.is_windows = saved_patch

# --- the coverage count and its error path --------------------------------------------------------
crew_basic = parity.SCENARIOS["crew_basic"]


def fx_scenario(name, label):
    return parity.Scenario(name=name, suite="a", steps=crew_basic.steps, checks=[("it ran", lambda r: True)], covers=[label], about="fixture")


# PARITY_JOBS=1: the fixture scenarios exist only in this process, a worker process would not have them
with Fixture(), mock.patch.dict(os.environ, {**stubbed("plain", "plain"), "PARITY_JOBS": "1"}):
    parity.SCENARIOS.clear()
    parity.SCENARIOS["fx_ok"] = fx_scenario("fx_ok", "first")
    lines = []
    rc = parity.coverage(False, out=lines.append)
    text = "\n".join(lines)
    ck("coverage counts one scenario of a three-check suite: a: 1/3, b: 0/1, 1 of 4 (25.0%)",
       rc == 0 and "a: 1/3" in text and "b: 0/1" in text and "coverage: 1 of 4 scenarios pass against Python (25.0%) in 2 suites" in text, text)
    ck("...and lists the covered check as passing", "PASS python  (fx_ok)" in text and "first" in text, text)
    parity.SCENARIOS.clear()
    parity.SCENARIOS["fx_bad"] = fx_scenario("fx_bad", "a label the suite does not have")
    lines = []
    rc = parity.coverage(False, out=lines.append)
    text = "\n".join(lines)
    ck("a covers label missing from its suite is an ERROR: exit 1, it names scenario, suite and label",
       rc == 1 and "ERROR scenario fx_bad covers a check that is not in a_test.sh: a label the suite does not have" in text, text)
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

why = parity.bash_available()
if why:
    print(f"  skip the runs that need the bash engine: {why}")
else:
    code, text = run("crew_basic")
    ck("crew_basic exits 0", code == 0, f"{code}\n{text}")
    ck("crew_basic: bash passes", "PASS bash: 1 checks" in text, text)
    ck("crew_basic: python is ported and matches bash", "PASS python: 1 checks" in text and "MATCH" in text, text)

    # A command the Python engine has not ported (a made-up word, so a later port cannot turn it ported): that side skips.
    unported = parity.Scenario(name="fx_unported", suite="a", steps=[parity.Step(["no-such-command"])],
                               checks=[("it ran", lambda r: True)], covers=["x"], about="fixture")
    lines = []
    with mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if not k.startswith(("PARITY_", "TARES_FACTORY_"))}):
        code, passes = parity.evaluate(unported, out=lines.append)
    ck("an unported command skips the Python side: SKIP python: not ported",
       any("SKIP python: not ported" in x for x in lines) and passes is not True, "\n".join(lines))

    code, text = run("crew_basic", env={"PARITY_FAIL": "bash"})
    ck("a failing bash side exits 1", code == 1 and "FAIL bash" in text, f"{code}\n{text}")

    # A Python side that is the bash engine itself prints the same, so the runs match.
    mirror = work / "mirror.py"
    mirror.write_text("import os, subprocess, sys\n"
                      "os.environ['TARES_FACTORY_ENGINE'] = 'bash'\n"
                      "sys.exit(subprocess.call([sys.executable, '-m', 'tares.crew'] + sys.argv[1:]))\n")
    same = f"{sys.executable} {mirror}"
    code, text = run("crew_basic", env={"PARITY_CMD_PYTHON": same})
    ck("a python side that prints the same as bash matches and exits 0",
       code == 0 and "PASS python: 1 checks" in text and "MATCH" in text, f"{code}\n{text}")

    # A Python side that prints something else: its checks pass, the output differs, so exit 1.
    stub = work / "stub.py"
    stub.write_text("import sys\n"
                    "print('no Tares answers at http://127.0.0.1:9. Start it with tares up. (stub)')\n"
                    "sys.exit(1)\n")
    code, text = run("crew_basic", env={"PARITY_CMD_PYTHON": f"{sys.executable} {stub}"})
    ck("a python side that differs from bash exits 1", code == 1 and "output differs from bash" in text, f"{code}\n{text}")

    # A Python side that fails a check exits 1 too.
    code, text = run("crew_basic", env={"PARITY_CMD_PYTHON": same, "PARITY_FAIL": "python"})
    ck("a failing python side exits 1", code == 1 and "FAIL python" in text, f"{code}\n{text}")

    # One real run, with --require-full: it prints what --coverage prints (the full run of every scenario is the slow part of
    # this test, so it is not done twice); that --coverage alone exits 0 is checked above, on the fixture suites.
    code, text = run("--coverage", "--require-full")
    ck("--require-full exits 1 below 100%", code == 1 and "--require-full" in text, f"{code}\n{text[-300:]}")
    last = [ln for ln in text.strip().splitlines() if ln.startswith("coverage: ")]
    ck("--coverage prints a percentage",
       bool(last) and "%" in last[-1] and "scenarios pass against Python" in last[-1], f"{code}\n{text[-400:]}")
    ck("--coverage lists the crew suite", "crew: " in text and "without Tares crew up stops plainly" in text, text[-400:])

# factory-bin: runs $FACTORY_BIN, and a call that lost the variable stops plainly (the 'tares' suite once ran
# `env -i ... "$F" spawn` without it and executed "spawn" itself).
FB = str(FACTORY / "factory-bin")
if os.name != "nt" and shutil.which("sh"):
    r = subprocess.run(["sh", FB, "spawn", "x"], env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True)
    ck("factory-bin without FACTORY_BIN stops with a plain message", r.returncode == 97 and "FACTORY_BIN is empty" in r.stderr
       and "not found" not in r.stderr, f"{r.returncode} {r.stderr}")
    r = subprocess.run(["sh", FB, "a b"], env={"PATH": "/usr/bin:/bin", "FACTORY_BIN": "echo hi"}, capture_output=True, text=True)
    ck("factory-bin runs a two-word FACTORY_BIN with the arguments", r.returncode == 0 and r.stdout.strip() == "hi a b", r.stdout)

shutil.rmtree(work, ignore_errors=True)
print(f"{P} passed, {F} failed")
sys.exit(1 if F else 0)
