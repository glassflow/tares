"""Crew settings, AGENTS.md and memory per person (TR-624). Each person has their own crew
settings over the cell's default, and a personal AGENTS.md and memory that only they see. With auth
off the one person is `local` and the cell keeps one set of settings, as before.
"""
import asyncio, json, os, subprocess, sys
import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import clean_env
from tares import docs as docs_mod
import tares.mcp_server as m

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


async def auth_on():
    port = "8875"
    proc = start("/tmp/personal_docs_on.duckdb", port, True)
    B = f"http://127.0.0.1:{port}"
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up (auth on)", False); return
        async with httpx.AsyncClient() as cx:
            async def key(name, email, scopes):
                body = {"name": name, "scopes": scopes}
                if email:
                    body["person"] = {"email": email, "name": name}
                r = await cx.post(f"{B}/api/keys", json=body, headers=H(AUTH))
                return r.json()["secret"]

            a = await key("Person A", "person-a@example.test", ["admin"])
            b = await key("Person B", "person-b@example.test", ["admin"])
            c = await key("Person C", "person-c@example.test", ["admin"])
            a_read = await key("Person A", "person-a@example.test", ["read"])
            nobody = await key("no person", None, ["admin"])

            # ── personal docs ──
            r = await cx.post(f"{B}/api/docs/personal/agents/lines", headers=H(a),
                              json={"text": "rule one"})
            ck("A writes a personal rule", r.status_code == 200 and r.json().get("added") is True,
               r.text)
            r = await cx.post(f"{B}/api/docs/personal/memory/lines", headers=H(a),
                              json={"text": "memory one"})
            ck("A writes a personal memory line", r.status_code == 200, r.text)
            r = await cx.post(f"{B}/api/docs/personal/agents/lines", headers=H(b),
                              json={"text": "rule two"})
            ck("B writes a personal rule", r.status_code == 200, r.text)
            r = await cx.post(f"{B}/api/docs/personal/agents/lines", headers=H(a),
                              json={"text": "rule one"})
            ck("the same line twice is not added twice", r.json().get("added") is False, r.text)

            ga = (await cx.get(f"{B}/api/docs/global", headers=H(a))).json()
            gb = (await cx.get(f"{B}/api/docs/global", headers=H(b))).json()
            ck("A reads A's rule", "rule one" in ga["personal"]["agents"]["body"], str(ga))
            ck("A does not read B's rule", "rule two" not in str(ga), str(ga))
            ck("B reads B's rule and not A's",
               "rule two" in gb["personal"]["agents"]["body"] and "rule one" not in str(gb)
               and "memory one" not in str(gb), str(gb))
            ck("B has no memory yet", gb["personal"]["memory"] is None, str(gb))
            gp = (await cx.get(f"{B}/api/docs/personal", headers=H(b))).json()
            ck("/api/docs/personal is the caller's own",
               "rule two" in gp["agents"]["body"] and "rule one" not in str(gp), str(gp))

            text_a = docs_mod.read_all_text(ga)
            ck("A's read has the rules section, with the team-wins line first",
               "# Your rules (Person A)" in text_a
               and docs_mod.PERSONAL_CONFLICT in text_a.split("# Your rules (Person A)")[1]
               .split("rule one")[0], text_a)
            ck("... then the memory section after the rules",
               text_a.index("# Your rules") < text_a.index("# Your memory (Person A)")
               and "memory one" in text_a, text_a)
            ck("... and nothing of B's", "rule two" not in text_a, text_a)
            gc = (await cx.get(f"{B}/api/docs/global", headers=H(c))).json()
            ck("a person with no personal docs gets no personal sections",
               "# Your" not in docs_mod.read_all_text(gc), str(gc))

            # ── the docs listing ──
            def mine(rows):
                return sorted(d["kind"] for d in rows if d["kind"].startswith("personal_"))
            la = (await cx.get(f"{B}/api/docs", headers=H(a))).json()
            lb = (await cx.get(f"{B}/api/docs", headers=H(b))).json()
            lroot = (await cx.get(f"{B}/api/docs", headers=H(AUTH))).json()
            ck("A lists their two personal docs", mine(la) == ["personal_agents", "personal_memory"],
               str(mine(la)))
            ck("B lists only their one", mine(lb) == ["personal_agents"], str(mine(lb)))
            ck("A's personal docs are not in B's list",
               not {d["id"] for d in la if d["kind"].startswith("personal_")}
               & {d["id"] for d in lb}, "")
            ck("the system credential lists none of them", mine(lroot) == [], str(mine(lroot)))

            # ── who may write ──
            r = await cx.post(f"{B}/api/docs/personal/agents/lines", headers=H(nobody),
                              json={"text": "rule three"})
            ck("a key with no person cannot write a personal doc -> 403", r.status_code == 403, r.text)
            r = await cx.post(f"{B}/api/docs/personal/agents/lines", headers=H(a_read),
                              json={"text": "rule three"})
            ck("a read key cannot write one -> 403", r.status_code == 403, r.text)
            r = await cx.post(f"{B}/api/docs/personal/nothing/lines", headers=H(a),
                              json={"text": "rule three"})
            ck("an unknown personal doc -> 404", r.status_code == 404, r.text)

            # ── crew settings ──
            base = (await cx.get(f"{B}/api/crew/settings", headers=H(AUTH))).json()
            r = await cx.put(f"{B}/api/crew/settings", headers=H(a), json={"builders_max": 7})
            ck("A saves their own settings", r.status_code == 200 and r.json()["builders_max"] == 7,
               r.text)
            sa = (await cx.get(f"{B}/api/crew/settings", headers=H(a))).json()
            sb = (await cx.get(f"{B}/api/crew/settings", headers=H(b))).json()
            sroot = (await cx.get(f"{B}/api/crew/settings", headers=H(AUTH))).json()
            ck("A reads their own", sa["builders_max"] == 7, str(sa))
            ck("B's settings did not change", sb["builders_max"] == base["builders_max"], str(sb))
            ck("the cell's default did not change", sroot["builders_max"] == base["builders_max"],
               str(sroot))
            r = await cx.put(f"{B}/api/crew/settings", headers=H(AUTH), json={"autonomy": "L4-ship"})
            ck("the system credential changes the cell's default", r.status_code == 200, r.text)
            sa = (await cx.get(f"{B}/api/crew/settings", headers=H(a))).json()
            sb = (await cx.get(f"{B}/api/crew/settings", headers=H(b))).json()
            ck("B now reads the new default", sb["autonomy"] == "L4-ship", str(sb))
            ck("A keeps their own value and reads the new default for the rest",
               sa["builders_max"] == 7 and sa["autonomy"] == "L4-ship", str(sa))
            r = await cx.put(f"{B}/api/crew/settings", headers=H(nobody), json={"builders_max": 2})
            ck("a key with no person cannot save settings -> 403",
               r.status_code == 403 and "no person" in r.text, r.text)
            r = await cx.put(f"{B}/api/crew/settings", headers=H(a_read), json={"builders_max": 2})
            ck("a read key cannot save settings -> 403", r.status_code == 403, r.text)
            sa = (await cx.get(f"{B}/api/crew/settings", headers=H(a))).json()
            ck("... and A's settings are as they were", sa["builders_max"] == 7, str(sa))

            # ── a person key with ingest only, no admin (the scope rule for the two writes) ──
            a_ing = await key("Person A ingest", "person-a@example.test", ["read", "ingest"])
            c_ing = await key("Person C ingest", "person-c@example.test", ["read", "ingest"])
            default_before = (await cx.get(f"{B}/api/crew/settings", headers=H(AUTH))).json()
            sb_before = (await cx.get(f"{B}/api/crew/settings", headers=H(b))).json()
            r = await cx.put(f"{B}/api/crew/settings", headers=H(a_ing), json={"builders_max": 5})
            ck("an ingest-only person key saves its own settings -> 200",
               r.status_code == 200 and r.json()["builders_max"] == 5, r.text)
            default_after = (await cx.get(f"{B}/api/crew/settings", headers=H(AUTH))).json()
            sb_after = (await cx.get(f"{B}/api/crew/settings", headers=H(b))).json()
            ck("... the cell's default did not change",
               default_after["builders_max"] == default_before["builders_max"], str(default_after))
            ck("... and another person's settings did not change",
               sb_after["builders_max"] == sb_before["builders_max"], str(sb_after))
            ck("... A reads the new value", (await cx.get(f"{B}/api/crew/settings",
               headers=H(a))).json()["builders_max"] == 5, "")
            r = await cx.post(f"{B}/api/docs/personal/agents/lines", headers=H(c_ing),
                              json={"text": "rule from C"})
            ck("an ingest-only person key writes its own personal rule -> 200",
               r.status_code == 200, r.text)
            ga = (await cx.get(f"{B}/api/docs/global", headers=H(a))).json()
            gc = (await cx.get(f"{B}/api/docs/global", headers=H(c))).json()
            ck("... it is C's and not A's", "rule from C" in gc["personal"]["agents"]["body"]
               and "rule from C" not in str(ga), str(gc))
            r = await cx.put(f"{B}/api/crew/settings", headers=H(c_ing), json={"autonomy": "L3-review"})
            ck("... and it still saves C's own settings", r.status_code == 200, r.text)
            ck("... without changing the default", (await cx.get(f"{B}/api/crew/settings",
               headers=H(AUTH))).json()["autonomy"] == default_before["autonomy"], "")
            r = await cx.get(f"{B}/api/crew/settings", headers=H(nobody))
            ck("a key with no person reads the default", r.status_code == 200
               and r.json()["builders_max"] == default_before["builders_max"], r.text)

            # ── MCP add_to_global with a scope ──
            real_cx, real_url = m._cx, m.TARESD
            try:
                m.TARESD = B
                team_before = (await cx.get(f"{B}/api/docs/global", headers=H(AUTH))).json()
                # an admin person: ignoring the scope would post the line to the team doc
                m._cx = lambda timeout=10: httpx.AsyncClient(timeout=timeout, headers=H(a))
                raw = await m.add_to_global("agents", "mcp personal rule", scope="personal")
                try:
                    out = json.loads(raw)
                except ValueError:
                    out = {"raw": raw}
                ck("MCP add_to_global(scope=personal) adds to the caller's own doc",
                   out.get("added") is True, raw)
                # an ingest-only person: the same call must work for them too
                m._cx = lambda timeout=10: httpx.AsyncClient(timeout=timeout, headers=H(a_ing))
                raw = await m.add_to_global("memory", "mcp personal fact", scope="personal")
                try:
                    out = json.loads(raw)
                except ValueError:
                    out = {"raw": raw}
                ck("... and for a person key without admin", out.get("added") is True, raw)
                ga = (await cx.get(f"{B}/api/docs/global", headers=H(a))).json()
                gb = (await cx.get(f"{B}/api/docs/global", headers=H(b))).json()
                team_after = (await cx.get(f"{B}/api/docs/global", headers=H(AUTH))).json()
                ck("... the lines are in A's personal docs",
                   "mcp personal rule" in ga["personal"]["agents"]["body"]
                   and "mcp personal fact" in ga["personal"]["memory"]["body"], str(ga))
                ck("... not in the team docs", "mcp personal" not in str(team_after)
                   and team_after == team_before, str(team_after))
                ck("... and not in B's", "mcp personal" not in str(gb), str(gb))
                bad = await m.add_to_global("agents", "x", scope="everyone")
                ck("a bad scope returns the error string", "team" in bad and "personal" in bad
                   and not bad.startswith("{"), bad)
            finally:
                m._cx, m.TARESD = real_cx, real_url

            # ── a personal doc cannot be attached to a project ──
            admin_a = await key("Person A admin", "person-a@example.test", ["admin"])
            p = (await cx.post(f"{B}/api/projects", headers=H(admin_a), json={
                "template": "custom", "name": "Attach", "objects": []})).json()["id"]
            pid = (await cx.get(f"{B}/api/docs/personal", headers=H(a))).json()["agents"]["id"]
            r = await cx.post(f"{B}/api/projects/{p}/docs/{pid}/use", headers=H(admin_a))
            ck("attaching a personal doc to a project -> 404", r.status_code == 404, r.text)
            r = await cx.get(f"{B}/api/projects/{p}/docs/{pid}", headers=H(admin_a))
            ck("... and the project does not serve it", r.status_code == 404, r.text)
    finally:
        proc.terminate(); proc.wait()


