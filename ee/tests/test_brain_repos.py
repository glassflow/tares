# Tares enterprise licence, see ee/LICENSE
"""The repos a cell connects to the brain (TR-1197): the brain_repos table and the routes under
/api/brain. A licensed cell with the brain settings connects, lists, retries and removes repos
(against the shared fake lore, never a real one); a licensed cell without the settings says
configured:false and 404s the rest; an unlicensed cell 404s every route as if it were not mounted.

In process, no ports. With CI's Postgres run (TARES_TEST_PG and tests/pg_harness on PYTHONPATH) the
same script keeps its state in Postgres.

Run: .venv/bin/python ee/tests/test_brain_repos.py
"""
import asyncio, os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings, puts the repo, ee/ and tests/ on sys.path
licensed.install_test_root()

TMP = tempfile.mkdtemp(prefix="tares-brain-repos-")
ROOT = "root-token-" + "brain-repos"
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
AUTH_OFF = "--auth-off" in sys.argv   # the second run, started by the first: auth is read at import
if not AUTH_OFF:
    os.environ["TARES_AUTH_TOKEN"] = ROOT

import httpx
from fastapi import FastAPI

import fake_lore
from tares import github_credentials
from tares_ee.brain import repos as repos_mod
from tares_ee.brain.lore import LoreClient

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


BRAIN_KEY = "brain-" + "key-test-1"   # gitleaks:allow
SHAPE = {"repo", "state", "files_done", "files_total", "indexed_commit", "last_synced", "error",
         "added_by", "added_by_name", "added_at"}
NOT_FOUND = {"detail": "Not Found"}
ALL = (("GET", "/api/brain/repos/available", None),
       ("POST", "/api/brain/repos", {"repos": ["acme/web"]}),
       ("DELETE", "/api/brain/repos/acme/web", None),
       ("POST", "/api/brain/repos/acme/web/retry", None))


def unit():
    print("== the store: the same on DuckDB and Postgres ==")
    from tares.store import Store
    s = Store(os.path.join(TMP, "unit.duckdb"))
    who = s.create_person("Kiran", "kiran@x.dev")
    ck("a new cell has no brain repos", s.list_brain_repos() == [])
    ck("add a repo", s.add_brain_repo("acme/web", "src-1", who["id"], "2026-01-01T00:00:00Z"))
    ck("the same repo twice is refused, the row stays",
       not s.add_brain_repo("acme/web", "src-2", None, "2026-02-01T00:00:00Z")
       and s.get_brain_repo("acme/web")["repo_source_id"] == "src-1")
    r = s.get_brain_repo("acme/web")
    ck("a new row is queued, nothing indexed", (r["state"], r["files_done"], r["files_total"],
       r["indexed_commit"], r["last_synced"], r["error"]) == ("queued", 0, 0, None, None, None), str(r))
    ck("the row names who added it", (r["added_by"], r["added_by_name"]) == (who["id"], "Kiran"), str(r))
    ck("an unknown repo is not there", s.get_brain_repo("acme/none") is None)
    ck("a queued repo is not requeued", not s.requeue_brain_repo("acme/web"))
    s.sc.execute("UPDATE brain_repos SET state = 'failed', error = 'boom', files_done = 3 WHERE repo = ?",
                 ["acme/web"])
    ck("a failed repo is requeued", s.requeue_brain_repo("acme/web"))
    r = s.get_brain_repo("acme/web")
    ck("... the error is cleared, the counts stay", (r["state"], r["error"], r["files_done"]) == ("queued", None, 3), str(r))
    ck("requeue of a repo that is not there is False", not s.requeue_brain_repo("acme/none"))
    ck("delete removes it", s.delete_brain_repo("acme/web") and s.list_brain_repos() == [])
    ck("delete of a repo that is not there is False", not s.delete_brain_repo("acme/web"))
    s.add_brain_repo("acme/nobody", None, None, "2026-01-01T00:00:00Z")
    r = s.get_brain_repo("acme/nobody")
    ck("no person and no source id are null", (r["added_by"], r["added_by_name"], r["repo_source_id"]) == (None, None, None), str(r))

    print("== the repo name check ==")
    ck("Owner/Name is stored in lowercase", repos_mod.check_repo(" Acme/Web ") == "acme/web")
    ck("a pasted GitHub address is the repo", repos_mod.check_repo("https://github.com/Acme/Web.git") == "acme/web")
    for bad in ("web", "a/b/c", "", None, 5, ["a/b"], "acme/..", "../web", "acme/.", "https://gitlab.com/a/b",
                "acme/" + "x" * 101, "a_b/c", "acme/.git", "a.b/c"):
        try:
            repos_mod.check_repo(bad)
            ok = False
        except ValueError:
            ok = True
        ck(f"check_repo refuses {bad!r}"[:70], ok)
    ck("a .git ending on the name is dropped", repos_mod.check_repo("Acme/Web.git") == "acme/web")
    ck("the lore name has no slash", repos_mod.lore_name("acme/web") == "acme_web")


