"""Model providers, as a cell configures them (TR-301).

A cell holds a list of providers. Each is a kind (Anthropic, OpenAI, or an OpenAI-compatible
endpoint such as LiteLLM, OpenRouter, Ollama or vLLM), a credential, a base URL and a display
name. One of them is the cell default: what Ask and the builder use, and what an agent uses when
it names no provider of its own.

Where each comes from:
- `anthropic` is the cell's original key and gateway settings (`anthropic_key`, `gateway_url`,
  `gateway_token`) and their environment variables. Those settings and their endpoints stay as
  they are, so a deployment that fills them (a hosted trial cell, a compose file) keeps working.
  Editing the Anthropic entry through the provider API writes those same settings.
- `openai` and every OpenAI-compatible entry live in one JSON setting (`model_providers`). An
  `openai` entry is seeded from `OPENAI_API_KEY` / `OPENAI_BASE_URL` when none is stored.
- The default is the `model_provider_default` setting, else `TARES_MODEL_PROVIDER`, else the
  Anthropic entry when configured, else the first configured entry.

Credentials are never returned: the listing says whether an entry is configured and where its
credential came from. Blank key on an update keeps the stored one.
"""
from __future__ import annotations

import json
import os
import re
from urllib.parse import urlsplit

from .config import API_BASE
from .models import AnthropicProvider, OpenAIProvider, Provider

KINDS = ("anthropic", "openai", "openai_compatible")
KIND_LABELS = {"anthropic": "Anthropic", "openai": "OpenAI",
               "openai_compatible": "OpenAI-compatible endpoint"}
DEFAULT_API_BASE = "https://api.anthropic.com"
OPENAI_API_BASE = "https://api.openai.com/v1"
SETTING = "model_providers"
DEFAULT_SETTING = "model_provider_default"
TIMEOUT = 120   # per model request
# The model choices the console offers per provider kind; a router lists its own (TR-303).
OPENAI_MODELS = ["gpt-5", "gpt-5-mini", "gpt-4.1", "gpt-4.1-mini", "gpt-4o"]


# ── the Anthropic entry: the original key + gateway settings ─────────────────
def resolve_api_base(store) -> tuple[str, str]:
    """(base URL, where-it-came-from). Where Anthropic-format calls go: a gateway saved in the
    console (TR-281), else the environment (`ANTHROPIC_BASE_URL`, or the old
    `TARES_ANTHROPIC_BASE`), else Anthropic itself. Read per request, like the key."""
    stored = (store.get_setting("gateway_url") or "").strip().rstrip("/")
    if stored:
        return stored, "console"
    if API_BASE != DEFAULT_API_BASE:
        which = "ANTHROPIC_BASE_URL" if os.getenv("ANTHROPIC_BASE_URL", "").strip() else "TARES_ANTHROPIC_BASE"
        return API_BASE, f"env:{which}"
    return DEFAULT_API_BASE, ""


def resolve_anthropic_headers(store) -> tuple[dict[str, str], str]:
    """(headers, where-it-came-from). The console-stored values win over the environment: the
    user's own credential takes over from whatever the deployment shipped the moment they save
    one. That order is load-bearing for hosted trials: an operator-provided key in the env must
    yield to the customer's key instantly, so their spend lands on their key, not the trial's.
    Deleting the stored value falls back to the env (if the deployment still carries one).

    A gateway token saved in the console (Settings, TR-281) goes first, as a bearer header: it
    exists only because the user set a gateway up on purpose. Then the stored key, as the
    Anthropic key header, which is what a gateway expects from a plain key too."""
    gateway_token = (store.get_setting("gateway_token") or "").strip()
    stored = (store.get_setting("anthropic_key") or "").strip()
    auth_token = os.getenv("ANTHROPIC_AUTH_TOKEN", "").strip()
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    headers = {"anthropic-version": "2023-06-01"}
    if gateway_token:
        headers["Authorization"] = f"Bearer {gateway_token}"
        key_origin = "console:gateway"
    elif stored:
        headers["x-api-key"] = stored
        key_origin = "console"
    elif auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
        key_origin = "env:ANTHROPIC_AUTH_TOKEN"
    elif api_key:
        headers["x-api-key"] = api_key
        key_origin = "env:ANTHROPIC_API_KEY"
    else:
        return {}, ""
    return headers, key_origin


