# Tares enterprise licence, see ee/LICENSE
"""Each repo row of GET /api/brain says how many decisions the brain holds from its folders (TR-1442).

The count is kept in memory by `tares_ee.brain.decisions_repo.RepoDecisions` (`count`): filled once at
start-up from lore's list of the `repo_decisions` source and added to by every put. The Repos page polls the
route every 10 to 60 seconds, so a request must never call lore or GitHub. Here it runs against the shared
fake lore (ee/tests/fake_lore.py), the fake control plane's licence helpers and a small fake GitHub (an httpx
mock transport that counts its calls; never the real one).

Covered: 0 with no folders and before any import; the row after an import (one per decision put, the
files that are not markdown or not in a chosen folder left out); a poll makes no lore and no GitHub call
(20 polls, and the count is the same); a second chosen folder raises it; a dropped folder lowers it at
once; a folder whose name only starts like a chosen one is not counted; another repo's documents are not
counted; a put lore refuses is not counted; after a restart (a new importer, nothing in memory) the count is
right as soon as start-up has read lore, also when GitHub then answers with a rate limit, and a lore that
cannot list leaves the count at 0 without a crash; the pure `count`.

In process, no ports. With CI's Postgres run (TARES_TEST_PG and tests/pg_harness on PYTHONPATH) the same
script keeps its state in Postgres.

Run: .venv/bin/python ee/tests/test_brain_decisions_repo_count.py
"""
import asyncio, base64, logging, os, sys, tempfile
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings, puts the repo, ee/ and tests/ on sys.path
licensed.install_test_root()

TMP = tempfile.mkdtemp(prefix="tares-brain-decisions-count-")
ROOT = "root-token-" + "brain-decisions-count"
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_AUTH_TOKEN"] = ROOT

import httpx

import fake_lore
from tares import github_credentials
from tares_ee.brain import decisions_repo as dr
from tares_ee.brain import index as index_mod
from tares_ee.brain import repos as repos_mod
from tares_ee.brain import sync as sync_mod

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


BRAIN_KEY = fake_lore.FakeLore().key
GH_TOKEN = "gh-token-" + "for-the-count-tests"       # gitleaks:allow
SOURCE = "repo_decisions"
NOW = "2026-01-01T00:00:00Z"


class Logs(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())

    def mine(self):
        return [l for l in self.lines if "brain repo decisions" in l]


class Gh:
    """GitHub's tree, blob and commit routes for repos whose files are {path: text}; one commit per file, no
    pull requests. Counts every call; `budget` is the calls left before a 403 rate limit (None: unlimited)."""

    def __init__(self):
        self.repos = {}
        self.requests = []
        self.budget: int | None = None

    async def handle(self, request):
        path = request.url.raw_path.decode().split("?")[0]
        q = dict(request.url.params)
        self.requests.append(path)
        if self.budget is not None:
            if self.budget <= 0:
                return httpx.Response(403, headers={"x-ratelimit-remaining": "0"}, json={"message": "API rate limit exceeded"})
            self.budget -= 1
        parts = path.strip("/").split("/")
        files = self.repos.get(f"{parts[1]}/{parts[2]}")
        rest = "/".join(parts[3:])
        if files is None:
            return httpx.Response(404, json={"message": "Not Found"})
        if rest.startswith("git/trees/"):
            return httpx.Response(200, json={"tree": [{"path": p, "type": "blob", "sha": "blob-" + p} for p in files], "truncated": False})
        if rest.startswith("git/blobs/"):
            p = unquote(rest[len("git/blobs/blob-"):])
            return httpx.Response(200, json={"encoding": "base64", "size": len(files[p]),
                                             "content": base64.encodebytes(files[p].encode()).decode()})
        if rest.endswith("/pulls"):
            return httpx.Response(200, json=[])
        p = unquote(q.get("path", ""))
        c = {"sha": "c-" + p, "commit": {"author": {"name": "Ann A", "date": "2024-01-01T00:00:00Z"},
                                         "committer": {"name": "Ann A", "date": "2024-01-01T00:00:00Z"}},
             "author": {"login": "ann"}}
        return httpx.Response(200, json=[c] if p in files else [])


