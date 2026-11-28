# Tares enterprise licence, see ee/LICENSE
"""The decision importer retries a push lore did not take (TR-1514).

`RepoDecisions._drain` takes a push's files off its queue before it imports them. When lore was down or answered
an error for that import, the files were gone from the queue and the documents of the push stayed stale until
another push, a restart or a folder change. The importer now keeps the files of a push whose put or delete lore
did not take and tries them again on a back-off: a minute after the first failure, doubling with each one in a
row up to half an hour (the syncer's TR-1510 steps), with no new push. In process against the shared fake lore
(ee/tests/fake_lore.py) and a small fake GitHub; the retry's sleep is replaced by a gate the test opens.

Covered: lore fails during a push's import, comes back, and the push's documents arrive with no new push (its blob was cached by the failed try); while lore stays down the
waits go 60, 120, ... and stop at 1800, then start over after a success; a connection error counts like an error answer; a failed delete is retried; two failing pushes
share one timer and one retry; a push that succeeds meanwhile leaves the files of the failed one waiting; a full
pass (no usable file list) that failed is retried as a full pass and dominates the files named; a later full pass
that succeeds clears the retry and its timer; a refusal (422) is no retry; a repo removed, and a folder dropped,
end the retry (the folder drop with no GitHub or lore call); no licence or brain when the timer fires waits one
more step; GitHub (a tree error, a rate limit) or the token failing while a retry runs keeps the files for the next
step, and a first try only GitHub cut short is no retry; an error that is not a lore answer counts; a failed delete of a dropped
folder does not turn a later push into a retry; a push whose import succeeds the first time sets no timer; the shutdown
cancels the timer and clears the back-off.

Run: .venv/bin/python ee/tests/test_brain_decisions_repo_retry.py
"""
import asyncio, base64, hashlib, os, sys, tempfile
from datetime import datetime, timezone
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings, puts the repo, ee/ and tests/ on sys.path
licensed.install_test_root()

TMP = tempfile.mkdtemp(prefix="tares-brain-decisions-retry-")
ROOT = "root-token-" + "brain-decisions-retry"
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
GH_TOKEN = "gh-token-" + "for-the-retry-tests"       # gitleaks:allow
SOURCE = "repo_decisions"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
REPO = "acme/web"


def sha_of(text):
    return hashlib.sha1(text.encode()).hexdigest()


