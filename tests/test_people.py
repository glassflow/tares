"""People on the cell (TR-618): a list of people, keys that belong to one, and a credential that
names the person calling. Root token = the system, no person; a key = its person (an unbound key is
`local` while `local` is alone on the cell); auth off = `local`. Also the upgrade of a database that
has keys but no people. Everything that needs a second person is in ee/tests/test_people_team.py.
"""
import asyncio, os, subprocess, sys
import duckdb
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


async def auth_one_person():
    port = "8861"
    proc = start("/tmp/people_on.duckdb", port, True)
    B = f"http://127.0.0.1:{port}"
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up (auth on)", False); return
        async with httpx.AsyncClient() as cx:
            # ── the system credential is not a person ──
            w = (await cx.get(f"{B}/api/whoami", headers=H(AUTH))).json()
            ck("root token: kind system, person null", w.get("kind") == "system"
               and w.get("person") is None, str(w))
            people = (await cx.get(f"{B}/api/people?limit=100", headers=H(AUTH))).json()["people"]
            ck("a fresh cell has only `local`", [p["id"] for p in people] == ["local"], str(people))

            # ── a key with no person acts as `local` while `local` is alone ──
            r = await cx.post(f"{B}/api/keys", json={"name": "old", "scopes": ["admin"]},
                              headers=H(AUTH))
            ck("create unbound key", r.status_code == 201 and r.json()["person"] is None, r.text)
            old_key, old_id = r.json()["secret"], r.json()["id"]
            w = (await cx.get(f"{B}/api/whoami", headers=H(old_key))).json()
            ck("unbound key, only `local`: person local", w["kind"] == "key"
               and (w["person"] or {}).get("id") == "local", str(w))

            # ── on a one-person cell, an unbound read or ingest key is not `local` for edits ──
            for sc in ("read", "ingest"):
                r = await cx.post(f"{B}/api/keys", json={"name": f"u-{sc}", "scopes": [sc]},
                                  headers=H(AUTH))
                r = await cx.patch(f"{B}/api/people/local", headers=H(r.json()["secret"]),
                                   json={"name": "Hacked"})
                ck(f"an unbound {sc} key cannot rename `local` -> 403", r.status_code == 403, r.text)
            ck("... and `local` is untouched",
               (await cx.get(f"{B}/api/people?limit=100", headers=H(AUTH))).json()["people"][0]["name"] == "You")
    finally:
        proc.terminate()
        proc.wait(timeout=10)


async def auth_off():
    port = "8862"
    proc = start("/tmp/people_off.duckdb", port, False)
    B = f"http://127.0.0.1:{port}"
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up (auth off)", False); return
        async with httpx.AsyncClient() as cx:
            w = (await cx.get(f"{B}/api/whoami")).json()
            ck("auth off: kind open, person local", w.get("kind") == "open"
               and (w.get("person") or {}).get("id") == "local", str(w))
            people = (await cx.get(f"{B}/api/people?limit=100")).json()["people"]
            ck("auth off: `local` is listed, an admin",
               people and people[0]["id"] == "local" and people[0]["is_admin"], str(people))
            r = await cx.patch(f"{B}/api/people/local", json={"name": "Ashish"})
            ck("auth off: anyone may rename `local`", r.status_code == 200 and r.json()["name"] == "Ashish", r.text)
            w = (await cx.get(f"{B}/api/whoami")).json()
            ck("whoami follows the rename", w["person"]["name"] == "Ashish", str(w))
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def upgrade():
    """A database from before people: its keys survive, `local` is made, person_id is added."""
    from tares.store import Store
    db = "/tmp/people_old.duckdb"
    for p in (db, db + ".wal"):
        if os.path.exists(p):
            os.remove(p)
    con = duckdb.connect(db)
    con.execute("CREATE TABLE api_keys (id TEXT PRIMARY KEY, name TEXT, prefix TEXT, hash TEXT, "
                "scopes JSON, created_at TIMESTAMPTZ, last_used_at TIMESTAMPTZ, revoked_at TIMESTAMPTZ)")
    con.execute("INSERT INTO api_keys VALUES ('k1', 'legacy', 'nvf_k1', 'h', '[\"read\"]', now(), NULL, NULL)")
    con.close()
    s = Store(db)
    ck("upgrade: `local` is made", [p["id"] for p in s.list_people()] == ["local"])
    k = s.get_api_key("k1")
    ck("upgrade: the old key is kept, bound to nobody", k and k["person_id"] is None, str(k))
    s.con.close()
    s = Store(db)
    ck("upgrade: reopening adds no second `local`", len(s.list_people()) == 1)
    s.con.close()


def threads():
    """Several threads create one person at once (the store, no HTTP): one row, no crash."""
    import threading
    from tares.store import Store
    db = "/tmp/people_race.duckdb"
    for p in (db, db + ".wal"):
        if os.path.exists(p):
            os.remove(p)
    s = Store(db)

    def race(fn):
        out = []
        def run(i):
            try:
                out.append(("ok", fn(i)))
            except ValueError as e:
                out.append(("clash", str(e)))
            except Exception as e:   # anything else is the 500 this guards against
                out.append(("crash", repr(e)))
        ts = [threading.Thread(target=run, args=(i,)) for i in range(8)]
        [t.start() for t in ts]; [t.join() for t in ts]
        return out

    out = race(lambda i: s.create_person(f"T{i}", email="t@example.com"))
    ck("8 threads create one email: one wins, 7 clash, none crash",
       sorted(k for k, _ in out) == ["clash"] * 7 + ["ok"], str(out))
    ck("... one row", sum(1 for p in s.list_people() if p["email"] == "t@example.com") == 1)
    out = race(lambda i: s.person_or_create("u@example.com", f"U{i}")["id"])
    ck("8 threads person_or_create one email: all get the same person",
       {k for k, _ in out} == {"ok"} and len({v for _, v in out}) == 1, str(out))
    ck("... one row", sum(1 for p in s.list_people() if p["email"] == "u@example.com") == 1)
    s.con.close()


asyncio.run(auth_one_person())
asyncio.run(auth_off())
upgrade()
threads()
print(f"\n{P} passed, {F} failed")
raise SystemExit(1 if F else 0)
