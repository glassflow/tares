"""The slack a timing check allows over its target, by state backend. Shared by the page-times test
(ee/tests/test_factory_page_times.py, TR-1607) and the read cost checks of test_sre_home (TR-1731),
test_sre_services and test_factory_home (TR-1793); it lives here so a core test can use it in the run
without ee.

Use: sys.path.insert(0, os.path.dirname(__file__)); from _timing import SLACK, read_limit
"""
# How much a measured time may exceed what it is held to. Postgres gets more: a round trip to the
# database (a container here, a service on CI) varies from run to run. The page-times test gives it to
# the routes in its OVER list only; test_sre_home gives it to its 300 ms target.
SLACK = {"duckdb": 1.25, "postgres": 1.5}


def read_limit(store, p95_ms, target_ms=300):
    """The limit a read cost check holds `p95_ms` to: the target with the slack of the backend the
    state is on. The store says which, not the env: CI's duckdb and mit suites set TARES_TEST_PG
    without the harness on the path. Prints the p95 and the limit, and a note when the read is over
    the target but inside the limit."""
    backend = "postgres" if store.sessions_db.dialect == "postgresql" else "duckdb"
    limit = target_ms * SLACK[backend]
    print(f"  p95 {p95_ms:.0f} ms, limit {limit:.0f} ms (target {target_ms} ms, {backend} slack {SLACK[backend]})")
    if target_ms <= p95_ms < limit:
        # a note, not a failure: over the target, within what this backend's timing varies by
        print(f"  note: p95 {p95_ms:.0f} ms is over the {target_ms} ms target, within the {backend} slack")
    return limit
