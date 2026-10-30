"""A desk item is for one person, and only that person's words answer it (TR-634).

Covers: an item about Q's ticket is addressed to Q (else the lead, else the caller; `to` takes a
person id or an email, and an unknown one is refused); `desk_list` with `mine` and `my_work` list an
item for the person it is addressed to and not for anyone else; an answer typed in P's recorded
session does not satisfy an item for Q, one typed in Q's does (whichever station Q was in, or none);
an answer that arrives before its recording is a retryable error and succeeds once the recording is
in; the decision is Q's; a standing answer becomes a grant given by Q (project by default, personal
on request), and a project grant over a repo Q owns waits for the project's lead; an item from before
items were addressed keeps the older rule.

Run: .venv/bin/python tests/test_desk_person.py
"""
import asyncio
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-deskperson-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-deskperson"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

import tares.mcp_server as m
import tares.mcp_factory as mf
from tares import factory

logging.getLogger("httpx").setLevel(logging.WARNING)   # the MCP import turns request logs on

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def now_plus(seconds: float) -> str:
    t = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def says(sid, text, seconds):
    return {"type": "user", "sessionId": sid, "cwd": "/w", "timestamp": now_plus(seconds),
            "message": {"role": "user", "content": text}}


def nd(lines) -> str:
    return "\n".join(json.dumps(x) for x in lines) + "\n"


