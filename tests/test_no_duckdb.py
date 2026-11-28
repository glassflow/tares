"""Tares runs without DuckDB (TR-1815).

- `tares up` on a fresh data directory, a few session lines posted, then stopped: no file in the
  directory has `duckdb` in its name.
- A process that builds the app has not imported `duckdb`, whether it is installed or not.
- The package names no `duckdb` dependency.

Run: .venv/bin/python tests/test_no_duckdb.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from _env import clean_env  # noqa: E402

import httpx  # noqa: E402

FAILS = []


def ck(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"   {detail}"))
    if not ok:
        FAILS.append(name)


def free_port() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return str(s.getsockname()[1])


def fresh_up():
    print("== tares up on a fresh data directory ==")
    data_dir = os.path.join(tempfile.mkdtemp(prefix="tares-no-duckdb-"), "home")
    port = free_port()
    env = clean_env(TARES_AUTH_TOKEN=None, TARES_HOME=None)
    proc = subprocess.Popen([sys.executable, "-c", "from tares.cli import main; main()", "up",
                             "--data-dir", data_dir, "--port", port], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        up = False
        for _ in range(300):
            try:
                if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                    up = True
                    break
            except httpx.HTTPError:
                pass
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        ck("the daemon answers /health", up)
        if up:
            body = "\n".join(json.dumps({
                "sessionId": "s1", "type": "assistant", "cwd": "/w", "uuid": f"s1-{i}",
                "timestamp": "2026-11-27T10:00:00Z",
                "message": {"role": "assistant", "content": [{"type": "text", "text": f"m{i}"}]}})
                for i in range(5)) + "\n"
            r = httpx.post(f"{base}/ingest/claude_code", content=body,
                           headers={"content-type": "application/x-ndjson"}, timeout=30)
            ck("a few session lines are taken", r.status_code == 202, f"{r.status_code} {r.text[:200]}")
    finally:
        proc.terminate()
        proc.wait(timeout=20)
    files = [os.path.relpath(os.path.join(d, f), data_dir) for d, _, fs in os.walk(data_dir) for f in fs]
    ck("the data directory holds the session store", "tares.sqlite" in files or
       os.path.exists(os.path.join(data_dir, ".pgschema")), files)
    ck("and no file of DuckDB", not [f for f in files if "duckdb" in f.lower()], files)


def no_import():
    print("== building the app imports no duckdb ==")
    code = ("import sys\n"
            "from tares.daemon import make_app\n"
            "make_app()\n"
            "print('DUCKDB', 'duckdb' in sys.modules)\n")
    env = clean_env(TARES_HOME=tempfile.mkdtemp(prefix="tares-no-duckdb-app-"), TARES_AUTH_TOKEN=None)
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    ck("the app is built", out.returncode == 0, out.stderr[-500:])
    ck("'duckdb' is not in sys.modules", "DUCKDB False" in out.stdout, out.stdout[-300:])


def no_dependency():
    print("== the package ==")
    text = open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8").read()
    deps = text[text.index("dependencies = ["):]
    deps = deps[:deps.index("]")]
    ck("pyproject.toml names no duckdb and no pytz", "duckdb" not in deps and "pytz" not in deps, deps)


if __name__ == "__main__":
    fresh_up()
    no_import()
    no_dependency()
    print(f"\n{len(FAILS)} failed: {FAILS}" if FAILS else "\nall ok")
    sys.exit(1 if FAILS else 0)
