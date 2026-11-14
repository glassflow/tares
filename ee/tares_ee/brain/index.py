# Tares enterprise licence, see ee/LICENSE
"""Starting a repo's first index and following it to ready (TR-1198).

Connecting a repo (`repos.py`) stores it `queued`. This file does the rest:

1. `Indexer.start(repo)` takes a fresh GitHub token and asks the control plane (`cp.py`) for an index
   Job, then notes `job_started_at` on the row, so a repo is asked for once. A refusal or a cell
   without control plane settings makes the row `failed`, with the reason in plain words, so Retry works.
2. A background task (`Indexer.run`, started and stopped with the app) polls every
   `POLL_SECONDS` while any repo is `queued`, `cloning` or `indexing`: the control plane says where
   the Job is (`queued` with its `place`, running, `done`, `failed`), lore says how many files are
   indexed (`/progress`). A `queued` row with no job yet (connected before this existed) gets its
   job from the poll. While nothing is running the task sleeps and reads nothing but a cheap check
   every `IDLE_SECONDS`; a connect or a retry wakes it at once.
3. When the Job is `done`, the cell tells lore to adopt it (`sync_repo` with a fresh token) and the
   row becomes `ready` with `indexed_commit` and `last_synced`.

Row states: `queued` (waiting, `place` set when the control plane says where), `cloning` (the Job
runs, lore has no files to index yet), `indexing` (`files_done` of `files_total`), `ready`, `failed`.

A GitHub token is held in a local variable for one call. It is never stored, logged or shown: every
text that may hold one goes through `lore.scrub` before it reaches a row, a log line or an answer.
A control plane that does not answer is waited out: the row keeps its state and shows why in
`error` until it answers again, or `MAX_MISSES` polls in a row (about five minutes) fail it.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

from tares import github_credentials
from tares import licence
from tares.envelope import now_utc

from .cp import CpClient, CpError, CpNotConfigured
from .lore import LoreClient, LoreError, scrub

__all__ = ["IDLE_SECONDS", "Indexer", "MAX_MISSES", "POLL_SECONDS", "RUNNING", "STATES", "cp_client",
           "web_url_template"]

log = logging.getLogger("tares_ee.brain.index")

STATES = ("queued", "cloning", "indexing", "ready", "failed")
RUNNING = ("queued", "cloning", "indexing")   # the states the poller follows
POLL_SECONDS = 10.0      # between two polls while a repo is running (tests make it small)
IDLE_SECONDS = 60.0      # between two looks for work while none is running
MAX_MISSES = 30          # polls in a row the control plane may not answer before the repo fails
GITHUB_TIMEOUT = 60      # seconds one GitHub repo listing may take
MAX_ERROR = 300

# Hints in a failed Job's last log line that mean GitHub refused the clone
_REFUSED = ("authentication failed", "could not read username", "permission denied", "repository not found",
            "invalid username or password", "requested url returned error: 403",
            "requested url returned error: 401", "requested url returned error: 404")


def cp_client() -> CpClient:
    """The client the indexer uses (a test replaces this with the fake control plane's)."""
    return CpClient.from_env()


def web_url_template(repo: str) -> str:
    """How lore links a hit of this repo's code on GitHub."""
    return f"https://github.com/{repo}/blob/{{commit}}/{{path}}#L{{start}}-L{{end}}"


def remote_url(repo: str) -> str:
    return f"https://github.com/{repo}.git"


def _count(v) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def _line(text, secrets=()) -> str:
    """One line of someone else's text, safe to keep: no token, no line breaks, not too long."""
    out = " ".join(scrub(text, secrets).split())
    return out[:MAX_ERROR]


def failed_words(repo: str, error) -> str:
    """What to tell a person about a Job that failed, from its last log line."""
    line = _line(error)
    if not line or line[0] in "{[":
        return f"The index of {repo} failed. Retry to start it again."
    if any(h in line.lower() for h in _REFUSED):
        return (f"GitHub refused to let the brain clone {repo}: the token could not read it. "
                "Check that a GitHub connection on this cell can read the repo, then retry.")
    return f"The index of {repo} failed: {line.rstrip('.')}. Retry to start it again."


class Indexer:
    """One per app. `lore_client` and `cp_client` are called for each use (a test swaps the
    functions); `store` is the cell's store."""

    def __init__(self, store, lore_client, cp_client_fn=None):
        self.store = store
        self._lore = lore_client
        self._cp = cp_client_fn or (lambda: cp_client())
        self._locks: dict[str, asyncio.Lock] = {}
        self._misses: dict[str, int] = {}
        self._wake = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.ticks = 0   # polls that did work, for tests

    # -- the one-at-a-time rule per repo ----------------------------------------------------

    def lock(self, repo: str) -> asyncio.Lock:
        return self._locks.setdefault(repo, asyncio.Lock())

    # -- starting ---------------------------------------------------------------------------

    async def start(self, repo: str) -> None:
        """Ask the control plane for the repo's index Job, once: only a `queued` row whose job was
        not asked for yet. Never raises; a failure is the row's `error`."""
        async with self.lock(repo):
            row = self.store.get_brain_repo(repo)
            if row and row["state"] == "queued" and not row["job_started_at"]:
                await self._start(row)
        self.wake()

    async def _start(self, row: dict) -> None:
        repo, source = row["repo"], row["repo_source_id"]
        if not source:
            return self._fail(repo, f"The brain has no repo source for {repo}. Remove it and connect it again.")
        token = ""
        try:
            token = await self.github_token(repo)
            async with self._cp() as cp:
                await cp.start_index(source, remote_url(repo), token)
        except CpNotConfigured as e:
            return self._fail(repo, str(e))
        except CpError as e:
            return self._fail(repo, f"Could not start the index of {repo}: {scrub(e, [token])}")
        except ValueError as e:   # no GitHub connection can read the repo
            return self._fail(repo, scrub(e, [token]))
        except Exception as e:   # noqa: BLE001  (type only: the text of an unknown error may hold a token)
            log.warning("brain index of %s did not start: %s", repo, type(e).__name__)
            return self._fail(repo, f"Could not start the index of {repo}.")
        self._misses.pop(repo, None)
        self.store.update_brain_repo(repo, {"job_started_at": now_utc(), "place": None, "error": None},
                                     only_in=("queued",))

    async def github_token(self, repo: str) -> str:
        """A fresh token from the first GitHub connection that can read `repo`. Raises ValueError
        in plain words when there is none."""
        creds = self.store.list_github_credentials()
        if not creds:
            raise ValueError(f"This cell has no GitHub connection, so the brain cannot read {repo}. "
                             "Add one under Settings, GitHub, then retry.")
        reasons = []
        for cred in creds:
            try:
                found = await asyncio.wait_for(github_credentials.list_repos_for(cred), GITHUB_TIMEOUT)
                if not any(str(g.get("full_name") or "").strip().lower() == repo for g in found):
                    continue
                return await github_credentials.get_token(self.store, cred["name"], repo)
            except asyncio.TimeoutError:
                reasons.append("GitHub took too long to answer")
            except ValueError as e:
                reasons.append(_line(e))
        why = f" ({reasons[0]})" if reasons else ""
        raise ValueError(f"No GitHub connection on this cell can read {repo}{why}. "
                         "Add the repo to a connection under Settings, GitHub, then retry.")

    # -- the poll ---------------------------------------------------------------------------

    def active(self) -> bool:
        return licence.ee_allowed() and LoreClient.configured()

    def has_work(self) -> bool:
        return any(r["state"] in RUNNING for r in self.store.list_brain_repos())

    async def tick(self) -> int:
        """One pass over every repo that is not ready or failed. Returns how many it looked at."""
        if not self.active():
            return 0
        n = 0
        for row in self.store.list_brain_repos():
            if row["state"] not in RUNNING:
                continue
            n += 1
            try:
                await self.advance(row["repo"])
            except Exception as e:   # noqa: BLE001  (one repo must not stop the others)
                log.warning("brain index poll of %s failed: %s", row["repo"], type(e).__name__)
        if n:
            self.ticks += 1
        return n

    async def advance(self, repo: str) -> None:
        async with self.lock(repo):
            if not self.active():   # the licence or the brain settings went while this waited for the lock
                return
            row = self.store.get_brain_repo(repo)   # read again: a retry or a delete may have come first
            if row is None or row["state"] not in RUNNING:
                return
            if row["state"] == "queued" and not row["job_started_at"]:
                return await self._start(row)
            source = row["repo_source_id"]
            try:
                async with self._cp() as cp:
                    answer = await cp.index_status(source)
            except CpNotConfigured as e:
                return self._fail(repo, str(e))
            except CpError as e:
                return self._miss(repo, str(e))
            self._misses.pop(repo, None)
            if answer is None:
                return await self._gone(row)
            state = str(answer.get("state") or "")
            if state == "done":
                return await self._adopt(row)
            if state == "failed":
                return self._fail(repo, failed_words(repo, answer.get("error")))
            if state == "queued":
                place = _count(answer.get("place")) or None
                self.store.update_brain_repo(repo, {"state": "queued", "place": place, "error": None},
                                             only_in=RUNNING)
                return
            await self._progress(row)

    async def _progress(self, row: dict) -> None:
        """The Job runs: cloning until lore has files to index, then indexing with its counts."""
        repo = row["repo"]
        done, total, phase = 0, 0, "cloning"
        try:
            async with self._lore() as lore:
                p = await lore.repo_progress(row["repo_source_id"])
            done, total = _count(p.get("files_done")), _count(p.get("files_total"))
            if p.get("state") == "indexing" or total:
                phase = "indexing"
        except LoreError as e:
            log.info("brain progress of %s not read: %s", repo, _line(e))
        self.store.update_brain_repo(repo, {"state": phase, "files_done": done, "files_total": total,
                                            "place": None, "error": None}, only_in=RUNNING)

    async def _gone(self, row: dict) -> None:
        """The control plane has no Job for a repo we asked for: it finished and was cleaned up
        while the cell was not looking, or never existed. Lore says whether it was adopted-ready."""
        repo = row["repo"]
        try:
            async with self._lore() as lore:
                p = await lore.repo_progress(row["repo_source_id"])
        except LoreError:
            p = {}
        if p.get("state") == "done":
            return await self._adopt(row)
        self._fail(repo, f"The index job for {repo} is gone and the brain did not finish it. "
                         "Retry to start it again.")

    async def _adopt(self, row: dict) -> None:
        """The Job is done: lore picks up what it indexed (with a fresh token) and the repo is ready."""
        repo = row["repo"]
        token = ""
        try:
            token = await self.github_token(repo)
            async with self._lore() as lore:
                report = await lore.sync_repo(row["repo_source_id"], token=token)
        except (LoreError, ValueError) as e:
            return self._fail(repo, f"The index of {repo} finished, but the brain could not take it over: "
                                    f"{scrub(e, [token])}")
        except Exception as e:   # noqa: BLE001
            log.warning("brain adoption of %s failed: %s", repo, type(e).__name__)
            return self._fail(repo, f"The index of {repo} finished, but the brain could not take it over.")
        total = max(row["files_total"] or 0, _count(report.get("files_indexed")) + _count(report.get("files_unchanged")))
        commit = report.get("indexed_commit")
        self.store.update_brain_repo(repo, {
            "state": "ready", "files_done": total, "files_total": total,
            "indexed_commit": commit if isinstance(commit, str) and commit else None,
            "last_synced": now_utc(), "error": None, "place": None}, only_in=RUNNING)

    def _miss(self, repo: str, words: str) -> None:
        """The control plane did not answer: keep the state, show why, give up after MAX_MISSES."""
        n = self._misses[repo] = self._misses.get(repo, 0) + 1
        if n >= MAX_MISSES:
            self._misses.pop(repo, None)
            return self._fail(repo, f"{words} The index of {repo} could not be followed any longer. Retry to start it again.")
        self.store.update_brain_repo(repo, {"error": f"{words} Trying again."}, only_in=RUNNING)

    def _fail(self, repo: str, words: str) -> None:
        self._misses.pop(repo, None)
        self.store.update_brain_repo(repo, {"state": "failed", "error": _line(words), "place": None},
                                     only_in=RUNNING)

    # -- the task ---------------------------------------------------------------------------

    def wake(self) -> None:
        """Look for work now (a repo was connected or retried)."""
        self._wake.set()

    async def run(self) -> None:
        """Poll while repos are running; sleep (a cheap look every IDLE_SECONDS) while none are."""
        while True:
            self._wake.clear()   # before looking, so a wake during the poll is not lost
            try:
                busy = self.active() and self.has_work()
            except Exception as e:   # noqa: BLE001
                log.warning("brain index could not read its repos: %s", type(e).__name__)
                busy = False
            if busy:
                await self.tick()
                delay = POLL_SECONDS
            else:
                delay = IDLE_SECONDS
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), delay)

    def attach(self, app) -> None:
        """Start the task with the app and stop it when the app stops (one task per app)."""
        if getattr(app.state, "brain_indexer", None) is not None:
            return
        app.state.brain_indexer = self
        outer = app.router.lifespan_context

        @contextlib.asynccontextmanager
        async def lifespan(a):
            async with outer(a) as state:
                if self.task is None or self.task.done():
                    self.task = asyncio.create_task(self.run())
                try:
                    yield state
                finally:
                    task, self.task = self.task, None
                    if task is not None:
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await task

        app.router.lifespan_context = lifespan
