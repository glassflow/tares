"""Run CI's tests, or one shard of them.

    python tests/ci_run.py                       # everything, in order
    python tests/ci_run.py --shard 2 --of 5      # the 2nd of 5 shards (1-based)
    python tests/ci_run.py --suite postgres ...  # the list for the Postgres run
    python tests/ci_run.py --suite mit ...       # the core tests that need no ee/ (CI deletes it first)
    python tests/ci_run.py --suite ee ...        # the tests of the enterprise code, ee/tests
    python tests/ci_run.py --suite ee-postgres   # the same, with the state in Postgres

The tests are the ones named in tests/ci_list.txt (tests/<name>.py) and tests/factory/ci_list.txt
(tests/factory/<name>_test.sh, the factory CLI against a real Tares). The ee suites read
ee/tests/ci_list.txt (ee/tests/<name>.py) and ee/tests/factory/ci_list.txt
(ee/tests/factory/<name>_test.sh, shown as ee-factory:<name>) the same way. Shards are balanced by
WEIGHT (seconds a test took on a CI runner); anything unlisted counts as DEFAULT_WEIGHT. A test
that becomes slow should get a weight here so its shard does not finish last. Stops at the first
failure."""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY_LIST = os.path.join(HERE, "ci_list.txt")
SH_LIST = os.path.join(HERE, "factory", "ci_list.txt")
EE_DIR = os.path.join(os.path.dirname(HERE), "ee", "tests")
EE_PY_LIST = os.path.join(EE_DIR, "ci_list.txt")
EE_SH_LIST = os.path.join(EE_DIR, "factory", "ci_list.txt")

SUITES = ["duckdb", "postgres", "mit", "ee", "ee-postgres"]

# Not in the Postgres run: they are about the DuckDB file itself.
DUCKDB_ONLY = {"test_duckdb_memory", "test_state_postgres"}

# Not in the mit run, where every ee directory is deleted before tares is installed: they look at ee/.
NEEDS_EE = {"test_ee_packaging"}

DEFAULT_WEIGHT = 4
WEIGHT = {
    "factory:claude_update": 240, "factory:idle_wake": 235, "factory:pace": 225,
    "factory:crew": 100, "test_watch_escalate": 70, "test_slack": 65, "factory:freeze": 60,
    "test_project_keys": 19, "test_agents": 15, "test_plugin_challenger": 11, "test_tracing": 11,
    "factory:lsp": 11, "test_seed": 10, "factory:tares": 10, "test_login": 9, "test_usage": 9,
    "test_slack_ask": 9, "factory:usage": 9, "test_github_tickets": 8, "test_degraded": 8,
}


def names(path):
    with open(path) as f:
        return [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]


def items(suite):
    if suite in ("ee", "ee-postgres"):
        py = [t for t in names(EE_PY_LIST) if suite == "ee" or t not in DUCKDB_ONLY]
        return py + [f"ee-factory:{t}" for t in names(EE_SH_LIST)]
    py = [t for t in names(PY_LIST) if suite in ("duckdb", "mit") or t not in DUCKDB_ONLY]
    if suite == "mit":
        py = [t for t in py if t not in NEEDS_EE]
    return py + [f"factory:{t}" for t in names(SH_LIST)]


def shard(all_items, n, of):
    """Longest first, each onto the lightest shard; then back in list order within the shard."""
    order = {t: i for i, t in enumerate(all_items)}
    bins = [[0, []] for _ in range(of)]
    for t in sorted(all_items, key=lambda t: -WEIGHT.get(t, DEFAULT_WEIGHT)):
        b = min(bins, key=lambda b: b[0])
        b[0] += WEIGHT.get(t, DEFAULT_WEIGHT)
        b[1].append(t)
    return sorted(bins[n - 1][1], key=order.get)


def run(t, suite="duckdb"):
    if t.startswith(("factory:", "ee-factory:")):
        kind, name = t.split(":", 1)
        base = EE_DIR if kind == "ee-factory" else HERE
        print(f"== {kind.replace('-', ' ')} {name} ==", flush=True)
        return subprocess.call(["bash", os.path.join(base, "factory", f"{name}_test.sh")])
    base = EE_DIR if suite in ("ee", "ee-postgres") else HERE
    print(f"== {t} ==", flush=True)
    return subprocess.call([sys.executable, os.path.join(base, f"{t}.py")])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--suite", choices=SUITES, default="duckdb")
    p.add_argument("--shard", type=int, default=1)
    p.add_argument("--of", type=int, default=1)
    p.add_argument("--list", action="store_true", help="print the shard's tests, run nothing")
    a = p.parse_args()
    if not 1 <= a.shard <= a.of:
        p.error("--shard must be between 1 and --of")
    mine = shard(items(a.suite), a.shard, a.of)
    if a.list:
        print("\n".join(mine))
        return 0
    for t in mine:
        rc = run(t, a.suite)
        if rc:
            print(f"FAILED: {t} (exit {rc})", flush=True)
            return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
