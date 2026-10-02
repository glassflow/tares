"""GitHub connector, the personal token path: polls one repository for its commits and for pull
requests opened, merged or closed. (With the GitHub App installed, every event of every repo
arrives by webhook instead: see github_app.py. The person picks a credential, never a mode.)

Commits: the cursor is the newest seen commit SHA; each poll fetches the latest commits (newest
first) and ingests everything above it, so there are no duplicates. Pull requests: a second cursor
(`<source>#prs`) holds the newest `updated_at` seen; each poll lists pull requests by last update
and reports what happened since, without remembering per-PR state (github_events.pr_poll_events).
The first poll sets that cursor and imports no old pull requests.

Events follow the GitHub event contract (github_events.py) shared with the App connector: commits
are `commit` events keyed `owner/repo`, pull requests `pull_request` events keyed
`owner/repo#<number>` with an `action` label. The contract's labels are always set, whatever labels
the source declares, so a trigger on `action=merged` works on any GitHub source.

Auth: a stored credential (`credential`, resolved at every poll) or this source's own `token`;
required for private repos, recommended otherwise (60 requests/hour per IP without one).
`discover()` validates the repo, finds its default branch, and proposes labels.
"""
from __future__ import annotations

from datetime import datetime, timezone

import time

import httpx

from ..envelope import Envelope, now_utc
from . import github_events as gh
from .base import Connector

_API_DEFAULT = "https://api.github.com"
FILES_MAX = 20          # files kept per commit payload
PATCH_MAX = 4_000       # chars kept per file patch
RATELIMIT_FLOOR = 500   # below this many remaining requests, stop fetching file lists for a while


