"""The monitoring slot over the tracing settings (TR-866): off, Rius and any OTLP backend.

The tracing settings (`tracing_enabled`, `tracing_provider`, `tracing_endpoint`,
`tracing_api_key`, `tracing_headers`, with their `TARES_TRACING_*` environment fallbacks) stay
the only storage. `tares.tracing.resolve` reads them for the cell's own agent traces, and
`/api/settings/tracing` writes them. The slot reads and writes the same keys, so choosing a
monitoring integration through the integrations API and through the old route never disagree.
The store hands the monitoring slot's writes to `save` and its stored settings to `stored`.

A crew station's Claude Code sessions report to the same backend. `station_env` gives the
telemetry variables for the endpoint and never a key or a header value: those are secrets, and
an environment is readable by every process of the session. They come at run time instead,
through Claude Code's `otelHeadersHelper` setting (`station_settings`), which runs
`tares hook otel-headers` and prints the headers fetched from `GET /api/factory/monitoring/headers`.

Claude Code variables used (docs: code.claude.com/docs/en/monitoring-usage):
    CLAUDE_CODE_ENABLE_TELEMETRY=1                       turns telemetry on
    CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1                traces are a beta and need it
    OTEL_METRICS_EXPORTER, OTEL_LOGS_EXPORTER, OTEL_TRACES_EXPORTER = otlp
    OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf            the helper works over http only
    OTEL_EXPORTER_OTLP_ENDPOINT                          the base address; each signal adds /v1/<signal>
    OTEL_RESOURCE_ATTRIBUTES                             tares.station, tares.role, tares.project
"""
from __future__ import annotations

from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from ..monitoring import Monitoring
from ..types import Field
from ... import tracing

HELPER_COMMAND = "tares hook otel-headers"
# the labels a station's telemetry carries; a ticket is left out: a station works many of them
LABELS = ("station", "role", "project")
# the stored tracing keys the slot's settings map to (tracing.SETTING_*)
_KEYS = {"endpoint": "tracing_endpoint", "api_key": "tracing_api_key", "headers": "tracing_headers"}
PICK_UP = "Running stations pick this up on their next restart."


def clean_endpoint(raw: str) -> str:
    """The endpoint as a station may see it: scheme, host, port and path only. A user, a password,
    a query or a fragment (where a token could ride) are dropped. "" for anything that is not an
    http(s) address with a host."""
    try:
        u = urlsplit(str(raw or "").strip())
        if u.scheme not in ("http", "https") or not u.hostname:
            return ""
        host = u.hostname if ":" not in u.hostname else f"[{u.hostname}]"
        netloc = f"{host}:{u.port}" if u.port else host
        return urlunsplit((u.scheme, netloc, u.path.rstrip("/"), "", ""))
    except ValueError:
        return ""


def resource_attributes(labels: dict) -> str:
    """`k=v,k2=v2` for OTEL_RESOURCE_ATTRIBUTES: the station, role and project labels that have a
    value, each value percent-encoded (the variable allows no spaces, commas or `=`)."""
    out = []
    for key in LABELS:
        value = str((labels or {}).get(key) or "").strip()
        if value:
            out.append(f"tares.{key}={quote(value, safe='')}")
    return ",".join(out)


# ── the stored tracing settings ───────────────────────────────────────────────
def stored_provider(store) -> str:
    """The provider in use, as tracing.resolve takes it (stored setting, else the TARES_TRACING_*
    environment, else otlp when an endpoint is set, else rius)."""
    return tracing.resolve(store).provider


def stored(store, name: str) -> dict:
    """The stored settings of integration `name`, secrets included: {endpoint, api_key, headers},
    those that are set. {} for off, and for a provider other than the stored one."""
    if name not in tracing.PROVIDERS or stored_provider(store) != name:
        return {}
    got = {k: (store.get_setting(key) or "").strip() for k, key in _KEYS.items()}
    return {k: v for k, v in got.items() if v}


def fields_of(name: str) -> tuple:
    """The settings the integration `name` has, which are the only stored keys it may write."""
    cls = {"rius": MonitoringRius, "otlp": MonitoringOtlp}.get(name)
    return tuple(f.name for f in cls.settings_schema) if cls else ()


def save(store, name: str, merged: dict) -> None:
    """Choose monitoring integration `name` with `merged` settings (secrets already reconciled
    with `stored`). Off switches tracing off and keeps the rest, so switching it back on needs
    nothing typed again. Rius and OTLP write the provider and the values their own settings have
    (an empty one clears the stored value, the environment's takes over again) and switch tracing
    on. A value the integration has no setting for, such as the headers of a Rius choice that were
    set on /api/settings/tracing, is left as it is."""
    if name == "off":
        store.set_setting("tracing_enabled", "0")
        return
    store.set_setting("tracing_provider", name)
    for field in fields_of(name):
        value = str((merged or {}).get(field) or "").strip()
        store.set_setting(_KEYS[field], value or None)
    store.set_setting("tracing_enabled", "1")


