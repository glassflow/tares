"""scripts/check_changelog.py (TR-1249): a pull request that changes tares/ or ui/src/ needs a changelog fragment or the `no changelog` label."""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _env import clean_env

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import check_changelog as cc

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


def verdict(diff, labels=""):
    """check() on `git diff --name-status` text, the way main() reads it."""
    changed, added = cc.parse(diff.splitlines())
    return cc.check(changed, labels.split(","), added)


def cli(diff, labels=""):
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_changelog.py"), "--labels", labels],
                       input=diff, capture_output=True, text=True, env=clean_env())
    return r.returncode, r.stderr + r.stdout


PY = "M\ttares/daemon.py\n"
FRAG = "A\tchangelog.d/TR-1.md\n"
msg = verdict(PY)
ck("a tares/ change with no fragment fails", msg is not None)
ck("the message says what to add: changelog.d/<ticket>.md", msg is not None and "changelog.d/<ticket>.md" in msg and "no changelog" in msg, str(msg))
ck("the message has no em dash", msg is not None and chr(0x2014) not in msg)
ck("the same change with an added fragment passes", verdict(PY + FRAG) is None)
ck("a modified fragment passes", verdict(PY + "M\tchangelog.d/TR-1.md\n") is None)
ck("a renamed-in fragment passes", verdict(PY + "R100\tdocs/x.md\tchangelog.d/TR-1.md\n") is None)
ck("a ui/src change with no fragment fails", verdict("M\tui/src/App.tsx\n") is not None)
ck("a ui/src change with the `no changelog` label passes", verdict("M\tui/src/App.tsx\n", "bug,no changelog") is None)
ck("the label is matched exactly, not as a part", verdict(PY, "no changelog please") is not None)
ck("a docs-only change passes", verdict("M\tdocs/guide.md\nM\tREADME.md\n") is None)
ck("a change to tests/ and scripts/ alone passes", verdict("A\ttests/test_x.py\nM\tscripts/release.sh\n") is None)
ck("a path that only looks like tares/ passes", verdict("M\ttares_other/x.py\nM\tui/srcx/y.ts\n") is None)
ck("a deleted fragment alone does not count", verdict(PY + "D\tchangelog.d/TR-1.md\n") is not None)
ck("README.md in changelog.d does not count", verdict(PY + "A\tchangelog.d/README.md\nM\tchangelog.d/README.md\n") is not None)
ck("a non-markdown file or a nested file in changelog.d does not count", verdict(PY + "A\tchangelog.d/x.txt\nA\tchangelog.d/sub/y.md\n") is not None)
ck("check() on a plain path list counts a fragment among them", cc.check(["tares/a.py", "changelog.d/x.md"], []) is None
   and cc.check(["tares/a.py", "changelog.d/README.md"], []) is not None)
ck("a blank line and an unexpected line in the diff are skipped", verdict("\n" + PY + "garbage\n" + FRAG) is None)

code, out = cli(PY)
ck("CLI: exits 1 for a tares/ change with no fragment, the message on stderr", code == 1 and "changelog.d/<ticket>.md" in out, f"{code} {out}")
code, out = cli(PY + FRAG)
ck("CLI: exits 0 with a fragment", code == 0, f"{code} {out}")
code, out = cli("M\tui/src/a.tsx\n", "x,no changelog")
ck("CLI: exits 0 with the label", code == 0, f"{code} {out}")
code, out = cli(PY + "D\tchangelog.d/TR-1.md\n")
ck("CLI: a deleted fragment still exits 1", code == 1, f"{code} {out}")
code, out = cli("")
ck("CLI: an empty diff exits 0", code == 0, f"{code} {out}")

print(f"\n{P} passed, {F} failed")
raise SystemExit(1 if F else 0)
