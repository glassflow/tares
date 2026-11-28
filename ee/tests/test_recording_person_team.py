# Tares enterprise licence, see ee/LICENSE
"""A recording carries the person of the key that shipped it (TR-620), with two people on the cell:
the cell sets `tares_person=<person id>` on every claude_code line pushed to /ingest/claude_code, from
the credential and never from what the sender wrote, so a line that names the other person still
carries the key's own. The root token (no person) leaves no label. Runs on a licensed daemon; the
auth-off half is tests/test_recording_person.py.

Run: .venv/bin/python ee/tests/test_recording_person_team.py
"""
import asyncio, json, os, subprocess, sys
import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # puts the repo and tests/ on sys.path
from _env import clean_env, remove_db

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


def start(db, port):
    remove_db(db)
    env = {**clean_env(), "TARES_HOME": db, "TARES_PORT": port, "TARES_OTLP_GRPC_PORT": "off",
           "TARES_AUTH_TOKEN": AUTH, "TARES_LICENSE": licensed.test_licence()}
    return subprocess.Popen(licensed.daemon_argv(), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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
    """The tares_person label of each stored line with `text` in `session`, read back through the
    project's session route (the session is tied to the project `Rec` by its first line): the id, or
    None when the line has no such label."""
    hd = H(tok) if tok else {}
    uid = next(p["id"] for p in (await cx.get(f"{B}/api/projects", headers=hd)).json()["projects"]
               if p["name"] == "Rec")
    rd = (await cx.get(f"{B}/api/projects/{uid}/sessions/{session}", headers=hd,
                       params={"limit": 1000})).json()
    return [(r.get("labels") or {}).get("tares_person") for r in rd.get("lines", [])
            if (r.get("text") or "") == text]


def tie(sid):
    """The line that ties session `sid` to the project `Rec`."""
    return json.dumps({"type": "session_project", "sessionId": sid, "cwd": "/tmp/proj",
                       "timestamp": "2026-10-21T09:59:00Z", "tares_project": "Rec"}) + "\n"


async def auth_on():
    port = "8871"
    proc = start("/tmp/recording_person_team_on", port)
    B = f"http://127.0.0.1:{port}"
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up (auth on)", False); return
        async with httpx.AsyncClient() as cx:
            keys, pid = {}, {}
            for who, mail in (("P", "pat@example.com"), ("Q", "quinn@example.com")):
                r = await cx.post(f"{B}/api/keys", headers=H(AUTH), json={
                    "name": f"{who}-laptop", "scopes": ["ingest", "read"],
                    "person": {"email": mail, "name": who}})
                keys[who], pid[who] = r.json()["secret"], r.json()["person"]["id"]
            ck("two people with bound keys", pid["P"] != pid["Q"] and all(keys.values()), str(pid))
            r = await cx.post(f"{B}/api/projects", headers=H(AUTH), json={"template": "custom", "name": "Rec"})
            ck("a project to tie the sessions to", r.status_code == 201, r.text)
            for sid in ("sp", "sq", "sr"):
                r = await push(cx, B, AUTH, tie(sid))
                ck(f"session {sid} is tied to it", r.status_code == 202, r.text)

            # a line shipped with P's key carries P, whatever the line says
            r = await push(cx, B, keys["P"], line("sp", "from-p-plain") + "\n")
            ck("P's key ingests", r.status_code == 202, r.text)
            r = await push(cx, B, keys["P"], line("sp", "from-p-spoof", spoof=pid["Q"]) + "\n",
                           **{"x-tares-person": pid["Q"]})
            ck("P's key ingests a line that names Q", r.status_code == 202, r.text)
            # a batch (two lines in one body) from Q
            r = await push(cx, B, keys["Q"], line("sq", "from-q-one") + "\n"
                           + line("sq", "from-q-two", spoof=pid["P"]) + "\n")
            ck("a batch from Q's key ingests both lines", r.status_code == 202
               and r.json()["ingested"] == 2, r.text)
            # the root token: a system credential, no person; the client still tries to name one
            r = await push(cx, B, AUTH, line("sr", "from-root") + "\n"
                           + line("sr", "from-root-spoof", spoof=pid["Q"]) + "\n")
            ck("the root token ingests", r.status_code == 202, r.text)

            lp = await persons(cx, B, AUTH, "sp", "from-p-plain")
            ck("the stored event has tares_person = P", lp == [pid["P"]], str(lp))
            lp = await persons(cx, B, AUTH, "sp", "from-p-spoof")
            ck("P's key, line says Q: the stored label is P, not Q", lp == [pid["P"]], str(lp))
            l1 = await persons(cx, B, AUTH, "sq", "from-q-one")
            l2 = await persons(cx, B, AUTH, "sq", "from-q-two")
            ck("a batch from Q: every line is Q (also the one naming P)",
               l1 == [pid["Q"]] and l2 == [pid["Q"]], f"{l1} {l2}")
            lr = await persons(cx, B, AUTH, "sr", "from-root")
            ck("root token: no tares_person label", lr == [None], str(lr))
            lr = await persons(cx, B, AUTH, "sr", "from-root-spoof")
            ck("root token, line says Q: still no label", lr == [None], str(lr))

    finally:
        proc.terminate(); proc.wait(timeout=10)


async def main():
    await auth_on()
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())
