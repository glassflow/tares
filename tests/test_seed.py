"""TARES_SEED_USECASE: a cell born with its project already running.

Boots real daemons (subprocess, like test_agents) against the hosted-demo stubs and checks the
one-shot semantics: seeded on first boot, marker written, a user's deletion never resurrected,
an unknown template fixable (no marker), and no seeding at all when the var is unset.
"""
import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

DB, PORT = "/tmp/tares-seed-test.duckdb", "8811"
B = f"http://127.0.0.1:{PORT}"


class FakeStack(BaseHTTPRequestHandler):
    """Just enough of the hosted demo stack for the template's plan/detect to be happy."""
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'{"status":"ok","data":{"result":[]}}')


def daemon_env(stack_port: int, seed: str | None) -> dict:
    base = f"http://127.0.0.1:{stack_port}"
    env = {**os.environ, "TARES_DB": DB, "TARES_PORT": PORT, "TARES_OTLP_GRPC_PORT": "off",
           "TARES_DEMO_PROMETHEUS_URL": base, "TARES_DEMO_LOKI_URL": base,
           "TARES_DEMO_API_SERVER_URL": base}
    env.pop("TARES_CATALOG", None)
    env.pop("TARES_SEED_USECASE", None)
    if seed is not None:
        env["TARES_SEED_USECASE"] = seed
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
    ok = await _wait(f"{B}/health")
    return proc, ok


def stop(proc):
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()


async def projects(cx):
    """The projects besides the default one (never seeded, never deleted, made only when something
    has no other project)."""
    everything = (await cx.get(f"{B}/api/projects")).json()["projects"]
    assert sum(1 for p in everything if p.get("default")) <= 1, everything
    return [p for p in everything if not p.get("default")]


async def main():
    for p in (DB, DB + ".wal"):
        if os.path.exists(p):
            os.remove(p)
    stack = HTTPServer(("127.0.0.1", 0), FakeStack)
    threading.Thread(target=stack.serve_forever, daemon=True).start()

    # ── first boot with the var: seeded exactly once ─────────────────────────
    proc, up = await boot(daemon_env(stack.server_port, "ai_sre_demo"))
    try:
        ck("daemon up", up)
        async with httpx.AsyncClient(timeout=20) as cx:
            uc = await projects(cx)
            ck("project seeded on first boot", len(uc) == 1 and uc[0]["template"] == "ai_sre_demo",
               json.dumps(uc)[:200])
            uid = uc[0]["id"] if uc else ""
            srcs = {s["name"] for s in (await cx.get(f"{B}/api/sources")).json()}
            ck("seeded objects exist (loki logs source included)",
               {"demo_metrics", "demo_logs", "demo_alerts"} <= srcs, str(srcs))
            logs = next(s for s in (await cx.get(f"{B}/api/sources")).json() if s["name"] == "demo_logs")
            ck("seeded in hosted mode", logs["connector"] == "loki", logs["connector"])
            # a user deletes it on purpose
            r = await cx.delete(f"{B}/api/projects/{uid}")
            ck("user can delete the seeded project", r.status_code == 200, r.text[:200])
    finally:
        stop(proc)

    # ── second boot, same var: the deletion sticks ───────────────────────────
    proc, up = await boot(daemon_env(stack.server_port, "ai_sre_demo"))
    try:
        ck("daemon back up", up)
        async with httpx.AsyncClient(timeout=20) as cx:
            uc = await projects(cx)
            ck("deleted project is NOT resurrected on restart", uc == [], json.dumps(uc)[:200])
    finally:
        stop(proc)

    # ── unknown template: warns, no marker, so fixing the config still seeds ───
    for p in (DB, DB + ".wal"):
        os.path.exists(p) and os.remove(p)
    proc, up = await boot(daemon_env(stack.server_port, "no_such_template"))
    try:
        ck("daemon healthy despite unknown seed template", up)
        async with httpx.AsyncClient(timeout=20) as cx:
            ck("nothing seeded for an unknown template", await projects(cx) == [])
    finally:
        stop(proc)
    proc, up = await boot(daemon_env(stack.server_port, "ai_sre_demo"))
    try:
        ck("daemon up after fixing the config", up)
        async with httpx.AsyncClient(timeout=20) as cx:
            uc = await projects(cx)
            ck("fixed config seeds on the next boot (typo wrote no marker)",
               len(uc) == 1, json.dumps(uc)[:200])
    finally:
        stop(proc)

    # ── var unset: nothing happens ───────────────────────────────────────────
    for p in (DB, DB + ".wal"):
        os.path.exists(p) and os.remove(p)
    proc, up = await boot(daemon_env(stack.server_port, None))
    try:
        ck("daemon up without the var", up)
        async with httpx.AsyncClient(timeout=20) as cx:
            ck("no seeding without TARES_SEED_USECASE", await projects(cx) == [])
    finally:
        stop(proc)

    stack.shutdown()
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())