# ── the stored list ──────────────────────────────────────────────────────────
def _stored(store) -> list[dict]:
    raw = store.get_setting(SETTING)
    try:
        entries = json.loads(raw) if raw else []
    except ValueError:
        entries = []
    return [e for e in entries if isinstance(e, dict) and e.get("id")]


def _save(store, entries: list[dict]) -> None:
    store.set_setting(SETTING, json.dumps(entries) if entries else None)


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s[:40]


def _openai_env() -> dict | None:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        return None
    return {"id": "openai", "kind": "openai", "name": "OpenAI", "key": key,
            "base_url": os.getenv("OPENAI_BASE_URL", "").strip().rstrip("/") or OPENAI_API_BASE,
            "source": "env:OPENAI_API_KEY"}


PLATFORM_ID = "platform"


def _platform_env() -> dict | None:
    """The provider the platform hands a hosted cell: an OpenAI-compatible endpoint (Tares Cloud's
    LiteLLM) reached with a per-cell key that carries the trial budget. Seeded from the
    environment, like the OpenAI entry; named by the platform so the console says whose it is."""
    url = os.getenv("TARES_PLATFORM_PROVIDER_URL", "").strip().rstrip("/")
    if not url:
        return None
    return {"id": PLATFORM_ID, "kind": "openai_compatible",
            "name": os.getenv("TARES_PLATFORM_PROVIDER_NAME", "").strip() or "Platform",
            "key": os.getenv("TARES_PLATFORM_PROVIDER_KEY", "").strip(), "base_url": url,
            "source": "env:TARES_PLATFORM_PROVIDER_URL"}


def _entries(store) -> list[dict]:
    """Every entry with its credential, for building providers. Not for returning."""
    out = []
    headers, origin = resolve_anthropic_headers(store)
    base, base_origin = resolve_api_base(store)
    out.append({"id": "anthropic", "kind": "anthropic", "name": "Anthropic", "headers": headers,
                "base_url": base, "base_source": base_origin, "source": origin,
                "stored": bool(store.get_setting("anthropic_key")),
                "gateway_stored": bool(store.get_setting("gateway_url")),
                "gateway_token_stored": bool(store.get_setting("gateway_token"))})
    stored = _stored(store)
    env = _openai_env()
    platform = _platform_env()
    for e in stored:
        if e["id"] == "anthropic":
            # the Anthropic credential lives in its own settings; this row only holds the
            # model list the endpoint reported (TR-303)
            out[0].update({k: e.get(k) for k in ("models", "models_error", "models_at") if k in e})
            continue
        if e["id"] == "openai" and not e.get("key") and env:
            # a stored OpenAI row holding only a model list: the env key is still the credential
            out.append({**e, "key": env["key"], "base_url": e.get("base_url") or env["base_url"],
                        "source": env["source"], "stored": False})
            continue
        if e["id"] == PLATFORM_ID:
            # the platform entry is the environment's; a stored row holds only its model list,
            # and means nothing once the deployment stops handing the cell a provider
            if platform:
                out.append({**platform, **{k: e.get(k) for k in ("models", "models_error", "models_at") if k in e},
                            "stored": False})
            continue
        out.append({**e, "source": "console" if e.get("key") else "", "stored": True})
    if not any(e["id"] == "openai" for e in stored) and env:
        out.append({**env, "stored": False})
    if platform and not any(e["id"] == PLATFORM_ID for e in stored):
        out.append({**platform, "stored": False})
    return out


def _configured(e: dict) -> bool:
    if e["kind"] == "anthropic":
        return bool(e.get("headers"))
    # a local server (Ollama, vLLM) needs no key: a base URL is enough for the compatible kind
    return bool(e.get("key")) or (e["kind"] == "openai_compatible" and bool(e.get("base_url")))


def default_id(store, entries: list[dict] | None = None) -> str | None:
    entries = entries if entries is not None else _entries(store)
    ok = {e["id"] for e in entries if _configured(e)}
    if not ok:
        return None
    # a console choice, then the environment's, then the platform's entry when the platform gave
    # the cell one (a hosted trial: the cell was born to run on it), then Anthropic
    for cand in ((store.get_setting(DEFAULT_SETTING) or "").strip(),
                 os.getenv("TARES_MODEL_PROVIDER", "").strip(), PLATFORM_ID, "anthropic"):
        if cand in ok:
            return cand
    return next(e["id"] for e in entries if e["id"] in ok)


