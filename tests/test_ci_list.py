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

# Every tests/test_*.py runs in CI or is skipped on purpose: it is in ci_list.txt, or in ci_skip.txt with a reason.
skips = {}
for ln in names(ROOT / "tests" / "ci_skip.txt"):
    name, _, why = ln.partition(":")
    skips[name.strip()] = why.strip()
ck("skip list: every line is `name: reason`", all(skips.values()) and len(skips) == len(names(ROOT / "tests" / "ci_skip.txt")), str(skips))
ck("skip list: every name has its file", all((ROOT / "tests" / f"{n}.py").is_file() for n in skips), ", ".join(n for n in skips if not (ROOT / "tests" / f"{n}.py").is_file()))
ck("skip list: no test is also in the CI list", not (set(skips) & set(py)), ", ".join(sorted(set(skips) & set(py))))
unlisted = sorted(f.stem for f in (ROOT / "tests").glob("test_*.py") if f.stem not in py and f.stem not in skips)
ck("every tests/test_*.py is in tests/ci_list.txt or tests/ci_skip.txt", not unlisted, "in neither list: " + ", ".join(unlisted))

ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
ci = re.sub(r"(?m)^\s*#.*$", "", ci)  # a commented-out step is not a step
ck("ci.yml has no inline python test list", "for t in test_" not in ci)


def jobs(text):
    """The text of each job under `jobs:`, by name (a job header is a two-space key)."""
    body = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"(?m)^  ([A-Za-z0-9_-]+):\s*$", body)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


def loops(job):
    """The loops of a job that read a list: (file, filter, body) in order; body is what runs per name."""
    out = []
    for m in re.finditer(r"for t in \$\(grep -vE '\^\\s\*\(#\|\$\)' (tests/(?:factory/)?ci_list\.txt)((?: \| [^)]*)?)\); do\n(.*?)\n\s*done", job, re.S):
        body = [ln.strip() for ln in m.group(3).splitlines() if ln.strip() and not ln.strip().startswith("echo ")]
        out.append((m.group(1), m.group(2).strip(), body))
    return out


PY, SH = "tests/ci_list.txt", "tests/factory/ci_list.txt"
RUN_PY, RUN_SH = ['python "tests/$t.py"'], ['bash "tests/factory/${t}_test.sh"']
SKIP = "| grep -vxE 'test_state_postgres|test_duckdb_memory'"
js = jobs(ci)
ck("ci.yml has the jobs test, backend-postgres and secrets", {"test", "backend-postgres", "secrets"} <= set(js), str(sorted(js)))
ck("job test: runs the python list, then the bash list, both unfiltered, each running its test", loops(js.get("test", "")) == [(PY, "", RUN_PY), (SH, "", RUN_SH)], str(loops(js.get("test", ""))))
ck("job backend-postgres: runs the python list without test_state_postgres and test_duckdb_memory, then the bash list unfiltered, each running its test", loops(js.get("backend-postgres", "")) == [(PY, SKIP, RUN_PY), (SH, "", RUN_SH)], str(loops(js.get("backend-postgres", ""))))
ck("job secrets: runs no test list", loops(js.get("secrets", "")) == [] and "ci_list" not in js.get("secrets", ""))
ck("no other job reads a list", all(loops(j) == [] for n, j in js.items() if n not in ("test", "backend-postgres")))
ck("every list in ci.yml is read by a loop (none is read some other way)", ci.count("ci_list.txt") == sum(len(loops(j)) for j in js.values()), str(ci.count("ci_list.txt")))

print(f"\n{P} passed, {F} failed")
raise SystemExit(1 if F else 0)
