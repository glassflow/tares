# Tares enterprise licence, see ee/LICENSE
"""The decision folder files follow pushes, edits and deletes (TR-1219).

`tares_ee.brain.decisions_repo.RepoDecisions` keeps the lore documents of the chosen folders (TR-1202) in step
with the repo. A push to the default branch (a webhook envelope from core's GitHub hub, handled by `Syncer.on_envelopes` and told to
`RepoDecisions.on_push`) names the files it touched; the tree read decides for each one: still there means
imported again (an unchanged blob costs no GitHub call, TR-1210's cache), gone means its document is deleted.
A push with no usable file list (GitHub cuts the list at 20 commits) and a moved head found by the poll of a
token repo run a full pass over the chosen folders. A folder an admin drops deletes its documents. In process
against the shared fake lore (ee/tests/fake_lore.py), the fake control plane's licence helpers and a small
mutable fake GitHub (an httpx mock transport that counts its calls; never the real one).

Covered: a file added, edited and deleted by a push reaches lore and the row's count (the M2 acceptance: a
deleted ADR file leaves the brain), each costing one tree read and only the changed file's blob; a push that
touches nothing in a chosen folder, or no markdown, makes no GitHub and no lore call; an unchanged file named
by a push costs no blob; a rename (removed and added) and several commits in one push; a push with no commit
list, and one with 20 commits, reconcile everything (an edit and a delete nobody named still arrive); a
truncated tree deletes nothing; a poll that sees a moved head of a token repo reconciles, one that sees the
same head does not; a dropped folder (and all folders dropped) deletes its documents and no other repo's or
folder's; a document lore no longer has (404) counts as deleted; another lore error leaves the document and
the count, tells no lie, and the next pass deletes it; pushes of another branch, from a non-App source or
before the licence do nothing; a document of a folder dropped while the cell was down goes at start-up; a delete
that lands while the seed holds an already-read list is not put back by the seed's merge; a force push
reconciles everything (TR-1455); an edit that makes a file stop counting (over the size limit, its blob gone,
no usable date) deletes its document; a start-up that could not read lore's list reads it again before a push
deletes, and a push waits for start-up's read (a slow seed) before it deletes; the pure `pushed_files`.

In process, no ports. With CI's Postgres run (TARES_TEST_PG and tests/pg_harness on PYTHONPATH) the same
script keeps its state in Postgres.

Run: .venv/bin/python ee/tests/test_brain_decisions_repo_sync.py
"""
import asyncio, base64, hashlib, logging, os, sys, tempfile
from datetime import datetime, timezone
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings, puts the repo, ee/ and tests/ on sys.path
licensed.install_test_root()

TMP = tempfile.mkdtemp(prefix="tares-brain-decisions-sync-")
ROOT = "root-token-" + "brain-decisions-sync"
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_AUTH_TOKEN"] = ROOT

import httpx

import fake_lore
from tares import github_credentials
from tares.config import SourceCfg
from tares.connectors import github_events as gh_events
from tares.envelope import Envelope
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
GH_TOKEN = "gh-token-" + "for-the-sync-tests"       # gitleaks:allow
SOURCE = "repo_decisions"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class Logs(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())

    def mine(self):
        return [l for l in self.lines if "brain repo decisions" in l]


def sha_of(text):
    return hashlib.sha1(text.encode()).hexdigest()


class Gh:
    """GitHub's tree, blob and commit routes for repos whose files are {path: text}, which a test changes
    between pushes; a blob's sha comes from its text. No pull requests. Counts every call."""

    def __init__(self):
        self.repos = {}
        self.requests = []
        self.truncated = False
        self.head = {}
        self.hidden = set()      # blob shas GitHub no longer has (404)
        self.undated = set()     # paths whose commits carry no date
        self.tree_sizes = {}     # path -> the size the tree itself lists for it (the tree lists none otherwise)

    def count(self, what):
        return len([p for p in self.requests if f"/{what}/" in p])

    async def handle(self, request):
        path = request.url.raw_path.decode().split("?")[0]
        q = dict(request.url.params)
        self.requests.append(path)
        parts = path.strip("/").split("/")
        files = self.repos.get(f"{parts[1]}/{parts[2]}")
        rest = "/".join(parts[3:])
        if files is None:
            return httpx.Response(404, json={"message": "Not Found"})
        if rest.startswith("git/trees/"):
            shown = list(files)[:-1] if self.truncated else list(files)   # a cut tree misses files
            return httpx.Response(200, json={"tree": [{"path": p, "type": "blob", "sha": sha_of(files[p]),
                                                       **({"size": self.tree_sizes[p]} if p in self.tree_sizes else {})} for p in shown],
                                             "truncated": self.truncated})
        if rest.startswith("git/blobs/"):
            want = rest[len("git/blobs/"):]
            text = next((t for t in files.values() if sha_of(t) == want), None)
            if text is None or want in self.hidden:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"encoding": "base64", "size": len(text),
                                             "content": base64.encodebytes(text.encode()).decode()})
        if rest.endswith("/pulls"):
            return httpx.Response(200, json=[])
        p = unquote(q.get("path", ""))
        when = None if p in self.undated else "2024-01-01T00:00:00Z"
        c = {"sha": "c-" + p, "commit": {"author": {"name": "Ann A", "date": when},
                                         "committer": {"name": "Ann A", "date": when}},
             "author": {"login": "ann"}}
        return httpx.Response(200, json=[c] if p in files else [])


