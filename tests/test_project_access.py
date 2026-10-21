"""Who leads a project, its scope and its members (TR-623): a project has a lead (the person who
made it), a scope (team or private) and a member list, readable and editable over the API and the
MCP tools. Nothing is enforced yet: this is what the project says.

Run: .venv/bin/python tests/test_project_access.py
"""
import asyncio, json, logging, os, shutil, socket, subprocess, sys, tempfile
import duckdb
import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import clean_env
import tares.mcp_server as m

logging.getLogger("httpx").setLevel(logging.WARNING)   # the MCP import turns request logs on

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

AUTH = "root-token"
TMP = tempfile.mkdtemp(prefix="project-access-")


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


def mk(cx, B, name, hd):
    return cx.post(f"{B}/api/projects", headers=hd,
                   json={"template": "custom", "name": name, "objects": []})


def ids(members):
    return [(x["id"], x["reason"]) for x in members]


async def auth_on():
    proc, B = start("on", True)
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up (auth on)", False); return
        ROOT = H(AUTH)
        async with httpx.AsyncClient(timeout=30) as cx:
            async def person(name, email, admin=False):
                r = await cx.post(f"{B}/api/people", headers=ROOT,
                                  json={"name": name, "email": email, "is_admin": admin})
                return r.json()["id"]

            async def key(pid, scope):
                r = await cx.post(f"{B}/api/keys", headers=ROOT,
                                  json={"name": f"k-{pid}", "scopes": [scope], "person_id": pid})
                return H(r.json()["secret"])

            pp, pq, pa, pr = (await person("Pia", "pia@x.dev"), await person("Quinn", "quinn@x.dev"),
                              await person("Ada", "ada@x.dev", admin=True),
                              await person("Rae", "rae@x.dev"))
            # read keys (Q_ Quinn, PR_ Pia, AR_ Ada) cannot write at all; the others carry the admin
            # scope: P_ Pia, QW_ Quinn, A_ Ada, R_ Rae
            P_, Q_, A_ = await key(pp, "admin"), await key(pq, "read"), await key(pa, "admin")
            R_, QW_ = await key(pr, "admin"), await key(pq, "admin")
            PR_, AR_ = await key(pp, "read"), await key(pa, "read")

            print("== creating ==")
            r = await mk(cx, B, "Shop", P_)
            shop = r.json()
            ck("create answers 201 with lead, scope and members", r.status_code == 201
               and shop["lead"] == {"id": pp, "name": "Pia"} and shop["scope"] == "team"
               and ids(shop["members"]) == [(pp, "lead")], r.text[:300])
            uid = shop["id"]
            r = await mk(cx, B, "Rooted", ROOT)
            ck("a project made with the root token has no lead", r.status_code == 201
               and r.json()["lead"] is None and r.json()["scope"] == "team"
               and r.json()["members"] == [], r.text[:300])
            rooted = r.json()["id"]

            print("== reading ==")
            g = (await cx.get(f"{B}/api/projects/{uid}", headers=Q_)).json()
            ck("GET shows lead, scope, members", g["lead"]["id"] == pp and g["scope"] == "team"
               and ids(g["members"]) == [(pp, "lead")], str(g)[:300])
            lst = {p["id"]: p for p in (await cx.get(f"{B}/api/projects", headers=Q_)).json()["projects"]}
            ck("the list shows them too", lst[uid]["lead"]["name"] == "Pia" and lst[uid]["scope"] == "team"
               and lst[rooted]["lead"] is None, str(lst[uid])[:300])

            print("== the lead changes things ==")
            r = await cx.patch(f"{B}/api/projects/{uid}", headers=P_, json={"scope": "private"})
            ck("lead sets private", r.status_code == 200 and r.json()["scope"] == "private", r.text[:200])
            r = await cx.post(f"{B}/api/projects/{uid}/members", headers=P_, json={"person_id": pq})
            ck("lead adds Quinn", r.status_code == 200
               and ids(r.json()["members"]) == [(pp, "lead"), (pq, "manual")], r.text[:300])
            r = await cx.post(f"{B}/api/projects/{uid}/members", headers=P_, json={"person_id": pq})
            ck("adding Quinn again changes nothing", r.status_code == 200
               and ids(r.json()["members"]) == [(pp, "lead"), (pq, "manual")], r.text[:300])
            r = await cx.post(f"{B}/api/projects/{uid}/members", headers=P_, json={"person_id": pp})
            ck("adding the lead changes nothing", r.status_code == 200
               and ids(r.json()["members"]) == [(pp, "lead"), (pq, "manual")], r.text[:300])
            r = await cx.post(f"{B}/api/projects/{uid}/members", headers=P_, json={"person_id": "nobody"})
            ck("adding someone who does not exist: 404", r.status_code == 404, r.text)
            r = await cx.post(f"{B}/api/projects/{uid}/members", headers=P_, json={})
            ck("adding nobody: 400", r.status_code == 400, r.text)
            r = await cx.patch(f"{B}/api/projects/{uid}", headers=P_, json={"scope": "secret"})
            ck("scope other than team or private: 400", r.status_code == 400, r.text)
            r = await cx.patch(f"{B}/api/projects/{uid}", headers=P_, json={})
            ck("saying nothing: 400", r.status_code == 400, r.text)
            r = await cx.patch(f"{B}/api/projects/nope", headers=P_, json={"scope": "team"})
            ck("unknown project: 404", r.status_code == 404, r.text)
            r = await cx.patch(f"{B}/api/projects/{uid}", headers=P_, json={"lead_person_id": "nobody"})
            ck("a lead who is not a person: 404 with a plain message", r.status_code == 404
               and "no person" in r.text, r.text)
            r = await cx.delete(f"{B}/api/projects/{uid}/members/{pp}", headers=P_)
            ck("the lead cannot be taken off: 409", r.status_code == 409 and "lead" in r.text, r.text)

            print("== others are refused ==")
            def writes(hd):
                return (
                    ("PATCH scope", cx.patch(f"{B}/api/projects/{uid}", headers=hd, json={"scope": "team"})),
                    ("PATCH lead", cx.patch(f"{B}/api/projects/{uid}", headers=hd, json={"lead_person_id": pq})),
                    ("add member", cx.post(f"{B}/api/projects/{uid}/members", headers=hd, json={"person_id": pr})),
                    ("remove member", cx.delete(f"{B}/api/projects/{uid}/members/{pq}", headers=hd)))
            for who, hd in (("Quinn (read key, not lead)", Q_), ("Pia (read key, the lead)", PR_),
                            ("Ada (read key, an admin person)", AR_)):
                for label, req in writes(hd):
                    r = await req
                    ck(f"{who}: {label} -> 403 at the guard", r.status_code == 403
                       and "lacks the 'admin' scope" in r.json()["detail"], r.text)
            qw = writes(QW_)   # PATCH lead is open to an admin key, so not the second
            qw[1][1].close()
            for label, req in qw[:1] + qw[2:]:
                r = await req
                ck(f"Quinn (admin key, not lead): {label} -> 403", r.status_code == 403
                   and r.json()["detail"] == "only the project lead can change this", r.text)
            g = (await cx.get(f"{B}/api/projects/{uid}", headers=Q_)).json()
            ck("... and nothing changed", g["scope"] == "private" and g["lead"]["id"] == pp
               and ids(g["members"]) == [(pp, "lead"), (pq, "manual")], str(g)[:300])

            print("== an admin ==")
            r = await cx.patch(f"{B}/api/projects/{uid}", headers=A_, json={"scope": "team"})
            ck("admin person: cannot change the scope", r.status_code == 403, r.text)
            r = await cx.post(f"{B}/api/projects/{uid}/members", headers=A_, json={"person_id": pr})
            ck("admin person who is not the lead: cannot add members", r.status_code == 403, r.text)
            r = await cx.delete(f"{B}/api/projects/{uid}/members/{pq}", headers=A_)
            ck("... nor remove them", r.status_code == 403, r.text)
            r = await cx.patch(f"{B}/api/projects/{uid}", headers=A_,
                               json={"lead_person_id": pa, "scope": "team"})
            ck("admin person: lead and scope together is refused whole", r.status_code == 403
               and (await cx.get(f"{B}/api/projects/{uid}", headers=A_)).json()["lead"]["id"] == pp, r.text)
            r = await cx.patch(f"{B}/api/projects/{uid}", headers=R_, json={"lead_person_id": pr})
            ck("a key with the admin scope may change the lead", r.status_code == 200
               and r.json()["lead"]["id"] == pr and r.json()["scope"] == "private", r.text[:300])
            ck("... and the one who handed it over stays on it", (pp, "manual") in ids(r.json()["members"]),
               str(r.json()["members"]))
            r = await cx.patch(f"{B}/api/projects/{uid}", headers=R_, json={"scope": "team"})
            ck("... as lead now, it may change the scope too", r.status_code == 200
               and r.json()["scope"] == "team", r.text[:300])
            r = await cx.patch(f"{B}/api/projects/{uid}", headers=A_, json={"lead_person_id": pp})
            ck("admin person (is_admin) changes the lead", r.status_code == 200
               and r.json()["lead"]["id"] == pp, r.text[:300])
            g = (await cx.get(f"{B}/api/projects/{uid}", headers=A_)).json()
            ck("the new lead heads the members, the old lead stays on them", ids(g["members"])
               == [(pp, "lead"), (pq, "manual"), (pr, "manual")], str(g["members"]))
            r = await cx.patch(f"{B}/api/projects/{uid}", headers=R_, json={"lead_person_id": pp})
            ck("handing the lead to the lead changes nothing", r.status_code == 200
               and ids(r.json()["members"]) == [(pp, "lead"), (pq, "manual"), (pr, "manual")], r.text[:300])
            r = await cx.patch(f"{B}/api/projects/{uid}", headers=R_, json={"lead_person_id": pq})
            got = ids(r.json()["members"])
            ck("a member made lead is on the list once, as lead", r.status_code == 200
               and got[0] == (pq, "lead") and [i for i, _ in got].count(pq) == 1, str(got))
            ck("... the old lead and the others stay, each once", sorted(got[1:]) ==
               sorted([(pp, "manual"), (pr, "manual")]), str(got))
            r = await cx.patch(f"{B}/api/projects/{uid}", headers=R_, json={"lead_person_id": pp})
            got = ids(r.json()["members"])
            ck("... and handed back, nobody is listed twice", got[0] == (pp, "lead")
               and sorted(got[1:]) == sorted([(pq, "manual"), (pr, "manual")]), str(got))

            print("== no lead ==")
            r = await cx.patch(f"{B}/api/projects/{rooted}", headers=Q_, json={"lead_person_id": pq})
            ck("a read key cannot claim it", r.status_code == 403, r.text)
            r = await cx.patch(f"{B}/api/projects/{rooted}", headers=A_, json={"scope": "private"})
            ck("an admin cannot set its scope before claiming", r.status_code == 403, r.text)
            r = await cx.patch(f"{B}/api/projects/{rooted}", headers=A_, json={"lead_person_id": pa})
            ck("an admin person claims it", r.status_code == 200 and r.json()["lead"]["id"] == pa, r.text[:300])
            r = await cx.patch(f"{B}/api/projects/{rooted}", headers=A_, json={"scope": "private"})
            ck("... and as its lead sets the scope", r.status_code == 200 and r.json()["scope"] == "private",
               r.text[:300])

            print("== the system credential ==")
            r = await cx.patch(f"{B}/api/projects/{uid}", headers=ROOT, json={"lead_person_id": pq})
            ck("changes the lead", r.status_code == 200 and r.json()["lead"]["id"] == pq, r.text[:300])
            r = await cx.patch(f"{B}/api/projects/{uid}", headers=ROOT, json={"scope": "team"})
            ck("but not the scope", r.status_code == 403, r.text)
            r = await cx.post(f"{B}/api/projects/{uid}/members", headers=ROOT, json={"person_id": pr})
            ck("adds a member", r.status_code == 200 and (pr, "manual") in ids(r.json()["members"]), r.text[:300])
            r = await cx.delete(f"{B}/api/projects/{uid}/members/{pr}", headers=ROOT)
            ck("removes a member", r.status_code == 200 and pr not in [x["id"] for x in r.json()["members"]],
               r.text[:300])
            r = await cx.delete(f"{B}/api/projects/{uid}/members/{pr}", headers=ROOT)
            ck("removing someone who is not on it changes nothing", r.status_code == 200, r.text)
            r = await cx.delete(f"{B}/api/projects/{uid}/members/{pq}", headers=ROOT)
            ck("Quinn leads it now, so cannot be removed: 409", r.status_code == 409, r.text)

            print("== the MCP tools ==")
            m._cx = lambda timeout=10: httpx.AsyncClient(timeout=timeout, headers=P_)
            m.TARESD = B
            mine = (await mk(cx, B, "Mine", P_)).json()["id"]
            ppl = json.loads(await m.list_people())
            ck("list_people lists everyone with id, name, email", {x["email"] for x in ppl if x["email"]}
               >= {"pia@x.dev", "quinn@x.dev", "ada@x.dev"} and all("id" in x and "name" in x for x in ppl),
               str(ppl)[:300])
            out = json.loads(await m.set_project_access("Mine", scope="private",
                                                        add_members=["QUINN@x.dev", pr]))
            ck("set_project_access: scope, and members by email or id", out["scope"] == "private"
               and out["lead"]["id"] == pp
               and ids(out["members"]) == [(pp, "lead"), (pq, "manual"), (pr, "manual")], str(out)[:300])
            out = json.loads(await m.set_project_access("Mine", remove_members=["quinn@x.dev"]))
            ck("... removing by email", ids(out["members"]) == [(pp, "lead"), (pr, "manual")], str(out)[:300])
            out = json.loads(await m.set_project_access("Mine", lead="ada@x.dev"))
            ck("... naming the lead by email", out["lead"]["id"] == pa, str(out)[:300])
            bad = await m.set_project_access("Mine", scope="team")
            ck("... Pia is no longer the lead: the refusal comes back as text",
               bad.startswith("error 403") and "only the project lead" in bad, bad)
            bad = await m.set_project_access("Mine", add_members=["ghost@x.dev"])
            ck("... an unknown person is named", "ghost@x.dev" in bad and "no person" in bad, bad)
            out = json.loads(await m.set_project_access("Mine"))
            ck("... with nothing to change it reads the project", out["scope"] == "private"
               and out["lead"]["id"] == pa, str(out)[:300])
            combo = (await mk(cx, B, "Combo", P_)).json()["id"]
            raw = await m.set_project_access("Combo", lead="ada@x.dev", scope="private",
                                             add_members=["quinn@x.dev"], remove_members=[""])
            out = json.loads(raw) if raw.startswith("{") else {"error": raw}
            ck("a lead handed over in the same call as scope and members: all of it lands",
               out.get("lead", {}).get("id") == pa and out.get("scope") == "private"
               and (pq, "manual") in ids(out.get("members", [])), raw[:300])
            out = json.loads(await m.set_project_access("Combo", add_members=["", "  "]))
            ck("... a blank person is skipped, not a crash", out["lead"]["id"] == pa, str(out)[:300])

            print("== deleting ==")
            await cx.post(f"{B}/api/projects/{mine}/members", headers=A_, json={"person_id": pq})
            r = await cx.delete(f"{B}/api/projects/{mine}", headers=ROOT)
            ck("the project goes", r.status_code == 200, r.text[:200])
    finally:
        proc.terminate()
        proc.wait(timeout=10)


