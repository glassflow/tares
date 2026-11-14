# Tares enterprise licence, see ee/LICENSE
"""Starting a repo's first index and following it to ready (TR-1198). Connecting a repo asks the control
plane for an index Job; one background task per app follows the Job and lore until the repo is `ready` or
`failed`. Everything runs in process against the shared fake lore and the fake control plane
(ee/tests/fake_lore.py, ee/tests/fake_cp.py), with the real background task polling every 50 ms.

Covered: queued with its place, cloning, indexing with files_done of files_total, done then lore adopts
it with a fresh token and the repo is ready; a refused clone, a control plane that refuses, is down or is
not set up, a repo no GitHub connection reads, a lore error, a Job that went away; Retry; a row connected
before this existed gets its Job from the poll; never two Jobs for one repo; no token in any row, answer
or log line; the task idles with no licence, no brain settings or nothing to follow; one task per app,
stopped with it.

In process, no ports. With CI's Postgres run (TARES_TEST_PG and tests/pg_harness on PYTHONPATH) the same
script keeps its state in Postgres.

Run: .venv/bin/python ee/tests/test_brain_index.py
"""
import asyncio, json, logging, os, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings, puts the repo, ee/ and tests/ on sys.path
licensed.install_test_root()

TMP = tempfile.mkdtemp(prefix="tares-brain-index-")
ROOT = "root-token-" + "brain-index"
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_AUTH_TOKEN"] = ROOT

import httpx

import fake_cp
import fake_lore
from tares import github_credentials
from tares_ee.brain import cp as cp_mod
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
T1 = "plain-" + "q7w8e9" * 5          # a token no shape rule knows: only the explicit scrub can hide it
T2 = "ghs_" + "A1b2C3" * 6            # the rotated token, a GitHub shape
T3 = "late-" + "m4n5b6" * 5           # a third, again no known shape
TOKENS = (T1, T2, T3)


