"""The crew's read tools answer small by default: get_ticket cuts a long working doc to Goal,
Steps and Progress (section= reads the rest), keeps the latest messages and checks, and answers
"unchanged" to its own fingerprint; the size helpers stop a list at a limit and a byte cap and say
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
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
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
        msgs = [{"at": f"2026-11-17T10:{i:02d}:00Z", "from": "crew-reviewer", "to": "shop-build-m1",
                 "type": "VERDICT", "first_line": f"[TF:VERDICT] T1 Shop round {i} " + "w" * 300,
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


if __name__ == "__main__":
    helpers()
    gist_parts()
    asyncio.run(main())
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)
