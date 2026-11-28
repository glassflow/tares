# Tares enterprise licence, see ee/LICENSE
"""The decision folders of a connected repo (TR-1209): the brain_repo_folders table, the suggestions
read from the repo's git tree, GET and PUT /api/brain/repos/{owner}/{name}/folders, and `folders` on connect.

Against the shared fake lore and fake control plane and a fake GitHub tree (never a real one); the
background poll that follows an index is switched off. In process, no ports. With CI's Postgres run
(TARES_TEST_PG and tests/pg_harness on PYTHONPATH) the same script keeps its state in Postgres.

Run: .venv/bin/python ee/tests/test_brain_folders.py
"""
import asyncio, os, sys, tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings, puts the repo, ee/ and tests/ on sys.path
licensed.install_test_root()

TMP = tempfile.mkdtemp(prefix="tares-brain-folders-")
ROOT = "root-token-" + "brain-folders"
os.environ["TARES_HOME"] = os.path.join(TMP, "t")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_AUTH_TOKEN"] = ROOT

import httpx

import fake_cp
import fake_lore
from tares import github_credentials
from tares_ee.brain import decisions_repo as decisions_repo_mod
from tares_ee.brain import folders as folders_mod
from tares_ee.brain import index as index_mod
from tares_ee.brain import repos as repos_mod

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


BRAIN_KEY = "brain-" + "key-test-1"   # gitleaks:allow


def blob(path):
    return {"path": path, "type": "blob"}


def tree(path):
    return {"path": path, "type": "tree"}


def make_tree():
    """A repo with docs/adr (4 markdown files, one nested, plus a text file), a numbered folder that is
    not a known name, and folders that must not be suggested."""
    e = [tree("docs"), tree("docs/adr"), tree("docs/adr/archive"), tree("design"), tree("design/records"),
         tree("misc"), tree("notes"), tree("decisions"), tree("src")]
    e += [blob("README.md"), blob("src/main.py")]
    e += [blob("docs/adr/0001-use-postgres.md"), blob("docs/adr/0002-queue.md"), blob("docs/adr/README.md"),
          blob("docs/adr/archive/0003-old.md"), blob("docs/adr/template.txt")]
    e += [blob("design/records/0001-api.md"), blob("design/records/0002-auth.md"), blob("design/records/notes.md")]
    e += [blob("misc/0001-one.md"), blob("misc/a.md"), blob("misc/b.md"), blob("misc/c.md")]   # one of four: not most
    e += [blob("notes/0001-lonely.md")]                                                          # one file: too few
    e += [blob("decisions/readme.txt")]                                                          # known name, no markdown
    return e


def day(month, of=1):
    return datetime(2026, month, of, tzinfo=timezone.utc)


