"""The crew's read tools answer small by default: get_ticket cuts a long working doc to Goal,
Steps and Progress (section= reads the rest), keeps the latest messages and checks, and answers
"unchanged" to its own fingerprint; get_doc cuts a long doc to its headings (section= reads one); the size helpers stop a list at a limit and a byte cap and say
how to read the rest.

Drives the tool functions in-process against the daemon, as test_factory_mcp.py does.

Run: .venv/bin/python tests/test_tool_sizes.py
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="tares-tool-sizes-")
os.environ["TARES_HOME"] = os.path.join(_TMP, "api")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-tool-sizes"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

import tares.mcp_factory as mf
import tares.mcp_server as m

P = F = 0


def ck(label, cond, detail=None):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {str(detail)[:600]}"))


LONG_DOC = ("## Goal\n\nParse invoices.\n\n## Context and files\n\n" + "context line\n" * 1200
            + "\n## Steps\n\n1. read the PDF\n\n```bash\n# install deps\npip install pdfx\n```\n\n2. parse it\n\n## Progress\n\nstep 1 done\n\n### Checks\n\n"
            + "- pytest -q: 3 passed\n" * 50)


def helpers():
    print("== the helpers, on their own ==")
    rows = [{"n": i, "pad": "x" * 100} for i in range(50)]
    page, more = m._fit(rows, 20, 0, 100_000, "again(offset={offset})")
    ck("_fit stops at the limit and says how to go on",
       len(page) == 20 and "30 more of 50" in more and "again(offset=20)" in more, more)
    page, more = m._fit(rows, 50, 0, 2_000, "again(offset={offset})")
    ck("_fit stops at the byte cap too", len(json.dumps(page)) <= 2_000 and more, len(json.dumps(page)))
    page, more = m._fit(rows[:3], 20, 0, 100_000, "x")
    ck("_fit says nothing when all fit", len(page) == 3 and more == "", more)
    page, more = m._fit(rows, 20, 40, 100_000, "again(offset={offset})")
    ck("_fit reads on from an offset", [r["n"] for r in page] == list(range(40, 50)) and more == "", more)
    ck("_clip cuts a long text and leaves a short one", m._clip("x" * 50, 10) == "x" * 9 + "…"
       and m._clip("abc", 10) == "abc" and m._clip(None, 10) == "")
    secs = [h for h, _ in m._doc_sections(LONG_DOC)]
    ck("_doc_sections splits at # and ## headings", secs == ["Goal", "Context and files", "Steps", "Progress"],
       secs)
    ck("and keeps the text before the first heading",
       [h for h, _ in m._doc_sections("intro\n\n## Goal\n\ng")] == ["", "Goal"])
    ck("a # line inside a code fence is no heading",
       [h for h, _ in m._doc_sections("## Steps\n\n```\n# a\n## b\n```\n\nafter\n## Next\n")]
       == ["Steps", "Next"], [h for h, _ in m._doc_sections("## Steps\n\n```\n# a\n## b\n```\n\nafter\n## Next\n")])
    ck("_fingerprint is the same for the same content and not for another",
       m._fingerprint({"a": 1, "b": 2}) == m._fingerprint({"b": 2, "a": 1})
       and m._fingerprint({"a": 1}) != m._fingerprint({"a": 2}))


def url_patterns():
    print("== the link pattern ==")
    one = [{"url": f"https://l/acme/issue/T{i}", "label": f"T{i}"} for i in range(3)]
    ck("_url_pattern folds links that share a prefix", m._url_pattern(one) == "https://l/acme/issue/{label}")
    mixed = one + [{"url": "https://other/x/T9", "label": "T9"}]
    ck("_url_pattern gives up when the prefixes differ", m._url_pattern(mixed) == "")
    ck("_url_pattern gives up when a link is not prefix plus label",
       m._url_pattern(one + [{"url": "https://l/acme/issue/other", "label": "T9"}]) == "")
    ck("_url_pattern gives up with no links", m._url_pattern([]) == "" and m._url_pattern([{"label": "T1"}]) == "")


def gist_parts():
    print("== reviews and decisions in the gist ==")
    fake = {"label": "T9", "working_doc_body": "short",
            "reviews": [{"round": 1, "verdict": "changes", "head": "a" * 40, "findings": [
                {"label": "F1", "state": "open", "severity": "P2", "blocking": True, "file": "x.py",
                 "line": 7, "text": "w" * 500},
                {"label": "F2", "state": "fixed", "severity": "P3", "text": "old"}]}],
            "decisions": [{"label": f"D{i}", "question": "q" * 300, "choice": "c" * 300}
                          for i in range(1, 11)]}
    g = m._ticket_gist(fake)
    f1, f2 = g["reviews"][0]["findings"]
    ck("an open finding comes as its gist, clipped", isinstance(f1, dict) and f1["where"] == "x.py:7"
       and f1["blocking"] is True and len(f1["text"]) <= 200, f1)
    ck("a fixed finding comes as its label and state", f2 == "F2 fixed", f2)
    ck("the first 5 decisions come, clipped, with the count the ticket carries",
       [d["label"] for d in g["decisions"]] == ["D1", "D2", "D3", "D4", "D5"]
       and g["decisions_total"] == 10
       and all(len(d["question"]) <= 160 and len(d["choice"]) <= 160 for d in g["decisions"]),
       [d["label"] for d in g["decisions"]])


async def main():
    from tares.daemon import make_app
    app = make_app()
    root = {"Authorization": f"Bearer {TOKEN}"}
    m.TARESD = mf.TARESD = "http://t"
    m._cx = mf._cx = lambda timeout=10: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t", headers=root)
    async with app.router.lifespan_context(app):
        store = app.state.store
        await m.create_project("Shop", "A shop")
        t1 = json.loads(await m.write_ticket("Cart", project="Shop"))
        w = json.loads(await m.write_doc("working", "Working: cart", LONG_DOC, project="Shop"))
        await m.attach_working_doc(t1["id"], w["id"], project="Shop")
        small = json.loads(await m.write_ticket("Pay", project="Shop"))
        sw = json.loads(await m.write_doc("working", "Working: pay", "1. charge", project="Shop"))
        await m.attach_working_doc(small["id"], sw["id"], project="Shop")
        for i in range(20):
            await mf.add_check("T1", f"pytest tests/test_{i}.py " + "-q " * 40, "3 passed " * 30,
                               commit="a" * 40, project="Shop")
        msgs = [{"at": f"2026-11-17T10:{i:02d}:00Z", "session": None, "from": "crew-reviewer",
                 "to": "shop-build-m1", "type": "VERDICT",
                 "first_line": f"[TF:VERDICT] T1 Shop round {i} " + "w" * 300,
                 "text": f"[TF:VERDICT] T1 Shop round {i}"} for i in range(30)]
        store.protocol_messages = lambda source: msgs

        print("== get_ticket ==")
        raw = json.loads(await m.get_ticket("T1", project="Shop", detail="full"))
        ck("the doc is over 20 KB and 30 messages are there", len(raw["working_doc_body"]) > 20_000
           and len(raw["messages"]) == 30, (len(raw["working_doc_body"]), len(raw.get("messages") or [])))
        out = await m.get_ticket("T1", project="Shop")
        ck("by default it answers under 6 KB", len(out.encode()) < 6_000, len(out.encode()))
        g = json.loads(out)
        body = g["working_doc_body"]
        ck("a long working doc comes as Goal, Steps and Progress",
           "Parse invoices" in body and "1. read the PDF" in body and "step 1 done" in body
           and "context line" not in body and "pytest -q: 3 passed" not in body, body[:300])
        ck("and a # comment in a fenced block of Steps keeps the rest of Steps",
           "# install deps" in body and "pip install pdfx" in body and "2. parse it" in body, body[:400])
        ck("with every heading and its size",
           [(s["heading"], s["shown"]) for s in g["working_doc_sections"]]
           == [("Goal", True), ("Context and files", False), ("Steps", True), ("Progress", True)],
           g.get("working_doc_sections"))
        ck("the latest 8 messages come, with the count of them all",
           len(g["messages"]) == 8 and g["messages_total"] == 30
           and g["messages"][-1]["first_line"].startswith("[TF:VERDICT] T1 Shop round 29"),
           (len(g["messages"]), g.get("messages_total")))
        ck("and the latest 8 checks, with the count",
           len(g["checks"]) == 8 and g["checks_total"] == 20, (len(g["checks"]), g.get("checks_total")))
        ck("they are the latest ones, with a clipped command",
           g["checks"][-1]["command"].startswith("pytest tests/test_19.py")
           and g["checks"][0]["command"].startswith("pytest tests/test_12.py")
           and all(len(c["command"]) <= 120 and len(c["result"]) <= 100 for c in g["checks"]),
           [(c["command"][:30], len(c["command"])) for c in g["checks"]])
        ck("and the stage, holder and title are there", g["label"] == "T1" and "stage" in g
           and g["title"] == "Cart", sorted(g))
        sec = json.loads(await m.get_ticket("T1", project="Shop", section="steps"))
        ck("section=Steps reads all of Steps, fence and what follows", "# install deps" in sec["body"]
           and "2. parse it" in sec["body"], sec)
        sec = json.loads(await m.get_ticket("T1", project="Shop", section="Context and files"))
        ck("section= reads one section", sec["body"].count("context line") == 1200, sec)
        sec = json.loads(await m.get_ticket("T1", project="Shop", section="## steps"))
        ck("a section is found whatever its case or hashes", "1. read the PDF" in sec["body"], sec)
        whole = json.loads(await m.get_ticket("T1", project="Shop", section="all"))
        ck("section=all reads the whole doc", whole["working_doc_body"] == raw["working_doc_body"])
        bad = await m.get_ticket("T1", project="Shop", section="Nope")
        ck("an unknown section names the ones there are", "no section" in bad and "Steps" in bad, bad)
        again = json.loads(await m.get_ticket("T1", project="Shop", since=g["fingerprint"]))
        ck("since= on an unchanged ticket answers unchanged",
           again == {"unchanged": True, "fingerprint": g["fingerprint"]}, again)
        stale = json.loads(await m.get_ticket("T1", project="Shop", since="0123456789ab"))
        ck("a fingerprint it does not know gives the ticket", stale["label"] == "T1"
           and stale["fingerprint"] == g["fingerprint"], stale)
        st = json.loads(await m.get_ticket("T1", project="Shop", detail="status"))
        ck("detail=status is where it stands only", "working_doc_body" not in st and st["label"] == "T1"
           and "stage" in st and len(json.dumps(st)) < 1_500, st)
        sg = json.loads(await m.get_ticket("T1", project="Shop", detail="status", since=st["fingerprint"]))
        ck("and takes since= too", sg.get("unchanged") is True, sg)
        full = json.loads(await m.get_ticket("T1", project="Shop", detail="full"))
        ck("detail=full keeps the whole doc, every message and every check",
           full["working_doc_body"] == raw["working_doc_body"] and len(full["messages"]) == 30
           and len(full["checks"]) == 20 and "fingerprint" in full, sorted(full))
        short = json.loads(await m.get_ticket("T2", project="Shop"))
        ck("a short doc comes whole, with no section list",
           short["working_doc_body"] == "1. charge" and "working_doc_sections" not in short, short)
        orig = m._ticket_gist
        seen = {"at": "10:00"}
        m._ticket_gist = lambda t: {**orig(t), "pr": {"url": "u", "ci": seen.get("ci"), "checked_at": seen["at"]}}
        p1 = json.loads(await m.get_ticket("T1", project="Shop"))
        seen["at"] = "10:05"
        p2 = await m.get_ticket("T1", project="Shop", since=p1["fingerprint"])
        ck("GitHub being read again is no change", json.loads(p2).get("unchanged") is True, p2[:200])
        seen["ci"] = "failure"
        p3 = json.loads(await m.get_ticket("T1", project="Shop", since=p1["fingerprint"]))
        ck("but a new CI result is", p3.get("unchanged") is None and p3["fingerprint"] != p1["fingerprint"], p3)
        m._ticket_gist = orig
        await m.write_ticket("Cart and wishlist", id=t1["id"], project="Shop")
        moved = json.loads(await m.get_ticket("T1", project="Shop", since=g["fingerprint"]))
        ck("a changed ticket comes whole again", moved.get("title") == "Cart and wishlist"
           and moved["fingerprint"] != g["fingerprint"], moved)

        print("== the lists ==")
        for i in range(30):
            out = await m.write_doc("note", f"Note {i} " + "t" * 150, "n", project="Shop")
        ck("thirty notes are written", '"id"' in out, out)
        docs = json.loads(await m.list_docs(project="Shop", limit=10))
        ck("list_docs stops at its limit and says how to read on",
           len(docs) == 11 and "offset=10" in docs[-1]["more"], docs[-1])
        ck("and leaves the working docs and updated_at out",
           all(d.get("kind") != "working" and "updated_at" not in d for d in docs[:-1]), docs[:2])
        got, offset = [], 0
        while True:
            page = json.loads(await m.list_docs(kind="note", project="Shop", limit=12, offset=offset))
            got += [d for d in page if "more" not in d]
            if "more" not in page[-1]:
                break
            offset = len(got)
            ck("the docs note names the next offset", f"offset={offset}" in page[-1]["more"], page[-1])
        ck("reading on with offset gives every note once", len(got) == 30 and len({d["id"] for d in got}) == 30,
           len(got))
        every = json.loads(await m.list_docs(project="Shop", limit=100))
        ck("a long list has no working doc in it", "more" not in every[-1]
           and all(d["kind"] != "working" for d in every), [d["kind"] for d in every])
        wk = json.loads(await m.list_docs(kind="working", project="Shop"))
        ck("kind=working lists the working docs", len(wk) == 2, wk)

        await mf.write_milestone("Alpha", "x" * 300, [{"check": "pytest", "expect": "green"}], project="Shop")
        await mf.write_milestone("Beta", "second", [{"check": "curl /", "expect": "200"}], project="Shop")
        for i in range(98):
            await m.write_ticket(f"Ticket {i} " + "t" * 80, project="Shop", milestone="Alpha" if i < 5 else None)
        out = await m.list_tickets(project="Shop")
        tl = json.loads(out)
        ck("list_tickets of 100 tickets answers under 8,000 bytes", len(out.encode()) < 8_000, len(out.encode()))
        ck("and says how to read the rest", 0 < len(tl["tickets"]) <= 60 and "offset=" in tl["more"], tl.get("more"))
        ck("each ticket has its label, status and what blocks it",
           {"label", "status", "ready", "blocked_by", "stage", "repo"} <= set(tl["tickets"][0]), tl["tickets"][0])
        ck("and a project's tickets share one link pattern or each keeps its own",
           ("ticket_url" in tl) != ("url" in tl["tickets"][0]), sorted(tl["tickets"][0]))
        few = json.loads(await m.list_tickets(project="Shop", limit=7))
        ck("limit sets how many tickets come", len(few["tickets"]) == 7 and "offset=7" in few["more"],
           (len(few["tickets"]), few.get("more")))
        got, offset = [], 0
        while True:
            tl = json.loads(await m.list_tickets(project="Shop", open=True, offset=offset))
            got += tl["tickets"]
            if "more" not in tl:
                break
            offset = len(got)
            ck("the tickets note names the next offset", f"offset={offset}" in tl["more"], tl["more"])
        ck("reading on with offset gives every ticket once",
           len(got) == 100 and len({t["id"] for t in got}) == 100, len(got))
        full = json.loads(await m.list_tickets(project="Shop", detail="full"))
        ck("detail=full gives all 100 with every field, as before", len(full["tickets"]) == 100
           and "more" not in full and {"url", "working_doc", "working_doc_title", "assignee",
                                       "assignee_raw", "pr_note", "identifier"} <= set(full["tickets"][0]),
           sorted(full["tickets"][0]))
        al = json.loads(await m.list_tickets(project="Shop", milestone="Alpha", detail="full"))
        ck("milestone= narrows the list", len(al["tickets"]) == 5
           and {t["milestone_name"] for t in al["tickets"]} == {"Alpha"}, len(al["tickets"]))
        await m.write_ticket("", status="done", id="T3", project="Shop")
        op = json.loads(await m.list_tickets(project="Shop", open=True, limit=100, detail="full"))
        ck("open=True leaves the done ones out", len(op["tickets"]) == 99
           and all(t["status"] != "done" for t in op["tickets"]), len(op["tickets"]))

        print("== milestones and decisions ==")
        for t in al["tickets"]:
            await m.write_ticket("", status="done", id=t["id"], project="Shop")
        ms = json.loads(await mf.list_milestones(project="Shop"))
        alpha = next(x for x in ms if x["name"] == "Alpha")
        ck("a finished milestone comes short: goal cut, checks counted",
           alpha["finished"] is True and len(alpha["goal"]) <= 100 and alpha["checks"] == 1, alpha)
        beta = next(x for x in ms if x["name"] == "Beta")
        ck("an unfinished one keeps its goal and its checks", beta["checks"] == [{"check": "curl /", "expect": "200"}]
           and beta["goal"] == "second", beta)
        ck("a finished one still says which crew checks are to run and whether it is accepted",
           alpha["crew_to_run"] == [1] and alpha["accepted"] is False and "check_results" not in alpha
           and {"crew_failed", "person_wrong", "needs_person"} <= set(alpha), alpha)
        ck("an unfinished one has each check's result and what is to run",
           beta["check_results"][0]["n"] == 1 and beta["crew_to_run"] == [1], beta)
        msf = json.loads(await mf.list_milestones(project="Shop", detail="full"))
        alpha = next(x for x in msf if x["name"] == "Alpha")
        ck("detail=full gives the finished one whole", alpha["goal"] == "x" * 300
           and alpha["checks"] == [{"check": "pytest", "expect": "green"}]
           and alpha["check_results"][0]["result"] is None and alpha["crew_to_run"] == [1], alpha)

        rows = [{"label": f"D{i}", "question": "q" * 400, "words": "go with the plan", "choice": "go with the plan",
                 "standing": False, "matched": True, "via": "console", "tickets": [], "at": "2026-11-17T10:00:00Z",
                 "person": {"id": "p1", "name": "Kiran", "email": "k@x.io"}} for i in range(1, 41)]
        rows[0]["choice"] = "B"
        rows[2]["choice"] = None

        class _Decisions:
            """The daemon's client, but /decisions answers the rows above."""
            def __init__(self, real):
                self.real = real

            async def __aenter__(self):
                await self.real.__aenter__()
                return self

            async def __aexit__(self, *a):
                return await self.real.__aexit__(*a)

            async def get(self, url, **kw):
                if url.endswith("/decisions"):
                    return httpx.Response(200, json={"decisions": rows}, request=httpx.Request("GET", url))
                return await self.real.get(url, **kw)

        def fake(timeout=10, _real=mf._cx):
            return _Decisions(_real(timeout))

        keep = mf._cx
        mf._cx = fake
        try:
            out = await mf.list_decisions(project="Shop")
            dl = json.loads(out)
            ck("list_decisions answers its newest 15 under 8,000 bytes, with a note",
               len(out.encode()) < 8_000 and len(dl) == 16 and "offset=15" in dl[-1]["more"], (len(out), dl[-1]))
            ck("each question is cut, the person is its name only, an echoing choice is left out",
               all(len(d["question"]) <= 200 for d in dl[:-1]) and dl[0]["person"] == {"name": "Kiran"}
               and dl[0]["answer"] == "B" and "choice" not in dl[0] and "choice" not in dl[1], dl[:2])
            ck("a decision with no choice keeps its words as the answer",
               dl[2]["choice"] is None and dl[2]["answer"] == "go with the plan", dl[2])
            more = json.loads(await mf.list_decisions(project="Shop", limit=40, offset=15))
            ck("offset reads on", more[0]["label"] == "D16", more[0])
            fd = json.loads(await mf.list_decisions(project="Shop", detail="full"))
            ck("detail=full gives every decision whole, as before", len(fd) == 40
               and len(fd[0]["question"]) == 400 and fd[0]["person"] == {"id": "p1", "name": "Kiran", "email": "k@x.io"}
               and fd[1]["choice"] == "go with the plan" and "more" not in json.dumps(fd), fd[0])
        finally:
            mf._cx = keep


        print("== my_work and review_queue ==")
        await mf.hand_over(project="Shop", repos=[{"path": "/w/shop", "name": "acme/shop"}])
        first = json.loads(await mf.my_work())
        ck("my_work lists a ready ticket and carries a fingerprint",
           any(r["project"] == "Shop" for r in first["ready"]) and first["fingerprint"].startswith("w1."),
           sorted(first))
        async with m._cx(10) as cx:
            rest = (await cx.get("/api/crew/my-work")).json()
        ck("the answer keeps every key the REST answer has, and REST has no fingerprint",
           set(rest) <= set(first) and "fingerprint" not in rest and set(first) - set(rest) == {"fingerprint"},
           (sorted(rest), sorted(first)))
        same_raw = await mf.my_work(since=first["fingerprint"])
        ck("my_work(since=<same fingerprint>) answers under 300 bytes",
           len(same_raw.encode()) < 300 and json.loads(same_raw) == {"unchanged": True}, same_raw)
        nw = json.loads(await m.write_ticket("Gift cards", project="Shop"))
        moved = json.loads(await mf.my_work(since=first["fingerprint"]))
        ck("a new ready ticket shows in the next since= answer, the other parts only named",
           any(r["ticket"] == nw["label"] for r in moved["ready"])
           and moved["fingerprint"] != first["fingerprint"] and "ready" not in moved["same"]
           and "desk" in moved["same"] and "desk" not in moved, moved)
        await mf.assign_ticket(nw["label"], "shop-build-m1", project="Shop")
        held = json.loads(await mf.my_work(since=moved["fingerprint"]))
        ck("a ticket a station now holds shows in the next since= answer's in_progress",
           any(r["ticket"] == nw["label"] for r in held["in_progress"]) and "in_progress" not in held["same"]
           and "ready" not in held["same"], held)
        await mf.desk_add("Ship the cart first?", "yes", "it is small", options=["yes", "no"], project="Shop")
        d1 = json.loads(await mf.my_work(since=moved["fingerprint"]))
        ck("a new desk item comes whole", [d["label"] for d in d1["desk"]] == ["D1"]
           and d1["desk"][0]["question"] == "Ship the cart first?" and "seen" not in d1["desk"][0]
           and d1["desk_closed"] == [], d1)
        await mf.desk_add("Pay by card?", "yes", "everyone does", options=["yes", "no"], project="Shop")
        d2 = json.loads(await mf.my_work(since=d1["fingerprint"]))
        ck("a desk item already seen comes as its label and question, a new one whole",
           d2["desk"][0] == {"label": "D1", "question": "Ship the cart first?", "seen": True}
           and d2["desk"][1]["label"] == "D2" and "options" in d2["desk"][1], d2["desk"])
        fp = mf._work_fingerprint({**first, "desk": first["desk"] + [{"label": "D9", "question": "q"}]})
        gone = mf._work_since({**first, "desk": first["desk"]}, fp)
        ck("a desk item that closed is named in desk_closed", gone["desk_closed"] == ["D9"], gone)
        stale = json.loads(await mf.my_work(since="0123456789ab"))
        ck("a fingerprint it does not know gives everything again, with a new one",
           {"ready", "desk", "in_progress", "person"} <= set(stale) and stale["fingerprint"].startswith("w1."), sorted(stale))
        older = json.loads(await mf.my_work(since="w1.a.b.c.d.e.f.g.h"))
        ck("so does one of another shape", "ready" in older and "same" not in older and "unchanged" not in older, sorted(older))
        base = {"person": {"id": "p"}, "crew": "c", "ready": [{"ticket": "T1"}], "in_progress": [{"ticket": "T2"}],
                "bouncing": [{"ticket": "T3"}], "led_projects": [{"project": "A"}],
                "desk": [{"label": "D1", "question": "q one"}], "spec_questions": [{"id": "Q1"}]}
        bfp = mf._work_fingerprint(base)
        ck("every part of the REST answer is a part the fingerprint covers",
           set(mf.WORK_PARTS) == set(rest) - {"person", "crew"} == set(base) - {"person", "crew"},
           (mf.WORK_PARTS, sorted(rest)))
        for part in mf.WORK_PARTS:
            bump = [{"label": "D1", "question": "q changed"}] if part == "desk" else [{"x": part}]
            ans = mf._work_since({**base, part: bump}, bfp)
            ck(f"a changed {part} comes in full and the other parts are named", ans[part] == bump
               and part not in ans["same"] and sorted(ans["same"] + [part]) == sorted(mf.WORK_PARTS), ans)
        ck("a desk item whose words changed comes whole, not as seen",
           "seen" not in mf._work_since({**base, "desk": [{"label": "D1", "question": "q changed"}]}, bfp)["desk"][0])
        ck("a change to one desk item leaves the others as seen", [d.get("seen") for d in mf._work_since(
              {**base, "desk": [{"label": "D1", "question": "q one"}, {"label": "D2", "question": "new"}]},
              bfp)["desk"]] == [True, None])
        many = {**base, "desk": [{"label": f"D{i}", "question": "q " * 40, "options": ["a", "b"]} for i in range(1, 41)]}
        ck("the unchanged answer stays under 300 bytes with 40 desk items open",
           len(json.dumps(mf._work_since(many, mf._work_fingerprint(many)))) < 300)
        import inspect
        ck("review_queue takes `since`", "since" in inspect.signature(mf.review_queue).parameters)
        rq0 = json.loads(await mf.review_queue())
        ck("an empty queue answers a fingerprint and an empty list", rq0["queue"] == [] and rq0["fingerprint"], rq0)
        await mf.set_stage("T1", "review", pr="https://github.com/acme/shop/pull/7", project="Shop")
        rq = json.loads(await mf.review_queue())
        keys = {"project_name", "ticket", "title", "holder", "pr", "head", "waiting_minutes",
                "blocks_milestone", "layer1_clean", "verdict_note"}
        ck("each row keeps the keys it had", set(rq) == {"fingerprint", "queue"} and len(rq["queue"]) >= 1
           and all(set(r) == keys for r in rq["queue"]), [sorted(r) for r in rq["queue"]][:1])
        ck("a queue with a new ticket has another fingerprint", rq["fingerprint"] != rq0["fingerprint"])
        raw = await mf.review_queue(since=rq["fingerprint"])
        ck("review_queue(since=<same fingerprint>) answers {\"unchanged\": true} and nothing else",
           json.loads(raw) == {"unchanged": True} and len(raw.encode()) < 300, raw)
        full = json.loads(await mf.review_queue(since="not-a-fingerprint"))
        ck("a since that is not the fingerprint gets the whole queue", full == rq, full)
        keep_get = mf._cx

        class _Later:
            def __init__(self, real, minutes, edit=None):
                self.real, self.minutes, self.edit = real, minutes, edit or {}

            async def __aenter__(self):
                self.cx = await self.real.__aenter__()
                return self

            async def __aexit__(self, *a):
                return await self.real.__aexit__(*a)

            async def get(self, url, **kw):
                r = await self.cx.get(url, **kw)
                if url.endswith("/api/review-queue"):
                    d = r.json()
                    for q in d["queue"]:
                        q["waiting_minutes"] = (q.get("waiting_minutes") or 0) + self.minutes
                        q.update(self.edit)
                    return httpx.Response(200, json=d, request=httpx.Request("GET", url))
                return r

        mf._cx = lambda timeout=10: _Later(keep_get(timeout), 7)
        try:
            ck("minutes spent waiting are no change", json.loads(await mf.review_queue(
                since=rq["fingerprint"])) == {"unchanged": True})
            for field, edit in (("head", {"head": "b" * 40}), ("verdict_note", {"verdict": {"note": "changes asked"}})):
                mf._cx = lambda timeout=10, edit=edit: _Later(keep_get(timeout), 0, edit)
                moved = json.loads(await mf.review_queue(since=rq["fingerprint"]))
                ck(f"a new {field} on a ticket in the queue is a change", "unchanged" not in moved
                   and moved["fingerprint"] != rq["fingerprint"], moved)
        finally:
            mf._cx = keep_get
        await m.write_ticket("Refund", project="Shop")
        await mf.set_stage("T3", "review", pr="https://github.com/acme/shop/pull/8", project="Shop")
        ch = json.loads(await mf.review_queue(since=rq["fingerprint"]))
        ck("a second ticket in review shows in the next since= answer",
           "unchanged" not in ch and len(ch["queue"]) == len(rq["queue"]) + 1
           and ch["fingerprint"] != rq["fingerprint"], ch)
        await mf.set_stage("T3", "doing", project="Shop")
        back = json.loads(await mf.review_queue(since=ch["fingerprint"]))
        ck("a ticket leaving the queue shows too", "unchanged" not in back and back["fingerprint"] == rq["fingerprint"],
           back)

        print("== get_doc ==")
        big = ("## Goal\n\n" + "goal line\n" * 600 + "\n## Scope\n\nsmall scope\n\n```\n# not a heading\n```\n\n## Plan\n\n"
               + "plan line\n" * 2000)
        sd = json.loads(await m.write_doc("spec", "Spec: shop", big, project="Shop"))
        ck("the spec is over 30,000 characters", len(big) > 25_000, len(big))
        raw = await m.get_doc(sd["id"], project="Shop")
        gd = json.loads(raw)
        ck("a long doc answers under 6 KB by default", len(raw.encode()) < 6_000, len(raw.encode()))
        ck("and gives its headings with their sizes", [s["heading"] for s in gd["sections"]] == ["Goal", "Scope", "Plan"]
           and all(s["chars"] > 0 for s in gd["sections"]), gd.get("sections"))
        ck("and the start of its first section, cut", gd["body"].startswith("## Goal") and 2_000 < len(gd["body"]) <= 3_000
           and "small scope" not in gd["body"], len(gd["body"]))
        ck("and says how to read the rest", 'section="all"' in gd["note"] and f'get_doc("{sd["id"]}"' in gd["note"], gd["note"])
        ck("and keeps its id, kind and title", (gd["id"], gd["kind"], gd["title"]) == (sd["id"], "spec", "Spec: shop"), sorted(gd))
        one = json.loads(await m.get_doc(sd["id"], project="Shop", section="scope"))
        ck("section= reads one section, whatever its case", "small scope" in one["body"] and "goal line" not in one["body"]
           and "plan line" not in one["body"] and one["section"] == "scope", one["body"][:80])
        ck("a # inside a fence stays in its section", "# not a heading" in one["body"])
        hashes = json.loads(await m.get_doc(sd["id"], project="Shop", section="## Goal"))
        ck("a section is found with its hashes", hashes["body"].count("goal line") == 600, len(hashes["body"]))
        whole = json.loads(await m.get_doc(sd["id"], project="Shop", section="all"))
        ck("section=all reads the whole doc", whole["body"] == big and "sections" not in whole and "note" not in whole,
           len(whole["body"]))
        bad = await m.get_doc(sd["id"], project="Shop", section="Nope")
        ck("an unknown section names the ones there are", "no section" in bad and "Goal" in bad and "Plan" in bad, bad)
        gone = await m.get_doc("doc_nope000000", project="Shop")
        ck("a missing doc answers the error, not a doc", "\"body\"" not in gone and ("404" in gone or "not found" in gone.lower()), gone)
        edge = "## A\n\n" + "x" * (m.DOC_MAX - 6)
        ed = json.loads(await m.write_doc("note", "Edge", edge, project="Shop"))
        ck("a doc of exactly the limit comes whole", len(edge) == m.DOC_MAX
           and json.loads(await m.get_doc(ed["id"], project="Shop"))["body"] == edge)
        more = json.loads(await m.write_doc("note", "Edge+1", edge + "y", project="Shop"))
        ck("a doc one character over comes cut", "sections" in json.loads(await m.get_doc(more["id"], project="Shop")))
        sdoc = json.loads(await m.get_doc(sw["id"], project="Shop"))
        ck("a short doc comes whole, as before", sdoc["body"] == "1. charge" and "sections" not in sdoc and "note" not in sdoc, sdoc)
        ck("a section of a short doc reads", json.loads(await m.get_doc(sw["id"], project="Shop", section="all"))["body"] == "1. charge")
        many = "".join(f"## H{i}\n\ntext\n\n" for i in range(150)) + "x" * 13_000
        md = json.loads(await m.write_doc("note", "Many", many, project="Shop"))
        mg = json.loads(await m.get_doc(md["id"], project="Shop"))
        ck("a doc with many headings lists the first sixty and counts the rest", len(mg["sections"]) == m.DOC_HEADINGS + 1
           and mg["sections"][-1] == {"more": 90}, mg["sections"][-2:])
        ck("get_doc takes `section`", "section" in __import__("inspect").signature(m.get_doc).parameters)

        print("== review_brief and the repo map ==")
        mapbody = "# acme/shop\n\n## Layout\n" + "- `src/` the shop code\n" * 80
        wm = json.loads(await mf.write_repo_map("acme/shop", mapbody, "first map", project="Shop"))
        ver = wm["version"]
        b1 = await mf.review_brief("T1", project="Shop")
        ck("a brief with no known_map carries the map whole",
           "## Repo map: acme/shop" in b1 and f"version {ver}" in b1 and "the shop code" in b1, b1[-600:])
        b2 = await mf.review_brief("T1", project="Shop", known_map=ver)
        ck("a brief with the map's version carries one line for it",
           "the shop code" not in b2 and f"Version {ver}, the one you already read: unchanged" in b2
           and len(b2) < len(b1) - len(mapbody) + 300, (len(b1), len(b2)))
        ck("and the rest of the brief is the same", b1.split("## Repo map")[0] == b2.split("## Repo map")[0])
        b3 = await mf.review_brief("T1", project="Shop", known_map="0000000")
        ck("a version that is not the map's gets the map whole", "the shop code" in b3, b3[-300:])
        await mf.write_repo_map("acme/shop", mapbody + "- `docs/` the docs\n", "added docs", ver, project="Shop")
        b4 = await mf.review_brief("T1", project="Shop", known_map=ver)
        ck("a map changed since comes whole again, with its new version", "the shop code" in b4 and "`docs/`" in b4
           and "the one you already read" not in b4, b4[-300:])


if __name__ == "__main__":
    helpers()
    url_patterns()
    gist_parts()
    asyncio.run(main())
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)
