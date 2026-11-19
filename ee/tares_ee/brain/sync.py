# Tares enterprise licence, see ee/LICENSE
"""Keeping the repos the brain holds up to date (TR-1201).

A repo that is `ready` answers from lore's index. A push to its default branch makes that index
stale, so the cell tells lore to sync it (`lore.sync_repo` with a fresh GitHub token). Lore's own
poller and webhook stay off: the cell drives every sync.

* A push event of the cell's GitHub App (`github_app` source, `push`, `on_default_branch` true)
  reaches `Syncer.on_envelopes` through core's `Runtime.envelope_listeners`. It records that the repo
  wants a sync and wakes the task, so a push is searchable within a minute or two.
* A repo whose GitHub connection is a token (no App, so no webhooks) is polled every
  `SYNC_POLL_SECONDS`: one conditional request for the head of the default branch (a 304 costs no
  rate limit). A head that is not the one last synced asks for a sync.
* One sync per repo at a time. Pushes that arrive while one runs are folded into one more sync after
  it (lore fetches the newest head each time). A push that arrives while the repo is still indexing
  is remembered and synced when the repo reaches `ready`; a repo that is `failed` is left alone
  (Retry starts it again).
* A failed sync leaves the repo `ready`: the old index still answers. The reason goes to
  `sync_error` and `sync_failures` counts failures in a row. The next push tries again, and so does the next poll of a token repo; a repo the App covers is not polled (A25). From
  `SHOW_AFTER` in a row the reason is also the row's `error`, which is what the card shows. A
  success clears all three.
* A repo the App covers has no poll, so after a failure it would wait for the next push, which on a quiet
  repo can be days (TR-1510). It retries on a back-off instead: `RETRY_FIRST_SECONDS` after the first
  failure, doubling with each failure in a row up to `RETRY_CAP_SECONDS`. A retry is `request(repo)`
  with no head: lore fetches the newest head itself, so it costs no GitHub call. A push still syncs at
  once, and a success, a removed repo or a `failed` one ends the retrying. A token repo has the poll
  and no back-off. The push listeners hear nothing on a retry: `on_envelopes` tells them as the event
  arrives, whatever the sync does, and a retry is not a push (telling them None would walk every chosen
  folder at each step). A listener that fails on its own work retries it itself: the decision importer
  keeps the files of a push lore did not take and tries them again on its own back-off (TR-1514).

* The files a push touched are told to the `push_listeners` (the decision importer, TR-1219): from the
  event's payload (`pushed_files`), or None when it has none that can be trusted. A token repo has no
  events: when its poll sees a new head, the listeners hear None (look at everything).

A GitHub token is held in a local variable for one call. It is never stored, logged or shown: text
that may hold one goes through `lore.scrub` and `index._line`, and an error of unknown kind is logged
by its type only.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time

import httpx

from tares import github_credentials
from tares import licence
from tares.envelope import now_utc
from tares.factory import norm_repo

from .index import RUNNING, _count, _line, files_in_index, record_update
from .lore import LoreClient, LoreError, scrub

__all__ = ["RETRY_CAP_SECONDS", "RETRY_FIRST_SECONDS", "SHOW_AFTER", "SYNC_POLL_SECONDS", "Syncer", "TICK_SECONDS",
           "github_head", "pushed_files", "retry_delay"]

log = logging.getLogger("tares_ee.brain.sync")

SYNC_POLL_SECONDS = 60.0   # between two looks at the head of a token connection's repos
TICK_SECONDS = 5.0         # between two looks at a push waiting for a repo to be ready (tests make it small)
RETRY_FIRST_SECONDS = 60.0   # an App-covered repo retries this long after its first failed sync
RETRY_CAP_SECONDS = 1800.0   # the wait doubles with each failure in a row up to this
SHOW_AFTER = 3             # failures in a row before the card shows the error
HEAD_TIMEOUT = 15          # seconds one head check may take
GITHUB_TIMEOUT = 60        # seconds one repo listing may take
PUSH_COMMITS_CAP = 20      # GitHub lists at most this many commits in a push event

_SHA = re.compile(r"[0-9a-f]{40}")


def retry_delay(failures: int) -> float:
    """Seconds to wait after `failures` failed syncs in a row (at least 1): the first wait, doubled each time, up to the cap."""
    n = max(1, min(int(failures), 32))   # the cap is reached long before 32: this only keeps the power small
    return min(RETRY_CAP_SECONDS, RETRY_FIRST_SECONDS * 2 ** (n - 1))


async def github_head(api_url, token: str, repo: str, known=None) -> tuple[str, str]:
    """`(sha, etag)`: the head commit of the repo's default branch. `known` is the `(sha, etag)` of
    the last answer: GitHub answers 304 when nothing moved and `known` comes back. Raises ValueError
    in plain words (never with the token in it)."""
    api = (api_url or github_credentials.API_DEFAULT).rstrip("/")
    headers = {"Accept": "application/vnd.github.sha", "X-GitHub-Api-Version": "2022-11-28",
               "Authorization": f"Bearer {token}"}
    if known and known[1]:
        headers["If-None-Match"] = known[1]
    try:
        async with httpx.AsyncClient(timeout=HEAD_TIMEOUT) as cx:
            r = await cx.get(f"{api}/repos/{repo}/commits/HEAD", headers=headers)
    except httpx.HTTPError as e:
        raise ValueError(f"GitHub did not answer ({type(e).__name__})")
    if r.status_code == 304 and known and known[0]:
        return known[0], known[1]
    if r.status_code == 401:
        raise ValueError("GitHub rejected the token (401)")
    if r.status_code in (403, 429):
        raise ValueError(f"GitHub refused the check ({r.status_code}), maybe a rate limit")
    if r.status_code == 404:
        raise ValueError(f"GitHub cannot find {repo}, or the token cannot read it (404)")
    if r.status_code == 409:
        raise ValueError(f"{repo} has no commits yet (409)")
    sha = r.text.strip().lower() if r.status_code == 200 else ""
    if not _SHA.fullmatch(sha):
        raise ValueError(f"GitHub gave an answer the brain could not read ({r.status_code})")
    return sha, r.headers.get("etag") or ""


def pushed_files(payload) -> set[str] | None:
    """The paths a push event touched (added, modified or removed in any of its commits), or None when the
    event cannot say: no commit list, GitHub's cap of PUSH_COMMITS_CAP commits reached (it cuts the rest),
    a commit without its file lists, or a force push (its commits are the new ones; the files the rewrite
    dropped are named nowhere)."""
    if isinstance(payload, dict) and payload.get("forced"):
        return None
    commits = payload.get("commits") if isinstance(payload, dict) else None
    if not isinstance(commits, list) or not commits or len(commits) >= PUSH_COMMITS_CAP:
        return None
    out: set[str] = set()
    for c in commits:
        if not isinstance(c, dict):
            return None
        for key in ("added", "modified", "removed"):
            names = c.get(key)
            if not isinstance(names, list):
                return None
            out.update(n for n in names if isinstance(n, str))
    return out


class Syncer:
    """One per app, next to the Indexer (it shares the indexer's store, lore client and per-repo
    lock). `runtime` is core's runtime, whose `envelope_listeners` brings the push events."""

    def __init__(self, indexer, runtime=None):
        self.indexer = indexer
        self.store = indexer.store
        self.runtime = runtime
        self._want: dict[str, int] = {}      # repo -> pushes and polls that asked for a sync
        self._done: dict[str, int] = {}      # repo -> the count a finished or failed sync had seen
        self._head: dict[str, str] = {}      # repo -> the newest head a push or poll named
        self._synced: dict[str, str] = {}    # repo -> the head last synced (a poll compares to it)
        self._seen: dict[str, tuple[str, str]] = {}   # repo -> (head, etag) of the last poll
        self._told: dict[str, str] = {}      # repo -> the head the push listeners last heard of from a poll
        self.push_listeners: list = []       # callables (repo, changed paths or None), told of every push
        self._tasks: dict[str, asyncio.Task] = {}     # repo -> its running sync
        self._app: set[str] = set()          # repos a GitHub App covers (a push event or the poll said so): no poll, so a retry
        self._retry_at: dict[str, float] = {}   # App repo -> when (self._clock) its failed sync is tried again
        self._clock = time.monotonic          # tests move it
        self._wake = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.poll_task: asyncio.Task | None = None
        self.syncs = 0   # syncs that succeeded, for tests

    def active(self) -> bool:
        return licence.ee_allowed() and LoreClient.configured()

    # -- asking for a sync ------------------------------------------------------------------

    def on_envelopes(self, source: str, envelopes) -> None:
        """Core calls this for every batch of stored events. Only a push to the default branch from
        the cell's GitHub App counts. Returns at once."""
        pushes = []
        for env in envelopes or ():
            labels = getattr(env, "labels", None) or {}
            if getattr(env, "event_type", "") == "push" and labels.get("on_default_branch") == "true" \
                    and isinstance(labels.get("repo"), str):
                pushes.append((labels, getattr(env, "payload", None)))
        if not pushes or not self._is_app_source(source) or not self.active():
            return
        for labels, payload in pushes:   # no store read here: this runs on the event loop, inside the ingest
            repo = norm_repo(labels["repo"])
            self._app.add(repo)
            self._note(repo, labels.get("sha"))
            self._tell(repo, pushed_files(payload))

    def _tell(self, repo: str, paths) -> None:
        for listener in list(self.push_listeners):
            try:
                listener(repo, paths)
            except Exception as e:   # noqa: BLE001  (a listener must not stop the sync)
                log.warning("brain push listener for %s failed: %s", repo, type(e).__name__)

    def _is_app_source(self, name: str) -> bool:
        catalog = getattr(self.runtime, "catalog", None)
        cfg = catalog.sources.get(name) if catalog is not None else None
        return getattr(cfg, "connector", None) == "github_app"

    def request(self, repo: str, sha=None) -> bool:
        """Note that `repo` has a newer head (`sha`, when known) and wake the task. False when the
        repo is not on the brain or is `failed` (Retry restarts that one)."""
        row = self.store.get_brain_repo(repo) if repo else None
        if row is None or row["state"] == "failed":
            return False
        self._note(repo, sha)
        return True

    def _note(self, repo: str, sha=None) -> None:
        """Record the ask and wake the task, without reading the store. A repo that is not on the
        brain, or is failed, is dropped by `_start_tasks` on the task's own turn."""
        if not repo:
            return
        sha = sha.strip().lower() if isinstance(sha, str) else ""
        self._want[repo] = self._want.get(repo, 0) + 1
        if _SHA.fullmatch(sha) and sha != "0" * 40:
            self._head[repo] = sha
        else:
            self._head.pop(repo, None)
        self.wake()

    def wake(self) -> None:
        self._wake.set()

    def _forget(self, repo: str) -> None:
        for book in (self._want, self._done, self._head, self._synced, self._seen, self._told, self._retry_at):
            book.pop(repo, None)
        self._app.discard(repo)

    def _outstanding(self) -> bool:
        return any(n > self._done.get(r, 0) for r, n in self._want.items())

    def _start_tasks(self) -> None:
        """Start a sync for every ready repo that asked for one and has none running."""
        for repo, n in list(self._want.items()):
            if n <= self._done.get(repo, 0) or repo in self._tasks:
                continue
            row = self.store.get_brain_repo(repo)
            if row is None:   # a push for a repo that is not on the brain
                self._forget(repo)
            elif row["state"] == "ready":
                self._tasks[repo] = asyncio.create_task(self._run_repo(repo))
            elif row["state"] not in RUNNING:   # failed: nothing to keep current. Running: wait for ready
                self._forget(repo)

    def _start_retries(self) -> None:
        """Ask again for every App repo whose back-off is over. Nothing to catch up (the repo is gone, failed,
        indexing, already syncing or no longer failing) drops the retry."""
        now = self._clock()
        for repo, due in list(self._retry_at.items()):
            if due > now:
                continue
            self._retry_at.pop(repo, None)
            row = self.store.get_brain_repo(repo)
            if row is None or row["state"] == "failed":
                self._forget(repo)   # gone or failed: nothing to keep current
                continue
            if row["state"] != "ready" or not row.get("sync_failures") or repo in self._tasks:
                continue   # a sync running now re-arms the retry if it fails
            self._note(repo)   # no head: lore fetches the newest one, and the listeners are not told

    def _next_retry(self) -> float | None:
        return min(self._retry_at.values(), default=None)

    async def _run_repo(self, repo: str) -> None:
        try:
            await self._sync(repo)
        except Exception as e:   # noqa: BLE001  (one repo must not stop the others)
            log.warning("brain sync of %s failed: %s", repo, type(e).__name__)
        finally:
            self._tasks.pop(repo, None)
            self.wake()   # a push that came meanwhile asks for one more

    # -- one sync ---------------------------------------------------------------------------

    async def _sync(self, repo: str) -> None:
        async with self.indexer.lock(repo):
            if not self.active():
                return
            row = self.store.get_brain_repo(repo)
            if row is None:
                return self._forget(repo)
            if row["state"] != "ready":
                return   # moved on while this waited: the next look decides
            n, head = self._want.get(repo, 0), self._head.get(repo, "")
            if not row["repo_source_id"]:
                self._done[repo] = n
                return self._failed(repo, f"The brain has no repo source for {repo}.")
            token = ""
            try:
                token = await self.indexer.github_token(repo)
                async with self.indexer._lore() as lore:
                    report = await lore.sync_repo(row["repo_source_id"], token=token)
            except (LoreError, ValueError) as e:
                self._done[repo] = n
                return self._failed(repo, f"{repo} could not be brought up to date: {scrub(e, [token])}")
            except Exception as e:   # noqa: BLE001  (type only: the text of an unknown error may hold a token)
                log.warning("brain sync of %s failed: %s", repo, type(e).__name__)
                self._done[repo] = n
                return self._failed(repo, f"{repo} could not be brought up to date.")
            self._done[repo] = n
            self._succeeded(repo, report if isinstance(report, dict) else {}, head)

    def _succeeded(self, repo: str, report: dict, head: str) -> None:
        commit = report.get("indexed_commit")
        commit = commit if isinstance(commit, str) and commit else ""
        fields = {"last_synced": now_utc(), "error": None, "sync_error": None, "sync_failures": 0}
        if commit:
            fields["indexed_commit"] = commit
        # Lore reports the repo's own file count as `files_in_index`: that is the count, a 0 included.
        # An older lore has no such key. Its report counts only the files the push touched (indexed or
        # unchanged) and the ones it deleted, never the whole repo, so the repo's count is the one from
        # its first index less what later pushes deleted (TR-1463). Files a push adds are counted from
        # the next full index. Only a repo with no count yet takes the report's.
        changed = _count(report.get("files_indexed"))
        deleted = _count(report.get("files_deleted"))
        row = self.store.get_brain_repo(repo)
        before = (row or {}).get("files_total") or 0
        total = files_in_index(report)
        if total is None:
            total = max(0, before - deleted) if before else changed + _count(report.get("files_unchanged"))
        if total != before:
            fields["files_done"] = fields["files_total"] = total
        if self.store.update_brain_repo(repo, fields, only_in=("ready",)):
            record_update(self.store, repo, "update", True, commit=commit or None, files_changed=changed,
                          files_deleted=deleted, files_total=total)
        if head or commit:   # an empty head would make every later poll look like a new commit
            self._synced[repo] = head or commit
        self._retry_at.pop(repo, None)
        self.syncs += 1

    def _failed(self, repo: str, words: str) -> None:
        """Keep the repo ready, note the failure, show it on the card from SHOW_AFTER in a row."""
        row = self.store.get_brain_repo(repo)
        if row is None:
            return
        n = (row.get("sync_failures") or 0) + 1
        words = _line(words)
        fields = {"sync_failures": n, "sync_error": words}
        if n >= SHOW_AFTER:
            fields["error"] = words
        if self.store.update_brain_repo(repo, fields, only_in=("ready",)):
            record_update(self.store, repo, "update", False, error=words, files_total=row.get("files_total") or 0)
        if repo in self._app:   # no poll will ask again: back off, then try without waiting for a push
            self._retry_at[repo] = self._clock() + retry_delay(n)
        log.info("brain sync of %s failed, %d in a row", repo, n)

    # -- the poll, for repos without webhooks -----------------------------------------------

    async def poll(self) -> int:
        """One pass over the ready repos no App webhook covers. Returns how many it looked at."""
        if not self.active():
            return 0
        n = 0
        for row in self.store.list_brain_repos():
            repo = row["repo"]
            if row["state"] != "ready" or repo in self._tasks or self._want.get(repo, 0) > self._done.get(repo, 0):
                continue
            try:
                n += await self._poll_repo(row)
            except Exception as e:   # noqa: BLE001
                log.warning("brain head check of %s failed: %s", repo, type(e).__name__)
        return n

    async def _target(self, repo: str):
        """`(token, api_url)` of the token connection that reads `repo`, or None when a GitHub App
        reads it (its webhooks cover it). Raises ValueError in plain words."""
        creds = self.store.list_github_credentials()
        if not creds:
            raise ValueError("this cell has no GitHub connection")
        reader, reasons = None, []
        for cred in creds:
            try:
                found = await asyncio.wait_for(github_credentials.list_repos_for(cred), GITHUB_TIMEOUT)
            except asyncio.TimeoutError:
                reasons.append("GitHub took too long to answer")
                continue
            except ValueError as e:
                reasons.append(_line(e))
                continue
            if not any(str(g.get("full_name") or "").strip().lower() == repo for g in found):
                continue
            if github_credentials.is_app(cred):
                return None
            reader = reader or cred
        if reader is None:
            why = f" ({reasons[0]})" if reasons else ""
            raise ValueError(f"no GitHub connection on this cell can read {repo}{why}")
        return await github_credentials.get_token(self.store, reader["name"], repo), reader.get("api_url") or None

    async def _poll_repo(self, row: dict) -> int:
        repo, token = row["repo"], ""
        try:
            target = await self._target(repo)
            if target is None:
                self._app.add(repo)
                if row.get("sync_failures") and repo not in self._retry_at:
                    self._retry_at[repo] = self._clock()   # failed before this run knew: try now, then back off
                    self.wake()
                return 0
            self._app.discard(repo)   # a token reads it now: the poll is its retry
            self._retry_at.pop(repo, None)
            token, api = target
            sha, etag = await github_head(api, token, repo, self._seen.get(repo))
        except ValueError as e:
            self._failed(repo, f"{repo} could not be checked for new commits: {scrub(e, [token])}")
            return 1
        self._seen[repo] = (sha, etag)
        if sha != self._told.get(repo, self._synced.get(repo, row["indexed_commit"] or "")):
            self._told[repo] = sha
            self._tell(repo, None)   # no push event for a token repo: the listeners look at everything
        if sha != self._synced.get(repo, row["indexed_commit"] or ""):
            self.request(repo, sha)
        elif row.get("sync_failures"):   # the index is current again: nothing to show
            self.store.update_brain_repo(repo, {"sync_failures": 0, "sync_error": None, "error": None},
                                         only_in=("ready",))
        return 1

    # -- the task ---------------------------------------------------------------------------

    async def run(self) -> None:
        next_poll = 0.0   # the first look is at once: a token repo that moved while the cell was down is caught
        while True:
            self._wake.clear()
            due = time.monotonic() >= next_poll
            if due:   # whether the brain is on or a step below fails, the next look is a poll away
                next_poll = time.monotonic() + SYNC_POLL_SECONDS
            try:
                if self.active():
                    self._start_retries()
                    self._start_tasks()
                    if due and (self.poll_task is None or self.poll_task.done()):
                        self.poll_task = asyncio.create_task(self.poll())
            except Exception as e:   # noqa: BLE001
                log.warning("brain sync could not read its repos: %s", type(e).__name__)
            delay = TICK_SECONDS if self._outstanding() else max(0.05, next_poll - time.monotonic())
            retry = self._next_retry()
            if retry is not None and self.active():   # off: _start_retries does not run, so a due retry would only spin this loop
                delay = min(delay, max(0.05, retry - self._clock()))
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), delay)

    def attach(self, app) -> None:
        """Start the task with the app, stop it when the app stops, and listen for push events."""
        if getattr(app.state, "brain_syncer", None) is not None:
            return
        app.state.brain_syncer = self
        listeners = getattr(self.runtime, "envelope_listeners", None)
        if listeners is not None and self.on_envelopes not in listeners:
            listeners.append(self.on_envelopes)
        outer = app.router.lifespan_context

        @contextlib.asynccontextmanager
        async def lifespan(a):
            async with outer(a) as state:
                if self.task is None or self.task.done():
                    self.task = asyncio.create_task(self.run())
                try:
                    yield state
                finally:
                    running = [self.task, self.poll_task, *self._tasks.values()]
                    self.task = self.poll_task = None
                    for t in running:
                        if t is not None:
                            t.cancel()
                    for t in running:
                        if t is not None:
                            with contextlib.suppress(asyncio.CancelledError, Exception):
                                await t

        app.router.lifespan_context = lifespan