def unit():
    print("== folder paths ==")
    ck("a path loses its slashes", folders_mod.check_folder(" /docs/adr/ ") == "docs/adr")
    for bad in ("", "/", None, 5, ["a"], "..", "a/../b", "./a", "a//b", "a\\b", "x" * 201, "a\x00b", "a\nb", "."):
        try:
            folders_mod.check_folder(bad)
            ok = False
        except ValueError:
            ok = True
        ck(f"check_folder refuses {bad!r}"[:60], ok)
    ck("a list is checked in order", folders_mod.check_folders(["b", "/a/"]) == ["b", "a"])
    ck("an empty list is fine", folders_mod.check_folders([]) == [])
    for bad, words in (("docs/adr", "a list"), (None, "a list"), (["a", "a/"], "twice"), ([None], "not a folder"),
                       ([f"f{i}" for i in range(folders_mod.MAX_FOLDERS + 1)], "at most")):
        try:
            folders_mod.check_folders(bad)
            ok = False
        except ValueError as e:
            ok = words in str(e)
        ck(f"check_folders refuses {str(bad)[:30]!r} in plain words", ok)

    print("== suggestions from a tree ==")
    got = folders_mod.suggest(make_tree())
    ck("docs/adr is suggested with all its markdown files, nested and README included", {"path": "docs/adr", "files": 4, "why": "known"} in got, str(got))
    ck("a numbered folder is suggested with its count", {"path": "design/records", "files": 3, "why": "numbered"} in got, str(got))
    ck("known names come first, then numbered ones", [s["why"] for s in got] == ["known", "numbered"], str(got))
    ck("nothing else: not one of four, not a lone file, not a known folder without markdown",
       [s["path"] for s in got] == ["docs/adr", "design/records"], str(got))
    ck("the text file is not counted", folders_mod.count_files(make_tree(), "docs/adr") == 4)
    ck("an empty tree suggests nothing", folders_mod.suggest([]) == [])
    both = folders_mod.suggest([tree("ADR"), blob("ADR/0001-a.md"), tree("docs"), tree("docs/decisions"), blob("docs/decisions/x.mdx")])
    ck("a known name matches in any case and keeps the repo's spelling; .mdx counts",
       both == [{"path": "docs/decisions", "files": 1, "why": "known"}, {"path": "ADR", "files": 1, "why": "known"}], str(both))
    inside = folders_mod.suggest([tree("docs/adr"), blob("docs/adr/0001-a.md"), blob("docs/adr/0002-b.md"),
                                  blob("docs/adr/old/0001-a.md"), blob("docs/adr/old/0002-b.md")])
    ck("a numbered folder inside a suggested one is not suggested again", [s["path"] for s in inside] == ["docs/adr"], str(inside))
    only = folders_mod.suggest([blob("svc/a/adr/0001-a.md"), blob("svc/a/adr/0002-b.md")])
    ck("a numbered folder deep in a monorepo is found without a tree entry", only == [{"path": "svc/a/adr", "files": 2, "why": "numbered"}], str(only))
    cut = folders_mod.suggest(make_tree(), truncated=True)
    ck("a truncated tree looks only at the known names", [s["path"] for s in cut] == ["docs/adr"], str(cut))
    many = [blob(f"s{i:03d}/0001-a.md") for i in range(40)] + [blob(f"s{i:03d}/0002-b.md") for i in range(40)]
    ck("a monorepo's suggestions are capped", len(folders_mod.suggest(many)) == folders_mod.MAX_SUGGESTED)

    print("== the store: the same on DuckDB and Postgres ==")
    from tares.store import Store
    s = Store(os.path.join(tempfile.mkdtemp(dir=TMP), "unit"))   # its own folder: the daemon's session store is one file per data folder
    who = s.create_person("Kiran", "kiran@x.dev")
    s.add_brain_repo("acme/web", "src-1", None, day(1, 1))
    s.add_brain_repo("acme/api", "src-2", None, day(1, 1))
    ck("a new repo has no folders", s.list_brain_folders("acme/web") == [])
    ck("choose two: both added, none removed", s.set_brain_folders("acme/web", ["docs/adr", "adr"], who["id"], day(1, 2))
       == (["adr", "docs/adr"], []))
    rows = s.list_brain_folders("acme/web")
    ck("each folder keeps path, who chose it and when, by path",
       [(r["path"], r["chosen_by"], r["chosen_by_name"]) for r in rows] == [("adr", who["id"], "Kiran"), ("docs/adr", who["id"], "Kiran")]
       and all(r["chosen_at"] for r in rows), str(rows))
    ck("another repo is not touched", s.list_brain_folders("acme/api") == [])
    ck("change: one dropped, one added, one kept", s.set_brain_folders("acme/web", ["docs/adr", "decisions"], None, day(2, 1))
       == (["decisions"], ["adr"]))
    rows = {r["path"]: r for r in s.list_brain_folders("acme/web")}
    ck("a folder that stayed keeps its chooser and date; the new one has none", rows["docs/adr"]["chosen_by"] == who["id"]
       and str(rows["docs/adr"]["chosen_at"]).startswith("2026-01-02") and rows["decisions"]["chosen_by"] is None
       and rows["decisions"]["chosen_by_name"] is None, str(rows))
    ck("the same list again changes nothing", s.set_brain_folders("acme/web", ["decisions", "docs/adr"], None, day(3, 1)) == ([], []))
    ck("deleting the repo deletes its folders", s.delete_brain_repo("acme/web") and s.list_brain_folders("acme/web") == [])
    ck("an empty list clears", s.set_brain_folders("acme/api", ["a"], None, day(1, 1)) == (["a"], [])
       and s.set_brain_folders("acme/api", [], None, day(1, 1)) == ([], ["a"]) and s.list_brain_folders("acme/api") == [])

    print("== the git trees call ==")
    seen = []
    answer = {"status": 200, "json": {"tree": [{"path": "docs", "type": "tree", "mode": "040000", "sha": "x"},
                                              {"path": "docs/adr/0001-a.md", "type": "blob", "sha": "y"},
                                              {"type": "blob"}, "junk"], "truncated": False}}

    def handler(request):
        seen.append(request)
        return httpx.Response(answer["status"], json=answer["json"])
    real = httpx.AsyncClient

    class Shim:
        AsyncClient = staticmethod(lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    saved = github_credentials.httpx
    github_credentials.httpx = Shim
    try:
        got = asyncio.run(github_credentials.git_tree("tok-1", "acme/web"))
        ck("one call, to HEAD with recursive=1, with the token", len(seen) == 1 and seen[0].url.path == "/repos/acme/web/git/trees/HEAD"
           and seen[0].url.params["recursive"] == "1" and seen[0].headers["authorization"] == "Bearer tok-1", str(seen))
        ck("entries keep path and type; rows without a path are dropped", got == {"entries": [{"path": "docs", "type": "tree"},
           {"path": "docs/adr/0001-a.md", "type": "blob"}], "truncated": False}, str(got))
        answer["json"] = {"tree": [], "truncated": True}
        ck("truncated is passed on, and nothing is paged", asyncio.run(github_credentials.git_tree("t", "acme/web"))["truncated"] is True
           and len(seen) == 2)
        answer["status"] = 409
        ck("an empty repo (409) is an empty tree", asyncio.run(github_credentials.git_tree("t", "acme/web")) == {"entries": [], "truncated": False})
        for status, words in ((404, "cannot read it"), (401, "rejected the token"), (500, "returned 500")):
            answer["status"] = status
            try:
                asyncio.run(github_credentials.git_tree("t", "acme/web"))
                ok = False
            except ValueError as e:
                ok = words in str(e)
            ck(f"GitHub {status} is a ValueError in plain words", ok)
    finally:
        github_credentials.httpx = saved


async def main():
    from tares.daemon import make_app
    lore = fake_lore.FakeLore()
    cp = fake_cp.FakeCp()
    cp.start_as = ("queued", 2)
    repos_mod.lore_client = lore.client
    index_mod.cp_client = cp.client

    async def never(self):
        await asyncio.Event().wait()
    index_mod.Indexer.run = never
    names = ("web", "api", "docs", "two", "three", "empty", "cut", "down", "gone", "bad", "nope")
    readable = [{"full_name": f"acme/{n}"} for n in names]

    async def readable_repos(cred, query=""):
        return readable
    github_credentials.list_repos_for = readable_repos

    trees = {}   # repo -> what the fake GitHub answers for its tree, or an exception to raise
    asked = []

    async def fake_tree(token, repo, ref="HEAD", api_url=None):
        asked.append(repo)
        got = trees.get(repo, {"entries": make_tree(), "truncated": False})
        if isinstance(got, Exception):
            raise got
        return got
    github_credentials.git_tree = fake_tree

    def no_github(request):   # the decision importer (TR-1210) hangs on the same hook: it must never reach a real GitHub here
        return httpx.Response(404, json={"message": "Not Found"})
    decisions_repo_mod.TRANSPORT = httpx.MockTransport(no_github)

    changes = []

    async def hook(repo, added, removed):
        changes.append((repo, added, removed))
    folders_mod.HOOKS.append(hook)

    app = make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=H(ROOT))
        store = app.state.store
        store.upsert_github_credential("setup", "s" * 10 + "tok")
        FOLDERS = "/api/brain/repos/acme/web/folders"

        print("== no licence ==")
        os.environ["TARES_BRAIN_URL"] = fake_lore.BASE_URL
        os.environ["TARES_BRAIN_KEY"] = BRAIN_KEY
        for method, body in (("GET", None), ("PUT", {"folders": ["docs/adr"]})):
            r = await cx.request(method, FOLDERS, json=body)
            ck(f"no licence: {method} folders is 404 as an unmounted route", r.status_code == 404 and r.json() == {"detail": "Not Found"}, f"{r.status_code} {r.text[:100]}")
        r = await cx.put("/api/licence", json={"key": licensed.test_licence()})
        ck("the licence is stored", r.status_code == 200, r.text[:200])

        print("== connect with folders ==")
        r = await cx.post("/api/brain/repos", json={"repos": ["acme/web"], "folders": ["/docs/adr/"]})
        row = (r.json().get("repos") or [{}])[0] if r.status_code == 201 else {}
        ck("a list of folders is accepted for one repo; the row lists the paths", r.status_code == 201 and row.get("folders") == ["docs/adr"], f"{r.status_code} {r.text[:300]}")
        ck("the folder is stored with no person (the root token)", [(f["path"], f["chosen_by"]) for f in store.list_brain_folders("acme/web")] == [("docs/adr", None)])
        ck("the hook heard the added folder", changes == [("acme/web", ["docs/adr"], [])], str(changes))
        ck("connecting without folders gives an empty list", (await cx.post("/api/brain/repos", json={"repos": ["acme/api"]})).json()["repos"][0]["folders"] == [])
        r = await cx.post("/api/brain/repos", json={"repos": ["acme/docs", "acme/two"], "folders": {"Acme/Docs": ["adr"], "acme/two": ["docs/adr", "decisions"]}})
        ck("an object keyed by repo sets each repo's folders", r.status_code == 201 and [x["folders"] for x in r.json()["repos"]] == [["adr"], ["decisions", "docs/adr"]], f"{r.status_code} {r.text[:300]}")
        r = await cx.post("/api/brain/repos", json={"repos": ["acme/three"], "folders": None})
        ck("folders: null is none chosen", r.status_code == 201 and r.json()["repos"][0]["folders"] == [], r.text[:200])
        r = await cx.get("/api/brain")
        ck("GET /api/brain lists each repo's folders", {x["repo"]: x["folders"] for x in r.json()["repos"]}["acme/two"] == ["decisions", "docs/adr"], r.text[:300])

        print("== refused folders on connect ==")
        before = (len(store.list_brain_repos()), len(lore.requests), len(changes))

        async def refuse(label, payload, words):
            r = await cx.post("/api/brain/repos", json=payload)
            ck(label, r.status_code == 400 and words in r.text, f"{r.status_code} {r.text[:200]}")
            ck("  ... nothing stored, lore not called, no hook", (len(store.list_brain_repos()), len(lore.requests), len(changes)) == before)
        await refuse("a list with two repos", {"repos": ["acme/gone", "acme/bad"], "folders": ["adr"]}, "more than one repo")
        await refuse("folders of a repo that is not in the request", {"repos": ["acme/gone"], "folders": {"acme/bad": ["adr"]}}, "not one of the repos being connected")
        await refuse("a folder that is not a path", {"repos": ["acme/gone"], "folders": ["../etc"]}, "not a folder")
        await refuse("a bad repo key", {"repos": ["acme/gone"], "folders": {"nope": ["adr"]}}, "owner/name")
        await refuse("a number", {"repos": ["acme/gone"], "folders": 5}, "folders:")
        await refuse("a folder twice", {"repos": ["acme/gone"], "folders": ["adr", "/adr"]}, "twice")
        await refuse("an object whose value is not a list", {"repos": ["acme/gone"], "folders": {"acme/gone": "adr"}}, "a list")

        print("== GET: chosen and suggested ==")
        r = await cx.get(FOLDERS)
        j = r.json()
        ck("GET answers repo, folders, suggested, partial, scanned, scan_error", r.status_code == 200 and set(j) == {"repo", "folders", "suggested", "partial", "scanned", "scan_error"}, r.text[:300])
        ck("docs/adr is suggested with 4 files and is marked chosen", {"path": "docs/adr", "files": 4, "why": "known", "chosen": True} in j["suggested"], str(j["suggested"]))
        ck("a numbered folder is suggested, not chosen", {"path": "design/records", "files": 3, "why": "numbered", "chosen": False} in j["suggested"], str(j["suggested"]))
        ck("the chosen folder carries who, when and its file count", [(f["path"], f["files"]) for f in j["folders"]] == [("docs/adr", 4)]
           and set(j["folders"][0]) == {"path", "chosen_by", "chosen_by_name", "chosen_at", "files"} and j["folders"][0]["chosen_at"], str(j["folders"]))
        ck("not partial, scanned, no error", (j["partial"], j["scanned"], j["scan_error"]) == (False, True, None), r.text[:300])
        ck("the scan read the tree of this repo once", asked.count("acme/web") == 1, str(asked))
        r = await cx.get("/api/brain/repos/Acme/WEB/folders")
        ck("the repo name is matched in any case", r.status_code == 200 and r.json()["repo"] == "acme/web", f"{r.status_code}")

        print("== PUT: choose ==")
        n = len(changes)
        pid = (await cx.post("/api/people", json={"name": "Jo", "email": "jo@x.dev"})).json()["id"]

        async def key(scopes, person=None):
            body = {"name": "k-" + "-".join(scopes), "scopes": scopes}
            if person:
                body["person_id"] = person
            r = await cx.post("/api/keys", json=body)
            return H(r.json()["secret"])
        reader, admin = await key(["read"], pid), await key(["admin"], pid)
        first = {f["path"]: f for f in store.list_brain_folders("acme/web")}
        r = await cx.put(FOLDERS, json={"folders": ["docs/adr", "design/records"]}, headers=admin)
        j = r.json()
        ck("an admin chooses a second folder: added and removed say what changed", r.status_code == 200 and (j["added"], j["removed"]) == (["design/records"], [])
           and [f["path"] for f in j["folders"]] == ["design/records", "docs/adr"], f"{r.status_code} {r.text[:300]}")
        now = {f["path"]: f for f in store.list_brain_folders("acme/web")}
        ck("the new folder names the person on the key; docs/adr keeps its chooser and date", now["design/records"]["chosen_by"] == pid
           and now["design/records"]["chosen_by_name"] == "Jo" and now["docs/adr"]["chosen_at"] == first["docs/adr"]["chosen_at"]
           and now["docs/adr"]["chosen_by"] is None, str(now))
        ck("the hook heard the added folder only", changes[n:] == [("acme/web", ["design/records"], [])], str(changes[n:]))
        r = await cx.put(FOLDERS, json={"folders": ["design/records", "adr/new"]})
        ck("a drop and an add in one go", r.json()["added"] == ["adr/new"] and r.json()["removed"] == ["docs/adr"], r.text[:300])
        ck("... and the hook heard both", changes[-1] == ("acme/web", ["adr/new"], ["docs/adr"]), str(changes[-1]))
        n = len(changes)
        r = await cx.put(FOLDERS, json={"folders": ["adr/new", "design/records"]})
        ck("the same folders again: nothing added or removed, no hook", r.status_code == 200 and (r.json()["added"], r.json()["removed"]) == ([], [])
           and len(changes) == n, r.text[:300])
        r = await cx.get(FOLDERS)
        ck("a chosen folder that is not in the repo yet has 0 files", {f["path"]: f["files"] for f in r.json()["folders"]} == {"adr/new": 0, "design/records": 3}, r.text[:300])
        ck("the suggestions now mark design/records chosen and docs/adr not", {s["path"]: s["chosen"] for s in r.json()["suggested"]} == {"docs/adr": False, "design/records": True}, r.text[:300])
        r = await cx.put(FOLDERS, json={"folders": []})
        ck("an empty list clears the folders", r.status_code == 200 and r.json()["folders"] == [] and r.json()["removed"] == ["adr/new", "design/records"]
           and store.list_brain_folders("acme/web") == [], r.text[:300])
        await cx.put(FOLDERS, json={"folders": ["docs/adr"]})

        print("== PUT: refused ==")
        keep = store.list_brain_folders("acme/web")
        n = len(changes)
        for label, payload, code, words in (("folders missing", {}, 400, "a list"), ("folders null", {"folders": None}, 400, "a list"),
                                            ("folders is text", {"folders": "docs/adr"}, 400, "a list"),
                                            ("a null entry", {"folders": [None]}, 400, "not a folder"),
                                            ("a dot dot path", {"folders": ["a/../b"]}, 400, "not a folder"),
                                            ("the root", {"folders": ["/"]}, 400, "not a folder"),
                                            ("a folder twice", {"folders": ["a", "a"]}, 400, "twice"),
                                            ("an unknown field", {"folders": [], "force": True}, 400, "unknown field 'force'")):
            r = await cx.put(FOLDERS, json=payload)
            ck(f"{label} is {code}", r.status_code == code and words in r.text, f"{r.status_code} {r.text[:200]}")
        ck("... nothing changed and no hook ran", store.list_brain_folders("acme/web") == keep and len(changes) == n)
        r = await cx.put(FOLDERS, json=["a"])
        ck("a body that is not an object is 422", r.status_code == 422, f"{r.status_code}")
        r = await cx.put("/api/brain/repos/acme/nothere/folders", json={"folders": ["a"]})
        ck("a repo that is not connected is 404", r.status_code == 404 and "not connected" in r.text, f"{r.status_code} {r.text[:200]}")
        r = await cx.get("/api/brain/repos/acme/nothere/folders")
        ck("... for GET too", r.status_code == 404, f"{r.status_code}")
        r = await cx.get("/api/brain/repos/acme/../folders")
        ck("a dot dot repo name is not found or refused, never a scan", r.status_code in (400, 404), f"{r.status_code}")
        r = await cx.put("/api/brain/repos/a_b/c/folders", json={"folders": ["a"]})
        ck("a repo name that is not owner/name is 400", r.status_code == 400 and "is not a repo" in r.text, f"{r.status_code} {r.text[:200]}")

        print("== who may ==")
        r = await cx.get(FOLDERS, headers=reader)
        ck("a read key can read the folders", r.status_code == 200 and r.json()["folders"], f"{r.status_code}")
        r = await cx.put(FOLDERS, json={"folders": ["x"]}, headers=reader)
        ck("a read key cannot change them (403)", r.status_code == 403 and store.list_brain_folders("acme/web") == keep, f"{r.status_code} {r.text[:100]}")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as bare:
            r = await bare.get(FOLDERS)
        ck("no credential at all is refused by the daemon", r.status_code == 401, f"{r.status_code}")

        class Host:
            def __init__(self, is_admin):
                self.store, self._admin = store, is_admin
            def is_admin(self, request): return self._admin
            def require_admin(self, request):
                if not self._admin:
                    self.err(PermissionError("this needs an admin"), 403)
            def current_person(self, request): return None
            def err(self, e, code=400):
                from fastapi import HTTPException
                raise HTTPException(status_code=code, detail=str(e))
        from fastapi import FastAPI
        bare = FastAPI()
        repos_mod.register_routes(bare, Host(False))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bare), base_url="http://b") as bx:
            r = await bx.put(FOLDERS, json={"folders": ["x"]})
        ck("the handler checks admin itself, not only the daemon's guard", r.status_code == 403 and store.list_brain_folders("acme/web") == keep, f"{r.status_code}")

        print("== a huge repo: the tree is cut short ==")
        cut_entries = [tree("docs"), tree("docs/adr"), blob("docs/adr/0001-a.md"), blob("docs/adr/0002-b.md"),
                       tree("design/records"), blob("design/records/0001-x.md"), blob("design/records/0002-y.md")]
        await cx.post("/api/brain/repos", json={"repos": ["acme/cut"], "folders": ["elsewhere/adr", "docs/adr"]})
        trees["acme/cut"] = {"entries": cut_entries, "truncated": True}
        r = await cx.get("/api/brain/repos/acme/cut/folders")
        j = r.json()
        ck("partial is true and only the known names are suggested", j["partial"] is True and [s["path"] for s in j["suggested"]] == ["docs/adr"], r.text[:400])
        ck("a chosen folder GitHub did not send says null files, one it did says its count", {f["path"]: f["files"] for f in j["folders"]} == {"docs/adr": 2, "elsewhere/adr": None}, r.text[:400])

        print("== GitHub cannot be asked ==")
        await cx.post("/api/brain/repos", json={"repos": ["acme/down"], "folders": ["docs/adr"]})
        trees["acme/down"] = ValueError("GitHub returned 500 for the tree of acme/down")
        r = await cx.get("/api/brain/repos/acme/down/folders")
        j = r.json()
        ck("the chosen folders still come back, with the reason", r.status_code == 200 and j["scanned"] is False and "returned 500" in j["scan_error"]
           and j["suggested"] == [] and [(f["path"], f["files"]) for f in j["folders"]] == [("docs/adr", None)], r.text[:400])
        r = await cx.put("/api/brain/repos/acme/down/folders", json={"folders": ["decisions"]})
        ck("choosing needs no GitHub call", r.status_code == 200 and r.json()["added"] == ["decisions"], r.text[:200])
        store.delete_github_credential("setup")
        r = await cx.get(FOLDERS)
        ck("with no GitHub connection the scan says so in plain words", r.status_code == 200 and r.json()["scanned"] is False
           and "no GitHub connection" in r.json()["scan_error"] and r.json()["folders"], r.text[:300])
        store.upsert_github_credential("setup", "s" * 10 + "tok")
        trees["acme/empty"] = {"entries": [], "truncated": False}
        await cx.post("/api/brain/repos", json={"repos": ["acme/empty"]})
        r = await cx.get("/api/brain/repos/acme/empty/folders")
        ck("an empty repo suggests nothing and is not an error", r.status_code == 200 and r.json()["suggested"] == [] and r.json()["scanned"] is True, r.text[:300])

        print("== a hook that fails ==")
        async def broken(repo, added, removed):
            raise RuntimeError("boom with a secret ghp_x")
        folders_mod.HOOKS.insert(0, broken)
        r = await cx.put(FOLDERS, json={"folders": ["docs/adr", "adr"]})
        ck("the change is stored and the answer is 200; the next hook still runs", r.status_code == 200 and "adr" in [f["path"] for f in store.list_brain_folders("acme/web")]
           and changes[-1] == ("acme/web", ["adr"], []), f"{r.status_code} {r.text[:200]}")
        folders_mod.HOOKS.remove(broken)

        print("== removing the repo removes its folders ==")
        r = await cx.delete("/api/brain/repos/acme/web")
        ck("the repo is removed", r.status_code == 200 and store.get_brain_repo("acme/web") is None, f"{r.status_code} {r.text[:200]}")
        ck("... and its folders", store.list_brain_folders("acme/web") == [] and store.list_brain_folders("acme/two") != [])

        print("== the licence goes away ==")
        await cx.delete("/api/licence")
        r = await cx.get("/api/brain/repos/acme/two/folders")
        ck("the routes are 404 again", r.status_code == 404 and r.json() == {"detail": "Not Found"}, f"{r.status_code} {r.text[:100]}")
        await cx.aclose()


unit()
os.environ["TARES_LICENSE"] = ""
asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
