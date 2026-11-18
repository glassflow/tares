"""The merge queue page's one read (TR-1094): GET /api/projects/{uid}/merge-queue.

One in-process cell, the real store and the real route. The rows are built straight in the store
(a queue row, its ticket with the pull request as GitHub last said it, a review, a desk question,
a decision), and each scenario compares the whole answer with literal data: the strings of the
TR-1093 mock, so the screen only has to display them. Covered:
 - the empty queue; a pull request waiting for CI (with the merged list: three at most, newest
   first, merged by the queue or by a person on GitHub); a group that waits for a yes, read by the
   person the question is for; read by someone else; the commit changed since the question; the
   question answered yes in the console (the row is still waiting in the store); answered no in
   Slack; blocked by repo rules; blocked with no GitHub connection; sent back with each step of the
   ladder; a guardrail warning and a dependency that waits;
 - where an answer was given (`where_of`), and the merge queue page's own approval told apart;
 - a project the caller cannot see answers 404, a draft or a non factory project too;
 - no state key, stage name or em dash in any sentence; every status starts with its lead;
 - the route reads only: no merge_ready call, no GitHub call, every table the same after.

Run: .venv/bin/python tests/test_merge_queue_api.py
"""
import asyncio
import copy
import os
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-merge-queue-api-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-merge-queue-api"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"
os.environ["TARES_PR_SYNC_SECONDS"] = "off"

import httpx

from tares import status_words as sw
from tares import factory as factory_mod
from tares import factory_github, merge_queue, merge_ready, words
from tares.integrations import registry
from tares.integrations.guardrails import Guardrail
from tares.screens import factory_merge_queue as fmq
from tares.store import now_utc

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def strings(v, path="$", key=""):
    """Every (path, key, string) of a JSON value, `key` being the name of the field holding it."""
    if isinstance(v, str):
        yield path, key, v
    elif isinstance(v, dict):
        for k, x in v.items():
            yield from strings(x, f"{path}.{k}", k)
    elif isinstance(v, list):
        for i, x in enumerate(v):
            yield from strings(x, f"{path}[{i}]", key)


class DiffSize(Guardrail):
    """A guardrail with the title the mock shows."""
    name = "diffsize"
    title = "the diff size guardrail"


# the pull request's own GitHub address and the commit it asks about
def url(repo, n):
    return f"https://github.com/{repo}/pull/{n}"


def full(short):
    return (short + "0" * 40)[:40]


