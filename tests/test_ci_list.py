"""The CI test lists: every name has its file, none is listed twice, and ci.yml reads the lists."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


def names(path):
    return [ln.strip() for ln in path.read_text().splitlines() if ln.strip() and not ln.strip().startswith("#")]


def check(label, listfile, filename):
    got = names(listfile)
    ck(f"{label}: the list is not empty", len(got) > 0)
    missing = [n for n in got if not (listfile.parent / filename(n)).is_file()]
    ck(f"{label}: every name has its file", not missing, ", ".join(missing))
    dupes = sorted({n for n in got if got.count(n) > 1})
    ck(f"{label}: no name is listed twice", not dupes, ", ".join(dupes))
    return got


py = check("python", ROOT / "tests" / "ci_list.txt", lambda n: f"{n}.py")
sh = check("bash", ROOT / "tests" / "factory" / "ci_list.txt", lambda n: f"{n}_test.sh")
ck("python list holds this test", "test_ci_list" in py)

ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
ck("ci.yml has no inline python test list", "for t in test_" not in ci)
ck("ci.yml reads the python list", ci.count("tests/ci_list.txt") == 2, str(ci.count("tests/ci_list.txt")))
ck("ci.yml reads the bash list", ci.count("tests/factory/ci_list.txt") == 2, str(ci.count("tests/factory/ci_list.txt")))
skips = re.findall(r"tests/ci_list\.txt \| grep -vxE '([^']*)'\)", ci)
ck("the Postgres run skips exactly test_state_postgres and test_duckdb_memory", skips == ["test_state_postgres|test_duckdb_memory"], str(skips))
ck("every other loop reads its list unfiltered", len(re.findall(r"\$\(grep -vE '\^\\s\*\(#\|\$\)' tests/(factory/)?ci_list\.txt\)", ci)) == 3)

print(f"\n{P} passed, {F} failed")
raise SystemExit(1 if F else 0)
