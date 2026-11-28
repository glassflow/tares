# Tares enterprise licence, see ee/LICENSE
"""Writes on a private project's objects answer as if the object were not there (TR-944).

A person who is not on a private project, even with an admin-scope key, must not decide anything
that belongs only to that project. A rule proposal of the project gets the same 404, with the same
body, as one that does not exist. Ann leads the project and keeps access. (The trigger, agent, MCP
server and subscription routes went in TR-1810, the source routes, the guided setup and a project's
objects with the source catalog in TR-1811: what is left is the rule proposals.)

Run: .venv/bin/python ee/tests/test_private_writes.py
"""
import asyncio, os, shutil, signal, socket, subprocess, sys, tempfile

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # puts the repo and tests/ on sys.path
from _env import clean_env
from tares.envelope import now_utc
from tares.store import Store

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

AUTH = "root-token"
TMP = tempfile.mkdtemp(prefix="private-writes-")
DB = os.path.join(TMP, "cell")


def free_port() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return str(s.getsockname()[1])


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def spawn(port):
    env = {**clean_env(), "TARES_HOME": DB, "TARES_PORT": port, "TARES_OTLP_GRPC_PORT": "off",
           "TARES_AUTH_TOKEN": AUTH, "TARES_LICENSE": licensed.test_licence()}
    return subprocess.Popen(licensed.daemon_argv(),
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop(proc):
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
        proc.wait()


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


async def setup(B, ids):
    ROOT = H(AUTH)
    async with httpx.AsyncClient(timeout=15) as cx:
        async def person(name, email):
            return (await cx.post(f"{B}/api/people", headers=ROOT,
                                  json={"name": name, "email": email})).json()["id"]

        async def key(pid):
            r = await cx.post(f"{B}/api/keys", headers=ROOT,
                              json={"name": f"k-{pid}", "scopes": ["admin"], "person_id": pid})
            return r.json()["secret"]

        ann, kim = await person("Ann", "ann@example.org"), await person("Kim", "kim@example.org")
        ids["ann_key"], ids["kim_key"] = await key(ann), await key(kim)
        secret = (await cx.post(f"{B}/api/projects", headers=H(ids["ann_key"]), json={
            "template": "custom", "name": "Secret", "objects": []})).json()["id"]
        team = (await cx.post(f"{B}/api/projects", headers=H(ids["ann_key"]), json={
            "template": "custom", "name": "Open", "objects": []})).json()["id"]
        r = await cx.patch(f"{B}/api/projects/{secret}", headers=H(ids["ann_key"]),
                           json={"scope": "private"})
        ck("set up: Ann leads a private project, Kim is outside it",
           r.status_code == 200 and r.json()["scope"] == "private", r.text[:160])
        ids["secret"], ids["team"] = secret, team


def seed(ids):
    """A rule proposal of the private project and one of the team project; with the daemon stopped."""
    store = Store(DB)
    secret, team = ids["secret"], ids["team"]
    now = now_utc()
    ids["rule_secret"] = store.upsert_rule_proposal("example-org/repo-a", "secret-kind", secret,
                                                    "project", "Rule A", ["Secret T1"], now)["id"]
    ids["rule_open"] = store.upsert_rule_proposal("example-org/repo-b", "open-kind", team,
                                                  "project", "Rule B", ["Open T1"], now)["id"]
    store.close()


def shape(text, name):
    """A response body with the name asked about taken out, so two names compare equal."""
    return text.replace(name, "<name>")


async def checks(B, ids):
    ROOT, ANN, KIM = H(AUTH), H(ids["ann_key"]), H(ids["kim_key"])
    async with httpx.AsyncClient(timeout=30) as cx:
        print("== a rule proposal ==")
        for decision in ("reject", "accept"):
            r = await cx.post(f"{B}/api/rule-proposals/{ids['rule_secret']}/{decision}",
                              headers=KIM, json={})
            gone = await cx.post(f"{B}/api/rule-proposals/rp_nobody/{decision}", headers=KIM, json={})
            ck(f"{decision} a hidden proposal: as unknown as one that does not exist",
               r.status_code == gone.status_code == 404
               and shape(r.text, ids["rule_secret"]) == shape(gone.text, "rp_nobody"),
               f"{r.status_code} {r.text[:100]} / {gone.status_code} {gone.text[:100]}")

        sec = (await cx.get(f"{B}/api/rule-proposals", headers=ROOT)).json()["proposals"]
        ck("the hidden proposal is still open", any(p["id"] == ids["rule_secret"] and p["state"] == "open"
                                                     for p in sec), str(sec)[:200])

        print("== Ann keeps access ==")
        r = await cx.post(f"{B}/api/rule-proposals/{ids['rule_secret']}/reject", headers=ANN, json={})
        ck("Ann: reject the hidden proposal", r.status_code == 200, f"{r.status_code} {r.text[:100]}")


async def main():
    ids = {}
    port = free_port()
    B = f"http://127.0.0.1:{port}"
    proc = spawn(port)
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up", False); return
        await setup(B, ids)
    finally:
        stop(proc)
    seed(ids)
    proc = spawn(port)
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up (second start)", False); return
        await checks(B, ids)
    finally:
        stop(proc)
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    asyncio.run(main())
