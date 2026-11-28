"""What a restart costs on a store the size of a real factory cell (TR-1815).

A data directory is filled straight through the session store with 2145 sessions and 262977 lines
(the counts of a real factory cell, as in ee/tests/factory_seed.py), their replies' usage and their
stations. Then the daemon is started on it, the way `tares up` starts it:

- it is ready (GET /health answers ok) in under READY seconds;
- nothing it runs while it starts reads `session_lines`: a sentinel on every SQLAlchemy engine of
  the daemon's process writes down each statement that names the table, from the first import until
  the start's background work is done;
- after 10,000 lines are posted to /ingest/claude_code, its resident memory is within GROWTH of what
  it was once it had started: no line is kept in memory.

Runs on the backend the suite gives: SQLite, or Postgres under tests/pg_harness (TARES_TEST_PG).

Run: .venv/bin/python tests/test_restart_cost.py
Postgres: TARES_TEST_PG=postgresql://... PYTHONPATH=tests/pg_harness .venv/bin/python tests/test_restart_cost.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from _env import clean_env  # noqa: E402

import httpx  # noqa: E402
import psutil  # noqa: E402
from sqlalchemy import insert  # noqa: E402

from tares.session_store import tables as T  # noqa: E402
from tares.store import Store  # noqa: E402

READY = 5.0            # seconds from the process start to an ok /health
GROWTH = 0.10          # resident memory may grow this much while 10,000 lines are posted
SETTLE = 3.0           # seconds the start's background work is given before the counts are read
SESSIONS, LINES = 2145, 262977
POSTED, BATCH = 10000, 500
NOW = datetime.now(timezone.utc).replace(microsecond=0)
TYPES = ["user", "assistant", "tool_use", "tool_result", "assistant"]
FAILS = []

# the daemon, with a sentinel on every engine of its process before tares is imported
DAEMON = """
import os
from sqlalchemy import event
from sqlalchemy.engine import Engine
log = open(os.environ["RESTART_SENTINEL"], "a", buffering=1)

@event.listens_for(Engine, "before_cursor_execute")
def _seen(conn, cursor, statement, *a):
    if "session_lines" in statement:
        log.write(" ".join(statement.split())[:200] + "\\n")

