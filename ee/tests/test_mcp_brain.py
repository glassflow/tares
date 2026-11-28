# Tares enterprise licence, see ee/LICENSE
"""Asking the brain (TR-1200): the routes under /api/brain (ask, search, docs) and the three MCP
tools over them (`ask_brain`, `search_brain`, `get_brain_doc`).

Against the shared fake lore, never a real one. A licensed cell with brain settings asks lore with
its key; only a Ready repo is used and each repo that is not is named in `notes`; with none Ready the
answer says so and lore is not called; daily caps (quick and deep) refuse with 429 in plain words; every
call is kept in brain_calls (tool, question, repos, time, tokens, station, via); an unlicensed cell
404s every route and a cell without brain settings too.

In process, no ports. The tools list under a licence is checked in test_mcp_outcome_tools_ee.

Run: .venv/bin/python ee/tests/test_mcp_brain.py
"""
import asyncio, json, os, sys, tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings, puts the repo, ee/ and tests/ on sys.path
licensed.install_test_root()

TMP = tempfile.mkdtemp(prefix="tares-mcp-brain-")
ROOT = "root-token-" + "mcp-brain"
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_AUTH_TOKEN"] = ROOT

import httpx
from sqlalchemy import delete

import fake_cp
import fake_lore
from tares.session_store.tables import brain_calls
from tares_ee import mcp_brain
from tares_ee.brain import ask as ask_mod
from tares_ee.brain import index as index_mod
from tares_ee.brain import repos as repos_mod

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def H(tok=ROOT, **more):
    return {"Authorization": f"Bearer {tok}", **more}


BRAIN_KEY = "brain-" + "key-test-1"   # gitleaks:allow
NOT_FOUND = {"detail": "Not Found"}