def unit():
    print("== the files a push touched ==")
    f = sync_mod.pushed_files
    ck("added, modified and removed of every commit, as one set",
       f({"commits": [{"added": ["a.md"], "modified": ["b.md"], "removed": []}, {"added": [], "modified": [], "removed": ["c.md"]}]})
       == {"a.md", "b.md", "c.md"})
    ck("a commit with no files: an empty set (the push touched none)", f({"commits": [{"added": [], "modified": [], "removed": []}]}) == set())
    ck("no payload, no commit list or an empty one: unknown", f(None) is None and f({}) is None and f({"commits": []}) is None
       and f({"commits": "x"}) is None)
    ck("19 commits are listed, 20 are GitHub's cap and may be cut: unknown",
       f({"commits": [{"added": [], "modified": [], "removed": []}] * 19}) == set()
       and f({"commits": [{"added": [], "modified": [], "removed": []}] * 20}) is None)
    one = [{"added": ["a.md"], "modified": [], "removed": []}]
    ck("a force push: unknown, even with a commit list (the files the rewrite dropped are named nowhere)",
       f({"commits": one, "forced": True}) is None and f({"commits": one, "forced": False}) == {"a.md"} and f({"commits": one}) == {"a.md"})
    ck("a commit without its file lists: unknown", f({"commits": [{"id": "x"}]}) is None
       and f({"commits": [{"added": [], "modified": []}]}) is None and f({"commits": ["x"]}) is None)