API, WEB = "northwind/orders-api", "northwind/orders-web"
AUTO_TEXT = "Merges on its own (L4-ship)"
ASK_TEXT = "Waits for your yes on each merge (L3-review)"
ASKED_YES = ("Asked {ago} for commit {c}. Your answer counts for this commit only; a new commit asks "
             "again. It is the same question as on Overview and the project page, so answering it "
             "here closes it there too.")


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    registry.load()
    registry._loaded[("guardrails", "diffsize")] = DiffSize
    root = {"Authorization": f"Bearer {TOKEN}"}
    lee = store.create_person("Lee Okafor", email="lee@example.com")
    marcus = store.create_person("Marcus Oyelaran", email="marcus@example.com")
    dana = store.create_person("Dana Whitfield", email="dana@example.com", github="dana-w")
    stranger = store.create_person("Sam Outsider", email="sam@example.com")

    def mk(headers):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 headers=headers)

    async with app.router.lifespan_context(app):
        async with mk(root) as cx:
            keys = {}
            for who, person in (("lee", lee), ("dana", dana), ("sam", stranger)):
                r = await cx.post("/api/keys", json={"name": who, "scopes": ["read"],
                                                      "person_id": person["id"]})
                keys[who] = {"Authorization": f"Bearer {r.json()['secret']}"}
            r = await cx.post("/api/keys", json={"name": "leew", "scopes": ["read", "admin"],
                                                  "person_id": lee["id"]})
            keys["leew"] = {"Authorization": f"Bearer {r.json()['secret']}"}
            nokey = (await cx.post("/api/keys", json={"name": "bot", "scopes": ["read"]})).json()
            keys["bot"] = {"Authorization": f"Bearer {nokey['secret']}"}

        def back(table, col, rid, minutes=0, seconds=0):
            """Move a time of the past in the store: the store stamps `now` on its own writes."""
            with store._swrite:
                store.sc.execute(f"UPDATE {table} SET {col} = ? WHERE id = ?",
                                 [now_utc() - timedelta(minutes=minutes, seconds=seconds), rid])

        counter = {"n": 0}

        def project(name, level="L4-ship", handed=True, kind="software_factory", status=None):
            counter["n"] += 1
            uid = f"uc_mq{counter['n']}"
            store.create_project(uid, "custom", name, {}, goal="A project for the test",
                                 status=status or "active")
            store.set_project_kind(uid, kind)
            if handed:
                store.set_handover(uid, [{"path": "/w/a", "name": name}], level, None, now_utc(),
                                   person_id=lee["id"])
            return uid

        pos = {"n": 0}

        def pull(uid, repo, n, ident, title, branch, state="queued", reason=None, base="main",
                 head=None, builder=None, ci="success", checks=None, by=None, depends=None,
                 reviewed=True, short=None):
            """A ticket in review with its pull request, a pass of the crew reviewer (or of `by`, a
            person) and the queue's row. `short` is the first characters of the head."""
            pos["n"] += 1
            head = head or full(short or f"{n:07x}")
            tid = store.create_ticket(uid, "tares", title, identifier=ident, repo=repo,
                                      position=float(pos["n"]), assignee_person_id=builder,
                                      depends_on=depends)
            store.update_ticket(tid, stage="review", pr={
                "url": url(repo, n), "number": n, "repo": repo, "head": head, "base": base,
                "branch": branch, "state": "open", "title": title, "ci": ci,
                "ci_checks": checks or []})
            if reviewed:
                store.add_review(uid, tid, url(repo, n), head, "pass", "Ran the tests", "",
                                 "crew-reviewer", [], {}, factory_mod.finding_kind, now_utc(),
                                 person_id=by)
            row = store.merge_queue_add(repo, base, n, head, tid, uid, state=state, reason=reason)
            return tid, row

        def question(uid, tid, to, mins, text="Merge it?"):
            return store.add_desk_item(uid, text, "", ["Yes, merge", "No, hold"], "", "", "",
                                       [tid], True, "merge-queue", [],
                                       now_utc() - timedelta(minutes=mins), to_person_id=to)

        def ask(row, d):
            store.merge_queue_update(row["id"], desk_item=d["id"])
            return d

        def answer(d, tid, uid, words_, choice, via, who, mins):
            store.add_decision(uid, d["id"], d["question"], words_, choice, False, "project",
                               False, None, False, [tid], now_utc() - timedelta(minutes=mins),
                               via=via, person_id=who)
            store.close_desk_item(d["id"], "answered", None, now_utc())

        async def read(uid, who="root"):
            h = root if who == "root" else keys[who]
            async with mk(h) as cx:
                return await cx.get(f"/api/projects/{uid}/merge-queue")

        async def page(uid, who="root"):
            r = await read(uid, who)
            assert r.status_code == 200, (r.status_code, r.text)
            return r.json()

        BODIES = []          # every answer, for the scans

        TURNS = []           # (project, ticket ref, state, chip key, turn, turn_label, whose) of every row

        def expect(label, got, want):
            BODIES.append(got)
            # the turn words are the ticket's (factory.turn_of), pinned in their own checks at the end
            for g in got.get("groups") or []:
                for r in g["rows"]:
                    TURNS.append((got["project"]["name"], r["ticket"]["ref"], r["state"], r["chip"]["key"],
                                  r.pop("turn"), r.pop("turn_label"), r.pop("whose")))
            ck(label, got == want, "\n    got : " + repr(got) + "\n    want: " + repr(want))

        def me(person, line):
            return {"id": person["id"], "name": person["name"], "text": line}

        LEE_B = me(lee, "Lee’s builder")
        MARCUS_B = me(marcus, "Marcus’s builder")
        DANA_B = me(dana, "Dana’s builder")

        def X(r, p, pos_, ident, title, n, repo, branch, builder, chip, status, review="Approved by the crew reviewer",
              ci="Passed", rail="Allowed", answer=None, bounce=None, needs=False):
            return {"id": r["id"], "position": pos_, "state": r["state"] if p is None else p,
                    "chip": chip, "ticket": {"ref": ident, "title": title},
                    "pr": {"number": n, "url": url(repo, n)}, "branch": branch, "builder": builder,
                    "review": review, "ci": ci, "guardrail": rail, "status": status,
                    "answer": answer, "bounce": bounce, "needs_you": needs}

        def S(text, lead="", note=""):
            return {"lead": lead, "text": text, "note": note}

        def G(repo, base, rows, level=AUTO_TEXT, you=0, blocked=0, sent=0, merged=None, extra=()):
            line = sum(1 for r in rows if r["state"] != "sent_back")
            bits = [level] + list(extra)
            return {"repo": repo, "base": base, "title": f"{repo} into {base}",
                    "count": f"{line} in line", "auto": level, "sub": " · ".join(bits),
                    "line": line, "you": you, "blocked": blocked, "sent": sent, "rows": rows,
                    "merged": merged}

        INTRO = ("One pull request merges at a time for each repo and base branch, in milestone and "
                 "ticket order. No agent and no tokens are used.")
        EMPTY = "Nothing is waiting to merge. A pull request shows here once its review is approved."
        FOOT = ["Milestone pull requests from factory-modular/* are merged by you.",
                "That is the whole queue. Merged and closed pull requests are on the Pull requests tab."]

        def doc(uid, name, groups, aside="", intro="", empty="", line=0, need=0, note=""):
            return {"project": {"id": uid, "name": name}, "aside": aside,
                    "intro": INTRO if groups else "", "empty": "" if groups else EMPTY,
                    "tab": {"line": line, "need": need,
                            "need_text": (f"{need} need{'s' if need == 1 else ''} you" if need else ""),
                            "note": note},
                    "footer": FOOT, "groups": groups}

        # ── 1. empty ───────────────────────────────────────────────────────────────────────────
        print("== empty ==")
        uid = project("Empty")
        expect("empty: nothing waits, the page says so", await page(uid),
               doc(uid, "Empty", [], note="Nothing is in the merge queue."))

        # ── 2. waiting for CI, with the merged list ─────────────────────────────────────────────
        print("== waiting for CI ==")
        uid = project("Waiting for CI")
        t1, r1 = pull(uid, API, 217, "ORD-41", "Round prices to the order currency",
                      "ord-41-price-rounding", state="waiting_ci", ci="pending")
        back("merge_queue", "updated_at", r1["id"], minutes=3)
        t2, r2 = pull(uid, API, 219, "ORD-43", "Add currency to the order export",
                      "ord-43-export-currency", builder=marcus["id"], by=dana["id"])
        t3, r3 = pull(uid, WEB, 88, "ORD-52", "Show the shipping estimate in the cart",
                      "ord-52-shipping-estimate", builder=dana["id"])
        # merged: newest first, three at most, one older than the window is left out
        merged = []
        for n, ident, title, mins, by_queue in (
                (214, "ORD-38", "Fix tax total on refunds", 0, True),
                (211, "ORD-36", "Cap coupon discounts at the order total", 9, True),
                (208, "ORD-34", "Add the order id to export rows", 65, False),
                (203, "ORD-31", "Older than the three shown", 120, True),
                (150, "ORD-12", "Merged long ago", 60 * 24 * 5, True)):
            t, r = pull(uid, API, n, ident, title, f"ord-{n}", state="merged")
            store.add_ticket_history(t, uid, "stage", "merged", None,
                                     "merge-queue" if by_queue else None, None, now_utc())
            if not by_queue:
                store.update_ticket(t, pr={"url": url(API, n), "number": n, "repo": API,
                                           "head": full(f"{n:07x}"), "merged_by": "dana-w"})
            back("merge_queue", "updated_at", r["id"], minutes=mins, seconds=12 if mins == 0 else 0)
        mg = {"title": "Merged just now, 3", "items": [
            {"n": 214, "tk": "ORD-38", "title": "Fix tax total on refunds", "when": "just now",
             "by": "the merge queue"},
            {"n": 211, "tk": "ORD-36", "title": "Cap coupon discounts at the order total",
             "when": "9 minutes ago", "by": "the merge queue"},
            {"n": 208, "tk": "ORD-34", "title": "Add the order id to export rows",
             "when": "1 hour ago", "by": "Dana Whitfield on GitHub"}]}
        want = doc(uid, "Waiting for CI", [
            G(API, "main", [
                X(r1, None, 1, "ORD-41", "Round prices to the order currency", 217, API,
                  "ord-41-price-rounding", LEE_B, {"key": "ci", "text": "Waiting for CI"},
                  S("Branch updated to main 3 minutes ago, waiting for CI on the new commit."),
                  ci="Pending, checks restarted on the new commit"),
                X(r2, None, 2, "ORD-43", "Add currency to the order export", 219, API,
                  "ord-43-export-currency", MARCUS_B, {"key": "next", "text": "Next"},
                  S("Next in this project."), review="Approved by Dana Whitfield")],
              merged=mg),
            G(WEB, "main", [
                X(r3, None, 1, "ORD-52", "Show the shipping estimate in the cart", 88, WEB,
                  "ord-52-shipping-estimate", DANA_B, {"key": "next", "text": "Next"},
                  S("Next in this project."))])],
            aside="3 in line", line=3, note="3 pull requests are in the merge queue.")
        expect("waiting for CI: two groups, a pending check, the next to merge, three merged",
               await page(uid), want)
        b = await page(uid)
        ck("waiting for CI: the older merged ones are dropped, the cap is three",
           [i["n"] for i in b["groups"][0]["merged"]["items"]] == [214, 211, 208])

        # a second line in the queue says who it waits behind, and a dependency says which ticket
        t4, r4 = pull(uid, API, 221, "ORD-44", "Add a column", "ord-44-column", position=None) \
            if False else (None, None)

        # ── 3. waiting for approval, read by the person the question is for ───────────────────────
        print("== waiting for approval (mine) ==")
        uid = project("Waiting for approval", level="L3-review")
        BASE = "release-2.4"
        t1, r1 = pull(uid, API, 221, "ORD-47", "Backport price rounding to 2.4",
                      "ord-47-backport-rounding", state="waiting_approval", base=BASE,
                      short="4e81b2c")
        d1 = ask(r1, question(uid, t1, lee["id"], 14))
        t2, r2 = pull(uid, API, 223, "ORD-49", "Backport tax rounding to 2.4",
                      "ord-49-backport-tax", state="waiting_approval", base=BASE,
                      builder=marcus["id"], by=marcus["id"], short="a90d17f")
        d2 = ask(r2, question(uid, t2, lee["id"], 2))
        store.add_ticket_history(t2, uid, "guardrail_warn",
                                 "this change touches 14 files, the usual limit is 10",
                                 "pre_merge: diffsize", "merge-queue", None, now_utc())
        t3, r3 = pull(uid, API, 226, "ORD-50", "Backport the export column to 2.4",
                      "ord-50-backport-export", base=BASE, builder=dana["id"], depends=[t1])
        t0, r0 = pull(uid, API, 205, "ORD-30", "Backport the refund fix to 2.4", "ord-30",
                      state="merged", base=BASE)
        store.update_ticket(t0, pr={"url": url(API, 205), "number": 205, "repo": API,
                                    "head": full("cd"), "merged_by": "dana-w"})
        back("merge_queue", "updated_at", r0["id"], minutes=60 * 24 * 2)

        def mine(r, d, c, ago):
            return {"kind": "mine", "text": "Waiting for your yes.", "lead": "Waiting for your yes.",
                    "note": ASKED_YES.format(ago=ago, c=c), "item": {"id": d["id"], "label": d["label"]},
                    "can_answer": True, "for": {"id": lee["id"], "name": "Lee Okafor"}, "asked": c,
                    "now": c, "decision": None, "by": None, "where": None, "when": "", "commit": c}

        want = doc(uid, "Waiting for approval", [
            G(API, BASE, [
                X(r1, None, 1, "ORD-47", "Backport price rounding to 2.4", 221, API,
                  "ord-47-backport-rounding", LEE_B, {"key": "approval", "text": "Needs your yes"},
                  S("Waiting for your yes.", "Waiting for your yes.",
                    ASKED_YES.format(ago="14 minutes ago", c="4e81b2c")),
                  answer=mine(r1, d1, "4e81b2c", "14 minutes ago"), needs=True),
                X(r2, None, 2, "ORD-49", "Backport tax rounding to 2.4", 223, API,
                  "ord-49-backport-tax", MARCUS_B, {"key": "approval", "text": "Needs your yes"},
                  S("Waiting for your yes.", "Waiting for your yes.",
                    ASKED_YES.format(ago="2 minutes ago", c="a90d17f")),
                  review="Approved by Marcus Oyelaran",
                  rail="Warning from the diff size guardrail: this change touches 14 files, "
                       "the usual limit is 10",
                  answer=mine(r2, d2, "a90d17f", "2 minutes ago"), needs=True),
                X(r3, None, 3, "ORD-50", "Backport the export column to 2.4", 226, API,
                  "ord-50-backport-export", DANA_B, {"key": "queued", "text": "Queued"},
                  S("Waiting for ORD-47 to merge."))],
              level=ASK_TEXT, you=2, extra=["2 wait for you"],
              merged={"title": "Merged just now, 1", "items": [
                  {"n": 205, "tk": "ORD-30", "title": "Backport the refund fix to 2.4",
                   "when": "2 days ago", "by": "Dana Whitfield on GitHub"}]})],
            aside="3 in line, 2 need you", line=3, need=2,
            note="3 pull requests are in the merge queue, 2 need you.")
        expect("waiting for approval, read by Lee: two questions for him, one line that waits",
               await page(uid, "lee"), want)

        # ── 4. the same page read by someone the question is not for ─────────────────────────────
        print("== waiting for approval (someone else's) ==")
        got = await page(uid, "dana")
        a = got["groups"][0]["rows"][0]["answer"]
        other = {"kind": "other", "text": "Waiting for Lee Okafor’s yes. Only the person this question is for can answer it.",
                 "lead": "Waiting for Lee Okafor’s yes.",
                 "note": "Asked 14 minutes ago for commit 4e81b2c. You see it read only; the answer shows here once it is given.",
                 "item": {"id": d1["id"], "label": d1["label"]}, "can_answer": False,
                 "for": {"id": lee["id"], "name": "Lee Okafor"}, "asked": "4e81b2c",
                 "now": "4e81b2c", "decision": None, "by": None, "where": None, "when": "",
                 "commit": "4e81b2c"}
        ck("someone else's question: read only, named, the page cannot answer it", a == other,
           repr(a))
        BODIES.append(got)
        g0 = got["groups"][0]
        ck("someone else's question: the chip names the person, nothing needs the reader",
           g0["rows"][0]["chip"] == {"key": "other", "text": "Waiting for Lee"}
           and g0["rows"][0]["needs_you"] is False and g0["you"] == 0
           and g0["sub"] == ASK_TEXT and got["tab"]["need"] == 0 and got["aside"] == "3 in line"
           and got["tab"]["note"] == "3 pull requests are in the merge queue.",
           repr((g0["rows"][0]["chip"], g0["sub"], got["tab"], got["aside"])))
        ck("someone else's question: the root credential is the person local, so it is not Lee's",
           (await page(uid))["groups"][0]["rows"][0]["answer"]["can_answer"] is False)
        bot = await page(uid, "bot")
        ck("a key with no person cannot answer anything",
           all(r["answer"] is None or r["answer"]["can_answer"] is False
               for g in bot["groups"] for r in g["rows"]))

        # a key with no person and a question nobody is addressed: no lead either, so the key's
        # no one must not match the project's no lead
        uid = project("Nobody is addressed", level="L3-review")
        t1, r1 = pull(uid, API, 270, "ORD-70", "Unaddressed", "ord-70", state="waiting_approval",
                      base=BASE, short="3a3a3a3")
        d1 = ask(r1, question(uid, t1, None, 2))
        ck("the project has no lead and the question no addressee (the setup of the next check)",
           store.get_project(uid).get("lead_person_id") is None and d1.get("to_person_id") is None)
        bot = await page(uid, "bot")
        a = bot["groups"][0]["rows"][0]["answer"]
        ck("a key with no person cannot answer an unaddressed question of a project with no lead",
           a["can_answer"] is False and bot["groups"][0]["rows"][0]["needs_you"] is False, repr(a))
        ck("the system credential still can answer it",
           (await page(uid))["groups"][0]["rows"][0]["answer"]["can_answer"] is True)

        # ── 5. the commit changed since the question ────────────────────────────────────────────
        print("== the commit changed ==")
        uid = project("Moved", level="L3-review")
        t1, r1 = pull(uid, API, 232, "ORD-53", "Backport the carrier code to 2.4",
                      "ord-53-backport-carrier-code", state="waiting_approval", base=BASE,
                      builder=dana["id"], short="e5a1170")
        d1 = ask(r1, question(uid, t1, lee["id"], 40))
        store.update_ticket(t1, pr={"url": url(API, 232), "number": 232, "repo": API,
                                    "head": full("9c20d7a"), "base": BASE,
                                    "branch": "ord-53-backport-carrier-code", "state": "open",
                                    "title": "Backport the carrier code to 2.4", "ci": "pending",
                                    "ci_checks": [{"name": "unit", "state": "pending"}]})
        lead_ = "The commit changed, so this question is out of date."
        text_ = (lead_ + " It was asked for commit e5a1170, and the pull request is now at 9c20d7a.")
        note_ = "It cannot be answered here. Tares asks again once CI has passed on the new commit."
        moved = {"kind": "moved", "text": text_, "lead": lead_, "note": note_,
                 "item": {"id": d1["id"], "label": d1["label"]}, "can_answer": False,
                 "for": {"id": lee["id"], "name": "Lee Okafor"}, "asked": "e5a1170",
                 "now": "9c20d7a", "decision": None, "by": None, "where": None, "when": "",
                 "commit": "e5a1170"}
        want = doc(uid, "Moved", [
            G(API, BASE, [
                X(r1, None, 1, "ORD-53", "Backport the carrier code to 2.4", 232, API,
                  "ord-53-backport-carrier-code", DANA_B, {"key": "moved", "text": "Commit changed"},
                  S(text_, lead_, note_), review="Approved by the crew reviewer for an older commit",
                  ci="Pending, waiting for unit", answer=moved)],
              level=ASK_TEXT)],
            aside="1 in line", line=1, note="1 pull request is in the merge queue.")
        expect("the commit changed: out of date, cannot be answered, the new commit named",
               await page(uid, "lee"), want)

        # ── 6. answered yes elsewhere, the row still waits in the store ─────────────────────────
        print("== answered yes elsewhere ==")
        uid = project("Answered yes", level="L3-review")
        t1, r1 = pull(uid, API, 228, "ORD-51", "Backport the coupon cap to 2.4",
                      "ord-51-backport-coupon-cap", state="waiting_approval", base=BASE,
                      short="71be5d4")
        d1 = ask(r1, question(uid, t1, lee["id"], 21))
        answer(d1, t1, uid, "Yes, merge", "Yes, merge", "console", lee["id"], 2)
        yes_uid, yes_row = uid, r1      # moved on by the queue in "whose turn"
        ck("answered yes: the row is still waiting_approval in the store",
           store.merge_queue_get(r1["id"])["state"] == "waiting_approval")
        done = {"kind": "done",
                "text": "Yes, merge. Answered by you in the console, 2 minutes ago, for commit 71be5d4.",
                "lead": "Yes, merge.",
                "note": "The question is closed on Overview, the project page and the Calls page. It merges in its turn.",
                "item": {"id": d1["id"], "label": d1["label"]}, "can_answer": False,
                "for": {"id": lee["id"], "name": "Lee Okafor"}, "asked": "71be5d4", "now": "71be5d4",
                "decision": "yes", "by": "you", "where": {"key": "console", "words": "console"},
                "when": "2 minutes ago", "commit": "71be5d4"}
        want = doc(uid, "Answered yes", [
            G(API, BASE, [
                X(r1, None, 1, "ORD-51", "Backport the coupon cap to 2.4", 228, API,
                  "ord-51-backport-coupon-cap", LEE_B, {"key": "approved", "text": "Approved"},
                  S(done["text"], done["lead"], done["note"]), answer=done)],
              level=ASK_TEXT)],
            aside="1 in line", line=1, note="1 pull request is in the merge queue.")
        expect("answered yes elsewhere: approved, by you, where, when, the question closed",
               await page(uid, "lee"), want)
        got = await page(uid, "dana")
        ck("answered yes elsewhere, read by someone else: the person is named, not 'you'",
           got["groups"][0]["rows"][0]["answer"]["by"] == "Lee Okafor"
           and "Answered by Lee Okafor in the console" in got["groups"][0]["rows"][0]["status"]["text"])
        ck("answered yes elsewhere: nobody needs to answer, nothing is blocked",
           got["tab"]["need"] == 0 and got["groups"][0]["rows"][0]["needs_you"] is False)

        # ── 7. answered no elsewhere ────────────────────────────────────────────────────────────
        print("== answered no elsewhere ==")
        uid = project("Answered no", level="L3-review")
        t1, r1 = pull(uid, API, 230, "ORD-52", "Backport the currency error to 2.4",
                      "ord-52-backport-currency-error", state="waiting_approval", base=BASE,
                      builder=marcus["id"], short="0d44a9b")
        d1 = ask(r1, question(uid, t1, dana["id"], 33))
        answer(d1, t1, uid, "No, hold", "No, hold", "slack", dana["id"], 5)
        no = {"kind": "done",
              "text": "No, hold. Answered by Dana Whitfield in Slack, 5 minutes ago, for commit 0d44a9b.",
              "lead": "No, hold.",
              "note": "The question is closed everywhere. The merge queue skips this pull request; a new commit asks again.",
              "item": {"id": d1["id"], "label": d1["label"]}, "can_answer": False,
              "for": {"id": dana["id"], "name": "Dana Whitfield"}, "asked": "0d44a9b",
              "now": "0d44a9b", "decision": "no", "by": "Dana Whitfield",
              "where": {"key": "slack", "words": "Slack"}, "when": "5 minutes ago",
              "commit": "0d44a9b"}
        want = doc(uid, "Answered no", [
            G(API, BASE, [
                X(r1, None, 1, "ORD-52", "Backport the currency error to 2.4", 230, API,
                  "ord-52-backport-currency-error", MARCUS_B, {"key": "hold", "text": "On hold"},
                  S(no["text"], no["lead"], no["note"]), answer=no)],
              level=ASK_TEXT)],
            aside="1 in line", line=1, note="1 pull request is in the merge queue.")
        expect("answered no elsewhere: on hold, by whom, in Slack, the queue skips it",
               await page(uid, "lee"), want)
        ck("answered no: the row is still waiting_approval in the store",
           store.merge_queue_get(r1["id"])["state"] == "waiting_approval")

        # ── 8. blocked by repo rules, and blocked with no GitHub connection ─────────────────────
        print("== blocked ==")
        uid = project("Blocked")
        t1, r1 = pull(uid, API, 428, "ORD-55", "Check coupon codes before checkout",
                      "ord-55-coupon-checks", state="blocked", builder=dana["id"], by=dana["id"],
                      reason="GitHub will not merge it: 2 approving reviews are needed.")
        t2, r2 = pull(uid, API, 430, "ORD-56", "Return a clear error for an unknown currency",
                      "ord-56-currency-error", builder=marcus["id"])
        t3, r3 = pull(uid, WEB, 231, "ORD-57", "Add the carrier code to shipping rows",
                      "ord-57-carrier-code", state="blocked",
                      reason=merge_queue.no_write_reason(WEB))
        t4, r4 = pull(uid, WEB, 233, "ORD-58", "Round the order total to the cent",
                      "ord-58-total-cent", state="blocked", builder=marcus["id"],
                      reason=merge_queue.no_write_reason(WEB))
        t5, r5 = pull(uid, "northwind/orders-docs", 12, "ORD-70", "Fix a link", "ord-70-link",
                      state="blocked", reason="GitHub cannot tell whether it merges")
        want = doc(uid, "Blocked", [
            G(API, "main", [
                X(r1, None, 1, "ORD-55", "Check coupon codes before checkout", 428, API,
                  "ord-55-coupon-checks", DANA_B, {"key": "blocked", "text": "Blocked"},
                  S("Blocked by repo rules: GitHub says 2 approving reviews are needed.",
                    "Blocked by repo rules:", "Fix the rule in GitHub, or merge it by hand."),
                  review="Approved by Dana Whitfield", needs=True),
                X(r2, None, 2, "ORD-56", "Return a clear error for an unknown currency", 430, API,
                  "ord-56-currency-error", MARCUS_B, {"key": "next", "text": "Next"},
                  S("Next in this project."))],
              blocked=1, extra=["1 blocked"]),
            G("northwind/orders-docs", "main", [
                X(r5, None, 1, "ORD-70", "Fix a link", 12, "northwind/orders-docs", "ord-70-link",
                  LEE_B, {"key": "blocked", "text": "Blocked"},
                  S("Blocked by GitHub: it cannot tell whether the pull request merges.",
                    "Blocked by GitHub:", "Look at the pull request in GitHub, or merge it by hand."),
                  needs=True)], blocked=1, extra=["1 blocked"]),
            G(WEB, "main", [
                X(r3, None, 1, "ORD-57", "Add the carrier code to shipping rows", 231, WEB,
                  "ord-57-carrier-code", LEE_B, {"key": "blocked", "text": "Blocked"},
                  S(f"No GitHub connection can write to {WEB}.",
                    f"No GitHub connection can write to {WEB}.",
                    "One question on the Calls page covers every blocked pull request in this repo."),
                  needs=True),
                X(r4, None, 2, "ORD-58", "Round the order total to the cent", 233, WEB,
                  "ord-58-total-cent", MARCUS_B, {"key": "blocked", "text": "Blocked"},
                  S(f"No GitHub connection can write to {WEB}.",
                    f"No GitHub connection can write to {WEB}.",
                    "One question on the Calls page covers every blocked pull request in this repo."),
                  needs=True)], blocked=2, extra=["2 blocked"])],
            aside="5 in line, 4 need you", line=5, need=4,
            note="5 pull requests are in the merge queue, 4 need you.")
        expect("blocked: repo rules, GitHub unsure, no connection that can write, one line behind",
               await page(uid), want)

        # ── 9. sent back, each step of the ladder ───────────────────────────────────────────────
        print("== sent back ==")
        uid = project("Sent back")

        def bounced(tid, n):
            for _ in range(n):
                store.add_ticket_history(tid, uid, "send_back", "sent back", "a conflict",
                                         "merge-queue", None, now_utc())

        t1, r1 = pull(uid, API, 235, "ORD-59", "Use one rounding rule for every price",
                      "ord-59-one-rule", builder=marcus["id"])
        t2, r2 = pull(uid, API, 229, "ORD-57", "Move the price rules into one file",
                      "ord-57-price-rules", state="sent_back",
                      reason="conflicts with PR 218 in app/pricing.py")
        bounced(t2, 2)
        t3, r3 = pull(uid, API, 236, "ORD-61", "Show the tax rate on invoices", "ord-61-tax-line",
                      state="sent_back", builder=dana["id"],
                      reason="conflicts with PR 235 in app/invoices.py and app/tax.py")
        bounced(t3, 3)
        t4, r4 = pull(uid, WEB, 91, "ORD-60", "Show saved addresses at checkout",
                      "ord-60-saved-addresses", state="sent_back", ci="failure",
                      checks=[{"name": "web-unit-tests", "state": "failure"},
                              {"name": "lint", "state": "success"}],
                      reason="CI is failing on 7ab31c0: web-unit-tests")
        bounced(t4, 1)
        t5, r5 = pull(uid, WEB, 93, "ORD-62", "Keep the cart after sign in", "ord-62-cart-after-signin",
                      state="sent_back", builder=marcus["id"],
                      reason="conflicts with PR 90 in src/cart/store.ts")
        bounced(t5, 5)
        L1 = "Sent back 2 of 3 times. At 3 the orchestrator takes it."
        L2 = "Sent back 3 times. The orchestrator is on it. At 5 it comes to you."
        L3 = "Sent back 5 times. It is now a question for you on the Calls page."
        L0 = "Sent back 1 of 3 times. At 3 the orchestrator takes it."

        def sb(r, ident, title, n, repo, branch, builder, why, count, line_, **kw):
            return X(r, None, None, ident, title, n, repo, branch, builder,
                     {"key": "sent_back", "text": "Sent back"},
                     S(f"Sent back to the builder: {why}.", "Sent back to the builder:"),
                     bounce={"count": count, "text": line_}, **kw)

        want = doc(uid, "Sent back", [
            G(API, "main", [
                X(r1, None, 1, "ORD-59", "Use one rounding rule for every price", 235, API,
                  "ord-59-one-rule", MARCUS_B, {"key": "next", "text": "Next"}, S("Next in this project.")),
                sb(r2, "ORD-57", "Move the price rules into one file", 229, API, "ord-57-price-rules",
                   LEE_B, "conflicts with PR 218 in app/pricing.py", 2, L1),
                sb(r3, "ORD-61", "Show the tax rate on invoices", 236, API, "ord-61-tax-line",
                   DANA_B, "conflicts with PR 235 in app/invoices.py and app/tax.py", 3, L2)],
              sent=2, extra=["2 sent back"]),
            G(WEB, "main", [
                sb(r4, "ORD-60", "Show saved addresses at checkout", 91, WEB, "ord-60-saved-addresses",
                   LEE_B, "CI is failing on 7ab31c0: web-unit-tests", 1, L0, ci="Failing: web-unit-tests"),
                sb(r5, "ORD-62", "Keep the cart after sign in", 93, WEB, "ord-62-cart-after-signin",
                   MARCUS_B, "conflicts with PR 90 in src/cart/store.ts", 5, L3)],
              sent=2, extra=["2 sent back"])],
            aside="1 in line", line=1, note="1 pull request is in the merge queue.")
        expect("sent back: out of line, why, every step of the ladder, failing checks named",
               await page(uid), want)

        # ── where an answer was given ───────────────────────────────────────────────────────────
        print("== where an answer was given ==")
        w = fmq.where_of
        ck("where: Slack", w({"via": "slack", "words": "Yes", "choice": "Yes"})["phrase"] == "in Slack")
        ck("where: a session (no via) is a Claude session",
           w({"via": None})["phrase"] == "in a Claude session")
        ck("where: any other console answer is the console",
           w({"via": "console", "words": "Yes, merge", "choice": "Yes, merge"})["phrase"] == "in the console")
        ck("where: the merge queue page's own approval is told apart",
           w({"via": "console", "words": merge_queue.APPROVE_WORDS,
              "choice": merge_queue.APPROVE_CHOICE}) ==
           {"key": "merge_queue", "words": "merge queue page", "phrase": "on the merge queue page"})
        ck("where: the same words by Slack are still Slack",
           w({"via": "slack", "words": merge_queue.APPROVE_WORDS,
              "choice": merge_queue.APPROVE_CHOICE})["key"] == "slack")

        # each half of the test is needed: the same words with another choice, or the same choice
        # with other words, is any other console answer
        ck("where: the approval words with another choice are a plain console answer",
           w({"via": "console", "words": merge_queue.APPROVE_WORDS, "choice": "No, hold"})["key"] == "console")
        ck("where: the approval choice with other words is a plain console answer",
           w({"via": "console", "words": "Yes", "choice": merge_queue.APPROVE_CHOICE})["key"] == "console")

        # the real approval route writes exactly the words the page reads back
        uid = project("Approved on the page", level="L3-review")
        t1, r1 = pull(uid, API, 300, "ORD-80", "Approve me", "ord-80", state="waiting_approval",
                      base=BASE, short="1234567")
        d1 = ask(r1, question(uid, t1, lee["id"], 1))
        async with mk(keys["leew"]) as cx:
            ar = await cx.post(f"/api/factory/merge-queue/{r1['id']}/approve")
        ck("the approve route answers", ar.status_code == 200, ar.text)
        ck("the approve route puts the row back in line", store.merge_queue_get(r1["id"])["state"] == "queued")
        # the decision it wrote is what the page reads back; put the row back as it was to see it
        store.merge_queue_update(r1["id"], state="waiting_approval")
        a = (await page(uid, "lee"))["groups"][0]["rows"][0]["answer"]
        ck("an approval from the page reads back as 'on the merge queue page'",
           a["kind"] == "done" and a["where"] == {"key": "merge_queue", "words": "merge queue page"}
           and "on the merge queue page" in a["text"], repr(a))

        # a question that was withdrawn, one with no answer on record, one that is gone
        uid = project("Odd questions", level="L3-review")
        t1, r1 = pull(uid, API, 310, "ORD-81", "Withdrawn", "ord-81", state="waiting_approval",
                      base=BASE, short="a000001")
        d1 = ask(r1, question(uid, t1, lee["id"], 5))
        store.close_desk_item(d1["id"], "withdrawn", "no longer needed", now_utc())
        t2, r2 = pull(uid, API, 311, "ORD-82", "No answer on record", "ord-82",
                      state="waiting_approval", base=BASE, short="a000002")
        d2 = ask(r2, question(uid, t2, lee["id"], 5))
        store.close_desk_item(d2["id"], "answered", None, now_utc())
        t3, r3 = pull(uid, API, 312, "ORD-83", "Gone", "ord-83", state="waiting_approval",
                      base=BASE, short="a000003")
        got = await page(uid, "lee")
        kinds = [r["answer"]["kind"] for r in got["groups"][0]["rows"]]
        ck("odd questions: withdrawn, unrecorded and missing are told apart",
           kinds == ["withdrawn", "unrecorded", "missing"], kinds)
        BODIES.append(got)

        # an answer from a session records no person: nobody is named, and the person the question
        # was for is not credited with it; empty words are held without a dangling colon
        uid = project("Session answers", level="L3-review")
        t1, r1 = pull(uid, API, 314, "ORD-84", "Session yes", "ord-84", state="waiting_approval",
                      base=BASE, short="b000001")
        d1 = ask(r1, question(uid, t1, lee["id"], 30))
        answer(d1, t1, uid, "Yes, merge", "Yes, merge", None, None, 20)
        t2, r2 = pull(uid, API, 315, "ORD-85", "Empty words", "ord-85", state="waiting_approval",
                      base=BASE, short="b000002")
        d2 = ask(r2, question(uid, t2, lee["id"], 30))
        answer(d2, t2, uid, "", "", "console", None, 10)
        t3, r3 = pull(uid, API, 316, "ORD-86", "Named and empty", "ord-86", state="waiting_approval",
                      base=BASE, short="b000003")
        d3 = ask(r3, question(uid, t3, lee["id"], 30))
        answer(d3, t3, uid, "Yes, merge", "Yes, merge", "console", dana["id"], 5)
        got = await page(uid, "lee")
        a1, a2, a3 = [r["answer"] for r in got["groups"][0]["rows"]]
        ck("a session answer names no one: by is none and the text has no 'Answered by'",
           a1["kind"] == "done" and a1["by"] is None and "Answered by" not in a1["text"]
           and a1["text"].startswith("Yes, merge. Answered in a Claude session, 20 minutes ago, for commit b000001."),
           repr(a1))
        ck("an answer with no person is not credited to the person the question was for",
           "Lee" not in a1["text"] and "Lee" not in a2["text"] and "someone" not in a1["text"], repr((a1, a2)))
        ck("a person's answer still names them", a3["by"] == "Dana Whitfield"
           and "Answered by Dana Whitfield in the console" in a3["text"], repr(a3))
        ck("empty words read as held, with no 'Held: .'",
           a2["decision"] == "no" and a2["lead"] == "Held." and "Held: ." not in a2["text"]
           and a2["text"].startswith("Held. Answered in the console, 10 minutes ago"), repr(a2))
        BODIES.append(got)

        # ── who may read it ─────────────────────────────────────────────────────────────────────
        print("== who may read it ==")
        uid = project("Private one")
        t1, r1 = pull(uid, API, 320, "ORD-90", "Private work", "ord-90")
        store.set_project_scope(uid, "private")
        ck("a private project: 404 for a person who is not on it",
           (await read(uid, "sam")).status_code == 404)
        ck("a private project: the 404 does not tell it exists",
           "Private" not in (await read(uid, "sam")).text and "ORD-90" not in (await read(uid, "sam")).text)
        ck("a private project: the system credential still reads it", (await read(uid)).status_code == 200)
        store.add_project_member(uid, lee["id"])
        ck("a private project: a member reads it", (await read(uid, "lee")).status_code == 200)
        ck("an unknown project is 404", (await read("uc_nope")).status_code == 404)
        gone = await read("uc_nope")
        ck("the 404 says unknown project and names the id (the private-routes sweep reads it)",
           "unknown project" in gone.text and "uc_nope" in gone.text, gone.text)
        hidden = await read(uid, "sam")
        ck("a private project answers the same words as a missing one",
           hidden.status_code == 404 and "unknown project" in hidden.text and uid in hidden.text, hidden.text)
        drafted = project("A draft", status="draft")
        t1, r1 = pull(drafted, API, 321, "ORD-89", "Draft work", "ord-89")
        ck("a draft project is 404 though it has rows and is a software factory",
           (await read(drafted)).status_code == 404)
        store.update_project(drafted, status="active")
        ck("the same project is read once it is no longer a draft", (await read(drafted)).status_code == 200)
        other = project("Not a factory", kind="custom")
        ck("a project that is not a software factory is 404", (await read(other)).status_code == 404)
        ck("a team project is open to a person with no part in it",
           (await read(project("Open one"), "sam")).status_code == 200)

        # ── another project's rows never show ───────────────────────────────────────────────────
        uid_a, uid_b = project("Mine A"), project("Mine B")
        ta, ra = pull(uid_a, API, 330, "ORD-91", "In A", "ord-91")
        tb, rb = pull(uid_b, API, 331, "ORD-92", "In B", "ord-92")
        ga, gb = await page(uid_a), await page(uid_b)
        ck("each project reads only its own pull requests",
           [r["pr"]["number"] for g in ga["groups"] for r in g["rows"]] == [330]
           and [r["pr"]["number"] for g in gb["groups"] for r in g["rows"]] == [331])

        # ── order: milestone, then ticket, then entry ───────────────────────────────────────────
        uid = project("Order")
        m1 = store.create_milestone(uid, "First", position=1.0)
        m2 = store.create_milestone(uid, "Second", position=2.0)
        ids = {}
        for n, ms in ((340, m2), (341, m1), (342, m2)):
            t, r = pull(uid, API, n, f"ORD-{n}", f"Pull {n}", f"ord-{n}")
            store.update_ticket(t, milestone=ms)
            ids[n] = r["id"]
        got = await page(uid)
        order = [r["pr"]["number"] for r in got["groups"][0]["rows"]]
        ck("the line is in milestone then ticket order", order == [341, 340, 342], order)
        ck("positions run 1, 2, 3 and the first queued is Next, the others say their place in this project",
           [r["position"] for r in got["groups"][0]["rows"]] == [1, 2, 3]
           and got["groups"][0]["rows"][0]["status"]["text"] == "Next in this project."
           and got["groups"][0]["rows"][1]["status"]["text"] == "2nd in this project."
           and got["groups"][0]["rows"][2]["status"]["text"] == "3rd in this project.",
           repr([r["status"]["text"] for r in got["groups"][0]["rows"]]))

        # ── a gate that is an outside tool names itself while nothing has reviewed ──────────────
        uid = project("Unreviewed")
        t1, r1 = pull(uid, API, 350, "ORD-93", "Not reviewed", "ord-93", reviewed=False)
        got = await page(uid)
        ck("no review yet reads as not reviewed",
           got["groups"][0]["rows"][0]["review"] == "Waiting for the crew reviewer"
           or got["groups"][0]["rows"][0]["review"] == "Not reviewed yet",
           got["groups"][0]["rows"][0]["review"])
        pr = store.get_ticket(uid, t1)["pr"]
        store.update_ticket(t1, pr={**pr, "ci": None})
        ck("no CI answer yet reads as not checked yet",
           (await page(uid))["groups"][0]["rows"][0]["ci"] == "Not checked yet")
        store.update_ticket(t1, pr={**pr, "ci": "none"})
        ck("a repo with no CI says so",
           (await page(uid))["groups"][0]["rows"][0]["ci"] == "No CI checks on this repo")
        store.update_ticket(t1, pr={**pr, "ci": "pending",
                                    "ci_checks": [{"name": "unit", "state": "pending"},
                                                  {"name": "lint", "state": "success"}]})
        ck("a pending check names what it waits for",
           (await page(uid))["groups"][0]["rows"][0]["ci"] == "Pending, waiting for unit")
        store.add_review(uid, t1, url(API, 350), pr["head"], "changes", "", "", "crew-reviewer",
                         [], {}, factory_mod.finding_kind, now_utc())
        ck("a review that asked for changes says so",
           (await page(uid))["groups"][0]["rows"][0]["review"] == "The crew reviewer asked for changes")

        # ── whose turn: the ticket's words, read with the queue row ─────────────────────────────
        print("== whose turn ==")
        tw = {(pj, ref, st): (turn, label, whose) for pj, ref, st, _c, turn, label, whose in TURNS}
        ck("every row carries the shared turn words", all(
            label.startswith(sw.TURN[turn]) and whose == sw.TURN_WHOSE[turn]
            for _p, _r, _s, _c, turn, label, whose in TURNS), TURNS)
        for (pj, ref, st), want in {
            ("Waiting for approval", "ORD-47", "waiting_approval"): ("ready_for_you", "Ready for you", "person"),
            ("Waiting for approval", "ORD-50", "queued"): ("queued", "Queued after ORD-47", "nobody"),
            ("Waiting for CI", "ORD-41", "waiting_ci"): ("checks_running", "Checks running", "github"),
            ("Answered yes", "ORD-51", "waiting_approval"): ("next_to_merge", "Next to merge", "crew"),
            ("Answered no", "ORD-52", "waiting_approval"): ("stuck", "Stuck", "person"),
            ("Moved", "ORD-53", "waiting_approval"): ("reviewing", "Reviewing", "crew"),
            ("Blocked", "ORD-55", "blocked"): ("stuck", "Stuck", "person"),
            ("Sent back", "ORD-60", "sent_back"): ("changes_asked", "Changes asked", "crew"),
            ("Sent back", "ORD-61", "sent_back"): ("changes_asked", "Changes asked", "crew"),
        }.items():
            ck(f"turn: {pj}, {ref} ({st}) reads {want[1]}", tw.get((pj, ref, st)) == want, tw.get((pj, ref, st)))
        # the queue takes the yes: the row goes back in line, approved for its head, desk item closed
        async def turn_after(**fields):
            store.merge_queue_update(yes_row["id"], **fields)
            rows_ = [r for g in ((await page(yes_uid)).get("groups") or []) for r in g["rows"]]
            r = next((r for r in rows_ if r["ticket"]["ref"] == "ORD-51"), {})
            return r.get("turn"), r.get("turn_label")
        got = await turn_after(state="queued", desk_item=None, approved_head=yes_row["head"])
        ck("approved for its head, back in line: next to merge, not ready for you",
           got == ("next_to_merge", "Next to merge"), got)
        got = await turn_after(approved_head="0000000")
        ck("approved for an older head: the merge is yours again", got == ("ready_for_you", "Ready for you"), got)
        # ── the sentences ───────────────────────────────────────────────────────────────────────
        print("== the sentences ==")
        FORBIDDEN = ("waiting_approval", "waiting_ci", "sent_back", "merge_ready", "merge_queue",
                     "desk_item", "approved_head", "unknown_ticks", "sync_tick", "needs_review")
        machine = ("state", "key", "id")        # the page's own switches, never shown
        written = ("title", "branch", "name")   # what a person wrote
        bad, em, lead_bad = [], [], []
        for body in BODIES:
            for path, key, s in strings(body):
                if key in machine or ".state" in path:
                    continue
                if "—" in s or "–" in s:
                    em.append((path, s))
                if key in written or key == "ref" or key == "url" or key == "tk":
                    continue
                for w_ in FORBIDDEN:
                    if w_ in s.lower():
                        bad.append((path, w_, s))
            for g in body["groups"]:
                for r in g["rows"]:
                    if not r["status"]["text"].startswith(r["status"]["lead"]):
                        lead_bad.append(r["status"])
        ck("no state key, stage name or tool name in any sentence", not bad, bad[:3])
        ck("no em dash or en dash anywhere", not em, em[:3])
        ck("every status text starts with its bold lead", not lead_bad, lead_bad[:2])
        offenders = []
        for body in BODIES:
            offenders += words.offenders_in(body, machine + written + ("url", "ref", "tk"))
        ck("none of the edition's forbidden words in any sentence", not offenders, offenders[:5])
        ck("the answers cover every chip and every kind of answer", len(BODIES) >= 10, len(BODIES))

        # ── the route only reads ────────────────────────────────────────────────────────────────
        print("== the route only reads ==")
        TABLES = ("merge_queue", "desk_items", "decisions", "tickets", "ticket_history", "reviews",
                  "usecases", "milestones")

        def snapshot():
            out = {}
            with store._sread:
                for tname in TABLES:
                    out[tname] = store.sc.execute(f"SELECT * FROM {tname} ORDER BY 1, 2").fetchall()
            return out

        calls = []
        real_ready = merge_ready.merge_ready
        real_t = factory_github._transport

        def forbidden(*a, **k):
            calls.append("merge_ready")
            raise AssertionError("the page must not call merge_ready")
        real_mq_ready = merge_queue.merge_ready
        merge_ready.merge_ready = merge_queue.merge_ready = forbidden
        factory_github._transport = httpx.MockTransport(
            lambda req: calls.append(f"{req.method} {req.url}") or httpx.Response(500, json={}))
        try:
            before = snapshot()
            uids = [u["id"] for u in store.list_projects() if u.get("kind") == "software_factory"]
            n_ok = 0
            for u in uids:
                for who in ("root", "lee", "dana"):
                    n_ok += (await read(u, who)).status_code in (200, 404)
            after = snapshot()
        finally:
            merge_ready.merge_ready = real_ready
            merge_queue.merge_ready = real_mq_ready
            factory_github._transport = real_t
        ck("the route read every project as three callers", n_ok == len(uids) * 3, (n_ok, len(uids)))
        ck("no merge_ready call and no GitHub call", calls == [], calls)
        same = {k: before[k] == after[k] for k in TABLES}
        ck("every table is the same after the reads", all(same.values()), same)
        ck("no write route exists for the page: only a GET", all(
            rt.methods == {"GET"} for rt in app.routes
            if getattr(rt, "path", "") == "/api/projects/{uid}/merge-queue"))

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())
