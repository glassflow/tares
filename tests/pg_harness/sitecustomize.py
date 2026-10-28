"""CI's Postgres run (backend-postgres in .github/workflows/ci.yml): with this directory on
PYTHONPATH and TARES_TEST_PG set, every Store keeps its state in Postgres, in a schema of its own
per DuckDB file (its path and inode) and per test run (TARES_TEST_PG_RUN, inherited by the daemons
a test starts). A test that reopens the same file, or starts a daemon on it, finds the same state;
a file deleted and made again starts clean. A test that builds an old, DuckDB-only file passes
state_dsn=""."""
import hashlib
import os
import uuid

DSN = os.environ.get("TARES_TEST_PG")
if DSN:
    os.environ.setdefault("TARES_TEST_PG_RUN", uuid.uuid4().hex)
    import duckdb
    import psycopg
    import tares.store as _s

    _orig = _s.Store.__init__

    def _init(self, path="tares.duckdb", state_dsn=None):
        if state_dsn == "":
            return _orig(self, path, state_dsn=None)
        if path == ":memory:":
            key = f"mem-{id(self)}"
        else:
            if not os.path.exists(path):
                duckdb.connect(path).close()
            key = f"{os.path.abspath(path)}:{os.stat(path).st_ino}"
        schema = "t_" + hashlib.sha1((key + os.environ["TARES_TEST_PG_RUN"]).encode()).hexdigest()[:16]
        with psycopg.connect(DSN, autocommit=True) as c:
            c.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        _orig(self, path, state_dsn=f"{DSN}?options=-csearch_path%3D{schema}")

    _s.Store.__init__ = _init
