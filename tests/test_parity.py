"""The parity harness (tests/factory/parity.py): the normaliser, the crew_basic scenario, the exit codes,
and the scenario count gate.

Checks: what the normaliser removes (temp folder, timestamps, pids, ids, key order) and what it keeps;
`parity.py crew_basic` exits 0 and prints `SKIP python: not ported` while the Python engine has not
moved that command over; a failing bash side exits 1; a Python side that prints the same as bash
matches, and one that prints something else exits 1; `--coverage` prints a percentage and exits 0;
`--require-full` exits 1 below 100%; an unknown scenario exits 2.

The runs that need the bash engine are skipped where bash and jq are not there (Windows).

Run: .venv/bin/python tests/test_parity.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

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
                       errors="replace", env=e, cwd=str(ROOT), timeout=600)
    return p.returncode, p.stdout + p.stderr


# --- the normaliser -------------------------------------------------------------------------------
tmp = Path(tempfile.gettempdir()) / "parity-x-1"
n = parity.norm_text(f"in {tmp}/crew at 2026-11-07T10:11:12Z and 2026-11-07 10:11:12.123+01:00", tmp)
ck("the temp folder and timestamps are replaced", "<TMP>/crew" in n and n.count("<TS>") == 2 and str(tmp) not in n, n)
ck("an epoch time is replaced", parity.norm_text("at 1762510272 and 1762510272123") == "at <TS> and <TS>")
ck("a pid in text is replaced", parity.norm_text("another factory command (pid 4242) is starting")
   == "another factory command (pid <PID>) is starting")
ck("a session id and a job id are replaced",
   parity.norm_text("backgrounded · a1b2c3d4 · x and session-7") == "backgrounded · <ID> · x and <ID>")
ck("a uuid is replaced", parity.norm_text("id 123e4567-e89b-12d3-a456-426614174000") == "id <ID>")
ck("words and small numbers are kept", parity.norm_text("started 3 stations, port 8788") == "started 3 stations, port 8788")
ck("CRLF becomes LF", parity.norm_text("a\r\nb") == "a\nb")

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

# --- the suites and the count ---------------------------------------------------------------------
found = parity.suite_scenarios()
ck("every suite of tests/factory/ci_list.txt is counted", set(found) == set(parity.suites()) and len(found) > 0)
ck("the crew suite lists its checks", "without Tares crew up stops plainly" in found["crew"])
ck("a suite with no check line counts as one scenario", found.get("parse") == ["(the whole parse suite)"], found.get("parse"))
ck("each scenario covers checks that exist", all(label in found[sc.suite] for sc in parity.SCENARIOS.values() for label in sc.covers))

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
    ck("crew_basic: python is skipped as not ported", "SKIP python: not ported" in text, text)

    code, text = run("crew_basic", env={"PARITY_FAIL": "bash"})
    ck("a failing bash side exits 1", code == 1 and "FAIL bash" in text, f"{code}\n{text}")

    # A Python side that is the bash engine itself prints the same, so the runs match.
    work = Path(tempfile.mkdtemp(prefix="test-parity-"))
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

    code, text = run("--coverage")
    last = text.strip().splitlines()[-1] if text.strip() else ""
    ck("--coverage prints a percentage and exits 0",
       code == 0 and last.startswith("coverage: ") and "%" in last and "scenarios pass against Python" in last,
       f"{code}\n{text[-400:]}")
    ck("--coverage lists the crew suite", "crew: " in text and "without Tares crew up stops plainly" in text, text[-400:])
    code, text = run("--coverage", "--require-full")
    ck("--require-full exits 1 below 100%", code == 1 and "--require-full" in text, f"{code}\n{text[-300:]}")

if not why:
    shutil.rmtree(work, ignore_errors=True)
print(f"{P} passed, {F} failed")
sys.exit(1 if F else 0)
