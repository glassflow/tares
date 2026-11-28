# Tares enterprise licence, see ee/LICENSE
"""Removing a repo from the brain removes its decision folder files from lore (TR-1453).

A repo's `repo_folder` documents (TR-1210) used to stay in lore, and in the importer's held paths, after the repo
was removed. `RepoDecisions.forget_repo` (called by the removal, and by the start-up pass for a repo that is no
longer connected) deletes them, drops what was queued for the repo and forgets its paths. In process against the
shared fake lore, the fake control plane's licence helpers and a small fake GitHub (an httpx mock transport).

Covered: removing a repo deletes every one of its `repo_folder` documents and leaves other repos', hand decisions
and project decisions alone; the repo is gone from the importer's held paths; a push for the removed repo costs no
GitHub and no lore call and puts nothing back; a fresh start-up after the removal finds nothing to delete; a lore
error part way through leaves the rest held and tells no lie, the removal itself still finishes, and the next
start-up deletes what is left (from what lore holds, so also in a process that never held the paths); a repo that
is still connected is never forgotten, by the call or by start-up; a repo removed while the cell was down has its
documents deleted at the next start-up, also when that start-up could not read lore's list (the next read forgets
it); the removal wakes the project docs before it deletes; a reconnect that lands while the store is checked keeps
its queued import.

In process, no ports. With CI's Postgres run (TARES_TEST_PG and tests/pg_harness on PYTHONPATH) the same
script keeps its state in Postgres.

Run: .venv/bin/python ee/tests/test_brain_decisions_repo_remove.py
"""
import asyncio, base64, hashlib, logging, os, sys, tempfile
from datetime import datetime, timezone
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings, puts the repo, ee/ and tests/ on sys.path
licensed.install_test_root()

TMP = tempfile.mkdtemp(prefix="tares-brain-decisions-remove-")
ROOT = "root-token-" + "brain-decisions-remove"
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_AUTH_TOKEN"] = ROOT

import httpx

import fake_lore
from tares import github_credentials
from tares import github_events as gh_events
from tares.envelope import Envelope
from tares_ee.brain import decisions as project_docs_mod
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
            return httpx.Response(200, json={"tree": [{"path": p, "type": "blob", "sha": sha_of(files[p])} for p in shown],
                                             "truncated": self.truncated})
        if rest.startswith("git/blobs/"):
            want = rest[len("git/blobs/"):]
            text = next((t for t in files.values() if sha_of(t) == want), None)
            if text is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"encoding": "base64", "size": len(text),
                                             "content": base64.encodebytes(text.encode()).decode()})
        if rest.endswith("/pulls"):
            return httpx.Response(200, json=[])
        p = unquote(q.get("path", ""))
        c = {"sha": "c-" + p, "commit": {"author": {"name": "Ann A", "date": "2024-01-01T00:00:00Z"},
                                         "committer": {"name": "Ann A", "date": "2024-01-01T00:00:00Z"}},
             "author": {"login": "ann"}}
        return httpx.Response(200, json=[c] if p in files else [])



async def app_delivery(store, envs, source="github"):
    """What the GitHub App's webhook route does with a delivery's events (daemon `_ingest_github`): store
    them, then tell core's GitHub hub (github_webhook.after_store). Events of any other source are only
    stored, as on every other ingest route."""
    from tares import github_webhook
    await asyncio.to_thread(store.append, list(envs))
    if envs and source == "github":
        github_webhook.after_store(store, source, list(envs))


