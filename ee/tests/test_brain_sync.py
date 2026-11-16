# Tares enterprise licence, see ee/LICENSE
"""Keeping ready repos up to date (TR-1201). A push to a ready repo's default branch syncs it in lore with
a fresh token; a token connection (no App webhooks) is polled for a moved head; several pushes fold into one
more sync while one runs; a push during the first index is synced once the repo is ready; a failed sync keeps
the repo ready, records why, shows it on the card from the third failure in a row and is retried by the next
push or poll. In process against the shared fake lore (ee/tests/fake_lore.py), fake GitHub heads and the
real background task. Events go through core's `Runtime._store_envelopes`, the funnel every ingest uses, built
from the real push mapping (`github_events.build`).

Covered: the push filter (default branch only, the App source only, ready repos only), the fresh token, the
collapse, the pending push, the failure count and its card, the retry, the poll (ETag, App repos skipped,
failures), the licence and brain settings gates, no token in any row, answer or log line, the listener hook
in core, the task stopped with the app.

In process, no ports. With CI's Postgres run (TARES_TEST_PG and tests/pg_harness on PYTHONPATH) the same
script keeps its state in Postgres.

Run: .venv/bin/python ee/tests/test_brain_sync.py
"""
import asyncio, logging, os, sys, tempfile, time
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings, puts the repo, ee/ and tests/ on sys.path
licensed.install_test_root()

TMP = tempfile.mkdtemp(prefix="tares-brain-sync-")
ROOT = "root-token-" + "brain-sync"
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_AUTH_TOKEN"] = ROOT

import httpx

import fake_lore
from tares import github_credentials
from tares.config import SourceCfg
from tares.connectors import github_events as gh
from tares.envelope import Envelope, now_utc
from tares_ee.brain import index as index_mod
from tares_ee.brain import repos as repos_mod
from tares_ee.brain import sync as sync_mod
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
T1 = "plain-" + "q7w8e9" * 5          # a token no shape rule knows: only the explicit scrub can hide it
T2 = "ghs_" + "A1b2C3" * 6            # the rotated token, a GitHub shape
TA = "late-" + "m4n5b6" * 5           # the App's installation token
TOKENS = (T1, T2, TA)
SHA = [c * 40 for c in "abcdef12"]   # eight different commit ids (hex, as git makes them)


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
    print("== the head check ==")
    sent = []

    def make(status, body="", headers=None, boom=None):
        def handler(request):
            sent.append(request)
            if boom:
                raise boom
            return httpx.Response(status, text=body, headers=headers or {})
        return lambda **kw: real(transport=httpx.MockTransport(handler), **kw)

    real = sync_mod.httpx.AsyncClient

    def run(status, *known, body="", headers=None, boom=None):
        sync_mod.httpx.AsyncClient = make(status, body, headers, boom)
        try:
            return asyncio.run(sync_mod.github_head("http://gh.test/", T1, "acme/web", *known))
        except ValueError as e:
            return e
        finally:
            sync_mod.httpx.AsyncClient = real

    got = run(200, body=SHA[0].upper() + "\n", headers={"etag": '"e1"'})
    ck("a 200 gives the head, lowercase, and the etag", got == (SHA[0], '"e1"'), str(got))
    r = sent[-1]
    ck("... asked for HEAD of the repo, for the sha only, with the token, not a cached copy", (
        str(r.url) == "http://gh.test/repos/acme/web/commits/HEAD" and r.headers["accept"] == "application/vnd.github.sha"
        and r.headers["authorization"] == "Bearer " + T1 and "if-none-match" not in r.headers), f"{r.url} {dict(r.headers)}")
    got = run(304, (SHA[1], '"e2"'))
    ck("a 304 gives back what was known", got == (SHA[1], '"e2"'), str(got))
    ck("... and the known etag was sent", sent[-1].headers["if-none-match"] == '"e2"')
    ck("a 304 with nothing known is refused in words", isinstance(run(304), ValueError))
    for status, word in ((401, "rejected the token"), (403, "refused the check"), (429, "refused the check"),
                         (404, "cannot find acme/web"), (409, "no commits")):
        e = run(status)
        ck(f"{status} is plain words", isinstance(e, ValueError) and word in str(e) and T1 not in str(e), str(e))
    for bad in ("", "not a sha", "<html>", "abc"):
        ck(f"an answer that is not a commit id ({bad!r}) is refused", isinstance(run(200, body=bad), ValueError))
    e = run(200, boom=httpx.ConnectError("could not connect with " + T1))
    ck("a network error says its type and nothing else", isinstance(e, ValueError) and "ConnectError" in str(e)
       and T1 not in str(e), str(e))

    print("== the row and its two new fields ==")
    from tares.store import Store
    s = Store(os.path.join(TMP, "unit.duckdb"))
    s.add_brain_repo("acme/web", "src-1", None, "2026-01-01T00:00:00Z")
    r = s.get_brain_repo("acme/web")
    ck("a new row has no sync failures and no sync error", (r["sync_failures"], r["sync_error"]) == (0, None), str(r))
    s.update_brain_repo("acme/web", {"state": "ready", "sync_failures": 2, "sync_error": "boom"})
    r = s.get_brain_repo("acme/web")
    ck("both can be set", (r["sync_failures"], r["sync_error"]) == (2, "boom"), str(r))
    s.update_brain_repo("acme/web", {"state": "failed"})
    s.requeue_brain_repo("acme/web")
    r = s.get_brain_repo("acme/web")
    ck("a retry clears both", (r["state"], r["sync_failures"], r["sync_error"]) == ("queued", 0, None), str(r))
    ck("the listing carries them", [(x["sync_failures"], x["sync_error"]) for x in s.list_brain_repos()] == [(0, None)])
    ck("the defaults are three failures and a one minute poll", (sync_mod.SHOW_AFTER, sync_mod.SYNC_POLL_SECONDS) == (3, 60.0))