def list_providers(store) -> dict:
    """The redacted listing the console shows: no credential, only whether one resolves."""
    entries = _entries(store)
    default = default_id(store, entries)
    rows = []
    for e in entries:
        row = {"id": e["id"], "kind": e["kind"], "name": e.get("name") or KIND_LABELS[e["kind"]],
               "base_url": e.get("base_url") or ("" if e["kind"] == "anthropic" else OPENAI_API_BASE),
               "configured": _configured(e), "source": e.get("source") or "",
               "stored": bool(e.get("stored")), "default": e["id"] == default,
               "models": models_for(e), "models_error": e.get("models_error") or "",
               # one line for the row; the full error stays in models_error
               "models_problem": _problem(e.get("models_error") or ""),
               "models_at": e.get("models_at") or "",
               "discovers": True}
        if e["kind"] == "anthropic":
            row["base_source"] = e.get("base_source") or ""
            row["gateway_stored"] = e.get("gateway_stored", False)
            row["gateway_token_stored"] = e.get("gateway_token_stored", False)
        rows.append(row)
    return {"providers": rows, "default": default, "kinds": [
        {"id": k, "label": KIND_LABELS[k]} for k in KINDS]}


def models_for(entry: dict) -> list[str]:
    if entry["kind"] == "anthropic":
        from .builtin_agents import AGENT_MODELS
        return list(entry.get("models") or AGENT_MODELS)
    if entry["kind"] == "openai":
        return list(entry.get("models") or OPENAI_MODELS)
    return list(entry.get("models") or [])


def _headers(entry: dict) -> dict:
    headers = {"Authorization": f"Bearer {entry['key']}"} if entry.get("key") else {}
    if "openrouter.ai" in (entry.get("base_url") or ""):
        # OpenRouter attributes traffic to an app by these; optional, and harmless elsewhere
        headers.update({"HTTP-Referer": "https://tares.glassflow.ai", "X-Title": "Tares"})
    return headers


def build(entry: dict) -> Provider | None:
    if not _configured(entry):
        return None
    if entry["kind"] == "anthropic":
        return AnthropicProvider(entry["base_url"], entry["headers"], timeout=TIMEOUT)
    return OpenAIProvider(entry.get("base_url") or OPENAI_API_BASE, _headers(entry),
                          timeout=TIMEOUT, label=entry["id"])


def _problem(error: str) -> str:
    """The one-line reading of a discovery error for the provider row."""
    if not error:
        return ""
    low = error.lower()
    if "rejected the key" in low or " 401" in low or " 403" in low:
        return "the endpoint rejected the key; check it and save again"
    if "connect" in low or "refused" in low or "timed out" in low or "timeout" in low:
        return "the endpoint did not answer; is it running and reachable from this daemon?"
    if "404" in low:
        return "no models endpoint at this base URL; check the URL ends with /v1"
    return "the endpoint did not list its models"


# ── model discovery (TR-303) ─────────────────────────────────────────────────
MAX_MODELS = 500


async def discover_models(entry: dict, timeout: float = 8.0) -> list[str]:
    """What an OpenAI-format endpoint serves: `GET <base>/models`, the ids of `data`. LiteLLM
    restricts the answer to what the key may use; OpenRouter, Ollama and vLLM list everything
    they hold. Raises on any failure so the caller can show why."""
    import httpx
    if entry["kind"] == "anthropic":
        base = (entry.get("base_url") or DEFAULT_API_BASE).rstrip("/")
        url, headers = f"{base}/v1/models", dict(entry.get("headers") or {})
    else:
        base = (entry.get("base_url") or OPENAI_API_BASE).rstrip("/")
        url, headers = f"{base}/models", _headers(entry)
    async with httpx.AsyncClient(timeout=timeout) as cx:
        r = await cx.get(url, headers=headers)
    if r.status_code in (401, 403):
        raise ValueError(f"the endpoint rejected the key ({r.status_code}): {r.text[:200]}")
    if r.status_code >= 400:
        raise ValueError(f"{url} answered {r.status_code}: {r.text[:200]}")
    try:
        data = r.json()
    except ValueError:
        raise ValueError(f"{url} did not answer with JSON")
    rows = data.get("data") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError(f"{url} answered without a model list")
    ids = []
    for row in rows:
        mid = row.get("id") if isinstance(row, dict) else row
        if isinstance(mid, str) and mid and mid not in ids:
            ids.append(mid)
    return sorted(ids)[:MAX_MODELS]


