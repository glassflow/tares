"""TARES_SEED_PROJECT seeds only a new cell (TR-902).

A cell switched to SRE gets the var on its next upgrade. If it already has a project of its own it
must not get the demo, now or on a later boot; a brand-new cell still gets it on its first boot.
Boots real daemons like test_seed.

Run: .venv/bin/python tests/test_seed_new_cell_only.py
"""
import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import clean_env

import httpx

P = F = 0


def ck(l, c, d=""):
    global P, F
    P += 1 if c else 0
    F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


DB, PORT = "/tmp/tares-seed-newcell-test.duckdb", "8812"
B = f"http://127.0.0.1:{PORT}"


class FakeStack(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok","data":{"result":[]}}')


def daemon_env(stack_port, seed):
    base = f"http://127.0.0.1:{stack_port}"
    env = {**clean_env(), "TARES_DB": DB, "TARES_PORT": PORT, "TARES_OTLP_GRPC_PORT": "off",
           "TARES_DEMO_PROMETHEUS_URL": base, "TARES_DEMO_LOKI_URL": base,
           "TARES_DEMO_API_SERVER_URL": base}
    for k in ("TARES_CATALOG", "TARES_SEED_USECASE", "TARES_SEED_PROJECT"):
        env.pop(k, None)
    if seed is not None:
        env["TARES_SEED_PROJECT"] = seed
    return env


async def _wait(url, tries=80):
    for _ in range(tries):
        try:
            async with httpx.AsyncClient() as cx:
                if (await cx.get(url, timeout=1)).status_code == 200:
                    return True
        except Exception:
            pass
        await asyncio.sleep(0.25)
    return False


async def boot(env):
    proc = subprocess.Popen([sys.executable, "-c", "from tares.cli import run_daemon; run_daemon()"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc, await _wait(f"{B}/health")


def stop(proc):
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()


async def projects(cx):
    everything = (await cx.get(f"{B}/api/projects")).json()["projects"]
    return [p for p in everything if not p.get("default")]


def fresh():
    for p in (DB, DB + ".wal"):
        os.path.exists(p) and os.remove(p)


async def main():
    stack = HTTPServer(("127.0.0.1", 0), FakeStack)
    threading.Thread(target=stack.serve_forever, daemon=True).start()
    sp = stack.server_port

    print("== a new cell with the var gets the demo ==")
    fresh()
    proc, up = await boot(daemon_env(sp, "ai_sre_demo"))
    try:
        ck("daemon up", up)
        async with httpx.AsyncClient(timeout=20) as cx:
            got = await projects(cx)
            ck("the demo is there", len(got) == 1 and got[0]["template"] == "ai_sre_demo",
               json.dumps(got)[:200])
    finally:
        stop(proc)

    print("== a cell with a project of its own gets no demo, then or later ==")
    fresh()
    proc, up = await boot(daemon_env(sp, None))   # a factory cell: never carried the var
    try:
        ck("daemon up without the var", up)
        async with httpx.AsyncClient(timeout=20) as cx:
            r = await cx.post(f"{B}/api/projects", json={"template": "custom", "name": "Mine",
                                                         "objects": []})
            ck("the cell has a project", r.status_code == 201, r.text[:200])
    finally:
        stop(proc)
    for n in (1, 2):
        proc, up = await boot(daemon_env(sp, "ai_sre_demo"))   # switched to SRE: var appears
        try:
            ck(f"daemon up with the var (boot {n})", up)
            async with httpx.AsyncClient(timeout=20) as cx:
                got = await projects(cx)
                ck(f"only its own project, no demo (boot {n})",
                   [p["name"] for p in got] == ["Mine"], json.dumps(got)[:200])
        finally:
            stop(proc)

    print("== the skip is recorded: deleting the project later does not bring the demo ==")
    proc, up = await boot(daemon_env(sp, "ai_sre_demo"))
    try:
        async with httpx.AsyncClient(timeout=20) as cx:
            got = await projects(cx)
            r = await cx.delete(f"{B}/api/projects/{got[0]['id']}")
            ck("deleted its project", r.status_code == 200, r.text[:200])
    finally:
        stop(proc)
    proc, up = await boot(daemon_env(sp, "ai_sre_demo"))
    try:
        async with httpx.AsyncClient(timeout=20) as cx:
            ck("still no demo", await projects(cx) == [])
    finally:
        stop(proc)

    stack.shutdown()
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())
