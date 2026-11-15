# Tares enterprise licence, see ee/LICENSE
"""The cell's client for lore (TR-1216): each call against the fake lore, the timeouts, the plain
errors, and that neither the lore key nor a GitHub token reaches an error message or a log line.

Run: .venv/bin/python ee/tests/test_brain_lore_client.py
"""
import asyncio, io, logging, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # puts the repo and tests/ on sys.path
import httpx
import fake_lore
from tares_ee.brain import lore as L
from tares_ee.brain.lore import BrainNotConfigured, LoreClient, LoreError

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

# Made-up secrets, put together here so no line of this file reads as a credential to a secret scan.
KEY = "lore-" + "key-4f9c2a7e1b"   # gitleaks:allow
TOKEN = "gh" + "s_Zx81QpLmNo93TrWvKd0aBcDeFg"   # gitleaks:allow
PLAIN = "abc" + "123secret"   # gitleaks:allow
WRONG = "wrong-" + "key-123"   # gitleaks:allow
LOG = io.StringIO()
_h = logging.StreamHandler(LOG)
logging.getLogger("tares_ee.brain.lore").addHandler(_h)
logging.getLogger("tares_ee.brain.lore").setLevel(logging.DEBUG)


async def refused(coro):
    """The LoreError a call raises, or None when it did not."""
    try:
        await coro
    except LoreError as e:
        return e
    return None


