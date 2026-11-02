"""DuckDB's memory is bounded on every machine (TR-774). With no container limit and no env, a
store gets the default cap instead of DuckDB's 80% of the machine's RAM; the env var and a
container limit still win over it.

Runs against real stores; the container limit is faked since the host decides whether there is one.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tares.store as store_mod
from tares.store import DEFAULT_DUCKDB_MEMORY_LIMIT, Store

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


def settings(env, cgroup_bytes):
    for k in ("TARES_DUCKDB_MEMORY_LIMIT", "TARES_DUCKDB_THREADS"):
        os.environ.pop(k, None)
    os.environ.update(env)
    store_mod._cgroup_mem_bytes = lambda: cgroup_bytes
    path = os.path.join(tempfile.mkdtemp(), "t.duckdb")
    s = Store(path)
    mem, threads, tmp = s.con.execute(
        "SELECT current_setting('memory_limit'), current_setting('threads'), "
        "current_setting('temp_directory')").fetchone()
    s.con.close()
    return mem, int(threads), tmp, path


def gib(text):
    """DuckDB prints the limit as '953.6 MiB' or '1.0 GiB'; return bytes."""
    num, unit = text.split()
    return float(num) * {"KiB": 1 << 10, "MiB": 1 << 20, "GiB": 1 << 30}[unit]


def main():
    mem, threads, tmp, path = settings({}, None)
    check("no env, no container: the default cap, not 80% of RAM",
          abs(gib(mem) - 1_000_000_000) < 1 << 20 and DEFAULT_DUCKDB_MEMORY_LIMIT == "1GB", mem)
    check("no env, no container: 2 threads", threads == 2, str(threads))
    check("no env, no container: spills next to the database",
          tmp == os.path.join(os.path.dirname(path), ".duckdb_tmp"), tmp)

    mem, threads, _, _ = settings({"TARES_DUCKDB_MEMORY_LIMIT": "512MB"}, None)
    check("TARES_DUCKDB_MEMORY_LIMIT overrides the default",
          abs(gib(mem) - 512_000_000) < 1 << 20, mem)

    mem, _, _, _ = settings({}, 2 * (1 << 30))
    check("a container limit still wins over the default (60% of 2 GiB)",
          abs(gib(mem) - 1228_000_000) < 61_000_000, mem)   # printed to 2 digits

    mem, _, _, _ = settings({"TARES_DUCKDB_MEMORY_LIMIT": "512MB"}, 2 * (1 << 30))
    check("the env var wins over a container limit",
          abs(gib(mem) - 512_000_000) < 1 << 20, mem)

    _, threads, _, _ = settings({"TARES_DUCKDB_THREADS": "4"}, None)
    check("TARES_DUCKDB_THREADS still sets the threads", threads == 4, str(threads))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


main()