async def main():
    from tares.daemon import make_app
    logs = Logs()
    logging.getLogger().addHandler(logs)
    logging.getLogger().setLevel(logging.DEBUG)
    lore = fake_lore.FakeLore()
    repos_mod.lore_client = lore.client
    gh = Gh()
    adr = {f"docs/adr/a{i:03d}.md": f"# a{i}\nfirst\n" for i in range(3)}
    gh.repos["acme/web"] = {**adr, "docs/other/x.md": "# x\n", "README.md": "# readme\n", "docs/adr/notes.txt": "text\n"}
    gh.repos["acme/api"] = {"docs/adr/p000.md": "# p0\n", "docs/adr/p001.md": "# p1\n"}
    dr.TRANSPORT = httpx.MockTransport(gh.handle)

    async def never(self):
        await asyncio.Event().wait()
    index_mod.Indexer.run = never
    sync_mod.Syncer.run = never

    async def readable_repos(cred, query=""):
        return [{"full_name": r} for r in ("acme/web", "acme/api", "acme/leave")]
    github_credentials.list_repos_for = readable_repos

    heads = {}

    async def fake_head(api_url, token, repo, known=None):
        return heads[repo], '"etag-' + heads[repo][:4] + '"'
    sync_mod.github_head = fake_head

    def choose(store, repo, folders):
        if store.get_brain_repo(repo) is None:
            store.add_brain_repo(repo, "src-" + repo, None, NOW)
            store.update_brain_repo(repo, {"state": "ready", "indexed_commit": "0" * 40})
        return store.set_brain_folders(repo, folders, None, NOW)

    app = make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=H(ROOT))
        store = app.state.store
        rd = app.state.brain_repo_decisions
        syncer = app.state.brain_syncer
        runtime = app.state.runtime
        runtime.catalog.sources["github"] = SourceCfg(name="github", type="event_stream", connector="github_app", poll_seconds=0)
        runtime.catalog.sources["hook"] = SourceCfg(name="hook", type="event_stream", connector="webhook", poll_seconds=0)
        store.upsert_github_credential("setup", GH_TOKEN)

        seq = [0]

        def push(repo, added=(), modified=(), removed=(), commits=None, branch="main", payload_files=True, forced=False):
            seq[0] += 1
            sha = f"{seq[0]:040x}"
            lst = commits if commits is not None else [{"id": sha, "added": list(added), "modified": list(modified), "removed": list(removed)}]
            if not payload_files:
                lst = [{"id": sha}]
            payload = {"ref": "refs/heads/" + branch, "after": sha, "commits": lst, "head_commit": {"message": "change"},
                       "repository": {"full_name": repo, "default_branch": "main"}, "pusher": {"name": "kim"}}
            if forced:
                payload["forced"] = True
            b = gh_events.build("push", payload)
            return Envelope(source="github", source_type="event_stream", key_value=b["key"], event_type="push",
                            text=b["text"], event_time=b["event_time"], labels=b["labels"], payload=payload)

        async def ingest(*envs, source="github"):
            from tares.github_webhook import HUB   # the App's deliveries reach the brain here (TR-1807)
            if source == "github":
                HUB.tell(source, list(envs))
            await runtime._store_envelopes(source, list(envs))
            await rd.wait()

        def docs(repo="acme/web"):
            return {e[len(repo) + 1:]: d for (s, e), d in lore.documents.items() if s == SOURCE and e.startswith(repo + ":")}

        async def row(repo="acme/web"):
            r = await cx.get("/api/brain")
            assert r.status_code == 200, r.text
            return next(x for x in r.json()["repos"] if x["repo"] == repo)

        def calls():
            return len(gh.requests), len(lore.requests)

        r = await cx.put("/api/licence", json={"key": licensed.test_licence()})
        ck("the licence is stored", r.status_code == 200, r.text[:200])
        os.environ["TARES_BRAIN_URL"] = fake_lore.BASE_URL
        os.environ["TARES_BRAIN_KEY"] = BRAIN_KEY
        ck("the importer is a listener of the syncer's pushes", rd.on_push in syncer.push_listeners, str(syncer.push_listeners))

        choose(store, "acme/web", [])
        choose(store, "acme/api", [])
        r = await cx.put("/api/brain/repos/acme/web/folders", json={"folders": ["docs/adr"]})
        ck("the folder is chosen", r.status_code == 200, r.text[:200])
        await rd.wait()
        ck("the first import put the three markdown files of the folder", sorted(docs()) == sorted(adr), str(sorted(docs())))
        ck("the row says 3", (await row())["decisions"] == 3, str(await row()))

        print("== a file added by a push ==")
        gh.repos["acme/web"]["docs/adr/a003.md"] = "# a3\nnew\n"
        gh0 = len(gh.requests)
        trees0, blobs0 = gh.count("git/trees"), gh.count("git/blobs")
        await ingest(push("acme/web", added=["docs/adr/a003.md"]))
        ck("its document is in lore", "docs/adr/a003.md" in docs() and docs()["docs/adr/a003.md"]["text"] == "# a3\nnew\n", str(sorted(docs())))
        ck("the row says 4", (await row())["decisions"] == 4, str(await row()))
        ck("one tree read and one blob (the other files came from the cache)",
           gh.count("git/trees") - trees0 == 1 and gh.count("git/blobs") - blobs0 == 1, f"{gh.count('git/trees') - trees0} {gh.count('git/blobs') - blobs0}")

        print("== a file edited by a push ==")
        gh.repos["acme/web"]["docs/adr/a001.md"] = "# a1\nsecond version\n"
        blobs0, puts0 = gh.count("git/blobs"), len(lore.calls("POST", "put_documents"))
        await ingest(push("acme/web", modified=["docs/adr/a001.md"]))
        ck("lore holds the new text", docs()["docs/adr/a001.md"]["text"] == "# a1\nsecond version\n", docs()["docs/adr/a001.md"]["text"])
        ck("one blob, one put, and the count is the same", gh.count("git/blobs") - blobs0 == 1
           and len(lore.calls("POST", "put_documents")) - puts0 == 1 and (await row())["decisions"] == 4,
           f"{gh.count('git/blobs') - blobs0} {len(lore.calls('POST', 'put_documents')) - puts0}")

        print("== a file deleted by a push: it leaves the brain ==")
        del gh.repos["acme/web"]["docs/adr/a002.md"]
        blobs0, puts0 = gh.count("git/blobs"), len(lore.calls("POST", "put_documents"))
        await ingest(push("acme/web", removed=["docs/adr/a002.md"]))
        ck("its document is gone from lore, the others stay", sorted(docs()) == ["docs/adr/a000.md", "docs/adr/a001.md", "docs/adr/a003.md"], str(sorted(docs())))
        ck("the row says 3 at once", (await row())["decisions"] == 3, str(await row()))
        ck("it cost one lore delete, no blob and no put", len(lore.calls("DELETE", "delete_document")) == 1
           and gh.count("git/blobs") == blobs0 and len(lore.calls("POST", "put_documents")) == puts0)

        print("== a push that touches nothing in a chosen folder costs nothing ==")
        n = calls()
        await ingest(push("acme/web", modified=["README.md", "docs/other/x.md", "docs/adr/notes.txt", "docs/adrs/y.md"]))
        ck("not markdown, or not in the folder, or a folder that only starts like it: no GitHub and no lore call", calls() == n, f"{calls()} {n}")
        await ingest(push("acme/web", commits=[{"id": "x", "added": [], "modified": [], "removed": []}]))
        ck("a push that touched no file: the same", calls() == n)

        print("== a file the push names but did not change costs no blob ==")
        n_blobs, n_puts, n_dels = gh.count("git/blobs"), len(lore.calls("POST", "put_documents")), len(lore.calls("DELETE", "delete_document"))
        trees0 = gh.count("git/trees")
        await ingest(push("acme/web", modified=["docs/adr/a000.md"]))
        ck("one tree read, no blob, no put, no delete", gh.count("git/trees") - trees0 == 1 and gh.count("git/blobs") == n_blobs
           and len(lore.calls("POST", "put_documents")) == n_puts and len(lore.calls("DELETE", "delete_document")) == n_dels)

        print("== a rename and several commits in one push ==")
        gh.repos["acme/web"]["docs/adr/a010.md"] = gh.repos["acme/web"].pop("docs/adr/a000.md")
        gh.repos["acme/web"]["docs/adr/a011.md"] = "# a11\n"
        await ingest(push("acme/web", commits=[
            {"id": "1", "added": ["docs/adr/a010.md"], "modified": [], "removed": ["docs/adr/a000.md"]},
            {"id": "2", "added": ["docs/adr/a011.md"], "modified": [], "removed": []}]))
        ck("the old path is gone and both new ones are in", sorted(docs()) == ["docs/adr/a001.md", "docs/adr/a003.md", "docs/adr/a010.md", "docs/adr/a011.md"], str(sorted(docs())))
        ck("the row says 4", (await row())["decisions"] == 4, str(await row()))

        print("== a push that cannot say what it changed reconciles everything ==")
        gh.repos["acme/web"]["docs/adr/a003.md"] = "# a3\nedited quietly\n"
        del gh.repos["acme/web"]["docs/adr/a011.md"]
        gh.repos["acme/web"]["docs/adr/a020.md"] = "# a20\n"
        await ingest(push("acme/web", payload_files=False))
        ck("an edit, a delete and an add nobody named all arrive", docs()["docs/adr/a003.md"]["text"] == "# a3\nedited quietly\n"
           and "docs/adr/a011.md" not in docs() and "docs/adr/a020.md" in docs(), str(sorted(docs())))
        gh.repos["acme/web"]["docs/adr/a003.md"] = "# a3\nedited again\n"
        del gh.repos["acme/web"]["docs/adr/a020.md"]
        many = [{"id": str(i), "added": [], "modified": [], "removed": []} for i in range(20)]
        await ingest(push("acme/web", commits=many))
        ck("20 commits (GitHub's cap) do the same, though their lists are empty", docs()["docs/adr/a003.md"]["text"] == "# a3\nedited again\n"
           and "docs/adr/a020.md" not in docs(), str(sorted(docs())))
        ck("the row says 3", (await row())["decisions"] == 3, str(await row()))

        print("== a truncated tree deletes nothing ==")
        gh.truncated = True      # the tree misses the file listed last
        before = sorted(docs())
        await ingest(push("acme/web", payload_files=False))
        gh.truncated = False
        ck("every document is still there, and the log says why", sorted(docs()) == before
           and any("cut the tree" in l and "no document is deleted" in l for l in logs.mine()), str(sorted(docs())))
        ck("the row still says 3", (await row())["decisions"] == 3)

        print("== a token repo is polled: a moved head reconciles, the same head does not ==")
        heads["acme/web"] = "a" * 40
        gh.repos["acme/web"]["docs/adr/a001.md"] = "# a1\nthird version\n"
        await syncer._poll_repo(store.get_brain_repo("acme/web"))
        await rd.wait()
        ck("a moved head: the edit arrives", docs()["docs/adr/a001.md"]["text"] == "# a1\nthird version\n", docs()["docs/adr/a001.md"]["text"])
        n = calls()
        gh.repos["acme/web"]["docs/adr/a001.md"] = "# a1\nfourth version\n"
        await syncer._poll_repo(store.get_brain_repo("acme/web"))
        await rd.wait()
        ck("the same head: nothing is looked at", calls() == n and docs()["docs/adr/a001.md"]["text"] == "# a1\nthird version\n", f"{calls()} {n}")
        heads["acme/web"] = "b" * 40
        await syncer._poll_repo(store.get_brain_repo("acme/web"))
        await rd.wait()
        ck("the next moved head brings it", docs()["docs/adr/a001.md"]["text"] == "# a1\nfourth version\n")

        print("== a document lore no longer has counts as deleted ==")
        victim = "docs/adr/a010.md"
        lore.documents.pop((SOURCE, f"acme/web:{victim}"))
        del gh.repos["acme/web"][victim]
        n_logs = len([l for l in logs.mine() if "could not delete" in l])
        await ingest(push("acme/web", removed=[victim]))
        ck("the 404 is no error: no log line, and the row drops it", len([l for l in logs.mine() if "could not delete" in l]) == n_logs
           and (await row())["decisions"] == 2, str(await row()))

        print("== another lore error leaves the document, and the next pass deletes it ==")
        del gh.repos["acme/web"]["docs/adr/a003.md"]
        lore.fail("delete_document", 500, "boom " + BRAIN_KEY)
        await ingest(push("acme/web", removed=["docs/adr/a003.md"]))
        ck("the document is still in lore, and so is the count (it is not lying)", "docs/adr/a003.md" in docs() and (await row())["decisions"] == 2,
           f"{sorted(docs())} {await row()}")
        ck("the log line has no key", any("could not delete a document" in l for l in logs.mine()) and not any(BRAIN_KEY in l for l in logs.lines))
        await ingest(push("acme/web", payload_files=False))
        ck("a later full pass deletes it", "docs/adr/a003.md" not in docs() and (await row())["decisions"] == 1, f"{sorted(docs())} {await row()}")

        print("== a dropped folder deletes its documents ==")
        r = await cx.put("/api/brain/repos/acme/web/folders", json={"folders": ["docs/adr", "docs/other"]})
        await rd.wait()
        ck("docs/other is chosen and imported", sorted(docs()) == ["docs/adr/a001.md", "docs/other/x.md"], str(sorted(docs())))
        await cx.put("/api/brain/repos/acme/api/folders", json={"folders": ["docs/adr"]})
        await rd.wait()
        ck("acme/api has its own two documents", sorted(docs("acme/api")) == ["docs/adr/p000.md", "docs/adr/p001.md"], str(sorted(docs("acme/api"))))
        r = await cx.put("/api/brain/repos/acme/web/folders", json={"folders": ["docs/adr"]})
        await rd.wait()
        ck("the dropped folder's document is gone, the kept one stays", sorted(docs()) == ["docs/adr/a001.md"], str(sorted(docs())))
        ck("the other repo's documents are untouched", len(docs("acme/api")) == 2)
        r = await cx.put("/api/brain/repos/acme/web/folders", json={"folders": []})
        await rd.wait()
        ck("every folder dropped: no document of the repo is left", docs() == {} and (await row())["decisions"] == 0, str(sorted(docs())))
        ck("acme/api still has its two", len(docs("acme/api")) == 2 and (await row("acme/api"))["decisions"] == 2)
        n = calls()
        await ingest(push("acme/web", modified=["docs/adr/a001.md"]))
        ck("a push to a repo with no chosen folder costs nothing", calls() == n)

        print("== what does not count as a push of the branch ==")
        await cx.put("/api/brain/repos/acme/web/folders", json={"folders": ["docs/adr"]})
        await rd.wait()
        gh.repos["acme/web"]["docs/adr/a001.md"] = "# a1\nfifth version\n"
        n = calls()
        await ingest(push("acme/web", modified=["docs/adr/a001.md"], branch="dev"))
        ck("a push to another branch", calls() == n)
        await ingest(push("acme/web", modified=["docs/adr/a001.md"]), source="hook")
        ck("a push seen by a source that is not the App", calls() == n)
        await ingest(push("acme/web", modified=["docs/adr/a001.md"]))
        ck("the push of the default branch from the App does it", docs()["docs/adr/a001.md"]["text"] == "# a1\nfifth version\n")

        print("== a folder dropped while the cell was down goes at start-up ==")
        stale = "acme/web:docs/old/o000.md"
        c = lore.client()
        await c.put_documents(SOURCE, [{"external_id": stale, "title": "old", "text": "# old\n", "kind": "decision",
                                       "metadata": {"repo": "acme/web", "decided_by": ["Ann A"], "decided_at": "2024-01-01", "where": "repo_folder"}}])
        ck("the stale document is in lore", (SOURCE, stale) in lore.documents)
        fresh = dr.RepoDecisions(store, lambda: lore.client(), rd._token)
        await fresh.startup()
        await fresh.wait()
        ck("start-up read lore and deleted it; the chosen folder's document stays", (SOURCE, stale) not in lore.documents
           and "docs/adr/a001.md" in docs() and fresh.count("acme/web", ["docs/adr"]) == 1,
           f"{sorted(docs())} {fresh.count('acme/web', ['docs/adr'])}")

        print("== a delete that arrives while start-up is still reading lore ends deleted, never put back ==")
        gate = asyncio.Event()

        def slow_client():
            c = lore.client()
            listed = c.list_documents

            async def held_back(**kw):
                await gate.wait()
                return await listed(**kw)
            c.list_documents = held_back
            return c
        del gh.repos["acme/web"]["docs/adr/a001.md"]
        late = dr.RepoDecisions(store, slow_client, rd._token)
        late.startup_task = asyncio.create_task(late.startup())
        await asyncio.sleep(0.1)          # start-up is waiting for lore's list
        dels = len(lore.calls("DELETE", "delete_document"))
        late.on_push("acme/web", {"docs/adr/a001.md"})
        await asyncio.sleep(0.1)
        ck("while lore's list is held back, nothing is deleted yet", "docs/adr/a001.md" in docs()
           and len(lore.calls("DELETE", "delete_document")) == dels, str(len(lore.calls("DELETE", "delete_document")) - dels))
        gate.set()
        await late.wait()
        ck("once the list arrives the document is deleted and the count is 0 (start-up's own pass settles it)", "docs/adr/a001.md" not in docs()
           and late.count("acme/web", ["docs/adr"]) == 0, f"{sorted(docs())} {late.count('acme/web', ['docs/adr'])}")

        print("== a delete that lands while the seed holds a list it already read is not put back ==")
        ghost = "acme/web:docs/adr/ghost.md"
        c = lore.client()
        await c.put_documents(SOURCE, [{"external_id": ghost, "title": "ghost", "text": "# ghost\n", "kind": "decision",
                                       "metadata": {"repo": "acme/web", "decided_by": ["Ann A"], "decided_at": "2024-01-01", "where": "repo_folder"}}])
        read, release = asyncio.Event(), asyncio.Event()

        def late_client():
            c = lore.client()
            listed = c.list_documents

            async def after_read(**kw):
                page = await listed(**kw)    # lore's answer is read (it holds the ghost) ...
                read.set()
                await release.wait()         # ... but reaches the seed only after the delete
                return page
            c.list_documents = after_read
            return c
        seeded = dr.RepoDecisions(store, late_client, rd._token)
        task = asyncio.create_task(seeded._seed())
        await read.wait()
        removed = await seeded._delete("acme/web", ["docs/adr/ghost.md"])
        release.set()
        await task
        ck("the delete went through and the seed's merge did not put the path back", removed == 1 and (SOURCE, ghost) not in lore.documents
           and "docs/adr/ghost.md" not in seeded._held.get("acme/web", set()), f"{removed} {seeded._held}")
        ck("a seed with no delete in flight still fills the held paths", seeded._gone is None)

        print("== a force push reconciles the whole folder ==")
        choose(store, "acme/leave", ["docs/adr"])
        gh.repos["acme/leave"] = {"docs/adr/keep.md": "# keep\nv1\n", "docs/adr/rewritten.md": "# rewritten\nv1\n"}
        rd.request("acme/leave", ["docs/adr"])
        await rd.wait()
        ck("both files are in lore", sorted(docs("acme/leave")) == ["docs/adr/keep.md", "docs/adr/rewritten.md"], str(sorted(docs("acme/leave"))))
        del gh.repos["acme/leave"]["docs/adr/rewritten.md"]     # the rewrite dropped it; the push's commits never name it
        gh.repos["acme/leave"]["docs/adr/keep.md"] = "# keep\nv2\n"
        await ingest(push("acme/leave", modified=["docs/adr/keep.md"]))
        ck("an ordinary push that names only keep.md leaves the dropped file's document (nothing said it was gone)",
           "docs/adr/rewritten.md" in docs("acme/leave") and docs("acme/leave")["docs/adr/keep.md"]["text"] == "# keep\nv2\n")
        gh.repos["acme/leave"]["docs/adr/keep.md"] = "# keep\nv3\n"
        await ingest(push("acme/leave", modified=["docs/adr/keep.md"], forced=True))
        ck("a force push runs the full pass: the dropped file's document is deleted and the count follows",
           "docs/adr/rewritten.md" not in docs("acme/leave") and docs("acme/leave")["docs/adr/keep.md"]["text"] == "# keep\nv3\n"
           and rd.count("acme/leave", ["docs/adr"]) == 1, f"{sorted(docs('acme/leave'))} {rd.count('acme/leave', ['docs/adr'])}")

        print("== an edit that makes a file stop counting deletes its document ==")
        gh.repos["acme/leave"].update({"docs/adr/big.md": "# big\nsmall\n", "docs/adr/blob.md": "# blob\nv1\n", "docs/adr/undated.md": "# undated\nv1\n"})
        rd.request("acme/leave", ["docs/adr"])
        await rd.wait()
        ck("all four are in lore", sorted(docs("acme/leave")) == ["docs/adr/big.md", "docs/adr/blob.md", "docs/adr/keep.md", "docs/adr/undated.md"],
           str(sorted(docs("acme/leave"))))
        gh.repos["acme/leave"]["docs/adr/keep.md"] = "# keep\nv4\n"
        gh.repos["acme/leave"]["docs/adr/big.md"] = "# big\n" + "x" * 100          # now over the size limit
        gh.repos["acme/leave"]["docs/adr/blob.md"] = "# blob\nv2\n"
        gh.hidden.add(sha_of("# blob\nv2\n"))                                     # its blob is gone
        gh.repos["acme/leave"]["docs/adr/undated.md"] = "# undated\nv2\n"
        gh.undated.add("docs/adr/undated.md")                                        # no usable commit date
        dr.MAX_BYTES = 60
        try:
            await ingest(push("acme/leave", modified=["docs/adr/keep.md", "docs/adr/big.md", "docs/adr/blob.md", "docs/adr/undated.md"]))
        finally:
            dr.MAX_BYTES = 1_000_000
        left = docs("acme/leave")
        ck("a file now over the size limit loses its document", "docs/adr/big.md" not in left, str(sorted(left)))
        ck("a file whose blob is gone loses its document", "docs/adr/blob.md" not in left, str(sorted(left)))
        ck("a file with no usable date loses its document", "docs/adr/undated.md" not in left, str(sorted(left)))
        ck("the file that still qualifies is updated, and the count is 1",
           list(left) == ["docs/adr/keep.md"] and left["docs/adr/keep.md"]["text"] == "# keep\nv4\n" and rd.count("acme/leave", ["docs/adr"]) == 1,
           f"{sorted(left)} {rd.count('acme/leave', ['docs/adr'])}")
        gh.hidden.clear()
        gh.undated.clear()

        print("== a file the tree lists over the size limit loses its document ==")
        gh.repos["acme/leave"]["docs/adr/treebig.md"] = "# treebig\nsmall\n"
        rd.request("acme/leave", ["docs/adr"])
        await rd.wait()
        ck("treebig.md is in lore", "docs/adr/treebig.md" in docs("acme/leave"))
        gh.tree_sizes["docs/adr/treebig.md"] = 5_000_000
        await ingest(push("acme/leave", modified=["docs/adr/treebig.md"]))
        ck("a push that finds the tree's size over the limit deletes the document, with no blob read",
           "docs/adr/treebig.md" not in docs("acme/leave") and rd.count("acme/leave", ["docs/adr"]) == 1,
           f"{sorted(docs('acme/leave'))} {rd.count('acme/leave', ['docs/adr'])}")
        gh.tree_sizes.clear()
        del gh.repos["acme/leave"]["docs/adr/treebig.md"]

        print("== a folder dropped while start-up could not read lore's list is pruned once it can ==")
        gh.repos["acme/leave"]["docs/old/x.md"] = "# x\n"
        store.set_brain_folders("acme/leave", ["docs/adr", "docs/old"], None, NOW)
        rd.request("acme/leave", ["docs/old"])
        await rd.wait()
        ck("docs/old/x.md is in lore", "docs/old/x.md" in docs("acme/leave"))
        store.set_brain_folders("acme/leave", ["docs/adr"], None, NOW)      # the folder is dropped with no hook (the cell was down)
        dropped = dr.RepoDecisions(store, lambda: lore.client(), rd._token)
        lore.fail("list_documents", 500, "boom")
        await dropped._seed()
        ck("the failed seed holds nothing", dropped._seed_failed and not dropped._held.get("acme/leave"), str(dropped._held))
        dropped._prune.add("acme/leave")
        dropped._kick("acme/leave")
        await dropped.wait()
        ck("the prune reads lore's list again and deletes the dropped folder's document",
           "docs/old/x.md" not in docs("acme/leave") and not dropped._seed_failed and "docs/adr/keep.md" in docs("acme/leave"),
           f"{sorted(docs('acme/leave'))} {dropped._seed_failed}")

        print("== a start-up that could not read lore's list reads it again before a push deletes ==")
        gh.repos["acme/leave"]["docs/adr/old.md"] = "# old\n"
        rd.request("acme/leave", ["docs/adr"])
        await rd.wait()
        ck("old.md is in lore", "docs/adr/old.md" in docs("acme/leave"))
        del gh.repos["acme/leave"]["docs/adr/old.md"]
        blind = dr.RepoDecisions(store, lambda: lore.client(), rd._token)
        lore.fail("list_documents", 500, "boom")
        await blind._seed()
        ck("the failed seed is remembered and holds nothing", blind._seed_failed and not blind._held.get("acme/leave"), str(blind._held))
        lore.fail("list_documents", 500, "boom")      # the first retry fails too
        blind.on_push("acme/leave", {"docs/adr/old.md"})
        await blind.wait()
        ck("while lore's list cannot be read nothing is deleted, and the next pass tries again",
           "docs/adr/old.md" in docs("acme/leave") and blind._seed_failed)
        blind.on_push("acme/leave", {"docs/adr/old.md"})
        await blind.wait()
        ck("once it can be read, the push deletes the older document", "docs/adr/old.md" not in docs("acme/leave") and not blind._seed_failed
           and blind.count("acme/leave", ["docs/adr"]) == 1, f"{sorted(docs('acme/leave'))} {blind.count('acme/leave', ['docs/adr'])}")

        print("== a push waits for start-up's read of lore before it deletes ==")
        gh.repos["acme/leave"]["docs/adr/late.md"] = "# late\n"
        rd.request("acme/leave", ["docs/adr"])
        await rd.wait()
        ck("late.md is in lore", "docs/adr/late.md" in docs("acme/leave"))
        del gh.repos["acme/leave"]["docs/adr/late.md"]
        hold = asyncio.Event()

        def held_client():
            c = lore.client()
            listed = c.list_documents

            async def after_hold(**kw):
                await hold.wait()
                return await listed(**kw)
            c.list_documents = after_hold
            return c
        waiting = dr.RepoDecisions(store, held_client, rd._token)
        waiting.startup_task = asyncio.create_task(waiting._seed())     # only the read: nothing but the push can delete late.md
        await asyncio.sleep(0.1)
        trees = gh.count("git/trees")
        waiting.on_push("acme/leave", {"docs/adr/late.md"})
        await asyncio.sleep(0.1)
        ck("while lore's list is held back the push has not looked at GitHub", gh.count("git/trees") == trees and "docs/adr/late.md" in docs("acme/leave"),
           f"{gh.count('git/trees') - trees}")
        hold.set()
        await waiting.wait()
        ck("when the list arrives the push deletes the file's document", "docs/adr/late.md" not in docs("acme/leave")
           and waiting.count("acme/leave", ["docs/adr"]) == 1, f"{sorted(docs('acme/leave'))} {waiting.count('acme/leave', ['docs/adr'])}")

        print("== before the licence and without a brain, a push does nothing ==")
        os.environ["TARES_BRAIN_URL"] = ""
        n = calls()
        rd.on_push("acme/web", {"docs/adr/a001.md"})
        await rd.wait()
        ck("no brain: no call", calls() == n)
        os.environ["TARES_BRAIN_URL"] = fake_lore.BASE_URL
        await cx.aclose()


unit()
asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
