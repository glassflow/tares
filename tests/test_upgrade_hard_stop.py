"""A cell upgraded to this version and then stopped hard (a pod killed) must open again. The
upgrade adds columns to agent_runs, a table with an index; left in the WAL, they replayed after a
hard stop into "Corrupted ART index" and every query failed (seen on an upgraded copy of a real
cell, 2026-09-29). Store checkpoints after its migrations, so an upgrade never waits in the WAL.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import duckdb  # noqa: E402
from tares.store import Store  # noqa: E402

DB = os.path.join(tempfile.mkdtemp(prefix="tares-hardstop-"), "t.duckdb")
P = F = 0


def ck(label, cond, detail=""):
    global P, F
    if cond:
        P += 1; print(f"  ok   {label}")
    else:
        F += 1; print(f"  FAIL {label}  {detail}")


def wal_bytes():
    return os.path.getsize(DB + ".wal") if os.path.exists(DB + ".wal") else 0


print("== an old database, upgraded on open ==")
Store(DB).con.close()
con = duckdb.connect(DB)
# as the release before lineage left it: no lineage columns or indexes, runs already there
for ix in ("ix_agent_runs_dispatch", "ix_agent_runs_parent", "ix_agent_runs_project",
           "ix_dispatch_log_project", "ix_dispatch_log_parent"):
    con.execute(f"DROP INDEX IF EXISTS {ix}")
con.execute("DROP INDEX IF EXISTS ix_agent_runs_agent")
for col in ("woken_by", "parent_run_id", "project", "skills"):
    con.execute(f"ALTER TABLE agent_runs DROP COLUMN IF EXISTS {col}")
for col in ("project", "parent_run_id"):
    con.execute(f"ALTER TABLE dispatch_log DROP COLUMN IF EXISTS {col}")
con.execute("CREATE INDEX ix_agent_runs_agent ON agent_runs(agent)")
con.execute("DELETE FROM settings WHERE key = 'lineage_backfilled'")
for i in range(20):
    con.execute("INSERT INTO agent_runs (id, agent, trigger, dispatch_id, key_value, status, "
                "started_at) VALUES (?, 'a', 't', '', 'k', 'ok', now())", [f"old_{i}"])
con.execute("CHECKPOINT")
con.close()

st = Store(DB)
cols = {r[0] for r in st.con.execute(
    "SELECT column_name FROM information_schema.columns WHERE table_name = 'agent_runs'").fetchall()}
ck("the upgrade added the lineage columns", {"woken_by", "parent_run_id", "project"} <= cols, str(cols))
ck("nothing of the upgrade is left in the WAL once the store is open", wal_bytes() == 0,
   f"{wal_bytes()} bytes")
st.con.close()

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
