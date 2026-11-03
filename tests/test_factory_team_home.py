"""GET /api/home on a cell with more than one person, what is addressed to the caller (TR-739).

A daemon with auth on and the factory edition. First a cell of one person (only `local`): the
body is what TR-729 and TR-769 give, with no `summary` and the single-person kinds. Then three
people with a key each (Alex owns acme/web, Kim owns acme/api and leads the project, Lee owns
nothing). `waiting_for_you` is then the caller's own: a question a spec asked of Alex, a sign-off
Alex owes before the project goes to a crew, a grant Kim gave over Alex's repo, a desk item
addressed to Alex (a question, and a permission with an Allow action), a milestone Kim leads and a
pull request in Kim's project. Each carries who it comes from, ends when it is done, and is not in
Kim's or Lee's list. Every item is of a known kind, an action is a link or a thing to do, and
every string passes the words check and holds no em dash.

Run: .venv/bin/python tests/test_factory_team_home.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-team-home-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-team-home"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"

import httpx

from tares import factory as factory_mod
from tares import factory_home as fh
from tares import factory_team
from tares import words
from tares.store import now_utc

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def strings(v):
    """Every string in a body, with the path it sits at."""
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from strings(x)
    elif isinstance(v, list):
        for x in v:
            yield from strings(x)


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    root = {"Authorization": f"Bearer {TOKEN}"}
    keys, pid = {}, {}
    SEEN = {}   # every item any home showed during the run, by id

    def ago(**kw):
        return now_utc() - timedelta(**kw)

    async def call(who, method, path, body=None, **params):
        hdr = root if who == "root" else keys[who]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     headers=hdr) as cx:
            return await cx.request(method, path, json=body, params=params or None)

    async def home(who):
        fh._cache.clear()
        r = await call(who, "GET", "/api/home")
        assert r.status_code == 200, r.text
        for i in r.json()["waiting_for_you"]:
            SEEN[i["id"]] = i
        SEEN["summary:" + who] = {"summary": r.json().get("summary") or ""}
        return r.json()

    def by_kind(h):
        out = {}
        for i in h["waiting_for_you"]:
            out.setdefault(i["kind"], []).append(i)
        return out

    async def make_project(who, name):
        r = await call(who, "POST", "/api/projects", {"template": "custom", "name": name,
                                                      "objects": [], "kind": "software_factory"})
        assert r.status_code in (200, 201), r.text
        return r.json()["id"]

    async def ticket(who, uid, title, repo=None, **fields):
        r = await call(who, "POST", f"/api/projects/{uid}/tickets", {"title": title, "repo": repo})
        assert r.status_code == 201, r.text
        tid = r.json()["id"]
        if fields:
            store.update_ticket(tid, **fields)
        return tid

    async with app.router.lifespan_context(app):
        print("== a cell of one person: the body is the single-person one ==")
        ck("only local is on the cell", store.only_local_person())
        solo = await make_project("root", "solo")
        await ticket("root", solo, "Solo work", "solo/app")
        store.add_desk_item(solo, "solo was refused a command. Allow it?", "", [],
                            "solo may read a status", "it needs it", "", [], False,
                            "crew-orchestrator", [], ago(hours=2), station="solo-build-m1",
                            command="gh api repos/acme/web/statuses/abc")
        store.add_desk_item(solo, "Which colour for the links?", "", ["blue"], "blue", "it reads best",
                            "", [], False, "crew-orchestrator", [], ago(hours=1))
        one = await home("root")
        ck("no summary key", "summary" not in one, sorted(one))
        ck("the single-person kinds, not the team ones", {i["kind"] for i in one["waiting_for_you"]}
           == {"permission", "question"}, [i["kind"] for i in one["waiting_for_you"]])
        real = factory_team.team_home
        factory_team.team_home = lambda c, request, now: {}
        try:
            plain = await home("root")
        finally:
            factory_team.team_home = real
        drop = ("now", "start")
        ck("the body equals what the home gives with no team part", (
            {k: v for k, v in one.items() if k not in drop}
            == {k: v for k, v in plain.items() if k not in drop}))
        ck("team_home itself answers {} for a cell of one", factory_team.team_home(
            type("C", (), {"store": store})(), None, now_utc()) == {})
        ck("a key with no person is the cell's own key: same body", (await home("root"))["waiting_total"]
           == one["waiting_total"])

        print("== a cell of three people ==")
        for who in ("alex", "kim", "lee"):
            r = await call("root", "POST", "/api/keys", {"name": f"{who}-laptop", "scopes": ["admin"],
                                                         "person": {"email": f"{who}-th@example.com",
                                                                    "name": who.capitalize()}})
            keys[who] = {"Authorization": f"Bearer {r.json()['secret']}"}
            pid[who] = r.json()["person"]["id"]
        ck("three people, each with a key", len(set(pid.values())) == 3 and not store.only_local_person())
        for area, who in (("acme/api", "kim"), ("acme/web", "alex")):
            r = await call("kim", "PUT", f"/api/areas/{area}", {"person_id": pid[who]})
            ck(f"{area} is {who.capitalize()}'s", r.status_code < 300, r.text)
        for who in ("alex", "kim", "lee"):
            h = await home(who)
            ck(f"{who.capitalize()} starts with nothing waiting", h["waiting_for_you"] == []
               and h["waiting_total"] == 0 and h["summary"] == "Nothing waits for you.", h)

        shop = await make_project("kim", "Shop")
        ck("Kim leads Shop", store.get_project(shop)["lead_person_id"] == pid["kim"])
        await ticket("kim", shop, "Web page", "acme/web")
        await ticket("kim", shop, "Api route", "acme/api", stage="merged", status="done")

        print("== a question a spec asked ==")
        r = await call("kim", "POST", f"/api/projects/{shop}/questions",
                       {"repo": "acme/web", "text": "Which page does the checkout live on?"})
        ck("Kim asks the owner of acme/web", r.status_code == 201, r.text)
        qid = r.json()["id"]
        k = by_kind(await home("alex"))
        q = (k.get("question") or [{}])[0]
        ck("it is in Alex's list as a question", len(k.get("question", [])) == 1
           and q["title"] == f"Shop, {qid}" and q["detail"] == "Which page does the checkout live on?"
           and q["repo"] == "acme/web" and q["project"] == {"id": shop, "name": "Shop"}, k)
        ck("it comes from Kim", q["from"] == {"id": pid["kim"], "name": "Kim"}, q.get("from"))
        ck("with one Answer action that does answer", q["actions"] == [
            {"label": "Answer", "primary": True, "do": "answer", "project": shop, "question": qid}],
           q.get("actions"))
        ck("not in Kim's or Lee's", "question" not in by_kind(await home("kim"))
           and "question" not in by_kind(await home("lee")))
        row = lambda h: next(p for p in h["projects"] if p["id"] == shop)
        ck("Shop's row reads 'waiting on you' for Alex and not for Kim", row(await home("alex"))["state"]
           == "waiting_on_you" and row(await home("kim"))["state"] != "waiting_on_you",
           (row(await home("alex"))["state"], row(await home("kim"))["state"]))
        r = await call("alex", "POST", f"/api/projects/{shop}/questions/{qid}/answer", {"answer": "The cart"})
        ck("Alex answers it", r.status_code == 200, r.text)
        ck("it is gone", "question" not in by_kind(await home("alex")))

        print("== a sign-off owed before the project goes to a crew ==")
        k = by_kind(await home("alex"))
        so = (k.get("signoff") or [{}])[0]
        ck("Alex owes one for acme/web", len(k.get("signoff", [])) == 1 and so["repo"] == "acme/web"
           and so["title"] == "Shop" and so["project"] == {"id": shop, "name": "Shop"}, k)
        ck("it comes from Kim and says why", so["from"] == {"id": pid["kim"], "name": "Kim"}
           and so["detail"] == "1 ticket touches acme/web. Kim cannot hand the project to a crew "
                               "until you sign off.", so.get("detail"))
        ck("it can be signed, and the ticket read", so["actions"] == [
            {"label": "Sign off", "primary": True, "do": "sign_off", "project": shop,
             "repo": "acme/web"},
            {"label": "Read the ticket", "primary": False, "href": f"/projects/{shop}"}], so["actions"])
        ck("Kim owns acme/api and leads: nothing owed there, nothing for Kim or Lee",
           "signoff" not in by_kind(await home("kim")) and "signoff" not in by_kind(await home("lee")))
        r = await call("alex", "POST", f"/api/projects/{shop}/signoffs", {"repo": "acme/web"})
        ck("Alex signs off", r.status_code == 201, r.text)
        ck("it is gone", "signoff" not in by_kind(await home("alex")))

        print("== a grant that waits for the owner ==")
        r = await call("kim", "POST", "/api/grants", {"kind": "project", "project": shop,
                                                      "words": "merge to acme/web",
                                                      "areas": ["acme/web"]})
        ck("Kim gives a grant over Alex's repo, waiting", r.status_code == 201
           and r.json()["grant"]["state"] == "waiting", r.text)
        gid = r.json()["grant"]["id"]
        k = by_kind(await home("alex"))
        g = (k.get("grant") or [{}])[0]
        ck("it is in Alex's list as a grant", len(k.get("grant", [])) == 1
           and g["title"] == "merge to acme/web" and g["repo"] == "acme/web"
           and g["project"] == {"id": shop, "name": "Shop"}, k)
        ck("from Kim, with the reason it waits", g["from"] == {"id": pid["kim"], "name": "Kim"}
           and g["detail"] == "Kim gave this for acme/web, which you own. It takes effect when you "
                              "agree.", g.get("detail"))
        ck("Agree and Decline are things to do", g["actions"] == [
            {"label": "Agree", "primary": True, "do": "agree", "grant": gid},
            {"label": "Decline", "primary": False, "do": "decline", "grant": gid}], g["actions"])
        ck("not in Kim's or Lee's", "grant" not in by_kind(await home("kim"))
           and "grant" not in by_kind(await home("lee")))
        r = await call("alex", "POST", f"/api/grants/{gid}/consent")
        ck("Alex agrees", r.status_code < 300, r.text)
        ck("it is gone", "grant" not in by_kind(await home("alex")))

        print("== the desk: items addressed to the person ==")
        perm = store.add_desk_item(
            shop, "shop-build-m1 was refused a command. Allow it?", "", [],
            "shop-build-m1 may read the status of a commit", "it needs it to wait for CI", "", [],
            False, "crew-orchestrator", [], ago(hours=3), station="shop-build-m1",
            command="gh api repos/acme/web/statuses/abc", to_person_id=pid["alex"])
        ask = store.add_desk_item(
            shop, "Which colour for the links?", "", ["blue", "green"], "blue", "it reads best", "",
            [], False, "crew-orchestrator", [], ago(hours=1), to_person_id=pid["alex"])
        store.add_desk_item(
            shop, "Rename the repo?", "", [], "no", "it is fine", "", [], False,
            "crew-orchestrator", [], ago(hours=1), to_person_id=pid["lee"])
        batch = store.add_desk_item(
            shop, "A batch of calls to keep?", "", [], "keep them", "defaults", "", [], False,
            "crew-orchestrator", ["A1"], ago(hours=5), to_person_id=pid["alex"])
        k = by_kind(await home("alex"))
        ck("Alex has three desk items and no single-person permission or question kinds",
           len(k.get("desk", [])) == 3 and "permission" not in k and "question" not in k,
           [i["kind"] for i in (await home("alex"))["waiting_for_you"]])
        ck("the oldest comes first", [i["id"] for i in k["desk"]] == [
            f"desk:{batch['label']}", f"desk:{perm['label']}", f"desk:{ask['label']}"],
           [i["id"] for i in k["desk"]])
        dp = next(i for i in k["desk"] if i["id"] == f"desk:{perm['label']}")
        dq = next(i for i in k["desk"] if i["id"] == f"desk:{ask['label']}")
        ck("the permission offers Allow, and says which crew station wants what",
           dp["detail"] == "The builder wants to use: `gh api repos/acme/web/statuses/abc`"
           and [a.get("do") for a in dp["actions"]] == ["answer", "allow"]
           and dp["actions"][1] == {"label": "Allow", "primary": True, "do": "allow",
                                    "desk": perm["label"], "may_limit": True}, dp)
        ck("the question has one primary Answer", dq["detail"] == "Which colour for the links?"
           and dq["actions"] == [{"label": "Answer", "primary": True, "do": "answer",
                                  "desk": ask["label"]}], dq)
        ck("they come from Alex's own crew", dp["from"] == {"crew": True, "name": "Your crew"}
           and dq["from"]["name"] in ("Your crew", "Alex", "Kim", "Lee"), (dp["from"], dq["from"]))
        ck("Lee has only the item for Lee, Kim has none", len(by_kind(await home("lee")).get("desk", [])) == 1
           and "desk" not in by_kind(await home("kim")))
        r = await call("alex", "POST", f"/api/desk/{perm['label']}/allow", {
            "words": "shop-build-m1 may read the status of a commit", "scope": "project"})
        ck("Alex allows the permission", r.status_code == 200, r.text)
        ck("the permission is gone and the others stay", len(by_kind(await home("alex")).get("desk", [])) == 2)
        for d in store.list_desk("open"):
            store.close_desk_item(d["id"], "answered", None, now_utc())
        ck("answered, the desk items are gone", "desk" not in by_kind(await home("alex")))

        store.set_handover(shop, [{"path": "/w/shop", "name": "acme/api"}], None, None, now_utc())
        print("== a milestone the lead can check ==")
        r = await call("kim", "POST", f"/api/projects/{shop}/milestones", {
            "name": "M1: pages", "checks": [{"check": "open the page", "expect": "it loads"}]})
        ck("Kim adds a milestone", r.status_code < 300, r.text)
        mid = store.milestones_by_project([shop])[shop][0]["id"]
        t1 = await ticket("kim", shop, "Milestone ticket", "acme/api", milestone=mid, stage="merged",
                          status="done")
        k = by_kind(await home("kim"))
        ms = (k.get("milestone_to_check") or [{}])[0]
        ck("Kim leads Shop, so it is Kim's", len(k.get("milestone_to_check", [])) == 1
           and ms["title"] == "Shop, M1: pages" and ms["from"] == {"crew": True, "name": "Your crew"}
           and ms["detail"] == "Every ticket of this milestone merged. Try its 1 check."
           and ms["actions"] == [{"label": "Check it", "primary": True,
                                  "href": f"/projects/{shop}#milestone-{mid}"}], k)
        ck("not Alex's or Lee's", "milestone_to_check" not in by_kind(await home("alex"))
           and "milestone_to_check" not in by_kind(await home("lee")))

        print("== a pull request that passed review ==")
        pr_t = await ticket("kim", shop, "Add the footer", "acme/api", stage="review",
                            pr={"url": "https://github.com/acme/api/pull/7", "repo": "acme/api",
                                "head": "aaaaaaa1111"})
        store.add_review(shop, pr_t, None, "aaaaaaa1111", "pass", "ran the tests", None,
                         "crew-reviewer", [], {}, factory_mod.finding_kind, now_utc())
        k = by_kind(await home("kim"))
        pr = (k.get("prs_to_review") or [{}])[0]
        ck("it is in Kim's list, Kim leads Shop", len(k.get("prs_to_review", [])) == 1
           and pr["project"] == {"id": shop, "name": "Shop"}, k)
        ck("not in Lee's, who is not in Shop", "prs_to_review" not in by_kind(await home("lee")))

        print("== a private project the person is not on ==")
        vault = await make_project("lee", "Vault")
        r = await call("lee", "PATCH", f"/api/projects/{vault}", {"scope": "private"})
        ck("Lee makes Vault private", r.status_code < 300 and store.get_project(vault)["scope"] == "private",
           r.text)
        await ticket("lee", vault, "Rotate the secret keys", "acme/web")
        store.add_desk_item(vault, "Rotate the vault keys?", "", [], "yes", "why", "", [], False,
                            "crew-orchestrator", [], ago(minutes=3), to_person_id=pid["alex"])
        for who in ("alex", "kim"):
            dump = json.dumps((await home(who))["waiting_for_you"])
            ck(f"{who.capitalize()}: nothing of Vault is in the items", not any(
                w in dump for w in ("Vault", "secret keys", "vault keys", vault)),
               [w for w in ("Vault", "secret keys", "vault keys", vault) if w in dump])
        ck("Lee still sees his own project's ticket in his home",
           any(p["id"] == vault for p in (await home("lee"))["projects"]))

        print("== the whole answer ==")
        for who in ("alex", "kim", "lee"):
            h = await home(who)
            w = h["waiting_for_you"]
            ck(f"{who.capitalize()}: the total is the list, the summary counts it",
               h["waiting_total"] == len(w) and h["summary"] == factory_team.summary(w), h["summary"])
        h = await home("kim")
        ck("Kim's list is oldest first", [i["since"] for i in h["waiting_for_you"]]
           == sorted(i["since"] for i in h["waiting_for_you"]))
        ck("the summary has a sentence for none, one and many", [factory_team.summary([{}] * n) for n in (0, 1, 4)]
           == ["Nothing waits for you.", "1 thing waits for you.", "4 things wait for you."])
        h = await home("root")
        ck("the cell's own key is the person local: nothing addressed to them, and every pull "
           "request, as the key of a cell of one sees it", [i["kind"] for i in h["waiting_for_you"]]
           == ["prs_to_review"] and h["summary"] == "1 thing waits for you.", h["waiting_for_you"])
        print("== two keys of one name, two people ==")
        for who in ("alex", "lee"):
            r = await call("root", "POST", "/api/keys", {"name": "shared-name", "scopes": ["admin"],
                                                         "person": {"email": f"{who}-th@example.com"}})
            keys[who + "2"] = {"Authorization": f"Bearer {r.json()['secret']}"}
        store.add_desk_item(shop, "Cache check for Alex?", "", [], "yes", "why", "", [], False,
                            "crew-orchestrator", [], ago(minutes=5), to_person_id=pid["alex"])
        fh._cache.clear()
        titles = {}
        for who in ("alex2", "lee2"):    # no cache clear between them: the home is kept for a few seconds
            r = await call(who, "GET", "/api/home")
            titles[who] = [i["detail"] for i in r.json()["waiting_for_you"]]
        ck("each gets their own home", titles["alex2"] == ["Cache check for Alex?"] and titles["lee2"] == [],
           titles)

        print("== kinds, actions and words ==")
        items = [i for i in SEEN.values() if "kind" in i]
        ck("the run saw items of every team kind", {i["kind"] for i in items} >= {
            "question", "signoff", "grant", "desk", "milestone_to_check", "prs_to_review"},
           sorted({i["kind"] for i in items}))
        for i in items:
            ck(f"{i['id']} has a known kind", i["kind"] in fh.KINDS, i["kind"])
            for a in i["actions"]:
                ck(f"{i['id']} / {a['label']}: a link or a thing to do, never both",
                   (("href" in a) != ("do" in a)) and ("do" not in a or a["do"] in fh.ACTIONS), a)
        text = list(strings(list(SEEN.values())))
        bad = [(s, words.offenders(s)) for s in text if words.offenders(s)]
        ck("every string passes the words check", not bad, bad[:3])
        ck("no em dash in any string", not [s for s in text if "\u2014" in s])

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())
