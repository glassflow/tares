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
from types import SimpleNamespace

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
    BODIES = []   # every whole home body the run read

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
        BODIES.append(r.json())
        return r.json()

    async def home_of(who, view=None):
        """The home with the projects view asked for (mine is the default)."""
        fh._cache.clear()
        r = await call(who, "GET", "/api/home", **({"projects": view} if view else {}))
        assert r.status_code == 200, r.text
        BODIES.append(r.json())
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


        print("== part two: who the person waits on ==")
        atlas = await make_project("kim", "Atlas")
        t_web = await ticket("kim", atlas, "Web part", "acme/web")
        await ticket("kim", atlas, "Api part", "acme/api")
        await ticket("kim", atlas, "Lib part", "other/lib")
        r = await call("kim", "POST", f"/api/projects/{atlas}/questions",
                       {"repo": "acme/web", "text": "Is the cart in scope?"})
        ck("Kim asks Alex a question in Atlas", r.status_code == 201, r.text)
        aq = r.json()["id"]
        h = await home("kim")
        on = {e["project"]["id"]: e for e in h["you_are_waiting_on"]}
        e = on.get(atlas) or {}
        ck("Kim waits on one project: Atlas (Shop went to a crew, no other project depends on anyone)",
           list(on) == [atlas] and e["title"] == "Atlas" and e["project"] == {"id": atlas, "name": "Atlas"},
           list(on))
        st = {r_["repo"]: (r_["state"], r_["owner"]) for r_ in e["repos"]}
        ck("each repo has its owner and state: one Kim's own, one waiting, one with no owner", st == {
            "acme/api": ("yours", {"id": pid["kim"], "name": "Kim"}),
            "acme/web": ("waiting", {"id": pid["alex"], "name": "Alex"}),
            "other/lib": ("no_owner", None)}, st)
        ck("the open question names who it is for and how long it has waited",
           [(q["id"], q["to"], q["waited"]) for q in e["open_questions"]]
           == [(aq, {"id": pid["alex"], "name": "Alex"}, "just now")]
           and e["open_questions"][0]["asked_at"], e["open_questions"])
        ck("the detail says in words what is missing", e["detail"] == (
            "Waiting for Alex to sign off on acme/web. other/lib has no owner. 1 question open for Alex."),
           e["detail"])
        ck("Hand over anyway and Set an owner are offered", e["actions"] == [
            {"label": "Hand over anyway", "primary": False, "do": "hand_over_anyway", "project": atlas},
            {"label": "Set an owner", "primary": False, "href": "/settings/advanced"}], e["actions"])
        ck("the summary adds who Kim waits on, a sign-off and a question each counting one",
           h["summary"] == factory_team.summary(h["waiting_for_you"], [("Alex", 2)])
           and h["summary"].endswith("You are waiting on Alex for 2."), h["summary"])
        ck("Alex and Lee lead nothing: an empty list, and no second sentence",
           (await home("alex"))["you_are_waiting_on"] == [] and (await home("lee"))["you_are_waiting_on"] == []
           and "waiting on" not in (await home("alex"))["summary"])
        ck("the cell's one-person home never has the key", "you_are_waiting_on" not in one)
        ck("Atlas has not started, but a question and a sign-off wait for Alex in it: in his mine view, "
           "not in Lee's", atlas in [p["id"] for p in (await home_of("alex"))["projects"]]
           and atlas not in [p["id"] for p in (await home_of("lee"))["projects"]])

        r = await call("alex", "POST", f"/api/projects/{atlas}/questions/{aq}/answer", {"answer": "Yes"})
        await call("alex", "POST", f"/api/projects/{atlas}/signoffs", {"repo": "acme/web"})
        e = {x["project"]["id"]: x for x in (await home("kim"))["you_are_waiting_on"]}[atlas]
        st = {r_["repo"]: r_["state"] for r_ in e["repos"]}
        ck("Alex signs off and answers: the lead sees signed_off, the question is gone",
           st["acme/web"] == "signed_off" and e["open_questions"] == []
           and e["detail"] == "other/lib has no owner.", (st, e["detail"], e["open_questions"]))
        ck("Hand over anyway stays for the repo with no owner only through Set an owner",
           [a["label"] for a in e["actions"]] == ["Hand over anyway", "Set an owner"], e["actions"])
        r = await call("kim", "PUT", "/api/areas/other/lib", {"person_id": pid["kim"]})
        ck("Kim takes other/lib", r.status_code < 300, r.text)
        h = await home("kim")
        ck("everyone signed off and nothing asked: the project is no longer waited on",
           atlas not in {x["project"]["id"] for x in h["you_are_waiting_on"]}
           and h["summary"] == factory_team.summary(h["waiting_for_you"]), h["summary"])
        solo2 = await make_project("kim", "Kim only")
        await ticket("kim", solo2, "Own repo work", "acme/api")
        ck("a project whose repos are all Kim's, with nothing asked, is left out",
           solo2 not in {x["project"]["id"] for x in (await home("kim"))["you_are_waiting_on"]})
        await call("kim", "POST", f"/api/projects/{atlas}/questions",
                   {"repo": "acme/web", "text": "Second one?"})
        await call("kim", "POST", f"/api/projects/{solo2}/questions",
                   {"repo": "acme/web", "text": "A ticket for you?"})
        await ticket("kim", solo2, "Web work", "acme/web")
        h = await home("kim")
        ck("a second question in two projects: both listed, the sentence counts both for Alex",
           {x["project"]["id"] for x in h["you_are_waiting_on"]} == {atlas, solo2}
           and h["summary"].endswith("You are waiting on Alex for 3."), (h["summary"]))
        ck("the counts sentence for several people names each, most first",
           factory_team.summary([], [("Alex", 2), ("Lee", 1)])
           == "Nothing waits for you. You are waiting on Alex for 2 and Lee for 1.")
        for uid in (atlas, solo2):
            for q in store.list_spec_questions(uid, state="open"):
                r = await call("alex", "POST", f"/api/projects/{uid}/questions/{q['id']}/answer", {"answer": "ok"})
                assert r.status_code == 200, r.text
        ck("Alex answers them all", not store.list_spec_questions(atlas, state="open")
           and not store.list_spec_questions(solo2, state="open"))

        print("== part two: the project list, lead, members, scope, mine and team ==")
        store.update_ticket(t_web, assignee_person_id=pid["alex"])
        r = await call("kim", "POST", f"/api/projects/{atlas}/members", {"person_id": pid["lee"]})
        ck("Kim puts Lee on Atlas", r.status_code < 300, r.text)
        h = await home_of("kim")
        a = next(p for p in h["projects"] if p["id"] == atlas)
        ck("Atlas for Kim: lead, members besides the lead, scope, her tickets in words",
           a["lead"] == {"id": pid["kim"], "name": "Kim"} and a["members"] == [{"id": pid["lee"], "name": "Lee"}]
           and a["scope"] == "team" and a["yours"] == 2 and a["yours_text"] == "2 tickets yours, 1 Alex's"
           and a["mine"] is True, a)
        ck("the rows keep the single-person fields too", a["name"] == "Atlas" and a["state"]
           and "tickets" in a and "repos" in a and a["can_close"] in (True, False), sorted(a))
        h = await home_of("alex", "mine")
        a = next((p for p in h["projects"] if p["id"] == atlas), {})
        ck("Atlas for Alex (one ticket assigned to him): mine, '1 ticket yours, 2 Kim's'",
           a.get("mine") is True and a["yours"] == 1 and a["yours_text"] == "1 ticket yours, 2 Kim's", a)
        h = await home_of("lee")
        ck("Lee is on Atlas but holds no ticket: not in his mine view, in his team view and counts",
           atlas not in [p["id"] for p in h["projects"]]
           and atlas in [p["id"] for p in (await home_of("lee", "team"))["projects"]]
           and h["counts"]["mine"] == 0 and h["counts"]["team"] >= 2, h["counts"])
        hk, ht = await home_of("kim"), await home_of("kim", "team")
        am, at = await home_of("alex", "mine"), await home_of("alex", "team")
        ck("an unknown view reads as mine (Alex's two views differ)",
           [p["id"] for p in am["projects"]] != [p["id"] for p in at["projects"]]
           and [p["id"] for p in (await home_of("alex", "everything"))["projects"]]
           == [p["id"] for p in am["projects"]], (len(am["projects"]), len(at["projects"])))
        ck("Kim's counts are the same in both views and mine is a part of team",
           hk["counts"] == ht["counts"] and hk["counts"]["mine"] <= hk["counts"]["team"]
           and len(hk["projects"]) == hk["counts"]["mine"] and len(ht["projects"]) == hk["counts"]["team"],
           (hk["counts"], len(hk["projects"]), len(ht["projects"])))
        ck("every row of Kim's mine view is in her team view", {p["id"] for p in hk["projects"]} <= {p["id"] for p in ht["projects"]})
        fh._cache.clear()
        r1 = (await call("kim", "GET", "/api/home")).json()
        r2 = (await call("kim", "GET", "/api/home", projects="team")).json()
        r3 = (await call("kim", "GET", "/api/home")).json()
        ck("the home cache is kept per view: team, then mine again, is mine",
           len(r1["projects"]) == r1["counts"]["mine"] and len(r2["projects"]) == r2["counts"]["team"]
           and len(r3["projects"]) == r1["counts"]["mine"])
        req = SimpleNamespace(state=SimpleNamespace(credential={"name": "k", "person": {"id": "p"}}),
                              query_params={})
        req_team = SimpleNamespace(state=req.state, query_params={"projects": "team"})
        fh._cache.clear()
        fh._cache[fh._cache_key(store, req)] = (0, {})
        fh._cache[fh._cache_key(store, req_team)] = (0, {})
        fh._cache[fh._cache_key(store, SimpleNamespace(
            state=SimpleNamespace(credential={"name": "x", "person": {"id": "q"}}), query_params={}))] = (0, {})
        fh.drop_cache(store, req)
        ck("dropping a credential's kept home drops its mine and team views, not another person's",
           len(fh._cache) == 1 and next(iter(fh._cache))[1] == "x", list(fh._cache))
        fh._cache.clear()
        empty = await make_project("kim", "Fresh idea")
        ck("a project Kim leads with no ticket and nothing asked is still hers",
           empty in [p["id"] for p in (await home_of("kim"))["projects"]])
        store.add_project_member(atlas, pid["kim"])   # the lead on the member list too
        dropped = await ticket("kim", atlas, "Dropped work", "acme/api", status="canceled")
        a = next(p for p in (await home_of("kim"))["projects"] if p["id"] == atlas)
        ck("the lead is not listed among the members, and a canceled ticket is not counted",
           a["members"] == [{"id": pid["lee"], "name": "Lee"}] and a["yours"] == 2
           and a["yours_text"] == "2 tickets yours, 1 Alex's", a)

        print("== part two: a private project the person is not on ==")
        r = await call("lee", "POST", "/api/projects", {"template": "custom", "name": "Vault",
                                                        "objects": [], "kind": "software_factory",
                                                        "goal": "Hide the launch codes"})
        assert r.status_code in (200, 201), r.text
        vault = r.json()["id"]
        r = await call("lee", "PATCH", f"/api/projects/{vault}", {"scope": "private"})
        ck("Lee makes Vault private", r.status_code < 300 and store.get_project(vault)["scope"] == "private", r.text)
        await ticket("lee", vault, "Rotate the secret keys", "acme/web")
        r = await call("lee", "POST", f"/api/projects/{vault}/questions", {"repo": "acme/web",
                                                                         "text": "Who holds the codes?"})
        ck("Lee cannot ask Alex, who is not on his private project", r.status_code >= 400, r.text)
        for who in ("alex", "kim"):
            for view in ("mine", "team"):
                h = await home_of(who, view)
                dump = json.dumps(h)
                ck(f"{who.capitalize()} / {view}: nothing of Vault is anywhere in the body",
                   not any(w in dump for w in ("Vault", "launch codes", "secret keys", "vault keys", vault)),
                   [w for w in ("Vault", "launch codes", "secret keys", "vault keys", vault) if w in dump])
        h = await home_of("alex", "team")
        ck("the team view ends with one stub: hidden, and its lead's name only",
           h["projects"][-1] == {"hidden": True, "lead": {"name": "Lee"}}
           and sum(1 for p in h["projects"] if p.get("hidden")) == 1, h["projects"][-1])
        ck("hidden_summary says how many and whose", h["hidden_summary"]
           == "1 private project you are not on, led by Lee", h.get("hidden_summary"))
        ck("counts.team leaves the hidden one out", h["counts"]["team"] == len(h["projects"]) - 1, h["counts"])
        h = await home_of("alex", "mine")
        ck("the mine view has no stub, only the summary", not any(p.get("hidden") for p in h["projects"])
           and h["hidden_summary"] == "1 private project you are not on, led by Lee")
        h = await home_of("lee", "team")
        ck("Lee leads it: his own row, no stub, no summary", next(p for p in h["projects"] if p["id"] == vault)
           ["scope"] == "private" and not any(p.get("hidden") for p in h["projects"])
           and "hidden_summary" not in h)
        store.add_desk_item(vault, "Rotate the vault keys?", "", [], "yes", "why", "", [], False,
                            "crew-orchestrator", [], ago(minutes=3), to_person_id=pid["alex"])
        ck("a desk item addressed to Alex in a project he is not on is not in his list",
           "vault keys" not in json.dumps(await home_of("alex")))
        ck("a hidden project's desk item is not counted in the crew's open questions for Alex",
           (await home_of("lee"))["crew"]["desk_open"] == (await home_of("alex"))["crew"]["desk_open"] + 1,
           [(await home_of(w))["crew"]["desk_open"] for w in ("lee", "alex")])
        two = [{"repos": [{"state": "waiting", "owner": {"id": "p1", "name": "Sam"}}], "open_questions": []},
               {"repos": [{"state": "waiting", "owner": {"id": "p2", "name": "Sam"}}],
                "open_questions": [{"to": {"id": "p1", "name": "Sam"}}]}]
        ck("two people of one name are two entries in the waiting counts",
           factory_team.waiting_counts(two) == [("Sam", 2), ("Sam", 1)],
           factory_team.waiting_counts(two))
        ck("the stub has the same shape in two homes with no other key", all(
            set(p) == {"hidden", "lead"} for p in (await home_of("kim", "team"))["projects"] if p.get("hidden")))

        print("== part two: the crews ==")
        r = await call("root", "POST", "/api/sources", {"name": "claude_code", "connector": "claude_code",
                                                         "poll": "10s", "config": {"push": True}})
        ck("the claude_code source is made", r.status_code < 300, r.text)

        def st_line(sid, station, role, ago_min, crew, machine, project=""):
            o = {"type": "session_station", "sessionId": sid, "cwd": "/w/crew",
                 "timestamp": (now_utc() - timedelta(minutes=ago_min)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                 "station": station, "role": role, "parent": "", "factory_station": station,
                 "factory_crew": crew, "crew": crew, "factory_machine": machine, "machine": machine}
            if project:
                o["tares_project"] = project
            return o

        def state_line(sid, st_, ago_min):
            return {"type": "session_state", "sessionId": sid, "cwd": "/w/crew", "state": st_, "reason": "",
                    "timestamp": (now_utc() - timedelta(minutes=ago_min)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"}

        async def push(who, lines):
            hdr = keys[who]
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                         headers={**hdr, "Content-Type": "application/x-ndjson"}) as cx:
                r = await cx.post("/ingest/claude_code", content="\n".join(json.dumps(x) for x in lines) + "\n")
            ck(f"{who}'s crew lines are taken in", r.status_code < 300, r.text)

        await push("alex", [st_line("a1", "a-orchestrator", "orchestrator", 10, "a", "m1"),
                            st_line("a2", "a-build-m1", "builder", 4, "a", "m1", project=atlas),
                            state_line("a2", "working", 4)])
        await push("kim", [st_line("k1", "k-orchestrator", "orchestrator", 6, "k", "m3"),
                           state_line("k1", "idle", 6)])
        await push("lee", [st_line("l1", "l-orchestrator", "orchestrator", 2 * 24 * 60, "l", "m2"),
                           state_line("l1", "idle", 2 * 24 * 60)])
        h = await home_of("alex")
        names = [c["name"] for c in h["crews"]]
        ck("Alex: his own crew first, then Kim's; Lee's, silent for two days, is named apart",
           names == ["Your crew", "Kim's crew"] and h["crews_quiet"] == ["Lee's crew"], (names, h["crews_quiet"]))
        mine_c = h["crews"][0]
        ck("his crew: machine, person, how many work, and what they are doing",
           mine_c["machine"] == "m1" and mine_c["person"] == {"id": pid["alex"], "name": "Alex"}
           and mine_c["working"] == 1 and mine_c["doing"] == "Building Atlas."
           and mine_c["heard"] == "4 minutes ago" and mine_c["last_seen"], mine_c)
        ck("Kim's crew is idle", h["crews"][1]["doing"] == "Idle." and h["crews"][1]["working"] == 0
           and h["crews"][1]["person"] == {"id": pid["kim"], "name": "Kim"}, h["crews"][1])
        h = await home_of("lee")
        ck("Lee sees his own silent crew as 'Your crew' among the quiet ones",
           [c["name"] for c in h["crews"]] == ["Alex's crew", "Kim's crew"], [c["name"] for c in h["crews"]])
        ck("... and the quiet list says it is his", h["crews_quiet"] == ["Your crew"], h["crews_quiet"])
        h = await home_of("kim")
        ck("Kim: her crew first, Alex's second, Lee's quiet",
           [c["name"] for c in h["crews"]] == ["Your crew", "Alex's crew"]
           and h["crews_quiet"] == ["Lee's crew"], ([c["name"] for c in h["crews"]], h["crews_quiet"]))

        print("== part two: how far a crew may go ==")
        h = await home_of("alex")
        ck("with no limit set, the team allows up to merge and Alex's is the same, in words",
           h["crew_limit"] == {"mine": "you approve each merge", "team": "you approve each merge",
                               "text": "The team allows up to merge. Yours can be stricter, not looser."},
           h["crew_limit"])
        r = await call("root", "PUT", "/api/crew/settings", {"autonomy": "L4-ship"})
        ck("the team lets the crew merge on its own", r.status_code == 200, r.text)
        r = await call("alex", "PUT", "/api/crew/settings", {"autonomy": "L3-review"})
        ck("Alex keeps his own at review", r.status_code == 200, r.text)
        cl = (await home_of("alex"))["crew_limit"]
        ck("the team's limit and Alex's own, apart", cl["team"] == "a builder merges after the reviewer's pass "
           "and green CI" and cl["mine"] == "you approve each merge" and cl["text"] == (
               "The team allows merging on its own after review and green CI. Yours can be stricter, "
               "not looser."), cl)
        cl = (await home_of("kim"))["crew_limit"]
        ck("Kim has set none: hers follows the team's", cl["mine"] == cl["team"], cl)

        print("== part two: the repos a person owns ==")
        h = await home_of("alex")
        ck("Alex owns acme/web: sign-offs and questions that wait for him are in the note",
           [a["repo"] for a in h["areas"]] == ["acme/web"], h["areas"])
        ck("Kim owns acme/api and other/lib, sorted, nothing waiting for her there",
           (await home_of("kim"))["areas"] == [{"repo": "acme/api", "note": "nothing waiting"},
                                               {"repo": "other/lib", "note": "nothing waiting"}])
        ck("Lee owns nothing: an empty list", (await home_of("lee"))["areas"] == [])
        await call("kim", "POST", f"/api/projects/{atlas}/questions", {"repo": "acme/web", "text": "Third?"})
        n_open = len(by_kind(await home("alex")).get("question", []))
        note = next(a for a in (await home_of("alex"))["areas"] if a["repo"] == "acme/web")["note"]
        ck("an open question shows in the repo's note", n_open == 1
           and note == "1 sign-off asked, 1 question open", note)

        print("== part two: the new keys pass the same checks ==")
        for who in ("alex", "kim", "lee"):
            body = await home_of(who)
            for k_ in ("you_are_waiting_on", "crews", "crews_quiet", "crew_limit", "areas", "counts"):
                ck(f"{who.capitalize()}: {k_} is in the body", k_ in body)
            for e in body["you_are_waiting_on"]:
                for a in e["actions"]:
                    ck(f"{e['title']} / {a['label']}: a link or a thing to do, never both",
                       (("href" in a) != ("do" in a)) and ("do" not in a or a["do"] in fh.ACTIONS), a)
        part2 = []
        for body in BODIES:
            for k_ in ("you_are_waiting_on", "crews", "crews_quiet", "crew_limit", "areas", "summary",
                       "counts", "hidden_summary"):
                if k_ in body:
                    part2.append(body[k_])
            for p in body.get("projects", []):
                part2.append(p.get("yours_text") or "")
        text2 = list(strings(part2))
        bad2 = [(s, words.offenders(s)) for s in text2 if words.offenders(s)]
        ck("every new string passes the words check, names and all", not bad2, bad2[:3])
        ck("no em dash in a new string", not [s for s in text2 if "\u2014" in s])
        whole = words.offenders_in(BODIES, ("name", "title", "detail", "goal", "doing", "text"))
        ck("a whole body, skipping what people wrote, passes", not whole, whole[:3])

        print("== what the single-person home asks of a lead stays with the lead ==")
        orbit = await make_project("kim", "Orbit")
        store.create_doc(orbit, "spec", "Spec", "What to build.")
        await ticket("kim", orbit, "Orbit page", "acme/web")
        store.add_assumption(orbit, "Which logger?", "the standard one", "no new package", "", [],
                             "orbit-build-m1", "builder", ago(hours=3))
        fin = await make_project("kim", "Finished")
        await ticket("kim", fin, "Done thing", "acme/api", stage="merged", status="done")
        mine = await make_project("alex", "Alex Spec")
        store.create_doc(mine, "spec", "Spec", "What to build.")
        await ticket("alex", mine, "Alex page", "acme/web")
        k = by_kind(await home("kim"))
        ck("Kim leads Orbit: the spec is ready to hand to a crew", [i["title"] for i in k.get("ready_to_start", [])]
           == ["Orbit"], k.get("ready_to_start"))
        ck("and the call the crew made on it is hers to confirm", [i["title"] for i in k.get("calls_to_confirm", [])]
           == ["Orbit"], k.get("calls_to_confirm"))
        ck("and Finished is ready to close", [i["title"] for i in k.get("signoff", [])
                                              if i["id"] == f"signoff:{fin}"] == ["Finished"], k.get("signoff"))
        ck("not Alex Spec, which Alex leads", "Alex Spec" not in {i["title"] for i in k.get("ready_to_start", [])})
        a = by_kind(await home("alex"))
        ck("Alex has Alex Spec to hand over, and only that", [i["title"] for i in a.get("ready_to_start", [])]
           == ["Alex Spec"] and "calls_to_confirm" not in a and f"signoff:{fin}" not in {
               i["id"] for i in a.get("signoff", [])}, a)
        l = by_kind(await home("lee"))
        ck("Lee leads none of them: none of the three", not ({"ready_to_start", "calls_to_confirm"} & set(l))
           and f"signoff:{fin}" not in {i["id"] for i in l.get("signoff", [])}, l)

        print("== behaviours a quick change could drop ==")
        own = store.add_desk_item(orbit, "Pick a name?", "", [], "Nova", "short", "", [], False,
                                  "crew-orchestrator", [], ago(minutes=9), station="a-build-m1",
                                  to_person_id=pid["alex"])
        theirs = store.add_desk_item(orbit, "Pick a colour?", "", [], "blue", "calm", "", [], False,
                                     "crew-orchestrator", [], ago(minutes=8), station="k-orchestrator",
                                     to_person_id=pid["alex"])
        frm = {i["id"]: i["from"] for i in by_kind(await home("alex"))["desk"]}
        ck("an item from the caller's own crew comes from Your crew",
           frm[f"desk:{own['label']}"] == {"crew": True, "name": "Your crew"}, frm)
        ck("an item from another person's crew comes from that person",
           frm[f"desk:{theirs['label']}"] == {"id": pid["kim"], "name": "Kim"}, frm)
        second = await make_project("kim", "Second shop")
        await ticket("kim", second, "Second page", "acme/web")
        ck("Alex owes a sign-off on a project not yet handed over", any(
            i["project"]["id"] == second for i in by_kind(await home("alex")).get("signoff", [])))
        store.set_handover(second, [{"path": None, "name": "acme/web"}], None, "kim", now_utc(),
                           person_id=pid["kim"])
        ck("once it is handed over, nothing is owed on it", not any(
            i["project"]["id"] == second for i in by_kind(await home("alex")).get("signoff", [])))
        busy = await make_project("kim", "Busy")
        await ticket("kim", busy, "In flight", "acme/api", stage="doing", status="started")
        mark = lambda kind: [{"project": {"id": busy}, "kind": kind}]
        ck("a desk item holds a project that is building: it waits on you",
           fh.project_state(store.get_project(busy), store.list_tickets(busy), None, mark("desk"))
           == "waiting_on_you")
        ck("a pull request to review does not hold it",
           fh.project_state(store.get_project(busy), store.list_tickets(busy), None, mark("prs_to_review"))
           == "building")
        nobody = type("C", (), {
            "store": type("S", (), {"only_local_person": lambda self: False})(),
            "h": type("H", (), {"current_person": lambda self, r: None,
                                "is_system": lambda self, r: False})()})()
        ck("a project key, which names no person, gets none of the team keys",
           factory_team.team_home(nobody, None, now_utc()) == {})
        r = await call("lee", "POST", "/api/grants", {"kind": "project", "project": vault,
                                                      "words": "ship the Vault launch codes",
                                                      "areas": ["acme/web"]})
        ck("Lee gives a grant over Alex's repo on his private project", r.status_code == 201, r.text)
        g = (by_kind(await home("alex")).get("grant") or [{}])[-1]
        ck("Alex sees it, and its title does not name the private project",
           g.get("title") == "A grant over acme/web" and "Vault" not in json.dumps(g)
           and "launch codes" not in json.dumps(g), g)

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