async def main():
    from tares.daemon import make_app
    sync_mod.TICK_SECONDS = 0.05
    sync_mod.SYNC_POLL_SECONDS = 3600.0   # the polls below are asked for by hand; one scenario starts its own task
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

    names = ("web", "api", "docs", "pend", "late", "bad", "fail", "unk", "tok", "tok2", "tok3", "flip", "gone", "lic", "nosha", "off")
    visible = {"appcred": [], "setup": []}
    tokens = {"appcred": TA, "setup": T1}
    for n in ("web", "api", "docs", "pend", "late", "bad", "fail", "unk", "gone", "lic", "nosha", "off"):
        visible["appcred"].append(f"acme/{n}")
    for n in ("tok", "tok2", "tok3", "flip"):
        visible["setup"].append(f"acme/{n}")

    async def listing(cred, query=""):
        return [{"full_name": n} for n in visible[cred["name"]]]

    async def fresh_token(store, ref, repo=None, min_life=None):
        if ref not in tokens:
            raise ValueError(f"GitHub credential {ref!r} not found")
        return tokens[ref]
    github_credentials.list_repos_for = listing
    github_credentials.get_token = fresh_token

    app = make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=H(ROOT))
        store, runtime = app.state.store, app.state.runtime
        r = await cx.put("/api/licence", json={"key": licensed.test_licence()})
        ck("the licence is stored", r.status_code == 200, r.text[:200])
        store.upsert_github_credential("appcred", "", kind="app", config={"app_id": 7})
        store.upsert_github_credential("setup", T1)
        syncer = app.state.brain_syncer
        runtime.catalog.sources["github"] = SourceCfg(name="github", type="event_stream", connector="github_app", poll_seconds=0)
        runtime.catalog.sources["hook"] = SourceCfg(name="hook", type="event_stream", connector="webhook", poll_seconds=0)

        def row(repo):
            return store.get_brain_repo(repo) or {}

        def make_repo(repo, state="ready", commit=SHA[0]):
            sid = "id-" + repo.replace("/", "-")
            lore.repos[sid] = {"id": sid, "name": repo.replace("/", "_"), "indexed_commit": commit,
                               "web_url_template": None, "remote_url": None}
            store.add_brain_repo(repo, sid, None, now_utc())
            store.update_brain_repo(repo, {"state": state, "indexed_commit": commit, "files_done": 5, "files_total": 5,
                                           "last_synced": now_utc() - timedelta(days=1)})
            return sid

        def push(repo, sha, default=True, source_event="push", branch=None):
            payload = {"ref": "refs/heads/" + (branch or ("main" if default else "dev")), "after": sha,
                       "commits": [{"id": sha}], "head_commit": {"message": "change"},
                       "repository": {"full_name": repo, "default_branch": "main"}, "pusher": {"name": "kim"}}
            b = gh.build("push", payload)
            return Envelope(source="github", source_type="event_stream", key_value=b["key"], event_type=source_event,
                            text=b["text"], event_time=b["event_time"], labels=b["labels"])

        async def ingest(*envs, source="github"):
            await runtime._store_envelopes(source, list(envs))

        def syncs(sid=None):
            return [c for c in lore.calls("POST", "sync_repo") if sid is None or c.path == f"/v1/repos/{sid}/sync"]

        def clean(label):
            everything = repr([row(r["repo"]) for r in store.list_brain_repos()]) + "\n".join(logs.lines)
            ck(label, not any(t in everything for t in TOKENS), "a token is in a row or a log line")

        ck("the push events carry the labels the filter reads", push("acme/web", SHA[1]).labels.get("on_default_branch") == "true"
           and push("acme/web", SHA[1], default=False).labels.get("on_default_branch") == "false"
           and push("acme/web", SHA[1]).labels.get("repo") == "acme/web", str(push("acme/web", SHA[1]).labels))
        ck("the cell has one syncer and its task, listening to core's events", syncer is not None and syncer.task is not None
           and not syncer.task.done() and syncer.on_envelopes in runtime.envelope_listeners)
        syncer2 = repos_mod.syncing.Syncer(syncer.indexer, runtime)
        syncer2.attach(app)
        ck("attaching a second syncer to the app is refused: one task, one listener", app.state.brain_syncer is syncer
           and runtime.envelope_listeners.count(syncer.on_envelopes) == 1 and syncer2.on_envelopes not in runtime.envelope_listeners)

        print("== a push to the default branch syncs a ready repo ==")
        sid_web = make_repo("acme/web")
        before = row("acme/web")
        t0 = time.monotonic()
        lore.sync_report["indexed_commit"] = SHA[1]
        lore.sync_report["files_indexed"], lore.sync_report["files_unchanged"] = 3, 40
        await ingest(push("acme/web", SHA[1]))
        ok = await until(lambda: row("acme/web")["indexed_commit"] == SHA[1])
        took = time.monotonic() - t0
        r = row("acme/web")
        ck("the repo is synced and its commit, time and file count are updated", ok and r["state"] == "ready" and r["last_synced"] != before["last_synced"]
           and (r["files_done"], r["files_total"]) == (43, 43), str(r))
        ck("... well inside two minutes", took < 5, f"{took:.1f}s")
        ck("lore was told once, with the GitHub App's token for a repo only the App reads", len(syncs(sid_web)) == 1
           and syncs(sid_web)[0].json == {"token": TA}, str([c.json for c in syncs()]))
        ck("no error, no failures", (r["error"], r["sync_error"], r["sync_failures"]) == (None, None, 0), str(r))
        ck("the cell's own poller and webhook for lore are not asked: only sync was called", {c.route for c in lore.requests} <= {"sync_repo"},
           str({c.route for c in lore.requests}))
        tokens["appcred"] = T2   # the installation token is renewed between two pushes
        lore.sync_report["indexed_commit"] = SHA[2]
        await ingest(push("acme/web", SHA[2]))
        await until(lambda: len(syncs(sid_web)) == 2 and row("acme/web")["indexed_commit"] == SHA[2])
        ck("the next push asks for a token again and syncs again", [c.json for c in syncs(sid_web)] == [{"token": TA}, {"token": T2}],
           str([c.json for c in syncs(sid_web)]))
        make_repo("acme/nosha")
        env = push("acme/nosha", SHA[3])
        env.labels.pop("sha")
        lore.sync_report["indexed_commit"] = SHA[3]
        await ingest(env)
        ck("... a push without a sha label syncs the repo", await until(lambda: row("acme/nosha")["indexed_commit"] == SHA[3]), str(row("acme/nosha")))
        clean("no token in a row or a log line so far")

        print("== pushes that must not sync ==")
        make_repo("acme/off")
        n = len(syncs())
        await ingest(push("acme/off", SHA[4], default=False))
        await ingest(push("acme/unknown", SHA[4]))
        await ingest(push("acme/off", SHA[4], source_event="pull_request"))
        await ingest(push("acme/off", SHA[4]), source="hook")
        await ingest(push("acme/off", SHA[4]), source="no-such-source")
        await ingest()
        odd = push("acme/off", SHA[4])
        odd.labels["repo"] = 5
        await ingest(odd)
        odd2 = push("acme/off", SHA[4])
        odd2.labels = {}
        await ingest(odd2)
        make_repo("acme/bad", state="failed")
        await ingest(push("acme/bad", SHA[4]))
        await quiet()
        ck("not the default branch, not on the brain, not a push, not the App's source, no events, bad labels, a failed repo: no sync",
           len(syncs()) == n, f"{len(syncs()) - n} extra")
        ck("... and the failed repo is still failed, untouched", row("acme/bad")["state"] == "failed" and row("acme/bad")["sync_failures"] == 0)
        ck("... and nothing is remembered for them", not any(r in syncer._want for r in ("acme/off", "acme/unknown", "acme/bad")), str(syncer._want))

        print("== several pushes while one sync runs fold into one more ==")
        sid_api, sid_docs = make_repo("acme/api"), make_repo("acme/docs")
        gates, started, running, peak = {sid_api: asyncio.Event()}, [], {}, {}

        def gated():
            async def handler(request):
                path = request.url.path
                if request.method == "POST" and path.endswith("/sync"):
                    rid = path.split("/")[-2]
                    started.append(rid)
                    running[rid] = running.get(rid, 0) + 1
                    peak[rid] = max(peak.get(rid, 0), running[rid])
                    try:
                        if rid in gates:
                            await gates[rid].wait()
                        return lore.handle(request)
                    finally:
                        running[rid] -= 1
                return lore.handle(request)
            return LoreClient(fake_lore.BASE_URL, lore.key, transport=httpx.MockTransport(handler))
        repos_mod.lore_client = gated
        n_api = len(syncs(sid_api))
        lore.sync_report["indexed_commit"] = SHA[5]
        await ingest(push("acme/api", SHA[3]))
        ck("the first push starts a sync", await until(lambda: started.count(sid_api) == 1), str(started))
        for sha in (SHA[4], SHA[5], SHA[6]):
            await ingest(push("acme/api", sha))
        await quiet(0.3)
        ck("three more pushes start nothing while it runs", started.count(sid_api) == 1 and peak[sid_api] == 1, str(started))
        await ingest(push("acme/docs", SHA[3]))
        ck("another repo syncs meanwhile: one stuck repo does not hold the others", await until(lambda: row("acme/docs")["indexed_commit"] == SHA[5]),
           str(row("acme/docs")))
        gates[sid_api].set()
        ck("when it ends the three pushes become one more sync", await until(lambda: started.count(sid_api) == 2), str(started))
        await quiet(0.4)
        ck("... and that is all: two syncs for four pushes, never two at once", started.count(sid_api) == 2 and peak[sid_api] == 1
           and len(syncs(sid_api)) - n_api == 2, f"{started} {peak}")
        ck("the repo is synced to the newest head", row("acme/api")["indexed_commit"] == SHA[5])
        repos_mod.lore_client = lore.client

        print("== a push while the repo is still indexing is synced once it is ready ==")
        sid_pend = make_repo("acme/pend", state="indexing")
        n = len(syncs())
        lore.sync_report["indexed_commit"] = SHA[7]
        await ingest(push("acme/pend", SHA[6]))
        await ingest(push("acme/pend", SHA[7]))
        await quiet()
        ck("while indexing nothing is synced and the row is left alone", len(syncs()) == n and row("acme/pend")["state"] == "indexing"
           and row("acme/pend")["indexed_commit"] == SHA[0], str(row("acme/pend")))
        store.update_brain_repo("acme/pend", {"state": "ready"})
        ck("once ready it is synced", await until(lambda: len(syncs(sid_pend)) >= 1 and row("acme/pend")["indexed_commit"] == SHA[7]), str(row("acme/pend")))
        await quiet(0.3)
        ck("... once, for the latest head only", len(syncs(sid_pend)) == 1 and syncer._head["acme/pend"] == SHA[7], str(len(syncs(sid_pend))))
        sid_late = make_repo("acme/late", state="queued")
        await ingest(push("acme/late", SHA[6]))
        store.update_brain_repo("acme/late", {"state": "failed"})
        await quiet()
        ck("a repo that fails while its push waits is dropped, not synced", not syncs(sid_late) and row("acme/late")["state"] == "failed")
        make_repo("acme/gone", state="indexing")
        await ingest(push("acme/gone", SHA[6]))
        store.delete_brain_repo("acme/gone")
        syncer.wake()
        ck("... nothing is kept for it", await until(lambda: "acme/gone" not in syncer._want), str(syncer._want))

        print("== a failed sync keeps the repo ready, counts, shows on the card, and is retried ==")
        make_repo("acme/fail")

        async def card():
            return next(x for x in (await cx.get("/api/brain")).json()["repos"] if x["repo"] == "acme/fail")

        lore.sync_report["indexed_commit"] = SHA[1]
        lore.fail("sync_repo", 502, "the index is busy")
        await ingest(push("acme/fail", SHA[1]))
        await until(lambda: row("acme/fail")["sync_failures"] == 1)
        r = row("acme/fail")
        ck("one failure: still ready, counted, reason kept", r["state"] == "ready" and r["sync_failures"] == 1 and "the index is busy" in (r["sync_error"] or "")
           and "acme/fail" in r["sync_error"], str(r))
        ck("... not on the card yet", r["error"] is None and r["indexed_commit"] == SHA[0], str(r))
        n = len(lore.calls("POST", "sync_repo"))
        await quiet(0.5)
        ck("it does not try again by itself (the next push or poll does)", len(lore.calls("POST", "sync_repo")) == n)
        lore.fail("sync_repo", 502, "the index is busy")
        await ingest(push("acme/fail", SHA[2]))
        await until(lambda: row("acme/fail")["sync_failures"] == 2)
        ck("two in a row: counted, still not on the card", (row("acme/fail")["sync_failures"], row("acme/fail")["error"]) == (2, None), str(row("acme/fail")))
        real_token = syncer.indexer.github_token

        async def broken_token(repo):
            raise RuntimeError("socket gone, header was Bearer " + T1)
        syncer.indexer.github_token = broken_token
        await ingest(push("acme/fail", SHA[3]))
        await until(lambda: row("acme/fail")["sync_failures"] == 3)
        syncer.indexer.github_token = real_token
        r = row("acme/fail")
        ck("three in a row: the card shows it, the repo is still ready and its old commit still answers", r["state"] == "ready" and r["error"]
           and r["error"] == r["sync_error"] and r["indexed_commit"] == SHA[0], str(r))
        ck("an error of unknown kind says nothing of its text", T1 not in r["error"] and "socket" not in r["error"] and "acme/fail" in r["error"], r["error"])
        ck("... its type alone is logged", any("RuntimeError" in l and "acme/fail" in l for l in logs.lines))
        c = await card()
        ck("GET /api/brain shows the count, the reason and the error", (c["state"], c["sync_failures"], c["error"], c["sync_error"]) ==
           ("ready", 3, r["error"], r["sync_error"]), str(c))
        lore.fail("sync_repo", 500, f"bad token {T2} in the body")
        await ingest(push("acme/fail", SHA[4]))
        await until(lambda: row("acme/fail")["sync_failures"] == 4)
        ck("the fourth keeps the card and a token in lore's words is hidden", row("acme/fail")["error"] and T2 not in row("acme/fail")["error"]
           and "[hidden]" in row("acme/fail")["error"], row("acme/fail")["error"])
        class LeakyLore:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def sync_repo(self, source, token=None):
                raise ValueError("lore said no to " + (token or ""))
        real_lore = syncer.indexer._lore
        syncer.indexer._lore = lambda: LeakyLore()
        tokens["appcred"] = TA   # a token no shape rule knows: only the explicit scrub can hide it
        await ingest(push("acme/fail", SHA[4]))
        await until(lambda: row("acme/fail")["sync_failures"] == 5)
        syncer.indexer._lore = real_lore
        tokens["appcred"] = T2
        ck("a token in the words of an error from the sync call is hidden", "lore said no to" in row("acme/fail")["sync_error"]
           and TA not in row("acme/fail")["sync_error"] and TA not in (row("acme/fail")["error"] or ""), str(row("acme/fail")["sync_error"]))
        lore.sync_report["indexed_commit"] = SHA[4]
        await ingest(push("acme/fail", SHA[4]))
        await until(lambda: row("acme/fail")["indexed_commit"] == SHA[4])
        r = row("acme/fail")
        ck("a success clears the failures, the reason and the card", (r["sync_failures"], r["sync_error"], r["error"], r["state"]) == (0, None, None, "ready"), str(r))
        c = await card()
        ck("GET /api/brain agrees", (c["sync_failures"], c["sync_error"], c["error"]) == (0, None, None), str(c))
        visible["appcred"].remove("acme/fail")
        await ingest(push("acme/fail", SHA[5]))
        await until(lambda: row("acme/fail")["sync_failures"] == 1)
        ck("a repo no GitHub connection reads any more is a failure in words, not a crash", "No GitHub connection" in (row("acme/fail")["sync_error"] or "")
           and row("acme/fail")["state"] == "ready", str(row("acme/fail")))
        visible["appcred"].append("acme/fail")
        clean("no token in any row or log line after the failures")

        print("== a failure is only written to a repo that is still ready ==")
        for state in ("indexing", "failed"):
            repo = "acme/guard-" + state
            make_repo(repo, state=state)
            store.update_brain_repo(repo, {"sync_failures": 2})
            syncer._failed(repo, "the index is busy")
            r = row(repo)
            ck(f"a failure that lands after the repo moved to {state} changes nothing", (r["state"], r["sync_failures"], r["sync_error"], r["error"])
               == (state, 2, None, None), str(r))
        make_repo("acme/guard-ready")
        store.update_brain_repo("acme/guard-ready", {"sync_failures": 2})
        syncer._failed("acme/guard-ready", "the index is busy")
        r = row("acme/guard-ready")
        ck("... while a ready repo counts it and shows it from the third", (r["sync_failures"], r["sync_error"], r["error"]) ==
           (3, "the index is busy", "the index is busy"), str(r))

        print("== an empty head is not remembered as the head last synced ==")
        make_repo("acme/guard-head")
        syncer._succeeded("acme/guard-head", {}, "")
        ck("a sync that names no commit and was asked for none leaves nothing to compare a poll to", "acme/guard-head" not in syncer._synced,
           str(syncer._synced))
        syncer._succeeded("acme/guard-head", {"indexed_commit": SHA[2]}, "")
        ck("... the commit lore reports is kept", syncer._synced.get("acme/guard-head") == SHA[2], str(syncer._synced))
        syncer._succeeded("acme/guard-head", {}, SHA[3])
        ck("... and so is the head a push named", syncer._synced.get("acme/guard-head") == SHA[3], str(syncer._synced))

        print("== a push event is noted without reading the store ==")
        make_repo("acme/loop", state="indexing")   # waits for ready: no sync starts
        reads = []
        real_get = store.get_brain_repo
        store.get_brain_repo = lambda repo: (reads.append(repo), real_get(repo))[1]
        try:
            syncer.on_envelopes("github", [push("acme/loop", SHA[1]), push("acme/not-on-the-brain", SHA[1])])
        finally:
            store.get_brain_repo = real_get
        ck("the listener reads nothing from the store: it runs on the event loop", reads == [], str(reads))
        ck("... it noted both pushes for the task", {"acme/loop", "acme/not-on-the-brain"} <= set(syncer._want), str(syncer._want))
        ck("... and the task drops the repo that is not on the brain", await until(lambda: "acme/not-on-the-brain" not in syncer._want),
           str(syncer._want))
        ck("... while request() still answers from the store", syncer.request("acme/not-on-the-brain") is False and syncer.request("acme/loop") is True
           and "acme/not-on-the-brain" not in syncer._want)
        for gone in ("acme/loop", "acme/guard-indexing", "acme/guard-failed", "acme/guard-ready", "acme/guard-head"):
            store.delete_brain_repo(gone)
            syncer._forget(gone)

        print("== the poll: token connections, no webhooks ==")
        heads, head_calls, head_fail = {}, [], []

        async def fake_head(api, token, repo, known=None):
            head_calls.append((repo, token, known))
            if head_fail:
                raise ValueError(head_fail[0])
            return heads[repo], '"etag-' + heads[repo][:4] + '"'
        sync_mod.github_head = fake_head
        sid_tok = make_repo("acme/tok", commit=SHA[0])
        sid_tok2 = make_repo("acme/tok2", commit=SHA[0])
        make_repo("acme/tok3", state="indexing", commit=SHA[0])
        heads.update({"acme/tok": SHA[0], "acme/tok2": SHA[0], "acme/tok3": SHA[0]})
        n_web = len(syncs(sid_web))
        looked = await syncer.poll()
        ck("a poll looks at the ready token-connection repos only: no App repo, no repo that is still indexing", looked == 2
           and sorted(c[0] for c in head_calls) == ["acme/tok", "acme/tok2"] and len(syncs(sid_web)) == n_web, f"{looked} {head_calls}")
        ck("... with the token connection's token", all(c[1] == T1 for c in head_calls))
        await quiet(0.3)
        ck("the head is the one indexed: nothing is synced", not syncs(sid_tok) and not syncs(sid_tok2))
        head_calls.clear()
        heads["acme/tok"] = SHA[1]
        lore.sync_report["indexed_commit"] = SHA[1]
        await syncer.poll()
        ck("a head that moved syncs that repo and only it", await until(lambda: row("acme/tok")["indexed_commit"] == SHA[1]) and not syncs(sid_tok2),
           str([c.path for c in syncs()][-3:]))
        ck("... with a fresh token", syncs(sid_tok)[-1].json == {"token": T1})
        ck("the poll had remembered the etag of its first look and sent it", head_calls[0][2] == (SHA[0], '"etag-' + SHA[0][:4] + '"'), str(head_calls[0]))
        head_calls.clear()
        await syncer.poll()
        await quiet(0.3)
        ck("the next poll asks with the etag and, with the head synced, syncs nothing", {c[2] for c in head_calls if c[0] == "acme/tok"} ==
           {(SHA[1], '"etag-' + SHA[1][:4] + '"')} and len(syncs(sid_tok)) == 1, str(head_calls))
        heads["acme/tok"] = SHA[2]
        lore.fail("sync_repo", 502, "lore is busy")
        await syncer.poll()
        await until(lambda: row("acme/tok")["sync_failures"] == 1)
        ck("a poll that finds a moved head and fails to sync keeps the repo ready and counted", row("acme/tok")["state"] == "ready"
           and row("acme/tok")["indexed_commit"] == SHA[1] and "lore is busy" in row("acme/tok")["sync_error"], str(row("acme/tok")))
        lore.sync_report["indexed_commit"] = SHA[2]
        await syncer.poll()
        ck("the next poll tries again, though GitHub would answer 304, and it works", await until(lambda: row("acme/tok")["indexed_commit"] == SHA[2]
           and row("acme/tok")["sync_failures"] == 0), str(row("acme/tok")))
        head_fail.append("GitHub rejected the token (401)")
        await syncer.poll()
        await syncer.poll()
        ck("a head check that fails is counted like a failed sync, the repo stays ready", row("acme/tok2")["sync_failures"] == 2
           and "could not be checked" in row("acme/tok2")["sync_error"] and "401" in row("acme/tok2")["sync_error"]
           and row("acme/tok2")["error"] is None and row("acme/tok2")["state"] == "ready", str(row("acme/tok2")))
        await syncer.poll()
        ck("... three in a row show on the card", row("acme/tok2")["error"] and row("acme/tok2")["sync_failures"] == 3, str(row("acme/tok2")))
        head_fail.clear()
        await syncer.poll()
        ck("a check that works with the index current clears them", (row("acme/tok2")["sync_failures"], row("acme/tok2")["sync_error"],
           row("acme/tok2")["error"]) == (0, None, None), str(row("acme/tok2")))
        visible["setup"].remove("acme/tok2")
        await syncer.poll()
        ck("a repo no connection reads is a counted failure in words", row("acme/tok2")["sync_failures"] == 1 and "can read acme/tok2" in row("acme/tok2")["sync_error"],
           str(row("acme/tok2")))
        visible["setup"].append("acme/tok2")
        clean("no token in a row or a log line after the polls")

        print("== the poll starts on its own and stops with the task ==")
        make_repo("acme/flip", commit=SHA[0])
        heads["acme/flip"] = SHA[3]
        lore.sync_report["indexed_commit"] = SHA[3]
        sync_mod.SYNC_POLL_SECONDS = 0.1
        own = repos_mod.syncing.Syncer(syncer.indexer, None)
        own.task = asyncio.create_task(own.run())
        ck("a syncer's own task polls and syncs the moved head", await until(lambda: row("acme/flip")["indexed_commit"] == SHA[3]), str(row("acme/flip")))
        own.task.cancel()
        try:
            await own.task
        except asyncio.CancelledError:
            pass
        sync_mod.SYNC_POLL_SECONDS = 3600.0
        ck("... and ends when cancelled", own.task.done())

        print("== an idle task sleeps: no brain, or a step that fails, never spins ==")
        sync_mod.TICK_SECONDS = 0.05
        idle = repos_mod.syncing.Syncer(syncer.indexer, None)
        looks = []
        idle.active = lambda: looks.append(1) or False
        idle.task = asyncio.create_task(idle.run())
        await asyncio.sleep(0.6)
        idle.task.cancel()
        try:
            await idle.task
        except asyncio.CancelledError:
            pass
        ck("a task with the brain off looks once, not every 50 ms", len(looks) <= 2, str(len(looks)))
        broken = repos_mod.syncing.Syncer(syncer.indexer, None)
        steps = []

        def boom():
            steps.append(1)
            raise RuntimeError("no")
        broken.active = lambda: True
        broken._start_tasks = boom
        broken.poll = lambda: asyncio.sleep(0)
        broken.task = asyncio.create_task(broken.run())
        await asyncio.sleep(0.6)
        broken.task.cancel()
        try:
            await broken.task
        except asyncio.CancelledError:
            pass
        ck("a step that raises is tried once, then after a poll interval", len(steps) <= 2, str(len(steps)))

        print("== the licence and the brain settings ==")
        make_repo("acme/lic")
        n = len(syncs())
        r = await cx.put("/api/licence", json={"key": licensed.test_licence(features=("other",))})
        await ingest(push("acme/lic", SHA[6]))
        await syncer.poll()
        await quiet()
        ck("no licence: a push syncs nothing and a poll asks nothing", len(syncs()) == n and row("acme/lic")["indexed_commit"] == SHA[0], str(row("acme/lic")))
        r = await cx.put("/api/licence", json={"key": licensed.test_licence()})
        ck("... a licence again and the next push syncs", await ingest(push("acme/lic", SHA[6])) is None
           and await until(lambda: len(syncs()) > n), f"{len(syncs()) - n}")
        n = len(syncs())
        saved = os.environ.pop("TARES_BRAIN_KEY")
        await ingest(push("acme/lic", SHA[6]))
        await syncer.poll()
        await quiet()
        ck("no brain settings: nothing is synced or polled", len(syncs()) == n)
        os.environ["TARES_BRAIN_KEY"] = saved

        print("== core's hook ==")
        seen = []

        def boom(source, envs):
            raise RuntimeError("listener broke with " + T1)
        runtime.envelope_listeners.insert(0, boom)
        runtime.envelope_listeners.append(lambda source, envs: seen.append((source, len(envs))))
        await ingest(push("acme/off", SHA[4], default=False))
        ck("a listener that raises does not fail the ingest and the next one still runs", seen == [("github", 1)], str(seen))
        await ingest()
        ck("an empty batch calls no listener", seen == [("github", 1)])
        runtime.envelope_listeners.remove(boom)
        clean("no token anywhere at the end")

        print("== it stops with the app ==")
        task = syncer.task
    ck("the syncer's task ended with the app", task.done() and syncer.task is None)
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    unit()
    asyncio.run(main())
