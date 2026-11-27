"""Station telemetry settings (Tares 2.0, TR-1808): where crew stations send Claude Code's own OTLP logs,
traces and metrics. The monitoring slot (integrations/builtin/monitoring_tares.py) shows and changes them;
the plugin fetches the headers from /api/factory/monitoring/headers when a station starts. The cell itself
exports nothing with them.

    provider `rius`  endpoint defaults to Rius's ingest, the API key becomes a bearer header
    provider `otlp`  any endpoint, headers given explicitly (Langfuse, Phoenix, a collector)

A value saved in the console wins over the environment (TARES_TRACING_*). Moved out of tracing.py, the
cell's own agent tracing, which Tares 2.0 removes (TR-1810); the stored setting keys are unchanged.
"""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field

PROVIDERS = ("rius", "otlp")
RIUS_ENDPOINT = "https://ingest.eu.console.rius-glassflow.com"
RIUS_CONSOLE_URL = "https://eu.console.rius-glassflow.com"

# settings-table keys and their environment fallbacks
SETTING_ENABLED = ("tracing_enabled", "TARES_TRACING_ENABLED")
SETTING_PROVIDER = ("tracing_provider", "TARES_TRACING_PROVIDER")
SETTING_ENDPOINT = ("tracing_endpoint", "TARES_TRACING_ENDPOINT")
SETTING_API_KEY = ("tracing_api_key", "TARES_TRACING_API_KEY")
SETTING_HEADERS = ("tracing_headers", "TARES_TRACING_HEADERS")
ENV_INSTANCE = "TARES_INSTANCE_NAME"

_TRUE = ("1", "true", "yes", "on")


def _read(store, key: str, env: str) -> tuple[str, str]:
    """(value, where-it-came-from): the console-stored value, else the environment, else ''."""
    stored = (store.get_setting(key) or "").strip() if store is not None else ""
    if stored:
        return stored, "console"
    val = os.getenv(env, "").strip()
    return (val, f"env:{env}") if val else ("", "")


def parse_headers(raw: str) -> dict[str, str]:
    """`k=v,k2=v2` (the OTEL_EXPORTER_OTLP_HEADERS shape) -> dict. Bad pairs are skipped."""
    out: dict[str, str] = {}
    for pair in (raw or "").split(","):
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        if k.strip():
            out[k.strip()] = v.strip()
    return out


def instance_name() -> str:
    return os.getenv(ENV_INSTANCE, "").strip() or socket.gethostname() or "tares"


def tares_version() -> str:
    try:
        from importlib.metadata import version as _v
        return _v("tares")
    except Exception:
        return "dev"


@dataclass(frozen=True)
class TracingConfig:
    enabled: bool
    provider: str
    endpoint: str
    headers: dict[str, str]
    instance: str
    # where each value came from ("console", "env:...", "preset", "default", "")
    sources: dict[str, str] = field(default_factory=dict)
    key_stored: bool = False
    key_configured: bool = False
    headers_configured: bool = False

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.endpoint)

    @property
    def fingerprint(self) -> tuple:
        return (self.endpoint, tuple(sorted(self.headers.items())), self.instance)


def resolve(store) -> TracingConfig:
    enabled_raw, enabled_src = _read(store, *SETTING_ENABLED)
    provider, provider_src = _read(store, *SETTING_PROVIDER)
    endpoint, endpoint_src = _read(store, *SETTING_ENDPOINT)
    api_key, key_src = _read(store, *SETTING_API_KEY)
    headers_raw, headers_src = _read(store, *SETTING_HEADERS)

    provider = provider.lower()
    if provider not in PROVIDERS:
        # No provider named: an explicit endpoint means "some OTLP backend"; a key alone means
        # Rius, whose endpoint the preset knows.
        provider, provider_src = ("otlp", "default") if endpoint else ("rius", "default")
    if provider == "rius" and not endpoint:
        endpoint, endpoint_src = RIUS_ENDPOINT, "preset"

    headers = parse_headers(headers_raw)
    if api_key and not any(k.lower() == "authorization" for k in headers):
        headers["authorization"] = f"Bearer {api_key}"

    return TracingConfig(
        enabled=enabled_raw.lower() in _TRUE,
        provider=provider, endpoint=endpoint.rstrip("/"), headers=headers,
        instance=instance_name(),
        sources={"enabled": enabled_src, "provider": provider_src, "endpoint": endpoint_src,
                 "api_key": key_src, "headers": headers_src},
        key_stored=bool((store.get_setting(SETTING_API_KEY[0]) or "").strip()) if store else False,
        key_configured=bool(api_key), headers_configured=bool(headers_raw),
    )


def status(store) -> dict:
    """What the settings API returns: everything but the secrets."""
    cfg = resolve(store)
    return {
        "enabled": cfg.enabled, "enabled_source": cfg.sources["enabled"],
        "active": cfg.active,
        "provider": cfg.provider, "provider_source": cfg.sources["provider"],
        "endpoint": cfg.endpoint, "endpoint_source": cfg.sources["endpoint"],
        "key_configured": cfg.key_configured, "key_source": cfg.sources["api_key"],
        "key_stored": cfg.key_stored,
        "headers_configured": cfg.headers_configured, "headers_source": cfg.sources["headers"],
        "instance": cfg.instance,
        "providers": list(PROVIDERS),
        "rius_console_url": RIUS_CONSOLE_URL,
    }