class _Tracing(Monitoring):
    """Shared by rius and otlp: both read the cell's tracing settings. `store` is the cell's
    store; built bare (the settings page tests a connection) only `check_connection` works."""
    ee = False

    def __init__(self, store=None):
        self.store = store

    def _config(self):
        """The cell's tracing configuration when this integration is the one in use and it is
        active; None otherwise (also with no store, or one that cannot be read)."""
        if self.store is None:
            return None
        try:
            cfg = tracing.resolve(self.store)
        except Exception:  # a broken settings read gives nothing, like tracing itself
            return None
        return cfg if cfg.active and cfg.provider == self.name else None

    def exporter_config(self) -> dict:
        cfg = self._config()
        if cfg is None:
            return {}
        return {"provider": cfg.provider, "endpoint": cfg.endpoint, "headers": dict(cfg.headers)}

    def station_env(self, labels: dict) -> dict:
        cfg = self._config()
        endpoint = clean_endpoint(cfg.endpoint) if cfg else ""
        if not endpoint:
            return {}
        env = {
            "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
            "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
            "OTEL_METRICS_EXPORTER": "otlp",
            "OTEL_LOGS_EXPORTER": "otlp",
            "OTEL_TRACES_EXPORTER": "otlp",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
        }
        attrs = resource_attributes(labels)
        if attrs:
            env["OTEL_RESOURCE_ATTRIBUTES"] = attrs
        return env

    def station_settings(self) -> dict:
        cfg = self._config()
        if cfg is None or not clean_endpoint(cfg.endpoint) or not cfg.headers:
            return {}
        return {"otelHeadersHelper": HELPER_COMMAND}

    async def check_connection(self, settings: dict) -> tuple[bool, str]:
        endpoint = str((settings or {}).get("endpoint") or "").strip() or (
            tracing.RIUS_ENDPOINT if self.name == "rius" else "")
        if not endpoint:
            return False, "Enter the address of the OTLP endpoint, for example https://collector:4318."
        if not clean_endpoint(endpoint):
            return False, "The endpoint must be an http or https address, for example https://collector:4318."
        headers = tracing.parse_headers(str((settings or {}).get("headers") or ""))
        key = str((settings or {}).get("api_key") or "").strip()
        if key and not any(k.lower() == "authorization" for k in headers):
            headers["authorization"] = f"Bearer {key}"
        headers["content-type"] = "application/x-protobuf"
        shown = clean_endpoint(endpoint)     # never echo a user, password, query or fragment
        url = endpoint.rstrip("/") + "/v1/traces"
        try:
            # an empty body is a valid, empty export request: a collector answers 200 and sends nothing on
            async with httpx.AsyncClient(timeout=8) as cx:
                r = await cx.post(url, content=b"", headers=headers)
        except httpx.HTTPError as e:
            return False, f"Could not reach {shown}: {type(e).__name__}."
        if r.status_code in (401, 403):
            return False, f"{shown} refused the key or headers (HTTP {r.status_code})."
        if r.status_code >= 400 and r.status_code not in (400, 415):
            return False, f"{shown} answered HTTP {r.status_code} to a trace export."
        return True, f"{shown} accepts trace exports."


_SCHEMA_ENDPOINT = "OTLP/HTTP address, without /v1/traces, for example https://collector:4318. "


class MonitoringOff(Monitoring):
    name = "off"
    title = "Off"
    ee = False
    settings_schema: list = []

    def __init__(self, store=None):
        self.store = store

    async def check_connection(self, settings: dict) -> tuple[bool, str]:
        return True, "Nothing is sent."

    def exporter_config(self) -> dict:
        return {}

    def station_env(self, labels: dict) -> dict:
        return {}

    def station_settings(self) -> dict:
        return {}


class MonitoringRius(_Tracing):
    name = "rius"
    title = "Rius"
    settings_schema = [
        Field(name="api_key", label="Rius API key", kind="secret", required=False,
              help="Sent to Rius as a bearer key. Stations never see it: they fetch it when they "
                   "start. " + PICK_UP),
        Field(name="endpoint", label="Address", kind="url", required=False,
              help="Leave empty for Rius's own ingest address."),
    ]


class MonitoringOtlp(_Tracing):
    name = "otlp"
    title = "OTLP endpoint"
    settings_schema = [
        Field(name="endpoint", label="Address", kind="url", required=True,
              help=_SCHEMA_ENDPOINT + PICK_UP),
        Field(name="headers", label="Headers", kind="secret", required=False,
              help="k=v,k2=v2, for a backend that wants its own headers (Langfuse, Phoenix)."),
        Field(name="api_key", label="API key", kind="secret", required=False,
              help="Sent as a bearer key when the headers have no authorization."),
    ]
