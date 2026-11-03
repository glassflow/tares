"""A recording carries the person of the key that shipped it (TR-620): the cell sets
`tares_person=<person id>` on every claude_code event pushed to /ingest, from the credential and
never from what the sender wrote. The root token (no person) leaves no label; with auth off it is
`local`.
"""
import asyncio, hashlib, hmac, json, os, subprocess, sys
import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import clean_env

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

AUTH = "root-token"
SECRET = "whsec"
WEBHOOK_LABELS = [{"name": "app", "field": "app", "primary": True}]


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


async def webhook_labels(cx, B, source):
    """The label dicts of every event stored for a webhook source, read back through /read."""
    rd = (await cx.post(f"{B}/read", headers=H(AUTH), json={
        "selector": {"app": "a"}, "sources": [source], "window": "3650d",
        "include_payload": True})).json()
    return [r.get("labels") or {} for r in rd.get("rows", [])]


async def auth_on():
    port = "8871"
    proc = start("/tmp/recording_person_on.duckdb", port, True)
    B = f"http://127.0.0.1:{port}"
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up (auth on)", False); return
        async with httpx.AsyncClient() as cx:
            await cx.post(f"{B}/api/sources", headers=H(AUTH),
                          json={"name": "claude_code", "connector": "claude_code", "config": {}})
            keys, pid = {}, {}
            for who, mail in (("P", "pat@example.com"), ("Q", "quinn@example.com")):
                r = await cx.post(f"{B}/api/keys", headers=H(AUTH), json={
                    "name": f"{who}-laptop", "scopes": ["ingest", "read"],
                    "person": {"email": mail, "name": who}})
                keys[who], pid[who] = r.json()["secret"], r.json()["person"]["id"]
            ck("two people with bound keys", pid["P"] != pid["Q"] and all(keys.values()), str(pid))

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

            # only claude_code records people: other push sources get no label, whoever pushes
            await cx.post(f"{B}/api/sources", headers=H(AUTH), json={
                "name": "plain", "connector": "webhook", "config": {"text_template": "{note}", "labels": WEBHOOK_LABELS}})
            await cx.post(f"{B}/api/sources", headers=H(AUTH), json={
                "name": "signed", "connector": "webhook", "config": {
                    "text_template": "{note}", "labels": WEBHOOK_LABELS, "signature_scheme": "hmac_sha256",
                    "signing_secret": SECRET, "signature_header": "X-Signature"}})
            r = await cx.post(f"{B}/ingest/plain", headers=H(keys["P"]),
                              json={"app": "a", "note": "plain-from-p", "tares_person": pid["Q"]})
            ck("P's key ingests to a webhook source", r.status_code == 202, r.text)
            lw = await webhook_labels(cx, B, "plain")
            ck("a webhook source: a person-bound key leaves no tares_person label",
               len(lw) == 1 and all("tares_person" not in l for l in lw), str(lw))
            body = json.dumps({"app": "a", "note": "signed-no-key"}).encode()
            sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
            r = await cx.post(f"{B}/ingest/signed", content=body, headers={
                "content-type": "application/json", "X-Signature": sig})
            ck("a signed webhook (auth on, no key) ingests", r.status_code == 202, r.text)
            lw = await webhook_labels(cx, B, "signed")
            ck("a signed webhook: no tares_person label",
               len(lw) == 1 and all("tares_person" not in l for l in lw), str(lw))

            # the label is filterable like any other: a read by tares_person
            rd = (await cx.post(f"{B}/read", headers=H(AUTH), json={
                "selector": {"tares_person": pid["P"]}, "window": "3650d"})).json()
            ck("a read by tares_person=P finds P's two lines and nothing else",
               rd.get("count") == 2, str(rd)[:300])
    finally:
        proc.terminate(); proc.wait(timeout=10)


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
    await auth_on()
    await auth_off()
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())