def adr(n, folder="docs/adr", prefix="a"):
    return {f"{folder}/{prefix}{i:03d}.md": f"# {prefix}{i}\n" for i in range(n)}


def unit():
    print("== the pure count ==")
    rd = dr.RepoDecisions(None, lambda: None, None)
    rd._held["a/b"] = {"docs/adr/x.md", "docs/adr/deep/y.md", "docs/adr2/z.md", "docs/other/w.md", "adr/v.md"}
    ck("no folders: 0", rd.count("a/b", []) == 0)
    ck("a repo nothing was put for: 0", rd.count("c/d", ["docs/adr"]) == 0)
    ck("files at any depth under a chosen folder", rd.count("a/b", ["docs/adr"]) == 2)
    ck("a folder named like the start of another is not under it", rd.count("a/b", ["docs/adr"]) == 2 and rd.count("a/b", ["docs/adr2"]) == 1)
    ck("two folders add up; a file under both counts once", rd.count("a/b", ["docs/adr", "docs/other"]) == 3
       and rd.count("a/b", ["docs", "docs/adr"]) == 4)


async def main():
    from tares.daemon import make_app
    logs = Logs()
    logging.getLogger().addHandler(logs)
    logging.getLogger().setLevel(logging.DEBUG)
    lore = fake_lore.FakeLore()
    repos_mod.lore_client = lore.client
    gh = Gh()
    gh.repos["acme/web"] = {**adr(3), **adr(2, "docs/old", "o"), "docs/adr/notes.txt": "not markdown\n", "src/x.md": "# no\n"}
    gh.repos["acme/api"] = adr(2, "docs/adr", "p")
    dr.TRANSPORT = httpx.MockTransport(gh.handle)

    async def never(self):
        await asyncio.Event().wait()
    index_mod.Indexer.run = never
    sync_mod.Syncer.run = never

    async def readable_repos(cred, query=""):
        return [{"full_name": r} for r in ("acme/web", "acme/api")]
    github_credentials.list_repos_for = readable_repos

    def choose(store, repo, folders):
        if store.get_brain_repo(repo) is None:
            store.add_brain_repo(repo, "src-" + repo, None, NOW)
        return store.set_brain_folders(repo, folders, None, NOW)

    app = make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=H(ROOT))
        store = app.state.store
        rd = app.state.brain_repo_decisions
        store.upsert_github_credential("setup", GH_TOKEN)
        r = await cx.put("/api/licence", json={"key": licensed.test_licence()})
        ck("the licence is stored", r.status_code == 200, r.text[:200])
        os.environ["TARES_BRAIN_URL"] = fake_lore.BASE_URL
        os.environ["TARES_BRAIN_KEY"] = BRAIN_KEY

        async def row(repo):
            r = await cx.get("/api/brain")
            assert r.status_code == 200, r.text
            return next(x for x in r.json()["repos"] if x["repo"] == repo)

        print("== 0 with no folders and before an import ==")
        choose(store, "acme/web", [])
        choose(store, "acme/api", [])
        w = await row("acme/web")
        ck("no folders: the row says decisions 0", w["decisions"] == 0 and w["folders"] == [], str(w))

        print("== the count follows an import ==")
        r = await cx.put("/api/brain/repos/acme/web/folders", json={"folders": ["docs/adr"]})
        ck("the folder is chosen", r.status_code == 200, r.text[:200])
        await rd.wait()
        stored = {e for (src, e) in lore.documents if src == SOURCE}
        ck("three markdown files of the folder are in lore", len(stored) == 3, str(sorted(stored)))
        w = await row("acme/web")
        ck("the row says decisions 3", w["decisions"] == 3 and w["folders"] == ["docs/adr"], str(w))
        a = await row("acme/api")
        ck("another repo with no folders stays 0", a["decisions"] == 0, str(a))

        print("== a poll costs nothing: no lore call, no GitHub call ==")
        lore_n, gh_n = len(lore.requests), len(gh.requests)
        for _ in range(20):
            w = await row("acme/web")
        ck("20 polls: the same count, no request to lore or to GitHub", w["decisions"] == 3
           and len(lore.requests) == lore_n and len(gh.requests) == gh_n, f"{len(lore.requests) - lore_n} {len(gh.requests) - gh_n}")

        print("== a second folder raises it; a dropped one lowers it at once ==")
        r = await cx.put("/api/brain/repos/acme/web/folders", json={"folders": ["docs/adr", "docs/old"]})
        await rd.wait()
        w = await row("acme/web")
        ck("two folders: 3 + 2", w["decisions"] == 5, str(w))
        r = await cx.put("/api/brain/repos/acme/web/folders", json={"folders": ["docs/adr"]})
        w = await row("acme/web")
        ck("docs/old dropped: back to 3, with no wait and no lore call", w["decisions"] == 3, str(w))
        r = await cx.put("/api/brain/repos/acme/web/folders", json={"folders": []})
        w = await row("acme/web")
        ck("every folder dropped: 0", w["decisions"] == 0, str(w))
        await cx.put("/api/brain/repos/acme/web/folders", json={"folders": ["docs/adr"]})
        await rd.wait()

        print("== another repo's documents are not counted ==")
        await cx.put("/api/brain/repos/acme/api/folders", json={"folders": ["docs/adr"]})
        await rd.wait()
        w, a = await row("acme/web"), await row("acme/api")
        ck("acme/api has its own two, acme/web still three", a["decisions"] == 2 and w["decisions"] == 3, f"{a['decisions']} {w['decisions']}")

        print("== a put lore refuses is not counted ==")
        gh.repos["acme/api"]["docs/adr/new.md"] = "# new\n"
        lore.fail("put_documents", 500, "boom")
        await rd.import_folders("acme/api", {"docs/adr"})
        a = await row("acme/api")
        ck("the failed put leaves the count where it was", a["decisions"] == 2, str(a))
        await rd.import_folders("acme/api", {"docs/adr"})
        a = await row("acme/api")
        ck("the next import counts the new file", a["decisions"] == 3, str(a))

        print("== after a restart the count is right once start-up has read lore ==")
        fresh = dr.RepoDecisions(store, lambda: lore.client(), app.state.brain_repo_decisions._token)
        ck("a new importer has nothing in memory", fresh.count("acme/web", ["docs/adr"]) == 0)
        gh.budget = 0                       # GitHub is rate limited: the import of start-up cannot help
        lore_calls = len(lore.calls("GET", "list_documents"))
        await fresh.startup()
        await fresh.wait()
        ck("start-up read lore's list of the source, once", len(lore.calls("GET", "list_documents")) == lore_calls + 1,
           str(len(lore.calls("GET", "list_documents")) - lore_calls))
        ck("the counts are back although GitHub answered with a rate limit", fresh.count("acme/web", ["docs/adr"]) == 3
           and fresh.count("acme/api", ["docs/adr"]) == 3, f"{fresh.count('acme/web', ['docs/adr'])} {fresh.count('acme/api', ['docs/adr'])}")
        gh.budget = None

        print("== a lore that cannot list leaves the count at 0 and does not crash ==")
        blind = dr.RepoDecisions(store, lambda: lore.client(), app.state.brain_repo_decisions._token)
        for _ in range(8):   # start-up's read, and the retry before each prune and import (TR-1455): lore stays unreadable
            lore.fail("list_documents", 500, "no list " + BRAIN_KEY)
        gh.budget = 0
        await blind.startup()
        await blind.wait()
        gh.budget = None
        ck("0, and a line in the log without the key", blind.count("acme/web", ["docs/adr"]) == 0
           and any("could not read what lore holds" in l for l in logs.mine()) and not any(BRAIN_KEY in l for l in logs.lines),
           str(logs.mine()[-2:]))
        await cx.aclose()


unit()
asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
