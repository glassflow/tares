"""Area owners (TR-627): the cell knows which person owns each GitHub repo. An admin sets any
repo; a person may claim an unowned repo for themselves; matching ignores case.

Run: .venv/bin/python tests/test_areas.py
"""
import asyncio, json, logging, os, shutil, socket, subprocess, sys, tempfile
import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import clean_env
import tares.mcp_server as m
import tares.mcp_factory as mf

logging.getLogger("httpx").setLevel(logging.WARNING)   # the MCP import turns request logs on

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

AUTH = "root-token"
TMP = tempfile.mkdtemp(prefix="areas-")


def free_port() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return str(s.getsockname()[1])


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


def start(name, auth):
    db, port = os.path.join(TMP, name + ".duckdb"), free_port()
    env = {**clean_env(), "TARES_DB": db, "TARES_PORT": port, "TARES_OTLP_GRPC_PORT": "off"}
    if auth:
        env["TARES_AUTH_TOKEN"] = AUTH
    proc = subprocess.Popen([sys.executable, "-c", "from tares.cli import run_daemon; run_daemon()"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc, f"http://127.0.0.1:{port}"


def owners(rows):
    return {r["repo"]: r["person"]["id"] for r in rows}


def unit():
    print("== the store helper ==")
    from tares import areas
    from tares.store import Store
    s = Store(os.path.join(TMP, "unit.duckdb"))
    kiran = s.create_person("Kiran", "kiran@x.dev")
    ck("no areas: nobody owns a repo", areas.owner(s, "glassflow/tares") is None)
    areas.save(s, [{"repo": "GlassFlow/Tares-Cloud", "person_id": kiran["id"], "note": "infra"}])
    ck("saved in lowercase", [e["repo"] for e in areas.load(s)] == ["glassflow/tares-cloud"])
    ck("area_owner ignores case", (areas.owner(s, "Glassflow/Tares-Cloud") or {}).get("id") == kiran["id"])
    ck("... and padding", (areas.owner(s, "  glassflow/tares-cloud ") or {}).get("id") == kiran["id"])
    ck("a repo with no area has no owner", areas.owner(s, "glassflow/other") is None)
    ck("not a repo: no owner, no crash", areas.owner(s, "nonsense") is None and areas.owner(s, None) is None)
    areas.save(s, [{"repo": "a/b", "person_id": "local"}, {"repo": "A/B", "person_id": kiran["id"]}])
    ck("a repo given twice keeps the last, once", [(e["repo"], e["person_id"]) for e in areas.load(s)]
       == [("a/b", kiran["id"])], str(areas.load(s)))
    for bad in ({"repo": "x", "person_id": "local"}, {"repo": "a/b/c", "person_id": "local"},
                {"repo": "a/b", "person_id": "ghost"}):
        try:
            areas.save(s, [bad]); ok = False
        except ValueError:
            ok = True
        ck(f"save refuses {bad}", ok)
    s.con.close()


async def auth_on():
    proc, B = start("on", True)
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up (auth on)", False); return
        ROOT = H(AUTH)
        async with httpx.AsyncClient(timeout=30) as cx:
            async def person(name, email):
                r = await cx.post(f"{B}/api/people", headers=ROOT, json={"name": name, "email": email})
                return r.json()["id"]

            async def key(pid, scope):
                r = await cx.post(f"{B}/api/keys", headers=ROOT,
                                  json={"name": f"k-{pid}-{scope}", "scopes": scope.split("+"), "person_id": pid})
                return H(r.json()["secret"])

            kiran, jo = await person("Kiran", "kiran@x.dev"), await person("Jo", "jo@x.dev")
            J_ = await key(jo, "read+ingest")                                  # a person's own key (no admin)
            JR_, KA_ = await key(jo, "read"), await key(kiran, "admin")        # read-only; admin
            NOBODY = await cx.post(f"{B}/api/keys", headers=ROOT,
                                   json={"name": "nobody", "scopes": ["ingest"]})
            NOBODY = H(NOBODY.json()["secret"])

            print("== an admin sets and reads ==")
            r = await cx.get(f"{B}/api/areas", headers=JR_)
            ck("empty to start; a read key can read", r.status_code == 200 and r.json() == [], r.text)
            r = await cx.put(f"{B}/api/areas/glassflow/tares-cloud", headers=ROOT,
                             json={"person_id": kiran, "note": "infra"})
            ck("root sets one", r.status_code == 200 and r.json() == {
                "repo": "glassflow/tares-cloud", "person": {"id": kiran, "name": "Kiran"},
                "note": "infra"}, r.text)
            r = await cx.get(f"{B}/api/areas", headers=JR_)
            ck("GET lists it", r.json() and r.json()[0]["person"]["name"] == "Kiran", r.text)
            r = await cx.put(f"{B}/api/areas/GlassFlow/Tares-Cloud", headers=KA_, json={"person_id": jo})
            ck("an admin key sets it again in another case: still one entry, now Jo's",
               r.status_code == 200 and owners((await cx.get(f"{B}/api/areas", headers=JR_)).json())
               == {"glassflow/tares-cloud": jo}, r.text)
            ck("... a reassign without a note keeps the note the repo had",
               r.json().get("note") == "infra", r.text)
            r = await cx.put(f"{B}/api/areas/glassflow/tares-cloud", headers=ROOT,
                             json={"person_id": jo, "note": "billing"})
            ck("... a note in the body replaces it", r.json().get("note") == "billing", r.text)
            r = await cx.put(f"{B}/api/areas/glassflow/tares-cloud", headers=ROOT,
                             json={"person_id": jo, "note": None})
            ck("... and a null note clears it", r.status_code == 200 and r.json().get("note") == "",
               r.text)
            r = await cx.put(f"{B}/api/areas/glassflow/tares-cloud", headers=ROOT, json={"person_id": "ghost"})
            ck("unknown person_id: 400", r.status_code == 400, r.text)
            r = await cx.put(f"{B}/api/areas/glassflow/tares-cloud", headers=ROOT, json={})
            ck("no person_id: 400", r.status_code == 400, r.text)
            r = await cx.put(f"{B}/api/areas/glassflow/a%2Fb/c", headers=ROOT, json={"person_id": jo})
            ck("a path that is not owner/name does not set anything", r.status_code in (400, 404, 405), r.text)
            await cx.put(f"{B}/api/areas/glassflow/tares-cloud", headers=ROOT, json={"person_id": kiran})

            print("== a person claims ==")
            r = await cx.put(f"{B}/api/areas/glassflow/docs", headers=J_, json={"person_id": jo, "note": "mine"})
            ck("Jo claims an unowned repo", r.status_code == 200 and r.json()["person"]["id"] == jo
               and r.json()["note"] == "mine", r.text)
            r = await cx.put(f"{B}/api/areas/glassflow/docs", headers=J_, json={"person_id": jo})
            ck("... and may claim it again", r.status_code == 200, r.text)
            r = await cx.put(f"{B}/api/areas/glassflow/tares-cloud", headers=J_, json={"person_id": jo})
            ck("Jo cannot take Kiran's repo", r.status_code == 403 and "Kiran" in r.json()["detail"], r.text)
            r = await cx.put(f"{B}/api/areas/GlassFlow/Tares-Cloud", headers=J_, json={"person_id": jo})
            ck("... whatever the case", r.status_code == 403, r.text)
            r = await cx.put(f"{B}/api/areas/glassflow/tares-cloud", headers=J_, json={"person_id": kiran})
            ck("Jo cannot hand it to Kiran either", r.status_code == 403, r.text)
            r = await cx.put(f"{B}/api/areas/glassflow/web", headers=J_, json={"person_id": kiran})
            ck("Jo cannot claim an unowned repo for Kiran", r.status_code == 403
               and "yourself" in r.json()["detail"], r.text)
            r = await cx.put(f"{B}/api/areas/glassflow/web", headers=J_, json={"person_id": "ghost"})
            ck("claiming for a person who does not exist: 400", r.status_code == 400, r.text)
            r = await cx.put(f"{B}/api/areas/glassflow/web", headers=NOBODY, json={"person_id": jo})
            ck("a key with no person cannot claim", r.status_code == 403, r.text)
            r = await cx.put(f"{B}/api/areas/glassflow/web", headers=JR_, json={"person_id": jo})
            ck("a read key, even Jo's own, is refused at the guard", r.status_code == 403
               and "lacks the 'ingest' scope" in r.json()["detail"], r.text)
            r = await cx.put(f"{B}/api/areas/glassflow/web", json={"person_id": jo})
            ck("no credential: 401", r.status_code == 401, r.text)
            got = owners((await cx.get(f"{B}/api/areas", headers=JR_)).json())
            ck("none of the refusals changed anything", got == {"glassflow/tares-cloud": kiran,
               "glassflow/docs": jo}, str(got))

            print("== replace and delete are admin only ==")
            body = [{"repo": "x/y", "person_id": jo}]
            for who, hd in (("Jo's read+ingest key", J_), ("Jo's read key", JR_)):
                r = await cx.put(f"{B}/api/areas", headers=hd, json=body)
                ck(f"{who}: PUT all -> 403", r.status_code == 403, r.text)
                r = await cx.delete(f"{B}/api/areas/glassflow/docs", headers=hd)
                ck(f"{who}: DELETE -> 403", r.status_code == 403, r.text)
            ck("... nothing changed", len((await cx.get(f"{B}/api/areas", headers=JR_)).json()) == 2)
            r = await cx.put(f"{B}/api/areas", headers=ROOT, json=[
                {"repo": "A/B", "person_id": kiran, "note": "first"}, {"repo": "c/d", "person_id": jo},
                {"repo": "a/b", "person_id": jo}])
            ck("PUT all replaces the list and collapses a repo given twice", r.status_code == 200
               and owners(r.json()) == {"a/b": jo, "c/d": jo} and len(r.json()) == 2, r.text)
            r = await cx.put(f"{B}/api/areas", headers=ROOT, json=[{"repo": "e/f", "person_id": "ghost"}])
            ck("PUT all with an unknown person: 400, list kept", r.status_code == 400
               and len((await cx.get(f"{B}/api/areas", headers=JR_)).json()) == 2, r.text)
            r = await cx.put(f"{B}/api/areas", headers=KA_, json=[{"repo": "e/f", "person_id": kiran}])
            ck("an admin key may replace", r.status_code == 200 and owners(r.json()) == {"e/f": kiran}, r.text)
            r = await cx.delete(f"{B}/api/areas/E/F", headers=KA_)
            ck("an admin key deletes one (any case)", r.status_code == 200
               and (await cx.get(f"{B}/api/areas", headers=JR_)).json() == [], r.text)

            print("== claims at once ==")
            mia = await person("Mia", "mia@x.dev")
            M_ = await key(mia, "read+ingest")
            repos = [f"race/r{i}" for i in range(6)]
            rs = await asyncio.gather(*[cx.put(f"{B}/api/areas/{x}", headers=J_, json={"person_id": jo})
                                        for x in repos])
            ck("six claims of six repos at once: all 200", sorted(r.status_code for r in rs) == [200] * 6,
               str(sorted(r.status_code for r in rs)))
            got = owners((await cx.get(f"{B}/api/areas", headers=JR_)).json())
            ck("... and all six are kept", got == {x: jo for x in repos}, str(got))
            rs = await asyncio.gather(*[cx.put(f"{B}/api/areas/race/one", headers=hd, json={"person_id": who})
                                        for hd, who in [(J_, jo), (M_, mia)] * 4])
            codes = sorted(r.status_code for r in rs)
            ck("Jo and Mia claim one repo at once: one of them gets it, the other is refused",
               codes == [200] * 4 + [403] * 4, str(codes))
            got = owners((await cx.get(f"{B}/api/areas", headers=JR_)).json())
            ck("... and the six earlier repos are still there", all(got.get(x) == jo for x in repos), str(got))
            await cx.put(f"{B}/api/areas", headers=ROOT, json=[])
            ck("set down: the list is empty again", (await cx.get(f"{B}/api/areas", headers=JR_)).json() == [])

            print("== the MCP tools ==")
            who = {"k": J_}
            m._cx = mf._cx = lambda timeout=10: httpx.AsyncClient(timeout=timeout, headers=who["k"])
            m.TARESD = mf.TARESD = B
            who["k"] = ROOT
            out = await mf.set_area("glassflow/tares-cloud", "KIRAN@x.dev")
            ck("set_area by email, as an admin", json.loads(out)["person"]["id"] == kiran, out)
            ck("... the list shows it", [a["repo"] for a in json.loads(await mf.list_areas())]
               == ["glassflow/tares-cloud"])
            out = await mf.set_area("Glassflow/Tares-Cloud", jo)
            ck("... by id and in another case, it replaces the one entry", json.loads(out)["person"]["id"] == jo
               and len(json.loads(await mf.list_areas())) == 1, out)
            who["k"] = J_
            out = await mf.set_area("glassflow/mcp-claim", "jo@x.dev")
            ck("a person claims an unowned repo through the tool", json.loads(out)["person"]["id"] == jo, out)
            out = await mf.set_area("glassflow/mcp-claim", "kiran@x.dev")
            ck("... but cannot give it to Kiran: the refusal is text", out.startswith("error 403"), out)
            out = await mf.set_area("glassflow/x", "ghost@x.dev")
            ck("... an unknown person is named", "ghost@x.dev" in out and "no person" in out, out)
            out = await mf.set_area("justone", "jo@x.dev")
            ck("... a repo without owner/name is refused", "owner/name" in out, out)
            before = json.loads(await mf.list_areas())
            for bad in ("glassflow/a#b", "glassflow/a?b", "glassflow/a b"):
                out = await mf.set_area(bad, "jo@x.dev")
                ck(f"... {bad!r} is refused, not cut short at the # or ?",
                   "owner/name" in out and not out.startswith("{"), out)
            ck("... and nothing was saved by those", json.loads(await mf.list_areas()) == before, "")

            print("== project keys stay closed ==")
            r = await cx.post(f"{B}/api/projects", headers=ROOT, json={"template": "custom", "name": "P1", "objects": []})
            uid = r.json()["id"]
            r = await cx.post(f"{B}/api/projects/{uid}/keys", headers=ROOT, json={"name": "pk", "scopes": ["ingest"]})
            PK = H(r.json().get("secret", ""))
            r = await cx.get(f"{B}/api/areas", headers=PK)
            ck("a project key cannot read areas", r.status_code == 403, r.text)
            r = await cx.put(f"{B}/api/areas/glassflow/pk", headers=PK, json={"person_id": jo})
            ck("... nor claim one", r.status_code == 403, r.text)
    finally:
        proc.terminate(); proc.wait(10)


async def auth_off():
    proc, B = start("off", False)
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up (auth off)", False); return
        async with httpx.AsyncClient(timeout=30) as cx:
            dee = (await cx.post(f"{B}/api/people", json={"name": "Dee", "email": "dee@x.dev"})).json()["id"]
            r = await cx.put(f"{B}/api/areas/g/r", json={"person_id": "local"})
            ck("auth off: sets an area for `local`", r.status_code == 200 and r.json()["person"]["id"] == "local", r.text)
            r = await cx.put(f"{B}/api/areas/g/r", json={"person_id": dee})
            ck("auth off: is an admin, so reassigns it to Dee", r.status_code == 200
               and r.json()["person"]["name"] == "Dee", r.text)
            r = await cx.put(f"{B}/api/areas", json=[{"repo": "g/s", "person_id": dee}])
            ck("auth off: replaces the list", r.status_code == 200 and owners(r.json()) == {"g/s": dee}, r.text)
            r = await cx.delete(f"{B}/api/areas/G/S")
            ck("auth off: deletes", r.status_code == 200 and (await cx.get(f"{B}/api/areas")).json() == [], r.text)
    finally:
        proc.terminate(); proc.wait(10)


async def main():
    try:
        unit()
        await auth_on()
        await auth_off()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())
