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
A new row is `queued`; starting its index is TR-1198 and removing it from lore is TR-1199.
"""
from __future__ import annotations

import asyncio
import threading

from fastapi import Body, HTTPException, Request

from tares import github_credentials
from tares import licence
from tares.envelope import now_utc
from tares.factory import norm_repo

from .lore import _REPO_NAME, LoreClient, LoreError

__all__ = ["MAX_CONNECT", "STATES", "lore_client", "register_routes"]

STATES = ("queued", "indexing", "indexed", "failed")
MAX_CONNECT = 50           # repos in one POST
GITHUB_TIMEOUT = 60        # seconds the available list waits for GitHub


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


def row_view(r: dict) -> dict:
    """What the routes return for one row: the table's columns, and the name of who added it.
    `repo_source_id` stays inside the cell."""
    return {"repo": r["repo"], "state": r["state"], "files_done": r["files_done"],
            "files_total": r["files_total"], "indexed_commit": r["indexed_commit"],
            "last_synced": r["last_synced"], "error": r["error"], "added_by": r["added_by"],
            "added_by_name": r["added_by_name"], "added_at": r["added_at"]}


def register_routes(app, host) -> None:
    store = host.store
    lock = threading.Lock()

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
        last_synced, error, added_by, added_by_name, added_at}]}, oldest first."""
        need_licence()
        if not LoreClient.configured():
            return {"configured": False, "repos": []}
        return {"configured": True, "repos": [row_view(r) for r in store.list_brain_repos()]}

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
        """Admin only. {repos: ["owner/name", ...]}: connect each repo (queued). The whole list
        is checked first: a repo that is not owner/name, one named twice or one already
        connected refuses the request and connects nothing."""
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
                    made = await lore.create_repo(lore_name(repo),
                                                  remote_url=f"https://github.com/{repo}.git")
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
        return {"repos": [row_view(store.get_brain_repo(r)) for r in done]}

    @app.delete("/api/brain/repos/{owner}/{name}")
    def remove_repo(owner: str, name: str, request: Request):
        """Admin only. Take the repo off this cell's list. What lore indexed stays in lore."""
        need_brain()
        host.require_admin(request)
        repo = repo_from(owner, name)
        if not store.delete_brain_repo(repo):
            host.err(KeyError(f"{repo} is not connected to the brain"), 404)
        return {"ok": True, "removed": True}

    @app.post("/api/brain/repos/{owner}/{name}/retry")
    def retry_repo(owner: str, name: str, request: Request):
        """Admin only. A repo whose index failed goes back to queued with its error cleared. A
        repo in any other state is refused."""
        need_brain()
        host.require_admin(request)
        repo = repo_from(owner, name)
        row = store.get_brain_repo(repo)
        if row is None:
            host.err(KeyError(f"{repo} is not connected to the brain"), 404)
        if not store.requeue_brain_repo(repo, "failed"):
            now = (store.get_brain_repo(repo) or row)["state"]
            host.err(ValueError(f"{repo} is {now}: only a repo whose index failed can be retried"), 409)
        return row_view(store.get_brain_repo(repo))
