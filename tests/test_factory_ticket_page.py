"""The factory ticket page (TR-760): GET /api/factory/projects/{uid}/tickets/{ref}.

One read answers the whole page about one ticket: its state, milestone, who works on it, what it
depends on and what waits for it, its pull request, every review round with its findings, the
checks, the calls and decisions, its history and its working notes. Nothing in the answer may name
a station, a session or a folder of the crew's machine (the terminal command that opens the
builder's session is the one place a station name appears), and every shown word passes the
edition words list.

Run: .venv/bin/python tests/test_factory_ticket_page.py
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-factory-ticket-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-ticket"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"

import httpx

import tares
from tares import factory as factory_mod
from tares import words
from tares.store import now_utc

P = F = 0
SKIP = ("title", "notes", "text", "command", "question", "words", "name")


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def strings(v, path="$", key=""):
    """Every (path, key, string) in a JSON value, `key` being the name of the field holding it."""
    if isinstance(v, str):
        yield path, key, v
    elif isinstance(v, dict):
        for k, x in v.items():
            yield from strings(x, f"{path}.{k}", k)
    elif isinstance(v, list):
        for i, x in enumerate(v):
            yield from strings(x, f"{path}[{i}]", key)


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    cx_ = lambda token=TOKEN: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t",
        headers={"Authorization": f"Bearer {token}"} if token else {})

    def ago(**kw):
        return now_utc() - timedelta(**kw)

    def project(name, factory=True):
        uid = f"uc_{name.replace('-', '')}"
        store.create_project(uid, "custom", name, {}, goal="A project for the test")
        if factory:
            store.set_project_kind(uid, "software_factory")
        return uid

    def ticket(uid, title, **fields):
        owner = fields.pop("owner", "tares")
        ms, deps = fields.pop("milestone", None), fields.pop("depends_on", None)
        tid = store.create_ticket(uid, owner, title, milestone=ms, depends_on=deps)
        if fields:
            store.update_ticket(tid, **fields)
        return tid

    PATH = "/Users/alex/code/project-x"
    REPO = "example-org/docs-site"
    BUILDER, LEAD, REVIEWER = "project-x-build-m1", "crew-orchestrator", "crew-reviewer"
    SESSIONS = ["sess-b1", "sess-lead", "sess-rev"]

    async with app.router.lifespan_context(app):
        px = project("project-x")
        store.set_handover(px, [{"path": PATH, "name": REPO}], None, None, now_utc())
        store.set_session_station("sess-b1", BUILDER, "builder", LEAD, px, now_utc())
        store.set_session_station("sess-lead", LEAD, "orchestrator", "", None, now_utc())
        store.set_session_station("sess-rev", REVIEWER, "reviewer", LEAD, None, now_utc())
        m1 = store.create_milestone(px, "M1: pages", "The pages are live.", [])
        pr = lambda n, **kw: {"url": f"https://github.com/{REPO}/pull/{n}", "repo": REPO, "number": n,
                              "head": "aaaaaaa1111", **kw}
        doc = store.create_doc(px, "working", "Working notes: T2",
                               f"## Progress\n\nThe footer is built in {PATH}/src.\n", by="Alex")
        t1 = ticket(px, "Add the header", milestone=m1, stage="merged", status="done", holder=BUILDER,
                    pr=pr(11, state="closed", merged=True))
        t2 = ticket(px, "Add the footer", milestone=m1, stage="changes", status="in_progress",
                    holder=BUILDER, pr=pr(12, state="open", ci="failure", head="ccccccc3333"), depends_on=[t1],
                    working_doc=doc)
        t3 = ticket(px, "Add the sitemap", milestone=m1, stage="blocked", status="in_progress",
                    holder=LEAD, depends_on=[t2])
        t4 = ticket(px, "Tidy the readme")
        t5 = ticket(px, "Add the search box", owner="linear", milestone=m1)
        t6 = ticket(px, "Add the 404 page", stage="doing", status="in_progress", depends_on=[t4])
        store.update_ticket(t5, identifier="PX-5", url="https://linear.app/example/issue/PX-5")

        # t2: three review rounds, a finding fixed in a later round, one still open
        r1 = store.add_review(px, t2, None, "aaaaaaa1111", "changes", "ran the tests",
                              f"did not open {PATH}/build in a browser", REVIEWER,
                              [{"severity": "high", "file": f"{PATH}/src/footer.ts", "line": 12,
                                "blocking": True, "text": "The footer drops the last link"},
                               {"severity": "low", "file": "src/footer.css", "line": 0,
                                "blocking": False, "text": "A colour could be a token"}],
                              {}, factory_mod.finding_kind, ago(days=3))
        r2 = store.add_review(px, t2, None, "bbbbbbb2222", "changes", "ran the tests again", None,
                              REVIEWER, [{"severity": "high", "file": "src/footer.ts", "line": 40,
                                          "blocking": True, "text": "The link has no label"}],
                              {"F1": "fixed"}, factory_mod.finding_kind, ago(days=2))
        store.add_review(px, t2, None, "ccccccc3333", "changes", "ran the tests a third time", None,
                         REVIEWER, [], {"F3": "open"}, factory_mod.finding_kind, ago(days=1))
        store.add_review(px, t1, None, "aaaaaaa1111", "pass", "ran the tests", None, REVIEWER, [], {},
                         factory_mod.finding_kind, ago(days=5))
        store.add_review(px, t3, None, "aaaaaaa1111", "block", "read the diff", None, REVIEWER, [], {},
                         factory_mod.finding_kind, ago(days=1))

        store.add_check(px, t2, f"cd {PATH} && npm test", "12 passed, 0 failed", "ccccccc3333", "",
                        BUILDER, "sess-b1", ago(hours=4))
        store.add_check(px, t2, "python tests/test_footer.py", "3 passed, log in /Users/lee/tmp/out.log", "ccccccc3333", "",
                        BUILDER, "sess-b1", ago(hours=3))

        store.add_ticket_history(t2, px, "stage", "doing", None, BUILDER, "builder", ago(days=4))
        store.add_ticket_history(t2, px, "stage", "review", None, BUILDER, "builder", ago(days=3))
        store.add_ticket_history(t2, px, "stage", "changes", "asked for changes", REVIEWER, "reviewer",
                                 ago(days=2))
        store.add_ticket_history(t2, px, "holder", BUILDER, None, LEAD, "orchestrator", ago(days=5))
        store.add_ticket_history(t2, px, "holder", None, f"released from {PATH}/work", LEAD,
                                 "orchestrator", ago(hours=6))
        store.add_ticket_history(t2, px, "stage", "review", "new commits pushed after the verdict",
                                 "github", "github", ago(hours=5))
        store.add_ticket_history(t2, px, "title", "Add the footer", None, LEAD, "orchestrator", ago(days=6))

        a1 = store.add_assumption(px, "Which date format?", "ISO dates", f"it sorts, see {PATH}/dates", "",
                                  [t2], BUILDER, "builder", ago(days=2))
        a2 = store.add_assumption(px, "Which logger?", "the standard one", "no new package", "",
                                  [t2], BUILDER, "builder", ago(days=1))
        store.decide_assumption(a2["id"], "kept", None, None, now_utc())
        a3 = store.add_assumption(px, "Which font?", "the system one", "it is fast", "", [t2], BUILDER,
                                  "builder", ago(days=1))
        store.decide_assumption(a3["id"], "overturned", "Use the brand font", None, now_utc())
        store.add_assumption(px, "Which colour?", "blue", "it is calm", "", [t4], BUILDER, "builder",
                             ago(days=1))
        store.add_decision(px, "", "Tabs or spaces?", f"Tabs, as in {PATH}/style.md", "tabs", False,
                           "project", False, None, False, ["T2"], ago(days=3))
        store.add_decision(px, "", "Which licence?", "MIT", "MIT", False, "project", False, None, False,
                           [], ago(hours=2))

        pw = project("project-w", factory=False)
        ticket(pw, "Not a factory ticket")
        pd = project("project-d")
        ticket(pd, "In a draft")

        async with cx_() as cx:
            print("== the answer, a ticket with three review rounds ==")
            r = await cx.get(f"/api/factory/projects/{px}/tickets/T2")
            ck("the route answers", r.status_code == 200, r.text[:300])
            a = r.json()
            text = json.dumps(a)
            ck("the top level has exactly the fields of the page",
               set(a) == {"ref", "title", "state", "state_label", "milestone", "in", "url", "who",
                          "depends_on", "blocks", "pr", "reviews", "checks", "calls", "decisions",
                          "history", "notes", "terminal", "project", "now", "blocked_reason"}, sorted(a))
            ck("identity and state in words", a["ref"] == "T2" and a["title"] == "Add the footer"
               and a["state"] == "changes_asked" and a["state_label"] == "Changes asked",
               [a["ref"], a["state"], a["state_label"]])
            ck("the milestone", a["milestone"] == {"id": m1, "number": "M1", "name": "M1: pages"}, a["milestone"])
            ck("it lives in Tares, with no link", a["in"] == "Tares" and a["url"] is None, [a["in"], a["url"]])
            ck("who works on it is a role, not a station", a["who"] == "Builder for M1", a["who"])
            ck("depends on the header, in words and with a link",
               a["depends_on"] == [{"ref": "T1", "title": "Add the header", "state_label": "Merged",
                                    "href": f"/projects/{px}/tickets/T1"}], a["depends_on"])
            ck("blocks the sitemap, in words and with a link",
               a["blocks"] == [{"ref": "T3", "title": "Add the sitemap", "state_label": "Blocked",
                                "href": f"/projects/{px}/tickets/T3"}], a["blocks"])
            ck("the pull request", a["pr"] == {"number": 12, "url": f"https://github.com/{REPO}/pull/12",
                                                "state_text": "open, CI failing"}, a["pr"])
            ck("the project and a link back to it", a["project"] == {"id": px, "name": "project-x",
                                                                    "href": f"/projects/{px}"}, a["project"])

            print("== reviews and findings ==")
            rv = a["reviews"]
            ck("three rounds in order", [x["round"] for x in rv] == [1, 2, 3], [x["round"] for x in rv])
            ck("a verdict in words", {x["verdict_text"] for x in rv} == {"asked for changes"}, rv)
            ck("who reviewed and when", all(x["by"] == "the reviewer" for x in rv)
               and rv[0]["when"] == "3 days ago", [(x["by"], x["when"]) for x in rv])
            ck("what was checked and what was not, without the folder",
               rv[0]["verified"] == "ran the tests"
               and rv[0]["not_verified"] == f"did not open {REPO}/build in a browser"
               and rv[1]["not_verified"] is None, [rv[0]["not_verified"], rv[1]["not_verified"]])
            f1, f2 = rv[0]["findings"]
            ck("a finding says where it is, blocking or not, and what became of it",
               f1["text"] == "The footer drops the last link" and f1["file"] == f"{REPO}/src/footer.ts"
               and f1["line"] == 12 and f1["blocking"] is True and f1["state_text"] == "fixed in round 2"
               and f2["file"] == "src/footer.css" and f2["line"] is None and f2["blocking"] is False
               and f2["state_text"] == "open", [f1, f2])
            ck("a finding still open says open", rv[1]["findings"][0]["state_text"] == "open"
               and rv[2]["findings"] == [], rv[1:])
            ck("a merged ticket's pass and a blocked ticket's block read the same way",
               (await cx.get(f"/api/factory/projects/{px}/tickets/T1")).json()["reviews"][0]["verdict_text"] == "passed"
               and (await cx.get(f"/api/factory/projects/{px}/tickets/T3")).json()["reviews"][0]["verdict_text"]
               == "blocked")

            print("== checks, calls, decisions, history, notes ==")
            ck("the checks as the builder ran them, the folder swapped for the repo",
               [(c["command"], c["result"]) for c in a["checks"]]
               == [(f"cd {REPO} && npm test", "12 passed, 0 failed"),
                   ("python tests/test_footer.py", "3 passed, log in out.log")]
               and a["checks"][0]["when"] == "4 hours ago", a["checks"])
            calls = {c["ref"]: c for c in a["calls"]}
            ck("calls of this ticket only", set(calls) == {a1["label"], a2["label"], a3["label"]}, sorted(calls))
            ck("a call says who made it, and where it stands",
               calls[a1["label"]]["made"] == "by the builder, 2 days ago"
               and calls[a1["label"]]["state_text"] == "waiting for you"
               and calls[a2["label"]]["state_text"] == "confirmed"
               and calls[a3["label"]]["state_text"] == "you changed it"
               and calls[a3["label"]]["words"] == "Use the brand font"
               and calls[a1["label"]]["why"] == f"it sorts, see {REPO}/dates", calls)
            ck("the person's decisions that name the ticket, not the others",
               a["decisions"] == [{"question": "Tabs or spaces?", "words": f"Tabs, as in {REPO}/style.md",
                                   "when": "3 days ago"}], a["decisions"])
            hs = a["history"]
            ck("history newest first, in words", [x["what"] for x in hs] == [
                "Moved to in review", "Taken off whoever had it", "Changes asked",
                "Moved to in review", "Started", "Handed to the builder",
                "Changed the title"], [x["what"] for x in hs])
            ck("history says who did it", {x["what"]: x["who"] for x in hs}["Changes asked"] == "the reviewer"
               and {x["what"]: x["who"] for x in hs}["Handed to the builder"] == "the lead"
               and any(x["who"] == "GitHub" for x in hs), hs)
            ck("history keeps a reason and swaps the folder", any(x["text"] == f"released from {REPO}/work" for x in hs)
               and any(x["text"] == "new commits pushed after the verdict" for x in hs), hs)
            ck("the working notes, the folder swapped", a["notes"] == f"## Progress\n\nThe footer is built in {REPO}/src.\n",
               a["notes"])

            print("== the terminal command ==")
            store.set_session_state("sess-b1", "working", None, now_utc())
            a = (await cx.get(f"/api/factory/projects/{px}/tickets/T2")).json()
            ck("offered while a builder works on the ticket",
               a["terminal"] == [{"label": "Open the builder's session",
                                  "command": factory_mod.attach_command(BUILDER)}], a["terminal"])
            store.set_session_state("sess-b1", "ended", None, now_utc())
            a = (await cx.get(f"/api/factory/projects/{px}/tickets/T2")).json()
            ck("not offered once the builder's session ended", a["terminal"] == [], a["terminal"])
            store.set_session_state("sess-b1", "working", None, now_utc())
            t1_ = (await cx.get(f"/api/factory/projects/{px}/tickets/T1")).json()
            ck("not offered for a merged ticket, which nobody works on", t1_["terminal"] == [] and t1_["who"] == "Builder for M1"
               and t1_["state_label"] == "Merged" and t1_["pr"]["state_text"] == "merged", t1_)
            t3_ = (await cx.get(f"/api/factory/projects/{px}/tickets/T3")).json()
            ck("not offered when the lead holds the ticket", t3_["terminal"] == [] and t3_["who"] == "Lead"
               and t3_["state_label"] == "Blocked" and t3_["depends_on"][0]["ref"] == "T2"
               and t3_["blocked_reason"] == "Waiting for T2, Add the footer.", t3_)
            t6_ = (await cx.get(f"/api/factory/projects/{px}/tickets/T6")).json()
            ck("only a blocked ticket has a reason, even when it waits on another",
               a["blocked_reason"] is None and t1_["blocked_reason"] is None
               and t6_["state_label"] == "In progress" and t6_["depends_on"][0]["ref"] == "T4"
               and t6_["blocked_reason"] is None, t6_)

            print("== other tickets ==")
            t4_ = (await cx.get(f"/api/factory/projects/{px}/tickets/T4")).json()
            ck("a ticket nobody has: no who, no pr, empty lists, no milestone",
               t4_["who"] is None and t4_["pr"] is None and t4_["milestone"] is None
               and t4_["reviews"] == [] and t4_["checks"] == [] and t4_["decisions"] == []
               and t4_["history"] == [] and t4_["notes"] == "" and t4_["state_label"] == "To do"
               and [c["ref"] for c in t4_["calls"]] == ["A4"], t4_)
            t5_ = (await cx.get(f"/api/factory/projects/{px}/tickets/PX-5")).json()
            ck("a Linear ticket says so and links there, by its own reference", t5_["ref"] == "PX-5"
               and t5_["in"] == "Linear" and t5_["url"] == "https://linear.app/example/issue/PX-5", t5_)
            ck("a ticket opens by its id and by its number too",
               (await cx.get(f"/api/factory/projects/{px}/tickets/{t2}")).json()["ref"] == "T2"
               and (await cx.get(f"/api/factory/projects/{px}/tickets/2")).json()["ref"] == "T2")

            print("== words and paths ==")
            r = await cx.get(f"/api/factory/projects/{px}/tickets/T2")
            a, text = r.json(), r.text
            ck("the terminal command is there to be exempted", len(a["terminal"]) == 1, a["terminal"])
            bad = [(p, k, s) for p, k, s in strings(a)
                   if s[:1] in ("/", "~") and k not in ("url", "href")]
            ck("no value starts with / or ~ except a link", bad == [], bad)
            ck("no folder of the crew's machine", "/Users/" not in text and PATH not in text, text[:200])
            shown = {k: v for k, v in a.items() if k != "terminal"}
            hits = [n for n in [BUILDER, LEAD, REVIEWER] + SESSIONS if n in json.dumps(shown)]
            ck("no station name and no session id, outside the command that opens a session", hits == [], hits)
            raw = [(p, s) for p, k, s in strings(a) if k not in ("state", "id", "ref", "href", "url")
                   and re.search(r"\b(in_progress|changes_asked|ready_to_close|waiting_on_you|software_factory)\b", s)]
            ck("no raw state key in a shown string", raw == [], raw)
            found = words.offenders_in(a, SKIP)
            ck("the words list finds nothing", found == [], found)
            run = subprocess.run([sys.executable, "-m", "tares.words", "--skip", ",".join(SKIP)],
                                 input=r.text, capture_output=True, text=True,
                                 env={**os.environ, "PYTHONPATH": ROOT})
            ck("python -m tares.words says 0 problems", run.stdout.strip() == "0 problems" and run.returncode == 0,
               run.stdout + run.stderr)
            ck("no em dash", "—" not in text)

            print("== not found ==")
            for label, url in (("an unknown ticket", f"/api/factory/projects/{px}/tickets/T99"),
                               ("an unknown project", "/api/factory/projects/uc_nothing/tickets/T1"),
                               ("a project that is not a factory project", f"/api/factory/projects/{pw}/tickets/T1")):
                e = await cx.get(url)
                ck(f"{label} is 404", e.status_code == 404, e.status_code)
            store.update_project(pd, status="draft")
            store.set_project_kind(pd, "software_factory")
            ck("a draft project is 404 too", (await cx.get(f"/api/factory/projects/{pd}/tickets/T1")).status_code == 404)
            store.update_project(pd, status="active")
            ck("and the same project is answered once it is past its setup",
               (await cx.get(f"/api/factory/projects/{pd}/tickets/T1")).status_code == 200)
            ck("a ticket of another project is 404", (await cx.get(
                f"/api/factory/projects/{pd}/tickets/T2")).status_code == 404)
        async with cx_(None) as anon:
            e = await anon.get(f"/api/factory/projects/{px}/tickets/T2")
            ck("without a token it is refused", e.status_code in (401, 403), e.status_code)

        print("== a platform cell still serves the page ==")
        async with cx_() as cx:
            r = await cx.put("/api/settings/edition", json={"edition": "platform"})
            ck("the edition switches to platform", r.status_code == 200, r.text)
            r = await cx.get(f"/api/factory/projects/{px}/tickets/T2")
            ck("the route is served on a platform cell too", r.status_code == 200 and r.json()["ref"] == "T2",
               r.status_code)
            await cx.put("/api/settings/edition", json={"edition": "factory"})


print("== the code under test ==")
ck("the module comes from this checkout", os.path.abspath(tares.__file__).startswith(ROOT), tares.__file__)
asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