async def auth_off():
    port = "8876"
    proc = start("/tmp/personal_docs_off.duckdb", port, False)
    B = f"http://127.0.0.1:{port}"
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up (auth off)", False); return
        async with httpx.AsyncClient() as cx:
            base = (await cx.get(f"{B}/api/crew/settings")).json()
            r = await cx.put(f"{B}/api/crew/settings", json={"builders_max": 4})
            ck("auth off: settings save as the cell's", r.status_code == 200
               and r.json()["builders_max"] == 4, r.text)
            s = (await cx.get(f"{B}/api/crew/settings")).json()
            ck("auth off: the read returns them", s["builders_max"] == 4
               and s["autonomy"] == base["autonomy"], str(s))
            r = await cx.post(f"{B}/api/docs/personal/agents/lines", json={"text": "rule one"})
            ck("auth off: the one person writes a personal rule", r.status_code == 200, r.text)
            g = (await cx.get(f"{B}/api/docs/global")).json()
            ck("auth off: and reads it", "rule one" in g["personal"]["agents"]["body"], str(g))
    finally:
        proc.terminate(); proc.wait()


async def auth_on_solo():
    """Only `local` exists: a key with no person is `local`. Its admin scope changes the cell's
    default; a key without the admin scope does not."""
    port = "8877"
    proc = start("/tmp/personal_docs_solo.duckdb", port, True)
    B = f"http://127.0.0.1:{port}"
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up (auth on, one person)", False); return
        async with httpx.AsyncClient() as cx:
            async def key(name, scopes):
                r = await cx.post(f"{B}/api/keys", json={"name": name, "scopes": scopes},
                                  headers=H(AUTH))
                return r.json()["secret"]

            admin = await key("admin key", ["admin"])
            ingest = await key("ingest key", ["ingest"])
            r = await cx.put(f"{B}/api/crew/settings", headers=H(ingest), json={"builders_max": 9})
            ck("solo: a key without the admin scope cannot change the cell's default -> 403",
               r.status_code == 403, r.text)
            s = (await cx.get(f"{B}/api/crew/settings", headers=H(AUTH))).json()
            ck("solo: ... and the default is as it was", s["builders_max"] != 9, str(s))
            r = await cx.put(f"{B}/api/crew/settings", headers=H(admin), json={"builders_max": 9})
            ck("solo: the admin key changes the cell's default", r.status_code == 200
               and r.json()["builders_max"] == 9, r.text)
            s = (await cx.get(f"{B}/api/crew/settings", headers=H(AUTH))).json()
            ck("solo: the system credential reads it", s["builders_max"] == 9, str(s))
    finally:
        proc.terminate(); proc.wait()


async def main():
    await auth_on()
    await auth_on_solo()
    await auth_off()


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
