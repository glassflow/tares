# Tares enterprise licence, see ee/LICENSE
"""A failed sync of a repo the GitHub App covers is retried on a back-off (TR-1510). The App's repos have no
poll (A25), so before this a failed sync waited for the next push, which on a quiet repo is days, with the
error on the card the whole time.

Covered: the first retry after RETRY_FIRST_SECONDS and not before; the wait doubling with each failure in a
row up to RETRY_CAP_SECONDS; a push syncing at once while backing off; a success clearing the failure and the
back-off; a removed repo, a failed one and one that has no failures stopping the retry; a token repo having no
back-off and keeping its poll; a repo whose failure this run did not see (a restart) being retried; the push
listeners hearing the push once and nothing on a retry; the retry asking lore with no GitHub head and no token
in any row or log line.

TR-1513 adds: with the brain off (no licence, or no lore) a retry that is due does not wake the task every
50 ms, and runs at once when the brain is back; a due retry for a repo with no failures, one already
syncing and one still indexing is dropped without asking lore; an App repo a token connection now reads
loses its back-off and its place in the App's repos.

In process, no ports, against the shared fake lore (ee/tests/fake_lore.py). The syncer's clock is a fake one the
test moves, so a half hour of back-off takes no real time; the real background task does the retrying.

Run: .venv/bin/python ee/tests/test_brain_sync_retry.py
"""
import asyncio, logging, os, sys, tempfile, time
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings, puts the repo, ee/ and tests/ on sys.path
licensed.install_test_root()

TMP = tempfile.mkdtemp(prefix="tares-brain-sync-retry-")
ROOT = "root-token-" + "brain-retry"
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_AUTH_TOKEN"] = ROOT

import httpx

import fake_lore
from tares import github_credentials
from tares import github_events as gh
from tares.envelope import Envelope, now_utc
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


BRAIN_KEY = "brain-" + "key-retry-1"   # gitleaks:allow
TA = "late-" + "m4n5b6" * 5           # the App's installation token
T1 = "plain-" + "q7w8e9" * 5          # a token connection's token
TOKENS = (TA, T1)
SHA = [c * 40 for c in "abcdef12"]


