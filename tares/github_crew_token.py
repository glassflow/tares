"""A GitHub token for one repository, for the crew: `GET /api/factory/github/token`.

Who may ask: a key that belongs to a person (the stations of their crew included), the system
credential, or, with auth off, a program that is not a browser and calls the cell by `localhost`
or an IP address. The repository must be one that a project the caller can see names, on its
hand-over or on a ticket. The credential is a github.com one that follows the repository, App
credentials before personal tokens. A personal token is considered for an admin key only (the
system credential and auth off count as one).

An App token is asked for with PERMISSIONS only (a self-held App also narrows it to the one
repository). A personal token is handed out as stored. The answer carries the identity to commit
as. Nothing here prints a token or puts one in an error.
"""
from __future__ import annotations

import hashlib
import ipaddress
import re
from datetime import datetime, timezone
from typing import NamedTuple

import httpx

from . import factory, github_app, github_credentials
from .factory_api import NO_PERSON

PERMISSIONS = {"contents": "write", "pull_requests": "write", "checks": "write",
               "metadata": "read"}
MIN_LIFE = 600                      # seconds an App token handed out has left, at least
IDENTITY_KEY = "commit_identity"    # where a credential's config keeps the looked-up identity
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")
# a Host header: a name or address, an IPv6 address in brackets, either with a port
_HOST = re.compile(r"(?:\[(?P<v6>[^\[\]]+)\]|(?P<name>[^:\[\]@/]+))(?::[0-9]+)?")
_NOBODY = {"login": None, "name": None, "email": None}


class Refused(Exception):
    """No token: the HTTP status and the answer's body."""

    def __init__(self, status: int, detail: str, code: str | None = None):
        super().__init__(detail)
        self.status = status
        self.body = ({"code": code} if code else {}) | {"detail": detail}


class Follow(NamedTuple):
    """How a credential follows a repository: for an App, the installation to mint on and (self-held
    App) the repository's name as GitHub lists it. Both None for a personal token."""
    installation: int | None = None
    repo_name: str | None = None


async def token_for_repo(store, h, request, repo_param: str) -> dict:
    """The answer for `?repo=`: {token, expires_at, kind, login, name, email}. Refused otherwise."""
    _check_caller(h, request)
    repo = _parse_repo(repo_param)
    # a credential for another GitHub host is never used: its token must not travel to github.com
    creds = [c for c in store.list_github_credentials() if _on_github_com(c)]
    if not creds:
        raise Refused(404, "no github.com credential is stored (Settings > GitHub)",
                      "no_credential")
    if not _named_by_a_project(store, h, repo):
        raise Refused(403, f"no project you can see names the repository {repo}",
                      "repo_not_allowed")
    admin = h.is_admin(request)
    if not admin:     # a personal token is a person's own access: only an admin key is handed one
        creds = [c for c in creds if github_credentials.is_app(c)]
    creds.sort(key=lambda c: not github_credentials.is_app(c))     # stable: stored order within
    cred, follow = await _first_following(creds, repo, admin)
    token, expires_at, grant = await _token(store, cred, follow)
    identity = await _identity(store, cred, token, grant)
    kind = cred.get("kind") or "token"
    return {"token": token, "expires_at": expires_at,
            "kind": "pat" if kind == "token" else kind, **identity}


def _check_caller(h, request) -> None:
    if h.auth_off() and not _program_by_address(request):
        raise Refused(403, "with auth off, a GitHub token goes only to a program (not a browser) "
                           "that calls the cell by localhost or an IP address",
                      "repo_not_allowed")
    if not h.is_system(request) and not h.current_person(request):
        raise Refused(403, NO_PERSON, "repo_not_allowed")


def _program_by_address(request) -> bool:
    """Whether the request carries no browser header and is addressed to `localhost` or an IP
    address rather than to another name."""
    if "origin" in request.headers or "sec-fetch-site" in request.headers:
        return False
    # the header as sent: `request.url` stands in the server's own address for a malformed one
    sent = _HOST.fullmatch(request.headers.get("host", ""))
    if not sent:
        return False
    host = (sent["v6"] or sent["name"]).lower()
    if host == "localhost":
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _parse_repo(value: str) -> str:
    try:
        repo = factory.repo_ok(value)
    except ValueError:
        repo = None
    # `.` and `..` pass as characters but would change the path of a GitHub request
    if not repo or any(part in (".", "..") for part in repo.split("/")):
        raise Refused(400, "give the repository as ?repo=owner/name")
    return repo


def _on_github_com(cred: dict) -> bool:
    api = (cred.get("api_url") or "").strip().rstrip("/").lower()
    return api in ("", github_app.API_DEFAULT)


def _named_by_a_project(store, h, repo: str) -> bool:
    """Whether a project the caller can see names `repo` on its hand-over or as a ticket's own
    repository. A repository seen only in a pull request link does not count."""
    want = repo.lower()
    ids = [p["id"] for p in store.list_projects() if h.can_see(p)]
    for handover in store.handovers_by_project(ids).values():
        if any(str(r.get("name") or "").lower() == want for r in handover.get("repos") or []):
            return True
    return any(str(t.get("repo") or "").lower() == want
               for tickets in store.tickets_by_project(ids).values() for t in tickets)


