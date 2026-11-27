"""The by-hand Codex runner picks what it is asked to, and never passes a run of nothing (tests/test_codex_real.py,
TR-1600). Offline: every probe is a fake process, so nothing here needs a Codex login or calls a model.

 a. no name runs every probe, in order; names run only those, in order, once each;
 b. an unknown probe, alone or with known ones, an unknown option, and an --out that is missing, empty or a
    file each end with argparse's message and exit code 2, before any probe starts;
 c. a probe that fails makes the run fail, and the others still run; without TARES_PROBE_CODEX nothing runs.

Run: python tests/test_codex_real_runner.py
"""
import contextlib
import io
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import test_codex_real as real  # noqa: E402

P = F = 0
ALL = [n for n, _ in real.ORDER]
TMP = Path(tempfile.mkdtemp(prefix="tcrr-"))
ENV = {"TARES_PROBE_CODEX": "/nowhere/codex", "PATH": "/usr/bin"}


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def go(argv, fail=(), env=ENV):
    """The runner with a fake process for each probe: (exit code, the probes started, what it printed)."""
    ran = []

    def run(cmd, **kw):
        name = Path(cmd[1]).stem
        ran.append(name)
        return subprocess.CompletedProcess(cmd, 1 if name in fail else 0, f"{name} out\n", "")

    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            code = real.main(argv, run=run, environ=env)
        except SystemExit as e:
            code = e.code
    return code, ran, out.getvalue()


print("a. what runs")
d = TMP / "all"
code, ran, said = go(["--out", str(d)])
ck("no name runs every probe, in order, and passes", code == 0 and ran == ALL and "10 passed, 0 failed" in said
   and (d / "probe_review.log").read_text() == "probe_review out\n", (code, ran, said))
code, ran, said = go(["probe_review", "--out", str(TMP / "two"), "probe_launch", "probe_review"])
ck("names run only those, in order, once each", code == 0 and ran == ["probe_launch", "probe_review"]
   and "2 passed, 0 failed" in said, (code, ran, said))

print("b. a selection that is not right runs nothing")
for label, argv in [("an unknown probe", ["probe_nope"]),
                    ("an unknown probe with known ones", ["probe_launch", "probe_nope", "probe_review"]),
                    ("an unknown option", ["--fast", "probe_launch"]),
                    ("an abbreviated option", ["--ou", str(TMP / "x")]),
                    ("--out without a folder", ["probe_launch", "--out"]),
                    ("an empty --out", ["--out=", "probe_launch"])]:
    code, ran, said = go(argv)
    ck(f"{label}: exit 2, a message, no probe started", code == 2 and ran == [] and "error:" in said, (code, ran, said))
afile = TMP / "a-file"
afile.write_text("x")
code, ran, said = go(["--out", str(afile)])
ck("an --out that is a file: exit 2, no probe started", code == 2 and ran == [] and "is not a folder" in said,
   (code, ran, said))
code, ran, said = go(["probe_nope"])
ck("the message names the unknown probe and the probes there are", "probe_nope" in said and "probe_evidence" in said,
   said)

print("c. failures")
code, ran, said = go(["--out", str(TMP / "f")], fail={"probe_station"})
ck("a probe that fails fails the run, and the others still run", code == 1 and ran == ALL
   and "FAIL probe_station" in said and "9 passed, 1 failed" in said, (code, ran, said))
code, ran, said = go(["--out", str(TMP / "n")], env={"PATH": "/usr/bin"})
ck("without TARES_PROBE_CODEX nothing runs, and it fails",
   code == 1 and ran == [] and "needs a signed-in Codex" in said, (code, ran, said))

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
