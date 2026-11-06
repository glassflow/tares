"""The CI test lists (Python, bash and the console's browser e2e suites): every name has its file, none is listed twice, they are sorted, and ci.yml reads the lists."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


def entries(path):
    """(line number in the file, name) for each name: blank lines and # comments are skipped but still counted."""
    return [(n, ln.strip()) for n, ln in enumerate(path.read_text().splitlines(), 1)
            if ln.strip() and not ln.strip().startswith("#")]


def names(path):
    return [name for _, name in entries(path)]


def out_of_order(got, where="the list"):
    """A plain message naming the first name that sorts before the one above it, with its line number in the
    file, or "" when sorted. `got` is entries(). New tests go in their sorted place, so two PRs rarely touch
    the same lines of the list. The order is byte order, which is what `LC_ALL=C sort` gives."""
    for i in range(1, len(got)):
        if got[i][1] < got[i - 1][1]:
            return (f"line {got[i][0]} of {where}, {got[i][1]!r}, belongs before {got[i - 1][1]!r} "
                    f"(line {got[i - 1][0]}): put each name in its sorted place, in byte order "
                    "(`LC_ALL=C sort`)")
    return ""


def check(label, listfile, filename):
    got = names(listfile)
    ck(f"{label}: the list is not empty", len(got) > 0)
    missing = [n for n in got if not (listfile.parent / filename(n)).is_file()]
    ck(f"{label}: every name has its file", not missing, ", ".join(missing))
    dupes = sorted({n for n in got if got.count(n) > 1})
    ck(f"{label}: no name is listed twice", not dupes, ", ".join(dupes))
    where = str(listfile.relative_to(ROOT))
    msg = out_of_order(entries(listfile), where)
    ck(f"{label}: the list is sorted", not msg, msg)
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
ck("ci.yml has the jobs console, test, test-ee, wheel, e2e and secrets",
   {"console", "test", "test-ee", "wheel", "e2e", "secrets"} <= set(js), str(sorted(js)))
test = js.get("test", "")
RUN = "python tests/ci_run.py --suite ${{ matrix.suite }} --shard ${{ matrix.shard }} --of 5"
ck("job test: runs tests/ci_run.py per suite and shard", RUN in test and re.search(r"suite: \[duckdb, postgres, mit\]", test) and re.search(r"shard: \[1, 2, 3, 4, 5\]", test), test[:400])
step = test[test.rfind("run: |", 0, max(test.find(RUN), 0)):test.find(RUN)]
ck("job test: tests/check_ci_list.sh checks both lists in the step before ci_run.py (a missing or empty list fails the job)",
   all(re.search(r"(?m)^\s*bash tests/check_ci_list\.sh " + re.escape(f) + r"\s*$", step) for f in ("tests/ci_list.txt", "tests/factory/ci_list.txt")), step)
ck("tests/check_ci_list.sh exists and tests/test_ci_check.py is in the list", (ROOT / "tests" / "check_ci_list.sh").is_file() and "test_ci_check" in py)
ck("no job but test and test-ee names a list", all("ci_list" not in j and "ci_run" not in j for n, j in js.items() if n not in ("test", "test-ee")))

# The mit run proves the open source part works alone: every ee directory is deleted before the console
# build and the install, and the run checks tares_ee is not importable.
ck("job test: the mit run deletes every ee directory before the console build",
   re.search(r"matrix\.suite == 'mit'[\s\S]*?-type d -name ee -print \| xargs", test) is not None
   and test.find("-name ee") < test.find("npm ci"), test[:600])
ck("job test: the mit run checks tares_ee is not importable",
   "find_spec('tares_ee') is None" in test and "matrix.suite }}\" = mit" in test)

# The ee suites: ee/tests/ci_list.txt, run by the test-ee job (duckdb and postgres). The mit run has
# no ee directory, so there is no ee list to check there.
ee_list = ROOT / "ee" / "tests" / "ci_list.txt"
has_ee = (ROOT / "ee").is_dir()
ee = []
if has_ee:
    ee = check("ee", ee_list, lambda n: f"{n}.py")
    ck("ee list holds the smoke test", "test_ee_smoke" in ee)
    unlisted = sorted(f.stem for f in ee_list.parent.glob("test_*.py") if f.stem not in ee)
    ck("every ee/tests/test_*.py is in ee/tests/ci_list.txt", not unlisted, "not listed: " + ", ".join(unlisted))
test_ee = js.get("test-ee", "")
ck("job test-ee: checks the ee list, then runs the ee suite for each database",
   re.search(r"(?m)^\s*bash tests/check_ci_list\.sh ee/tests/ci_list\.txt\s*$", test_ee) is not None
   and "python tests/ci_run.py --suite ee-postgres" in test_ee
   and re.search(r"(?m)^\s*python tests/ci_run\.py --suite ee\s*$", test_ee) is not None
   and re.search(r"db: \[duckdb, postgres\]", test_ee) is not None, test_ee[:500])
wheel = js.get("wheel", "")
ck("job wheel: builds the wheel, installs it away from the checkout and imports tares_ee",
   "pip wheel . --no-deps" in wheel and "import tares_ee" in wheel and "RUNNER_TEMP/venv" in wheel, wheel[:400])