async def main():
    from tares.daemon import make_app
    logs = Logs()
    logging.getLogger().addHandler(logs)
    logging.getLogger().setLevel(logging.DEBUG)
    lore = fake_lore.FakeLore()
    repos_mod.lore_client = lore.client
    gh = Gh()
    adr = {f"docs/adr/a{i:03d}.md": f"# a{i}\nfirst\n" for i in range(3)}
    for repo in ("acme/web", "acme/api", "acme/old", "acme/half", "acme/keep", "acme/race", "acme/gone"):
        gh.repos[repo] = {**adr, "README.md": "# readme\n"}
    dr.TRANSPORT = httpx.MockTransport(gh.handle)

    async def never(self):
        await asyncio.Event().wait()
    index_mod.Indexer.run = never
    sync_mod.Syncer.run = never
    # A removal wakes the project docs loop, which lists lore at a time of its own; a late listing would be
    # counted against a later step, or take a failure meant for the importer. Nothing here needs its passes.
    project_docs_mod.Decisions.run = never

    async def readable_repos(cred, query=""):
        return [{"full_name": r} for r in gh.repos]
    github_credentials.list_repos_for = readable_repos

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
        store.upsert_github_credential("setup", GH_TOKEN)

        def push(repo, modified=()):
            sha = "1" * 40
            payload = {"ref": "refs/heads/main", "after": sha, "commits": [{"id": sha, "added": [], "modified": list(modified), "removed": []}],
                       "head_commit": {"message": "change"}, "repository": {"full_name": repo, "default_branch": "main"}, "pusher": {"name": "kim"}}
            b = gh_events.build("push", payload)
            return Envelope(source="github", source_type="event_stream", key_value=b["key"], event_type="push",
                            text=b["text"], event_time=b["event_time"], labels=b["labels"], payload=payload)

        def docs(repo):
            return {e[len(repo) + 1:] for (s, e) in lore.documents if s == SOURCE and e.startswith(repo + ":")}

        async def row(repo):
            r = await cx.get("/api/brain")
            assert r.status_code == 200, r.text
            return next((x for x in r.json()["repos"] if x["repo"] == repo), None)

        def calls():
            return len(gh.requests), len(lore.requests)

        r = await cx.put("/api/licence", json={"key": licensed.test_licence()})
        ck("the licence is stored", r.status_code == 200, r.text[:200])
        os.environ["TARES_BRAIN_URL"] = fake_lore.BASE_URL
        os.environ["TARES_BRAIN_KEY"] = BRAIN_KEY

        for repo in ("acme/web", "acme/api", "acme/keep"):
            choose(store, repo, [])
            r = await cx.put(f"/api/brain/{'repos/' + repo}/folders", json={"folders": ["docs/adr"]})
            assert r.status_code == 200, r.text
        await rd.wait()
        ck("three repos imported, three documents each", all(len(docs(r)) == 3 for r in ("acme/web", "acme/api", "acme/keep")),
           str({r: sorted(docs(r)) for r in ("acme/web", "acme/api")}))
        ck("the rows say 3", (await row("acme/web"))["decisions"] == 3 and (await row("acme/api"))["decisions"] == 3)

        c = lore.client()
        meta = {"repo": "acme/web", "decided_by": ["Ann A"], "decided_at": "2024-01-01"}
        await c.put_documents("decisions", [{"external_id": "hand-1", "title": "by hand", "text": "# hand\n", "kind": "decision", "metadata": dict(meta, where="by_hand")}])
        await c.put_documents("factory_projects", [{"external_id": "proj-1", "title": "project", "text": "# proj\n", "kind": "decision", "metadata": dict(meta, where="factory_project")}])

        print("== removing a repo deletes its decision folder files ==")
        r = await cx.delete("/api/brain/repos/acme/web")
        ck("the removal answers 200", r.status_code == 200, r.text[:200])
        ck("no repo_folder document of the repo is left in lore", docs("acme/web") == set(), str(sorted(docs("acme/web"))))
        ck("the other repos' documents are untouched", len(docs("acme/api")) == 3 and len(docs("acme/keep")) == 3)
        ck("the hand decision and the project decision are untouched",
           ("decisions", "hand-1") in lore.documents and ("factory_projects", "proj-1") in lore.documents)
        ck("the importer holds no path of the repo, and the row is gone", "acme/web" not in rd._held and await row("acme/web") is None, str(rd._held.keys()))
        ck("the other repo's count is unchanged", (await row("acme/api"))["decisions"] == 3)

        print("== a push for the removed repo puts nothing back ==")
        n = calls()
        await app_delivery(store, [push("acme/web", ["docs/adr/a000.md"])])
        await rd.wait()
        ck("no GitHub and no lore call, and still nothing in lore", calls() == n and docs("acme/web") == set(), f"{calls()} {n}")

        print("== start-up after the removal finds nothing to delete ==")
        fresh = dr.RepoDecisions(store, lambda: lore.client(), rd._token)
        dels = len(lore.calls("DELETE", "delete_document"))
        await fresh.startup()
        await fresh.wait()
        ck("no repo_folder document of the removed repo, no delete, the others stay",
           docs("acme/web") == set() and len(lore.calls("DELETE", "delete_document")) == dels
           and len(docs("acme/api")) == 3 and "acme/web" not in fresh._held, f"{sorted(docs('acme/web'))}")
        ck("the hand and project decisions are still there", ("decisions", "hand-1") in lore.documents and ("factory_projects", "proj-1") in lore.documents)

        print("== a repo that is still connected is never forgotten ==")
        dels = len(lore.calls("DELETE", "delete_document"))
        ck("forget_repo on a connected repo deletes nothing", await rd.forget_repo("acme/keep") == 0
           and len(lore.calls("DELETE", "delete_document")) == dels and len(docs("acme/keep")) == 3)
        await fresh.startup()
        await fresh.wait()
        ck("start-up keeps it too", len(docs("acme/keep")) == 3 and fresh.count("acme/keep", ["docs/adr"]) == 3)

        print("== a lore error part way: the rest stays held, the removal finishes, the next start-up deletes it ==")
        lore.fail("delete_document", 500, "boom " + BRAIN_KEY)
        r = await cx.delete("/api/brain/repos/acme/api")
        ck("the removal still answers 200 and the row is gone", r.status_code == 200 and await row("acme/api") is None, r.text[:200])
        ck("the documents the failed delete could not reach are still in lore, and still held", len(docs("acme/api")) == 3
           and len(rd._held.get("acme/api", ())) == 3, f"{sorted(docs('acme/api'))} {rd._held.get('acme/api')}")
        ck("the log says so and holds no key", any("still in lore" in l for l in logs.mine()) and not any(BRAIN_KEY in l for l in logs.lines))
        late = dr.RepoDecisions(store, lambda: lore.client(), rd._token)   # a process that never held the paths: lore's list is the source
        await late.startup()
        await late.wait()
        ck("the next start-up deletes what was left", docs("acme/api") == set() and "acme/api" not in late._held, str(sorted(docs("acme/api"))))
        ck("and the connected repo is still whole", len(docs("acme/keep")) == 3)
        await rd.startup()
        await rd.wait()
        ck("the first process's own start-up pass settles its held paths too", "acme/api" not in rd._held)

        print("== a repo removed while the cell was down ==")
        choose(store, "acme/old", [])
        await cx.put("/api/brain/repos/acme/old/folders", json={"folders": ["docs/adr"]})
        await rd.wait()
        ck("the repo's three documents are in lore", len(docs("acme/old")) == 3)
        store.delete_brain_repo("acme/old")   # the row goes with no hook
        down = dr.RepoDecisions(store, lambda: lore.client(), rd._token)
        await down.startup()
        await down.wait()
        ck("start-up deletes them", docs("acme/old") == set() and (await row("acme/keep"))["decisions"] == 3, str(sorted(docs("acme/old"))))

        print("== the wake comes before the deletes ==")
        order = []
        real_wake, real_forget = app.state.brain_decisions.wake, rd.forget_repo

        def wake():
            order.append("wake")
            real_wake()

        async def forget(repo):
            order.append("forget")
            return await real_forget(repo)
        app.state.brain_decisions.wake, rd.forget_repo = wake, forget
        choose(store, "acme/half", [])
        await cx.put("/api/brain/repos/acme/half/folders", json={"folders": ["docs/adr"]})
        await rd.wait()
        r = await cx.delete("/api/brain/repos/acme/half")
        app.state.brain_decisions.wake, rd.forget_repo = real_wake, real_forget
        ck("the removal wakes the project docs before it deletes the repo's documents", r.status_code == 200 and order == ["wake", "forget"], str(order))

        print("== a reconnect that lands while forget_repo checks the store keeps its import ==")
        choose(store, "acme/race", [])
        await cx.put("/api/brain/repos/acme/race/folders", json={"folders": ["docs/adr"]})
        await rd.wait()
        ck("acme/race has three documents", len(docs("acme/race")) == 3)
        store.delete_brain_repo("acme/race")
        real_get, loop, lying = store.get_brain_repo, asyncio.get_running_loop(), {"on": True}

        async def reconnect():
            lying["on"] = False
            choose(store, "acme/race", ["docs/adr"])
            rd.request("acme/race", ["docs/adr"])

        def stale(repo):
            if lying["on"] and repo == "acme/race":
                asyncio.run_coroutine_threadsafe(reconnect(), loop).result()
                return None   # the answer from before the reconnect
            return real_get(repo)
        store.get_brain_repo = stale
        was, seed_gate = rd.startup_task, asyncio.Event()
        rd.startup_task = asyncio.create_task(seed_gate.wait())   # start-up's read of lore is still running: the import waits for it
        try:
            await rd.forget_repo("acme/race")
            seed_gate.set()
            await rd.wait()
        finally:
            store.get_brain_repo = real_get
            rd.startup_task = was
        ck("the import the reconnect asked for still runs, so the repo has its documents", len(docs("acme/race")) == 3, str(sorted(docs("acme/race"))))
        ck("and the count follows", rd.count("acme/race", ["docs/adr"]) == 3, str(rd.count("acme/race", ["docs/adr"])))

        print("== a repo removed while start-up could not read lore is forgotten once a pass reads the list ==")
        choose(store, "acme/gone", [])
        await cx.put("/api/brain/repos/acme/gone/folders", json={"folders": ["docs/adr"]})
        await rd.wait()
        ck("acme/gone has three documents", len(docs("acme/gone")) == 3)
        store.delete_brain_repo("acme/gone")   # the row goes with no hook
        blind = dr.RepoDecisions(store, lambda: lore.client(), rd._token)
        lore.fail("list_documents", 500, "boom")
        await blind._seed()
        ck("the failed seed holds nothing, so nothing is deleted yet", blind._seed_failed and not blind._held and len(docs("acme/gone")) == 3, str(blind._held))
        blind.on_push("acme/keep", {"docs/adr/a000.md"})   # a pass for a connected repo reads lore's list again
        await blind.wait()
        ck("that read forgets the removed repo: its documents are deleted, the connected repo's stay",
           docs("acme/gone") == set() and not blind._seed_failed and "acme/gone" not in blind._held and len(docs("acme/keep")) == 3,
           f"{sorted(docs('acme/gone'))} {blind._seed_failed}")

        await cx.aclose()


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
