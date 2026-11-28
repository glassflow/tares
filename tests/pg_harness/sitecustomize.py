"""CI's Postgres run (the postgres suite of tests/ci_run.py): with this directory on
PYTHONPATH and TARES_TEST_PG set, every Store keeps its state in Postgres, in a schema of its own
per data directory. The schema's name is kept in the directory (`.pgschema`), so a test that
reopens the directory, or starts a daemon on it, finds the same state, and a directory emptied
and made again (`tests/_env.remove_db`, or removed outright) gets a new schema. A test that wants
a store on SQLite passes state_dsn=""."""
import os
import uuid

DSN = os.environ.get("TARES_TEST_PG")
if DSN:
    import psycopg
    import tares.store as _s

    NOTE = ".pgschema"
    _orig = _s.Store.__init__

    def _init(self, data_dir=".", state_dsn=None):
        if state_dsn == "":
            return _orig(self, data_dir, state_dsn=None)
        if data_dir == ":memory:":
            schema = "t_" + uuid.uuid4().hex[:16]
        else:
            os.makedirs(data_dir, exist_ok=True)
            note = os.path.join(data_dir, NOTE)
            if os.path.exists(note):
                schema = open(note).read().strip()
            else:
                schema = "t_" + uuid.uuid4().hex[:16]
                with open(note, "w") as f:
                    f.write(schema)
        with psycopg.connect(DSN, autocommit=True) as c:
            c.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        _orig(self, data_dir, state_dsn=f"{DSN}?options=-csearch_path%3D{schema}")

    _s.Store.__init__ = _init