class Logs(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


def unit():
    print("== the pieces ==")
    from tares.store import Store
    s = Store(os.path.join(TMP, "unit.duckdb"))
    s.add_brain_repo("acme/web", "src-1", None, "2026-01-01T00:00:00Z")
    ck("update_brain_repo changes the fields it is given", s.update_brain_repo("acme/web", {"state": "indexing", "files_done": 3,
       "files_total": 9, "place": 2}) and (s.get_brain_repo("acme/web")["state"], s.get_brain_repo("acme/web")["files_done"],
       s.get_brain_repo("acme/web")["files_total"], s.get_brain_repo("acme/web")["place"]) == ("indexing", 3, 9, 2))
    ck("... a value of None clears the column", s.update_brain_repo("acme/web", {"place": None, "error": None})
       and s.get_brain_repo("acme/web")["place"] is None)
    ck("only_in holds the change back when the state moved on", not s.update_brain_repo("acme/web", {"state": "ready"}, only_in=("queued", "cloning"))
       and s.get_brain_repo("acme/web")["state"] == "indexing")
    ck("... and lets it through when the state matches", s.update_brain_repo("acme/web", {"state": "ready"}, only_in=("indexing",))
       and s.get_brain_repo("acme/web")["state"] == "ready")
    ck("a repo that is not there is False", not s.update_brain_repo("acme/none", {"state": "failed"}))
    for bad in ({"repo": "x/y"}, {"repo_source_id": "z"}, {"added_by": "p"}, {"state; DROP TABLE brain_repos": 1}):
        try:
            s.update_brain_repo("acme/web", bad)
            ok = False
        except ValueError:
            ok = True
        ck(f"a column that is not settable is refused: {list(bad)[0][:20]}", ok)
    try:
        s.update_brain_repo("acme/web", {})
        ok = False
    except ValueError:
        ok = True
    ck("a change of nothing is refused, not run as bad SQL", ok)
    ck("the states are queued, cloning, indexing, ready, failed", index_mod.STATES == ("queued", "cloning", "indexing", "ready", "failed")
       and repos_mod.STATES == index_mod.STATES)
    ck("the poll interval is 10 seconds", index_mod.POLL_SECONDS == 10.0)
    ck("the code link lore shows is a GitHub blob link", index_mod.web_url_template("acme/web")
       == "https://github.com/acme/web/blob/{commit}/{path}#L{start}-L{end}")
    words = index_mod.failed_words
    ck("a refused clone is said in plain words", "GitHub refused to let the brain clone acme/web" in words(
       "acme/web", "fatal: could not read Username for 'https://github.com': terminal prompts disabled"))
    ck("... also for repository not found", "GitHub refused" in words("acme/web", "remote: Repository not found."))
    ck("another failure keeps its last line", words("acme/web", "fatal: out of memory\n") == "The index of acme/web failed: fatal: out of memory. Retry to start it again.",
       words("acme/web", "fatal: out of memory\n"))
    for blank in (None, "", "   ", '{"message": "x"}', "[1]"):
        w = words("acme/web", blank)
        ck(f"no usable log line ({blank!r}) is a plain sentence naming the repo", w == "The index of acme/web failed. Retry to start it again.", w)
    ck("a token in the log line is hidden", T2 not in words("acme/web", f"fatal: bad {T2} here") and "[hidden]" in words("acme/web", f"fatal: bad {T2} here"))
    ck("a long line is cut", len(words("acme/web", "x" * 5000)) < 450)
    ck("a count that is null, negative, text or a bool is 0", [index_mod._count(v) for v in (None, -3, "4", True, 0, 7)] == [0, 0, 0, 0, 0, 7])
    ck("no control plane settings: all three are named", cp_mod.missing_settings({}) == ["TARES_BRAIN_CP_URL", "TARES_BRAIN_CP_TOKEN", "TARES_CELL_SLUG"])
    ck("the instance name stands in for the cell slug", cp_mod.missing_settings({"TARES_BRAIN_CP_URL": "http://x", "TARES_BRAIN_CP_TOKEN": "t",
       "TARES_INSTANCE_NAME": "cell-a"}) == [])
    ck("blank settings count as missing", cp_mod.missing_settings({"TARES_BRAIN_CP_URL": "  ", "TARES_BRAIN_CP_TOKEN": "t", "TARES_CELL_SLUG": "c"})
       == ["TARES_BRAIN_CP_URL"])
    try:
        cp_mod.CpClient.from_env({})
        ok = False
    except cp_mod.CpNotConfigured as e:
        ok = "cannot start an index" in str(e)
    ck("a client without settings says it cannot start an index", ok)


async def until(cond, timeout=6.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False


async def main():
    from tares.daemon import make_app
    index_mod.POLL_SECONDS = 0.05
    index_mod.IDLE_SECONDS = 0.05
    logs = Logs()
    logging.getLogger().addHandler(logs)
    logging.getLogger().setLevel(logging.DEBUG)
    lore, cp = fake_lore.FakeLore(), fake_cp.FakeCp()
    real_cp_client = index_mod.cp_client
    repos_mod.lore_client = lore.client
    index_mod.cp_client = cp.client
    os.environ["TARES_BRAIN_URL"] = fake_lore.BASE_URL
    os.environ["TARES_BRAIN_KEY"] = BRAIN_KEY
    names = ("one", "two", "three", "c1", "c2", "c3", "c4", "poll", "poll2", "gone", "gone2", "gone3", "adopt", "lic", "nobrain",
             "race", "old", "inprog", "nulls", "nocp", "nocp2", "odd", "wake", "asked", "mid", "dup", "leak")
    readable = [{"full_name": f"acme/{n}"} for n in names]

    async def readable_repos(cred, query=""):
        return readable
    github_credentials.list_repos_for = readable_repos
    app = make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=H(ROOT))
        store = app.state.store
        r = await cx.put("/api/licence", json={"key": licensed.test_licence()})
        ck("the licence is stored", r.status_code == 200, r.text[:200])
        store.upsert_github_credential("setup", T1)
        indexer = app.state.brain_indexer

        def row(repo):
            return store.get_brain_repo(repo) or {}

        def src(repo):
            return row(repo).get("repo_source_id")

        def jobs_for(repo):
            return [c for c in cp.calls("POST", "start_index") if c.json["repo_source_id"] == src(repo)]

        async def connect(repo):
            r = await cx.post("/api/brain/repos", json={"repos": [repo]})
            ck(f"connect {repo} answers 201", r.status_code == 201, f"{r.status_code} {r.text[:300]}")
            return r

        async def state_is(repo, state, **more):
            def cond():
                r = row(repo)
                return r.get("state") == state and all(r.get(k) == v for k, v in more.items())
            ok = await until(cond)
            ck(f"{repo} is {state}" + "".join(f", {k} {v}" for k, v in more.items()), ok, str({k: row(repo).get(k) for k in ("state", *more, "error")}))
            return ok

        print("== one task per app ==")
        runs = lambda: [t for t in asyncio.all_tasks() if getattr(t.get_coro(), "__qualname__", "") == "Indexer.run"]
        ck("the app has one indexer and one task running it", indexer is not None and indexer.task is not None
           and not indexer.task.done() and len(runs()) == 1, str(runs()))
        first = indexer.task
        indexer.attach(app)
        ck("attaching again starts nothing new", indexer.task is first and len(runs()) == 1 and app.state.brain_indexer is indexer)
        ck("with nothing to follow it asks the control plane and lore for nothing", await asyncio.sleep(0.3) is None
           and not cp.requests and not lore.requests)

        print("== a repo is queued, then cloning, indexing, ready ==")
        cp.start_as = ("queued", 3)
        r = await connect("acme/one")
        body = r.json()["repos"][0]
        ck("the answer is a queued row with the place key", body["state"] == "queued" and "place" in body and body["error"] is None, str(body))
        made = lore.calls("POST", "create_repo")[0].json
        ck("lore got the repo source with a GitHub link template", made["web_url_template"].startswith("https://github.com/acme/one/blob/{commit}/")
           and made["remote_url"] == "https://github.com/acme/one.git", str(made))
        start = cp.calls("POST", "start_index")[0]
        ck("the control plane got the repo source id, the remote and the cell's GitHub token", start.json == {
           "repo_source_id": src("acme/one"), "remote_url": "https://github.com/acme/one.git", "token": T1}, str(start.json))
        ck("... with the cell's bearer token, on the cell's own path", start.headers["authorization"] == "Bearer " + cp.token
           and start.path == "/internal/cells/cell-a/brain/index", f"{start.path}")
        ck("the row notes the job was asked for", row("acme/one")["job_started_at"] is not None)
        await state_is("acme/one", "queued", place=3)
        j = (await cx.get("/api/brain")).json()["repos"]
        ck("GET /api/brain shows the place while queued", [x["place"] for x in j if x["repo"] == "acme/one"] == [3], str(j))
        cp.set(src("acme/one"), "queued", place=1)
        await state_is("acme/one", "queued", place=1)
        cp.set(src("acme/one"), "started")
        await state_is("acme/one", "cloning", place=None, files_done=0, files_total=0)
        lore.progress[src("acme/one")] = {"stage": "indexing", "files_done": 12, "files_total": 40}
        await state_is("acme/one", "indexing", files_done=12, files_total=40)
        j = (await cx.get("/api/brain")).json()["repos"][0]
        ck("GET /api/brain shows the counts", (j["state"], j["files_done"], j["files_total"], j["place"]) == ("indexing", 12, 40, None), str(j))
        lore.progress[src("acme/one")] = {"stage": "indexing", "files_done": None, "files_total": None}
        await state_is("acme/one", "indexing", files_done=0, files_total=0)
        ck("... null counts from lore do not break it", row("acme/one")["error"] is None)
        lore.progress[src("acme/one")] = {"stage": "indexing", "files_done": 30, "files_total": 40}
        await state_is("acme/one", "indexing", files_done=30, files_total=40)
        ck("the cell has not asked lore to sync yet", not lore.calls("POST", "sync_repo"))
        store.upsert_github_credential("setup", T2)   # the token is rotated while the Job runs
        cp.set(src("acme/one"), "done")
        await state_is("acme/one", "ready", place=None, error=None)
        ck("lore was told to take over the Job's index, once, with a fresh token", [c.json for c in lore.calls("POST", "sync_repo")] == [{"token": T2}],
           str([c.json for c in lore.calls("POST", "sync_repo")]))
        r1 = row("acme/one")
        ck("the repo is ready with its commit, its time and its file count", r1["indexed_commit"] == "0" * 40 and r1["last_synced"] is not None
           and (r1["files_done"], r1["files_total"]) == (40, 40), str(r1))
        ck("one job for the repo, however many polls", len(jobs_for("acme/one")) == 1)
        n_cp, n_lore = len(cp.requests), len(lore.requests)
        await asyncio.sleep(0.4)
        ck("with every repo ready the task asks nothing of the control plane or lore", (len(cp.requests), len(lore.requests)) == (n_cp, n_lore),
           f"{len(cp.requests) - n_cp} {len(lore.requests) - n_lore}")

        print("== a refused clone fails in plain words, and Retry recovers ==")
        cp.start_as = ("started", None)
        await connect("acme/two")
        await state_is("acme/two", "cloning")
        cp.set(src("acme/two"), "failed", error=f"fatal: could not read Username for 'https://x-access-token:{T2}@github.com': terminal prompts disabled")
        await state_is("acme/two", "failed", place=None)
        e = row("acme/two")["error"]
        ck("the error says GitHub refused the clone of that repo", e.startswith("GitHub refused to let the brain clone acme/two") and "{" not in e, e)
        ck("... with no token in it", not any(t in e for t in TOKENS), e)
        r = await cx.get("/api/brain")
        ck("GET /api/brain shows the same error", [x["error"] for x in r.json()["repos"] if x["repo"] == "acme/two"] == [e])
        store.upsert_github_credential("setup", T1)
        r = await cx.post("/api/brain/repos/acme/one/retry")
        ck("a ready repo cannot be retried", r.status_code == 409 and "acme/one is ready" in r.text, f"{r.status_code} {r.text[:200]}")
        asked = len(jobs_for("acme/two"))
        r = await cx.post("/api/brain/repos/acme/two/retry")
        ck("Retry answers the row, queued, error cleared", r.status_code == 200 and r.json()["state"] == "queued" and r.json()["error"] is None
           and r.json()["place"] is None, f"{r.status_code} {r.text[:300]}")
        ck("... and asked the control plane for a new job", len(jobs_for("acme/two")) == asked + 1, str(len(jobs_for("acme/two"))))
        ck("... with a fresh token", jobs_for("acme/two")[-1].json["token"] == T1)
        await state_is("acme/two", "cloning")
        cp.set(src("acme/two"), "done")
        await state_is("acme/two", "ready", error=None)
        ck("the retried repo went all the way to ready", len(lore.calls("POST", "sync_repo")) == 2)

        print("== two retries at once start one job ==")
        await connect("acme/race")
        await state_is("acme/race", "cloning")
        cp.set(src("acme/race"), "failed", error="fatal: out of memory")
        await state_is("acme/race", "failed")
        ck("a failed Job that is not a refusal keeps its last line", row("acme/race")["error"] == "The index of acme/race failed: fatal: out of memory. Retry to start it again.", row("acme/race")["error"])
        asked = len(jobs_for("acme/race"))
        rs = await asyncio.gather(cx.post("/api/brain/repos/acme/race/retry"), cx.post("/api/brain/repos/acme/race/retry"))
        ck("one retry wins, the other is refused (409)", sorted(x.status_code for x in rs) == [200, 409], str([x.status_code for x in rs]))
        ck("... and one new job was asked for", len(jobs_for("acme/race")) == asked + 1, str(len(jobs_for("acme/race"))))
        await state_is("acme/race", "cloning")
        cp.set(src("acme/race"), "done")
        await state_is("acme/race", "ready")

        print("== a row connected before this existed gets its job ==")
        async with repos_mod.lore_client() as lc:
            made = await lc.create_repo("acme_old", remote_url="https://github.com/acme/old.git")
        store.add_brain_repo("acme/old", made["id"], None, "2026-01-01T00:00:00Z")   # queued, no job, as TR-1197 left it
        ck("it has no job yet", row("acme/old")["state"] == "queued" and row("acme/old")["job_started_at"] is None)
        indexer.wake()
        await state_is("acme/old", "cloning")
        await asyncio.sleep(0.3)
        ck("the poll asked for its job, once", len(jobs_for("acme/old")) == 1, str(len(jobs_for("acme/old"))))
        ck("... with the cell's token and its remote", jobs_for("acme/old")[0].json["token"] == T1
           and jobs_for("acme/old")[0].json["remote_url"] == "https://github.com/acme/old.git")
        cp.set(src("acme/old"), "done")
        await state_is("acme/old", "ready")

        print("== a connect and the poll at the same moment ask once ==")
        async with repos_mod.lore_client() as lc:
            made = await lc.create_repo("acme_dup", remote_url="https://github.com/acme/dup.git")
        store.add_brain_repo("acme/dup", made["id"], None, "2026-01-01T00:00:00Z")
        await asyncio.gather(indexer.start("acme/dup"), indexer.advance("acme/dup"), indexer.start("acme/dup"))
        await state_is("acme/dup", "cloning")
        await asyncio.sleep(0.2)
        ck("three callers at once asked the control plane for one job", len(jobs_for("acme/dup")) == 1, str(len(jobs_for("acme/dup"))))
        cp.set(src("acme/dup"), "done")
        await state_is("acme/dup", "ready")

        print("== the control plane refuses, is down, or is not set up ==")
        async def failed_connect(repo, words, setup, label):
            setup()
            await connect(repo)
            ok = await state_is(repo, "failed")
            e = row(repo)["error"] or ""
            ck(f"{label}: {words!r} in the error", words in e, e)
            ck(f"{label}: no token, no raw JSON", not any(t in e for t in TOKENS) and "{" not in e and "Traceback" not in e, e)
            ck(f"{label}: the repo is connected, with its lore source", bool(src(repo)))
            return ok

        await failed_connect("acme/c1", "Could not reach the control plane", lambda: cp.raise_next("start_index", httpx.ConnectError("no route")), "down")
        await state_is("acme/c1", "failed")
        ck("down: Retry recovers it", (await cx.post("/api/brain/repos/acme/c1/retry")).status_code == 200)
        await state_is("acme/c1", "cloning")
        cp.set(src("acme/c1"), "done")
        await state_is("acme/c1", "ready")
        await failed_connect("acme/c2", "refused this cell's brain token", lambda: cp.fail("start_index", 401, "invalid cell token"), "refused")
        await failed_connect("acme/c3", "did not answer within", lambda: cp.raise_next("start_index", httpx.ReadTimeout("slow")), "timeout")
        await failed_connect("acme/c4", "could not run the index job", lambda: cp.fail(
            "start_index", 502, f"clone of https://x:{T1}@github.com/a/b failed for token {T1}"), "a 502 that echoes the token")
        ck("no row kept a Job, so Retry may start one", row("acme/c4")["job_started_at"] is None)
        class Leaky:   # a client whose error text holds the GitHub token: the indexer hides it too
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def start_index(self, *a): raise index_mod.CpError(f"clone failed for {T1} (and {T2})", 502)
        index_mod.cp_client = Leaky
        await failed_connect("acme/leak", "Could not start the index", lambda: None, "an error that holds the token")
        index_mod.cp_client = cp.client
        index_mod.cp_client = real_cp_client
        await failed_connect("acme/nocp", "This cell cannot start an index", lambda: None, "not configured")
        e = row("acme/nocp")["error"]
        ck("not configured: all three settings are named", all(n in e for n in ("TARES_BRAIN_CP_URL", "TARES_BRAIN_CP_TOKEN", "TARES_CELL_SLUG")), e)
        os.environ["TARES_BRAIN_CP_URL"] = "http://cp.invalid"
        await failed_connect("acme/nocp2", "This cell cannot start an index", lambda: None, "token missing")
        e = row("acme/nocp2")["error"]
        ck("only the missing settings are named", "TARES_BRAIN_CP_URL" not in e and "TARES_BRAIN_CP_TOKEN" in e, e)
        os.environ.pop("TARES_BRAIN_CP_URL")
        index_mod.cp_client = cp.client
        ck("a cell with nothing set did not crash the daemon", (await cx.get("/api/brain")).status_code == 200 and not indexer.task.done())

        print("== a job already asked for is not asked for again ==")
        cp.start_as = ("queued", 2)
        await connect("acme/asked")
        await state_is("acme/asked", "queued", place=2)
        ck("the row notes the job", row("acme/asked")["job_started_at"] is not None and len(jobs_for("acme/asked")) == 1)
        await indexer.start("acme/asked")
        await indexer.start("acme/asked")
        await asyncio.sleep(0.2)
        ck("starting a queued row that has its job asks for nothing", len(jobs_for("acme/asked")) == 1, str(len(jobs_for("acme/asked"))))
        cp.start_as = ("started", None)
        cp.set(src("acme/asked"), "done")
        await state_is("acme/asked", "ready")

        print("== a row that left queued while its job was being asked for is left alone ==")
        cp.before["start_index"] = lambda: store.update_brain_repo("acme/mid", {"state": "failed", "error": "set by someone else"})
        await connect("acme/mid")
        await asyncio.sleep(0.2)
        ck("the row stays as the other change left it, and notes no job", row("acme/mid")["state"] == "failed"
           and row("acme/mid")["error"] == "set by someone else" and row("acme/mid")["job_started_at"] is None, str(row("acme/mid")))

        print("== connecting wakes the task, whatever its sleep ==")
        index_mod.POLL_SECONDS = index_mod.IDLE_SECONDS = 30.0
        await asyncio.sleep(0.3)   # the task is now in a long sleep
        await connect("acme/wake")
        await state_is("acme/wake", "cloning")
        cp.set(src("acme/wake"), "done")
        indexer.wake()
        await state_is("acme/wake", "ready")
        index_mod.POLL_SECONDS = index_mod.IDLE_SECONDS = 0.05
        indexer.wake()

        print("== no GitHub connection can read the repo ==")
        readable.remove({"full_name": "acme/inprog"})
        await connect("acme/inprog")
        await state_is("acme/inprog", "failed")
        e = row("acme/inprog")["error"]
        ck("the error names the repo and what to do", "No GitHub connection on this cell can read acme/inprog" in e and "Settings, GitHub" in e, e)
        ck("no control plane job was asked for it", not jobs_for("acme/inprog"))
        readable.append({"full_name": "acme/inprog"})
        store.delete_github_credential("setup")
        await connect("acme/nulls")
        await state_is("acme/nulls", "failed")
        ck("with no GitHub connection at all it says so", "no GitHub connection" in row("acme/nulls")["error"], row("acme/nulls")["error"])
        store.upsert_github_credential("setup", T1)
        r = await cx.post("/api/brain/repos/acme/nulls/retry")
        ck("a retry with a connection back starts the job", r.status_code == 200 and len(jobs_for("acme/nulls")) == 1)
        await state_is("acme/nulls", "cloning")
        cp.set(src("acme/nulls"), "done")
        await state_is("acme/nulls", "ready")

        print("== the control plane stops answering while a repo runs ==")
        await connect("acme/poll")
        await state_is("acme/poll", "cloning")
        index_mod.MAX_MISSES = 4
        for _ in range(3):
            cp.raise_next("index_status", httpx.ConnectError("no route"))
        ok = await until(lambda: "Trying again" in (row("acme/poll")["error"] or ""))
        ck("the row keeps its state and says why", ok and row("acme/poll")["state"] == "cloning" and "Could not reach the control plane" in row("acme/poll")["error"],
           str(row("acme/poll")["error"]))
        await state_is("acme/poll", "cloning", error=None)
        ck("... and goes on when it answers again", True)
        cp.set(src("acme/poll"), "done")
        await state_is("acme/poll", "ready")
        await connect("acme/poll2")
        await state_is("acme/poll2", "cloning")
        for _ in range(4):
            cp.raise_next("index_status", httpx.ConnectError("no route"))
        await state_is("acme/poll2", "failed")
        ck("four misses in a row fail it, in plain words", "could not be followed any longer" in row("acme/poll2")["error"] and "Retry" in row("acme/poll2")["error"],
           row("acme/poll2")["error"])
        index_mod.MAX_MISSES = 30

        print("== lore and the Job disagree ==")
        await connect("acme/gone")
        await state_is("acme/gone", "cloning")
        del cp.jobs[src("acme/gone")]
        lore.progress[src("acme/gone")] = {"stage": "indexing", "files_done": 1, "files_total": 5}
        await state_is("acme/gone", "failed")
        ck("a Job that is gone and not finished fails, in words", "is gone" in row("acme/gone")["error"], row("acme/gone")["error"])
        await connect("acme/gone2")
        await state_is("acme/gone2", "cloning")
        del cp.jobs[src("acme/gone2")]
        lore.progress[src("acme/gone2")] = {"stage": "done"}
        await state_is("acme/gone2", "ready")
        ck("a Job that is gone but lore finished is adopted", True)
        await connect("acme/gone3")
        await state_is("acme/gone3", "cloning")
        del cp.jobs[src("acme/gone3")]
        lore.progress[src("acme/gone3")] = {"stage": "done", "finished_at": "2001-01-01T00:00:00Z"}
        await state_is("acme/gone3", "failed")
        ck("a Job that is gone and a done run older than it (an earlier attempt) is not adopted",
           "is gone" in row("acme/gone3")["error"], row("acme/gone3")["error"])
        await connect("acme/adopt")
        await state_is("acme/adopt", "cloning")
        lore.fail("repo_progress", 500, "progress is down")
        await state_is("acme/adopt", "cloning")
        ck("lore's progress failing is not a failure of the repo", row("acme/adopt")["error"] is None, str(row("acme/adopt")["error"]))
        store.upsert_github_credential("setup", T3)
        lore.fail("sync_repo", 500, f"disk full while using {T3}")
        cp.set(src("acme/adopt"), "done")
        await state_is("acme/adopt", "failed")
        e = row("acme/adopt")["error"]
        ck("lore refusing to take the Job over fails the repo, in words", "finished, but the brain could not take it over" in e, e)
        ck("... with no token in it", not any(t in e for t in TOKENS), e)
        store.upsert_github_credential("setup", T1)

        print("== odd answers from the control plane ==")
        await connect("acme/odd")
        await state_is("acme/odd", "cloning")
        for place in (None, 0, -2, "soon", True, 2.5):
            cp.set(src("acme/odd"), "queued", place=3)
            await state_is("acme/odd", "queued", place=3)
            cp.set(src("acme/odd"), "queued", place=place)
            ok = await until(lambda: row("acme/odd")["place"] is None)
            ck(f"a place of {place!r} is shown as no place, still queued", ok and row("acme/odd")["state"] == "queued", str(row("acme/odd")))
        cp.set(src("acme/odd"), "")
        await state_is("acme/odd", "cloning")
        ck("a state it does not know is followed as a running job", row("acme/odd")["error"] is None)
        cp.set(src("acme/odd"), "failed", error="")
        await state_is("acme/odd", "failed")
        ck("a failed Job with no error is a plain sentence naming the repo", row("acme/odd")["error"] == "The index of acme/odd failed. Retry to start it again.", row("acme/odd")["error"])

        print("== it does nothing without a licence or without the brain ==")
        await connect("acme/lic")
        await state_is("acme/lic", "cloning")
        r = await cx.delete("/api/licence")
        ck("the licence is removed", r.status_code == 200)
        async with indexer.lock("acme/lic"):   # a poll that began before the licence went has finished
            pass
        n = len(cp.requests) + len(lore.requests)
        cp.set(src("acme/lic"), "failed", error="fatal: out of memory")
        await asyncio.sleep(0.4)
        ck("no licence: no calls, the row is as it was", len(cp.requests) + len(lore.requests) == n and row("acme/lic")["state"] == "cloning",
           f"{len(cp.requests) + len(lore.requests) - n} {row('acme/lic')['state']}")
        r = await cx.put("/api/licence", json={"key": licensed.test_licence()})
        ck("the licence is back", r.status_code == 200)
        await state_is("acme/lic", "failed")
        await connect("acme/nobrain")
        await state_is("acme/nobrain", "cloning")
        for var in ("TARES_BRAIN_URL", "TARES_BRAIN_KEY"):
            saved = os.environ.pop(var)
            async with indexer.lock("acme/nobrain"):   # a poll that began before the setting went has finished
                pass
            n = len(cp.requests) + len(lore.requests)
            cp.set(src("acme/nobrain"), "failed", error="fatal: out of memory")
            await asyncio.sleep(0.3)
            ck(f"{var} missing: no calls, the row is as it was", len(cp.requests) + len(lore.requests) == n and row("acme/nobrain")["state"] == "cloning",
               f"{len(cp.requests) + len(lore.requests) - n} {row('acme/nobrain')['state']}")
            os.environ[var] = saved
        await state_is("acme/nobrain", "failed")

        print("== no token anywhere ==")
        rows = store.list_brain_repos()
        shown = (await cx.get("/api/brain")).text
        texts = [json.dumps(rows, default=str), shown, "\n".join(logs.lines)] + [str(r.get("error")) for r in rows]
        ck("no token in a stored row, in what GET /api/brain returns, or in a log line", not any(t in text for t in TOKENS for text in texts),
           str([(t[:8], i) for t in TOKENS for i, text in enumerate(texts) if t in text]))
        ck("... no GitHub token shape either", not any("ghs_" in text or "ghp_" in text for text in texts))
        ck("... and the cell's token and the brain key stay out too", not any(cp.token in text or BRAIN_KEY in text for text in texts))
        ck("the log has the control plane's trouble in plain words and nothing else about it", any("control plane call" in l for l in logs.lines))

        print("== shutdown ==")
        task = indexer.task
        await cx.aclose()
    ck("the task stops with the app", task.done() and indexer.task is None and not runs(), f"{task} {indexer.task} {runs()}")
    logging.getLogger().removeHandler(logs)


unit()
asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
