"""The short texts written from figures, and the summary of an accepted milestone (TR-1645).

Four parts: the number check, `figures_text` and its template, the summary through the API (queued
on acceptance, written on first read, once, "Write again", who may read), and the table's revision.
No model is ever called: a fake provider stands in, and the model settings are removed from the
environment before anything is imported.

    python tests/test_report_words.py

The revision part runs against Postgres too when TARES_TEST_PG is set.
"""
import asyncio
import os
import sys
import tempfile
import uuid
from datetime import timedelta

sys.path.insert(0, os.path.dirname(__file__))
from _env import scrub_os_environ  # noqa: E402

scrub_os_environ()
for key in [k for k in os.environ
            if k.startswith(("ANTHROPIC_", "OPENAI_", "TARES_PLATFORM_PROVIDER_", "TARES_SLACK_"))
            or k in ("TARES_ANTHROPIC_BASE", "TARES_MODEL_PROVIDER", "TARES_AGENT_MODEL")]:
    del os.environ[key]

TMP = tempfile.mkdtemp()
TOKEN = "root-token"
os.environ["TARES_DB"] = os.path.join(TMP, "tares.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "catalog.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"
os.environ["TARES_PR_SYNC_SECONDS"] = "off"

import httpx  # noqa: E402
from alembic import command  # noqa: E402
from sqlalchemy import inspect, text  # noqa: E402

from tares import factory as factory_mod  # noqa: E402
from tares import milestone_summary, report_words  # noqa: E402
from tares.daemon import make_app  # noqa: E402
from tares.envelope import now_utc  # noqa: E402
from tares.models import ModelReply  # noqa: E402
from tares.report_words import numbers_ok, unbacked  # noqa: E402
from tares.session_store import SessionStore  # noqa: E402
from tares.session_store.tables import metadata  # noqa: E402

PG = os.environ.get("TARES_TEST_PG")
HEAD = "0026_drop_catalog_sources"   # the newest revision: an upgrade ends there
P = F = 0


def ck(label, cond, detail=""):
    global P, F
    if cond:
        P += 1
        print(f"  ok   {label}")
    else:
        F += 1
        print(f"  FAIL {label} {detail}")


class Fake:
    """A model provider that answers what the test told it to."""
    kind = "anthropic"

    def __init__(self):
        self.reset()

    def reset(self):
        self.calls = []
        self.text = "The slides are done."
        self.stop = "end_turn"
        self.error = None
        self.gate = None          # an asyncio.Event the answer waits for
        self.now = self.most = 0  # calls under way, and the most at once

    async def complete(self, **kw):
        self.calls.append(kw)
        self.now += 1
        self.most = max(self.most, self.now)
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.error is not None:
                raise self.error
            return ModelReply(text=self.text, tool_calls=[], stop_reason=self.stop,
                              usage={"input_tokens": 900, "output_tokens": 120,
                                     "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0})
        finally:
            self.now -= 1


# ── the number check ─────────────────────────────────────────────────────────
FIGURES = {
    "delivered": {"count": 3, "tickets": [
        {"ref": "T12", "title": "Raise the limit to 6 digits"},
        {"ref": "T13", "title": "Release v1.2.3 on 2026-11-24"},
        {"ref": "T14", "title": "Keep the 30 day history"}]},
    "cost_usd": 12.5, "spend_usd": 1234.567, "done_pct": 46.2, "kept_share": 0.4567,
    "build_h": 1.2833, "open_age_s": 3870, "wait_days": 2, "lines": 1234,
    "weeks": [{"merged": 4}, {"merged": 21}], "flag": True,
}


def number_check():
    print("== the number check ==")
    good = [
        ("no numbers at all", "The slides are done."),
        ("a count", "3 tickets were delivered."),
        ("a count in a nested list", "21 were merged, after 4."),
        ("a number written as a word", "Three tickets were delivered, then twenty-one."),
        ("thousands with a comma", "It added 1,234 lines."),
        ("money", "It cost $12.50."),
        ("money in words", "It cost 12.50 dollars."),
        ("money rounded to cents", "Spend was $1,234.57."),
        ("a percent rounded as the page does", "46% is done."),
        ("a percent with a decimal", "46.2 percent is done."),
        ("a share shown as a percent", "45.7% was kept."),
        ("a duration with one decimal", "The build took 1.3 h."),
        ("a duration in two units", "It has been open for 1 hour 4 minutes."),
        ("a duration cut to its largest unit", "It has been open for 1 h."),
        ("a duration in days", "It waited 2 days."),
        ("a ticket's name", "T12 and T13 are in."),
        ("a version and a date from a title", "v1.2.3 went out on 2026-11-24."),
        ("a number quoted from a title", "Numbers now run to 6 digits."),
        ("one that counts nothing", "One of the tickets raised the limit, the one about history."),
    ]
    for label, t in good:
        ck(f"passes: {label}", numbers_ok(t, FIGURES), unbacked(t, FIGURES))
    bad = [
        ("an invented count", "5 tickets were delivered."),
        ("an invented count written as a word", "Seven tickets were delivered."),
        ("a count off by one", "It added 1,235 lines."),
        ("a count rounded off", "About 1,200 lines were added."),
        ("money that is not the figure", "It cost $13."),
        ("money cut to whole dollars", "It cost $12."),
        ("a count read as money", "It cost $3."),
        ("a count read as a percent", "3% is done."),
        ("a percent off by one", "47% is done."),
        ("a fraction not shown as a percent", "0.4567% was kept."),
        ("a duration that is not the figure", "The build took 1.4 h."),
        ("the wrong unit", "The build took 1.3 days."),
        ("a duration in two units off by a minute", "It has been open for 1 hour 5 minutes."),
        ("a ticket that is not there", "T99 is in."),
        ("a version that is not there", "v1.3.0 went out."),
        ("a date that is not there", "It went out on 2026-11-23."),
        ("a number from a title put beside another word", "The team closed 6 bugs."),
        ("a duration from a title read as a count", "The team closed 30 bugs."),
        ("a negative count", "The change was -3 tickets."),
        ("a vague amount", "A dozen fixes went in."),
        ("a half", "Half of it is left."),
        ("a number hidden in a good sentence", "3 tickets were delivered and 8 bugs fixed."),
    ]
    for label, t in bad:
        ck(f"refused: {label}", not numbers_ok(t, FIGURES), t)
    ck("it names what it refuses", unbacked("3 tickets and 8 bugs.", FIGURES) == ["8"],
       unbacked("3 tickets and 8 bugs.", FIGURES))
    ck("a yes or no in the figures is not the number 1",
       not numbers_ok("1 ticket.", {"done": True}))
    ck("a figure that is no number is ignored",
       not numbers_ok("7 left.", {"left": float("nan"), "list": [None]}))


# ── figures_text ─────────────────────────────────────────────────────────────
FACTS = {
    "project": "Slides",
    "milestone": {"name": "M1: The slides", "goal": "A deck people can open"},
    "delivered": {"count": 2, "tickets": [{"ref": "T1", "title": "Draw the first slide"},
                                          {"ref": "T2", "title": "Add 4 charts \u2014 and notes"}]},
    "decisions": {"count": 1, "items": [{"ref": "D1", "question": "Which export format?",
                                         "answer": "PDF only"}]},
    "left": {"count": 3, "milestone_count": 1, "later_milestones": [
        {"name": "M2: The search page", "open_count": 3, "tickets": []}]},
}
TEMPLATE_TEXT = ("Delivered: 2 tickets. Draw the first slide; Add 4 charts, and notes.\n"
                 "Decided on the way: 1. Which export format? PDF only.\n"
                 "Left: 3 open tickets in later milestones. M2: The search page, 3.")


async def writing_the_text(store, fake):
    print("== figures_text ==")
    report_words.resolve_provider = lambda s: (None, "")
    got = await report_words.figures_text("milestone", FACTS, store=store)
    ck("no key: the template, marked template:no-key", got == (TEMPLATE_TEXT, "template:no-key"), got)
    ck("the template's own numbers pass the check", numbers_ok(got[0], FACTS), unbacked(got[0], FACTS))
    ck("the template has no em dash", "\u2014" not in got[0])

    report_words.resolve_provider = lambda s: (fake, "console")
    used = []
    record = lambda *a: used.append(a)

    async def ask(**kw):
        return await report_words.figures_text("milestone", FACTS, store=store, record_usage=record, **kw)

    fake.reset()
    fake.text = "2 tickets delivered.\n\n  Export is PDF only \u2014 decided.  \n3 open in M2."
    got = await ask()
    ck("a text whose numbers are in the figures is kept", got == (
        "2 tickets delivered.\nExport is PDF only, decided.\n3 open in M2.", "model"), got)
    call = fake.calls[0]
    ck("the model gets no tools", call["tools"] == [] and call["tools_allowed"] is False, call)
    ck("it gets the figures and the rules", '"Draw the first slide"' in call["messages"][0]["content"]
       and "Use only the facts" in call["system"] and "no em dashes" in call["system"], call["system"])
    ck("the model usage is recorded once, with who paid",
       len(used) == 1 and used[0][1]["input_tokens"] == 900 and used[0][1]["calls"] == 1
       and used[0][2:4] == ("console", "anthropic"), used)
    # the summary reaches the model with the cell's agent tracing gone (TR-1810)
    import importlib.util
    ck("the model is called with no tracing module installed",
       importlib.util.find_spec("tares.tracing") is None and len(fake.calls) == 1,
       str(importlib.util.find_spec("tares.tracing")))

    fake.reset()
    fake.text = "2 tickets delivered and 17 bugs fixed."
    got = await ask()
    ck("an invented number is rejected and the template used",
       got == (TEMPLATE_TEXT, "template:rejected"), got)

    fake.reset()
    fake.text = "\n".join(["Done."] * 9)
    ck("more lines than a summary has: rejected", (await ask())[1] == "template:rejected")
    fake.reset()
    fake.text = "done " * 300
    ck("too long a text: rejected", (await ask())[1] == "template:rejected")

    for label, change in (("an error", {"error": RuntimeError("boom")}),
                          ("an empty answer", {"text": "  \n "}),
                          ("an answer cut off", {"stop": "max_tokens"})):
        fake.reset()
        for k, v in change.items():
            setattr(fake, k, v)
        got = await ask()
        ck(f"{label}: the template", got == (TEMPLATE_TEXT, "template"), got)
    fake.reset()
    fake.gate = asyncio.Event()
    got = await ask(timeout=0.05)
    ck("no answer in time: the template", got == (TEMPLATE_TEXT, "template"), got)

    fake.reset()

    def broken(*a):
        raise RuntimeError("ledger down")
    got = await report_words.figures_text("milestone", FACTS, store=store, record_usage=broken)
    ck("a usage count that fails does not lose the text", got == ("The slides are done.", "model"), got)

    print("== the template's sentences ==")
    template = report_words.TEMPLATES["milestone"]
    empty = {"delivered": {"count": 0, "tickets": []}, "decisions": {"count": 0, "items": []},
             "left": {"count": 0, "milestone_count": 0, "later_milestones": []}}
    ck("nothing at all", template(empty) == (
        "Delivered: no tickets.\nNo decisions were recorded on these tickets.\n"
        "Nothing is left in later milestones."), template(empty))
    big = {
        "delivered": {"count": 1200, "tickets": [{"title": t} for t in ("A.", "B", "C", "D")]},
        "decisions": {"count": 5, "items": [{"question": "One", "answer": "yes"},
                                            {"question": "Two?", "answer": ""},
                                            {"question": "Three?", "answer": "no"}]},
        "left": {"count": 12, "milestone_count": 5, "later_milestones": [
            {"name": f"M{i}: part", "open_count": i} for i in range(2, 7)]},
    }
    ck("large: the counts are whole, the names are the first few", template(big) == (
        "Delivered: 1,200 tickets. Among them: A; B; C.\n"
        "Decided on the way: 5. Among them: One: yes. Two?\n"
        "Left: 12 open tickets in 5 later milestones. The next ones: M2: part, 2; M3: part, 3; "
        "M4: part, 4."), template(big))


# ── the summary through the API ──────────────────────────────────────────────
async def through_the_api(app, store, fake):
    report_words.resolve_provider = lambda s: (fake, "console")
    root = {"Authorization": f"Bearer {TOKEN}"}
    crew = {**root, "X-Tares-Station": "demo-build-m1", "X-Tares-Role": "builder"}

    def project(name):
        uid = "uc_" + uuid.uuid4().hex[:10]
        store.create_project(uid, "custom", name, {}, goal=f"build {name}")
        store.set_project_kind(uid, "software_factory")
        return uid

    def ticket(uid, mid, title, state="merged"):
        t = store.create_ticket(uid, "tares", title, milestone=mid)
        if state == "merged":
            store.update_ticket(t, stage="merged", status="done")
        elif state == "canceled":
            store.update_ticket(t, status="canceled")
        elif state == "merged, then canceled":
            store.update_ticket(t, stage="merged", status="canceled")
        return factory_mod.label(store.get_ticket(uid, t))

    def accepted_by_itself(uid, name):
        """A milestone with no checks and every ticket merged: accepted, with no acceptance."""
        mid = store.create_milestone(uid, name, "", [])
        ticket(uid, mid, f"{name} work")
        return mid

    def for_a_person(uid, name):
        """A merged milestone that waits for a person to accept it."""
        mid = store.create_milestone(uid, name, "", factory_mod.checks_ok(
            [{"check": "open the page", "expect": "it shows", "for": "person"}]))
        ticket(uid, mid, f"{name} work")
        return mid

    def decide(uid, question, tickets, words, choice="", command=None):
        at = now_utc() - timedelta(minutes=1)
        d = store.add_desk_item(uid, question, "", [], "yes", "because", "", tickets, False,
                                "orchestrator", [], at, command=command,
                                station="demo-build-m1" if command else None)
        store.add_decision(uid, d["id"], question, words, choice, False, "", True, None, False,
                           tickets, now_utc())

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cx:
        def url(uid, mid, more=""):
            return f"/api/projects/{uid}/milestones/{mid}/summary{more}"

        async def read(uid, mid, headers=root):
            return await cx.get(url(uid, mid), headers=headers)

        async def written(uid, mid):
            """The summary once nothing is writing it."""
            for _ in range(200):
                body = (await read(uid, mid)).json()
                if not body["writing"]:
                    return body
                await asyncio.sleep(0.02)
            raise AssertionError("still writing")

        async def until(fn):
            for _ in range(200):
                if fn():
                    return True
                await asyncio.sleep(0.02)
            return False

        def accept(uid, mid):
            return cx.post(f"/api/projects/{uid}/milestones/{mid}/accept", json={}, headers=root)

        def kept(mid):
            return store.get_report_text("milestone", mid)

        print("== what the summary is written from ==")
        a = project("Slides")
        m1 = store.create_milestone(a, "M1: The slides", "A deck people can open", factory_mod.checks_ok(
            [{"check": "open the deck", "expect": "it opens", "for": "person"}]))
        t1 = ticket(a, m1, "Draw the first slide")
        t2 = ticket(a, m1, "Add the notes")
        ticket(a, m1, "A dropped idea", "canceled")
        ticket(a, m1, "A reverted idea", "merged, then canceled")
        m2 = store.create_milestone(a, "M2: The search page", "", [])
        t4 = ticket(a, m2, "Search the slides", "open")
        ticket(a, m2, "Rank the results of /srv/crew/deck", "open")
        ticket(a, m2, "Index the slides")
        ticket(a, m2, "A dropped search", "canceled")
        m3 = store.create_milestone(a, "M3: All done", "", [])
        ticket(a, m3, "Tidy up")
        decide(a, "Which export format?", [t1], "A", "PDF only")
        decide(a, "May the station run npm install?", [t2], "yes", command="npm install")
        decide(a, f"Merge PR #12 (notes) at 1a2b3c4?", [t2], "yes")
        decide(a, "Rank by date?", [t4], "no")
        store.set_handover(a, [{"path": "/srv/crew/deck", "name": "deck"}], None, None, now_utc())
        facts = milestone_summary.figures(store, a, m1)
        ck("delivered: the merged tickets, not the canceled one",
           facts["delivered"] == {"count": 2, "tickets": [
               {"ref": t1, "title": "Draw the first slide"}, {"ref": t2, "title": "Add the notes"}]},
           facts["delivered"])
        ck("decisions: the ones on these tickets, without a command allowed or a merge",
           [(d["question"], d["answer"]) for d in facts["decisions"]["items"]]
           == [("Which export format?", "PDF only")] and facts["decisions"]["count"] == 1,
           facts["decisions"])
        ck("left: the unfinished tickets of later milestones only",
           facts["left"]["count"] == 2 and facts["left"]["milestone_count"] == 1
           and facts["left"]["later_milestones"][0]["name"] == "M2: The search page"
           and facts["left"]["later_milestones"][0]["open_count"] == 2, facts["left"])
        later = facts["left"]["later_milestones"]
        ck("a crew folder in a title is shown as the repo's name",
           "Rank the results of deck" in [t["title"] for m in later for t in m["tickets"]]
           and "/srv/" not in str(facts), later)
        last = milestone_summary.figures(store, a, m3)["left"]
        ck("after the last milestone nothing is left, whatever is open before it",
           last == {"count": 0, "milestone_count": 0, "later_milestones": []}, last)
        ck("the milestone is named as the page names it", facts["milestone"] == {
            "name": "M1: The slides", "goal": "A deck people can open"}, facts["milestone"])
        long = project("Long")
        lm = store.create_milestone(long, "Big", "", [])
        for i in range(milestone_summary.MAX_DELIVERED + 2):
            ticket(long, lm, "x" * 400 if i == 0 else f"work {i}")
        facts = milestone_summary.figures(store, long, lm)
        ck("a large milestone is counted whole and listed in part",
           facts["delivered"]["count"] == milestone_summary.MAX_DELIVERED + 2
           and len(facts["delivered"]["tickets"]) == milestone_summary.MAX_DELIVERED, facts["delivered"]["count"])
        ck("a long title is cut", len(facts["delivered"]["tickets"][0]["title"])
           == milestone_summary.MAX_TITLE, facts["delivered"]["tickets"][0])

        print("== a milestone not accepted ==")
        fake.reset()
        r = await read(a, m1)
        ck("it has no summary, and the read asks no model", r.status_code == 200 and r.json() == {
            "milestone": m1, "summary": None, "writing": False} and not fake.calls, r.text)
        r = await cx.post(url(a, m1, "/rewrite"), headers=root)
        ck("and cannot be written again", r.status_code == 409 and "not accepted" in r.text
           and not fake.calls, r.text)

        print("== accepting it queues the summary ==")
        fake.gate = asyncio.Event()
        fake.text = "2 tickets delivered.\nExport is PDF only.\n2 tickets left in M2."
        r = await asyncio.wait_for(accept(a, m1), 5)
        ck("the acceptance answers while the model is still writing",
           r.status_code == 200 and r.json()["accepted"] and kept(m1) is None, r.text)
        ck("the model is asked without any read", await until(lambda: len(fake.calls) == 1), fake.calls)
        body = (await read(a, m1)).json()
        ck("a read meanwhile says it is being written", body["writing"] and body["summary"] is None, body)
        fake.gate.set()
        body = await written(a, m1)
        got = body["summary"] or {}
        ck("then it shows the text, where it came from, the figures and the time",
           got.get("text") == fake.text and got.get("source") == "model" and got.get("written_at")
           and got["figures"]["delivered"]["count"] == 2, body)
        ck("the usage is in the cell's ledger",
           store.model_usage_summary()["total"]["calls"] == 1, store.model_usage_summary())
        await read(a, m1)
        await read(a, m1)
        ck("a second read makes no model call", len(fake.calls) == 1, len(fake.calls))
        r = await read(a, "M1: The slides")
        ck("the milestone can be named instead of its id",
           r.status_code == 200 and r.json()["milestone"] == m1, r.text)

        print("== written once ==")
        first = kept(m1)
        store.put_report_text("milestone", m1, "other", "model", {}, now_utc())
        ck("a second write leaves the kept text", kept(m1)["text"] == first["text"], kept(m1))
        ticket(a, m2, "A new search idea", "open")
        await read(a, m1)
        ck("new figures do not change it by themselves", kept(m1)["figures"]["left"]["count"] == 2
           and len(fake.calls) == 1, kept(m1)["figures"]["left"])

        print("== write again ==")
        fake.gate = asyncio.Event()
        fake.text = "3 tickets left in M2."
        r = await cx.post(url(a, m1, "/rewrite"), headers=root)
        ck("it answers at once with the kept text", r.status_code == 202 and r.json()["writing"]
           and r.json()["summary"]["text"] == first["text"], r.text)
        r = await cx.post(url(a, m1, "/rewrite"), headers=root)
        ck("a second press while writing is accepted", r.status_code == 202 and r.json()["writing"], r.text)
        await until(lambda: len(fake.calls) >= 2)
        await asyncio.sleep(0.1)
        ck("and starts no second write", len(fake.calls) == 2, len(fake.calls))
        fake.gate.set()
        body = await written(a, m1)
        ck("the new text replaces the old, from today's figures",
           body["summary"]["text"] == "3 tickets left in M2."
           and body["summary"]["figures"]["left"]["count"] == 3
           and kept(m1)["written_at"] > first["written_at"], body)

        print("== a milestone accepted before this shipped ==")
        b = project("Search")
        old = accepted_by_itself(b, "Old")
        fake.reset()
        fake.gate = asyncio.Event()
        answers = await asyncio.gather(*[read(b, old) for _ in range(8)])
        ck("the first reads say it is being written",
           all(r.status_code == 200 and r.json()["writing"] for r in answers),
           [r.text for r in answers][:2])
        await until(lambda: bool(fake.calls))
        await asyncio.sleep(0.1)
        ck("eight first reads at once make one call", len(fake.calls) == 1, len(fake.calls))
        fake.gate.set()
        body = await written(b, old)
        ck("and all get the one text", body["summary"]["text"] == fake.text and len(fake.calls) == 1, body)
        again = milestone_summary.Summaries(store)
        again.queue(b, old)
        await until(lambda: not again.writing(old))
        ck("queueing one that is already written asks no model", len(fake.calls) == 1, len(fake.calls))

        print("== many at once ==")
        many = [accepted_by_itself(b, f"Part {i}") for i in range(5)]
        fake.reset()
        fake.gate = asyncio.Event()
        for mid in many:
            await read(b, mid)
        await until(lambda: len(fake.calls) >= milestone_summary.MAX_AT_ONCE)
        await asyncio.sleep(0.2)
        ck("only so many are written at once", len(fake.calls) == milestone_summary.MAX_AT_ONCE,
           len(fake.calls))
        fake.gate.set()
        for mid in many:
            await written(b, mid)
        ck("and every one is written in the end", len(fake.calls) == 5
           and fake.most == milestone_summary.MAX_AT_ONCE and all(kept(m) for m in many),
           (len(fake.calls), fake.most))

        print("== failures ==")
        c = project("Failing")
        broken_model = for_a_person(c, "Model fails")
        fake.reset()
        fake.error = RuntimeError("overloaded")
        r = await accept(c, broken_model)
        body = await written(c, broken_model)
        ck("a model that fails does not fail the acceptance, and the template is kept",
           r.status_code == 200 and r.json()["accepted"] and body["summary"]["source"] == "template"
           and body["summary"]["text"].startswith("Delivered: 1 ticket."), (r.text, body))

        no_loop = for_a_person(c, "Cannot start")
        spawn = milestone_summary.loop_mod.spawn

        def refuse(coro):
            coro.close()
            raise RuntimeError("no event loop")
        milestone_summary.loop_mod.spawn = refuse
        try:
            queued = milestone_summary.Summaries(store).queue(c, no_loop)
            milestone_summary.loop_mod.spawn = lambda coro: (
                refuse(coro) if coro.__name__ == "_write" else spawn(coro))
            r = await accept(c, no_loop)
        finally:
            milestone_summary.loop_mod.spawn = spawn
        ck("a write that cannot start does not fail the acceptance",
           queued is False and r.status_code == 200 and r.json()["accepted"], r.text)
        await read(c, no_loop)
        ck("and a later read writes it", (await written(c, no_loop))["summary"] is not None)

        broken_store = for_a_person(c, "Store fails")
        put = store.put_report_text

        def full(*a, **kw):
            raise OSError("disk full")
        fake.reset()
        store.put_report_text = full
        retry = milestone_summary.RETRY_S
        try:
            r = await accept(c, broken_store)
            body = await written(c, broken_store)
            ck("a store that fails does not fail the acceptance",
               r.status_code == 200 and body["summary"] is None and len(fake.calls) == 1, (r.text, body))
            await read(c, broken_store)
            await asyncio.sleep(0.1)
            ck("reads do not start a failed write over and over", len(fake.calls) == 1, len(fake.calls))
            store.put_report_text = put
            milestone_summary.RETRY_S = 0
            await read(c, broken_store)
            body = await written(c, broken_store)
            ck("after the wait a read tries again", body["summary"] is not None and len(fake.calls) == 2,
               (body, len(fake.calls)))
        finally:
            store.put_report_text = put
            milestone_summary.RETRY_S = retry

        print("== deleted while writing ==")
        gone = accepted_by_itself(c, "Going")
        fake.reset()
        fake.gate = asyncio.Event()
        await read(c, gone)
        await until(lambda: bool(fake.calls))
        store.delete_milestone(gone)
        fake.gate.set()
        await asyncio.sleep(0.3)
        ck("no text is kept for a milestone that was deleted", kept(gone) is None, kept(gone))
        r = await read(c, gone)
        ck("and it is not found", r.status_code == 404, r.text)

        print("== who may ==")
        fake.reset()
        before = kept(m1)
        r = await cx.get(url(a, m1))
        ck("no token: refused", r.status_code == 401, r.text)
        r = await read(a, m1, crew)
        ck("a station may read it", r.status_code == 200 and r.json()["summary"], r.text)
        r = await cx.post(url(a, m1, "/rewrite"), headers=crew)
        ck("a station may not ask for it again", r.status_code == 403 and "station" in r.text, r.text)

        async def key(uid, scopes):
            r = await cx.post("/api/keys", json={"name": uuid.uuid4().hex[:8], "project": uid,
                                                 "scopes": scopes}, headers=root)
            return {"Authorization": f"Bearer {r.json()['secret']}"}
        mine, theirs = await key(a, ["read"]), await key(b, ["read"])
        r = await read(a, m1, mine)
        ck("a read key of the project may read it", r.status_code == 200 and r.json()["summary"], r.text)
        r = await cx.post(url(a, m1, "/rewrite"), headers=mine)
        ck("but not write it again", r.status_code == 403, r.text)
        r = await read(a, m1, theirs)
        ck("another project's key may not read it", r.status_code == 403 and "slides" not in r.text.lower(),
           r.text)
        reader = await cx.post("/api/keys", json={"name": "reader", "scopes": ["read"]}, headers=root)
        r = await cx.post(url(a, m1, "/rewrite"),
                          headers={"Authorization": f"Bearer {reader.json()['secret']}"})
        ck("a read key of the cell may not write either", r.status_code == 403, r.text)
        r = await read(b, m1)
        ck("a milestone is only found in its own project", r.status_code == 404, r.text)
        await asyncio.sleep(0.1)
        ck("none of that changed the text or asked the model", kept(m1) == before and not fake.calls,
           len(fake.calls))

        store.set_project_scope(b, "private")
        theirs_by_cell = await cx.get(url(b, old), headers={
            "Authorization": f"Bearer {reader.json()['secret']}"})
        ck("a private project's summary is unknown to who is not on it",
           theirs_by_cell.status_code == 404 and "unknown project" in theirs_by_cell.text,
           theirs_by_cell.text)
        ck("and its own key still reads it", (await read(b, old, theirs)).status_code == 200)

        print("== deleting a project ==")
        ck("the project's texts are there", kept(m1) is not None and kept(old) is not None)
        store.delete_project(a)
        ck("go with it, and only its own", kept(m1) is None and kept(old) is not None)


# ── the revision ─────────────────────────────────────────────────────────────
def revision():
    print("== revision 0009_report_texts ==")
    d = tempfile.mkdtemp()
    schema = dsn = None
    if PG:
        import psycopg
        schema = "rt_" + uuid.uuid4().hex[:12]
        dsn = f"{PG}?options=-csearch_path%3D{schema}"
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute(f"CREATE SCHEMA {schema}")

    def step_down(sdb, to):
        with sdb.begin() as c:
            cfg = sdb._config()
            cfg.attributes["connection"] = c
            command.downgrade(cfg, to)

    def version(sdb):
        with sdb.begin() as c:
            return [x[0] for x in c.execute(text("SELECT version_num FROM session_store_version")).all()]

    def tables(sdb):
        return inspect(sdb.engine).get_table_names()

    try:
        sdb = SessionStore(d, dsn)
        sdb.upgrade()
        insp = inspect(sdb.engine)
        ck("the table exists", "report_texts" in insp.get_table_names())
        ck("its columns", sorted(c["name"] for c in insp.get_columns("report_texts"))
           == sorted(["kind", "key", "text", "source", "figures", "written_at"]))
        ck("its key is (kind, key)", insp.get_pk_constraint("report_texts")["constrained_columns"]
           == ["kind", "key"])
        ck("the store is at the head", version(sdb) == [HEAD], version(sdb))
        step_down(sdb, "0008_report_weeks")
        ck("a downgrade removes the table and keeps the others",
           "report_texts" not in tables(sdb) and "report_weeks" in tables(sdb)
           and version(sdb) == ["0008_report_weeks"], (tables(sdb), version(sdb)))
        sdb.upgrade()
        ck("an upgrade after it builds the table again",
           "report_texts" in tables(sdb) and version(sdb) == [HEAD], version(sdb))
        step_down(sdb, "0008_report_weeks")
        with sdb.begin() as c:
            metadata.tables["report_texts"].create(c)
            c.execute(text("INSERT INTO report_texts (kind, key, text) VALUES ('week', 'w', 'kept')"))
        sdb.upgrade()
        with sdb.begin() as c:
            row = c.execute(text("SELECT text FROM report_texts WHERE key = 'w'")).scalar()
        ck("an upgrade over a table that already exists stamps the revision and keeps the rows",
           version(sdb) == [HEAD] and row == "kept", (version(sdb), row))
        sdb.close()
    finally:
        if PG:
            import psycopg
            with psycopg.connect(PG, autocommit=True) as c:
                c.execute(f"DROP SCHEMA {schema} CASCADE")


async def main():
    number_check()
    app = make_app()
    store = app.state.store
    fake = Fake()
    async with app.router.lifespan_context(app):
        await writing_the_text(store, fake)
        await through_the_api(app, store, fake)
    revision()


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