ck("job wheel: starts taresd with no licence key and with a bad one and reads /api/licence",
   "TARES_LICENSE=TL1.not.valid" in wheel and "/api/licence" in wheel and "boot 18787 none False" in wheel
   and "env False" in wheel, wheel[-700:])

# The console's browser e2e suites: ui/tests/e2e_suites.txt, run by tests/ci_e2e.py in the e2e job.
e2e_list = ROOT / "ui" / "tests" / "e2e_suites.txt"
e2e = names(e2e_list)
ck("e2e: the list is not empty", len(e2e) > 0)
missing = [n for n in e2e if not (e2e_list.parent / f"{n}.e2e.cjs").is_file()]
ck("e2e: every name has its ui/tests/<name>.e2e.cjs", not missing, ", ".join(missing))
dupes = sorted({n for n in e2e if e2e.count(n) > 1})
ck("e2e: no name is listed twice", not dupes, ", ".join(dupes))
msg = out_of_order(entries(e2e_list), "ui/tests/e2e_suites.txt")
ck("e2e: the list is sorted", not msg, msg)
unlisted = sorted(f.name[:-len(".e2e.cjs")] for f in e2e_list.parent.glob("*.e2e.cjs") if f.name[:-len(".e2e.cjs")] not in e2e)
ck("e2e: every ui/tests/*.e2e.cjs is in the list", not unlisted, "not listed: " + ", ".join(unlisted))
pkg = json.loads((ROOT / "ui" / "package.json").read_text())["scripts"]
platform = re.findall(r"tests/([\w-]+)\.e2e\.cjs", pkg["test:platform"])
ck("e2e: every suite in `npm run test:platform` is in the list", platform and set(platform) <= set(e2e),
   ", ".join(sorted(set(platform) - set(e2e))))
e2e_job = js.get("e2e", "")
ck("job e2e: runs tests/ci_e2e.py for each shard of its matrix",
   re.search(r"python tests/ci_e2e\.py --shard \$\{\{ matrix\.shard \}\} --of (\d+)", e2e_job) is not None
   and re.search(r"shard: \[1(, \d+)*\]", e2e_job) is not None, e2e_job[:300])
of = int(re.search(r"--of (\d+)", e2e_job).group(1)) if "--of" in e2e_job else 0
ck("job e2e: the matrix lists shards 1 to N for --of N",
   re.search(r"shard: \[" + ", ".join(str(i) for i in range(1, of + 1)) + r"\]", e2e_job) is not None, f"--of {of}")
ck("job e2e: has a timeout-minutes, so a hang does not run for the 6 hour default", re.search(r"^\s+timeout-minutes: \d+", e2e_job, re.M) is not None)
ck("job e2e: screenshots are uploaded only when the run fails", "if: failure()" in e2e_job and "upload-artifact" in e2e_job)

import sys
sys.path.insert(0, str(ROOT / "tests"))
import ci_e2e
ck("ci_e2e.py reads ui/tests/e2e_suites.txt", ci_e2e.names() == e2e)
got = [t for i in range(1, of + 1) for t in ci_e2e.shard(e2e, i, of)]
ck(f"ci_e2e.py: the {of} shards run every listed suite once", sorted(got) == sorted(e2e) and len(got) == len(set(got)),
   f"missing {sorted(set(e2e) - set(got))}, extra {sorted(set(got) - set(e2e))}")
ports = [ci_e2e.port(n, e2e) for n in e2e]
ck("ci_e2e.py: every suite has its own port, far enough apart for its daemons", len(set(ports)) == len(ports)
   and all(b - a >= 30 for a, b in zip(ports, ports[1:])), str(ports))
ck("ci_e2e.py: only listed names carry a weight", set(ci_e2e.WEIGHT) <= set(e2e), ", ".join(sorted(set(ci_e2e.WEIGHT) - set(e2e))))

# tests/ci_run.py reads the two lists, and its shards together run each suite exactly once.
import sys
sys.path.insert(0, str(ROOT / "tests"))
import ci_run
want = {"duckdb": py + [f"factory:{n}" for n in sh],
        "postgres": [n for n in py if n not in ("test_state_postgres", "test_duckdb_memory")] + [f"factory:{n}" for n in sh],
        "mit": [n for n in py if n != "test_ee_packaging"] + [f"factory:{n}" for n in sh],
        }
if has_ee:
    want["ee"] = list(ee)
    want["ee-postgres"] = [n for n in ee if n not in ci_run.DUCKDB_ONLY]
for suite, w in want.items():
    got = [t for i in range(1, 6) for t in ci_run.shard(ci_run.items(suite), i, 5)]
    ck(f"ci_run.py, suite {suite}: the 5 shards run every listed test once", sorted(got) == sorted(w) and len(got) == len(set(got)),
       f"missing {sorted(set(w) - set(got))}, extra {sorted(set(got) - set(w))}")

ck("ci_run.py: the mit suite leaves out test_ee_packaging, the one test that needs ee/",
   "test_ee_packaging" in py and "test_ee_packaging" not in ci_run.items("mit"))

print(f"\n{P} passed, {F} failed")
raise SystemExit(1 if F else 0)