async def main():
    from tares.daemon import make_app
    lore = fake_lore.FakeLore()
    repos_mod.lore_client = lore.client      # every route talks to the fake
    app = make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=H(ROOT))
        store = app.state.store

        async def licence(text):
            r = await cx.put("/api/licence", json={"key": text})
            ck("the licence is stored", r.status_code == 200, r.text[:200])

        def is_404(r):
            return r.status_code == 404 and r.json() == NOT_FOUND

        async def all_404(why, get_too=True):
            if get_too:
                r = await cx.get("/api/brain")
                ck(f"{why}: GET /api/brain is 404, as an unmounted route", is_404(r), f"{r.status_code} {r.text[:100]}")
            for method, path, body in ALL:
                r = await cx.request(method, path, json=body)
                ck(f"{why}: {method} {path} is 404", is_404(r), f"{r.status_code} {r.text[:100]}")
            ck(f"{why}: nothing stored, lore never called", store.list_brain_repos() == [] and not lore.requests,
               str(store.list_brain_repos()))

        print("== no licence ==")
        os.environ["TARES_BRAIN_URL"] = fake_lore.BASE_URL
        os.environ["TARES_BRAIN_KEY"] = BRAIN_KEY
        await all_404("no licence, brain settings present")
        await licence(licensed.test_licence(features=("other",)))
        await all_404("licence without the enterprise feature")

        print("== licensed, no brain settings ==")
        await licence(licensed.test_licence())
        for var in ("TARES_BRAIN_URL", "TARES_BRAIN_KEY"):
            saved = os.environ.pop(var)
            r = await cx.get("/api/brain")
            ck(f"{var} missing: configured false, no repos", r.status_code == 200 and r.json() == {
                "configured": False, "repos": []}, f"{r.status_code} {r.text[:100]}")
            os.environ[var] = saved
        os.environ.pop("TARES_BRAIN_KEY")
        os.environ["TARES_BRAIN_URL"] = "  "
        r = await cx.get("/api/brain")
        ck("a blank URL is not configured", r.json() == {"configured": False, "repos": []}, r.text)
        os.environ.pop("TARES_BRAIN_URL")
        await all_404("licensed, brain not configured", get_too=False)
        r = await cx.get("/api/brain")
        ck("... and GET /api/brain still says configured:false", r.status_code == 200 and r.json()["configured"] is False, r.text)
        os.environ["TARES_BRAIN_URL"] = fake_lore.BASE_URL
        os.environ["TARES_BRAIN_KEY"] = BRAIN_KEY

        print("== licensed and configured: connect and list ==")
        r = await cx.get("/api/brain")
        ck("configured, no repos yet", r.status_code == 200 and r.json() == {"configured": True, "repos": []}, r.text)
        r = await cx.post("/api/brain/repos", json={"repos": ["Acme/Web"]})
        body = r.json() if r.status_code == 201 else {}
        ck("connect answers 201 with the new row", r.status_code == 201 and len(body.get("repos", [])) == 1
           and set(body["repos"][0]) == SHAPE, f"{r.status_code} {r.text[:300]}")
        row = body["repos"][0] if body.get("repos") else {}
        ck("the repo is lowercase, queued, nothing indexed", (row.get("repo"), row.get("state"), row.get("files_done"),
           row.get("files_total"), row.get("indexed_commit"), row.get("last_synced"), row.get("error"))
           == ("acme/web", "queued", 0, 0, None, None, None), str(row))
        ck("added_at is set", bool(row.get("added_at")), str(row))
        made = lore.calls("POST", "create_repo")
        ck("one lore repo source was created, named for owner and name, with the https remote",
           len(made) == 1 and made[0].json == {"name": "acme_web", "remote_url": "https://github.com/acme/web.git"},
           str([m.json for m in made]))
        ck("nothing was indexed: no sync call", not lore.calls("POST", "sync_repo"))
        stored = store.get_brain_repo("acme/web") or {}
        ck("the row keeps lore's repo id", stored.get("repo_source_id") == next(iter(lore.repos)), str(stored))
        r = await cx.get("/api/brain")
        j = r.json()
        ck("GET lists it, in the same shape, without the source id", j["configured"] is True and [x["repo"] for x in j["repos"]]
           == ["acme/web"] and set(j["repos"][0]) == SHAPE and "repo_source_id" not in j["repos"][0], r.text[:300])
        r = await cx.post("/api/brain/repos", json={"repos": ["acme/api", "https://github.com/Acme/Docs"]})
        ck("two at once, a pasted address too", r.status_code == 201 and [x["repo"] for x in r.json()["repos"]]
           == ["acme/api", "acme/docs"], r.text[:300])
        ck("all three are listed, oldest first", [x["repo"] for x in (await cx.get("/api/brain")).json()["repos"]]
           == ["acme/web", "acme/api", "acme/docs"])
        ck("every lore call carried the cell's brain key (lore answered none with 401)", all(m.status == 200 for m in lore.requests),
           str([m.status for m in lore.requests]))

        print("== refused input ==")
        before = (len(store.list_brain_repos()), len(lore.requests))

        async def refuse(label, payload, code=400, words=None):
            r = await cx.post("/api/brain/repos", json=payload)
            text = r.text
            ck(label, r.status_code == code and (words is None or words in text), f"{r.status_code} {text[:200]}")
            ck("  ... nothing stored and lore not called", (len(store.list_brain_repos()), len(lore.requests)) == before)

        await refuse("empty list", {"repos": []}, words="repos: a list")
        await refuse("repos missing", {}, words="repos: a list")
        await refuse("repos null", {"repos": None}, words="repos: a list")
        await refuse("repos is text", {"repos": "acme/new"}, words="repos: a list")
        await refuse("an unknown field", {"repos": ["acme/new"], "force": True}, words="unknown field 'force'")
        await refuse("not owner/name", {"repos": ["web"]}, words="owner/name")
        await refuse("too many parts", {"repos": ["a/b/c"]}, words="owner/name")
        await refuse("a number", {"repos": [5]}, words="owner/name")
        await refuse("a null entry", {"repos": [None]}, words="owner/name")
        await refuse("an empty entry", {"repos": [""]}, words="owner/name")
        await refuse("a dot part", {"repos": ["acme/.."]}, words="owner/name")
        await refuse("a repo that is not on GitHub", {"repos": ["https://gitlab.com/a/b"]}, words="owner/name")
        await refuse("the same repo twice, in another case", {"repos": ["acme/new", "Acme/New"]}, words="acme/new is in the list twice")
        await refuse("one bad entry connects none of the good ones", {"repos": ["acme/good", "bad"]}, words="owner/name")
        await refuse("too many at once", {"repos": [f"acme/r{i}" for i in range(repos_mod.MAX_CONNECT + 1)]}, words="at most")
        await refuse("already connected", {"repos": ["acme/fresh", "ACME/WEB"]}, 409, words="already connected: acme/web")
        r = await cx.post("/api/brain/repos", json=["acme/new"])
        ck("a body that is not an object is refused (422)", r.status_code == 422, f"{r.status_code}")
        ck("... nothing stored", len(store.list_brain_repos()) == before[0])

        print("== lore fails ==")
        lore.fail("create_repo", 500, "disk full")
        r = await cx.post("/api/brain/repos", json={"repos": ["acme/one"]})
        ck("a lore failure is 502 in plain words, and stores nothing", r.status_code == 502 and "Could not connect acme/one" in r.text
           and "The brain had a problem" in r.text and store.get_brain_repo("acme/one") is None, f"{r.status_code} {r.text[:300]}")
        ck("... the lore key is not in the answer", BRAIN_KEY not in r.text and lore.key not in r.text)
        real = lore.handle
        calls = {"n": 0}

        def second_fails(request):
            calls["n"] += 1
            if calls["n"] == 2:
                return httpx.Response(502, json={"error": "upstream"})
            return real(request)
        repos_mod.lore_client = lambda: LoreClient(fake_lore.BASE_URL, lore.key,
                                                   transport=httpx.MockTransport(second_fails))
        r = await cx.post("/api/brain/repos", json={"repos": ["acme/p1", "acme/p2", "acme/p3"]})
        ck("the second of three fails: 502 naming it and what was kept", r.status_code == 502 and "Could not connect acme/p2" in r.text
           and "Connected before it failed: acme/p1" in r.text, f"{r.status_code} {r.text[:300]}")
        ck("... the first is stored, the others are not", store.get_brain_repo("acme/p1") is not None
           and store.get_brain_repo("acme/p2") is None and store.get_brain_repo("acme/p3") is None)
        repos_mod.lore_client = lambda: LoreClient(fake_lore.BASE_URL, lore.key, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"name": "acme_p9"})))
        r = await cx.post("/api/brain/repos", json={"repos": ["acme/p9"]})
        ck("lore answering without a repo id is 502 and stores nothing", r.status_code == 502 and "did not say which repo" in r.text
           and store.get_brain_repo("acme/p9") is None, f"{r.status_code} {r.text[:300]}")
        ck("... and the failure names the repo it was on", "Could not connect acme/p9" in r.text, r.text[:300])
        repos_mod.lore_client = lore.client
        r = await cx.post("/api/brain/repos", json={"repos": ["acme/p2"]})
        ck("it can be connected again", r.status_code == 201, r.text[:200])
        lore.fail("create_repo", 401, "unauthorized")
        r = await cx.post("/api/brain/repos", json={"repos": ["acme/p4"]})
        ck("lore refusing the key says so, without the key", r.status_code == 502 and "refused this cell's key" in r.text
           and BRAIN_KEY not in r.text, r.text[:300])
        os.environ["TARES_BRAIN_KEY"] = "bad key é"
        repos_mod.lore_client = LoreClient.from_env
        r = await cx.post("/api/brain/repos", json={"repos": ["acme/p5"]})
        ck("a key lore cannot be sent is 502 in plain words", r.status_code == 502 and "plain printable ASCII" in r.text, r.text[:300])
        os.environ["TARES_BRAIN_KEY"] = BRAIN_KEY
        repos_mod.lore_client = lore.client
        for p in ("acme/p1", "acme/p2"):
            store.delete_brain_repo(p)

        print("== retry ==")
        def set_state(repo, state, error=None):
            store.sc.execute("UPDATE brain_repos SET state = ?, error = ? WHERE repo = ?", [state, error, repo])

        r = await cx.post("/api/brain/repos/acme/web/retry")
        ck("a queued repo is refused (409) in plain words", r.status_code == 409 and "acme/web is queued" in r.text
           and "only a repo whose index failed" in r.text, f"{r.status_code} {r.text[:200]}")
        for st in ("indexing", "indexed"):
            set_state("acme/web", st)
            r = await cx.post("/api/brain/repos/acme/web/retry")
            ck(f"a repo that is {st} is refused", r.status_code == 409 and f"acme/web is {st}" in r.text, f"{r.status_code} {r.text[:200]}")
            ck("  ... and left as it was", store.get_brain_repo("acme/web")["state"] == st)
        set_state("acme/web", "failed", "lore ran out of memory")
        r = await cx.post("/api/brain/repos/Acme/WEB/retry")
        ck("a failed repo goes back to queued, error cleared (any case in the path)", r.status_code == 200
           and r.json()["state"] == "queued" and r.json()["error"] is None and set(r.json()) == SHAPE, f"{r.status_code} {r.text[:300]}")
        ck("... in the table too, and no index was started", store.get_brain_repo("acme/web")["state"] == "queued"
           and store.get_brain_repo("acme/web")["error"] is None and not lore.calls("POST", "sync_repo"))
        r = await cx.post("/api/brain/repos/acme/ghost/retry")
        ck("a repo that is not connected is 404", r.status_code == 404 and "not connected" in r.text, f"{r.status_code} {r.text[:200]}")
        r = await cx.post("/api/brain/repos/acme/bad%20name/retry")
        ck("a name that is not a repo name is 400", r.status_code == 400 and "owner/name" in r.text, f"{r.status_code} {r.text[:200]}")

        print("== delete ==")
        r = await cx.delete("/api/brain/repos/Acme/Docs")
        ck("delete removes the row (any case in the path)", r.status_code == 200 and r.json() == {"ok": True, "removed": True}
           and store.get_brain_repo("acme/docs") is None, f"{r.status_code} {r.text}")
        ck("the others stay", store.get_brain_repo("acme/web") is not None and store.get_brain_repo("acme/api") is not None)
        ck("lore was not asked to forget it", not lore.calls("DELETE", "delete_repo") and len(lore.repos) >= 1)
        r = await cx.delete("/api/brain/repos/acme/docs")
        ck("deleting it again is 404", r.status_code == 404 and "not connected" in r.text, f"{r.status_code} {r.text[:200]}")
        r = await cx.delete("/api/brain/repos/acme/bad%20name")
        ck("a bad name is 400", r.status_code == 400, f"{r.status_code} {r.text[:200]}")
        r = await cx.post("/api/brain/repos", json={"repos": ["acme/docs"]})
        ck("a deleted repo can be connected again", r.status_code == 201, r.text[:200])

        print("== available ==")
        listed = []

        async def fake_list(cred, query=""):
            if cred["name"] == "broken":
                raise ValueError("GitHub rejected the token (401 unauthorized)")
            return listed
        real_list = github_credentials.list_repos_for
        github_credentials.list_repos_for = fake_list
        try:
            r = await cx.get("/api/brain/repos/available")
            ck("no GitHub connection: github false, no repos", r.status_code == 200 and r.json() == {"github": False, "repos": []}, r.text)
            store.upsert_github_credential("main", "x" * 10 + "tok")
            listed.extend([{"full_name": "Acme/Web", "private": True}, {"full_name": "acme/zed", "private": False},
                           {"full_name": "acme/Alpha"}, {"full_name": ""}, {"private": True}, {"full_name": "acme/zed"}])
            r = await cx.get("/api/brain/repos/available")
            j = r.json()
            ck("every readable repo, sorted, lowercase, no blanks or repeats",
               [x["repo"] for x in j["repos"]] == ["acme/alpha", "acme/web", "acme/zed"] and j["github"] is True, r.text[:300])
            ck("each entry is {repo, private, connected} and nothing else", all(set(x) == {"repo", "private", "connected"} for x in j["repos"]))
            ck("connected marks the repos already on the brain", {x["repo"]: x["connected"] for x in j["repos"]}
               == {"acme/alpha": False, "acme/web": True, "acme/zed": False}, r.text[:300])
            ck("private comes from GitHub, missing means false", {x["repo"]: x["private"] for x in j["repos"]}
               == {"acme/alpha": False, "acme/web": True, "acme/zed": False}, r.text[:300])
            store.upsert_github_credential("broken", "y" * 10 + "tok")
            r = await cx.get("/api/brain/repos/available")
            ck("one connection failing does not hide the repos of another", r.status_code == 200 and len(r.json()["repos"]) == 3, r.text[:200])
            listed.clear()
            r = await cx.get("/api/brain/repos/available")
            ck("an empty list is an empty list", r.status_code == 200 and r.json()["repos"] == [] and r.json()["github"] is True, r.text[:200])
            store.delete_github_credential("main")
            r = await cx.get("/api/brain/repos/available")
            ck("every connection failing is 502 with GitHub's words", r.status_code == 502 and "GitHub rejected the token" in r.text,
               f"{r.status_code} {r.text[:200]}")
            store.delete_github_credential("broken")
        finally:
            github_credentials.list_repos_for = real_list

        print("== who may change it ==")
        async def key(scopes, person=None):
            body = {"name": "k-" + "-".join(scopes), "scopes": scopes}
            if person:
                body["person_id"] = person
            r = await cx.post("/api/keys", json=body)
            return H(r.json()["secret"])
        pid = (await cx.post("/api/people", json={"name": "Jo", "email": "jo@x.dev"})).json()["id"]
        reader, writer, admin = await key(["read"], pid), await key(["read", "ingest"], pid), await key(["admin"], pid)
        n = len(store.list_brain_repos())
        for who, tok in (("a read key", reader), ("a read and ingest key", writer)):
            r = await cx.get("/api/brain", headers=tok)
            ck(f"{who} can read /api/brain", r.status_code == 200 and r.json()["configured"] is True, r.text[:100])
            r = await cx.get("/api/brain/repos/available", headers=tok)
            ck(f"{who} can read the available repos", r.status_code == 200, f"{r.status_code}")
            for method, path, body in (("POST", "/api/brain/repos", {"repos": ["acme/mine"]}),
                                       ("DELETE", "/api/brain/repos/acme/web", None),
                                       ("POST", "/api/brain/repos/acme/web/retry", None)):
                r = await cx.request(method, path, json=body, headers=tok)
                ck(f"{who}: {method} {path} is refused (403)", r.status_code == 403, f"{r.status_code} {r.text[:100]}")
        set_state("acme/api", "failed", "x")
        ck("nothing changed", len(store.list_brain_repos()) == n and store.get_brain_repo("acme/web") is not None
           and store.get_brain_repo("acme/mine") is None)
        r = await cx.post("/api/brain/repos", json={"repos": ["acme/mine"]}, headers=admin)
        ck("an admin key connects a repo, and the row names the person on the key", r.status_code == 201
           and r.json()["repos"][0]["added_by"] == pid and r.json()["repos"][0]["added_by_name"] == "Jo", r.text[:300])
        r = await cx.post("/api/brain/repos/acme/api/retry", headers=admin)
        ck("an admin key retries", r.status_code == 200 and r.json()["state"] == "queued", r.text[:200])
        r = await cx.delete("/api/brain/repos/acme/mine", headers=admin)
        ck("an admin key removes", r.status_code == 200 and store.get_brain_repo("acme/mine") is None, r.text[:200])
        r = await cx.post("/api/brain/repos", json={"repos": ["acme/rootset"]})
        ck("the root token (no person) connects, added_by is null", r.status_code == 201 and r.json()["repos"][0]["added_by"] is None
           and r.json()["repos"][0]["added_by_name"] is None, r.text[:300])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as bare_cx:
            r = await bare_cx.get("/api/brain")
        ck("no credential at all is refused by the daemon", r.status_code == 401, f"{r.status_code}")

        print("== the handlers check admin themselves ==")
        # the daemon's guard already refuses a non-admin write; the handler must not rely on it
        class Host:
            def __init__(self, admin):
                self.store, self._admin = store, admin
            def is_admin(self, request): return self._admin
            def require_admin(self, request):
                if not self._admin:
                    self.err(PermissionError("this needs an admin"), 403)
            def current_person(self, request): return None
            def err(self, e, code=400):
                from fastapi import HTTPException
                raise HTTPException(status_code=code, detail=str(e))
        for admin_on in (False, True):
            bare = FastAPI()
            repos_mod.register_routes(bare, Host(admin_on))
            bx = httpx.AsyncClient(transport=httpx.ASGITransport(app=bare), base_url="http://b")
            n = len(store.list_brain_repos())
            codes = [(await bx.request(m, p, json=b)).status_code for m, p, b in (
                ("POST", "/api/brain/repos", {"repos": ["acme/bare"]}), ("DELETE", "/api/brain/repos/acme/bare", None),
                ("POST", "/api/brain/repos/acme/web/retry", None))]
            if admin_on:
                ck("an admin host: connect, delete and retry go through to their own answers", codes == [201, 200, 409], str(codes))
            else:
                ck("a non-admin host: all three are 403", codes == [403, 403, 403], str(codes))
                ck("... nothing stored", len(store.list_brain_repos()) == n)
            await bx.aclose()

        print("== the licence goes away ==")
        r = await cx.delete("/api/licence")
        ck("the licence is removed", r.status_code == 200, r.text[:200])
        r = await cx.get("/api/brain")
        ck("the routes are 404 again", is_404(r), f"{r.status_code} {r.text[:100]}")
        await cx.aclose()


