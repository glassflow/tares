"""A recording carries the person of the key that shipped it (TR-620): the cell sets
`tares_person=<person id>` on every claude_code event pushed to /ingest, from the credential and
never from what the sender wrote. The root token (no person) leaves no label; with auth off it is
`local`. The checks with two people are in ee/tests/test_recording_person_team.py.
"""
import asyncio, json, os, subprocess, sys
import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import clean_env

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

AUTH = "root-token"


async def _wait(url):
    for _ in range(80):
        try:
            async with httpx.AsyncClient() as cx:
                if (await cx.get(url, timeout=1)).status_code == 200:
                    return True
        except Exception:
            pass
        await asyncio.sleep(0.25)
    return False


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def start(db, port, auth):
    for p in (db, db + ".wal"):
        if os.path.exists(p):
            os.remove(p)
    env = {**clean_env(), "TARES_DB": db, "TARES_PORT": port, "TARES_OTLP_GRPC_PORT": "off"}
    if auth:
        env["TARES_AUTH_TOKEN"] = AUTH
    return subprocess.Popen([sys.executable, "-c", "from tares.cli import run_daemon; run_daemon()"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def line(sid, text, spoof=None, **extra):
    o = {"sessionId": sid, "type": "assistant", "cwd": "/tmp/proj",
         "timestamp": "2026-10-21T10:00:00Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}, **extra}
    if spoof:
        o["tares_person"] = spoof
        o["labels"] = {"tares_person": spoof}
    return json.dumps(o)


async def push(cx, B, tok, body, **headers):
    h = {"content-type": "application/x-ndjson", **(H(tok) if tok else {}), **headers}
    return await cx.post(f"{B}/ingest/claude_code", content=body, headers=h)


async def persons(cx, B, tok, session, text):
    """The tares_person label of each stored event with `text` in `session`, read back through
    /read: the id, or None when the event has no such label."""
    rd = (await cx.post(f"{B}/read", headers=H(tok) if tok else {}, json={
        "selector": {"session": session}, "window": "3650d", "include_payload": True})).json()
    return [(r.get("labels") or {}).get("tares_person") for r in rd.get("rows", [])
            if (r.get("text") or "") == text]


async def auth_off():
    port = "8872"
    proc = start("/tmp/recording_person_off.duckdb", port, False)
    B = f"http://127.0.0.1:{port}"
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up (auth off)", False); return
        async with httpx.AsyncClient() as cx:
            await cx.post(f"{B}/api/sources",
                          json={"name": "claude_code", "connector": "claude_code", "config": {}})
            r = await push(cx, B, None, line("so", "off-plain") + "\n"
                           + line("so", "off-spoof", spoof="p_someone") + "\n")
            ck("auth off: ingest", r.status_code == 202 and r.json()["ingested"] == 2, r.text)
            lo = await persons(cx, B, None, "so", "off-plain")
            ck("auth off: the label is local", lo == ["local"], str(lo))
            lo = await persons(cx, B, None, "so", "off-spoof")
            ck("auth off, line names someone else: still local", lo == ["local"], str(lo))
    finally:
        proc.terminate(); proc.wait(timeout=10)


async def main():
    await auth_off()
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())