def _parse_iso(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _normalize_repo(repo) -> str:
    """Accept 'owner/name' but also pasted URLs (https://github.com/owner/name[.git][/…])."""
    r = str(repo or "").strip().rstrip("/")
    if "://" in r:
        r = r.split("://", 1)[1]
    parts = r.split("/")
    if parts and "." in parts[0]:   # a hostname (github.com, GHE) — owner names can't contain dots
        parts = parts[1:]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise ValueError(f"repo must be owner/name (got {repo!r})")
    name = parts[1][:-4] if parts[1].endswith(".git") else parts[1]
    return f"{parts[0]}/{name}"


def _headers(token: str | None) -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


class GithubConnector(Connector):
    CONFIG_SCHEMA = {
        "repo": {"type": "string", "required": True, "discover_input": True,
                 "help": "owner/name, e.g. glassflow/tares (a pasted GitHub URL works too)"},
        "branch": {"type": "string",
                   "help": "branch to follow (empty = the repo's default branch, labeled by its "
                           "real name). One source follows one branch; add a source per branch "
                           "to watch several"},
        "credential": {"type": "string", "discover_input": True,
                       "help": "name of a stored GitHub credential (Settings > GitHub); the token "
                               "is read from it at every poll, so rotating the credential rotates "
                               "this source too. Set this or `token`, not both"},
        "token": {"type": "string", "secret": True, "discover_input": True,
                  "help": "GitHub token for this source only; required for private repos, "
                          "recommended otherwise: without one GitHub allows 60 API requests/hour "
                          "per IP. Prefer a stored credential"},
        "limit": {"type": "number", "default": 20,
                  "help": "newest commits fetched per poll (the first poll imports this many)"},
        "files": {"type": "boolean", "default": True, "advanced": True,
                  "help": "with a token, fetch each new commit's changed files (paths and patches, "
                          "capped) into the payload; one extra API call per commit"},
        "prs": {"type": "boolean", "default": True, "advanced": True,
                "help": "also report pull requests opened, merged or closed (one extra API call "
                        "per poll). Reviews, comments and CI runs need the GitHub App"},
        "api_url": {"type": "string", "advanced": True,
                    "help": "GitHub API base for GitHub Enterprise (default https://api.github.com)"},
    }

    PROVIDES = [
        {"name": "repo", "primary": True, "help": "owner/name"},
        {"name": "author", "help": "commit or pull request author (GitHub login)"},
        {"name": "branch", "help": "branch followed, or a pull request's head branch"},
        {"name": "action", "help": "pull requests: opened | merged | closed"},
        {"name": "number", "help": "pull request number"},
        {"name": "base", "help": "the branch a pull request merges into"},
        {"name": "sha", "help": "commit sha, or a pull request's head commit"},
        {"name": "title", "help": "pull request title"},
        {"name": "url", "help": "link to the pull request"},
    ]

    async def _get(self, cx, url: str, token: str | None, params: dict, etag_key: str, repo: str):
        """One conditional GET with the shared error surface. Returns parsed JSON, or None on 304
        (a 304 costs nothing against the rate limit, so steady polling only spends quota on
        actual change)."""
        etags = getattr(self, "_etags", None)
        if etags is None:
            etags = self._etags = {}
        headers = _headers(token)
        if etag_key in etags:
            headers["If-None-Match"] = etags[etag_key]
        try:
            r = await cx.get(url, params=params, headers=headers)
        except Exception as e:
            raise ValueError(f"could not reach GitHub: {e}")
        if r.status_code == 304:
            return None
        if r.status_code in (403, 429) and r.headers.get("x-ratelimit-remaining") == "0":
            raise ValueError("GitHub rate limit exhausted (unauthenticated: 60 requests/hour per IP)"
                             "; set a token or raise the poll interval")
        if r.status_code == 404:
            raise ValueError(f"repo {repo!r} not found"
                             + ("; check owner/name (the token may also lack access)" if token
                                else "; private repos need a token"))
        if r.status_code == 401:
            raise ValueError("GitHub rejected the token (401 unauthorized)")
        if r.status_code != 200:
            raise ValueError(f"GitHub returned {r.status_code} for {repo!r}")
        if r.headers.get("etag"):
            etags[etag_key] = r.headers["etag"]
        return r.json()

    async def _token(self, repo: str | None = None) -> str | None:
        """The token to use: the stored credential named in `credential` (resolved now, so a
        rotation is picked up on the next poll; an App credential mints an installation token for
        the installation covering `repo`), else this source's own `token`."""
        c = self.cfg.config
        if c.get("credential"):
            from ..github_credentials import get_token
            return await get_token(self.store, c["credential"], repo)
        return c.get("token") or None

    def _api(self) -> str:
        c = self.cfg.config
        if not c.get("api_url") and c.get("credential"):
            from ..github_credentials import resolve_api_url
            url = resolve_api_url(self.store, c["credential"])
            if url:
                return url.rstrip("/")
        return (c.get("api_url") or _API_DEFAULT).rstrip("/")

    async def poll(self):
        c = self.cfg.config
        repo = _normalize_repo(c["repo"])
        api = self._api()
        token = await self._token(repo)
        limit = int(c.get("limit", 20))
        async with httpx.AsyncClient(timeout=15) as cx:
            branch = str(c.get("branch") or "")
            if not branch:
                # no branch configured: follow the repo's default branch — resolved once by name
                # so the branch label is real ("main"), not blank
                if not getattr(self, "_default_branch", None):
                    meta = await self._get(cx, f"{api}/repos/{repo}", token, {}, "meta", repo)
                    self._default_branch = (meta or {}).get("default_branch") or "main"
                branch = self._default_branch
            commits = await self._get(cx, f"{api}/repos/{repo}/commits", token,
                                      {"per_page": limit, "sha": branch}, f"c:{branch}", repo)
            new = []
            if isinstance(commits, list) and commits:
                cursor = self.store.get_cursor(self.cfg.name)
                for commit in commits:           # newest first; stop at the last-seen SHA
                    if commit.get("sha") == cursor:
                        break
                    new.append(commit)
                if token and c.get("files", True):
                    # the list endpoint has no file list; one more call per new commit gives the
                    # agent what changed without a tool call. Only with a token (60/h
                    # unauthenticated would not survive it), and backed off when quota runs low.
                    for commit in new:
                        files = await self._files(cx, api, repo, commit.get("sha", ""), token)
                        if files is not None:
                            commit["files"] = files["files"]
                            commit["files_truncated"] = files["truncated"]
                self.store.set_cursor(self.cfg.name, commits[0].get("sha"))
            prs = []
            if c.get("prs", True):
                try:
                    prs = await self._poll_prs(cx, api, repo, token)
                except ValueError as e:
                    # commits still land: a fine-grained token without pull request access answers
                    # 404 here while its commits read fine. Said once per source, not every poll.
                    if not getattr(self, "_prs_warned", False):
                        print(f"[github {self.cfg.name}] pull requests not readable ({e}); "
                              "commits only. Give the token Pull requests: read, or set prs off")
                        self._prs_warned = True
        envs = [self._commit_envelope({**commit, "_branch": branch}, repo)
                for commit in reversed(new)]  # chronological
        return sorted(envs + prs, key=lambda e: e.event_time)

    async def _poll_prs(self, cx, api: str, repo: str, token: str | None) -> list[Envelope]:
        """Pull requests opened, merged or closed since the last poll. Lists them by last update
        (newest first, one page of 50) and stops at the first one not updated since the cursor;
        the first poll only sets the cursor, so connecting a busy repo imports no history."""
        key = f"{self.cfg.name}#prs"
        since = gh.parse_time(self.store.get_cursor(key))
        pulls = await self._get(cx, f"{api}/repos/{repo}/pulls", token,
                                {"state": "all", "sort": "updated", "direction": "desc",
                                 "per_page": 50}, "prs", repo)
        if not isinstance(pulls, list):
            if since is None:          # 304 or empty: still start the clock
                self.store.set_cursor(key, now_utc().isoformat())
            return []
        newest = max((gh.parse_time(p.get("updated_at")) for p in pulls
                      if gh.parse_time(p.get("updated_at"))), default=None)
        if since is None:
            # first poll: from now on, no backfill (GitHub's clock ahead of ours: from its newest)
            start = max(newest, now_utc()) if newest else now_utc()
            self.store.set_cursor(key, start.isoformat())
            return []
        out = []
        for pr in pulls:
            updated = gh.parse_time(pr.get("updated_at"))
            if updated is None or updated <= since:
                break
            for action, when in gh.pr_poll_events(pr, repo, since):
                out.append(self._pr_envelope(pr, repo, action, when))
        if newest and newest > since:
            self.store.set_cursor(key, newest.isoformat())
        return out

    def _pr_envelope(self, pr: dict, repo: str, action: str, when) -> Envelope:
        stored = {**pr, "_github_event": "pull_request", "_action": action, "_repo": repo}
        labels = self.labels_for(self.label_context(stored))
        return Envelope(source=self.cfg.name, source_type=self.cfg.type,
                        key_value=f"{repo}#{pr.get('number')}", event_type="pull_request",
                        text=gh.pr_text(labels)[:300], event_time=when or now_utc(),
                        payload=stored, labels=labels)

    async def _files(self, cx, api: str, repo: str, sha: str, token: str) -> dict | None:
        """The changed files of one commit (GET /repos/{repo}/commits/{sha}), trimmed so a payload
        stays a payload: at most FILES_MAX files, each patch cut at PATCH_MAX chars, with a
        `truncated` flag when anything was cut. None when the call fails or the rate limit is
        nearly spent (the commit still lands, just without files)."""
        if not sha:
            return None
        if getattr(self, "_ratelimit_low_until", 0) > time.monotonic():
            return None
        try:
            r = await cx.get(f"{api}/repos/{repo}/commits/{sha}", headers=_headers(token))
        except Exception:
            return None
        try:
            remaining = int(r.headers.get("x-ratelimit-remaining", "1000"))
        except ValueError:
            remaining = 1000
        if remaining < RATELIMIT_FLOOR:
            print(f"[github {self.cfg.name}] rate limit low ({remaining} left); skipping commit "
                  f"file lists for 10 minutes")
            self._ratelimit_low_until = time.monotonic() + 600
        if r.status_code != 200:
            return None
        raw = (r.json() or {}).get("files") or []
        truncated = len(raw) > FILES_MAX
        files = []
        for f in raw[:FILES_MAX]:
            patch = f.get("patch") or ""
            if len(patch) > PATCH_MAX:
                patch, truncated = patch[:PATCH_MAX], True
            files.append({"filename": f.get("filename"), "status": f.get("status"),
                          "additions": f.get("additions"), "deletions": f.get("deletions"),
                          "patch": patch})
        return {"files": files, "truncated": truncated}

    def labels_for(self, context: dict | None = None) -> dict:
        # the event contract's labels always, whatever the source declares; declared labels win
        contract = {k: v for k, v in (context or {}).items()
                    if v not in (None, "") and k != "author_name"}
        return {**contract, **super().labels_for(context)}

    def label_context(self, commit: dict | None) -> dict:
        # repo comes from config; branch from config or the `_branch` the poller stamped into the
        # payload (multi-branch mode); author/sha from the commit. Shared by ingest and backfill
        # so the synthesized labels survive a relabel. A stored pull request (stamped
        # `_github_event`) gets the contract's pull request labels.
        commit = commit or {}
        c = self.cfg.config
        if commit.get("_github_event") == "pull_request":
            repo = commit.get("_repo") or c.get("repo")
            return gh.pr_fields(commit, repo, commit.get("_action") or "")
        cm = commit.get("commit") or {}
        cm_author = cm.get("author") or {}
        login = (commit.get("author") or {}).get("login") or cm_author.get("name") or "unknown"
        try:
            repo = _normalize_repo(c.get("repo"))
        except ValueError:
            repo = c.get("repo")
        return {"repo": repo, "branch": c.get("branch") or commit.get("_branch") or "",
                "author": login, "author_name": cm_author.get("name"),
                "sha": commit.get("sha", "")}

    def _commit_envelope(self, commit: dict, repo_or_ctx) -> Envelope:
        sha = commit.get("sha", "")
        cm = commit.get("commit") or {}
        cm_author = cm.get("author") or {}
        login = (commit.get("author") or {}).get("login") or cm_author.get("name") or "unknown"
        summary = (cm.get("message") or "").strip().splitlines()[0] if cm.get("message") else ""
        ctx = self.label_context(commit)
        fallback = repo_or_ctx["repo"] if isinstance(repo_or_ctx, dict) else repo_or_ctx
        labels, key = self.keyed(ctx, fallback=fallback)
        return Envelope(
            source=self.cfg.name, source_type=self.cfg.type, key_value=key,
            event_type="commit", text=f"{sha[:7]} {login}: {summary}"[:300],
            event_time=_parse_iso(cm_author.get("date")) or now_utc(),
            payload=commit, labels=labels,
        )

    @classmethod
    async def discover(cls, config: dict) -> dict:
        if not config.get("repo"):
            raise ValueError("enter the repo (owner/name) first, then Discover")
        repo = _normalize_repo(config["repo"])
        api = (config.get("api_url") or _API_DEFAULT).rstrip("/")
        token = config.get("token")
        async with httpx.AsyncClient(timeout=15) as cx:
            try:
                meta = await cx.get(f"{api}/repos/{repo}", headers=_headers(token))
            except Exception as e:
                raise ValueError(f"could not reach GitHub: {e}")
            if meta.status_code == 404:
                raise ValueError(f"repo {repo!r} not found"
                                 + ("; check owner/name (the token may also lack access)" if token
                                    else " (private repos need a token)"))
            if meta.status_code != 200:
                raise ValueError(f"GitHub returned {meta.status_code} for {repo!r}")
            info = meta.json()
            branch = info.get("default_branch") or "main"
            commits = (await cx.get(f"{api}/repos/{repo}/commits",
                                    params={"per_page": 10, "sha": branch},
                                    headers=_headers(token))).json()
        authors = sorted({(c.get("author") or {}).get("login")
                          or ((c.get("commit") or {}).get("author") or {}).get("name")
                          for c in commits if isinstance(c, dict)} - {None})
        return {
            "connector": "github",
            "summary": f"{repo} · default branch {branch}"
                       + (" · private" if info.get("private") else "")
                       + (f" · recent authors: {', '.join(authors[:6])}" if authors else ""),
            "recent_authors": authors[:8],
            "proposed_config": {
                "repo": repo, "branch": branch,
                # keep the credential reference the user chose (the daemon resolved it into a
                # token for this call; the token itself must not land in the proposal)
                **({"credential": config["credential"]} if config.get("credential") else {}),
                "labels": [{"name": "repo", "field": "repo", "primary": True},
                           {"name": "author", "field": "author"},
                           {"name": "branch", "field": "branch"}],
            },
        }