from tares.cli import run_daemon
run_daemon()
"""


def ck(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"   {detail}"))
    if not ok:
        FAILS.append(name)


def free_port() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return str(s.getsockname()[1])


def seed(data_dir: str) -> None:
    """The sessions, their stored lines, usage and stations, written in chunks through Core."""
    s = Store(data_dir)
    per, extra = divmod(LINES, SESSIONS)
    flags = json.dumps({"include_thinking": False, "redact": True})
    sessions, lines, usage, stations = [], [], [], []

    def flush(force=False):
        with s.sessions_db.begin() as c:
            for table, rows in ((T.sessions, sessions), (T.session_lines, lines),
                                (T.session_usage, usage), (T.session_stations, stations)):
                if rows and (force or len(rows) >= 20000):
                    c.execute(insert(table), rows)
                    rows.clear()

    for k in range(SESSIONS):
        sid = f"seed-{k:05d}"
        n = per + (1 if k < extra else 0)
        first = NOW - timedelta(days=30) + timedelta(minutes=k * 15)
        sessions.append({"session": sid, "source": "claude_code", "started_at": first,
                         "last_line_at": first + timedelta(seconds=n * 10), "line_count": n,
                         "tool_calls": n // 5, "coordination_calls": 0, "lines_complete": True,
                         "updated_at": first, "lines_origin": "table"})
        for i in range(n):
            kind = TYPES[i % len(TYPES)]
            at = first + timedelta(seconds=i * 10)
            lines.append({"session": sid, "seq": i + 1, "line_key": f"{sid}:{i}", "at": at, "type": kind,
                          "labels": "{}", "render": flags,
                          "payload": json.dumps({"uuid": f"{sid}:{i}", "type": kind, "sessionId": sid,
                                                 "message": {"role": kind, "content": f"line {i} " + "x" * 200}})})
            if kind == "assistant" and i % 10 == 1:
                usage.append({"session": sid, "message": f"m-{sid}-{i}", "replied_at": at,
                              "model": "claude-sonnet-4-5", "input_tokens": 10, "output_tokens": 20,
                              "cache_write_tokens": 0, "cache_read_tokens": 100, "cost_usd": 0.001,
                              "cache_write_1h_tokens": 0})
        stations.append({"session": sid, "station": f"crew-build-m{k % 40}", "role": "builder",
                         "seen_at": first, "crew_id": "crew-seed"})
        flush()
    flush(force=True)
    s.close()


def wire(sid: str, i: int) -> dict:
    return {"sessionId": sid, "type": "assistant", "cwd": "/w", "uuid": f"{sid}-{i}",
            "timestamp": (NOW + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "message": {"role": "assistant", "content": [{"type": "text", "text": f"posted {i} " + "y" * 200}]}}


def main():
    data_dir = tempfile.mkdtemp(prefix="tares-restart-cost-")
    sentinel = os.path.join(tempfile.mkdtemp(prefix="tares-restart-sentinel-"), "seen.txt")
    t = time.perf_counter()
    seed(data_dir)
    s = Store(data_dir)
    backend = s.sessions_db.dialect
    s.close()
    print(f"== a store of {SESSIONS} sessions and {LINES} lines on {backend}, seeded in "
          f"{time.perf_counter() - t:.0f}s ==")

    port = free_port()
    env = clean_env(TARES_HOME=data_dir, TARES_PORT=port, RESTART_SENTINEL=sentinel,
                    TARES_AUTH_TOKEN=None)
    base = f"http://127.0.0.1:{port}"
    t0 = time.perf_counter()
    proc = subprocess.Popen([sys.executable, "-c", DAEMON], env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        ready = None
        while time.perf_counter() - t0 < 60:
            try:
                r = httpx.get(f"{base}/health", timeout=1)
                if r.status_code == 200 and r.json().get("status") == "ok":
                    ready = time.perf_counter() - t0
                    break
            except httpx.HTTPError:
                pass
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        ck(f"the daemon is ready in under {READY:.0f}s", ready is not None and ready < READY,
           "not ready" if ready is None else f"{ready:.2f}s")
        if ready is None:
            return
        print(f"  ready in {ready:.2f}s")
        time.sleep(SETTLE)
        seen = open(sentinel).read().splitlines() if os.path.exists(sentinel) else []
        ck("nothing read session_lines while it started", seen == [], seen[:3])

        me = psutil.Process(proc.pid)
        start_rss = me.memory_info().rss
        with httpx.Client(timeout=120) as cx:
            for b in range(POSTED // BATCH):
                body = "\n".join(json.dumps(wire(f"posted-{b % 20:02d}", b * BATCH + i))
                                 for i in range(BATCH)) + "\n"
                r = cx.post(f"{base}/ingest/claude_code", content=body,
                            headers={"content-type": "application/x-ndjson"})
                if r.status_code != 202:
                    ck("every batch is taken", False, f"{r.status_code} {r.text[:200]}")
                    break
        time.sleep(1.0)
        end_rss = me.memory_info().rss
        after = open(sentinel).read().splitlines() if os.path.exists(sentinel) else []
        ck("the sentinel sees the statements the posts ran on session_lines (it works)",
           any("INSERT INTO session_lines" in x for x in after), after[:2])
        grew = (end_rss - start_rss) / start_rss
        print(f"  resident memory {start_rss / 2**20:.0f} MiB after the start, "
              f"{end_rss / 2**20:.0f} MiB after {POSTED} lines ({grew:+.1%})")
        ck(f"its memory after {POSTED} posted lines is within {GROWTH:.0%} of what it was after the start",
           abs(grew) <= GROWTH, f"{grew:+.1%}")
        s = Store(data_dir)
        with s.sessions_db.read() as c:
            from sqlalchemy import func, select
            n = c.execute(select(func.count()).select_from(T.session_lines)).scalar_one()
        s.close()
        ck("and every posted line is stored", n == LINES + POSTED, n)
    finally:
        proc.terminate()
        proc.wait(timeout=20)


if __name__ == "__main__":
    main()
    print(f"\n{len(FAILS)} failed: {FAILS}" if FAILS else "\nall ok")
    sys.exit(1 if FAILS else 0)