async def _follows(cred: dict, repo: str) -> Follow | None:
    """How `cred` follows `repo`, or None when it does not. Raises when that could not be told."""
    want = repo.lower()
    kind = cred.get("kind") or "token"
    if kind == "app_broker":
        followed = (cred.get("config") or {}).get("repositories") or []
        if want not in {str(r).lower() for r in followed}:
            return None
        installations = github_app.installations(cred)
        if not installations:
            raise ValueError("the credential has no installation")
        return Follow(installations[0]["id"])
    if kind == "app":
        failed = None
        for inst in github_app.installations(cred):
            try:
                listed = await github_app.installation_repos(cred, inst["id"])
            except ValueError as e:     # another installation may still cover the repository
                failed = failed or e
                continue
            for r in listed:
                if r["full_name"].lower() == want:
                    return Follow(inst["id"], r["full_name"].split("/", 1)[-1])
        if failed:
            raise failed
        return None
    if kind != "token" or not cred.get("token"):
        return None
    async with httpx.AsyncClient(timeout=15) as cx:
        r = await cx.get(f"{github_app.API_DEFAULT}/repos/{repo}",
                         headers=github_credentials._headers(cred["token"]))
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise ValueError(f"GitHub returned {r.status_code} for {repo}")
    permissions = r.json().get("permissions") or {}
    return Follow() if permissions.get("push") is True else None


def _plain(e: Exception, what: str) -> str:
    """`what` and why. Only a ValueError's own words are passed on (ours, written to be read);
    any other error is named by its type, since its text may quote a request."""
    return f"{what}: {e}" if type(e) is ValueError else f"{what} ({type(e).__name__})"


async def _first_following(creds: list[dict], repo: str, admin: bool) -> tuple[dict, Follow]:
    failed = None
    for cred in creds:
        try:
            follow = await _follows(cred, repo)
        except Exception as e:      # a later credential may still follow the repository
            failed = failed or _plain(e, f"could not check GitHub credential {cred['name']!r}")
            continue
        if follow:
            return cred, follow
    if failed:
        raise Refused(502, failed)
    which = ("no GitHub credential follows the repository" if admin else
             "personal tokens go to admin keys only, and no GitHub App credential follows the "
             "repository")
    raise Refused(403, f"{which} {repo} (Settings > GitHub)", "repo_not_allowed")


async def _token(store, cred: dict, follow: Follow):
    """(token, expires_at or None, the App's Grant or None)."""
    try:
        if not github_credentials.is_app(cred):
            return await github_credentials.get_token(store, cred["name"]), None, None
        grant = await github_app.installation_grant(cred, follow.installation, MIN_LIFE,
                                                    PERMISSIONS, follow.repo_name)
    except github_app.PermissionsMissing:
        raise Refused(409, "the GitHub App lacks a permission the crew needs (contents, pull "
                           "requests and checks: write; metadata: read). Approve it for the App "
                           "on GitHub, then ask again", "app_permissions_missing")
    except Exception as e:
        raise Refused(502, _plain(e, f"could not get a token from GitHub credential "
                                     f"{cred['name']!r}"))
    expires = datetime.fromtimestamp(grant.exp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return grant.token, expires, grant


# ── who to commit as ────────────────────────────────────────────────────────────

def _bot(slug: str | None, bot_id: int | None) -> dict:
    if not slug:
        return dict(_NOBODY)
    handle = f"{slug}[bot]"
    return {"login": handle, "name": handle,
            "email": f"{bot_id}+{handle}@users.noreply.github.com" if bot_id else None}


async def _lookup(cred: dict, path: str, token: str) -> dict:
    """A GitHub user object, {} when it could not be read."""
    try:
        async with httpx.AsyncClient(timeout=15) as cx:
            r = await cx.get(f"{github_app.API_DEFAULT}{path}",
                             headers=github_credentials._headers(token))
        if r.status_code == 200 and isinstance(r.json(), dict):
            return r.json()
        why = f"GitHub returned {r.status_code}"
    except Exception as e:
        why = type(e).__name__
    print(f"[github {cred['name']}] could not look up who to commit as: {why}")
    return {}


def _saved(cred: dict, key: str) -> dict | None:
    """The identity kept on the credential, if it was looked up for `key` (an App's slug, or a
    digest of a personal token)."""
    saved = (cred.get("config") or {}).get(IDENTITY_KEY)
    return saved if isinstance(saved, dict) and saved.get("for") == key else None


def _save(store, name: str, key: str, identity: dict) -> None:
    fresh = store.get_github_credential(name)     # a webhook may have changed the config meanwhile
    if fresh:
        store.update_github_credential_config(
            name, {**(fresh.get("config") or {}), IDENTITY_KEY: {"for": key, **identity}})


async def _identity(store, cred: dict, token: str, grant) -> dict:
    """{login, name, email} to commit as, all None when it cannot be told and none is kept."""
    kind = cred.get("kind") or "token"
    if kind == "app_broker":
        return _bot(grant.app_slug, grant.bot_id)
    if kind == "app":
        slug = str((cred.get("config") or {}).get("slug") or "")
        if not _SLUG.fullmatch(slug):
            return dict(_NOBODY)
        saved = _saved(cred, slug)
        if saved:
            return _bot(slug, saved.get("bot_id"))
        user = await _lookup(cred, f"/users/{slug}[bot]", token)
        bot_id = github_app.positive_int(user.get("id"))
        if not bot_id:
            return dict(_NOBODY)
        _save(store, cred["name"], slug, {"bot_id": bot_id})
        return _bot(slug, bot_id)
    key = "token:" + hashlib.sha256(token.encode()).hexdigest()[:16]
    saved = _saved(cred, key)
    if saved:
        return {k: saved.get(k) for k in _NOBODY}
    user = await _lookup(cred, "/user", token)
    login = user.get("login")
    if not login:
        return dict(_NOBODY)
    user_id = github_app.positive_int(user.get("id"))
    identity = {"login": login, "name": user.get("name") or login,
                "email": user.get("email")
                or (f"{user_id}+{login}@users.noreply.github.com" if user_id else None)}
    if identity["email"]:
        _save(store, cred["name"], key, identity)
    return identity