class Gh:
    """GitHub's tree, blob and commit routes for repos whose files are {path: text}. Counts every call."""

    def __init__(self):
        self.repos = {}
        self.requests = []
        self.tree_fails = []   # statuses the next tree reads answer, one each

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
            if self.tree_fails:
                return httpx.Response(self.tree_fails.pop(0), json={"message": "down"})
            return httpx.Response(200, json={"tree": [{"path": p, "type": "blob", "sha": sha_of(files[p])} for p in files],
                                             "truncated": False})
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
    lore = fake_lore.FakeLore()
    repos_mod.lore_client = lore.client
    gh = Gh()
    adr = {f"docs/adr/a{i:03d}.md": f"# a{i}\nfirst\n" for i in range(3)}
    gh.repos[REPO] = {**adr, "docs/other/x.md": "# x\n", "README.md": "# readme\n"}
    dr.TRANSPORT = httpx.MockTransport(gh.handle)

    async def never(self):
        await asyncio.Event().wait()
    index_mod.Indexer.run = never
    sync_mod.Syncer.run = never
    # A repo removal wakes the project docs loop, which lists lore itself: its calls would be counted here.
    project_docs_mod.Decisions.run = never

    async def readable_repos(cred, query=""):
        return [{"full_name": REPO}]
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

        seq = [0]

        def push(repo, added=(), modified=(), removed=(), payload_files=True):
            seq[0] += 1
            sha = f"{seq[0]:040x}"
            lst = [{"id": sha, "added": list(added), "modified": list(modified), "removed": list(removed)}]
            if not payload_files:
                lst = [{"id": sha}]
            payload = {"ref": "refs/heads/main", "after": sha, "commits": lst, "head_commit": {"message": "change"},
                       "repository": {"full_name": repo, "default_branch": "main"}, "pusher": {"name": "kim"}}
            b = gh_events.build("push", payload)
            return Envelope(source="github", source_type="event_stream", key_value=b["key"], event_type="push",
                            text=b["text"], event_time=b["event_time"], labels=b["labels"], payload=payload)

        async def ingest(*envs):
            await app_delivery(store, envs)
            await rd.wait()

        def docs(repo=REPO):
            return {e[len(repo) + 1:]: d for (s, e), d in lore.documents.items() if s == SOURCE and e.startswith(repo + ":")}

        # The retry's sleep: every wait is noted and held until the test opens its gate.
        sleeps, gates = [], []

        async def gated(delay):
            sleeps.append(delay)
            g = asyncio.get_running_loop().create_future()
            gates.append(g)
            await g
        rd._sleep = gated

        def reset():
            sleeps.clear()
            gates.clear()

        async def fire():
            """The wait of the running timer ends; the retry runs to its end."""
            g = next(g for g in gates if not g.done())
            g.set_result(None)
            await asyncio.sleep(0.05)
            await rd.wait()

        def idle():
            return not rd._retry and not rd._retry_n and not rd._retry_tasks and not rd.retry_in

        def down(n=1, status=500):
            for _ in range(n):
                lore.fail("put_documents", status, "boom " + BRAIN_KEY)

        r = await cx.put("/api/licence", json={"key": licensed.test_licence()})
        ck("the licence is stored", r.status_code == 200, r.text[:200])
        os.environ["TARES_BRAIN_URL"] = fake_lore.BASE_URL
        os.environ["TARES_BRAIN_KEY"] = BRAIN_KEY

        choose(store, REPO, [])
        r = await cx.put(f"/api/brain/repos/{REPO}/folders", json={"folders": ["docs/adr"]})
        await rd.wait()
        ck("the folder's three documents are in lore", sorted(docs()) == sorted(adr), str(sorted(docs())))
        ck("a first import that succeeds sets no timer", idle() and sleeps == [], f"{rd._retry} {sleeps}")

        print("== a push while lore fails, then lore comes back ==")
        gh.repos[REPO]["docs/adr/a003.md"] = "# a3\nnew\n"
        down()
        await ingest(push(REPO, added=["docs/adr/a003.md"]))
        ck("lore did not take the file: its document is not there", "docs/adr/a003.md" not in docs(), str(sorted(docs())))
        ck("the file is kept and a retry waits a minute", rd._retry == {REPO: {"docs/adr/a003.md"}} and sleeps == [60.0]
           and rd.retry_in == {REPO: 60.0}, f"{rd._retry} {sleeps}")
        trees, blobs = gh.count("git/trees"), gh.count("git/blobs")
        await fire()
        ck("with no new push the document arrives", docs().get("docs/adr/a003.md", {}).get("text") == "# a3\nnew\n", str(sorted(docs())))
        ck("the retry read one tree and no blob (the failed try had cached the file's blob, the rest came from the cache)",
           gh.count("git/trees") - trees == 1 and gh.count("git/blobs") - blobs == 0, f"{gh.count('git/trees') - trees} {gh.count('git/blobs') - blobs}")
        ck("a success clears the retry, the count and the timer", idle(), f"{rd._retry} {rd._retry_n} {rd._retry_tasks}")
        reset()

        print("== lore stays down: the waits double to the cap ==")
        gh.repos[REPO]["docs/adr/a004.md"] = "# a4\nnew\n"
        lore.raise_next("put_documents", httpx.ConnectError("lore is down"))   # a connection error counts like an error answer
        down(7)
        await ingest(push(REPO, added=["docs/adr/a004.md"]))
        for _ in range(7):
            await fire()
        ck("a minute, then twice as long each time, then half an hour and no longer",
           sleeps == [60.0, 120.0, 240.0, 480.0, 960.0, 1800.0, 1800.0, 1800.0], str(sleeps))
        ck("the document is still not there and the file is still kept", "docs/adr/a004.md" not in docs()
           and rd._retry == {REPO: {"docs/adr/a004.md"}}, f"{sorted(docs())} {rd._retry}")
        await fire()
        ck("lore is back: the next try imports it", "docs/adr/a004.md" in docs() and idle() and len(sleeps) == 8,
           f"{sorted(docs())} {sleeps}")
        reset()
        gh.repos[REPO]["docs/adr/a005.md"] = "# a5\nnew\n"
        down()
        await ingest(push(REPO, added=["docs/adr/a005.md"]))
        ck("after a success the back-off starts over at a minute", sleeps == [60.0], str(sleeps))
        await fire()
        ck("and that retry imports it", "docs/adr/a005.md" in docs() and idle(), str(sorted(docs())))
        reset()

        print("== a delete lore did not take is retried ==")
        del gh.repos[REPO]["docs/adr/a005.md"]
        lore.fail("delete_document", 500, "boom " + BRAIN_KEY)
        await ingest(push(REPO, removed=["docs/adr/a005.md"]))
        ck("the document stays and a retry waits", "docs/adr/a005.md" in docs() and sleeps == [60.0], f"{sorted(docs())} {sleeps}")
        await fire()
        ck("the retry deletes it", "docs/adr/a005.md" not in docs() and idle(), str(sorted(docs())))
        reset()

        print("== two failing pushes share one timer; a push that succeeds meanwhile does not clear the others ==")
        for n in "678":
            gh.repos[REPO][f"docs/adr/a00{n}.md"] = f"# a{n}\nnew\n"
        down(2)
        await ingest(push(REPO, added=["docs/adr/a006.md"]))
        await ingest(push(REPO, added=["docs/adr/a007.md"]))
        ck("one timer, both files kept", sleeps == [60.0] and rd._retry == {REPO: {"docs/adr/a006.md", "docs/adr/a007.md"}},
           f"{sleeps} {rd._retry}")
        await ingest(push(REPO, added=["docs/adr/a008.md"]))
        ck("a push lore takes arrives at once and leaves the other two waiting", "docs/adr/a008.md" in docs()
           and "docs/adr/a006.md" not in docs() and rd._retry == {REPO: {"docs/adr/a006.md", "docs/adr/a007.md"}}, f"{sorted(docs())} {rd._retry}")
        await fire()
        ck("the one retry brings both", "docs/adr/a006.md" in docs() and "docs/adr/a007.md" in docs() and idle(), str(sorted(docs())))
        reset()

        print("== a full pass that failed is retried as a full pass ==")
        gh.repos[REPO]["docs/adr/a001.md"] = "# a1\nedited, nobody names it\n"
        gh.repos[REPO]["docs/adr/a009.md"] = "# a9\nnew\n"
        down()
        await ingest(push(REPO, payload_files=False))
        ck("no file list: the whole folder is kept for the retry", rd._retry == {REPO: None} and sleeps == [60.0], f"{rd._retry} {sleeps}")
        down()
        await ingest(push(REPO, added=["docs/adr/a009.md"]))
        ck("a push that names a file does not narrow it", rd._retry == {REPO: None}, str(rd._retry))
        await fire()
        ck("the retry walks the folder: the edit nobody named and the new file arrive", docs()["docs/adr/a001.md"]["text"] == "# a1\nedited, nobody names it\n"
           and "docs/adr/a009.md" in docs() and idle(), f"{sorted(docs())} {rd._retry}")
        reset()

        print("== a later pass that succeeds ends the retry and its timer ==")
        gh.repos[REPO]["docs/adr/a010.md"] = "# a10\nnew\n"
        down()
        await ingest(push(REPO, added=["docs/adr/a010.md"]))
        ck("the file waits", sleeps == [60.0] and "docs/adr/a010.md" not in docs(), f"{sleeps} {sorted(docs())}")
        timer = rd._retry_tasks[REPO]
        await ingest(push(REPO, payload_files=False))
        ck("a full pass that lore takes brings the file", "docs/adr/a010.md" in docs(), str(sorted(docs())))
        await asyncio.sleep(0)
        ck("and nothing is left to retry: no state, the timer is cancelled", idle() and timer.cancelled(), f"{rd._retry} {timer}")
        reset()

        print("== GitHub fails while a retry runs: the retry goes on ==")
        gh.repos[REPO]["docs/adr/a016.md"] = "# a16\nnew\n"
        down()
        await ingest(push(REPO, added=["docs/adr/a016.md"]))
        ck("the file waits a minute", sleeps == [60.0] and rd._retry == {REPO: {"docs/adr/a016.md"}}, f"{sleeps} {rd._retry}")
        gh.tree_fails[:] = [502, 429]
        await fire()
        ck("GitHub answered 502 for the tree: the file is kept and the next step is two minutes", "docs/adr/a016.md" not in docs()
           and sleeps == [60.0, 120.0] and rd._retry == {REPO: {"docs/adr/a016.md"}} and bool(rd._retry_tasks), f"{sleeps} {rd._retry} {rd._retry_tasks}")
        await fire()
        ck("a rate limit the next time: kept again, four minutes", "docs/adr/a016.md" not in docs() and sleeps == [60.0, 120.0, 240.0]
           and rd._retry == {REPO: {"docs/adr/a016.md"}}, f"{sleeps} {rd._retry}")
        await fire()
        ck("GitHub is back: the document arrives and the retry ends", "docs/adr/a016.md" in docs() and idle(), f"{sorted(docs())} {rd._retry}")
        reset()
        gh.repos[REPO]["docs/adr/a017.md"] = "# a17\nnew\n"
        gh.tree_fails[:] = [502]
        await ingest(push(REPO, added=["docs/adr/a017.md"]))
        ck("a first try that only GitHub cut short is no retry (the ticket is about lore)", "docs/adr/a017.md" not in docs() and idle()
           and sleeps == [], f"{rd._retry} {sleeps}")
        await ingest(push(REPO, added=["docs/adr/a017.md"]))
        ck("the next push brings it", "docs/adr/a017.md" in docs() and idle(), str(sorted(docs())))
        reset()

        print("== an error that is not a lore answer counts too ==")
        gh.repos[REPO]["docs/adr/a018.md"] = "# a18\nnew\n"
        real_lore = rd._lore

        def broken():
            rd._lore = real_lore
            raise RuntimeError("not a lore error")
        rd._lore = broken   # the client factory itself fails, once
        await ingest(push(REPO, added=["docs/adr/a018.md"]))
        ck("the file is kept for a retry", sleeps == [60.0] and rd._retry == {REPO: {"docs/adr/a018.md"}}, f"{sleeps} {rd._retry}")
        await fire()
        ck("and the retry brings it", "docs/adr/a018.md" in docs() and idle(), str(sorted(docs())))
        reset()

        print("== no GitHub token while a retry runs: the retry goes on ==")
        gh.repos[REPO]["docs/adr/a019.md"] = "# a19\nnew\n"
        down()
        await ingest(push(REPO, added=["docs/adr/a019.md"]))
        real_token = rd._token

        async def no_token(repo):
            rd._token = real_token
            raise ValueError("no token for this repo")
        rd._token = no_token
        await fire()
        ck("the token could not be read: the file is kept and the next step is two minutes", "docs/adr/a019.md" not in docs()
           and sleeps == [60.0, 120.0] and rd._retry == {REPO: {"docs/adr/a019.md"}}, f"{sleeps} {rd._retry}")

        async def broken_token(repo):
            rd._token = real_token
            raise RuntimeError("the token store broke")
        rd._token = broken_token
        await fire()
        ck("an error that is not a ValueError counts the same: kept, four minutes", "docs/adr/a019.md" not in docs()
           and sleeps == [60.0, 120.0, 240.0] and rd._retry == {REPO: {"docs/adr/a019.md"}}, f"{sleeps} {rd._retry}")
        await fire()
        ck("the token is back: the document arrives", "docs/adr/a019.md" in docs() and idle(), str(sorted(docs())))
        reset()

        print("== a failed delete of a dropped folder is no push's retry ==")
        lore.fail("delete_document", 500, "boom " + BRAIN_KEY)
        await cx.put(f"/api/brain/repos/{REPO}/folders", json={"folders": ["docs/other"]})
        await rd.wait()
        ck("the folder's documents stay (lore failed) and no timer is set", sleeps == [] and idle(), f"{sleeps} {rd._retry}")
        gh.repos[REPO]["docs/other/y.md"] = "# y\nnew\n"
        await ingest(push(REPO, added=["docs/other/y.md"]))
        ck("a later push that lore takes sets no retry for that old failure", "docs/other/y.md" in docs() and idle() and sleeps == [],
           f"{rd._retry} {sleeps} {sorted(docs())}")
        await cx.put(f"/api/brain/repos/{REPO}/folders", json={"folders": ["docs/adr"]})
        await rd.wait()
        reset()

        print("== a refusal is no outage ==")
        gh.repos[REPO]["docs/adr/a011.md"] = "# a11\nrefused\n"
        down(status=422)
        await ingest(push(REPO, added=["docs/adr/a011.md"]))
        ck("lore refused the one document: it stays out and nothing is retried", "docs/adr/a011.md" not in docs() and idle() and sleeps == [],
           f"{rd._retry} {sleeps}")
        reset()

        print("== a repo removed while a retry waits ==")
        gh.repos[REPO]["docs/adr/a012.md"] = "# a12\nnew\n"
        down()
        await ingest(push(REPO, added=["docs/adr/a012.md"]))
        timer = rd._retry_tasks[REPO]
        ck("the file waits", sleeps == [60.0] and REPO in rd._retry, str(sleeps))
        r = await cx.delete(f"/api/brain/repos/{REPO}")
        await rd.wait()
        await asyncio.sleep(0)
        ck("the repo's documents are gone and the retry with them", r.status_code == 200 and docs() == {} and idle() and timer.cancelled(),
           f"{r.status_code} {sorted(docs())} {rd._retry} {timer}")
        reset()

        print("== a folder dropped while a retry waits ==")
        choose(store, REPO, [])
        await cx.put(f"/api/brain/repos/{REPO}/folders", json={"folders": ["docs/adr"]})
        await rd.wait()
        ck("the repo is back with its folder's documents", len(docs()) >= 3 and idle(), str(sorted(docs())))
        gh.repos[REPO]["docs/adr/a013.md"] = "# a13\nnew\n"
        down()
        await ingest(push(REPO, added=["docs/adr/a013.md"]))
        ck("the file waits", sleeps == [60.0] and "docs/adr/a013.md" not in docs(), f"{sleeps} {sorted(docs())}")
        await cx.put(f"/api/brain/repos/{REPO}/folders", json={"folders": ["docs/other"]})
        await rd.wait()
        n_gh, n_lore = len(gh.requests), len(lore.requests)
        await fire()
        ck("the retry finds the folder no longer chosen: it makes no GitHub or lore call and ends",
           (len(gh.requests), len(lore.requests)) == (n_gh, n_lore) and idle() and not any(k.startswith("docs/adr") for k in docs()),
           f"{len(gh.requests) - n_gh} {len(lore.requests) - n_lore} {rd._retry} {sorted(docs())}")
        reset()

        print("== no licence or brain when the timer fires ==")
        await cx.put(f"/api/brain/repos/{REPO}/folders", json={"folders": ["docs/adr", "docs/other"]})
        await rd.wait()
        gh.repos[REPO]["docs/adr/a014.md"] = "# a14\nnew\n"
        down()
        await ingest(push(REPO, added=["docs/adr/a014.md"]))
        ck("the file waits a minute", sleeps == [60.0] and "docs/adr/a014.md" not in docs(), f"{sleeps}")
        os.environ["TARES_BRAIN_URL"] = ""
        n_lore = len(lore.requests)
        await fire()
        ck("no brain: no lore call, and the retry waits the next step", len(lore.requests) == n_lore and sleeps == [60.0, 120.0]
           and rd._retry == {REPO: {"docs/adr/a014.md"}}, f"{sleeps} {rd._retry}")
        os.environ["TARES_BRAIN_URL"] = fake_lore.BASE_URL
        await fire()
        ck("the brain is back: the file arrives", "docs/adr/a014.md" in docs() and idle(), str(sorted(docs())))
        reset()

        print("== the shutdown cancels a waiting timer ==")
        gh.repos[REPO]["docs/adr/a015.md"] = "# a15\nnew\n"
        down()
        await ingest(push(REPO, added=["docs/adr/a015.md"]))
        waiting = rd._retry_tasks[REPO]
        ck("a timer waits", sleeps == [60.0] and not waiting.done(), str(sleeps))
        await cx.aclose()
    ck("when the app shuts down the timer is cancelled and nothing is kept", waiting.cancelled() and idle(),
       f"{waiting} {rd._retry} {rd._retry_tasks} {rd.retry_in}")


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
