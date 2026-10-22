"""Grants by kind (TR-630): team, project and personal grants as rows. A project grant that names
a repo someone else owns waits until that owner consents, and one from a person who is neither the
project's lead nor an admin waits for the lead too (an admin, when the project has no lead). The old grants docs become rows on the
first start and stay as they were. With auth off the one person is `local` and grants work as before.

Run: .venv/bin/python tests/test_grants_teams.py
"""
import asyncio, json, os, shutil, socket, subprocess, sys, tempfile
import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import clean_env
import logging
import tares.mcp_server as m
import tares.mcp_factory as mf

logging.getLogger("httpx").setLevel(logging.WARNING)   # the MCP import turns request logs on

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

AUTH = "root-token"
TMP = tempfile.mkdtemp(prefix="grants-teams-")


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


def start(db, auth):
    port = free_port()
    env = {**clean_env(), "TARES_DB": db, "TARES_PORT": port, "TARES_OTLP_GRPC_PORT": "off"}
    if auth:
        env["TARES_AUTH_TOKEN"] = AUTH
    proc = subprocess.Popen([sys.executable, "-c", "from tares.cli import run_daemon; run_daemon()"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc, f"http://127.0.0.1:{port}"


def stop(proc):
    proc.terminate(); proc.wait()


def words(rows):
    return sorted(g["words"] for g in rows)


async def migration():
    print("== the old grants docs become rows, once ==")
    from tares.store import Store
    db = os.path.join(TMP, "migrate.duckdb")
    s = Store(db)
    gid = s.create_doc(None, "grants", "Grants (all projects)",
                       "# Grants\n\n- \"run the tests\" (2026-10-01)\n- \"open pull requests\" (2026-10-02)\n",
                       by="tares")
    s.set_setting("global_grants", gid)
    s.create_project("p1", "custom", "Legacy", {})
    s.create_project("p2", "custom", "Other", {})
    own = s.create_doc("p1", "grants", "Grants: Legacy",
                       "# Grants\n\n- \"touch the staging cluster\" (2026-10-03)\n", by="x")
    s.use_doc("p1", gid)
    s.con.close()

    proc, B = start(db, False)
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up (migration)", False); return
        async with httpx.AsyncClient(timeout=30) as cx:
            r = (await cx.get(f"{B}/api/grants", params={"project": "p1"})).json()
            team = [g for g in r["grants"] if g["kind"] == "team"]
            proj = [g for g in r["grants"] if g["kind"] == "project"]
            ck("each line of the shared doc is an active team grant",
               words(team) == ["open pull requests", "run the tests"]
               and all(g["state"] == "active" and g["in_effect"] for g in team), str(r))
            ck("each line of the project's doc is an active project grant",
               words(proj) == ["touch the staging cluster"] and proj[0]["project"] == "p1"
               and proj[0]["in_effect"], str(r))
            r2 = (await cx.get(f"{B}/api/grants", params={"project": "p2"})).json()
            ck("another project sees the team grants and not p1's own",
               words(r2["grants"]) == ["open pull requests", "run the tests"], str(r2))
            ck("the old docs are as they were",
               "run the tests" in r["all"] and "touch the staging cluster" in r["project"], str(r))
    finally:
        stop(proc)

    proc, B = start(db, False)     # a second start does not add them again
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up (second start)", False); return
        async with httpx.AsyncClient(timeout=30) as cx:
            r = (await cx.get(f"{B}/api/grants", params={"project": "p1"})).json()
            ck("a second start adds nothing", len(r["grants"]) == 3, str(r["grants"]))
    finally:
        stop(proc)


async def auth_on():
    print("== who may add, waiting and consent (auth on) ==")
    proc, B = start(os.path.join(TMP, "on.duckdb"), True)
    ROOT = H(AUTH)
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up (auth on)", False); return
        async with httpx.AsyncClient(timeout=30) as cx:
            async def person(name, email):
                r = await cx.post(f"{B}/api/people", headers=ROOT, json={"name": name, "email": email})
                return r.json()["id"]

            async def key(pid, scope):
                r = await cx.post(f"{B}/api/keys", headers=ROOT,
                                  json={"name": f"k-{pid}-{scope}", "scopes": scope.split("+"),
                                        "person_id": pid})
                return H(r.json()["secret"])

            a, b, c, d = (await person("Person A", "a@example.test"), await person("Person B", "b@example.test"),
                          await person("Person C", "c@example.test"), await person("Person D", "d@example.test"))
            KA, KB, KC = await key(a, "read+ingest"), await key(b, "read+ingest"), await key(c, "read+ingest")
            KD, KA_ADMIN = await key(d, "read+ingest+admin"), await key(a, "admin")
            NOBODY = H((await cx.post(f"{B}/api/keys", headers=ROOT,
                                      json={"name": "nobody", "scopes": ["ingest"]})).json()["secret"])

            p1 = (await cx.post(f"{B}/api/projects", headers=KA_ADMIN, json={
                "template": "custom", "name": "Web", "objects": []})).json()["id"]
            p2 = (await cx.post(f"{B}/api/projects", headers=KA_ADMIN, json={
                "template": "custom", "name": "Docs", "objects": []})).json()["id"]
            r = await cx.put(f"{B}/api/areas/acme/web", headers=ROOT, json={"person_id": b})
            ck("set up: A leads two projects (their ingest key has no admin scope), B owns acme/web", r.status_code == 200, r.text)
            await cx.put(f"{B}/api/areas/acme/api", headers=ROOT, json={"person_id": a})

            print("-- team --")
            r = await cx.post(f"{B}/api/grants", headers=KA, json={"kind": "team", "words": "run the tests"})
            ck("a person without the admin scope cannot add a team grant -> 403", r.status_code == 403, r.text)
            r = await cx.post(f"{B}/api/grants", headers=KD, json={"kind": "team", "words": "run the tests"})
            ck("an admin key adds one", r.status_code == 201 and r.json()["grant"]["kind"] == "team"
               and r.json()["grant"]["in_effect"] and r.json()["grant"]["id"].startswith("G"), r.text)
            r = await cx.post(f"{B}/api/grants", headers=ROOT, json={"kind": "team", "words": "tag releases"})
            ck("the system credential adds one", r.status_code == 201, r.text)
            r = await cx.post(f"{B}/api/grants", headers=KD, json={"kind": "team", "words": "run the tests"})
            ck("the same words twice are one grant", r.status_code == 201 and r.json()["added"] is False, r.text)
            for pid in (p1, p2):
                rows = (await cx.get(f"{B}/api/grants", headers=KB, params={"project": pid})).json()["grants"]
                ck(f"a team grant shows in project {pid[:6]}'s list",
                   {"run the tests", "tag releases"} <= set(words(rows)), str(rows))

            print("-- project: who may add --")
            r = await cx.post(f"{B}/api/grants", headers=KC,
                              json={"kind": "project", "project": p1, "words": "deploy to staging"})
            ck("C, not the lead and owning nothing, cannot -> 403", r.status_code == 403, r.text)
            r = await cx.post(f"{B}/api/grants", headers=KB,
                              json={"kind": "project", "project": p1, "words": "change acme/web",
                                    "areas": ["acme/web"]})
            g = r.json().get("grant", {})
            ck("B, owner of the repo it names but not the lead, may add it: it waits for the lead",
               r.status_code == 201 and g.get("state") == "waiting" and not g.get("in_effect")
               and [w["id"] for w in g.get("waiting_for", [])] == [a], r.text)
            bgid = g.get("id")
            r = await cx.post(f"{B}/api/grants", headers=KB,
                              json={"kind": "project", "project": p1, "words": "change acme/api",
                                    "areas": ["acme/api"]})
            ck("B cannot grant for a repo A owns -> 403", r.status_code == 403, r.text)
            r = await cx.post(f"{B}/api/grants", headers=KA,
                              json={"kind": "project", "project": p1, "words": "deploy to staging"})
            ck("A, the lead, adds one with no areas: in effect at once",
               r.status_code == 201 and r.json()["grant"]["in_effect"]
               and r.json()["grant"]["given_by"]["id"] == a, r.text)
            r = await cx.post(f"{B}/api/grants", headers=KA, json={"kind": "project", "words": "no project"})
            ck("a project grant must name its project -> 400", r.status_code == 400, r.text)
            r = await cx.post(f"{B}/api/grants", headers=KA,
                              json={"kind": "project", "project": p1, "words": "bad area", "areas": ["nonsense"]})
            ck("an area that is not owner/name -> 400", r.status_code == 400, r.text)

            print("-- waiting for the owner --")
            r = await cx.post(f"{B}/api/grants", headers=KA,
                              json={"kind": "project", "project": p1, "words": "merge to acme/web",
                                    "areas": ["acme/web"]})
            g = r.json().get("grant", {})
            ck("A adds a project grant naming acme/web, owned by B: it is waiting",
               r.status_code == 201 and g.get("state") == "waiting" and g.get("in_effect") is False, r.text)
            ck("... waiting for B", [w["id"] for w in g.get("waiting_for", [])] == [b], str(g))
            gid = g.get("id")
            doc = (await cx.get(f"{B}/api/grants", headers=KA, params={"project": p1})).json()
            ck("... and the grants doc does not hold it yet", "merge to acme/web" not in doc["project"], doc["project"])

            r = await cx.post(f"{B}/api/grants/{gid}/consent", headers=KC)
            ck("C cannot consent -> 403", r.status_code == 403, r.text)
            r = await cx.post(f"{B}/api/grants/{gid}/consent", headers=KA)
            ck("A, the giver, cannot consent for B -> 403", r.status_code == 403, r.text)
            r = await cx.post(f"{B}/api/grants/{gid}/consent", headers=NOBODY)
            ck("a key with no person cannot consent -> 403", r.status_code == 403, r.text)
            r = await cx.get(f"{B}/api/grants", headers=KA, params={"project": p1})
            now = next(x for x in r.json()["grants"] if x["id"] == gid)
            ck("... and it is still waiting", now["state"] == "waiting", str(now))

            r = await cx.post(f"{B}/api/grants/{gid}/consent", headers=KB)
            g = r.json().get("grant", {})
            ck("B consents: it is active and in effect",
               r.status_code == 200 and g.get("state") == "active" and g.get("in_effect") is True
               and g.get("waiting_for") == [], r.text)
            ck("... B is on the consents, with the time",
               [(x["id"], bool(x["at"])) for x in g.get("consents", [])] == [(b, True)], str(g))
            doc = (await cx.get(f"{B}/api/grants", headers=KA, params={"project": p1})).json()
            ck("... and the grants doc now holds it", "merge to acme/web" in doc["project"], doc["project"])
            r = await cx.post(f"{B}/api/grants/{gid}/consent", headers=KB)
            ck("consenting again -> 409", r.status_code == 409, r.text)

            print("-- a non-lead's grant waits for the lead (A18) --")
            doc = (await cx.get(f"{B}/api/grants", headers=KA, params={"project": p1})).json()
            ck("B's grant is not in the grants doc yet", "change acme/web" not in doc["project"], doc["project"])
            r = await cx.post(f"{B}/api/grants/{bgid}/consent", headers=KB)
            ck("B cannot consent to their own grant, as the owner or for the lead -> 403", r.status_code == 403, r.text)
            r = await cx.post(f"{B}/api/grants/{bgid}/consent", headers=KC)
            ck("C cannot consent for the lead -> 403", r.status_code == 403, r.text)
            r = await cx.post(f"{B}/api/grants/{bgid}/consent", headers=KD)
            ck("an admin cannot stand in for a lead the project has -> 403", r.status_code == 403, r.text)
            r = await cx.post(f"{B}/api/grants/{bgid}/consent", headers=KA)
            g = r.json().get("grant", {})
            ck("the lead consents: it is active", r.status_code == 200 and g.get("state") == "active"
               and g.get("in_effect") and g.get("waiting_for") == [], r.text)
            doc = (await cx.get(f"{B}/api/grants", headers=KA, params={"project": p1})).json()
            ck("... and the grants doc holds it", "change acme/web" in doc["project"], doc["project"])
            r = await cx.post(f"{B}/api/grants", headers=KB,
                              json={"kind": "project", "project": p1, "words": "open the docs PR",
                                    "areas": ["acme/web"]})
            wg = r.json()["grant"]
            r = await cx.post(f"{B}/api/grants", headers=KA,
                              json={"kind": "project", "project": p1, "words": "open the docs PR"})
            g = r.json().get("grant", {})
            ck("the lead's own add of the same words takes the waiting one into effect",
               r.status_code == 201 and r.json()["added"] is True and g.get("id") == wg["id"]
               and g.get("state") == "active", r.text)
            r = await cx.post(f"{B}/api/grants", headers=KA,
                              json={"kind": "project", "project": p1, "words": "open the docs PR"})
            ck("... and again is the same grant", r.json()["added"] is False and r.json()["grant"]["id"] == wg["id"], r.text)
            r = await cx.post(f"{B}/api/grants", headers=KB,
                              json={"kind": "project", "project": p1, "words": "second ask",
                                    "areas": ["acme/web"]})
            w1 = r.json()["grant"]
            r = await cx.post(f"{B}/api/grants", headers=KB,
                              json={"kind": "project", "project": p1, "words": "second ask",
                                    "areas": ["acme/web"]})
            ck("the same non-lead grant twice is one waiting grant", r.json()["added"] is False
               and r.json()["grant"]["id"] == w1["id"] and r.json()["grant"]["state"] == "waiting", r.text)

            print("-- a project with no lead: an admin consents --")
            p3 = (await cx.post(f"{B}/api/projects", headers=ROOT, json={
                "template": "custom", "name": "Leadless", "objects": []})).json()["id"]
            r = await cx.post(f"{B}/api/grants", headers=KB,
                              json={"kind": "project", "project": p3, "words": "edit web on p3",
                                    "areas": ["acme/web"]})
            g = r.json().get("grant", {})
            ck("B's grant on it waits for an admin", r.status_code == 201 and g.get("state") == "waiting"
               and [w["id"] for w in g.get("waiting_for", [])] == ["admin"], r.text)
            for who, key_ in (("B", KB), ("A without the admin scope", KA), ("C", KC)):
                r = await cx.post(f"{B}/api/grants/{g['id']}/consent", headers=key_)
                ck(f"{who} cannot consent -> 403", r.status_code == 403, r.text)
            r = await cx.post(f"{B}/api/grants/{g['id']}/consent", headers=KD)
            g2 = r.json().get("grant", {})
            ck("a key with the admin scope consents: active, on the consents",
               r.status_code == 200 and g2.get("state") == "active"
               and [c["id"] for c in g2.get("consents", [])] == [d], r.text)
            r = await cx.post(f"{B}/api/grants", headers=KB,
                              json={"kind": "project", "project": p3, "words": "edit web again",
                                    "areas": ["acme/web"]})
            r = await cx.post(f"{B}/api/grants/{r.json()['grant']['id']}/consent", headers=ROOT)
            ck("the system credential consents too", r.status_code == 200
               and r.json()["grant"]["state"] == "active", r.text)

            print("-- the owner who cannot see the project can still consent --")
            p4 = (await cx.post(f"{B}/api/projects", headers=KA_ADMIN, json={
                "template": "custom", "name": "Secret", "objects": []})).json()["id"]
            await cx.patch(f"{B}/api/projects/{p4}", headers=KA_ADMIN, json={"scope": "private"})
            r = await cx.get(f"{B}/api/grants", headers=KB, params={"project": p4})
            ck("set up: B cannot see the private project -> 404", r.status_code == 404, r.text)
            r = await cx.post(f"{B}/api/grants", headers=KA,
                              json={"kind": "project", "project": p4, "words": "touch acme/web in secret",
                                    "areas": ["acme/web"]})
            sg = r.json()["grant"]
            ck("A's grant on it waits for B", sg["state"] == "waiting"
               and [w["id"] for w in sg["waiting_for"]] == [b], r.text)
            mine = (await cx.get(f"{B}/api/grants", headers=KB)).json()
            ck("B finds it under waiting_for_me, with its words",
               [x["id"] for x in mine["waiting_for_me"]] == [sg["id"]]
               and mine["waiting_for_me"][0]["words"] == "touch acme/web in secret", str(mine["waiting_for_me"]))
            ck("... and not in the plain list", all(x["id"] != sg["id"] for x in mine["grants"]), str(mine["grants"]))
            r = await cx.post(f"{B}/api/grants/{sg['id']}/revoke", headers=KB)
            ck("B cannot revoke it: the project is not theirs to see -> 404", r.status_code == 404, r.text)
            r = await cx.post(f"{B}/api/grants/{sg['id']}/consent", headers=KC)
            ck("C, not waited for, still gets 404 for the private project's grant", r.status_code == 404, r.text)
            r = await cx.post(f"{B}/api/grants/{sg['id']}/consent", headers=KB)
            ck("B consents: it is active", r.status_code == 200 and r.json()["grant"]["state"] == "active", r.text)
            mine = (await cx.get(f"{B}/api/grants", headers=KB)).json()
            ck("... and leaves waiting_for_me", mine["waiting_for_me"] == [], str(mine["waiting_for_me"]))

            print("-- a grant nobody is left to wait for takes effect --")
            await cx.put(f"{B}/api/areas/acme/ops", headers=ROOT, json={"person_id": b})
            r = await cx.post(f"{B}/api/grants", headers=KA,
                              json={"kind": "project", "project": p1, "words": "restart acme/ops",
                                    "areas": ["acme/ops"]})
            og = r.json()["grant"]
            ck("A's grant naming acme/ops waits for B", og["state"] == "waiting", r.text)
            await cx.put(f"{B}/api/areas/acme/ops", headers=ROOT, json={"person_id": a})
            rows = (await cx.get(f"{B}/api/grants", headers=KA, params={"project": p1})).json()
            now = next(x for x in rows["grants"] if x["id"] == og["id"])
            ck("acme/ops moves to A, the giver: the next read finds it in effect",
               now["state"] == "active" and now["in_effect"] and now["waiting_for"] == [], str(now))
            ck("... and the grants doc holds it", "restart acme/ops" in rows["project"], rows["project"])

            print("-- two owners, both must --")
            await cx.put(f"{B}/api/areas/acme/billing", headers=ROOT, json={"person_id": c})
            r = await cx.post(f"{B}/api/grants", headers=KA,
                              json={"kind": "project", "project": p2, "words": "touch both",
                                    "areas": ["acme/web", "acme/billing"]})
            g2 = r.json()["grant"]
            ck("a grant naming two owners' repos waits for both",
               g2["state"] == "waiting" and sorted(w["id"] for w in g2["waiting_for"]) == sorted([b, c]), str(g2))
            r = await cx.post(f"{B}/api/grants/{g2['id']}/consent", headers=KB)
            ck("one consent is not enough",
               r.json()["grant"]["state"] == "waiting"
               and [w["id"] for w in r.json()["grant"]["waiting_for"]] == [c], r.text)
            r = await cx.post(f"{B}/api/grants/{g2['id']}/consent", headers=KC)
            ck("the second makes it active", r.json()["grant"]["state"] == "active", r.text)

            print("-- personal --")
            r = await cx.post(f"{B}/api/grants", headers=KB, json={"kind": "personal", "words": "use my laptop"})
            pg = r.json().get("grant", {})
            ck("B adds a personal grant for themselves",
               r.status_code == 201 and pg.get("kind") == "personal" and pg.get("in_effect")
               and pg.get("holder", {}).get("id") == b, r.text)
            rb = (await cx.get(f"{B}/api/grants", headers=KB, params={"project": p1})).json()["grants"]
            ra = (await cx.get(f"{B}/api/grants", headers=KA, params={"project": p1})).json()["grants"]
            ck("B sees it", "use my laptop" in words(rb), str(rb))
            ck("A does not see B's personal grant", "use my laptop" not in words(ra), str(ra))
            r = await cx.get(f"{B}/api/grants", headers=ROOT, params={"project": p1})
            ck("the system credential does not see it either", "use my laptop" not in words(r.json()["grants"]), r.text)
            r = await cx.post(f"{B}/api/grants", headers=NOBODY, json={"kind": "personal", "words": "mine"})
            ck("a key with no person cannot add a personal grant -> 403", r.status_code == 403, r.text)
            r = await cx.post(f"{B}/api/grants/{pg['id']}/revoke", headers=KA)
            ck("A cannot see, so cannot revoke, B's personal grant -> 404", r.status_code == 404, r.text)
            r = await cx.post(f"{B}/api/grants", headers=KB,
                              json={"kind": "personal", "words": "x", "areas": ["acme/web"]})
            ck("areas belong to a project grant -> 400", r.status_code == 400, r.text)

            print("-- revoke --")
            r = await cx.post(f"{B}/api/grants/{gid}/revoke", headers=KC)
            ck("C cannot revoke the project grant -> 403", r.status_code == 403, r.text)
            r = await cx.post(f"{B}/api/grants/{gid}/revoke", headers=KA)
            ck("A (giver and lead) revokes it", r.status_code == 200 and r.json()["grant"]["state"] == "revoked", r.text)
            rows = (await cx.get(f"{B}/api/grants", headers=KA, params={"project": p1})).json()
            ck("... it is gone from the list and from the doc",
               gid not in [x["id"] for x in rows["grants"]] and "merge to acme/web" not in rows["project"],
               str(rows))
            r = await cx.post(f"{B}/api/grants/{gid}/revoke", headers=KA)
            ck("revoking twice -> 409", r.status_code == 409, r.text)
            r = await cx.post(f"{B}/api/grants/{pg['id']}/revoke", headers=KD)
            ck("an admin key cannot see, so cannot revoke, B's personal grant -> 404", r.status_code == 404, r.text)
            r = await cx.post(f"{B}/api/grants/{pg['id']}/revoke", headers=KB)
            ck("B revokes their own personal grant", r.status_code == 200, r.text)
            r = await cx.post(f"{B}/api/grants", headers=KA,
                              json={"kind": "project", "project": p1, "words": "admin ends this"})
            r = await cx.post(f"{B}/api/grants/{r.json()['grant']['id']}/revoke", headers=KD)
            ck("an admin key revokes a project grant it neither gave nor leads",
               r.status_code == 200 and r.json()["grant"]["state"] == "revoked", r.text)

            print("-- personal grants stay with their project --")
            r = await cx.post(f"{B}/api/grants", headers=KB,
                              json={"kind": "personal", "words": "use p2 laptop", "project": p2})
            ck("B adds a personal grant tied to the Docs project", r.status_code == 201, r.text)
            for label, params, want in (("Docs", {"project": p2}, True), ("Web", {"project": p1}, False),
                                        ("no project", {}, False)):
                rows = (await cx.get(f"{B}/api/grants", headers=KB, params=params)).json()["grants"]
                ck(f"B sees it when asking for {label}: {want}", ("use p2 laptop" in words(rows)) is want, str(rows))

            print("-- the list --")
            rows = (await cx.get(f"{B}/api/grants", headers=KA, params={"project": p1})).json()["grants"]
            waiting = [x for x in rows if not x["in_effect"]]
            ck("the list has the shape the console reads",
               all({"id", "kind", "words", "state", "given_by", "consents", "waiting_for", "in_effect"} <= set(x)
                   for x in rows), str(rows[:1]))
            ck("grants in effect come first", rows == [x for x in rows if x["in_effect"]] + waiting, str(rows))
            r = await cx.get(f"{B}/api/grants", headers=KA, params={"project": "nope"})
            ck("an unknown project -> 404", r.status_code == 404, r.text)

            print("-- the MCP tools --")
            who = {"k": KA}
            m._cx = mf._cx = lambda timeout=10: httpx.AsyncClient(timeout=timeout, headers=who["k"])
            m.TARESD = mf.TARESD = B
            out = await mf.add_grant("ship acme/web fixes", kind="project", project=p1, areas=["acme/web"])
            ck("add_grant with areas: it reports a grant waiting for B",
               "waiting" in out and "Person B" in out, out)
            out = await mf.read_grants(p1)
            ck("read_grants lists it under waiting, not in effect",
               "Waiting for consent, not in effect" in out
               and "ship acme/web fixes" in out.split("Waiting for consent")[1]
               and "ship acme/web fixes" not in out.split("Waiting for consent")[0], out)
            ck("... and the team grants and the project's own under In effect, first",
               out.startswith("In effect:") and "run the tests" in out and "deploy to staging" in out, out)
            who["k"] = KC
            gnum = next(x["id"] for x in (await cx.get(f"{B}/api/grants", headers=KA,
                        params={"project": p1})).json()["grants"] if x["words"] == "ship acme/web fixes")
            out = await mf.consent_grant(gnum)
            ck("consent_grant by C is refused", "403" in out or "not one of them" in out, out)
            who["k"] = KB
            out = await mf.consent_grant(gnum)
            ck("consent_grant by B makes it active", "active" in out, out)
            who["k"] = KA
            out = await mf.read_grants(p1)
            ck("read_grants now lists it in effect, no longer waiting",
               "ship acme/web fixes" in out.split("Waiting for consent")[0]
               and "ship acme/web fixes" not in out.partition("Waiting for consent")[2], out)
            who["k"] = KA
            out = await mf.add_grant("by the old keyword", scope="personal")
            ck("add_grant still takes scope= as the older name of kind",
               "personal" in out and "by the old keyword" in out and "TypeError" not in out, out)
            who["k"] = KB
            out = await mf.read_grants()
            ck("read_grants, with no project, lists what waits for you", "Waiting for consent" not in out, out)
            await cx.post(f"{B}/api/grants", headers=KA, json={
                "kind": "project", "project": p4, "words": "secret ask", "areas": ["acme/web"]})
            out = await mf.read_grants()
            ck("... and a grant waiting for you on a project you cannot see",
               "Waiting for consent" in out and "secret ask" in out.split("Waiting for consent")[1], out)
            who["k"] = KD
            out = await mf.add_grant("read the logs", "all")
            ck("the older call add_grant(words, \"all\") is a team grant",
               "team" in out and "read the logs" in out, out)
    finally:
        stop(proc)


async def old_doc_lines():
    print("== read_grants: an old doc line whose grant was revoked stays out ==")
    doc = {"all": '# Grants\n\n- "old words" (2026-10-01)\n- "other old words" (2026-10-01)\n',
           "project": "", "grants": []}

    def serve(extra):
        def handler(request):
            return httpx.Response(200, json={**doc, **extra})
        return lambda timeout=10: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    m._cx = mf._cx = serve({"known_words": ["old words"]})
    out = await mf.read_grants()
    ck("a line whose words belong to a revoked grant is not listed", "old words" not in out.replace("other old words", ""), out)
    ck("... and one that belongs to no grant still is", "other old words" in out, out)
    m._cx = mf._cx = serve({})
    out = await mf.read_grants()
    ck("(control) without known_words both are listed", out.count("old words") == 2, out)


async def auth_off():
    print("== auth off: one person, as before ==")
    proc, B = start(os.path.join(TMP, "off.duckdb"), False)
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up (auth off)", False); return
        async with httpx.AsyncClient(timeout=30) as cx:
            p = (await cx.post(f"{B}/api/projects", json={"template": "custom", "name": "Solo",
                                                          "objects": []})).json()["id"]
            r = await cx.post(f"{B}/api/grants", json={"words": "run the tests"})
            ck("the older call shape {words} adds a team grant",
               r.status_code == 201 and r.json()["grant"]["kind"] == "team", r.text)
            r = await cx.post(f"{B}/api/grants", json={"words": "edit docs", "scope": "project", "project": p})
            ck("... and {words, scope: project, project} a project grant",
               r.status_code == 201 and r.json()["grant"]["kind"] == "project", r.text)
            r = await cx.post(f"{B}/api/grants", json={"kind": "personal", "words": "my machine"})
            ck("a personal grant is for `local`", r.status_code == 201 and r.json()["grant"]["holder"]["id"] == "local", r.text)
            rows = (await cx.get(f"{B}/api/grants", params={"project": p})).json()
            ck("all three are listed, and the docs hold the first two",
               words(rows["grants"]) == ["edit docs", "my machine", "run the tests"]
               and "run the tests" in rows["all"] and "edit docs" in rows["project"], str(rows))
    finally:
        stop(proc)


async def main():
    await migration()
    await auth_on()
    await old_doc_lines()
    await auth_off()


try:
    asyncio.run(main())
finally:
    shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
