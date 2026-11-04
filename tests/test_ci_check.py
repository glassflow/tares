"""tests/check_ci_list.sh: a CI test list that is missing, renamed or empty fails with a clear message."""
import shutil, subprocess, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tests" / "check_ci_list.sh"

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


def run(workdir, listname="tests/ci_list.txt"):
    r = subprocess.run(["bash", str(SCRIPT), listname], cwd=workdir, capture_output=True, text=True)
    return r.returncode, r.stderr + r.stdout


MADE = []


def fresh(content="# header\ntest_a\ntest_b\n"):
    d = Path(tempfile.mkdtemp(prefix="ci_check_"))
    MADE.append(d)
    (d / "tests").mkdir()
    if content is not None:
        (d / "tests" / "ci_list.txt").write_text(content)
    return d


d = fresh()
rc, out = run(d)
ck("a list with test names passes", rc == 0 and out == "", f"{rc} {out!r}")

d = fresh(None)
rc, out = run(d)
ck("a missing list fails and names the file", rc != 0 and "tests/ci_list.txt is missing" in out, f"{rc} {out!r}")

d = fresh()
(d / "tests" / "ci_list.txt").rename(d / "tests" / "ci_tests.txt")
rc, out = run(d)
ck("a renamed list fails and names the file", rc != 0 and "tests/ci_list.txt is missing" in out, f"{rc} {out!r}")

d = fresh("")
rc, out = run(d)
ck("an empty list fails with a clear message", rc != 0 and "has no test names" in out, f"{rc} {out!r}")

d = fresh("# only a comment\n\n   \n# another\n")
rc, out = run(d)
ck("a list with only comments and blank lines fails", rc != 0 and "has no test names" in out, f"{rc} {out!r}")

d = fresh()
rc, out = run(d, "tests/factory/ci_list.txt")
ck("the factory list is checked by its own path", rc != 0 and "tests/factory/ci_list.txt is missing" in out, f"{rc} {out!r}")

# The two real lists pass.
rc1, o1 = run(ROOT, "tests/ci_list.txt")
rc2, o2 = run(ROOT, "tests/factory/ci_list.txt")
ck("the real lists pass", rc1 == 0 and rc2 == 0, o1 + o2)

for p in MADE:
    shutil.rmtree(p, ignore_errors=True)
print(f"\n{P} passed, {F} failed")
raise SystemExit(1 if F else 0)
