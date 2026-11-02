"""CI's Postgres run (backend-postgres in .github/workflows/ci.yml): with this directory on
PYTHONPATH and TARES_TEST_PG set, every Store keeps its state in Postgres, in a schema of its own
per DuckDB file. The schema's name is kept next to the file (<file>.pgschema), so a test that
reopens the file, or starts a daemon on it, finds the same state, and a file deleted and made again
gets a new schema (Linux reuses a deleted file's inode at once, so the inode cannot tell them
apart). A test that builds an old, DuckDB-only file passes state_dsn=""."""
import os
import uuid

DSN = os.environ.get("TARES_TEST_PG")
if DSN:
    import duckdb
    import psycopg
    import tares.store as _s

    _orig = _s.Store.__init__

    def _init(self, path="tares.duckdb", state_dsn=None):
        if state_dsn == "":
            return _orig(self, path, state_dsn=None)
        if path == ":memory:":
            schema = "t_" + uuid.uuid4().hex[:16]
        else:
            note = path + ".pgschema"
            if not os.path.exists(path):
                duckdb.connect(path).close()
                if os.path.exists(note):
                    os.remove(note)              # the file is new: so is its state
            if os.path.exists(note):
                schema = open(note).read().strip()
            else:
                schema = "t_" + uuid.uuid4().hex[:16]
                with open(note, "w") as f:
                    f.write(schema)
        with psycopg.connect(DSN, autocommit=True) as c:
            c.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        _orig(self, path, state_dsn=f"{DSN}?options=-csearch_path%3D{schema}")

    _s.Store.__init__ = _init
