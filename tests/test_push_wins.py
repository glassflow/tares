"""Push wins: a poll-mode source that also accepts pushes (claude_code — tailed locally *or* fed by
its Claude Code plugin) flips to push mode the first time it receives a pushed event, so the daemon
stops tailing files the plugin is now feeding and events don't land twice. Native push sources
(webhook) are unaffected.
"""
import asyncio, os, subprocess, sys
import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import clean_env

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

DB, PORT = "/tmp/pushwins.duckdb", "8809"


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


def _source(sources, name):
    return next((s for s in sources if s["name"] == name), None)


async def main():
    for p in (DB, DB + ".wal"):
        if os.path.exists(p):
            os.remove(p)
    env = {**clean_env(), "TARES_DB": DB, "TARES_CATALOG": "/tmp/none_pw.yaml",
           "TARES_PORT": PORT, "TARES_OTLP_GRPC_PORT": "off"}
    proc = subprocess.Popen([sys.executable, "-c", "from tares.cli import run_daemon; run_daemon()"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    B = f"http://127.0.0.1:{PORT}"
    line = ('{"sessionId":"sess-1","type":"assistant","cwd":"/tmp/proj","timestamp":'
            '"2026-07-03T10:00:00Z","message":{"role":"assistant","content":[{"type":"text",'
            '"text":"hello"}]}}')
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up", False); return
        async with httpx.AsyncClient() as cx:
            # a claude_code source created as a local tail (push defaults to false)
            await cx.post(f"{B}/api/sources", json={"name": "claude_code", "connector": "claude_code", "config": {}})
            src = _source((await cx.get(f"{B}/api/sources")).json(), "claude_code")
            ck("claude_code starts in tail mode (push falsy)", not src["config"].get("push"), str(src["config"]))

            # first pushed transcript line ingests AND flips the source to push mode
            r = await cx.post(f"{B}/ingest/claude_code", content=line + "\n",
                              headers={"content-type": "application/x-ndjson"})
            ck("push ingests the transcript line", r.status_code == 202 and r.json()["ingested"] == 1, r.text)
            src = _source((await cx.get(f"{B}/api/sources")).json(), "claude_code")
            ck("source flipped to push mode after first push", src["config"].get("push") is True, str(src["config"]))

            # second push is a no-op flip (already push) and still ingests
            r = await cx.post(f"{B}/ingest/claude_code", content=line + "\n",
                              headers={"content-type": "application/x-ndjson"})
            ck("second push still ingests (idempotent flip)", r.status_code == 202 and r.json()["ingested"] == 1, r.text)

            # a native push connector (webhook) is never given a push flag by the guard
            await cx.post(f"{B}/api/sources", json={"name": "evt", "connector": "webhook", "config": {}})
            await cx.post(f"{B}/ingest/evt", json={"a": 1})
            src = _source((await cx.get(f"{B}/api/sources")).json(), "evt")
            ck("webhook source config untouched by guard", "push" not in src["config"], str(src["config"]))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    asyncio.run(main())