async def auth_off():
    """No TARES_AUTH_TOKEN: everyone is the admin, as on the other ee routes."""
    os.environ["TARES_DB"] = os.path.join(TMP, "off.duckdb")
    os.environ["TARES_LICENSE"] = licensed.test_licence()
    from tares.daemon import make_app
    lore = fake_lore.FakeLore()
    repos_mod.lore_client = lore.client
    app = make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
        print("== auth off ==")
        r = await cx.post("/api/brain/repos", json={"repos": ["acme/web"]})
        ck("connect needs no key; added_by is the local person", r.status_code == 201
           and r.json()["repos"][0]["added_by"] == "local", f"{r.status_code} {r.text[:200]}")
        r = await cx.post("/api/brain/repos/acme/web/retry")
        ck("retry on a queued repo is 409, not 403", r.status_code == 409, f"{r.status_code}")
        r = await cx.delete("/api/brain/repos/acme/web")
        ck("delete needs no key", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        await cx.aclose()


if AUTH_OFF:
    asyncio.run(auth_off())
else:
    unit()
    os.environ["TARES_LICENSE"] = ""
    asyncio.run(main())
    import subprocess
    env = {k: v for k, v in os.environ.items() if k not in ("TARES_AUTH_TOKEN", "TARES_LICENSE")}
    off = subprocess.run([sys.executable, os.path.abspath(__file__), "--auth-off"], env=env)
    F += 1 if off.returncode else 0
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
