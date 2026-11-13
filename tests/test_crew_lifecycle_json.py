"""`--json` on the lifecycle commands (TR-1283): one result document on stdout, the error document on failure.

Each case runs `python -m tares.crew ...` as a subprocess against the fake Claude Code in a throwaway crew home
(tests/_crew_fake.py) and a small fake Tares (tests/factory/fake_up_tares.py), so file descriptor 1 is the real
one. A case checks that stdout is exactly one JSON line with schema_version first, that it validates against
schemas/result.json or schemas/error.json, and the exit code. Without --json the text and the exit codes are
what they were.

Run: python tests/test_crew_lifecycle_json.py
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
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "factory"))

import _crew_guard  # noqa: E402  (read before HOME changes: it remembers the real home)

_crew_guard.scratch_home()
from _crew_fake import fake_crew_env  # noqa: E402
from fake_up_tares import FakeTares  # noqa: E402

try:
    from jsonschema import Draft202012Validator  # noqa: E402
except ImportError:
    print("FAIL  jsonschema is not installed (a test-only dependency: pip install jsonschema)")
    print("\n0 passed, 1 failed")
    sys.exit(1)

SCHEMAS = ROOT / "tares" / "crew" / "schemas"
RESULT = Draft202012Validator(json.loads((SCHEMAS / "result.json").read_text(encoding="utf-8")))
ERROR = Draft202012Validator(json.loads((SCHEMAS / "error.json").read_text(encoding="utf-8")))
P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail!s}"[:700]))


TMP = Path(tempfile.mkdtemp(prefix="tares-lifecycle-json-"))
BASE = fake_crew_env(TMP)
CREW = TMP / "crew"
WORK = TMP / "work"
WORK.mkdir()
FAKE = FakeTares(TMP / "tares", settings={"autonomy": "L4-ship", "releaser": False, "release_profile": "kind",
                                           "prod_pattern": "(^|[-_.])(prod|production)([-_.]|$)",
                                           "challenger": False, "builders_max": 2}).start()


def crew(args, **extra):
    env = dict(BASE, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", TARESD_URL=FAKE.url,
               TARES_FACTORY_MCP_URL=FAKE.mcp_url,
               TARES_PLUGIN_DIR=str(ROOT / "claude-plugin"))
    env.update(extra)
    env.pop("PYTHONSTARTUP", None)
    p = subprocess.run([sys.executable, "-m", "tares.crew", *args], env=env, cwd=str(ROOT), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=180)
    return p.returncode, p.stdout.replace("\r\n", "\n"), p.stderr.replace("\r\n", "\n")


def doc_of(label, code, out, err, want_code, kind):
    """One line, JSON, schema_version first, valid against result.json or error.json, the right exit code."""
    lines = out.splitlines()
    ck(f"{label}: exit {want_code}", code == want_code, f"{code} {err[-300:]}")
    ck(f"{label}: stdout is one line", len(lines) == 1 and out.endswith("\n"), repr(out[:300]))
    try:
        doc = json.loads(lines[0])
    except (ValueError, IndexError):
        ck(f"{label}: stdout is JSON", False, repr(out[:300]))
        return {}
    ck(f"{label}: schema_version is the first key", next(iter(doc), None) == "schema_version", list(doc))
    v = RESULT if kind == "result" else ERROR
    bad = [e.message for e in v.iter_errors(doc)]
    ck(f"{label}: validates against {kind}.json", not bad, bad)
    return doc


def stations_write(rows):
    (CREW / "stations.json").write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")


def stations_read():
    return json.loads((CREW / "stations.json").read_text(encoding="utf-8"))


def row(**over):
    r = {"role": "builder", "status": "active", "parent": "crew-orchestrator", "project": "shop", "repo": "",
         "cwd": str(WORK), "model": "opus", "effort": "high", "id": "aaaa0001", "sessionId": "sess-1"}
    r.update(over)
    return r


def reset():
    shutil.rmtree(TMP / "claude", ignore_errors=True)
    (TMP / "claude").mkdir()
    (TMP / "claude" / "agents.json").write_text("[]\n", encoding="utf-8")
    (CREW / "HALT").unlink(missing_ok=True)
    stations_write({})


def halt_reason():
    return json.loads((CREW / "HALT").read_text(encoding="utf-8"))["reason"]


# ---------- 1. halt ----------
print("== halt")
reset()
code, out, err = crew(["crew", "halt", "--json"])
d = doc_of("halt --json", code, out, err, 0, "result")
ck("halt --json: ok, command, the halted note", d.get("ok") is True and d.get("command") == "crew halt"
   and any(n.startswith("crew halted:") for n in d.get("notes", [])), d)
ck("halt --json: no 'tares factory: ' prefix on a note", not any(n.startswith("tares factory:") for n in d.get("notes", [])), d)
ck("halt --json: default reason", halt_reason() == "halted by the human", halt_reason())
ck("halt --json: stderr is empty", err == "", err)
for args in (["crew", "halt", "--json", "why"], ["crew", "halt", "why", "--json"]):
    (CREW / "HALT").unlink(missing_ok=True)
    code, out, err = crew(args)
    doc_of(" ".join(args), code, out, err, 0, "result")
    ck(" ".join(args) + ": reason is why", halt_reason() == "why", halt_reason())

# ---------- 2. resume, down ----------
print("== resume and down")
code, out, err = crew(["crew", "resume", "--json"])
d = doc_of("resume --json", code, out, err, 0, "result")
ck("resume --json: HALT is gone, notes say so", not (CREW / "HALT").exists() and d.get("notes") == ["crew resumed"], d)
code, out, err = crew(["crew", "down", "--json"])
d = doc_of("down --json", code, out, err, 0, "result")
ck("down --json: command and notes", d.get("command") == "crew down" and any("all stations stopped" in n for n in d.get("notes", [])), d)

# ---------- 3. retire ----------
print("== retire")
reset()
stations_write({"crew-builder": row()})
code, out, err = crew(["retire", "--json", "crew-builder"])
d = doc_of("retire --json <station>", code, out, err, 0, "result")
ck("retire --json: marked retired", stations_read()["crew-builder"]["status"] == "retired", stations_read())
ck("retire --json: command and note", d.get("command") == "retire" and "retired crew-builder" in d.get("notes", []), d)
code, out, err = crew(["retire", "--json"])
d = doc_of("retire --json, no station", code, out, err, 1, "error")
ck("retire --json, no station: the usage text is the message", "usage" in d.get("error", {}).get("message", ""), d)
code, out, err = crew(["retire", "nobody", "--json"])
d = doc_of("retire --json, unknown station", code, out, err, 1, "error")
ck("retire --json, unknown station: code error", d.get("error", {}).get("code") == "error", d)
ck("...and no notes key when nothing was printed", "notes" not in d, d)

# ---------- 4. revive ----------
print("== revive")
reset()
stations_write({"crew-builder": row(status="stopped")})
code, out, err = crew(["revive", "--json", "crew-builder"])
d = doc_of("revive --json, stopped station", code, out, err, 0, "result")
ck("revive --json: command is revive", d.get("command") == "revive", d)
ck("revive --json: a note says it started", any("start" in n.lower() or "revive" in n.lower() for n in d.get("notes", [])), d)
code, out, err = crew(["revive", "--json", "crew-builder"])
d = doc_of("revive --json, running station", code, out, err, 1, "error")
ck("revive --json, running station: says it runs", "running" in d.get("error", {}).get("message", ""), d)

# ---------- 5. freeze, unfreeze ----------
print("== freeze and unfreeze")
reset()
code, out, err = crew(["crew", "freeze", "--json"])
d = doc_of("freeze --json", code, out, err, 0, "result")
ck("freeze --json: command and a note", d.get("command") == "crew freeze" and bool(d.get("notes")), d)
code, out, err = crew(["crew", "unfreeze", "--json"])
d = doc_of("unfreeze --json", code, out, err, 0, "result")
ck("unfreeze --json: command", d.get("command") == "crew unfreeze", d)
code, out, err = crew(["crew", "freeze", "--json"], TARESD_URL="http://127.0.0.1:9")
doc_of("freeze --json, Tares refuses", code, out, err, 1, "error")
code, out, err = crew(["crew", "unfreeze"], TARESD_URL="http://127.0.0.1:9")
ck("unfreeze without --json against no Tares: text on stderr, nothing on stdout, exit 1",
   code == 1 and out == "" and err.startswith("tares factory: "), (code, out, err[-200:]))

# ---------- 6. update, restart ----------
print("== update and restart")
reset()
(TMP / "claude" / "version").write_text("2.0.0\n", encoding="utf-8")
code, out, err = crew(["crew", "update", "--json", "--wait", "0"])
d = doc_of("update --json --wait 0", code, out, err, 0, "result")
ck("update --json: command", d.get("command") == "crew update", d)
code, out, err = crew(["crew", "restart", "--json", "--wait", "0"])
d = doc_of("crew restart --json", code, out, err, 0, "result")
ck("crew restart --json: command", d.get("command") == "crew restart", d)
code, out, err = crew(["crew", "update", "--json", "--bogus"])
doc_of("update --json --bogus", code, out, err, 1, "error")

# the descriptor guard: a child that writes to the inherited stdout cannot land in the document
print("== the descriptor guard")
probe = (
    "import subprocess, sys\n"
    "from tares.crew import jsonrun\n"
    "def body(a):\n"
    "    subprocess.run([sys.executable, '-c', 'print(\"child out\")'], check=False)\n"
    "    print('tares factory: a note')\n"
    "    return 0\n"
    "sys.exit(jsonrun.run(sys.argv[1:], 'probe', body, text_fail=lambda e: None))\n")
p = subprocess.run([sys.executable, "-c", probe, "--json"], capture_output=True, text=True, cwd=str(ROOT),
                   env=dict(BASE, PYTHONPATH=str(ROOT)), timeout=60)
d = doc_of("a child writing to stdout", p.returncode, p.stdout.replace("\r\n", "\n"), p.stderr, 0, "result")
ck("...its text went to stderr and the note is kept", "child out" in p.stderr and d.get("notes") == ["a note"], (p.stderr, d))

# failing after printing: the error document keeps the notes collected so far (TR-1309)
print("== notes on a failure")
probe2 = """import sys
from tares.crew import jsonrun
from tares.crew.errors import CrewError
def body(a):
    print('tares factory: stopped crew-builder', file=sys.stderr)
    print('a line on stdout')
    if 'exit3' in a:
        return 3
    raise CrewError('could not stop crew-reviewer')
