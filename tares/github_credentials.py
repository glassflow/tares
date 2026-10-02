"""GitHub credentials: a token stored once, referenced by name from sources and MCP servers.

Before this, every `github` source carried its own token and an agent's GitHub MCP server carried
another copy. A stored credential is one place to paste, test and rotate it: sources set
`credential: <name>` instead of `token`, an MCP server sets `auth_value: credential:github/<name>`,
and both resolve the token at use time, so rotating the credential rotates everything at once.

Kinds: `token` (a personal token), `app` (a GitHub App the cell holds: app id, private key,
installations) and `app_broker` (an App whose key stays with a broker, Tares Cloud's control plane,
which hands this cell tokens for its own installation). Callers ask `get_token()` for a token and
never care which kind is behind it; github_app.py mints and caches App tokens.
"""
from __future__ import annotations

import time

import httpx

API_DEFAULT = "https://api.github.com"
CREDENTIAL_PREFIX = "credential:github/"
_REPOS_TTL = 300   # seconds a repo listing is reused (the wizard re-reads it on every keystroke)
_repos_cache: dict[str, tuple[float, list[dict]]] = {}


def _headers(token: str) -> dict:
    return {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {token}"}


def is_credential_ref(value: str | None) -> bool:
    return str(value or "").startswith(CREDENTIAL_PREFIX)


def credential_name(ref: str) -> str:
    """`credential:github/<name>` -> `<name>`; a bare name is returned as is."""
    ref = str(ref or "").strip()
    return ref[len(CREDENTIAL_PREFIX):] if ref.startswith(CREDENTIAL_PREFIX) else ref


APP_KINDS = ("app", "app_broker")


def is_app(cred: dict | None) -> bool:
    return bool(cred) and cred.get("kind") in APP_KINDS


def resolve_github_token(store, ref: str | None) -> str | None:
    """The personal token behind a credential name or `credential:github/<name>`; None if unknown,
    empty, or an App credential (whose tokens are minted: use `get_token`). Never raises."""
    name = credential_name(ref)
    if not name:
        return None
    cred = store.get_github_credential(name)
    if not cred or is_app(cred):
        return None
    return cred.get("token") or None


async def get_token(store, ref: str | None, repo: str | None = None,
                    min_life: int | None = None) -> str:
    """A token for the credential named by `ref`, whatever its kind: the stored personal token, or
    an installation token (the installation covering `repo`, or the only one). Raises ValueError
    with a message that names the cause, for the caller to surface (a source's last error, an MCP
    connect failure, a test button)."""
    name = credential_name(ref)
    cred = store.get_github_credential(name) if name else None
    if not cred:
        raise ValueError(f"GitHub credential {name!r} not found (Settings > GitHub)")
    if is_app(cred):
        from .github_app import REFRESH_MARGIN, token_for
        return await token_for(cred, repo, min_life or REFRESH_MARGIN)
    if not cred.get("token"):
        raise ValueError(f"GitHub credential {name!r} has no token (Settings > GitHub)")
    return cred["token"]


def resolve_api_url(store, ref: str | None) -> str | None:
    """The API base a credential was created for (GitHub Enterprise), if any."""
    cred = store.get_github_credential(credential_name(ref))
    return (cred or {}).get("api_url") or None


# never on the wire: the key that mints tokens, the secrets that verify and broker them
_SECRET_CONFIG = ("private_key", "webhook_secret", "client_secret", "broker_secret")


def redact(cred: dict) -> dict:
    """The wire form: everything except the token and the App's secrets, plus whether each is set."""
    kind = cred.get("kind") or "token"
    out = {"name": cred["name"], "kind": kind,
           "api_url": cred.get("api_url") or "", "account": cred.get("account") or "",
           "token_configured": bool(cred.get("token")),
           "created_at": cred.get("created_at"), "updated_at": cred.get("updated_at")}
    if kind in APP_KINDS:
        cfg = cred.get("config") or {}
        from .github_app import installations
        out.update({
            "app_id": cfg.get("app_id") or "", "slug": cfg.get("slug") or "",
            "html_url": cfg.get("html_url") or "", "owner": cfg.get("owner") or "",
            "source": cfg.get("source") or "",
            "installations": installations(cred),
            "key_configured": bool(cfg.get("private_key")) if kind == "app" else False,
            "webhook_secret_configured": bool(cfg.get("webhook_secret")),
            "broker": kind == "app_broker",
        })
    return out


async def test_credential(token: str, api_url: str | None = None) -> dict:
    """GET /user with the token: the login it belongs to and, for classic tokens, the scopes.
    Raises ValueError with a message that names the cause."""
    api = (api_url or API_DEFAULT).rstrip("/")
    async with httpx.AsyncClient(timeout=15) as cx:
        try:
            r = await cx.get(f"{api}/user", headers=_headers(token))
        except Exception as e:
            raise ValueError(f"could not reach GitHub: {e}")
    if r.status_code == 401:
        raise ValueError("GitHub rejected the token (401 unauthorized)")
    if r.status_code != 200:
        raise ValueError(f"GitHub returned {r.status_code} for /user")
    body = r.json() if r.content else {}
    scopes = [s.strip() for s in (r.headers.get("x-oauth-scopes") or "").split(",") if s.strip()]
    return {"login": body.get("login") or "", "name": body.get("name") or "",
            "scopes": scopes}


async def list_repos(name: str, token: str, api_url: str | None = None, query: str = "",
                     use_cache: bool = True) -> list[dict]:
    """Repos the token can see, newest push first: `[{full_name, default_branch, private,
    pushed_at}]`. Paginates /user/repos; the full list is cached per credential for 5 minutes and
    the query filters the cached list, so a wizard's search box does not hit GitHub per keystroke.
    Fine-grained tokens list only the repositories they were granted."""
    api = (api_url or API_DEFAULT).rstrip("/")
    cache_key = f"{name}@{api}"
    now = time.monotonic()
    hit = _repos_cache.get(cache_key) if use_cache else None
    if hit and now - hit[0] < _REPOS_TTL:
        repos = hit[1]
    else:
        repos = []
        async with httpx.AsyncClient(timeout=20) as cx:
            page = 1
            while page <= 30:      # 3,000 repos is plenty for a picker
                try:
                    r = await cx.get(f"{api}/user/repos", headers=_headers(token),
                                     params={"affiliation": "owner,collaborator,organization_member",
                                             "per_page": 100, "sort": "pushed", "page": page})
                except Exception as e:
                    raise ValueError(f"could not reach GitHub: {e}")
                if r.status_code == 401:
                    raise ValueError("GitHub rejected the token (401 unauthorized)")
                if r.status_code != 200:
                    raise ValueError(f"GitHub returned {r.status_code} for /user/repos")
                batch = r.json() or []
                if not isinstance(batch, list):
                    break
                for repo in batch:
                    if not isinstance(repo, dict) or not repo.get("full_name"):
                        continue
                    repos.append({"full_name": repo["full_name"],
                                  "default_branch": repo.get("default_branch") or "main",
                                  "private": bool(repo.get("private")),
                                  "pushed_at": repo.get("pushed_at")})
                if len(batch) < 100:
                    break
                page += 1
        _repos_cache[cache_key] = (now, repos)
    q = (query or "").strip().lower()
    if not q:
        return list(repos)
    return [r for r in repos if q in r["full_name"].lower()]


async def list_repos_for(cred: dict, query: str = "") -> list[dict]:
    """Repos a credential can see, whatever its kind: a token's repos, or every repo of every
    installation of an App (each listed by its installation, cached five minutes)."""
    if not is_app(cred):
        return await list_repos(cred["name"], cred["token"], cred.get("api_url") or None, query)
    from .github_app import installation_repos, installations
    repos: list[dict] = []
    errors = []
    for inst in installations(cred):
        try:
            repos += await installation_repos(cred, inst["id"])
        except ValueError as e:     # one suspended or removed installation must not hide the rest
            errors.append(e)
    if errors and not repos:
        raise errors[0]
    q = (query or "").strip().lower()
    return [r for r in repos if q in r["full_name"].lower()] if q else repos


def forget_repos(name: str) -> None:
    """Drop the cached listing (and minted tokens) for a credential (updated or deleted)."""
    for key in [k for k in _repos_cache if k.startswith(f"{name}@")]:
        _repos_cache.pop(key, None)
    from .github_app import forget
    forget(name)


async def list_tree(token: str, repo: str, ref: str = "", path: str = "",
                    api_url: str | None = None) -> dict:
    """What is at `path` in `repo` at `ref`: `{ref, path, dirs: [...], files: [...], markdown: [...],
    exists: bool}`. One call to the contents API (a directory listing), used by the project wizard
    to show the layout of a context repo before the agent is asked to maintain it."""
    api = (api_url or API_DEFAULT).rstrip("/")
    clean = path.strip("/")
    url = f"{api}/repos/{repo}/contents/{clean}" if clean else f"{api}/repos/{repo}/contents"
    params = {"ref": ref} if ref else {}
    async with httpx.AsyncClient(timeout=20) as cx:
        try:
            r = await cx.get(url, headers=_headers(token), params=params)
        except Exception as e:
            raise ValueError(f"could not reach GitHub: {e}")
    if r.status_code == 404:
        return {"ref": ref, "path": clean, "dirs": [], "files": [], "markdown": [], "exists": False}
    if r.status_code == 401:
        raise ValueError("GitHub rejected the token (401 unauthorized)")
    if r.status_code != 200:
        raise ValueError(f"GitHub returned {r.status_code} for {repo}/{clean or '/'}")
    entries = r.json()
    if isinstance(entries, dict):      # a file, not a directory
        return {"ref": ref, "path": clean, "dirs": [], "files": [entries.get("name", clean)],
                "markdown": [], "exists": True}
    dirs = sorted(e["name"] for e in entries if isinstance(e, dict) and e.get("type") == "dir")
    files = sorted(e["name"] for e in entries if isinstance(e, dict) and e.get("type") == "file")
    md = [f for f in files if f.lower().endswith((".md", ".mdx"))]
    return {"ref": ref, "path": clean, "dirs": dirs, "files": files, "markdown": md, "exists": True}