class Logs(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


async def until(cond, timeout=6.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False


async def quiet(seconds=0.4):
    await asyncio.sleep(seconds)


def unit():
    print("== the back-off ladder ==")
    ladder = [sync_mod.retry_delay(n) for n in range(1, 9)]
    ck("a minute, doubling, capped at half an hour", ladder == [60, 120, 240, 480, 960, 1800, 1800, 1800], str(ladder))
    ck("no failures counts as one, and a huge count stays at the cap", (sync_mod.retry_delay(0), sync_mod.retry_delay(10 ** 9)) == (60, 1800))
    ck("the defaults are one minute and half an hour", (sync_mod.RETRY_FIRST_SECONDS, sync_mod.RETRY_CAP_SECONDS) == (60.0, 1800.0))



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
    sync_mod.TICK_SECONDS = 0.05
    sync_mod.SYNC_POLL_SECONDS = 3600.0   # the polls below are asked for by hand
    logs = Logs()
    logging.getLogger().addHandler(logs)
    logging.getLogger().setLevel(logging.DEBUG)
    lore = fake_lore.FakeLore()
    repos_mod.lore_client = lore.client
    os.environ["TARES_BRAIN_URL"] = fake_lore.BASE_URL
    os.environ["TARES_BRAIN_KEY"] = BRAIN_KEY

    async def never(self):                # the indexer's own poll is not under test here
        await asyncio.Event().wait()
    index_mod.Indexer.run = never

    visible = {"appcred": ["acme/app", "acme/push", "acme/gone", "acme/dead", "acme/ok", "acme/old", "acme/timer", "acme/spin"], "setup": ["acme/tok"]}
    tokens = {"appcred": TA, "setup": T1}

    async def listing(cred, query=""):
        return [{"full_name": n} for n in visible[cred["name"]]]

    async def fresh_token(store, ref, repo=None, min_life=None):
        return tokens[ref]
    github_credentials.list_repos_for = listing
    github_credentials.get_token = fresh_token

    heads = {}     # token repo -> the head GitHub says

    async def fake_head(api, token, repo, known=None):
        return heads[repo], '"e"'
    sync_mod.github_head = fake_head

    app = make_app()
    async with app.router.lifespan_context(app):
        store = app.state.store
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=H(ROOT))
        r = await cx.put("/api/licence", json={"key": licensed.test_licence()})
        ck("the licence is stored", r.status_code == 200, r.text[:200])
        store.upsert_github_credential("appcred", "", kind="app", config={"app_id": 7})
        store.upsert_github_credential("setup", T1)
        syncer = app.state.brain_syncer

        clock = [1000.0]
        syncer._clock = lambda: clock[0]
        heard = []
        syncer.push_listeners.append(lambda repo, paths: heard.append((repo, paths)))

        def advance(seconds):
            clock[0] += seconds
            syncer.wake()

        def row(repo):
            return store.get_brain_repo(repo) or {}

        def make_repo(repo, state="ready", commit=SHA[0], failures=0):
            sid = "id-" + repo.replace("/", "-")
            lore.repos[sid] = {"id": sid, "name": repo.replace("/", "_"), "indexed_commit": commit,
                               "web_url_template": None, "remote_url": None}
            store.add_brain_repo(repo, sid, None, now_utc())
            store.update_brain_repo(repo, {"state": state, "indexed_commit": commit, "files_done": 5, "files_total": 5,
                                           "last_synced": now_utc() - timedelta(days=1), "sync_failures": failures,
                                           "sync_error": "earlier" if failures else None})
            return sid

        def push(repo, sha):
            payload = {"ref": "refs/heads/main", "after": sha, "commits": [{"id": sha, "modified": ["a.md"], "added": [], "removed": []}],
                       "head_commit": {"message": "change"}, "repository": {"full_name": repo, "default_branch": "main"},
                       "pusher": {"name": "kim"}}
            b = gh.build("push", payload)
            return Envelope(source="github", source_type="event_stream", key_value=b["key"], event_type="push",
                            text=b["text"], event_time=b["event_time"], labels=b["labels"], payload=payload)

        async def ingest(*envs):
            await app_delivery(store, envs)

        def syncs(sid):
            return [c for c in lore.calls("POST", "sync_repo") if c.path == f"/v1/repos/{sid}/sync"]

        def down(n):   # lore refuses the next n syncs
            for _ in range(n):
                lore.fail("sync_repo", 503, "the brain is down")

        print("== the first retry comes after a minute, not before ==")
        sid = make_repo("acme/app")
        lore.sync_report["indexed_commit"] = SHA[1]
        down(1)
        await ingest(push("acme/app", SHA[1]))
        ck("the push's sync fails once", await until(lambda: row("acme/app")["sync_failures"] == 1), str(row("acme/app")))
        ck("the repo stays ready on its old commit", (row("acme/app")["state"], row("acme/app")["indexed_commit"]) == ("ready", SHA[0]))
        ck("a retry is planned a minute from now", syncer._retry_at.get("acme/app") == clock[0] + 60, str(syncer._retry_at))
        ck("the listeners heard the push once, with its files", heard == [("acme/app", {"a.md"})], str(heard))
        n = len(syncs(sid))
        advance(59)
        await quiet()
        ck("59 seconds on, lore is not asked again", len(syncs(sid)) == n and row("acme/app")["sync_failures"] == 1, str(row("acme/app")))
        advance(1)
        ok = await until(lambda: row("acme/app")["indexed_commit"] == SHA[1])
        ck("at the minute the repo is retried and caught up, with no push", ok and len(syncs(sid)) == n + 1, str(row("acme/app")))
        r = row("acme/app")
        ck("a success clears the failure, the error and the back-off", (r["sync_failures"], r["sync_error"], r["error"]) == (0, None, None)
           and "acme/app" not in syncer._retry_at, str((r, syncer._retry_at)))
        ck("the retry asked lore with the App's token, once", syncs(sid)[-1].json == {"token": TA}, str(syncs(sid)[-1].json))
        ck("the retry told the listeners nothing: it is not a push", heard == [("acme/app", {"a.md"})], str(heard))
        advance(7200)
        await quiet()
        ck("a repo with no failure is not retried, however long it waits", len(syncs(sid)) == n + 1)

        print("== the wait doubles with each failure in a row, up to the cap ==")
        sid = make_repo("acme/dead")
        syncer._app.add("acme/dead")
        down(1)
        await ingest(push("acme/dead", SHA[2]))
        await until(lambda: row("acme/dead")["sync_failures"] == 1)
        gaps = []
        for failures in range(1, 8):
            planned = syncer._retry_at.get("acme/dead")
            gap = None if planned is None else planned - clock[0]
            gaps.append(gap)
            if gap is None:
                break
            before = len(syncs(sid))
            down(1)
            advance(gap - 1)
            await quiet(0.25)
            if len(syncs(sid)) != before:
                gaps.append("early")
                break
            advance(1)
            if not await until(lambda: row("acme/dead")["sync_failures"] == failures + 1):
                gaps.append("no retry")
                break
        ck("a minute, two, four, eight, sixteen, then half an hour twice", gaps == [60, 120, 240, 480, 960, 1800, 1800], str(gaps))
        ck("... each wait went by without a retry and was then retried", row("acme/dead")["sync_failures"] == 8, str(row("acme/dead")))
        ck("the card shows the error from the third failure in a row", row("acme/dead")["error"] is not None)
        lore.sync_report["indexed_commit"] = SHA[2]
        advance(sync_mod.RETRY_CAP_SECONDS)
        await until(lambda: row("acme/dead")["indexed_commit"] == SHA[2])
        ck("... the repo is on the pushed commit, clean, with nothing planned", (row("acme/dead")["sync_failures"], row("acme/dead")["error"]) == (0, None)
           and "acme/dead" not in syncer._retry_at, str(row("acme/dead")))

        print("== a push syncs at once, while a retry is waiting ==")
        sid = make_repo("acme/push")
        syncer._app.add("acme/push")
        lore.sync_report["indexed_commit"] = SHA[3]
        down(1)
        await ingest(push("acme/push", SHA[3]))
        await until(lambda: row("acme/push")["sync_failures"] == 1)
        planned = syncer._retry_at.get("acme/push")
        n = len(syncs(sid))
        await ingest(push("acme/push", SHA[3]))
        ok = await until(lambda: row("acme/push")["indexed_commit"] == SHA[3])
        ck("the push synced without the clock moving", ok and clock[0] < planned and len(syncs(sid)) == n + 1, str(row("acme/push")))
        ck("... and it ended the back-off", "acme/push" not in syncer._retry_at and row("acme/push")["sync_failures"] == 0, str(syncer._retry_at))
        advance(7200)
        await quiet()
        ck("... so the planned retry never fires", len(syncs(sid)) == n + 1)

        print("== a removed or failed repo stops retrying ==")
        sid_gone = make_repo("acme/gone")
        lore.sync_report["indexed_commit"] = SHA[4]
        down(1)
        await ingest(push("acme/gone", SHA[4]))
        await until(lambda: row("acme/gone")["sync_failures"] == 1)
        ck("a retry is planned", "acme/gone" in syncer._retry_at)
        n = len(syncs(sid_gone))
        store.delete_brain_repo("acme/gone")
        advance(60)
        await quiet()
        ck("the repo is removed: no retry, nothing left planned or remembered", len(syncs(sid_gone)) == n and "acme/gone" not in syncer._retry_at
           and "acme/gone" not in syncer._app and "acme/gone" not in syncer._want, str((syncer._retry_at, syncer._want)))

        sid_old = make_repo("acme/old")
        down(1)
        await ingest(push("acme/old", SHA[4]))
        await until(lambda: row("acme/old")["sync_failures"] == 1)
        n = len(syncs(sid_old))
        store.update_brain_repo("acme/old", {"state": "failed", "error": "the index broke"})
        advance(60)
        await quiet()
        ck("the repo goes failed: no retry, and it is left as it is", len(syncs(sid_old)) == n and "acme/old" not in syncer._retry_at
           and row("acme/old")["state"] == "failed", str(row("acme/old")))

        print("== a failure this run did not see (a restart) is retried ==")
        sid = make_repo("acme/ok", failures=4)
        ck("the repo is not known to be the App's yet and nothing is planned", "acme/ok" not in syncer._app and "acme/ok" not in syncer._retry_at)
        n = len(syncs(sid))
        lore.sync_report["indexed_commit"] = SHA[5]
        await syncer.poll()
        ok = await until(lambda: row("acme/ok")["indexed_commit"] == SHA[5])
        ck("the poll sees the App covers it and retries it at once", ok and len(syncs(sid)) == n + 1 and "acme/ok" in syncer._app, str(row("acme/ok")))
        ck("... and the failure is cleared", (row("acme/ok")["sync_failures"], row("acme/ok")["error"]) == (0, None), str(row("acme/ok")))

        print("== a token repo keeps its poll and gets no back-off ==")
        sid = make_repo("acme/tok")
        heads["acme/tok"] = SHA[6]
        lore.sync_report["indexed_commit"] = SHA[6]
        down(1)
        await syncer.poll()
        await until(lambda: row("acme/tok")["sync_failures"] == 1)
        ck("the poll saw a new head and the sync failed", row("acme/tok")["indexed_commit"] == SHA[0], str(row("acme/tok")))
        ck("no back-off is planned and it is not the App's", "acme/tok" not in syncer._retry_at and "acme/tok" not in syncer._app, str(syncer._retry_at))
        n = len(syncs(sid))
        advance(7200)
        await quiet()
        ck("however long it waits, nothing retries it but the poll", len(syncs(sid)) == n)
        await syncer.poll()
        ok = await until(lambda: row("acme/tok")["indexed_commit"] == SHA[6])
        ck("the next poll syncs it again, as before", ok and len(syncs(sid)) == n + 1 and row("acme/tok")["sync_failures"] == 0, str(row("acme/tok")))

        print("== on the real clock the task wakes by itself when the back-off is over ==")
        sid = make_repo("acme/timer")
        syncer._clock = time.monotonic
        sync_mod.RETRY_FIRST_SECONDS = 0.5
        lore.sync_report["indexed_commit"] = SHA[7]
        down(1)
        await ingest(push("acme/timer", SHA[7]))
        began = time.monotonic()
        await until(lambda: row("acme/timer")["sync_failures"] == 1)
        n = len(syncs(sid))
        ok = await until(lambda: row("acme/timer")["indexed_commit"] == SHA[7], timeout=5.0)
        waited = time.monotonic() - began
        ck("nobody woke it: the retry came by itself, not before the wait", ok and len(syncs(sid)) == n + 1 and waited >= 0.45,
           f"{waited:.2f}s {row('acme/timer')}")
        sync_mod.RETRY_FIRST_SECONDS = 60.0

        print("== with the brain off, a retry that is due does not spin the task (TR-1513) ==")
        sid = make_repo("acme/spin", failures=2)
        syncer._app.add("acme/spin")
        syncer._retry_at["acme/spin"] = syncer._clock() - 30   # due, and the brain is off so nothing takes it
        looks = [0]
        real_next = syncer._next_retry

        def counted():
            looks[0] += 1
            return real_next()
        syncer._next_retry = counted
        syncer.active = lambda: False   # the licence is off, or lore is not set up
        syncer.wake()
        await quiet(0.1)
        looks[0] = 0
        await quiet(1.0)
        ck("a second with the retry due: the task looked at most twice, not every 50 ms", looks[0] <= 2, f"{looks[0]} looks")
        n = len(syncs(sid))
        ck("... lore was not asked and the retry is still planned", len(syncs(sid)) == n and "acme/spin" in syncer._retry_at, str(syncer._retry_at))
        del syncer.active
        syncer._next_retry = real_next
        lore.sync_report["indexed_commit"] = SHA[7]
        syncer.wake()
        ok = await until(lambda: row("acme/spin")["sync_failures"] == 0)
        ck("the brain comes back: the due retry runs at once", ok and len(syncs(sid)) == n + 1 and "acme/spin" not in syncer._retry_at, str(row("acme/spin")))

        print("== a due retry with nothing to catch up is dropped without asking lore (TR-1513) ==")
        sid = make_repo("acme/nofail")
        syncer._app.add("acme/nofail")
        syncer._retry_at["acme/nofail"] = syncer._clock() - 1
        want = syncer._want.get("acme/nofail", 0)
        n = len(syncs(sid))
        syncer.wake()
        ok = await until(lambda: "acme/nofail" not in syncer._retry_at)
        await quiet()
        ck("a repo with no failures: the retry is dropped, nothing asked", ok and len(syncs(sid)) == n and syncer._want.get("acme/nofail", 0) == want,
           str((syncer._retry_at, syncer._want.get("acme/nofail"))))
        ck("... and it stays the App's", "acme/nofail" in syncer._app)

        sid = make_repo("acme/busy", failures=1)
        syncer._app.add("acme/busy")
        running = asyncio.create_task(asyncio.Event().wait())   # a sync that is under way for this repo
        syncer._tasks["acme/busy"] = running
        syncer._retry_at["acme/busy"] = syncer._clock() - 1
        want = syncer._want.get("acme/busy", 0)
        n = len(syncs(sid))
        syncer.wake()
        ok = await until(lambda: "acme/busy" not in syncer._retry_at)
        await quiet()
        ck("a repo already syncing: the retry is dropped and no second ask is queued", ok and len(syncs(sid)) == n and syncer._want.get("acme/busy", 0) == want,
           str((syncer._retry_at, syncer._want.get("acme/busy"))))
        running.cancel()
        syncer._tasks.pop("acme/busy", None)

        sid = make_repo("acme/busy2", failures=1)
        syncer._app.add("acme/busy2")
        store.update_brain_repo("acme/busy2", {"state": "indexing"})
        syncer._retry_at["acme/busy2"] = syncer._clock() - 1
        want = syncer._want.get("acme/busy2", 0)
        n = len(syncs(sid))
        syncer.wake()
        ok = await until(lambda: "acme/busy2" not in syncer._retry_at)
        await quiet()
        ck("a repo that is indexing: the retry is dropped and nothing is asked", ok and len(syncs(sid)) == n and syncer._want.get("acme/busy2", 0) == want
           and row("acme/busy2")["state"] == "indexing", str((syncer._retry_at, row("acme/busy2"))))

        print("== an App repo a token now reads loses its back-off (TR-1513) ==")
        visible["setup"].append("acme/turn")
        sid = make_repo("acme/turn", failures=3)
        heads["acme/turn"] = SHA[0]   # the head is the indexed commit: the poll has nothing to sync
        syncer._app.add("acme/turn")
        syncer._retry_at["acme/turn"] = syncer._clock() + 600
        n = len(syncs(sid))
        looked = await syncer._poll_repo(row("acme/turn"))
        ck("the poll looked at it", looked == 1)
        ck("it is no longer the App's and has no back-off planned", "acme/turn" not in syncer._app and "acme/turn" not in syncer._retry_at,
           str((syncer._app, syncer._retry_at)))
        ck("... the poll saw the index current and cleared the failure, with no sync", len(syncs(sid)) == n and row("acme/turn")["sync_failures"] == 0, str(row("acme/turn")))

        everything = repr([row(r["repo"]) for r in store.list_brain_repos()]) + "\n".join(logs.lines)
        ck("no token in a row or a log line", not any(t in everything for t in TOKENS), "a token is in a row or a log line")
        ck("lore was only ever asked to sync", {c.route for c in lore.requests} <= {"sync_repo"}, str({c.route for c in lore.requests}))
        await cx.aclose()


unit()
asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
