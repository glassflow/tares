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


js = jobs(ci)
ck("ci.yml has the jobs console, test and secrets", {"console", "test", "secrets"} <= set(js), str(sorted(js)))
test = js.get("test", "")
RUN = "python tests/ci_run.py --suite ${{ matrix.suite }} --shard ${{ matrix.shard }} --of 5"
ck("job test: runs tests/ci_run.py per suite and shard", RUN in test and re.search(r"suite: \[duckdb, postgres\]", test) and re.search(r"shard: \[1, 2, 3, 4, 5\]", test), test[:400])
step = test[test.rfind("run: |", 0, max(test.find(RUN), 0)):test.find(RUN)]
ck("job test: tests/check_ci_list.sh checks both lists in the step before ci_run.py (a missing or empty list fails the job)",
   all(re.search(r"(?m)^\s*bash tests/check_ci_list\.sh " + re.escape(f) + r"\s*$", step) for f in ("tests/ci_list.txt", "tests/factory/ci_list.txt")), step)
ck("tests/check_ci_list.sh exists and tests/test_ci_check.py is in the list", (ROOT / "tests" / "check_ci_list.sh").is_file() and "test_ci_check" in py)
ck("no job but test names a list", all("ci_list" not in j and "ci_run" not in j for n, j in js.items() if n != "test"))

# tests/ci_run.py reads the two lists, and its shards together run each suite exactly once.
import sys
sys.path.insert(0, str(ROOT / "tests"))
import ci_run
want = {"duckdb": py + [f"factory:{n}" for n in sh],
        "postgres": [n for n in py if n not in ("test_state_postgres", "test_duckdb_memory")] + [f"factory:{n}" for n in sh]}
for suite, w in want.items():
    got = [t for i in range(1, 6) for t in ci_run.shard(ci_run.items(suite), i, 5)]
    ck(f"ci_run.py, suite {suite}: the 5 shards run every listed test once", sorted(got) == sorted(w) and len(got) == len(set(got)),
       f"missing {sorted(set(w) - set(got))}, extra {sorted(set(got) - set(w))}")

print(f"\n{P} passed, {F} failed")
raise SystemExit(1 if F else 0)