async def main():
    # -- configuration ----------------------------------------------------------------------
    for env in ({}, {"TARES_BRAIN_URL": "http://x"}, {"TARES_BRAIN_KEY": "k"},
                {"TARES_BRAIN_URL": "  ", "TARES_BRAIN_KEY": "k"}, {"TARES_BRAIN_URL": "http://x", "TARES_BRAIN_KEY": ""}):
        try:
            LoreClient.from_env(env)
            ck(f"missing env {sorted(env)} refused", False)
        except BrainNotConfigured as e:
            ck(f"missing env {sorted(env)}: the brain is not configured", str(e) == L.NOT_CONFIGURED and not LoreClient.configured(env), str(e))
    ck("configured when both are set", LoreClient.configured({"TARES_BRAIN_URL": "http://x", "TARES_BRAIN_KEY": "k"}))
    c = LoreClient.from_env({"TARES_BRAIN_URL": " http://lore:8080/ ", "TARES_BRAIN_KEY": " k "})
    ck("url is trimmed of spaces and a trailing slash", c.base_url == "http://lore:8080")
    await c.aclose()
    try:
        LoreClient("lore:8080", "k")
        ck("a url without a scheme is refused", False)
    except LoreError as e:
        ck("a url without a scheme is refused", "http://" in str(e))

    fake = fake_lore.FakeLore(KEY)
    async with fake.client() as lore:
        # -- repos --------------------------------------------------------------------------
        repo = await lore.create_repo("tares", remote_url="https://github.com/glassflow/tares.git",
                                      web_url_template="https://github.com/glassflow/tares/blob/{commit}/{path}")
        r = fake.calls(route="create_repo")[-1]
        ck("create_repo posts name, remote and template with the Bearer key",
           r.method == "POST" and r.path == "/v1/repos" and r.json == {
               "name": "tares", "remote_url": "https://github.com/glassflow/tares.git",
               "web_url_template": "https://github.com/glassflow/tares/blob/{commit}/{path}"}
           and r.headers["authorization"] == f"Bearer {KEY}", str(r))
        ck("create_repo returns lore's repo", repo["name"] == "tares" and repo["id"] and repo["remote_url"].endswith("tares.git"))
        again = await lore.create_repo("tares")
        ck("a second create of the same name keeps the repo and sends no remote", again["id"] == repo["id"]
           and "remote_url" not in fake.calls(route="create_repo")[-1].json)
        listed = await lore.list_repos()
        ck("list_repos", [x["name"] for x in listed["repos"]] == ["tares"])
        for bad in ("", "  ", None, "a/b", "a b", "x" * 101, "../x"):
            e = await refused(lore.create_repo(bad))
            ck(f"create_repo refuses the name {bad!r} before any call", e is not None and e.status is None, str(e))
        ck("no call went out for a refused name", len(fake.calls(route="create_repo")) == 2)

        sync = await lore.sync_repo(repo["id"], token=TOKEN)
        r = fake.calls(route="sync_repo")[-1]
        ck("sync_repo posts the token in the body, not the path or query",
           r.path == f"/v1/repos/{repo['id']}/sync" and r.json == {"token": TOKEN} and not r.query and TOKEN not in r.path, str(r))
        ck("sync_repo returns lore's report", sync.get("indexed_commit") == "0" * 40 and sync["files_indexed"] == 1 and sync["full"] is True, str(sync))
        ck("sync waits up to 600 s for the whole index run, not 10", r.timeout.get("read") == 600.0, str(r.timeout))
        async with fake.client(sync_timeout=7) as quick:
            await quick.sync_repo(repo["id"])
        ck("the sync timeout can be set", fake.calls(route="sync_repo")[-1].timeout.get("read") == 7.0)
        for bad in (123, ["t"]):
            n = len(fake.requests)
            e = await refused(lore.sync_repo(repo["id"], token=bad))
            ck(f"sync_repo refuses a token that is {bad!r} without a call", e is not None and len(fake.requests) == n, str(e))
        for bad in (5, ["u"]):
            n = len(fake.requests)
            e = await refused(lore.create_repo("tares", remote_url=bad))
            e2 = await refused(lore.create_repo("tares", web_url_template=bad))
            ck(f"create_repo refuses a remote or template that is {bad!r} without a call", e is not None and e2 is not None and len(fake.requests) == n, f"{e} {e2}")
        await lore.sync_repo(repo["id"], token="  ")
        ck("a blank token sends an empty body", fake.calls(route="sync_repo")[-1].json == {})
        e = await refused(lore.sync_repo(""))
        ck("sync_repo refuses a blank repo id", e is not None and "repo id" in str(e))
        e = await refused(lore.sync_repo("nope"))
        ck("an unknown repo is a plain not-found", e is not None and e.status == 404 and "no such repo" in str(e), str(e))

        fake.progress[repo["id"]] = {"stage": "indexing", "files_done": 3, "files_total": 10}
        prog = await lore.repo_progress(repo["id"])
        ck("repo_progress returns lore's json as it is", prog == {"stage": "indexing", "files_done": 3, "files_total": 10}
           and fake.calls(route="repo_progress")[-1].path == f"/v1/repos/{repo['id']}/progress")
        e = await refused(lore.repo_progress("nope"))
        ck("progress of an unknown repo is a 404", e is not None and e.status == 404)

        # -- documents ----------------------------------------------------------------------
        docs = [{"external_id": "notes/a.md", "title": "Notes", "text": "licence key lives in the cell", "kind": "note"},
                {"external_id": "b", "title": "B", "text": ""}]
        rep = await lore.put_documents("notes", docs)
        ck("put_documents sends the source and documents", rep["upserted"] == 2
           and fake.calls(route="put_documents")[-1].json == {"source": "notes", "documents": docs})
        ck("the same documents again are unchanged", (await lore.put_documents("notes", docs))["unchanged"] == 2)
        for src, d, why in (("", docs, "source"), (None, docs, "source"), ("notes", [], "no documents"),
                            ("notes", [{"external_id": "x", "title": "T"}], "text"),
                            ("notes", [{"external_id": " ", "title": "T", "text": "t"}], "external_id"),
                            ("notes", [{"external_id": "x", "title": "", "text": "t"}], "title")):
            e = await refused(lore.put_documents(src, d))
            ck(f"put_documents refuses {why!r}", e is not None and e.status is None, str(e))
        n = len(fake.requests)
        e = await refused(lore.put_documents("notes", [None]))
        e2 = await refused(lore.put_documents("notes", ["text"]))
        ck("put_documents refuses an entry that is not an object", e is not None and e2 is not None and len(fake.requests) == n, f"{e} {e2}")
        doc_id = fake.documents[("notes", "notes/a.md")]["id"]
        got = await lore.get_document(doc_id)
        ck("get_document by id", got["title"] == "Notes" and fake.calls(route="get_document")[-1].path == f"/v1/documents/{doc_id}")
        e = await refused(lore.get_document("00000000-0000-0000-0000-000000000000"))
        ck("a missing document is a 404", e is not None and e.status == 404)
        e = await refused(lore.get_document(" "))
        ck("get_document refuses a blank id", e is not None)
        ck("delete_document returns nothing", await lore.delete_document("notes", "notes/a.md") is None)
        r = fake.calls(route="delete_document")[-1]
        ck("the external id keeps its slash in the path", r.path == "/v1/documents/notes/notes/a.md" and ("notes", "notes/a.md") not in fake.documents, r.path)
        await refused(lore.delete_document("my source", "a b/c?d"))   # nothing stored there: 404, the path is what counts
        ck("a source and id are escaped", fake.calls(route="delete_document")[-1].path == "/v1/documents/my%20source/a%20b/c%3Fd", fake.requests[-1].path)
        e = await refused(lore.delete_document("notes", "notes/a.md"))
        ck("deleting twice is a 404", e is not None and e.status == 404)
        for src, ext in (("notes", ".."), ("notes", "a/../b"), ("..", "a"), ("", "a"), ("notes", "")):
            n = len(fake.requests)
            e = await refused(lore.delete_document(src, ext))
            ck(f"delete_document refuses {src!r}/{ext!r} without a call", e is not None and len(fake.requests) == n)

        # -- search -------------------------------------------------------------------------
        res = await lore.search("licence", k=5, kind=["note", " ", "code"], source="notes")
        r = fake.calls(route="search")[-1]
        ck("search sends q, k, kinds joined by commas and the source",
           r.query == {"q": "licence", "k": "5", "kind": "note,code", "source": "notes"}, str(r.query))
        ck("search of an empty index returns no hits", res["hits"] == [])
        await lore.put_documents("notes", docs[:1])
        res = await lore.search("licence KEY")
        ck("search finds a stored document", [h["external_id"] for h in res["hits"]] == ["notes/a.md"]
           and fake.calls(route="search")[-1].query == {"q": "licence KEY"})
        for bad in (5, ["x"]):
            n = len(fake.requests)
            e = await refused(lore.search("q", source=bad))
            e2 = await refused(lore.ask("q", source=bad))
            e3 = await refused(lore.delete_document(bad, "x"))
            ck(f"a source that is {bad!r} is a LoreError, not an AttributeError", e is not None and e2 is not None and e3 is not None and len(fake.requests) == n, f"{e} {e2} {e3}")
        await lore.search("x", kind="code", source="  ")
        ck("a kind string and a blank source", fake.calls(route="search")[-1].query == {"q": "x", "kind": "code"})
        for q in ("", "   ", None):
            n = len(fake.requests)
            e = await refused(lore.search(q))
            ck(f"search refuses q={q!r} without a call", e is not None and len(fake.requests) == n)
        for bad_k in ("5", 1.5, True, "abc"):
            n = len(fake.requests)
            e1 = await refused(lore.search("q", k=bad_k))
            e2 = await refused(lore.ask("q", k=bad_k))
            ck(f"search and ask refuse k={bad_k!r} without a call", e1 is not None and e2 is not None and len(fake.requests) == n, f"{e1} {e2}")
        fake.hits = []
        ck("empty hits are an empty list", (await lore.search("zzz"))["hits"] == [])

        # -- ask ----------------------------------------------------------------------------
        fake.answer = "In licence.py."
        a = await lore.ask("how does a cell get its licence key?")
        r = fake.calls(route="ask")[-1]
        ck("a fast ask: question and mode, 10 s", r.json == {"question": "how does a cell get its licence key?", "mode": "fast"}
           and r.timeout.get("read") == 10.0 and a["answer"] == "In licence.py." and a["citations"][0]["url"], str(r))
        d = await lore.ask("  go deeper  ", mode="deep", k=20, kind=["code"], source="tares")
        r = fake.calls(route="ask")[-1]
        ck("a deep ask: mode deep, filters, 120 s", r.json == {"question": "go deeper", "mode": "deep", "k": 20, "kind": ["code"], "source": "tares"}
           and r.timeout.get("read") == 120.0 and d["mode"] == "deep", str(r))
        await lore.ask("q", kind="code")
        ck("an ask with a kind string sends one kind, not its letters", fake.calls(route="ask")[-1].json["kind"] == ["code"], str(fake.calls(route="ask")[-1].json))
        await lore.ask("q", kind=[" ", None, "doc"])
        ck("ask drops blank and non-text kinds", fake.calls(route="ask")[-1].json["kind"] == ["doc"])
        await lore.ask("q", kind="  ")
        ck("a blank kind string sends no kind", "kind" not in fake.calls(route="ask")[-1].json)
        for q, mode in (("", "fast"), ("  ", "deep"), (None, "fast"), ("q", "sources"), ("q", ""), ("q", None)):
            n = len(fake.requests)
            e = await refused(lore.ask(q, mode=mode))
            ck(f"ask refuses question={q!r} mode={mode!r} without a call", e is not None and len(fake.requests) == n, str(e))
        ck("every call so far carried the Bearer key", all(x.headers.get("authorization") == f"Bearer {KEY}" for x in fake.requests))

        # -- delete the repo ----------------------------------------------------------------
        ck("delete_repo returns nothing", await lore.delete_repo(repo["id"]) is None and repo["id"] not in fake.repos
           and fake.calls(route="delete_repo")[-1].method == "DELETE")
        e = await refused(lore.delete_repo(repo["id"]))
        ck("deleting an unknown repo is a 404", e is not None and e.status == 404)

        # -- failures in plain words --------------------------------------------------------
        for status, want in ((401, "refused this cell's key"), (403, "may not do that"), (404, "no such repo"),
                             (429, "too many requests"), (500, "had a problem (status 500"), (502, "git remote"),
                             (400, "refused the request (status 400: bad things")):
            fake.fail("list_repos", status, "git remote failed" if status == 502 else "bad things")
            e = await refused(lore.list_repos())
            ck(f"status {status} says it in words", e is not None and e.status == status and want in str(e) and "Traceback" not in str(e), str(e))
        fake.raise_next("sync_repo", httpx.ReadTimeout("read timed out"))
        e = await refused(lore.sync_repo("00000000-0000-0000-0000-000000000000"))
        ck("a sync timeout names the 600 seconds", e is not None and "within 600 seconds" in str(e), str(e))
        fake.raise_next("list_repos", httpx.ReadTimeout("read timed out"))
        e = await refused(lore.list_repos())
        ck("a timeout names the 10 seconds", e is not None and e.status is None and "within 10 seconds" in str(e), str(e))
        fake.raise_next("ask", httpx.ReadTimeout("read timed out"))
        e = await refused(lore.ask("q", mode="deep"))
        ck("a deep timeout names the 120 seconds", e is not None and "within 120 seconds" in str(e), str(e))
        fake.raise_next("list_repos", httpx.ConnectError("[Errno 111] connection refused to http://fake-lore.test"))
        e = await refused(lore.list_repos())
        ck("no connection says so", e is not None and e.status is None and str(e) == "Could not reach the brain.", str(e))

    # -- the replies that are not json ---------------------------------------------------------
    def odd(body, status=200):
        return LoreClient(fake_lore.BASE_URL, KEY, transport=httpx.MockTransport(lambda r: httpx.Response(status, content=body)))
    for body, label in ((b"<html>", "html"), (b"[1,2]", "a json list"), (b"null", "null")):
        async with odd(body) as c:
            e = await refused(c.list_repos())
            ck(f"a 200 with {label} is a plain error", e is not None and "cannot read" in str(e), str(e))
    for code in (301, 302, 307):
        async with odd(b"", code) as c:
            e = await refused(c.list_repos())
            ck(f"a {code} is an error that points at the url, not an empty success", e is not None and e.status == code and "TARES_BRAIN_URL" in str(e), str(e))
    ck("the client does not follow redirects", LoreClient(fake_lore.BASE_URL, KEY)._http.follow_redirects is False)
    async with LoreClient(fake_lore.BASE_URL, KEY, transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(ValueError(f"odd {KEY}")))) as c:
        e = await refused(c.list_repos())
        ck("an unexpected error inside a call is still a LoreError without the key", e is not None and KEY not in str(e) and KEY not in repr(e) and e.__cause__ is None, str(e))
    for odd_key in ("s\u00e9cret-" + "key", "tab\tkey", "new\nline"):
        try:
            LoreClient(fake_lore.BASE_URL, odd_key)
            ck(f"a key with {odd_key!r} is refused", False)
        except LoreError as e:
            ck(f"a key with {odd_key!r} is refused without echoing it", odd_key not in str(e) and odd_key not in repr(e) and "ASCII" in str(e), repr(e))
    async with odd(b"") as c:
        ck("an empty 200 is an empty dict", await c.list_repos() == {})
    async with odd(b"<html>proxy error</html>", 502) as c:
        e = await refused(c.list_repos())
        ck("a 502 html page is not copied into the message", e is not None and "proxy" not in str(e) and "502" in str(e), str(e))

    # -- the key and a token never appear ------------------------------------------------------
    leaky = fake_lore.FakeLore(KEY)
    leak_key_and_token = f"fetch failed for https://x-access-token:{TOKEN}@github.com/o/r.git using {KEY} and Bearer {KEY}"
    for route, call in (
        ("sync_repo", lambda c, rid: c.sync_repo(rid, token=TOKEN)),
        ("create_repo", lambda c, rid: c.create_repo("tares", remote_url=f"https://user:{TOKEN}@github.com/o/r.git")),
        ("ask", lambda c, rid: c.ask("q", mode="deep")),
        ("search", lambda c, rid: c.search("q")),
    ):
        async with leaky.client() as c:
            rid = (await c.create_repo("leak"))["id"]
            leaky.fail(route, 502, leak_key_and_token)
            e = await refused(call(c, rid))
            ck(f"{route}: lore's error text is scrubbed", e is not None and KEY not in str(e) and TOKEN not in str(e) and "x-access-token" not in str(e) and "[hidden]" in str(e), str(e))
            ck(f"{route}: nor does the exception hold them", KEY not in repr(e) and TOKEN not in repr(e) and KEY not in str(e.args) and e.__cause__ is None)
    async with leaky.client() as c:
        rid = (await c.create_repo("leak2"))["id"]
        leaky.fail("sync_repo", 400, f"bad token {TOKEN}")
        e = await refused(c.sync_repo(rid, token=TOKEN))
        ck("the token passed to a call is hidden wherever lore echoes it", TOKEN not in str(e) and "[hidden]" in str(e), str(e))
        leaky.fail("sync_repo", 400, f"bad token {PLAIN}")
        e = await refused(c.sync_repo(rid, token=PLAIN))
        ck("also a token with no known shape", PLAIN not in str(e), str(e))
        leaky.raise_next("sync_repo", httpx.ConnectError(f"cannot connect with {KEY} {TOKEN}"))
        e = await refused(c.sync_repo(rid, token=TOKEN))
        ck("a transport error never copies its own text", e is not None and KEY not in str(e) and TOKEN not in str(e))
    wrong = LoreClient(fake_lore.BASE_URL, WRONG, transport=httpx.MockTransport(leaky.handle))
    e = await refused(wrong.list_repos())
    ck("a wrong key is refused in words, without the key", e is not None and e.status == 401 and WRONG not in str(e) and "refused" in str(e), str(e))
    await wrong.aclose()
    logged = LOG.getvalue()
    ck("the log has the failures", "brain call" in logged and logged.count("\n") >= 10, logged[:200])
    ck("no log line holds the key or a token", KEY not in logged and TOKEN not in logged and PLAIN not in logged and WRONG not in logged, logged)
    shapes = ["gh" + "p_abcdef123456", "gh" + "o_abcdef123456", "github" + "_pat_11ABCDEF_xyz123", "https://me:pw@host/x"]   # gitleaks:allow
    out = L.scrub("a " + " ".join(shapes) + " b")
    ck("scrub hides the token shapes", not any(x in out for x in ("abcdef123456", "xyz123", "me:pw")), out)
    short = L.scrub("token gh" + "p_abcdefghij1234", ["abc"])   # gitleaks:allow
    ck("a short secret does not cut a token shape in two", "defghij" not in short and "1234" not in short, short)
    ck("scrub keeps plain text and handles None", L.scrub("repo not found") == "repo not found" and L.scrub(None) == "")

    # -- the fake itself: another test relies on these ----------------------------------------
    f2 = fake_lore.FakeLore()
    async with f2.client() as c:
        await c.create_repo("a")
        f2.fail("create_repo", 500, "boom")
        ck("fail() fires once", (await refused(c.create_repo("b"))) is not None and (await c.create_repo("b"))["name"] == "b")
        ck("requests are recorded with their route and status", [(x.route, x.status) for x in f2.requests] == [
            ("create_repo", 200), ("create_repo", 500), ("create_repo", 200)], str([(x.route, x.status) for x in f2.requests]))
    async with f2.client() as c:
        await c.put_documents("s", [{"external_id": "d", "title": "T", "text": "t"}])
        ck("a document's kind defaults to note as in lore", f2.documents[("s", "d")]["kind"] == "note")
        e = await refused(c.put_documents("s", [{"external_id": "e", "title": "T", "text": "t", "kind": "code"}]))
        ck("the fake refuses a document kind lore refuses", e is not None and e.status == 400 and "note or doc" in str(e), str(e))
        e = await refused(c.create_repo("cred", remote_url="https://me:pw@github.com/o/r.git"))
        ck("the fake refuses a remote url with credentials, as lore does", e is not None and e.status == 400 and "pw" not in str(e), str(e))
        res = await c.search("t", k=-1)
        ck("a negative k still returns hits", len(res["hits"]) == 1 and {"chunk_id", "path", "start_line", "end_line", "symbols"} <= set(res["hits"][0]), str(res))
    nokey = httpx.Client(transport=httpx.MockTransport(f2.handle), base_url=fake_lore.BASE_URL)
    ck("the fake answers 401 without the key", nokey.get("/v1/repos").status_code == 401 and nokey.get("/v1/nope", headers={"Authorization": f"Bearer {f2.key}"}).status_code == 404)

asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
