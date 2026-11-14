"""The factory Reviews page (TR-735): GET /api/factory/reviews.

Every pull request in review is in one of three lists: waiting for the person, with the crew, or
nobody is on it (TR-1009). The split is one rule (factory_waiting.review_split, assumption A10):
it waits for the person when the project's autonomy is L3-review and the reviewer passed it on its
current commit; it is with the crew only with a named next step; the rest is nobody's, with the
reason. A ticket closed in its home leaves review (TR-1008). Checked here: the split,
the sort, the wording of each row, the project filter, the autonomy sentence, the milestone a pull
request holds up, that the home page's "PRs to review" and this page always agree (under L3, under
L4-ship and for a project that sets its own), that /api/review-queue still carries every old field,
and the words of the answer.

Run: .venv/bin/python tests/test_factory_reviews_page.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-factory-reviews-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-reviews"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"

import httpx

from tares import factory as factory_mod
from tares import factory_home as fh
from tares import words
from tares.screens import factory_reviews as fr
from tares.store import now_utc

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def units():
    print("== the helpers ==")
    ck("review words", [fr.review_words(v) for v in (
        {"verdict": "pass", "round": 2, "current": True}, {"verdict": "pass", "round": 1, "current": False},
        {"verdict": "changes", "round": 1, "current": True}, {"verdict": "block", "round": 3}, None)] == [
        "Passed, round 2", "Passed on an older commit", "Changes asked, round 1",
        "Blocked by the reviewer, round 3", "Not reviewed yet"], "")
    ms = [{"id": "a", "name": "1. Foundations"}, {"id": "b", "name": "Faster lists"},
          {"id": "c", "name": "M3: Search"}, {"id": "d", "name": "4) Wrap up"}]
    ck("a milestone already numbered keeps its own number", [fr.milestone_words(ms, i) for i in "acd"] == [
        "Holds up 1. Foundations", "Holds up M3: Search", "Holds up 4) Wrap up"], "")
    ck("an unnumbered one gets M and its place", fr.milestone_words(ms, "b") == "Holds up M2: Faster lists")
    ck("an unknown milestone holds up nothing", fr.milestone_words(ms, "zz") is None
       and fr.milestone_words(ms, None) is None)


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    mk = lambda token=TOKEN: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t",
        headers={"Authorization": f"Bearer {token}"} if token else {})

    def ago(**kw):
        return now_utc() - timedelta(**kw)

    def back(tid, **kw):
        with store._swrite:
            store.sc.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", [ago(**kw), tid])

    def project(name):
        uid = f"uc_{name.replace('-', '')}"
        store.create_project(uid, "custom", name, {}, goal="A project for the test")
        store.set_project_kind(uid, "software_factory")
        return uid

    def in_review(uid, title, verdict="pass", *, head="aaaaaaa1111", reviewed="aaaaaaa1111",
                  ci="success", n=None, milestone=None, rounds=1, **waited):
        """A ticket in review with a pull request, the reviewer's verdict on `reviewed`, waiting
        for the given time. `verdict` None means it was never reviewed."""
        n = n or (len(title) + 100)
        tid = store.create_ticket(uid, "tares", title, milestone=milestone, repo="example-org/service-a")
        store.update_ticket(tid, stage="review", pr={
            "url": f"https://github.com/example-org/service-a/pull/{n}", "number": n,
            "repo": "example-org/service-a", "head": head, "ci": ci, "title": f"PR: {title}"})
        if verdict:
            for r in range(rounds):
                store.add_review(uid, tid, None, reviewed, "changes" if r < rounds - 1 else verdict,
                                 "ran the tests", None, "crew-reviewer", [], {},
                                 factory_mod.finding_kind, now_utc())
        back(tid, **waited)
        return tid

    async def put_crew(cx, autonomy):
        r = await cx.put("/api/crew/settings", json={"autonomy": autonomy})
        assert r.status_code == 200, r.text

    async def reviews(cx, query=""):
        r = await cx.get("/api/factory/reviews" + query)
        assert r.status_code == 200, r.text
        return r.json()

    async def home_prs(cx):
        fh._cache.clear()
        r = await cx.get("/api/home")
        assert r.status_code == 200, r.text
        return [i for i in r.json()["waiting_for_you"] if i["kind"] == "prs_to_review"]

    def refs(rows):
        return [(r["project"]["name"], r["ticket"]["title"]) for r in rows]

    async def agree(cx, label, query=""):
        """Home's PRs to review, per project, name the same pull requests this page lists as waiting."""
        a = await reviews(cx, query)
        home = {i["project"]["id"]: i for i in await home_prs(cx)}
        want = {}
        for r in a["waiting_for_you"]:
            want.setdefault(r["project"]["id"], []).append(r)
        same = set(home) == set(want) and all(
            home[p]["label"] == ("1 PR to review" if len(rs) == 1 else f"{len(rs)} PRs to review")
            and all(r["ticket"]["title"] in home[p]["detail"] for r in rs) for p, rs in want.items())
        ck(f"{label}: the home page and the Reviews page list the same pull requests", same,
           (sorted(home), sorted(want)))
        return a

    async with app.router.lifespan_context(app):
        async with mk() as cx:
            print("== a cell with nothing in review ==")
            a = await reviews(cx)
            ck("empty lists, the L3 sentence, no project", a["waiting_for_you"] == [] and a["with_the_crew"] == []
               and a["autonomy_text"] == "You merge every pull request after the crew's review."
               and a["project"] is None and "now" in a, a)
            ck("the answer's keys", set(a) == {"waiting_for_you", "waiting_for_others", "with_the_crew", "nobody_on_it",
                                               "autonomy_text", "project", "now"}, sorted(a))

            px, py, pu = project("project-x"), project("project-y"), project("project-u")
            # the crew has project-x and project-y (handed over, autonomy the crew's own)
            for p_ in (px, py):
                store.set_handover(p_, [{"path": "/w/a", "name": "example-org/service-a"}], None, None, now_utc())
            ms_found = store.create_milestone(px, "1. Foundations")
            ms_setup = store.create_milestone(py, "Setup")
            ms_fast = store.create_milestone(py, "Faster lists")
            # project-x: three passed on the current commit (6 days, 3 hours, 10 minutes), one passed on
            # an older commit, one with changes asked, one never reviewed with CI going, one
            # reviewed twice, one with no CI at all
            x_old = in_review(px, "Add the footer", milestone=ms_found, days=6, n=11, rounds=2)
            x_mid = in_review(px, "Add the sitemap", hours=3, n=12, ci="failure")
            x_new = in_review(px, "Add the search box", minutes=10, n=13, ci=None)
            x_old_head = in_review(px, "Add the news page", head="bbbbbbb2222ff", days=2, n=14)
            x_changes = in_review(px, "Add the header", verdict="changes", hours=30, n=15)
            x_none = in_review(px, "Add the archive", verdict=None, ci="pending", minutes=8, n=16)
            # project-y: one pass that holds up its second milestone
            y_hold = in_review(py, "Batch the lists", milestone=ms_fast, hours=50, n=21)
            # a pull request with an open sibling in its milestone does not hold the milestone up
            y_free = in_review(py, "Tidy the lists", verdict=None, milestone=ms_setup, minutes=5, n=22)
            store.create_ticket(py, "tares", "Other work", milestone=ms_setup)
            # project-u: nothing in review

            print("== the split under L3-review (the default) ==")
            a = await agree(cx, "L3")
            wait, crew = a["waiting_for_you"], a["with_the_crew"]
            ck("waiting for you: the passed ones, longest waiting first", refs(wait) == [
                ("project-x", "Add the footer"), ("project-y", "Batch the lists"),
                ("project-x", "Add the sitemap"), ("project-x", "Add the search box")], refs(wait))
            ck("with the crew: the rest, longest waiting first", refs(crew) == [
                ("project-x", "Add the news page"), ("project-x", "Add the header"),
                ("project-x", "Add the archive"), ("project-y", "Tidy the lists")], refs(crew))
            ck("with the crew: each names who has it next", {r["ticket"]["title"]: r["why"] for r in crew} == {
                "Add the news page": "The reviewer reviews it",
                "Add the header": "The builder works on the changes the reviewer asked for",
                "Add the archive": "The reviewer reviews it", "Tidy the lists": "The reviewer reviews it"},
               {r["ticket"]["title"]: r["why"] for r in crew})
            ck("waiting for you carries no why", all(r["why"] is None for r in wait))
            ck("nobody is on none of them", a["nobody_on_it"] == [], refs(a["nobody_on_it"]))
            ck("the autonomy sentence for L3", a["autonomy_text"] == "You merge every pull request after the crew's review.")
            ck("waited is in words, never abbreviated", [r["waited"] for r in wait] == ["6 days", "2 days", "3 hours", "10 minutes"]
               and not any("min " in r["waited"] + " " or r["waited"].endswith("min") for r in wait + crew),
               [r["waited"] for r in wait])
            ck("since is a time", all(r["since"] and r["since"].startswith("20") for r in wait + crew))
            r0 = wait[0]
            ck("a row carries its project, ticket, pull request", r0["project"] == {"id": px, "name": "project-x"}
               and r0["ticket"] == {"ref": "T1", "title": "Add the footer", "href": f"/projects/{px}/tickets/T1"}
               and r0["pr"] == {"number": 11, "url": "https://github.com/example-org/service-a/pull/11",
                                "title": "PR: Add the footer", "repo": "example-org/service-a"}, r0)
            ck("a row's own keys", all(set(r) == {"project", "ticket", "pr", "waited", "since", "review", "ci",
                                                  "ci_state", "holds_up", "codex_open", "why"} for r in wait + crew))
            by = {r["ticket"]["title"]: r for r in wait + crew}
            ck("review words", {t: by[t]["review"] for t in by} == {
                "Add the footer": "Passed, round 2", "Add the sitemap": "Passed, round 1",
                "Add the search box": "Passed, round 1", "Add the news page": "Passed on an older commit",
                "Add the header": "Changes asked, round 1", "Add the archive": "Not reviewed yet",
                "Batch the lists": "Passed, round 1", "Tidy the lists": "Not reviewed yet"}, {t: by[t]["review"] for t in by})
            ck("CI words and dots", {t: (by[t]["ci"], by[t]["ci_state"]) for t in by} == {
                "Add the footer": ("CI passed", "ok"), "Add the sitemap": ("CI failing", "bad"),
                "Add the search box": ("No CI", "none"), "Add the news page": ("CI passed", "ok"),
                "Add the header": ("CI passed", "ok"), "Add the archive": ("CI in progress", "go"),
                "Batch the lists": ("CI passed", "ok"), "Tidy the lists": ("CI passed", "ok")}, {t: (by[t]["ci"], by[t]["ci_state"]) for t in by})
            ck("holds up: a numbered milestone keeps its name, an unnumbered one gets M and its place",
               by["Add the footer"]["holds_up"] == "Holds up 1. Foundations"
               and by["Batch the lists"]["holds_up"] == "Holds up M2: Faster lists"
               and all(r["holds_up"] is None for t, r in by.items() if t not in ("Add the footer", "Batch the lists")),
               {t: r["holds_up"] for t, r in by.items()})
            ck("nothing was opened for the challenger", all(r["codex_open"] is False for r in wait + crew))

            print("== the project filter ==")
            f = await reviews(cx, f"?project={px}")
            ck("one project: its own rows only, the filter named", refs(f["waiting_for_you"]) == [
                ("project-x", "Add the footer"), ("project-x", "Add the sitemap"), ("project-x", "Add the search box")]
               and len(f["with_the_crew"]) == 3 and f["project"] == {"id": px, "name": "project-x"}, f)
            f = await reviews(cx, f"?project={pu}")
            ck("a project with nothing in review: empty lists, still named", f["waiting_for_you"] == []
               and f["with_the_crew"] == [] and f["project"] == {"id": pu, "name": "project-u"}, f)
            r = await cx.get("/api/factory/reviews?project=nope")
            f = r.json()
            ck("an unknown project is not a 404: empty lists, no project", r.status_code == 200
               and f["waiting_for_you"] == [] and f["with_the_crew"] == [] and f["project"] is None, r.text)

            print("== the same rule under L4-ship ==")
            await put_crew(cx, "L4-ship")
            a = await agree(cx, "L4")
            ck("nothing waits for you, every pull request is with the crew", a["waiting_for_you"] == []
               and len(a["with_the_crew"]) == 8, (len(a["waiting_for_you"]), len(a["with_the_crew"])))
            ck("the autonomy sentence for L4", a["autonomy_text"] == "The crew merges after its review and green CI.")
            ck("still longest waiting first", [r["ticket"]["title"] for r in a["with_the_crew"]][:3] == [
                "Add the footer", "Batch the lists", "Add the news page"], refs(a["with_the_crew"]))
            ck("the home page lists none", await home_prs(cx) == [])
            l4 = {r["ticket"]["title"]: r["why"] for r in a["with_the_crew"]}
            ck("under L4: a passed, green one is the crew's merge, a red one the builder's",
               l4["Add the footer"] == "The crew merges it" and l4["Add the sitemap"] == "The builder fixes the failing CI"
               and l4["Add the search box"] == "The crew merges it", l4)

            print("== a project that sets its own autonomy ==")
            store.set_handover(py, [{"path": "/w/a", "name": "example-org/service-a"}], "L3-review", None, now_utc())
            a = await agree(cx, "project L3 under crew L4")
            ck("project-y waits for you under a crew at L4-ship", refs(a["waiting_for_you"]) == [("project-y", "Batch the lists")]
               and len(a["with_the_crew"]) == 7, refs(a["waiting_for_you"]))
            f = await reviews(cx, f"?project={py}")
            ck("filtered to it, the sentence follows the project", f["autonomy_text"].startswith("You merge")
               and len(f["waiting_for_you"]) == 1, f)
            f = await reviews(cx, f"?project={px}")
            ck("filtered to another, the crew's sentence", f["autonomy_text"].startswith("The crew merges"), f)
            await put_crew(cx, "L3-review")
            store.set_handover(px, [{"path": "/w/a", "name": "example-org/service-a"}], "L4-ship", None, now_utc())
            a = await agree(cx, "project L4 under crew L3")
            # the cell's L3-review is a ceiling the merge queue keeps (merge_queue.autonomy_of): a
            # project's own L4-ship does not let the crew merge, so its passed PRs still wait
            ck("under a cell at L3-review a project's own L4-ship does not let the crew merge: both still wait",
               ("project-y", "Batch the lists") in refs(a["waiting_for_you"])
               and any(r[0] == "project-x" for r in refs(a["waiting_for_you"])), refs(a["waiting_for_you"]))
            store.set_handover(px, [{"path": "/w/a", "name": "example-org/service-a"}], None, None, now_utc())
            a = await agree(cx, "crew default again")
            ck("a project with no autonomy follows the crew again", len(a["waiting_for_you"]) == 4, len(a["waiting_for_you"]))

            print("== a merged pull request ==")
            store.update_ticket(x_new, stage="merged")
            a = await agree(cx, "after a merge")
            ck("a merged ticket leaves the page", all(r["ticket"]["title"] != "Add the search box"
                                                       for r in a["waiting_for_you"] + a["with_the_crew"]))

            print("== nobody is on it (TR-1009) ==")
            pn = project("project-n")
            n_pass = in_review(pn, "Own session pass", hours=2, n=31)
            n_open = in_review(pn, "Own session open", verdict=None, hours=1, n=32)
            a = await agree(cx, "never handed over")
            nob = {r["ticket"]["title"]: r["why"] for r in a["nobody_on_it"]}
            ck("a project never handed over: a passed one still waits for you",
               ("project-n", "Own session pass") in refs(a["waiting_for_you"]), refs(a["waiting_for_you"]))
            ck("a project never handed over: the rest is nobody's, and says so",
               nob == {"Own session open": "The project was never handed to the crew"}, nob)
            store.set_handover(pn, [{"path": "/w/a", "name": "example-org/service-a"}], None, None, now_utc())
            store.take_over(pn, "I will finish it", None, now_utc())
            nob = {r["ticket"]["title"]: r["why"] for r in (await agree(cx, "taken over"))["nobody_on_it"]}
            ck("a project taken over: nobody in the crew has it", nob == {"Own session open": "You took this project over from the crew"}, nob)
            store.update_project(pn, status="done")
            a = await agree(cx, "finished")
            nob = {r["ticket"]["title"]: r["why"] for r in a["nobody_on_it"]}
            fin = "The project is finished, so the crew no longer works on it"
            ck("a finished project: every pull request in review is nobody's, a passed one too",
               nob == {"Own session pass": fin, "Own session open": fin}
               and not any(r["project"]["id"] == pn for r in a["waiting_for_you"] + a["with_the_crew"]), nob)
            pr_ = store.get_ticket(px, x_changes)["pr"]
            store.update_ticket(x_changes, pr={**pr_, "state": "closed", "merged": False})
            a = await agree(cx, "closed without merging")
            ck("a pull request closed without merging is nobody's",
               {r["ticket"]["title"]: r["why"] for r in a["nobody_on_it"]}.get("Add the header")
               == "The pull request was closed without merging", refs(a["nobody_on_it"]))
            x_block = in_review(px, "Add the blocker", verdict="block", hours=1, n=17)
            a = await agree(cx, "blocked by the reviewer")
            ck("a pull request the reviewer blocked is nobody's, and needs the person",
               {r["ticket"]["title"]: r["why"] for r in a["nobody_on_it"]}.get("Add the blocker")
               == "The reviewer blocked it; it needs your decision", refs(a["nobody_on_it"]))

            print("== a ticket closed in its home leaves review (TR-1008) ==")
            store.update_ticket(x_none, status="done", why="Linear marked it Done")
            t_ = store.get_ticket(px, x_none)
            a = await agree(cx, "closed in Linear")
            ck("done in its home: the stage is cleared and the history says why", t_["stage"] is None
               and store.ticket_history(x_none)[-1]["reason"] == "Linear marked it Done", (t_["stage"], store.ticket_history(x_none)[-1:]))
            ck("and it is on no list", not any(r["ticket"]["title"] == "Add the archive"
                                              for r in a["waiting_for_you"] + a["with_the_crew"] + a["nobody_on_it"]))
            with store._swrite:   # a ticket closed before TR-1008 kept its stage
                store.sc.execute("UPDATE tickets SET status = 'canceled', stage = 'review' WHERE id = ?", [y_free])
            q = (await cx.get("/api/review-queue")).json()["queue"]
            ck("the review queue leaves out a canceled ticket even before the repair",
               not any(x["title"] == "Tidy the lists" for x in q))
            store._ticket_stage_repair()
            ck("the repair clears its stage", store.get_ticket(py, y_free)["stage"] is None)
            store._ticket_stage_repair()
            ck("a second repair finds nothing", sum(1 for x in store.ticket_history(y_free) if x["field"] == "stage") == 1,
               store.ticket_history(y_free))
            store.update_ticket(x_new, status="done")    # merged above
            ck("a merged ticket marked done keeps merged", store.get_ticket(px, x_new)["stage"] == "merged")

            print("== the review queue keeps its fields and adds the new ones ==")
            q = (await cx.get("/api/review-queue")).json()["queue"]
            old = {"project", "project_name", "ticket", "title", "holder", "repo", "pr", "head", "waiting_since",
                   "blocks_milestone", "layer1_clean", "verdict", "waiting_minutes"}
            ck("every old field is still there", q and all(old <= set(x) for x in q), sorted(q[0]) if q else q)
            qi = next(x for x in q if x["title"] == "Add the footer")
            ck("number, pr_title and ci are added, title is still the ticket's", qi["number"] == 11
               and qi["pr_title"] == "PR: Add the footer" and qi["ci"] == "success" and qi["title"] == "Add the footer", qi)
            ck("the ticket's milestone id is added", qi["milestone"] == ms_found, qi)

            print("== the words ==")
            a = await reviews(cx)
            user_text = ("name", "title", "repo", "url", "ref", "href")
            clean = words.offenders_in(a, user_text)
            ck("no forbidden word or dash outside the person's own text", clean == [], clean)
            ck("no minutes abbreviation in any sentence", not any(
                "min" in s.split() or s.endswith(" min") for r in a["waiting_for_you"] + a["with_the_crew"]
                for s in (r["waited"], r["review"], r["ci"], r["holds_up"] or "")))

            print("== the token ==")
            ck("the page is behind the token", (await mk(None).get("/api/factory/reviews")).status_code == 401)


units()
asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
