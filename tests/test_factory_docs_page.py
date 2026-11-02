"""The factory Docs and memory page (TR-737): GET /api/factory/docs and /api/factory/docs/{id}.

The list (the two shared documents, each open project's own, the finished ones), one document as
rows with who and when (per line for a project's memory, for the whole document elsewhere), lines
added through the routes that already exist and read back, and the daemon's own API reference
moved off /docs so a full page load of /docs reaches the console.

Run: .venv/bin/python tests/test_factory_docs_page.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-factory-docs-")
_DIST = os.path.join(_TMP, "dist")
os.makedirs(_DIST)
with open(os.path.join(_DIST, "index.html"), "w") as f:
    f.write("<!doctype html><html><body><div id=root>console-shell-marker</div></body></html>")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-docs"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"
os.environ["TARES_UI_DIST"] = _DIST

import httpx

from tares import words
from tares.screens import factory_docs as fd

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def units():
    print("== the helpers ==")
    now = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    d = lambda **kw: now - timedelta(**kw)
    ck("ago: minutes, hours, days (not weeks) up to two months, then weeks",
       [fd.ago(d(seconds=10), now), fd.ago(d(minutes=5), now), fd.ago(d(hours=3), now),
        fd.ago(d(days=1, hours=2), now), fd.ago(d(days=12), now), fd.ago(d(days=70), now),
        fd.ago(None, now)]
       == ["just now", "5 minutes ago", "3 hours ago", "1 day ago", "12 days ago", "10 weeks ago", ""],
       [fd.ago(d(days=12), now), fd.ago(d(days=70), now)])
    rows = fd.blocks("# Title\n\nFirst line\nsecond line.\n\n## Part\n- one\n- two\n\n```\ncode\n\nmore\n```\n"
                     "| a | b |\n|---|---|\n| 1 | 2 |\n")
    ck("blocks: a heading and a bullet are rows, a paragraph and a fence stay whole",
       [(r["kind"], r["text"]) for r in rows]
       == [("h1", "Title"), ("p", "First line\nsecond line."), ("h2", "Part"), ("li", "one"),
           ("li", "two"), ("p", "```\ncode\n\nmore\n```"), ("p", "| a | b |\n|---|---|\n| 1 | 2 |")],
       rows)
    m = fd.STAMP.search("Expand the questions first. (2026-10-25, crew-orchestrator)")
    ck("a memory line's stamp is its day and its author",
       m and m.groups() == ("2026-10-25", "crew-orchestrator"), m and m.groups())
    ck("text with a bracket in the middle has no stamp", fd.STAMP.search("Use (a, b) pairs here") is None)


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    mk = lambda token=TOKEN: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t",
        headers={"Authorization": f"Bearer {token}"} if token else {})

    async def doc_of(cx, doc_id):
        return (await cx.get(f"/api/factory/docs/{doc_id}")).json()["doc"]

    def clean(label, body):
        found = words.offenders_in(body, ("title", "name", "text"))
        ck(label + ": no forbidden word or dash outside the documents", found == [], found)

    async with app.router.lifespan_context(app):
        async with mk() as cx:
            print("== a cell with no documents ==")
            r = await cx.get("/api/factory/docs")
            a = r.json()
            ck("the list answers", r.status_code == 200, r.text)
            ck("empty, with the two shared documents named by kind",
               a["empty"] is True and [s["kind"] for s in a["shared"]] == ["agents", "memory"]
               and [s["id"] for s in a["shared"]] == ["agents", "memory"]
               and a["projects"] == [] and a["finished"] == [], a)
            r = await cx.get("/api/factory/docs/memory")
            d = r.json()["doc"]
            ck("with no document the list's own open document is none", a["doc"] is None, a)
            ck("a shared document nobody wrote yet is empty and takes a first line",
               r.status_code == 200 and d["empty"] is True and d["lines"] == []
               and d["changed"] == "Nothing here yet." and d["shared"] is True
               and d["add"]["path"] == "/api/docs/global/memory/lines", d)
            r = await cx.get("/api/factory/docs/doc_nope")
            ck("an unknown document is 404", r.status_code == 404, r.status_code)

            print("== projects and their documents ==")
            ids = {}
            for name in ("project-x", "project-y", "project-z", "project-w"):
                r = await cx.post("/api/projects", json={"template": "custom", "name": name, "objects": [],
                                                         "goal": "g",
                                                         **({} if name == "project-w"
                                                            else {"kind": "software_factory"})})
                ids[name] = r.json()["id"]
            px, py, pz, pw = (ids[n] for n in ("project-x", "project-y", "project-z", "project-w"))
            mkdoc = lambda uid, kind, title, body: cx.post(
                f"/api/projects/{uid}/docs", json={"kind": kind, "title": title, "body": body})
            for uid, kind, title, body in (
                    (px, "spec", "Spec", "# Spec\n\nBuild the docs site.\n"),
                    (px, "plan", "Plan: project-x", "# Plan\n\n## M1: pages\n\nGoal: pages live.\n- T1 Home page\n"),
                    (px, "agents", "Rules: project-x", "# Rules\n\nUse tabs.\n"),
                    (px, "note", "A note", "not shown"),
                    (py, "spec", "Spec", "Move the blog."),
                    (pw, "spec", "Spec", "Not a factory project.")):
                r = await mkdoc(uid, kind, title, body)
                assert r.status_code == 201, r.text
            store.update_project(py, status="done")

            r = await cx.post("/api/projects/%s/memory/lines" % px, json={"text": "Keep one fact per line.",
                                                                          "by": "crew-orchestrator"})
            ck("a line goes in project memory through the existing route", r.status_code == 201
               and r.json()["added"] is True, r.text)
            mem_id = r.json()["id"]
            for kind, line in (("agents", "Stop only processes you started."),
                               ("agents", "Use factory-heavy for builds.")):
                r = await cx.post(f"/api/docs/global/{kind}/lines", json={"text": line, "by": "crew-reviewer"})
                assert r.status_code == 200, r.text

            a = (await cx.get("/api/factory/docs")).json()
            clean("the list", a)
            ck("not empty, two shared documents with their real ids",
               a["empty"] is False and len(a["shared"]) == 2
               and a["shared"][0]["id"].startswith("doc_") and a["shared"][0]["label"] == "Rules"
               and a["shared"][1]["label"] == "Memory", a["shared"])
            ck("a project with documents brings the shared ones into being: real ids",
               all(x["id"].startswith("doc_") for x in a["shared"]), a["shared"])
            ck("one open factory project with spec, plan, rules and memory, in reading order",
               [p["name"] for p in a["projects"]] == ["project-x"]
               and [d["label"] for d in a["projects"][0]["docs"]] == ["Spec", "Plan", "Rules", "Memory"]
               and a["projects"][0]["id"] == px, a["projects"])
            ck("a finished project is listed apart; one without documents or not a factory one is not",
               [p["name"] for p in a["finished"]] == ["project-y"], a)
            ck("the list has no body", all("body" not in d and "lines" not in d
                                           for p in a["projects"] for d in p["docs"]), a)
            ck("the list route opens the first shared document", a["doc"]["id"] == a["shared"][0]["id"]
               and a["doc"]["label"] == "Rules", a["doc"] and a["doc"]["id"])
            one = (await cx.get(f"/api/factory/docs/{mem_id}")).json()
            ck("one document's route answers the same page with that document open",
               {k: one[k] for k in ("shared", "projects", "finished", "empty")}
               == {k: a[k] for k in ("shared", "projects", "finished", "empty")}
               and one["doc"]["id"] == mem_id, one["doc"]["id"])

            print("== who and when ==")
            # a builder session, and a memory line from 3 days ago written by it
            store_now = datetime.now(timezone.utc)
            ts = (store_now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            await cx.post("/ingest/claude_code", headers={"Content-Type": "application/x-ndjson"},
                          content=json.dumps({"type": "session_station", "sessionId": "b1", "cwd": "/w",
                                              "timestamp": ts, "station": "project-x-build-m1",
                                              "role": "builder", "parent": "crew-orchestrator",
                                              "factory_station": "project-x-build-m1"}) + "\n")
            day = lambda n: (store_now - timedelta(days=n)).date().isoformat()
            body = ("# Memory: project-x\n\nWhat the builders learned about this project.\n\n"
                    f"- Keep one fact per line. ({day(9)}, crew-orchestrator)\n"
                    f"- Expand the questions first. ({day(3)}, project-x-build-m1)\n"
                    f"- Checks come before review. ({day(1)}, console)\n"
                    f"- A line with no stamp.\n"
                    f"- Not a real day. (2026-02-30, someone)\n")
            store.update_doc(mem_id, body=body, by="project-x-build-m1")
            d = await doc_of(cx, mem_id)
            clean("project memory", d)
            L = d["lines"]
            ck("the note that opens the memory is not a line",
               [x["text"] for x in L] == ["Keep one fact per line.", "Expand the questions first.",
                                          "Checks come before review.", "A line with no stamp.",
                                          "Not a real day. (2026-02-30, someone)"], L)
            ck("a stamp becomes who and when, in role words", (L[0]["by"], L[0]["when"]) == ("the Lead", "9 days ago")
               and (L[1]["by"], L[1]["when"]) == ("the Builder", "3 days ago")
               and (L[2]["by"], L[2]["when"]) == ("you", "1 day ago"), L)
            ck("a line without a stamp has no author", (L[3]["by"], L[3]["when"]) == ("", ""), L[3])
            ck("an impossible date stays in the line as written, with no author and no 500",
               (L[4]["text"], L[4]["by"], L[4]["when"]) == ("Not a real day. (2026-02-30, someone)", "", ""), L[4])
            ck("the header names who changed it and when",
               d["changed"] == "Changed by the Builder, just now" and d["project"] == {"id": px, "name": "project-x"}
               and d["shared"] is False and d["kind"] == "memory", d)
            ck("memory takes a line through the project memory route",
               d["add"]["path"] == f"/api/projects/{px}/memory/lines", d["add"])

            print("== a line added from the page ==")
            r = await cx.post(d["add"]["path"], json={"text": "Run the checks twice.", "by": "console"})
            ck("added", r.status_code == 201 and r.json()["added"] is True, r.text)
            L2 = (await doc_of(cx, mem_id))["lines"]
            ck("it is the last line, by you, just now",
               L2[-1]["text"] == "Run the checks twice." and (L2[-1]["by"], L2[-1]["when"]) == ("you", "just now")
               and len(L2) == 6, L2[-1])
            r = await cx.post(d["add"]["path"], json={"text": "Run the checks twice.", "by": "console"})
            ck("the same line again is refused, and the server says so (added false)",
               r.status_code == 201 and r.json()["added"] is False, r.text)
            ck("and nothing changed", len((await doc_of(cx, mem_id))["lines"]) == 6)
            r = await cx.post(d["add"]["path"], json={"text": "   ", "by": "console"})
            ck("an empty line is refused with a sentence", r.status_code == 400 and "one line" in r.text, r.text)

            print("== the shared documents and the project's other documents ==")
            a = (await cx.get("/api/factory/docs")).json()
            rules = await doc_of(cx, a['shared'][0]['id'])
            clean("the shared rules", rules)
            ck("the shared rules: the opening note and each line, no title, no author per line",
               [x["text"] for x in rules["lines"]][-2:] == ["Stop only processes you started.",
                                                            "Use factory-heavy for builds."]
               and rules["lines"][0]["kind"] == "p" and all(x["by"] == "" and x["when"] == "" for x in rules["lines"]),
               rules["lines"])
            ck("the shared rules say who changed them, as a whole", rules["changed"] == "Changed by the Reviewer, just now",
               rules["changed"])
            r = await cx.post(rules["add"]["path"], json={"text": "Name the ticket in each commit.", "by": "console"})
            rules2 = await doc_of(cx, rules['id'])
            ck("a line added to the shared rules is the last one, and the header says you",
               r.status_code == 200 and rules2["lines"][-1]["text"] == "Name the ticket in each commit."
               and rules2["changed"] == "Changed by you, just now", rules2["changed"])
            r = await cx.post(rules["add"]["path"], json={"text": "Name the ticket in each commit.", "by": "console"})
            ck("a duplicate shared line is refused (added false)", r.json()["added"] is False, r.text)
            r = await cx.post("/api/docs/global/memory/lines", json={"text": "Alex prefers short PRs.", "by": "console"})
            a = (await cx.get("/api/factory/docs")).json()
            ck("the shared memory shows the line",
               [x["text"] for x in (await doc_of(cx, a['shared'][1]['id']))["lines"]]
               == ["Alex prefers short PRs."], a["shared"])
            plan_id = next(x["id"] for x in a["projects"][0]["docs"] if x["kind"] == "plan")
            plan = await doc_of(cx, plan_id)
            clean("the plan", plan)
            ck("a plan: headings and rows in order, the title left to the header, no way to add",
               [(x["kind"], x["text"]) for x in plan["lines"]]
               == [("h2", "M1: pages"), ("p", "Goal: pages live."), ("li", "T1 Home page")]
               and plan["add"] is None and plan["project"]["name"] == "project-x", plan["lines"])
            ck("titles are in words: shared ones and '<word>: <project>'",
               [x["title"] for x in a["shared"]] == ["Rules for every project", "Memory for every project"]
               and rules["title"] == "Rules for every project" and plan["title"] == "Plan: project-x", plan["title"])
            ck("a plan has no author on its lines", all(x["by"] == "" for x in plan["lines"]))
            note = next(x for x in store.list_docs(px) if x["kind"] == "note")
            r = await cx.get(f"/api/factory/docs/{note['id']}")
            ck("a note is not a document of this page", r.status_code == 404, r.status_code)
            r = await cx.get(f"/api/factory/docs/{next(x for x in store.list_docs(pw) if x['kind'] == 'spec')['id']}")
            ck("nor is a document of a project that is not a factory project", r.status_code == 404, r.status_code)

            print("== the pages: /docs is the console, the API reference moved ==")
            ck("a full page load of /docs without a token serves the console",
               (r := await mk(None).get("/docs")).status_code == 200 and "console-shell-marker" in r.text, r.text[:200])
            ck("with a document in the address too",
               (r := await mk(None).get(f"/docs?doc={mem_id}")).status_code == 200
               and "console-shell-marker" in r.text, r.status_code)
            ck("the API reference answers at /api/reference",
               (r := await cx.get("/api/reference")).status_code == 200 and "swagger" in r.text.lower(), r.status_code)
            ck("and its schema is still at /openapi.json",
               "/api/factory/docs" in (await mk(None).get("/openapi.json")).json()["paths"])
            ck("/redoc is gone: the console answers", "console-shell-marker" in (await mk(None).get("/redoc")).text)
            ck("the reference is behind the token, like the rest of /api",
               (await mk(None).get("/api/reference")).status_code == 401)
            ck("the documents are behind the token", (await mk(None).get("/api/factory/docs")).status_code == 401)


units()
asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