async def auth_off():
    proc, B = start("off", False)
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up (auth off)", False); return
        async with httpx.AsyncClient(timeout=30) as cx:
            r = await mk(cx, B, "Solo", {})
            ck("auth off: the creator is `local`, named You", r.status_code == 201
               and r.json()["lead"] == {"id": "local", "name": "You"} and r.json()["scope"] == "team"
               and ids(r.json()["members"]) == [("local", "lead")], r.text[:300])
            uid = r.json()["id"]
            r = await cx.patch(f"{B}/api/projects/{uid}", json={"scope": "private"})
            ck("auth off: it leads what it made, so sets the scope", r.status_code == 200
               and r.json()["scope"] == "private", r.text[:300])
            r = await cx.post(f"{B}/api/people", json={"name": "Dee", "email": "dee@x.dev"})
            dee = r.json()["id"]
            r = await cx.post(f"{B}/api/projects/{uid}/members", json={"person_id": dee})
            ck("auth off: adds a member", r.status_code == 200
               and ids(r.json()["members"]) == [("local", "lead"), (dee, "manual")], r.text[:300])
            r = await cx.patch(f"{B}/api/projects/{uid}", json={"lead_person_id": dee})
            ck("auth off: hands the lead to Dee", r.status_code == 200 and r.json()["lead"]["id"] == dee,
               r.text[:300])
            r = await cx.patch(f"{B}/api/projects/{uid}", json={"scope": "team"})
            ck("auth off: no longer the lead, so the scope is closed to `local`", r.status_code == 403, r.text)
            r = await cx.patch(f"{B}/api/projects/{uid}", json={"lead_person_id": "local"})
            ck("auth off: but the lead can be taken back (system)", r.status_code == 200, r.text[:200])
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def unit():
    """Rows from before: no lead, no scope. And the member functions of the store."""
    print("== projects from before ==")
    from tares.store import Store
    path = os.path.join(TMP, "old.duckdb")
    con = duckdb.connect(path)
    con.execute("CREATE TABLE usecases (id TEXT PRIMARY KEY, recipe TEXT, name TEXT, params JSON, "
                "status TEXT, created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ, last_error TEXT)")
    con.execute("INSERT INTO usecases VALUES ('uc_old', 'custom', 'Old', '{}', 'active', now(), now(), NULL)")
    con.close()
    s = Store(path)
    old = s.get_project("uc_old")
    ck("an old project reads scope team and no lead", old["scope"] == "team" and old["lead_person_id"] is None,
       str(old))
    ck("... list_projects too", [(p["scope"], p["lead_person_id"]) for p in s.list_projects()]
       == [("team", None)])
    s.set_project_scope("uc_old", "private")
    s.con.close()
    s = Store(path)
    ck("reopening keeps what was set", s.get_project("uc_old")["scope"] == "private")

    print("== store ==")
    ann = s.create_person("Ann", "ann@x.dev")["id"]
    ben = s.create_person("Ben", "ben@x.dev")["id"]
    s.set_project_lead("uc_old", ann)
    ck("is_project_member: the lead", s.is_project_member("uc_old", ann))
    ck("is_project_member: someone else is not", not s.is_project_member("uc_old", ben))
    ck("is_project_member: nobody is not", not s.is_project_member("uc_old", None))
    ck("add_project_member: true the first time", s.add_project_member("uc_old", ben, ann, "ticket"))
    ck("... false the second, the first reason stays", not s.add_project_member("uc_old", ben, ann)
       and [x["reason"] for x in s.list_project_members("uc_old")] == ["ticket"])
    ck("... the person is a member", s.is_project_member("uc_old", ben))
    try:
        s.set_project_scope("uc_old", "open"); bad = False
    except ValueError:
        bad = True
    ck("set_project_scope refuses anything but team or private", bad)
    s.delete_project("uc_old")
    ck("delete_project deletes its members", s.list_project_members("uc_old") == []
       and s.get_project("uc_old") is None)
    s.con.close()


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
