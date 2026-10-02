"""GitHub App auth: the cell acting as a GitHub App installation instead of with a personal token.

Two credential kinds use this module (both live in the `github_credentials` table, see
github_credentials.py; callers only ever ask that module for a token):

* `app`: the cell holds the App itself (app id, private key, webhook secret) and mints its own
  installation tokens. A self-hoster gets one with the "Create GitHub App" button (GitHub's App
  manifest flow, `manifest()` + `convert_manifest()`), or enters an App they made by hand.
* `app_broker`: the cell holds no key. A broker (on Tares Cloud, the control plane, which keeps the
  GlassFlow-owned App's key) mints a token for this cell's installation on request. The cell posts
  to `token_url` with its own bearer secret and caches what comes back exactly like a minted one.

An installation token lives one hour. Tokens are cached per (credential, installation) and refreshed
when under five minutes remain, one refresh in flight at a time.

The browser redirects GitHub sends back (the manifest callback, the install callback) carry no
Tares bearer token, so they are authenticated by a signed `state` instead (`sign_state`).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import time

import httpx

API_DEFAULT = "https://api.github.com"
WEB_DEFAULT = "https://github.com"
REFRESH_MARGIN = 300        # seconds: refresh a token with less than this left
REPOS_TTL = 300             # seconds an installation's repo list is reused
STATE_TTL = 3600            # seconds a signed state is accepted (GitHub's manifest code lives 1h)
STATE_SECRET_SETTING = "github_state_secret"

# The permissions and events a Tares App asks for. Write permissions are there so agents can act;
# the person creating the App sees them on GitHub's page and can untick any.
APP_PERMISSIONS = {
    "metadata": "read", "contents": "write", "pull_requests": "write", "issues": "write",
    "checks": "write", "actions": "read", "statuses": "read",
}
APP_EVENTS = [
    "pull_request", "pull_request_review", "pull_request_review_comment", "push", "issues",
    "issue_comment", "release", "workflow_run",
]

_tokens: dict[tuple[str, int], tuple[str, float]] = {}     # (cred, installation) -> (token, exp)
_locks: dict[tuple[str, int], asyncio.Lock] = {}
_repos: dict[tuple[str, int], tuple[float, list[dict]]] = {}


def _api(cred: dict) -> str:
    return (cred.get("api_url") or API_DEFAULT).rstrip("/")


def _headers(token: str | None = None, bearer: str | None = None) -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token or bearer:
        h["Authorization"] = f"Bearer {token or bearer}"
    return h


def forget(name: str) -> None:
    """Drop cached tokens and repo lists of a credential (rotated, deleted, uninstalled)."""
    for cache in (_tokens, _repos):
        for key in [k for k in cache if k[0] == name]:
            cache.pop(key, None)


# ── App JWT and installation tokens ─────────────────────────────────────────────

def app_jwt(app_id: str | int, private_key: str) -> str:
    """The short-lived JWT that authenticates as the App itself (RS256, 10 minutes, issued 60s
    in the past to absorb clock skew). Raises ValueError naming a bad key."""
    import jwt   # PyJWT; with `cryptography` for RS256
    now = int(time.time())
    try:
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(app_id)},
                          private_key, algorithm="RS256")
    except Exception as e:
        raise ValueError(f"the App's private key is not a valid PEM RSA key ({type(e).__name__})")


def _app_cfg(cred: dict) -> dict:
    cfg = cred.get("config") or {}
    if not cfg.get("app_id") or not cfg.get("private_key"):
        raise ValueError(f"GitHub App credential {cred.get('name')!r} has no app id or private key")
    return cfg


def installations(cred: dict) -> list[dict]:
    """The installations a credential knows: `[{id, account, account_type, repository_selection}]`.
    A broker credential has exactly one."""
    cfg = cred.get("config") or {}
    if cred.get("kind") == "app_broker":
        iid = cfg.get("installation_id")
        return [{"id": int(iid), "account": cred.get("account") or cfg.get("account") or ""}] \
            if iid else []
    return [i for i in cfg.get("installations") or [] if isinstance(i, dict) and i.get("id")]


def _explain(r: httpx.Response, what: str) -> str:
    try:
        msg = (r.json() or {}).get("message") or ""
    except ValueError:
        msg = ""
    if r.status_code == 401:
        return f"GitHub rejected the App's credentials for {what} (401): wrong app id or key"
    if r.status_code == 404:
        return f"{what}: not found (404); the App is not installed there, or was uninstalled"
    if r.status_code == 403:
        return f"{what}: forbidden (403){': ' + msg if msg else ''}; the App lacks a permission"
    return f"GitHub returned {r.status_code} for {what}{': ' + msg if msg else ''}"


async def _mint(cred: dict, installation_id: int) -> tuple[str, float]:
    """A fresh installation token and its expiry (epoch seconds)."""
    if cred.get("kind") == "app_broker":
        cfg = cred.get("config") or {}
        url, secret = cfg.get("token_url"), cfg.get("broker_secret")
        if not url or not secret:
            raise ValueError(f"GitHub credential {cred.get('name')!r} has no broker URL or secret")
        async with httpx.AsyncClient(timeout=20) as cx:
            try:
                r = await cx.post(url, headers={"Authorization": f"Bearer {secret}"},
                                  json={"installation_id": installation_id})
            except Exception as e:
                raise ValueError(f"could not reach the GitHub token broker: {e}")
        if r.status_code != 200:
            raise ValueError(f"the GitHub token broker returned {r.status_code}")
        body = r.json() or {}
    else:
        cfg = _app_cfg(cred)
        token = app_jwt(cfg["app_id"], cfg["private_key"])
        async with httpx.AsyncClient(timeout=20) as cx:
            try:
                r = await cx.post(f"{_api(cred)}/app/installations/{installation_id}/access_tokens",
                                  headers=_headers(bearer=token))
            except Exception as e:
                raise ValueError(f"could not reach GitHub: {e}")
        if r.status_code != 201:
            raise ValueError(_explain(r, f"installation {installation_id}"))
        body = r.json() or {}
    tok = body.get("token")
    if not tok:
        raise ValueError("GitHub returned no installation token")
    return tok, _parse_exp(body.get("expires_at"))


def _parse_exp(s) -> float:
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return time.time() + 3000      # GitHub says one hour; assume a little less


async def installation_token(cred: dict, installation_id: int,
                             min_life: int = REFRESH_MARGIN) -> str:
    """An installation token for `installation_id` with at least `min_life` seconds left (cached
    until then; an MCP connection that keeps one token for a whole run asks for more). Raises
    ValueError naming the cause (bad key, not installed, broker down)."""
    key = (cred["name"], int(installation_id))
    hit = _tokens.get(key)
    if hit and hit[1] - time.time() > min_life:
        return hit[0]
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _tokens.get(key)      # a refresh that finished while this one waited
        if hit and hit[1] - time.time() > min_life:
            return hit[0]
        tok, exp = await _mint(cred, int(installation_id))
        if hit:
            print(f"[github {cred['name']}] installation {installation_id}: token refreshed")
        _tokens[key] = (tok, exp)
        return tok


async def installation_repos(cred: dict, installation_id: int, use_cache: bool = True) -> list[dict]:
    """Repos an installation covers: `[{full_name, default_branch, private, pushed_at}]`, cached
    five minutes (an `installation_repositories` webhook drops the cache)."""
    key = (cred["name"], int(installation_id))
    hit = _repos.get(key) if use_cache else None
    if hit and time.monotonic() - hit[0] < REPOS_TTL:
        return hit[1]
    token = await installation_token(cred, installation_id)
    repos: list[dict] = []
    async with httpx.AsyncClient(timeout=20) as cx:
        for page in range(1, 31):
            try:
                r = await cx.get(f"{_api(cred)}/installation/repositories", headers=_headers(token),
                                 params={"per_page": 100, "page": page})
            except Exception as e:
                raise ValueError(f"could not reach GitHub: {e}")
            if r.status_code != 200:
                raise ValueError(_explain(r, "the installation's repositories"))
            batch = (r.json() or {}).get("repositories") or []
            for repo in batch:
                if isinstance(repo, dict) and repo.get("full_name"):
                    repos.append({"full_name": repo["full_name"],
                                  "default_branch": repo.get("default_branch") or "main",
                                  "private": bool(repo.get("private")),
                                  "pushed_at": repo.get("pushed_at")})
            if len(batch) < 100:
                break
    _repos[key] = (time.monotonic(), repos)
    return repos


def forget_repos(name: str, installation_id: int | None = None) -> None:
    for key in [k for k in _repos if k[0] == name and (installation_id is None
                                                       or k[1] == int(installation_id))]:
        _repos.pop(key, None)


async def token_for(cred: dict, repo: str | None = None, min_life: int = REFRESH_MARGIN) -> str:
    """The token to use for `repo` (owner/name): the installation that covers it, or the only
    installation when there is one. Raises ValueError when there is none or the choice is unclear."""
    insts = installations(cred)
    if not insts:
        raise ValueError(f"GitHub App credential {cred['name']!r} is not installed anywhere yet; "
                         "install it on GitHub (Settings > GitHub)")
    if len(insts) == 1:
        return await installation_token(cred, insts[0]["id"], min_life)
    if repo:
        owner = repo.split("/", 1)[0].lower()
        by_owner = [i for i in insts if str(i.get("account") or "").lower() == owner]
        for inst in by_owner + [i for i in insts if i not in by_owner]:
            try:
                names = {r["full_name"].lower() for r in await installation_repos(cred, inst["id"])}
            except ValueError:
                continue
            if repo.lower() in names:
                return await installation_token(cred, inst["id"], min_life)
        raise ValueError(f"no installation of GitHub App credential {cred['name']!r} covers {repo}")
    raise ValueError(f"GitHub App credential {cred['name']!r} is installed on several accounts "
                     f"({', '.join(str(i.get('account') or i['id']) for i in insts)}); "
                     "say which repository")


async def get_installation(cred: dict, installation_id: int) -> dict:
    """GET /app/installations/{id} as the App: proves the installation belongs to this App and
    returns `{id, account, account_type, repository_selection}`."""
    cfg = _app_cfg(cred)
    async with httpx.AsyncClient(timeout=20) as cx:
        try:
            r = await cx.get(f"{_api(cred)}/app/installations/{int(installation_id)}",
                             headers=_headers(bearer=app_jwt(cfg["app_id"], cfg["private_key"])))
        except Exception as e:
            raise ValueError(f"could not reach GitHub: {e}")
    if r.status_code != 200:
        raise ValueError(_explain(r, f"installation {installation_id}"))
    return _inst_row(r.json() or {})


async def list_installations(cred: dict) -> list[dict]:
    """Every installation of the App (GET /app/installations), to resync a credential."""
    cfg = _app_cfg(cred)
    out: list[dict] = []
    async with httpx.AsyncClient(timeout=20) as cx:
        for page in range(1, 11):
            try:
                r = await cx.get(f"{_api(cred)}/app/installations",
                                 headers=_headers(bearer=app_jwt(cfg["app_id"], cfg["private_key"])),
                                 params={"per_page": 100, "page": page})
            except Exception as e:
                raise ValueError(f"could not reach GitHub: {e}")
            if r.status_code != 200:
                raise ValueError(_explain(r, "the App's installations"))
            batch = r.json() or []
            out += [_inst_row(i) for i in batch if isinstance(i, dict)]
            if len(batch) < 100:
                break
    return out


def _inst_row(i: dict) -> dict:
    acct = i.get("account") or {}
    return {"id": int(i.get("id") or 0), "account": acct.get("login") or "",
            "account_type": acct.get("type") or "", "repository_selection":
            i.get("repository_selection") or ""}


def may_add_installation(cred: dict, installation_id: int, account: str) -> bool:
    """Whether an installation reported by a webhook or a resync may join the credential without
    a person having started it here: it is already known, or it is on the App owner's account.
    Anything else (a stranger installing a public App) needs our install link (signed state)."""
    if any(i["id"] == int(installation_id) for i in installations(cred)):
        return True
    owner = str((cred.get("config") or {}).get("owner") or "").lower()
    return bool(owner) and owner == str(account or "").lower()


def with_installation(cred: dict, inst: dict) -> dict:
    """The credential's config with `inst` added (or refreshed)."""
    cfg = dict(cred.get("config") or {})
    rows = [i for i in cfg.get("installations") or [] if i.get("id") != inst["id"]]
    cfg["installations"] = rows + [inst]
    return cfg


