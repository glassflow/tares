"""include_payload: /read serves a one-line summary `text` by default, but returns the full
lossless stored record as `raw` on each row when asked. Proves the read path can hand agents the
complete event (not just the truncated summary), across all sources and narrowed to named ones,
while the default stays lean. Uses claude_code because its `text` is a deliberately lossy summary (a
tool_use renders as `→ Bash({first 80 chars…})`) while the raw record keeps the full input.
"""
import asyncio, json, os, subprocess, sys
import httpx

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

DB, PORT = "/tmp/inclpayload.duckdb", "8810"
LONG_CMD = "echo " + "x" * 300   # >80 chars, so the summary text must truncate it but raw keeps it


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


async def main():
    for p in (DB, DB + ".wal"):
        if os.path.exists(p):
            os.remove(p)
    env = {**os.environ, "TARES_DB": DB, "TARES_CATALOG": "/tmp/none_ip.yaml",
           "TARES_PORT": PORT, "TARES_OTLP_GRPC_PORT": "off"}
    proc = subprocess.Popen([sys.executable, "-c", "from tares.cli import run_daemon; run_daemon()"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    B = f"http://127.0.0.1:{PORT}"
    line = json.dumps({"sessionId": "sess-raw", "type": "assistant", "cwd": "/tmp/proj",
                       "timestamp": "2026-07-03T10:00:00Z",
                       "message": {"role": "assistant",
                                   "content": [{"type": "tool_use", "name": "Bash",
                                                "input": {"command": LONG_CMD}}]}})
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up", False); return
        async with httpx.AsyncClient() as cx:
            await cx.post(f"{B}/api/sources", json={"name": "claude_code", "connector": "claude_code",
                                                    "config": {"push": True}})
            r = await cx.post(f"{B}/ingest/claude_code", content=line + "\n",
                              headers={"content-type": "application/x-ndjson"})
            ck("transcript line ingested", r.status_code == 202 and r.json()["ingested"] == 1, r.text)

            # ── /read default: summary text, no raw ──────────────────────────────
            r = await cx.post(f"{B}/read", json={"selector": {"session": "sess-raw"}, "window": "1h"})
            row = r.json()["rows"][0]
            ck("read (default) returns rows", r.json()["count"] == 1, r.text)
            ck("default row has summary text", row.get("text", "").startswith("→ Bash"), str(row))
            ck("default row has NO raw", "raw" not in row, str(row))
            ck("summary text is lossy (full command not in it)", LONG_CMD not in row.get("text", ""))

            # ── /read include_payload: full lossless record on `raw` ─────────────
            r = await cx.post(f"{B}/read", json={"selector": {"session": "sess-raw"}, "window": "1h",
                                                 "include_payload": True})
            row = r.json()["rows"][0]
            raw = row.get("raw")
            ck("include_payload row carries raw", isinstance(raw, dict), str(row)[:200])
            ck("raw is the full stored record (sessionId, cwd)",
               raw and raw.get("sessionId") == "sess-raw" and raw.get("cwd") == "/tmp/proj", str(raw)[:200])
            ck("raw is lossless — full command preserved though text truncated it",
               raw and raw["message"]["content"][0]["input"]["command"] == LONG_CMD)
            ck("summary text still present alongside raw", row.get("text", "").startswith("→ Bash"))

            # ── /read narrowed to named sources: same behaviour ─────────────────
            r = await cx.post(f"{B}/read", json={"sources": ["claude_code"],
                                                 "selector": {"session": "sess-raw"}, "window": "1h"})
            ck("narrowed read (default) has no raw", "raw" not in r.json()["rows"][0], str(r.json()["rows"][0]))
            r = await cx.post(f"{B}/read", json={"sources": ["claude_code"],
                                                 "selector": {"session": "sess-raw"},
                                                 "window": "1h", "include_payload": True})
            qraw = r.json()["rows"][0].get("raw")
            ck("narrowed read include_payload carries lossless raw",
               isinstance(qraw, dict) and qraw["message"]["content"][0]["input"]["command"] == LONG_CMD,
               str(r.json()["rows"][0])[:200])
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
