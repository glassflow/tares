# Tares enterprise licence, see ee/LICENSE
"""The repos a cell connected to the brain, and the routes that manage them (TR-1197).

A licensed cell with `TARES_BRAIN_URL` and `TARES_BRAIN_KEY` keeps one `brain_repos` row per
connected repo (the table is in core's `factory_store.py`, like the other tables the ee routes use):

    GET    /api/brain                           {configured, repos: [...]}
    GET    /api/brain/repos/available           the repos the cell's GitHub connection can read
    POST   /api/brain/repos                     {repos: ["owner/name", ...]}   admin
    DELETE /api/brain/repos/{owner}/{name}                                     admin
    POST   /api/brain/repos/{owner}/{name}/retry                               admin

Without a licence every route answers 404 exactly as an unmounted route does (the licence can
change while the daemon runs, so the check is per request, not at mount time). With a licence but
no brain settings, `GET /api/brain` says `configured: false` and every other route answers 404.
A new row is `queued`, and connecting it asks the control plane for an index Job (`index.py`, TR-1198);
a background task there follows the Job: `queued` (with its `place`), `cloning`, `indexing`
(`files_done` of `files_total`), `ready` or `failed`. A ready repo is kept up to date by `sync.py`
(TR-1201): a push to its default branch syncs it. Removing a repo (TR-1199) cancels its index at the
control plane, deletes it in lore (asking again for up to four minutes while lore's run on it is live) and
then deletes the row. That can outlast the proxy in front of a cell (60 s), so the work runs in the
background: the route answers 200 when it finished within INLINE_WAIT, else 202 `{removing: true}`, and the
row says `removing` (and `remove_error` when a run gave up) until it is gone.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time

from fastapi import Body, HTTPException, Request
from fastapi.responses import JSONResponse

from tares import github_credentials
from tares import licence
from tares.envelope import now_utc
from tares.factory import norm_repo

from . import index as indexing
from . import sync as syncing
from .cp import CpError
from .lore import _REPO_NAME, LoreClient, LoreError

log = logging.getLogger("tares.brain")

__all__ = ["MAX_CONNECT", "STATES", "lore_client", "register_routes"]

STATES = indexing.STATES   # queued, cloning, indexing, ready, failed
MAX_CONNECT = 50           # repos in one POST
GITHUB_TIMEOUT = 60        # seconds the available list waits for GitHub
REMOVE_WAIT = 240.0        # seconds removing a repo waits for lore's run on it to stop (lore answers 409 for 90 s
                           # after its last write, and a cancelled Job's pod runs on through its grace period)
REMOVE_STEP = 3.0          # seconds before the second ask; each wait is half as long again as the last
REMOVE_STEP_MAX = 20.0     # ... up to this
INLINE_WAIT = 45.0         # seconds the remove route waits for the work before it answers 202 (the proxy in front of a
                           # cell cuts a request at 60 s)
CP_WAIT = 60.0             # seconds removing a repo asks again while the control plane answers 502 (a cluster error)


def lore_client() -> LoreClient:
    """The client every route here uses (a test replaces this with the fake lore's)."""
    return LoreClient.from_env()


def check_repo(value) -> str:
    """`owner/name` as stored (lowercase), or ValueError in plain words. The lore client's own
    name check applies to both parts, and a . or .. part is refused."""
    if not isinstance(value, str):
        raise ValueError(f"{value!r} is not a repo: write it as owner/name, like glassflow/tares")
    repo = norm_repo(value)
    owner, _, name = repo.partition("/")
    if name.endswith(".git"):
        name = name[:-4]
    for part in (owner, name):
        if part in ("", ".", "..") or not _REPO_NAME.fullmatch(part):
            raise ValueError(f"{value!r} is not a repo: write it as owner/name, like glassflow/tares")
    if "_" in owner or "." in owner:
        # a GitHub owner holds letters, digits and hyphens; an underscore would let two repos
        # share one lore name (a_b/c and a/b_c)
        raise ValueError(f"{value!r} is not a repo: an owner holds letters, digits and hyphens")
    return f"{owner}/{name}"


def lore_name(repo: str) -> str:
    """The name of the repo source in lore: owner and name joined by an underscore (lore names
    cannot hold a slash). check_repo refuses an underscore in the owner, so two repos never share one."""
    return repo.replace("/", "_", 1)


def row_view(r: dict, removing: bool = False, remove_error: str | None = None) -> dict:
    """What the routes return for one row: the table's columns, the name of who added it, `removing`
    (a removal is running in the background) and `remove_error` (why the last removal gave up, None
    otherwise). `repo_source_id` stays inside the cell."""
    return {"repo": r["repo"], "state": r["state"], "files_done": r["files_done"],
            "files_total": r["files_total"], "indexed_commit": r["indexed_commit"],
            "last_synced": r["last_synced"], "error": r["error"], "place": r["place"],
            "sync_failures": r.get("sync_failures") or 0, "sync_error": r.get("sync_error"),
            "added_by": r["added_by"],
            "added_by_name": r["added_by_name"], "added_at": r["added_at"],
            "removing": removing, "remove_error": None if removing else remove_error}


def register_routes(app, host) -> None:
    store = host.store
    lock = threading.Lock()
    indexer = indexing.Indexer(store, lambda: lore_client())   # looked up per call: a test swaps lore_client
    indexer.attach(app)
    syncer = syncing.Syncer(indexer, getattr(host, "runtime", None))   # keeps ready repos current (TR-1201)
    syncer.attach(app)
    removals: dict[str, asyncio.Task] = {}   # repo -> its running removal (also keeps the task referenced)
    remove_errors: dict[str, str] = {}       # repo -> why its last removal gave up; kept in memory only

    def view(r: dict) -> dict:
        return row_view(r, r["repo"] in removals, remove_errors.get(r["repo"]))

    def need_licence():
        if not licence.ee_allowed():
            raise HTTPException(status_code=404, detail="Not Found")   # as if never mounted

    def need_brain():
        need_licence()
        if not LoreClient.configured():
            raise HTTPException(status_code=404, detail="Not Found")

    def repo_from(owner: str, name: str) -> str:
        try:
            return check_repo(f"{owner}/{name}")
        except ValueError as e:
            host.err(e, 400)

    @app.get("/api/brain")
    def brain_state():
        """{configured, repos: [{repo, state, files_done, files_total, indexed_commit,
        last_synced, error, place, sync_failures, sync_error, added_by, added_by_name, added_at, removing,
        remove_error}], oldest first. `state` is queued (`place` says where in the control plane's queue,
        when it does), cloning, indexing, ready or failed (`error` says why, in plain words). A ready repo
        whose last syncs failed stays ready: `sync_failures` counts them in a row, `sync_error` is the
        last reason, and from the third in a row `error` shows it too. `removing` is true while a removal
        runs in the background; `remove_error` says why the last one gave up."""
        need_licence()
        if not LoreClient.configured():
            return {"configured": False, "repos": []}
        return {"configured": True, "repos": [view(r) for r in store.list_brain_repos()]}

    @app.get("/api/brain/repos/available")
    async def available_repos():
        """{github, repos: [{repo, private, connected}]}: the repos the cell's GitHub connections
        can read, `connected` for the ones already on the brain. `github` is false when the cell
        has no GitHub connection."""
        need_brain()
        creds = store.list_github_credentials()
        connected = {r["repo"] for r in store.list_brain_repos()}
        seen: dict[str, dict] = {}
        failures, answered = [], 0
        for cred in creds:
            try:
                found = await asyncio.wait_for(github_credentials.list_repos_for(cred), GITHUB_TIMEOUT)
            except asyncio.TimeoutError:
                failures.append("GitHub took too long to list the repos")
                continue
            except ValueError as e:
                failures.append(str(e))
                continue
            answered += 1
            for g in found:
                full = str(g.get("full_name") or "").strip().lower()
                if full and full not in seen:
                    seen[full] = {"repo": full, "private": bool(g.get("private")),
                                  "connected": full in connected}
        if failures and not answered:
            host.err(ValueError(failures[0]), 502)
        return {"github": bool(creds), "repos": sorted(seen.values(), key=lambda r: r["repo"])}

    @app.post("/api/brain/repos", status_code=201)
    async def connect_repos(request: Request, body: dict = Body(...)):
        """Admin only. {repos: ["owner/name", ...]}: connect each repo and ask the control plane
        for its index. The whole list is checked first: a repo that is not owner/name, one named
        twice or one already connected refuses the request and connects nothing. A repo whose
        index could not be started (no GitHub connection reaches it, the control plane refused or
        is not set up) is still connected, `failed` with the reason in `error`; Retry starts it again."""
        need_brain()
        host.require_admin(request)
        extra = sorted(set(body) - {"repos"})
        if extra:
            host.err(ValueError(f"unknown field {extra[0]!r}: send {{\"repos\": [\"owner/name\"]}}"))
        given = body.get("repos")
        if not isinstance(given, list) or not given:
            host.err(ValueError("repos: a list of the repos to connect, like [\"glassflow/tares\"]"))
        if len(given) > MAX_CONNECT:
            host.err(ValueError(f"connect at most {MAX_CONNECT} repos at a time"))
        repos: list[str] = []
        for value in given:
            try:
                repo = check_repo(value)
            except ValueError as e:
                host.err(e)
            if repo in repos:
                host.err(ValueError(f"{repo} is in the list twice"))
            repos.append(repo)
        have = {r["repo"] for r in store.list_brain_repos()}
        already = [r for r in repos if r in have]
        if already:
            host.err(ValueError(f"already connected: {', '.join(already)}"), 409)
        person = host.current_person(request)
        by = person["id"] if person else None
        done: list[str] = []
        current = repos[0]
        try:
            async with lore_client() as lore:
                for repo in repos:
                    current = repo
                    made = await lore.create_repo(lore_name(repo), remote_url=indexing.remote_url(repo),
                                                  web_url_template=indexing.web_url_template(repo))
                    source = str(made.get("id") or "")
                    if not source:
                        raise LoreError("The brain did not say which repo it made.")
                    with lock:
                        added = store.add_brain_repo(repo, source, by, now_utc())
                    if added:
                        done.append(repo)
        except LoreError as e:
            kept = f" Connected before it failed: {', '.join(done)}." if done else ""
            host.err(ValueError(f"Could not connect {current}: {e}{kept}"), 502)
        finally:
            await asyncio.gather(*(indexer.start(r) for r in done))   # never raises: a failure is the row's error
        return {"repos": [view(store.get_brain_repo(r)) for r in done]}

    @app.delete("/api/brain/repos/{owner}/{name}")
    async def remove_repo(owner: str, name: str, request: Request):
        """Admin only. Remove the repo: its queued or running index is cancelled at the control
        plane (a cluster error, 502, is asked again for a minute; a wrong token, no brain index or no cancel
        route is 502 here with the row and lore untouched), lore deletes the repo and everything it indexed
        from it (source, chunks, mirror, checkout, documents), then the row goes. While lore still runs the
        index it answers 409 (for 90 s after its last write, and a cancelled Job's pod may write until its
        grace period ends): the delete is asked again with a growing wait for up to four minutes.

        The work runs in the background, so a closed connection or the proxy's 60 s cut does not stop it.
        This answers 200 `{ok, removed}` when it finished within INLINE_WAIT, the error (502, or 409 "Still
        indexing. Try again in a minute.") when it gave up in that time, and else 202 `{ok, removing}`:
        the row then says `removing`, and `remove_error` when the run gives up later. Asking again while a
        removal runs joins it; asking again after it gave up starts a new one."""
        need_brain()
        host.require_admin(request)
        repo = repo_from(owner, name)
        task = removals.get(repo)
        if task is None or task.done():
            if store.get_brain_repo(repo) is None:
                host.err(KeyError(f"{repo} is not connected to the brain"), 404)
            remove_errors.pop(repo, None)
            task = removals[repo] = asyncio.ensure_future(run_remove(repo))
        done, _ = await asyncio.wait({task}, timeout=INLINE_WAIT)
        if not done:
            return JSONResponse({"ok": True, "removing": True}, status_code=202)
        failure = task.result()
        if failure:
            host.err(ValueError(failure[1]), failure[0])
        return {"ok": True, "removed": True}

    async def run_remove(repo: str) -> tuple[int, str] | None:
        """The removal itself. Returns None when the row is gone, else (status, words) for why it is not;
        the words are also kept in `remove_errors` for the row. Never raises."""
        try:
            async with indexer.removing(repo):
                row = store.get_brain_repo(repo)
                if row is not None:   # None: a removal finished first
                    await cancel_job(repo, row)
                    if row["repo_source_id"]:
                        await forget_in_lore(repo, row["repo_source_id"])
                    store.delete_brain_repo(repo)
            remove_errors.pop(repo, None)
            return None
        except Exception as e:   # host.err raises an HTTPException; anything else is a bug we still report
            if hasattr(e, "status_code"):   # ours: the words are plain and carry no secret
                status, words = e.status_code, str(getattr(e, "detail", None) or e)
            else:   # unknown: its text may hold a token, so say it plainly and log only the type
                log.warning("removal of %s failed: %s", repo, type(e).__name__)
                status, words = 502, f"Could not remove {repo}. Try again in a minute."
            remove_errors[repo] = words
            return status, words
        finally:
            removals.pop(repo, None)

    async def cancel_job(repo: str, row: dict) -> None:
        """Cancel the repo's Job at the control plane. A 502 is a cluster error that a second call
        finishes, so it is asked again for CP_WAIT. Any other error (401, 503, 405, 501, ...) means "not
        cancelled", not "gone": this answers 502 with it and nothing in lore or the row is touched."""
        deadline, step = time.monotonic() + CP_WAIT, REMOVE_STEP
        while True:
            try:
                await indexer.cancel(row)
                return
            except CpError as e:
                if e.status != 502 or time.monotonic() + step > deadline:
                    again = "" if "Try again" in str(e) else " Try again in a minute."
                    host.err(ValueError(f"Could not stop the index of {repo}: {e}{again}"), 502)
            await asyncio.sleep(step)
            step = min(step * 1.5, REMOVE_STEP_MAX)

    async def forget_in_lore(repo: str, source: str) -> None:
        """Delete the repo's source in lore, asking again while its index run is live (409), with a
        growing wait, for REMOVE_WAIT. The row is kept until lore answers 204 or 404."""
        deadline, step = time.monotonic() + REMOVE_WAIT, REMOVE_STEP
        while True:
            try:
                async with lore_client() as lore:
                    await lore.delete_repo(source)
                return
            except LoreError as e:
                if e.status == 404:   # lore does not have it any more
                    return
                if e.status != 409:
                    host.err(ValueError(f"Could not remove {repo} from the brain: {e}"), 502)
            if time.monotonic() + step > deadline:
                host.err(ValueError("Still indexing. Try again in a minute."), 409)
            await asyncio.sleep(step)
            step = min(step * 1.5, REMOVE_STEP_MAX)

    @app.post("/api/brain/repos/{owner}/{name}/retry")
    async def retry_repo(owner: str, name: str, request: Request):
        """Admin only. A repo whose index failed goes back to queued with its error cleared and
        its index is asked for again. A repo in any other state is refused."""
        need_brain()
        host.require_admin(request)
        repo = repo_from(owner, name)
        row = store.get_brain_repo(repo)
        if row is None:
            host.err(KeyError(f"{repo} is not connected to the brain"), 404)
        if indexer.is_removing(repo) or repo in removals:
            host.err(ValueError(f"{repo} is being removed"), 409)
        async with indexer.lock(repo):
            requeued = store.requeue_brain_repo(repo, "failed")
        if not requeued:
            now = (store.get_brain_repo(repo) or row)["state"]
            host.err(ValueError(f"{repo} is {now}: only a repo whose index failed can be retried"), 409)
        remove_errors.pop(repo, None)
        await indexer.start(repo)
        fresh = store.get_brain_repo(repo)
        if fresh is None:   # a removal finished while the start ran
            host.err(KeyError(f"{repo} is not connected to the brain"), 404)
        return view(fresh)
