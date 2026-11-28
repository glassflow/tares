"""Claude Code and Codex lines arrive through the runtime integration (Tares 2.0, TR-1804).

POST /ingest/claude_code and POST /ingest/codex are routes of their own: the runtime slot's integration
makes session lines of the body (tares/session_map.py, tares/session_codex.py) and the route stores them
under `claude_code`. There is no source catalog (TR-1811), and the Claude Code row on Settings reads the
newest stored line.

  * a Claude Code line and a Codex fact each land in the sessions table, stamped with the person of the
    key that sent them (the cell's one person, without a licence);
  * the source catalog's route is gone (404), before and after;
  * the Claude Code tool row on Settings says it is reporting;
  * no credential answers 401, a key without the ingest scope 403.

Run: python tests/test_ingest_runtime_slot.py
"""
import asyncio, json, os, subprocess, sys, tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import clean_env

import httpx

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


PORT, AUTH = "8895", "root-token-intake"
NOW = datetime.now(timezone.utc).replace(microsecond=0).isoformat()


async def _wait(url):
    for _ in range(120):
        try:
            async with httpx.AsyncClient() as cx:
                if (await cx.get(url, timeout=1)).status_code == 200:
                    return True
        except Exception:
            pass
        await asyncio.sleep(0.25)
    return False


def _stored(db_path: str, session: str) -> tuple[dict, list]:
    """({session: (line_count, source)}, the tares_person of each line of `session`) from the store the
    daemon used: opened in this process on the same file, so the Postgres harness finds its schema too."""
    from sqlalchemy import select
    from tares.session_store.tables import session_lines, sessions
    from tares.store import Store
    st = Store(db_path)
    try:
        with st.sessions_db.read() as conn:
            got = {r[0]: (r[1], r[2]) for r in conn.execute(
                select(sessions.c.session, sessions.c.line_count, sessions.c.source)).all()}
            cols = set(session_lines.c.keys())
            people = []
            if "labels" in cols:
                for (lab,) in conn.execute(select(session_lines.c.labels).where(session_lines.c.session == session)):
                    lab = json.loads(lab) if isinstance(lab, str) else (lab or {})
                    people.append(lab.get("tares_person"))
        return got, people
    finally:
        st.close()


async def main():
    data = tempfile.mkdtemp(prefix="tares-intake-")
    env = clean_env(TARES_HOME=os.path.join(data, "t"), TARES_CATALOG=os.path.join(data, "none.yaml"),
                    TARES_PORT=PORT, TARES_OTLP_GRPC_PORT="off", TARES_AUTH_TOKEN=AUTH, TARES_EDITION="factory")
    log = open(os.path.join(data, "daemon.log"), "w")
    proc = subprocess.Popen([sys.executable, "-c", "from tares.cli import run_daemon; run_daemon()"],
                            env=env, stdout=log, stderr=subprocess.STDOUT)
    B = f"http://127.0.0.1:{PORT}"
    H = {"Authorization": f"Bearer {AUTH}"}
    pid = None
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up", False, open(os.path.join(data, "daemon.log")).read()[-800:])
            return
        async with httpx.AsyncClient(base_url=B, timeout=20) as cx:
            r = await cx.post("/api/keys", headers=H, json={"name": "intake", "scopes": ["ingest"]})
            key = r.json().get("secret", "")
            r = await cx.post("/api/keys", headers=H, json={"name": "reader", "scopes": ["read"]})
            reader = r.json().get("secret", "")
            ck("an ingest key and a read key", bool(key and reader), r.text[:200])
            r = await cx.get("/api/whoami", headers={"Authorization": f"Bearer {key}"})
            who = r.json() if r.status_code == 200 else {}
            person = who.get("person")
            pid = person.get("id") if isinstance(person, dict) else who.get("person_id")
            KH = {"Authorization": f"Bearer {key}", "content-type": "application/x-ndjson"}

            r = await cx.get("/api/sources", headers=H)
            ck("no source catalog route before the first line", r.status_code == 404, r.text[:200])

            print("== Claude Code ==")
            lines = [{"type": "user", "sessionId": "cc-1", "uuid": "u1", "timestamp": NOW, "cwd": "/w/shop",
                      "message": {"role": "user", "content": "add a cart"}},
                     {"type": "assistant", "sessionId": "cc-1", "uuid": "u2", "timestamp": NOW, "cwd": "/w/shop",
                      "message": {"role": "assistant", "model": "claude-opus-5-5",
                                  "content": [{"type": "text", "text": "on it"}]}}]
            r = await cx.post("/ingest/claude_code", headers=KH,
                              content="\n".join(json.dumps(o) for o in lines) + "\n")
            ck("POST /ingest/claude_code answers 202 with the count", r.status_code == 202
               and r.json().get("ingested") == 2, r.text[:200])

            print("== Codex ==")
            fact = {"type": "codex", "kind": "start", "sessionId": "cx-1", "timestamp": NOW, "cwd": "/w/shop",
                    "call": "c1", "turn": "t1", "tool": "Bash", "command": "pytest -q", "command_cwd": "/w/shop"}
            r = await cx.post("/ingest/codex", headers={**KH, "content-type": "application/json"},
                              content=json.dumps([fact]))
            ck("POST /ingest/codex answers 202", r.status_code == 202 and r.json().get("ingested", 0) >= 1,
               r.text[:200])

            print("== credentials ==")
            r = await cx.post("/ingest/claude_code", content=json.dumps(lines[0]))
            ck("no credential: 401", r.status_code == 401, f"{r.status_code} {r.text[:120]}")
            r = await cx.post("/ingest/claude_code", headers={"Authorization": f"Bearer {reader}"},
                              content=json.dumps(lines[0]))
            ck("a read-only key: 403", r.status_code == 403, f"{r.status_code} {r.text[:120]}")

            await asyncio.sleep(1.0)
            r = await cx.get("/api/sources", headers=H)
            ck("still no source catalog route after both intakes", r.status_code == 404, r.text[:200])

            r = await cx.get("/api/factory/settings", headers=H)
            tools = (r.json().get("tools") if r.status_code == 200 else None) or []
            row = next((t for t in tools if isinstance(t, dict) and t.get("key") == "claude_code"), None)
            ck("the Claude Code tool row on Settings says it is reporting",
               bool(row) and row.get("connected") is True and "Reporting" in (row.get("detail") or ""),
               str(row)[:300])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()

    print("== stored ==")
    got, stamped = _stored(os.path.join(data, "t"), "cc-1")
    ck("the Claude Code session is in the sessions table with its 2 lines",
       got.get("cc-1", (0, ""))[0] == 2 and got["cc-1"][1] == "claude_code", str(got))
    ck("the Codex session is stored under claude_code too", got.get("cx-1", (0, ""))[1] == "claude_code", str(got))
    ck("every line carries the person of the key that sent it, as the old intake did",
       len(stamped) == 2 and all(s and (pid is None or s == pid) for s in stamped), f"{stamped} {pid}")

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    asyncio.run(main())