sys.exit(jsonrun.run(sys.argv[1:], 'probe', body, text_fail=lambda e: None))
"""
NOTES = ["stopped crew-builder", "a line on stdout"]
p = subprocess.run([sys.executable, "-c", probe2, "--json"], capture_output=True, text=True, cwd=str(ROOT),
                   env=dict(BASE, PYTHONPATH=str(ROOT)), timeout=60)
d = doc_of("a failure after printing", p.returncode, p.stdout.replace("\r\n", "\n"), p.stderr, 1, "error")
ck("...the error document carries the notes, in the order schema_version, error, notes",
   d == {"schema_version": 1, "error": {"code": "error", "message": "could not stop crew-reviewer"}, "notes": NOTES}
   and list(d) == ["schema_version", "error", "notes"], d)
ck("...and nothing reached stderr", p.stderr == "", p.stderr)
p = subprocess.run([sys.executable, "-c", probe2, "--json", "exit3"], capture_output=True, text=True, cwd=str(ROOT),
                   env=dict(BASE, PYTHONPATH=str(ROOT)), timeout=60)
d = doc_of("a body that returns exit 3", p.returncode, p.stdout.replace("\r\n", "\n"), p.stderr, 1, "error")
ck("...error.code error, message probe failed (exit 3), and the notes",
   d.get("error") == {"code": "error", "message": "probe failed (exit 3)"} and d.get("notes") == NOTES, d)

# ---------- 7. up ----------
print("== up")
reset()
FAKE.settings.clear()
before = len(FAKE.written())
code, out, err = crew(["crew", "up", "--json"])
d = doc_of("up --json without --yes", code, out, err, 2, "error")
ck("up --json without --yes: code usage", d.get("error", {}).get("code") == "usage", d)
ck("...and nothing was written to Tares", len(FAKE.written()) == before, FAKE.written()[before:])
code, out, err = crew(["crew", "up", "--json", "--yes"])
d = doc_of("up --json --yes, settings missing", code, out, err, 1, "error")
ck("...code setup_needed, names a question and the setup command", d.get("error", {}).get("code") == "setup_needed"
   and "releaser" in d["error"]["message"] and "crew setup" in d["error"]["message"], d)
ck("...and no PUT reached Tares", len(FAKE.written()) == before, FAKE.written()[before:])
FAKE.settings.update({"autonomy": "L4-ship", "releaser": False, "release_profile": "kind",
                      "prod_pattern": "(^|[-_.])(prod|production)([-_.]|$)", "challenger": False, "builders_max": 2})
(TMP / "home" / ".claude.json").write_text(json.dumps({"projects": {str(CREW): {"hasTrustDialogAccepted": True}}}), encoding="utf-8")
FAKE.grants.append('- "git push" (2026-01-01)')
code, out, err = crew(["crew", "up", "--yes", "--json", "--no-watch"], FACTORY_NO_WATCH="1")
d = doc_of("up --yes --json, settings complete", code, out, err, 0, "result")
ck("up --json: command is crew up", d.get("command") == "crew up", d)

# ---------- 8. without --json nothing changed ----------
print("== without --json")
reset()
code, out, err = crew(["crew", "halt"])
ck("halt: exit 0, stdout empty, the same two lines on stderr", code == 0 and out == "" and err.splitlines()[:1] == [
    "tares factory: crew halted: stations finish their current step and start nothing risky (merges, pushes, deploys, "
    "tags, new sessions). tares factory crew resume lifts it."] and len(err.splitlines()) == 2, (code, out, err))
code, out, err = crew(["retire", "nobody"])
ck("failing retire: exit 1, stdout empty, the text line on stderr", code == 1 and out == "" and err.strip() == "tares factory: nobody is not a recorded station",
   (code, out, err))

FAKE.stop()
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