def without_installation(cred: dict, installation_id: int) -> dict:
    cfg = dict(cred.get("config") or {})
    cfg["installations"] = [i for i in cfg.get("installations") or []
                            if i.get("id") != int(installation_id)]
    return cfg


# ── the manifest flow ───────────────────────────────────────────────────────────

def manifest(name: str, base_url: str, hook_url: str, *, public: bool = False) -> dict:
    """The App manifest GitHub's "create from manifest" page takes. `base_url` is the cell's public
    address (where GitHub sends the browser back), `hook_url` the ingest URL webhooks go to."""
    base = base_url.rstrip("/")
    return {
        "name": name[:34],          # GitHub's limit on an App name
        "url": base,
        "hook_attributes": {"url": hook_url, "active": True},
        "redirect_url": f"{base}/api/integrations/github/apps/callback",
        "setup_url": f"{base}/api/integrations/github/apps/installed",
        "setup_on_update": True,
        "public": public,
        "default_permissions": dict(APP_PERMISSIONS),
        "default_events": list(APP_EVENTS),
    }


def manifest_action(org: str = "", web_url: str | None = None) -> str:
    """Where the manifest form is POSTed: the personal account's or an organization's new-App page."""
    web = (web_url or WEB_DEFAULT).rstrip("/")
    org = (org or "").strip()
    return f"{web}/organizations/{org}/settings/apps/new" if org else f"{web}/settings/apps/new"