async def refresh_models(store, provider_id: str) -> dict:
    """Discover and store the model list of one OpenAI-format entry. A failure keeps the last
    list and records the error on the entry, so the console can say what went wrong."""
    entries = _stored(store)
    current = next((e for e in entries if e["id"] == provider_id), None)
    if current is None:
        if provider_id == "anthropic":
            # the credential lives in its own settings; this row holds only the model list
            current = {"id": "anthropic", "kind": "anthropic", "name": "Anthropic"}
        elif provider_id == "openai" and _openai_env():
            # the env-seeded OpenAI entry has no stored row yet; store one without a key so the
            # list has somewhere to live (the env key stays in use: a row with no key defers)
            current = {"id": "openai", "kind": "openai", "name": "OpenAI", "key": "", "base_url": ""}
        elif provider_id == PLATFORM_ID and _platform_env():
            current = {"id": PLATFORM_ID, "kind": "openai_compatible", "key": "", "base_url": ""}
        else:
            raise KeyError(f"unknown provider {provider_id!r}")
        entries.append(current)
    if provider_id == "anthropic":
        live = entry(store, "anthropic") or {}
        if not live.get("headers"):
            raise ValueError("no Anthropic key configured yet")
        probe = {"kind": "anthropic", "headers": live["headers"], "base_url": live.get("base_url")}
    else:
        probe = {**current}
    if current["id"] == "openai" and not current.get("key"):
        env = _openai_env()
        if env:
            probe = {**probe, "key": env["key"], "base_url": current.get("base_url") or env["base_url"]}
    if current["id"] == PLATFORM_ID:
        platform = _platform_env()
        if platform:
            probe = {**probe, "kind": "openai_compatible", "key": platform["key"], "base_url": platform["base_url"]}
    try:
        models = await discover_models(probe)
        current["models"], current["models_error"] = models, ""
    except Exception as e:  # noqa: BLE001 — the error is the result
        current["models_error"] = f"{type(e).__name__}: {e}"[:300]
    from datetime import datetime, timezone
    current["models_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _save(store, entries)
    return {"models": current.get("models") or [], "error": current.get("models_error") or ""}


def resolve_provider(store, provider_id: str | None = None) -> tuple[Provider | None, str]:
    """(provider, where-its-credential-came-from). The cell default when `provider_id` is empty.
    None when nothing usable is configured, or the named entry is missing or unconfigured."""
    entries = _entries(store)
    pid = provider_id or default_id(store, entries)
    if not pid:
        return None, ""
    entry = next((e for e in entries if e["id"] == pid), None)
    if entry is None:
        return None, ""
    return build(entry), entry.get("source") or ""


# ── edits ────────────────────────────────────────────────────────────────────
def _check_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url:
        return ""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("base URL must be an http(s) URL with a host, e.g. http://localhost:4000/v1")
    return url


def save_provider(store, provider_id: str, kind: str, name: str = "", key: str = "",
                  base_url: str = "") -> str:
    """Create or update one entry; returns its id. Blank key keeps the stored one."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    key = key.strip()
    base_url = _check_url(base_url)
    first = default_id(store) is None   # the first configured provider becomes the default
    pid = _save_entry(store, provider_id, kind, name, key, base_url)
    if first and default_id(store) == pid:
        store.set_setting(DEFAULT_SETTING, pid)
    return pid


def _save_entry(store, provider_id: str, kind: str, name: str, key: str, base_url: str) -> str:
    if provider_id == PLATFORM_ID or (not provider_id and slug(name) == PLATFORM_ID):
        raise ValueError("the platform's entry is set by the deployment; add your own provider instead")
    if provider_id == "anthropic" and kind != "anthropic":
        raise ValueError("the Anthropic entry keeps its kind; add a new entry instead")
    if provider_id == "openai" and kind != "openai":
        raise ValueError("the OpenAI entry keeps its kind; add a new entry instead")
    if kind == "anthropic":
        if provider_id not in ("", "anthropic"):
            raise ValueError("the Anthropic entry is always 'anthropic'")
        if key:
            store.set_setting("anthropic_key", key)
        if base_url and base_url != DEFAULT_API_BASE:
            store.set_setting("gateway_url", base_url)
        elif base_url == DEFAULT_API_BASE or (not base_url and provider_id):
            store.set_setting("gateway_url", None)
        return "anthropic"
    entries = _stored(store)
    if kind == "openai":
        pid = "openai"
    else:
        pid = provider_id or slug(name)
        if not pid:
            raise ValueError("an OpenAI-compatible endpoint needs a name")
        if pid in ("anthropic", "openai"):
            raise ValueError(f"'{pid}' is reserved; pick another name")
        if not base_url and not any(e["id"] == pid and e.get("base_url") for e in entries):
            raise ValueError("an OpenAI-compatible endpoint needs a base URL")
    current = next((e for e in entries if e["id"] == pid), None)
    if current and current.get("kind") != kind:
        raise ValueError(f"{current.get('name') or pid} keeps its kind; add a new entry instead")
    entry = {"id": pid, "kind": kind,
             "name": name.strip() or (current or {}).get("name") or KIND_LABELS[kind],
             "key": key or (current or {}).get("key") or "",
             "base_url": base_url or (current or {}).get("base_url") or "",
             "models": (current or {}).get("models") or [],
             "models_error": (current or {}).get("models_error") or "",
             "models_at": (current or {}).get("models_at") or ""}
    entries = [e for e in entries if e["id"] != pid] + [entry]
    _save(store, entries)
    return pid


def delete_provider(store, provider_id: str) -> None:
    if provider_id == PLATFORM_ID and _platform_env():
        raise ValueError("the platform's entry is set by the deployment and cannot be removed here")
    if provider_id == "anthropic":
        store.set_setting("anthropic_key", None)
        store.set_setting("gateway_url", None)
        store.set_setting("gateway_token", None)
    _save(store, [e for e in _stored(store) if e["id"] != provider_id])
    if (store.get_setting(DEFAULT_SETTING) or "") == provider_id:
        store.set_setting(DEFAULT_SETTING, None)


def set_default(store, provider_id: str) -> None:
    entries = _entries(store)
    entry = next((e for e in entries if e["id"] == provider_id), None)
    if entry is None:
        raise KeyError(f"unknown provider {provider_id!r}")
    if not _configured(entry):
        raise ValueError(f"{entry.get('name') or provider_id} has no credential yet")
    store.set_setting(DEFAULT_SETTING, provider_id)


async def discover_env_entries(store) -> None:
    """At daemon start: read the model list of every provider the environment seeded that has
    not been read yet (the platform's entry, an OpenAI key from the env). A console-saved entry
    is read when it is saved; an environment one has no such moment, and without this the picker
    stays empty until someone presses refresh. Failures are recorded on the entry, never raised."""
    for e in _entries(store):
        if e.get("stored") or e["kind"] == "anthropic" or e.get("models_at"):
            continue
        if not _configured(e):
            continue
        try:
            result = await refresh_models(store, e["id"])
            print(f"taresd: provider {e['id']!r} lists {len(result['models'])} model(s)"
                  + (f"; {result['error']}" if result.get("error") else ""))
        except Exception as exc:  # noqa: BLE001 — startup must never fail over a model list
            print(f"taresd: provider {e['id']!r} model discovery failed: {exc}")


# ── per agent (TR-302) ───────────────────────────────────────────────────────
def entry(store, provider_id: str) -> dict | None:
    return next((e for e in _entries(store) if e["id"] == provider_id), None)


def default_model_for(store, provider_id: str | None) -> str:
    """The model an agent runs on when it names none: TARES_AGENT_MODEL on Anthropic (and on the
    cell default, when the variable is set on purpose), else the first model the provider lists.
    Empty when a router lists nothing yet: the agent must then name one."""
    from .builtin_agents import MODEL
    pid = provider_id or default_id(store)
    e = entry(store, pid) if pid else None
    if e is None or e["kind"] == "anthropic":
        return MODEL
    if os.getenv("TARES_AGENT_MODEL", "").strip() and pid == default_id(store):
        return MODEL
    models = models_for(e)
    return models[0] if models else ""


def resolve_for_agent(store, agent: dict) -> tuple[Provider | None, str, str | None, str]:
    """(provider, credential origin, provider id, note) for one agent. The agent's own provider
    when it names one that is configured; else the cell default, with a note saying why, so a
    template that names a provider this cell lacks still runs instead of failing (TR-302)."""
    want = (agent.get("provider") or "").strip()
    if want:
        e = entry(store, want)
        if e is not None and _configured(e):
            return build(e), e.get("source") or "", want, ""
        why = ("is not configured" if e is not None else "does not exist")
    pid = default_id(store)
    if not pid:
        return None, "", None, ""
    e = entry(store, pid)
    note = f"provider {want!r} {why}; ran on the default ({pid})" if want else ""
    return build(e), e.get("source") or "", pid, note
