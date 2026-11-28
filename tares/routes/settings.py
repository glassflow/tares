"""The cell's settings: GitHub credentials and the Create GitHub App flow, the Anthropic key, the
model gateway and providers, and the Linear connection."""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
from functools import partial
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from .. import linear as _linear
from .. import providers as providers_mod
from ..deps import Deps
from ..providers import (
    DEFAULT_API_BASE,
    resolve_anthropic_headers,
    resolve_api_base,
)
from . import tickets as _tickets


class GithubCredentialIn(BaseModel):
    name: str
    token: str = ""              # secret; blank-to-keep on update
    api_url: str | None = None   # GitHub Enterprise API base; empty = github.com; omitted on
                                 # update = keep
    # token (default) | app (an App entered by hand) | app_broker (Tares Cloud's control plane
    # holds the App's key and hands this cell tokens; see github_app.py)
    kind: str = "token"
    app_id: str = ""
    private_key: str = ""        # secret; blank-to-keep on update
    webhook_secret: str = ""     # secret; blank-to-keep on update
    installation_ids: list[int] = []
    token_url: str = ""          # app_broker: where the cell asks for a token
    broker_secret: str = ""      # app_broker: the cell's own bearer to the broker; blank-to-keep
    installation_id: int | None = None   # app_broker: the one installation
    account: str = ""            # app_broker: the installation's account login
    # app_broker: the repositories (owner/repo) this workspace follows in the installation
    # (TR-372); replaced wholesale when present, kept when omitted on update
    repositories: list[str] | None = None


class GithubAppCreateIn(BaseModel):
    name: str = "github-app"     # the credential's name in Tares
    org: str = ""                # GitHub organization to create the App in; empty = your account
    app_name: str = ""           # the App's name on GitHub (globally unique); empty = suggested
    public_url: str = ""         # this cell's address as GitHub reaches it; empty = this request's


class AnthropicKeyIn(BaseModel):
    key: str    # blank-to-keep is not offered here: the only edits are "set a new one" or DELETE


class GatewayIn(BaseModel):
    url: str
    token: str = ""    # blank keeps the stored token; the URL alone can change


class ProviderIn(BaseModel):
    kind: str                # anthropic | openai | openai_compatible
    name: str = ""           # display name; names an OpenAI-compatible entry (its id is the slug)
    key: str = ""            # blank keeps the stored credential
    base_url: str = ""       # optional for anthropic/openai, required for openai_compatible


class ProviderDefaultIn(BaseModel):
    id: str


