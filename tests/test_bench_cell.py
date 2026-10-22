"""scripts/bench_cell.py (TR-654): neutralize() clears what lets a daemon on a cell's copy reach
the outside world, the sandbox denies outbound network, and the bench runs end to end on a tiny
database. CI runs on Linux, where the bench refuses to start a daemon without sandbox-exec, so
there only neutralize() and stage_review() are covered.

Run: .venv/bin/python tests/test_bench_cell.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.dirname(__file__))
from _env import clean_env  # noqa: E402

import duckdb  # noqa: E402

import bench_cell  # noqa: E402
from tares.store import Store  # noqa: E402

PASS = FAIL = 0


def ck(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


def tiny_db(path: str) -> None:
    s = Store(path)
    s.create_project("uc_bench", "custom", "Bench", {"objects": []})
    s.set_project_kind("uc_bench", "software_factory")
    s.create_ticket("uc_bench", "tares", "First")
    s.create_ticket("uc_bench", "tares", "Second")
    s.upsert_catalog_source("gh", "github", "github", "60s", {"repo": "a/b"}, paused=False)
    s.upsert_github_credential("default", "ghp_secret")
    s.set_setting("anthropic_key", "sk-secret")
    s.con.close()


tmp = tempfile.mkdtemp(prefix="test-bench-cell-")
try:
    db = os.path.join(tmp, "tiny.duckdb")
    tiny_db(db)

    print("neutralize")
    bench_cell.neutralize(db)
    con = duckdb.connect(db, read_only=True)
    ck("sources paused", con.execute("SELECT bool_and(paused) FROM catalog_sources").fetchone()[0])
    ck("github credentials gone", con.execute("SELECT count(*) FROM github_credentials").fetchone()[0] == 0)
    ck("settings gone", con.execute("SELECT count(*) FROM settings").fetchone()[0] == 0)
    con.close()

    print("stage_review")
    ck("moves open tickets to review", bench_cell.stage_review(db, 1) == 1)
    con = duckdb.connect(db, read_only=True)
    ck("one ticket in review",
       con.execute("SELECT count(*) FROM tickets WHERE stage = 'review'").fetchone()[0] == 1)
    con.close()

    if sys.platform != "darwin" or not shutil.which("sandbox-exec"):
        print("  skip sandbox and end-to-end bench: needs macOS sandbox-exec (CI runs on Linux)")
    else:
        print("sandbox")
        probe = ("import socket\ntry:\n    socket.create_connection(('1.1.1.1', 443), timeout=2)\n"
                 "    print('connected')\nexcept OSError:\n    print('blocked')")
        out = subprocess.run(bench_cell.sandboxed([sys.executable, "-c", probe]),
                             capture_output=True, text=True, timeout=30).stdout.strip()
        ck("outbound network is denied", out == "blocked", out)

        print("bench end to end")
        fresh = os.path.join(tmp, "fresh.duckdb")
        tiny_db(fresh)
        r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "bench_cell.py"),
                            "--db", fresh, "--runs", "1"],
                           capture_output=True, text=True, timeout=300, env=clean_env())
        ck("exits 0", r.returncode == 0, r.stdout[-1500:] + r.stderr[-1500:])
        ck("has the tickets row", any(line.startswith("tickets ") for line in r.stdout.splitlines()),
           r.stdout[-800:])
        ck("original unchanged", "unchanged" in r.stdout, r.stdout[-300:])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