async def main():
    from tares.daemon import make_app
    from tares.envelope import now_utc
    from tares import mcp_server as srv

    lore = fake_lore.FakeLore()
    repos_mod.lore_client = lore.client
    index_mod.cp_client = fake_cp.FakeCp().client

    async def never(self):                   # no background poll: these rows are set by hand
        await asyncio.Event().wait()
    index_mod.Indexer.run = never
    app = make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=H())
        store = app.state.store

        def is_404(r):
            return r.status_code == 404 and r.json() == NOT_FOUND

        def calls():
            return store.list_brain_calls()

        def put_repo(repo, state="ready", done=0, total=0, error=None):
            """A connected repo in the state asked for, with a lore source of its own."""
            src = repo.replace("/", "_")
            store.add_brain_repo(repo, "id-" + src, None, now_utc(), state=state)
            store.update_brain_repo(repo, {"files_done": done, "files_total": total, "error": error})

        def drop_repos():
            for r in store.list_brain_repos():
                store.delete_brain_repo(r["repo"])

        def clear():
            drop_repos()
            with store.sessions_db.begin() as conn:
                conn.execute(delete(brain_calls))
            lore.requests.clear()

        def asked(body=None, **more):
            return cx.post("/api/brain/ask", json=body if body is not None else {"question": "How does X work?"}, **more)

        async def licence(text):
            r = await cx.put("/api/licence", json={"key": text})
            ck("the licence is stored", r.status_code == 200, r.text[:200])

        print("== no licence, a licence without the feature, and no brain settings ==")
        os.environ["TARES_BRAIN_URL"] = fake_lore.BASE_URL
        os.environ["TARES_BRAIN_KEY"] = BRAIN_KEY
        put_repo("acme/web")
        every = (("POST", "/api/brain/ask", {"question": "q"}), ("GET", "/api/brain/search?q=x", None),
                 ("GET", "/api/brain/docs/abc", None))

        async def all_404(why):
            for method, path, body in every:
                r = await cx.request(method, path, json=body)
                ck(f"{why}: {method} {path.split('?')[0]} is 404, as an unmounted route", is_404(r), f"{r.status_code} {r.text[:100]}")
            ck(f"{why}: lore never called, nothing recorded", not lore.requests and calls() == [], str(lore.requests))
        await all_404("no licence")
        await licence(licensed.test_licence(features=("other",)))
        await all_404("licence without the enterprise feature")
        await licence(licensed.test_licence())
        for var in ("TARES_BRAIN_URL", "TARES_BRAIN_KEY"):
            saved = os.environ.pop(var)
            await all_404(f"licensed, {var} missing")
            os.environ[var] = saved
        clear()

        print("== nothing connected, or no repo ready: lore is not called ==")
        r = await asked()
        j = r.json()
        ck("no repo at all: ready false and where to connect one", r.status_code == 200 and j["ready"] is False
           and "No repo is connected" in j["message"] and "Second Brain" in j["message"] and j["notes"] == [], r.text)
        r = await cx.get("/api/brain/search", params={"q": "x"})
        ck("search with no repo says the same", r.status_code == 200 and r.json()["ready"] is False
           and "No repo is connected" in r.json()["message"], r.text)
        put_repo("acme/web", "indexing", 812, 1816)
        put_repo("acme/api", "queued")
        put_repo("acme/old", "failed", error="clone failed")
        put_repo("acme/new", "cloning")
        r = await asked()
        j = r.json()
        ck("no repo Ready: ready false and each repo named", r.status_code == 200 and j["ready"] is False
           and j["message"].startswith("No repo is ready on the brain yet.")
           and "acme/web is still indexing (812 of 1,816 files). Use grep on it for now." in j["message"]
           and "acme/api is waiting to be indexed." in j["message"]
           and "acme/old could not be indexed (clone failed)." in j["message"]
           and "acme/new is still being fetched." in j["message"] and len(j["notes"]) == 4, r.text)
        ck("... no em dash anywhere in what the person reads", "—" not in r.text)
        r = await cx.get("/api/brain/search", params={"q": "x"})
        ck("search with none Ready says the same", r.json()["ready"] is False and len(r.json()["notes"]) == 4, r.text)
        ck("lore never called, nothing recorded (no call was made)", not lore.requests and calls() == [])
        clear()

        print("== one Ready repo: ask ==")
        put_repo("acme/web")
        lore.answer = "It works like this."
        r = await asked({"question": "  How does X work?  "}, headers={"X-Tares-Station": "build-m1"})
        j = r.json()
        ck("an answer with its citations, notes and repos used", r.status_code == 200 and j["answer"] == "It works like this."
           and j["citations"][0]["path"] == "README.md" and j["notes"] == [] and j["repos"] == ["acme/web"], r.text)
        ck("... and how long it took, in whole milliseconds", isinstance(j["duration_ms"], int) and j["duration_ms"] >= 0)
        sent = lore.calls("POST", "ask")
        ck("lore got one ask, the question trimmed, fast, filtered to the repo's source",
           len(sent) == 1 and sent[0].json == {"question": "How does X work?", "mode": "fast", "source": "acme_web"}, str([s.json for s in sent]))
        ck("... with the cell's key", sent[0].headers["authorization"] == f"Bearer {lore.key}" and sent[0].status == 200)
        ck("the lore key is not in the answer", BRAIN_KEY not in r.text and lore.key not in r.text)
        rows = calls()
        row = rows[0] if rows else {}
        ck("one call kept: tool, depth, question, repos", len(rows) == 1 and (row["tool"], row["deep"], row["question"],
           row["repos"]) == ("ask_brain", False, "How does X work?", ["acme/web"]), str(rows))
        ck("... how it went: ok, time, the tokens lore reported", (row["ok"], row["error"], row["input_tokens"],
           row["output_tokens"]) == (True, None, 1, 1) and row["duration_ms"] == j["duration_ms"], str(row))
        ck("... who: the station it came with, no person (the cell's token), the console", (row["station"], row["person_id"],
           row["via"]) == ("build-m1", None, "console"), str(row))
        r = await asked({"question": "again"}, headers={"X-Tares-Via": "mcp"})
        ck("a call that says it came over MCP is kept as mcp", r.status_code == 200 and calls()[0]["via"] == "mcp"
           and calls()[0]["station"] is None, str(calls()[:1]))

        print("== deep ==")
        r = await asked({"question": "Why is it built so?", "deep": True})
        ck("a deep ask is answered", r.status_code == 200 and r.json()["answer"] == "It works like this.", r.text)
        ck("... lore got mode deep", lore.calls("POST", "ask")[-1].json["mode"] == "deep")
        ck("... and it is kept as deep", calls()[0]["deep"] is True and calls()[0]["tool"] == "ask_brain", str(calls()[0]))
        ck("the quick ask is unchanged by a deep one", lore.calls("POST", "ask")[0].json["mode"] == "fast")

        print("== refused input ==")
        before = (len(lore.requests), len(calls()))

        async def refuse(label, payload, words, code=400):
            r = await asked(payload)
            ck(label, r.status_code == code and words in r.text, f"{r.status_code} {r.text[:200]}")
            ck("  ... lore not called, nothing recorded", (len(lore.requests), len(calls())) == before)
        await refuse("no question", {}, "question: what to ask")
        await refuse("a blank question", {"question": "   "}, "question: what to ask")
        await refuse("a question that is not text", {"question": 5}, "question: what to ask")
        await refuse("a question over lore's 2,000 characters", {"question": "q" * 2001}, "at most 2,000 characters")
        await refuse("deep that is not true or false", {"question": "q", "deep": "yes"}, "deep: true or false")
        await refuse("an unknown field", {"question": "q", "mode": "deep"}, "unknown field 'mode'")
        r = await cx.post("/api/brain/ask", json=["q"])
        ck("a body that is not an object is refused (422)", r.status_code == 422, f"{r.status_code}")

        print("== several repos, some not ready ==")
        clear()
        put_repo("acme/web")
        put_repo("acme/api", "indexing", 5, 50)
        r = await asked()
        j = r.json()
        ck("one Ready repo and one indexing: asked only of the Ready one's source, the other named", r.status_code == 200
           and lore.calls("POST", "ask")[0].json.get("source") == "acme_web" and j["repos"] == ["acme/web"]
           and j["notes"] == ["acme/api is still indexing (5 of 50 files). Use grep on it for now."], f"{r.text[:300]} {[c.json for c in lore.calls('POST', 'ask')]}")
        put_repo("acme/docs")
        lore.requests.clear()
        r = await asked()
        j = r.json()
        ck("two Ready and one indexing: lore searches all its sources, the note says the third may still feed it",
           "source" not in lore.calls("POST", "ask")[0].json and sorted(j["repos"]) == ["acme/docs", "acme/web"]
           and j["notes"] == ["acme/api is still indexing (5 of 50 files): it may still feed this answer. Use grep on it too."], f"{r.text[:300]}")
        clear()
        put_repo("acme/web")
        put_repo("acme/docs")
        r = await asked()
        ck("two Ready, none waiting: no filter, no notes", "source" not in lore.calls("POST", "ask")[0].json
           and r.json()["notes"] == [], r.text[:300])
        ck("... both repos kept on the call", sorted(calls()[0]["repos"]) == ["acme/docs", "acme/web"], str(calls()[0]))

        print("== search ==")
        clear()
        put_repo("acme/web")
        put_repo("acme/api", "indexing", 1, 9)
        hit = lambda src, t: {"document_id": f"d-{src}", "source": src, "kind": "code", "title": t, "uri": None,
                              "snippet": "...", "score": 1.0, "path": "a.py", "start_line": 1, "end_line": 2}
        lore.hits = [hit("acme_web", "web.py"), hit("acme_api", "api.py")]
        r = await cx.get("/api/brain/search", params={"q": " where is auth "})
        j = r.json()
        ck("with one Ready repo, lore is asked for its source", lore.calls("GET", "search")[0].query == {
            "q": "where is auth", "k": "12", "source": "acme_web"}, str(lore.calls("GET", "search")[0].query))
        ck("a hit from a repo that is not Ready is dropped here, whatever lore returned", [h["title"] for h in j["hits"]]
           == ["web.py"] and j["repos"] == ["acme/web"] and len(j["notes"]) == 1 and "acme/api" in j["notes"][0], r.text[:300])
        row = calls()[0]
        ck("the search is kept: tool, query, repos, ok, no tokens", (row["tool"], row["deep"], row["question"], row["repos"],
           row["ok"], row["input_tokens"]) == ("search_brain", False, "where is auth", ["acme/web"], True, None), str(row))
        put_repo("acme/docs")
        lore.requests.clear()
        r = await cx.get("/api/brain/search", params={"q": "auth", "k": 3})
        ck("two Ready: no source filter, k passed on, the not-ready hits still dropped", "source" not in lore.calls("GET", "search")[0].query
           and lore.calls("GET", "search")[0].query["k"] == "3" and [h["title"] for h in r.json()["hits"]] == ["web.py"], r.text[:300])
        r = await cx.get("/api/brain/search", params={"q": "auth", "repo": "ACME/Docs"})
        ck("repo narrows it to one connected repo (any case)", lore.calls("GET", "search")[-1].query.get("source") == "acme_docs"
           and r.json()["repos"] == ["acme/docs"] and r.json()["notes"] == [], r.text[:300])
        r = await cx.get("/api/brain/search", params={"q": "auth", "repo": "acme/api"})
        n = len(lore.requests)
        ck("repo naming one that is not ready: ready false, said in words, lore not called", r.json()["ready"] is False
           and "acme/api is still indexing (1 of 9 files)" in r.json()["message"] and n == len(lore.requests) - 0, r.text[:300])
        r = await cx.get("/api/brain/search", params={"q": "auth", "repo": "acme/none"})
        ck("repo naming one that is not connected: 404 in words", r.status_code == 404 and "acme/none is not connected" in r.text, r.text)
        r = await cx.get("/api/brain/search", params={"q": "auth", "repo": "nonsense"})
        ck("repo that is not owner/name: 400", r.status_code == 400 and "owner/name" in r.text, r.text)
        for label, params, code in (("a blank q", {"q": "  "}, 400), ("no q", {}, 400), ("k of 0", {"q": "x", "k": 0}, 422),
                                    ("k over 50", {"q": "x", "k": 51}, 422)):
            before = len(lore.requests)
            r = await cx.get("/api/brain/search", params=params)
            ck(f"{label} is {code} and lore is not called", r.status_code == code and len(lore.requests) == before, f"{r.status_code} {r.text[:100]}")
        lore.hits = None
        before = (len(lore.requests), len(calls()))
        r = await cx.get("/api/brain/search", params={"q": "q" * 2001})
        ck("a search over 2,000 characters is 400 in words, lore not called, nothing recorded",
           r.status_code == 400 and "at most 2,000 characters" in r.text and (len(lore.requests), len(calls())) == before,
           f"{r.status_code} {r.text[:120]}")
        r = await cx.get("/api/brain/search", params={"q": "q" * 2000})
        ck("... and 2,000 exactly goes through", r.status_code == 200, f"{r.status_code} {r.text[:120]}")

        print("== documents ==")
        clear()
        async with lore.client() as client:
            await client.put_documents("acme_web", [{"external_id": "e1", "title": "Design", "text": "the design", "kind": "doc",
                                                      "uri": "https://x/y"}])
        did = next(iter(lore.documents.values()))["id"]
        r = await cx.get(f"/api/brain/docs/{did}")
        ck("a document by its id", r.status_code == 200 and r.json()["title"] == "Design" and r.json()["text"] == "the design", r.text[:200])
        row = calls()[0]
        ck("... kept as get_brain_doc with the id", (row["tool"], row["question"], row["repos"], row["ok"]) == ("get_brain_doc", did, [], True), str(row))
        r = await cx.get("/api/brain/docs/nope")
        ck("an unknown document is 404 in lore's words", r.status_code == 404 and "no such repo or document" in r.text, r.text)
        ck("... kept as failed, with the reason", calls()[0]["ok"] is False and "no such repo or document" in (calls()[0]["error"] or ""), str(calls()[0]))

        print("== lore fails ==")
        clear()
        put_repo("acme/web")
        lore.fail("ask", 400, "question too long")
        r = await asked()
        ck("a 400 from lore stays a 400 (the caller's input), not a 502 to retry", r.status_code == 400, f"{r.status_code} {r.text[:200]}")
        ck("... kept as failed", calls()[0]["ok"] is False, str(calls()[0]))
        lore.fail("ask", 500, "model is down")
        r = await asked()
        ck("a lore failure is 502 in plain words, key not shown", r.status_code == 502 and "The brain had a problem" in r.text
           and BRAIN_KEY not in r.text and lore.key not in r.text, f"{r.status_code} {r.text[:200]}")
        row = calls()[0]
        ck("... kept as failed with the reason and the time", row["ok"] is False and "The brain had a problem" in row["error"]
           and row["duration_ms"] is not None and row["input_tokens"] is None, str(row))
        lore.fail("ask", 429, "slow down")
        r = await asked()
        ck("lore busy is 429 in plain words", r.status_code == 429 and "busy" in r.text, f"{r.status_code} {r.text[:200]}")
        lore.raise_next("ask", httpx.ReadTimeout("slow"))
        r = await asked()
        ck("lore not answering in time is 502, said in plain words", r.status_code == 502 and "did not answer" in r.text, f"{r.status_code} {r.text[:200]}")
        lore.fail("search", 500, "index broken")
        r = await cx.get("/api/brain/search", params={"q": "x"})
        ck("a search failure is 502 too", r.status_code == 502 and calls()[0]["ok"] is False, f"{r.status_code} {r.text[:200]}")
        r = await asked()
        ck("and the next ask works again", r.status_code == 200, r.text[:200])

        print("== tokens lore reported are kept ==")
        clear()
        put_repo("acme/web")
        plain = lore._ask
        lore._ask = lambda rec, args: fake_lore._ok({"answer": "a", "mode": "fast", "model": "m", "citations": [],
                                                     "sources_considered": 0, "tool_calls": 0, "latency_ms": 1,
                                                     "usage": {"input_tokens": 1234, "output_tokens": 56}})
        await asked()
        ck("input and output tokens are kept as lore said them", (calls()[0]["input_tokens"], calls()[0]["output_tokens"]) == (1234, 56), str(calls()[0]))
        lore._ask = lambda rec, args: fake_lore._ok({"answer": "a", "citations": [], "usage": {"input_tokens": "x", "output_tokens": True}})
        await asked()
        ck("tokens that are not whole numbers are not kept", (calls()[0]["input_tokens"], calls()[0]["output_tokens"]) == (None, None), str(calls()[0]))
        lore._ask = lambda rec, args: fake_lore._ok({"answer": "a", "citations": []})
        await asked()
        ck("no usage at all keeps none, the call still works", calls()[0]["ok"] is True and calls()[0]["input_tokens"] is None)
        lore._ask = plain

        print("== daily caps ==")
        ck("the defaults are 200 quick and 20 deep a day", ask_mod.caps() == (200, 20))
        os.environ["TARES_BRAIN_ASK_DAILY"] = "2"
        os.environ["TARES_BRAIN_DEEP_DAILY"] = "1"
        ck("the environment sets them", ask_mod.caps() == (2, 1))
        clear()
        put_repo("acme/web")
        midnight = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
        store.reserve_brain_call({"id": "old1", "at": midnight - timedelta(minutes=1), "tool": "ask_brain", "deep": False,
                                  "question": "yesterday", "repos": []})
        store.reserve_brain_call({"id": "old2", "at": midnight - timedelta(hours=5), "tool": "ask_brain", "deep": True,
                                  "question": "yesterday", "repos": []})
        r1, r2 = await asked(), await asked()
        ck("yesterday's calls do not count: the first two quick asks of today go through", r1.status_code == 200 and r2.status_code == 200,
           f"{r1.status_code} {r2.status_code}")
        n = len(lore.calls("POST", "ask"))
        r = await asked()
        ck("the third is 429 in plain words", r.status_code == 429 and "used its 2 answers for today" in r.text
           and "midnight UTC" in r.text and "—" not in r.text, f"{r.status_code} {r.text[:200]}")
        ck("... lore not called and the refused call is not kept", len(lore.calls("POST", "ask")) == n
           and len([c for c in calls() if c["tool"] == "ask_brain" and c["question"] != "yesterday"]) == 2)
        r = await asked({"question": "deep one", "deep": True})
        ck("a deep ask has its own cap: the first goes through", r.status_code == 200, r.text[:200])
        r = await asked({"question": "deep two", "deep": True})
        ck("the second deep ask is 429, said for deep answers", r.status_code == 429 and "used its 1 deep answers for today" in r.text, f"{r.status_code} {r.text[:200]}")
        r = await cx.get("/api/brain/search", params={"q": "x"})
        ck("a search is not capped", r.status_code == 200)
        os.environ["TARES_BRAIN_ASK_DAILY"] = "3"
        r = await asked()
        ck("a raised cap takes effect at once", r.status_code == 200, f"{r.status_code} {r.text[:100]}")
        os.environ["TARES_BRAIN_ASK_DAILY"] = "0"
        r = await asked()
        ck("a cap of 0 refuses every ask", r.status_code == 429, f"{r.status_code}")
        for bad in ("lots", "-4", "", "  "):
            os.environ["TARES_BRAIN_ASK_DAILY"] = bad
            os.environ["TARES_BRAIN_DEEP_DAILY"] = bad
            ck(f"a cap of {bad!r} falls back to the default", ask_mod.caps() == (200, 20), str(ask_mod.caps()))
        os.environ["TARES_BRAIN_ASK_DAILY"] = "1"
        clear()
        put_repo("acme/web")
        lore.fail("ask", 500, "down")
        await asked()
        r = await asked()
        ck("a failed ask counts against the cap (it spent lore's time)", r.status_code == 429, f"{r.status_code} {r.text[:100]}")
        for var in ("TARES_BRAIN_ASK_DAILY", "TARES_BRAIN_DEEP_DAILY"):
            os.environ.pop(var)

        print("== the MCP tools ==")
        clear()
        put_repo("acme/web")
        put_repo("acme/api", "indexing", 1, 9)
        ck("three tools, named for the three jobs", [t.__name__ for t in mcp_brain.TOOLS] == ["ask_brain", "search_brain", "get_brain_doc"])
        srv.TARESD = "http://t"
        srv._cx = lambda timeout=10: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", timeout=timeout,
                                                       headers=H(**{"X-Tares-Station": "ashish-build"}))
        out = json.loads(await mcp_brain.ask_brain("How does X work?"))
        ck("ask_brain: the answer, citations, notes and repos, nothing else", set(out) == {"answer", "citations", "notes", "repos", "mode"}
           and out["answer"] == "It works like this." and out["notes"] and "acme/api" in out["notes"][0], str(out))
        row = calls()[0]
        ck("... the call is kept as coming over MCP from that station", (row["via"], row["station"], row["tool"], row["deep"]) == ("mcp", "ashish-build", "ask_brain", False), str(row))
        out = json.loads(await mcp_brain.ask_brain("Why?", deep=True))
        ck("ask_brain deep asks lore deep", out["answer"] and lore.calls("POST", "ask")[-1].json["mode"] == "deep" and calls()[0]["deep"] is True)
        lore.hits = [hit("acme_web", "web.py")]
        out = json.loads(await mcp_brain.search_brain("auth", k=5))
        ck("search_brain: hits, notes and repos", set(out) == {"hits", "notes", "repos"} and out["hits"][0]["title"] == "web.py", str(out))
        ck("... kept as search_brain over MCP", (calls()[0]["tool"], calls()[0]["via"]) == ("search_brain", "mcp"), str(calls()[0]))
        out = json.loads(await mcp_brain.search_brain("auth", repo="acme/api"))
        ck("search_brain on a repo not ready says so", out["ready"] is False and "still indexing" in out["message"], str(out))
        lore.hits = None
        out = json.loads(await mcp_brain.get_brain_doc(did))
        ck("get_brain_doc: the document", out["title"] == "Design" and out["text"] == "the design", str(out))
        out = await mcp_brain.get_brain_doc("nope?x=1#top")
        ck("a document id with ? and # reaches the cell whole, not cut at the ?", calls()[0]["question"] == "nope?x=1#top", str(calls()[0]))
        out = await mcp_brain.get_brain_doc("nope")
        ck("a refusal comes back as words, not a crash", out.startswith("error 404:") and "no such repo or document" in out, out)
        drop_repos()
        out = json.loads(await mcp_brain.ask_brain("x"))
        ck("ask_brain with no repo says where to connect one", out == {"ready": False, "message": out["message"], "notes": []}
           and "No repo is connected" in out["message"], str(out))
        os.environ["TARES_BRAIN_ASK_DAILY"] = "0"
        put_repo("acme/web")
        out = await mcp_brain.ask_brain("x")
        ck("a capped ask comes back as the plain words", out.startswith("error 429:") and "used its 0 answers" in out, out)
        os.environ.pop("TARES_BRAIN_ASK_DAILY")
        await licence(licensed.test_licence(features=("other",)))
        out = await mcp_brain.ask_brain("x")
        ck("an unlicensed cell answers the tool with a plain 404", out.startswith("error 404:"), out)
        await cx.aclose()


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