def router(deps: Deps) -> APIRouter:
    r = APIRouter()
    store = deps.store
    _err = deps.err
    _linear_err = partial(_tickets.linear_err, deps)

    # ── GitHub credentials: a token or an App stored once, used by the crew and the integrations ──
    # Same write-only contract as the other credentials: the token is never returned, blank-to-keep
    # on update, and the console can list, test and delete.
    from .. import github_app as _gh_app
    from .. import github_webhook as _gh_webhook
    from ..github_credentials import (
        APP_KINDS,
        forget_repos,
        get_token,
        list_repos_for,
        list_tree,
        test_credential,
    )
    from ..github_credentials import redact as _gh_redact

    def _gh_users(name: str) -> dict:
        """Where a credential is used, so deleting one is an informed act. Nothing in the cell
        names a credential since the source catalog went (TR-1811): both lists are empty."""
        return {"sources": [], "mcp_servers": []}

    def _gh_deliveries(cred: dict) -> dict:
        """What the settings row shows about an App's webhook: its key, the name its events go
        under, last delivery, refusals, drops."""
        row = store.integration_key_for("ci_github", cred["name"])
        if not row:
            return {}
        src = row.get("source") or "github"
        st = _gh_webhook.STATS.get(src) or {}
        return {"source": src, "ingest_key": row["key"],
                "deliveries": {"received": st.get("deliveries", 0), "stored": st.get("stored", 0),
                               "last_at": st.get("last_at"), "last_event": st.get("last_event"),
                               "unknown_installation": st.get("unknown_installation", 0),
                               "rejected_signature": _gh_webhook.REJECTED.get(src, 0)}}

    def _gh_row(c: dict) -> dict:
        return {**_gh_redact(c), **_gh_users(c["name"]),
                **(_gh_deliveries(c) if c.get("kind") in APP_KINDS else {})}

    def _ensure_gh_app_key(name: str, ingest_key: str | None = None) -> dict:
        """The webhook key of an App credential (integration_keys, TR-1805), made when missing:
        {key, source}. Its events go under `github` when no other credential's key uses that name,
        else `github_<credential>`. A key given (an App recreated under the same name, whose hook
        URL carries the new key) replaces the old one."""
        row = store.integration_key_for("ci_github", name)
        if row and (not ingest_key or row["key"] == ingest_key):
            return row
        if row:
            src = row.get("source") or "github"
        else:
            taken = store.integration_key_sources("ci_github")
            src = "github" if "github" not in taken else \
                "github_" + re.sub(r"[^A-Za-z0-9_]", "_", name)
        key = ingest_key or f"github_app-{secrets.token_hex(8)}"
        store.put_integration_key(key, "ci_github", name, src)
        return {"key": key, "source": src}

    def _check_gh_name(name: str) -> str:
        name = (name or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            _err(ValueError("name must be alphanumeric/_/-"))
        if store.get_github_credential(name) is not None:
            _err(ValueError(f"GitHub credential {name!r} already exists"), 409)
        return name

    def _gh_repositories(names: list[str]) -> list[str]:
        """An app_broker's picked repositories: trimmed, each owner/repo, in order, no repeats. At
        most 500, GitHub's limit for one token."""
        if len(names) > 500:
            _err(ValueError("repositories: at most 500"))
        out: list[str] = []
        seen: set[str] = set()
        for n in names:
            n = (n or "").strip()
            if len(n) > 200 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+", n):
                _err(ValueError(f"repositories: {n[:200]!r} is not owner/repo"))
            if n.lower() not in seen:
                seen.add(n.lower())
                out.append(n)
        return out

    @r.get("/api/integrations/github")
    def list_github_credentials():
        return {"credentials": [_gh_row(c) for c in store.list_github_credentials()]}

    @r.post("/api/integrations/github", status_code=201)
    async def create_github_credential(body: GithubCredentialIn):
        name = _check_gh_name(body.name)
        api_url = (body.api_url or "").strip()
        if body.kind == "app":
            # an App the person created by hand: the same credential the manifest flow stores
            if not (body.app_id.strip() and body.private_key.strip() and body.webhook_secret.strip()):
                _err(ValueError("app id, private key and webhook secret are required"))
            try:
                _gh_app.app_jwt(body.app_id.strip(), body.private_key.strip())
            except ValueError as e:
                _err(e)
            cfg = {"app_id": body.app_id.strip(), "private_key": body.private_key.strip(),
                   "webhook_secret": body.webhook_secret.strip(), "installations": []}
            cred = {"name": name, "kind": "app", "api_url": api_url, "config": cfg}
            for iid in body.installation_ids:
                try:
                    cfg = _gh_app.with_installation({**cred, "config": cfg},
                                                    await _gh_app.get_installation(cred, iid))
                except ValueError as e:
                    _err(e)
            store.upsert_github_credential(name, "", "app", api_url, "", config=cfg)
            hook = _ensure_gh_app_key(name)
            return {"ok": True, "account": "", "source": hook["source"], "ingest_key": hook["key"]}
        if body.kind == "app_broker":
            # Tares Cloud: the control plane keeps the App's key and brokers tokens (TR-166)
            if not (body.token_url.strip() and body.broker_secret.strip()
                    and body.installation_id and body.webhook_secret.strip()):
                _err(ValueError("token_url, broker_secret, installation_id and webhook_secret "
                                "are required"))
            cfg = {"token_url": body.token_url.strip(), "broker_secret": body.broker_secret.strip(),
                   "installation_id": int(body.installation_id),
                   "webhook_secret": body.webhook_secret.strip(),
                   "repositories": _gh_repositories(body.repositories or [])}
            store.upsert_github_credential(name, "", "app_broker", api_url, body.account.strip(),
                                           config=cfg)
            hook = _ensure_gh_app_key(name)
            return {"ok": True, "account": body.account.strip(), "source": hook["source"],
                    "ingest_key": hook["key"]}
        if body.kind != "token":
            _err(ValueError("kind must be token, app or app_broker"))
        if not body.token.strip():
            _err(ValueError("token is required"))
        account = ""
        try:   # best effort: a bad token is still stored, the Test button explains it
            account = (await test_credential(body.token.strip(), api_url or None))["login"]
        except ValueError:
            pass
        store.upsert_github_credential(name, body.token.strip(), "token", api_url, account)
        return {"ok": True, "account": account}

    @r.put("/api/integrations/github/{name}")
    async def update_github_credential(name: str, body: GithubCredentialIn):
        existing = store.get_github_credential(name)
        if existing is None:
            _err(KeyError(f"unknown GitHub credential {name!r}"), 404)
        api_url = (existing.get("api_url") or "") if body.api_url is None else body.api_url.strip()
        account = existing.get("account") or ""
        kind = existing.get("kind") or "token"
        if kind in APP_KINDS:
            # rotate a secret (blank-to-keep); installations follow GitHub, never this form
            cfg = dict(existing.get("config") or {})
            for field in ("private_key", "webhook_secret", "broker_secret", "token_url"):
                val = getattr(body, field, "").strip()
                if val:
                    cfg[field] = val
            if body.app_id.strip():
                cfg["app_id"] = body.app_id.strip()
            if kind == "app_broker" and body.repositories is not None:
                cfg["repositories"] = _gh_repositories(body.repositories)
            if kind == "app" and body.private_key.strip():
                try:
                    _gh_app.app_jwt(cfg.get("app_id"), cfg["private_key"])
                except ValueError as e:
                    _err(e)
            store.upsert_github_credential(name, "", kind, api_url,
                                           body.account.strip() or account, config=cfg)
            forget_repos(name)
            return {"ok": True, "account": body.account.strip() or account}
        token = body.token.strip() or existing["token"]     # blank-to-keep
        if body.token.strip():
            try:
                account = (await test_credential(token, api_url or None))["login"]
            except ValueError:
                pass
        store.upsert_github_credential(name, token, kind, api_url, account)
        forget_repos(name)
        return {"ok": True, "account": account}

    @r.delete("/api/integrations/github/{name}")
    def delete_github_credential(name: str):
        if store.get_github_credential(name) is None:
            _err(KeyError(f"unknown GitHub credential {name!r}"), 404)
        store.delete_github_credential(name)
        store.delete_integration_keys("ci_github", name)   # its webhook key stops working (TR-1807)
        forget_repos(name)
        return {"ok": True, **_gh_users(name)}

    @r.post("/api/integrations/github/{name}/test")
    async def test_github_credential(name: str):
        cred = store.get_github_credential(name)
        if cred is None:
            _err(KeyError(f"unknown GitHub credential {name!r}"), 404)
        if cred.get("kind") in APP_KINDS:
            return await _test_gh_app(cred)
        try:
            info = await test_credential(cred["token"], cred.get("api_url") or None)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if info["login"] and info["login"] != cred.get("account"):
            store.upsert_github_credential(name, cred["token"], cred.get("kind") or "token",
                                           cred.get("api_url") or "", info["login"])
        return {"ok": True, **info}

    async def _test_gh_app(cred: dict) -> dict:
        """An App: resync its installations from GitHub (a self-held App can list them), then mint
        a token per installation and count its repos. The proof the whole chain works."""
        try:
            if cred["kind"] == "app":
                # keep what is known or on the owner's account; a stranger's install of a public
                # App is not ours (see github_app_installed)
                insts = [i for i in await _gh_app.list_installations(cred)
                         if _gh_app.may_add_installation(cred, i["id"], i["account"])]
                cfg = {**(cred.get("config") or {}), "installations": insts}
                store.update_github_credential_config(cred["name"], cfg)
                cred = {**cred, "config": cfg}
            rows = []
            for inst in _gh_app.installations(cred):
                repos = await _gh_app.installation_repos(cred, inst["id"], use_cache=False)
                rows.append({**inst, "repos": len(repos)})
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if not rows:
            return {"ok": False, "error": "the App is not installed anywhere yet; install it on "
                                          "GitHub", "installations": []}
        return {"ok": True, "login": rows[0]["account"], "installations": rows}

    @r.get("/api/integrations/github/{name}/repos")
    async def github_credential_repos(name: str, query: str = ""):
        cred = store.get_github_credential(name)
        if cred is None:
            _err(KeyError(f"unknown GitHub credential {name!r}"), 404)
        try:
            repos = await asyncio.wait_for(list_repos_for(cred, query), timeout=60)
        except ValueError as e:
            _err(e, 502)
        except (TimeoutError, asyncio.TimeoutError):
            _err(ValueError("GitHub took too long to list repositories"), 504)
        return {"repos": repos}

    @r.get("/api/integrations/github/{name}/tree")
    async def github_credential_tree(name: str, repo: str, ref: str = "", path: str = ""):
        cred = store.get_github_credential(name)
        if cred is None:
            _err(KeyError(f"unknown GitHub credential {name!r}"), 404)
        if not repo or "/" not in repo:
            _err(ValueError("repo must be owner/name"))
        try:
            token = await get_token(store, name, repo)
            return await asyncio.wait_for(
                list_tree(token, repo, ref, path, cred.get("api_url") or None), timeout=30)
        except ValueError as e:
            _err(e, 502)
        except (TimeoutError, asyncio.TimeoutError):
            _err(ValueError("GitHub took too long to list the repository"), 504)

    # ── Create GitHub App: GitHub's manifest flow, then the install ──
    # 1. POST .../apps (admin) returns the manifest and where to POST it; the console submits it as
    #    a form, so the person lands on GitHub's "create App" page with everything filled in.
    # 2. GitHub sends the browser to .../apps/callback?code=...&state=...; the code is exchanged for
    #    the App's id, key and webhook secret, the credential and its webhook key are stored.
    # 3. The person installs it; GitHub sends the browser to .../apps/installed?installation_id=...
    # The two callbacks are public to the auth middleware (a redirect has no Tares key) and gated
    # by the signed state, or for the install by proving the installation belongs to our App.

    def _gh_settings(params: dict) -> RedirectResponse:
        from urllib.parse import urlencode
        return RedirectResponse(f"/settings?{urlencode({'tab': 'github', **params})}",
                                status_code=303)

    @r.post("/api/integrations/github/apps")
    def create_github_app(body: GithubAppCreateIn, request: Request):
        name = _check_gh_name(body.name)
        base = (body.public_url.strip() or os.getenv("TARES_PUBLIC_URL", "").strip()
                or str(request.base_url)).rstrip("/")
        ingest_key = f"github_app-{secrets.token_hex(8)}"
        hook_url = f"{base}/ingest/{ingest_key}"
        host = urlsplit(base).hostname or ""
        app_name = body.app_name.strip() or f"Tares {host.split('.')[0] or name}"
        state = _gh_app.sign_state(store, {"op": "create", "cred": name, "key": ingest_key})
        warning = None
        if host in ("localhost", "127.0.0.1", "0.0.0.0") or host.endswith(".local"):
            warning = (f"GitHub cannot reach {base}. The App can still be created, but its "
                       "webhooks will fail until Tares is reachable (a public address or a tunnel); "
                       "enter that address as the public URL.")
        return {"action": f"{_gh_app.manifest_action(body.org)}?state={state}",
                "manifest": json.dumps(_gh_app.manifest(app_name, base, hook_url)),
                "hook_url": hook_url, "warning": warning}

    @r.get("/api/integrations/github/apps/callback")
    async def github_app_created(code: str = "", state: str = ""):
        try:
            data = _gh_app.verify_state(store, state)
            if data.get("op") != "create" or not code:
                raise ValueError("this link is not a GitHub App creation callback")
            name = data["cred"]
            if store.get_github_credential(name) is not None:
                raise ValueError(f"GitHub credential {name!r} already exists")
            conv = await _gh_app.convert_manifest(code)
        except ValueError as e:
            return _gh_settings({"error": str(e)})
        cfg = {k: conv[k] for k in ("app_id", "slug", "client_id", "client_secret", "private_key",
                                    "webhook_secret", "html_url", "owner", "app_name")}
        cfg["installations"] = []
        store.upsert_github_credential(name, "", "app", "", conv["owner"], config=cfg)
        _ensure_gh_app_key(name, data["key"])
        return _gh_settings({"github": name, "event": "created"})

    @r.get("/api/integrations/github/{name}/install")
    def github_app_install_link(name: str):
        """The GitHub page that installs this App (with a signed state naming the credential)."""
        cred = store.get_github_credential(name)
        if cred is None or cred.get("kind") != "app" or not (cred.get("config") or {}).get("slug"):
            _err(KeyError(f"{name!r} is not a GitHub App created here"), 404)
        state = _gh_app.sign_state(store, {"op": "install", "cred": name})
        return {"url": _gh_app.install_url(cred["config"]["slug"], state)}

    @r.get("/api/integrations/github/apps/installed")
    async def github_app_installed(installation_id: int = 0, setup_action: str = "",
                                   state: str = ""):
        if setup_action == "request" or not installation_id:
            return _gh_settings({"event": "requested", "detail": "an organization owner has to "
                                 "approve the install on GitHub"})
        named = None
        if state:
            try:
                data = _gh_app.verify_state(store, state)
                named = data.get("cred") if data.get("op") == "install" else None
            except ValueError:
                named = None
        # Two gates. The installation must belong to one of our Apps (GET /app/installations/{id}
        # signed as each App succeeds only for its own). And someone allowed here must have asked
        # for it: our signed state (the install link is admin-only), or, for an install started on
        # GitHub's own page, the App owner's account or one already known. Without the second, a
        # stranger installing a public App would join this cell.
        apps = [c for c in store.list_github_credentials() if c.get("kind") == "app"
                and (named is None or c["name"] == named)]
        for cred in apps:
            try:
                inst = await _gh_app.get_installation(cred, installation_id)
            except ValueError:
                continue
            if named is None and not _gh_app.may_add_installation(cred, installation_id,
                                                                  inst.get("account", "")):
                return _gh_settings({"error": f"installation on {inst.get('account')!r} was not "
                                              f"added: start it from Settings > GitHub "
                                              f"(Install / Add an organization)"})
            store.update_github_credential_config(cred["name"],
                                                  _gh_app.with_installation(cred, inst))
            if inst.get("account") and not cred.get("account"):
                store.upsert_github_credential(cred["name"], "", "app", cred.get("api_url") or "",
                                               inst["account"])
            _gh_app.forget_repos(cred["name"], installation_id)
            return _gh_settings({"github": cred["name"], "event": "installed",
                                 "account": inst.get("account", "")})
        return _gh_settings({"error": f"installation {installation_id} does not belong to a "
                                      "GitHub App created on this Tares"})

    # ── the Anthropic key: a stored key wins, env is the fallback ────────────
    @r.get("/api/settings/anthropic-key")
    def get_anthropic_key():
        """Never returns the key — only whether one is resolvable and where it came from. A key
        saved here takes over from the deployment's env key; `env_overrides` is kept for API
        compatibility and can now only be true when no key is stored."""
        headers, origin = resolve_anthropic_headers(store)
        return {"configured": bool(headers), "source": origin,
                "stored": bool(store.get_setting("anthropic_key")),
                "env_overrides": origin.startswith("env:")}

    @r.put("/api/settings/anthropic-key")
    def set_anthropic_key(body: AnthropicKeyIn):
        key = body.key.strip()
        if not key:
            _err(ValueError("key is required (use DELETE to remove the stored key)"))
        store.set_setting("anthropic_key", key)
        _, origin = resolve_anthropic_headers(store)
        return {"ok": True, "source": origin}

    @r.delete("/api/settings/anthropic-key")
    def clear_anthropic_key():
        """Removing the stored key falls back to the deployment's env key when one exists — on a
        hosted trial cell that is the trial key, until its operator removes it too."""
        store.set_setting("anthropic_key", None)
        key, origin = resolve_anthropic_headers(store)
        return {"ok": True, "configured": bool(key), "source": origin}

    # ── model access through a gateway (TR-281): stored on the cell like the key ──────────
    # Where model calls go, and the credential for it. Saved here they win over the environment,
    # so a cloud customer with a mandated proxy configures it on their own cell and the platform
    # key stops being used, the same as when they store their own key. The token is write-only.
    def _gateway_status() -> dict:
        url, origin = resolve_api_base(store)
        return {"configured": origin != "", "url": url if origin else "", "source": origin,
                "stored": bool(store.get_setting("gateway_url")),
                "token_stored": bool(store.get_setting("gateway_token")),
                "default_url": DEFAULT_API_BASE}

    @r.get("/api/settings/gateway")
    def get_gateway():
        return _gateway_status()

    @r.put("/api/settings/gateway")
    def set_gateway(body: GatewayIn):
        url = body.url.strip().rstrip("/")
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            _err(ValueError("gateway url must be an http(s) URL with a host, e.g. "
                            "https://llm-gateway.internal"))
        store.set_setting("gateway_url", url)
        if body.token.strip():
            store.set_setting("gateway_token", body.token.strip())
        return {"ok": True, **_gateway_status()}

    @r.delete("/api/settings/gateway")
    def clear_gateway():
        """Back to the environment's gateway if the deployment set one, else Anthropic."""
        store.set_setting("gateway_url", None)
        store.set_setting("gateway_token", None)
        return {"ok": True, **_gateway_status()}

    # ── model providers (TR-301): the list a cell holds, one of them the default ──────────
    # The Anthropic entry is the key + gateway settings above under another door; OpenAI and
    # OpenAI-compatible entries (LiteLLM, OpenRouter, Ollama, vLLM) live in one JSON setting.
    # Credentials are write-only: the listing says configured-or-not and where from.
    @r.get("/api/settings/providers")
    def get_providers():
        return providers_mod.list_providers(store)

    @r.put("/api/settings/providers/default")
    def set_default_provider(body: ProviderDefaultIn):
        try:
            providers_mod.set_default(store, body.id.strip())
        except KeyError as e:
            _err(e, 404)
        except ValueError as e:
            _err(e)
        return {"ok": True, **providers_mod.list_providers(store)}

    @r.put("/api/settings/providers/{provider_id}")
    async def save_provider(provider_id: str, body: ProviderIn):
        """`new` as the id creates an entry (its id is the kind, or the slug of the name for an
        OpenAI-compatible endpoint)."""
        try:
            pid = providers_mod.save_provider(store, "" if provider_id == "new" else provider_id,
                                             body.kind.strip(), body.name, body.key, body.base_url)
        except ValueError as e:
            _err(e)
        # The endpoint lists its own models: ask it now, so the picker is filled the moment the
        # entry is saved. A failure is recorded on the entry, not raised: the entry is saved
        # either way, the built-in list stands in, and the console shows why.
        try:
            discovery = await providers_mod.refresh_models(store, pid)
        except ValueError as e:   # no credential to ask with yet
            discovery = {"models": [], "error": str(e)}
        return {"ok": True, "id": pid, "discovery": discovery, **providers_mod.list_providers(store)}

    @r.post("/api/settings/providers/{provider_id}/models")
    async def refresh_provider_models(provider_id: str):
        """Re-read what the endpoint serves (the admin added a model, the key's allowance
        changed, a newer Claude model). The built-in list stands in while it cannot be read."""
        try:
            result = await providers_mod.refresh_models(store, provider_id)
        except KeyError as e:
            _err(e, 404)
        except ValueError as e:
            _err(e)
        return {"ok": True, **result, **providers_mod.list_providers(store)}

    @r.delete("/api/settings/providers/{provider_id}")
    def delete_provider(provider_id: str):
        known = {p["id"] for p in providers_mod.list_providers(store)["providers"]}
        if provider_id not in known:
            _err(KeyError(f"unknown provider {provider_id!r}"), 404)
        try:
            providers_mod.delete_provider(store, provider_id)
        except ValueError as e:
            _err(e)
        return {"ok": True, **providers_mod.list_providers(store)}

    # ── Linear (TR-408): the cell's connection, and the Linear project a project's tickets live
    # in. Tares reads from Linear; write_ticket and write_milestone create there through Tares ──
    @r.get("/api/linear")
    def linear_status():
        """Whether Linear is connected (never the token), and as whom."""
        return _linear.public_connection(store)

    @r.post("/api/linear")
    async def linear_connect_key(body: dict = Body(...)):
        """{api_key}: connect with a personal API key, checked against Linear first."""
        try:
            await _linear.connect_api_key(store, str(body.get("api_key") or ""))
        except _linear.LinearError as e:
            _linear_err(e)
        return _linear.public_connection(store)

    @r.put("/api/settings/linear-connection")
    def linear_cloud_set(body: dict = Body(...)):
        """Tares Cloud only, from the control plane (TR-537): {token, expires_at, account} after a
        sign-in with the GlassFlow Linear app or a renewal, or {error, account} when Linear refused
        to renew it. No restart; the next sync uses it."""
        try:
            _linear.set_cloud_connection(store, body)
        except _linear.LinearError as e:
            _err(e)
        return _linear.public_connection(store)

    @r.delete("/api/settings/linear-connection")
    def linear_cloud_clear():
        """Tares Cloud only: the control plane disconnected this workspace (and revoked the token
        at Linear)."""
        _linear.disconnect(store)
        return _linear.public_connection(store)

    @r.delete("/api/linear")
    def linear_disconnect():
        """Forget the connection. Linked projects keep their tickets and stop syncing until
        Linear is connected again. On Tares Cloud the console disconnects through the control
        plane instead, which also revokes the token at Linear."""
        _linear.disconnect(store)
        return _linear.public_connection(store)

    @r.get("/api/linear/projects")
    async def linear_projects(q: str = ""):
        """Linear projects this connection can see, most recently updated first, for a picker."""
        try:
            return {"projects": await _linear.list_projects(store, q)}
        except _linear.LinearError as e:
            _linear_err(e)

    return r