async def convert_manifest(code: str, api_url: str | None = None) -> dict:
    """Exchange the one-hour code GitHub returns for the App's id, slug, private key, webhook
    secret and client id (POST /app-manifests/{code}/conversions, no auth)."""
    api = (api_url or API_DEFAULT).rstrip("/")
    async with httpx.AsyncClient(timeout=20) as cx:
        try:
            r = await cx.post(f"{api}/app-manifests/{code}/conversions", headers=_headers())
        except Exception as e:
            raise ValueError(f"could not reach GitHub: {e}")
    if r.status_code != 201:
        raise ValueError(_explain(r, "the App manifest conversion")
                         + " (the code is valid one hour and only once)")
    b = r.json() or {}
    owner = b.get("owner") or {}
    return {"app_id": str(b.get("id") or ""), "slug": b.get("slug") or "",
            "client_id": b.get("client_id") or "", "client_secret": b.get("client_secret") or "",
            "private_key": b.get("pem") or "", "webhook_secret": b.get("webhook_secret") or "",
            "html_url": b.get("html_url") or "", "owner": owner.get("login") or "",
            "app_name": b.get("name") or ""}


def install_url(slug: str, state: str, web_url: str | None = None) -> str:
    web = (web_url or WEB_DEFAULT).rstrip("/")
    return f"{web}/apps/{slug}/installations/new?state={state}"


# ── signed state for the browser round trips ────────────────────────────────────

def _state_secret(store) -> bytes:
    s = store.get_setting(STATE_SECRET_SETTING)
    if not s:
        s = secrets.token_urlsafe(32)
        store.set_setting(STATE_SECRET_SETTING, s)
    return s.encode()


def sign_state(store, data: dict) -> str:
    body = dict(data, exp=int(time.time()) + STATE_TTL, n=secrets.token_hex(4))
    raw = base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode()).rstrip(b"=")
    sig = hmac.new(_state_secret(store), raw, hashlib.sha256).hexdigest()[:32]
    return f"{raw.decode()}.{sig}"


def verify_state(store, state: str) -> dict:
    """The data a state was signed with; ValueError when forged, tampered with or expired."""
    try:
        raw, sig = str(state or "").rsplit(".", 1)
    except ValueError:
        raise ValueError("missing or malformed state")
    want = hmac.new(_state_secret(store), raw.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(want.encode(), sig.encode("utf-8", "replace")):
        raise ValueError("state signature does not match")
    try:
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except (ValueError, TypeError):
        raise ValueError("malformed state")
    if int(data.get("exp") or 0) < time.time():
        raise ValueError("state expired; start again from Settings > GitHub")
    return data