def not_found(out, name):
    return (isinstance(out, str) and out.startswith("error 409: ")
            and f"Answer not found yet in {name}'s recorded sessions" in out
            and "try again after the next sync" in out)


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    root = {"Authorization": f"Bearer {TOKEN}"}
    m.TARESD = mf.TARESD = "http://t"
    headers = dict(root)
    mk = lambda timeout=10: httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                              base_url="http://t", headers=dict(headers))
    m._cx = mf._cx = mk
    factory.MATCH_WAIT_SECONDS = 0   # a miss answers at once

    def as_(h):
        headers.clear()
        headers.update(h)

    async with app.router.lifespan_context(app):
        async with mk() as cx:
            r = await cx.post("/api/sources", json={"name": "claude_code", "connector": "claude_code",
                                                    "poll": "10s", "config": {"push": True}})
            ck("the claude_code source is made", r.status_code < 300, r.text)
            keys, pid = {}, {}
            for who in ("P", "Q", "R"):
                r = await cx.post("/api/keys", json={
                    "name": f"{who}-laptop", "scopes": ["admin"],
                    "person": {"email": f"{who.lower()}@example.com", "name": who}})
                keys[who] = {"Authorization": f"Bearer {r.json()['secret']}"}
                pid[who] = r.json()["person"]["id"]
            ck("three people with bound keys", len(set(pid.values())) == 3)

            async def make_project(name):
                r = await cx.post("/api/projects", json={"template": "custom", "name": name,
                                                         "objects": [], "kind": "software_factory"})
                return r.json()["id"]

            async def make_ticket(uid, title, who=None):
                body = {"title": title}
                if who:
                    body["assignee"] = pid[who]
                r = await cx.post(f"/api/projects/{uid}/tickets", json=body)
                assert r.status_code == 201, r.text
                return r.json()

            shop = await make_project("Shop")         # P leads it
            blog = await make_project("Blog")         # Q leads it
            quiet = await make_project("Quiet")       # nobody leads it
            store.set_project_lead(shop, pid["P"])
            store.set_project_lead(blog, pid["Q"])
            s1 = await make_ticket(shop, "P builds the cart", "P")
            s2 = await make_ticket(shop, "Q builds payments", "Q")
            s3 = await make_ticket(shop, "Nobody's task")
            b1 = await make_ticket(blog, "Q writes a post", "Q")
            L = {k: factory.label(v) for k, v in (("s1", s1), ("s2", s2), ("s3", s3), ("b1", b1))}
            store.update_ticket(s2["id"], repo="acme/web")
            r = await cx.put("/api/areas/acme/web", json={"person_id": pid["Q"]})
            ck("Q owns acme/web", r.status_code < 300, r.text)

        async def push(who, lines):
            async with mk() as cx:
                r = await cx.post("/ingest/claude_code", content=nd(lines),
                                  headers={"Content-Type": "application/x-ndjson",
                                           **(keys[who] if who else root)})
                ck(f"{who or 'root'}'s lines are taken in", r.status_code < 300, r.text)

        def orchestrator(who):
            as_({**keys[who], "X-Tares-Station": "crew-orchestrator", "X-Tares-Role": "orchestrator"})

        async def add(question, **kw):
            out = await mf.desk_add(question, "yes", "because", **kw)
            try:
                return json.loads(out)
            except ValueError:
                return out

        def to_of(d):
            return ((d.get("to_person") or {}).get("id")) if isinstance(d, dict) else None

        async def my_work_desk(who):
            async with mk() as cx:
                r = await cx.get("/api/crew/my-work", headers=keys[who])
            return r.json()["desk"] if r.status_code == 200 else r.text

        print("== who an item is addressed to ==")
        orchestrator("P")                       # P's orchestrator writes all of these
        d1 = await add("Shop payments: Stripe test mode or live?", project="Shop", tickets=[L["s2"]])
        ck("an item about Q's ticket is addressed to Q", to_of(d1) == pid["Q"], d1)
        d2 = await add("Shop cart: one page or two?", project="Shop", tickets=[L["s1"]])
        ck("an item about P's ticket is addressed to P", to_of(d2) == pid["P"], d2)
        d3 = await add("Shop: which logo?", project="Shop", tickets=[L["s3"]])
        ck("a ticket with no assignee falls to the project's lead", to_of(d3) == pid["P"], d3)
        d4 = await add("Blog: comments on or off?", project="Blog")
        ck("an item with no ticket is for the project's lead", to_of(d4) == pid["Q"], d4)
        d5 = await add("Quiet: archive it?", project="Quiet")
        ck("a project with no lead falls to the caller", to_of(d5) == pid["P"], d5)
        d6 = await add("Shop: ship on Friday?", project="Shop", tickets=[L["s2"]], to="r@example.com")
        ck("`to` as an email wins over the ticket's assignee", to_of(d6) == pid["R"], d6)
        d7 = await add("Shop: rename it?", project="Shop", tickets=[L["s1"]], to=pid["Q"])
        ck("`to` as a person id works too", to_of(d7) == pid["Q"], d7)
        bad = await add("Shop: nobody to ask?", project="Shop", to="nobody@example.com")
        ck("an unknown `to` is refused and makes no person",
           isinstance(bad, str) and "error 404" in bad and "no person" in bad
           and store.find_person_by_email("nobody@example.com") is None, bad)
        as_(root)
        d8 = await add("Cell-wide: back up nightly?")
        ck("the root token's item is for `local`", to_of(d8) == "local", d8)

        print("== mine, and my_work ==")
        as_(keys["Q"])
        mine = json.loads(await mf.desk_list(mine=True))
        ck("desk_list mine is Q's items", sorted(d["label"] for d in mine)
           == sorted([d1["label"], d4["label"], d7["label"]]), mine)
        wq = await my_work_desk("Q")
        ck("my_work for Q lists the item about their ticket", d1["label"] in [d["label"] for d in wq]
           and all(d["label"] != d2["label"] for d in wq), wq)
        row = next(d for d in wq if d["label"] == d1["label"])
        ck("with its question and project", row["question"].startswith("Shop payments")
           and row["project_name"] == "Shop", row)
        wp = await my_work_desk("P")
        ck("my_work for P does not list it", d1["label"] not in [d["label"] for d in wp]
           and d2["label"] in [d["label"] for d in wp], wp)
        wr = await my_work_desk("R")
        ck("R has just the one for R", [d["label"] for d in wr] == [d6["label"]], wr)
        async with mk() as cx:
            r = await cx.get("/api/crew/my-work", headers=root)
        ck("the root token sees the items for `local`, and spec_questions stay empty",
           [d["label"] for d in r.json()["desk"]] == [d8["label"]] and r.json()["spec_questions"] == [],
           r.text)

        print("== only Q's own sessions answer Q's item ==")
        await push("P", [says("p-s1", "Stripe test mode please", 2)])
        as_({**keys["Q"], "X-Tares-Station": "crew-orchestrator", "X-Tares-Role": "orchestrator"})
        out = await mf.desk_answer(d1["label"], "Stripe test mode please")
        ck("an answer typed in P's session does not satisfy it, and says whose it waits for",
           not_found(out, "Q"), out)
        ck("the item stays open", store.get_desk_item(d1["label"])["state"] == "open")
        await push("Q", [says("q-s1", "Stripe test mode please", 3)])
        out = json.loads(await mf.desk_answer(d1["label"], "Stripe test mode please"))
        ck("the same words in Q's session do", out["recorded"] and out["item"] == d1["label"]
           and store.get_desk_item(d1["label"])["state"] == "answered", out)
        dec = next((x for x in store.list_decisions(None) if x["question"].startswith("Shop payments")),
                   {})
        ck("and the decision is Q's, though P asked", (dec.get("person") or {}).get("id") == pid["Q"], dec)
        ck("no grant from a plain answer", out["became_a_grant"] is False and out["grant"] is None,
           out)
        as_(keys["Q"])
        mine = json.loads(await mf.desk_list(mine=True))
        ck("it leaves Q's my_work once answered", d1["label"] not in [d["label"] for d in mine], mine)

        print("== any session of Q's counts, not only the orchestrator's ==")
        await push("Q", [{"type": "session_station", "sessionId": "q-b1", "cwd": "/w",
                          "timestamp": now_plus(1), "station": "shop-build-m1", "role": "builder"},
                         says("q-b1", "Two pages, a quick one", 4)])
        as_(keys["P"])   # P's own orchestrator hands the answer in; the words are Q's, from Q's builder
        as_({**keys["P"], "X-Tares-Station": "crew-orchestrator", "X-Tares-Role": "orchestrator"})
        out = await mf.desk_answer(d7["label"], "Two pages, a quick one")
        ck("Q's words in a builder session answer Q's item", isinstance(out, str)
           and json.loads(out).get("recorded") is True, out)
        dec7 = next((x for x in store.list_decisions(None) if x["question"].startswith("Shop: rename")),
                    {})
        ck("the decision is Q's, though P's key carried it", (dec7.get("person") or {}).get("id")
           == pid["Q"], dec7)

        print("== an answer before its recording ==")
        orchestrator("P")
        d9 = await add("Shop: refunds in this release?", project="Shop", tickets=[L["s2"]])
        orchestrator("Q")
        words = "Refunds can wait for the next release"
        out = await mf.desk_answer(d9["label"], words)
        ck("nothing recorded yet is the retryable error", not_found(out, "Q"), out)
        ck("not an unverified answer: still open, no decision",
           store.get_desk_item(d9["label"])["state"] == "open")
        await push("Q", [says("q-s2", words, 5)])
        out = json.loads(await mf.desk_answer(d9["label"], words))
        ck("after the recording is in, the same call succeeds", out["recorded"], out)

        print("== a reply from a machine whose clock runs behind ==")
        orchestrator("P")
        d9b = await add("Shop: gift cards?", project="Shop", tickets=[L["s2"]])
        d9c = await add("Shop: coupons?", project="Shop", tickets=[L["s2"]])
        orchestrator("Q")
        late, early = "Gift cards go in the next release", "Coupons stay off for now"
        await push("Q", [says("q-s2", late, -20), says("q-s2", early, -300)])
        out = await mf.desk_answer(d9b["label"], late)
        ck("typed 20 s before the cell stamped the question: still the answer",
           out.startswith("{") and json.loads(out)["recorded"], out)
        out = await mf.desk_answer(d9c["label"], early)
        ck("typed 5 minutes before the question: not an answer", not_found(out, "Q"), out)

        print("== lines Claude Code writes that Q did not type ==")
        orchestrator("P")
        d15 = await add("Shop: ship on Friday?", project="Shop", tickets=[L["s2"]])
        orchestrator("Q")
        words = "Ship it on Friday"
        notyped = [
            dict(says("q-meta", words, 11), isMeta=True, origin={"kind": "peer"}),
            dict(says("q-meta", words, 12), isMeta=False, origin={"kind": "task-notification"}),
            dict(says("q-meta", words, 13), isCompactSummary=True),
            dict(says("q-meta", words, 13.5), isMeta=True),   # a scheduled prompt
        ]
        await push("Q", notyped)
        out = await mf.desk_answer(d15["label"], words)
        ck("a peer message, a task notification and a compaction summary do not answer",
           not_found(out, "Q") and store.get_desk_item(d15["label"])["state"] == "open", out)
        await push("Q", [says("q-typed", words, 14)])
        out = json.loads(await mf.desk_answer(d15["label"], words))
        ck("a typed line with no origin (headless, older Claude Code) does", out["recorded"], out)

        print("== a standing answer is a grant given by Q ==")
        orchestrator("P")
        d10 = await add("Blog: deploy to staging without asking?", project="Blog")
        ck("(an item for the Blog's lead, Q)", to_of(d10) == pid["Q"], d10)
        orchestrator("Q")
        words = "Deploys to staging are fine without asking"
        out = await mf.desk_answer(d10["label"], words, standing=True)
        ck("a standing answer not yet recorded is refused and makes no grant",
           not_found(out, "Q") and not any(g["words"] == words for g in store.all_grant_rows()), out)
        await push("Q", [says("q-s3", words, 6)])
        out = json.loads(await mf.desk_answer(d10["label"], words, standing=True))
        g = out["grant"]
        ck("once recorded it becomes a project grant given by Q", out["became_a_grant"] and g
           and g["kind"] == "project" and g["project"] == blog and g["given_by"]["id"] == pid["Q"]
           and g["state"] == "active" and g["in_effect"], out)
        row = store.get_grant_row(g["id"])
        ck("the row says the same", row["given_by"] == pid["Q"] and row["holder_person_id"] is None, row)

        orchestrator("P")
        d11 = await add("Blog: read the prod logs?", project="Blog")
        orchestrator("P")
        words = "Reading prod logs is fine for me"
        await push("Q", [says("q-s4", words, 7)])
        out = json.loads(await mf.desk_answer(d11["label"], words, standing=True, grant_kind="personal"))
        g = out["grant"]
        ck("grant_kind personal is Q's own, for that project: given by Q, held by Q", out["became_a_grant"] and g
           and g["kind"] == "personal" and g["given_by"]["id"] == pid["Q"]
           and g["holder"]["id"] == pid["Q"] and g["project"] == blog, out)

        orchestrator("P")
        d12 = await add("Shop: run the payment sandbox without asking?", project="Shop",
                        tickets=[L["s2"]])
        words = "The payment sandbox is fine to run"
        await push("Q", [says("q-s5", words, 8)])
        out = json.loads(await mf.desk_answer(d12["label"], words, standing=True))
        g = out["grant"]
        ck("a project grant Q gives over a repo Q owns, in a project P leads, waits for P",
           g and g["given_by"]["id"] == pid["Q"] and g["state"] == "waiting"
           and g["areas"] == ["acme/web"] and not g["in_effect"]
           and any(w["id"] == pid["P"] for w in g["waiting_for"]), out)
        ck("and it is not reported as granted", out["became_a_grant"] is False
           and not next(x for x in store.list_decisions(None)
                        if x["question"].startswith("Shop: run the payment sandbox"))["granted"], out)

        orchestrator("P")
        d16 = await add("Shop: let the payment code merge itself?", project="Shop",
                        tickets=[L["s2"]], to="r@example.com")
        orchestrator("R")
        words = "Payment code may merge itself"
        await push("R", [says("r-s1", words, 15)])
        out = await mf.desk_answer(d16["label"], words, standing=True)
        ck("a project grant from someone who neither leads the project nor owns its repos: refused",
           isinstance(out, str) and "error 403" in out
           and store.get_desk_item(d16["label"])["state"] == "open"
           and not any(g["words"] == words for g in store.all_grant_rows()), out)

        d13 = await add("Quiet: allow anything?", project="Quiet", to="q@example.com")
        words = "Anything goes for every project"
        await push("Q", [says("q-s6", words, 9)])
        out = await mf.desk_answer(d13["label"], words, standing=True, scope="all")
        ck("a grant for every project needs an admin, and Q is not one: refused, item still open",
           isinstance(out, str) and "error 403" in out
           and store.get_desk_item(d13["label"])["state"] == "open", out)

        print("== an item goes only to someone who can see the project ==")
        as_(root)
        async with mk() as cx:
            r = await cx.post("/api/projects", json={"template": "custom", "name": "Vault",
                                                     "objects": [], "kind": "software_factory"})
            vault = r.json()["id"]
            store.set_project_lead(vault, pid["P"])
            store.set_project_scope(vault, "private")
        ck("set up: Vault is private, P leads it", store.get_project(vault)["scope"] == "private")
        orchestrator("P")
        out = await add("Vault: rotate the key?", project="Vault", to=pid["Q"])
        ck("`to` a person who is not on a private project: 403", isinstance(out, str)
           and out.startswith("error 403") and "not on this private project" in out, out)
        out = await add("Vault: rotate the key?", project="Vault", to="q@example.com")
        ck("an email gets the same answer", isinstance(out, str) and out.startswith("error 403"), out)
        ck("nothing was added", not [d for d in store.list_desk("open", vault)])
        out = await add("Vault: rotate the key?", project="Vault", to=pid["P"])
        ck("`to` the lead works", to_of(out) == pid["P"], out)
        store.add_project_member(vault, pid["Q"], pid["P"], reason="manual")
        out = await add("Vault: who owns it?", project="Vault", to=pid["Q"])
        ck("`to` a member works", to_of(out) == pid["Q"], out)
        out = await add("Shop: ship it?", project="Shop", to=pid["R"])
        ck("on a team project anyone on the cell can be addressed", to_of(out) == pid["R"], out)

        print("== an item from before items were addressed keeps the older rule ==")
        as_(root)
        d14 = await add("Old: tabs or spaces?")
        with store._lock:
            store.sc.execute("UPDATE desk_items SET to_person_id = NULL WHERE number = ?", [int(d14["label"][1:])])
        ck("(an item with no person)", store.get_desk_item(d14["label"])["to_person_id"] is None)
        wd = await my_work_desk("Q")
        ck("it is in nobody's my_work but `local`'s", d14["label"] not in [d["label"] for d in wd])
        async with mk() as cx:
            r = await cx.get("/api/crew/my-work", headers=root)
        ck("`local` (the root token) has it", d14["label"] in [d["label"] for d in r.json()["desk"]],
           r.text)
        out = await mf.desk_answer(d14["label"], "Tabs, always")
        ck("a miss is the old soft answer, not an error", isinstance(out, str)
           and json.loads(out)["recorded"] is False, out)
        await push(None, [says("root-s1", "Tabs, always", 10)])
        out = json.loads(await mf.desk_answer(d14["label"], "Tabs, always"))
        ck("words typed in any session are found, as before", out["recorded"], out)

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    asyncio.run(main())
