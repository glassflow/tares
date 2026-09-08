"""Connector registry - maps catalog `connector:` names to implementations.

SPECS describes each connector's config surface so the UI can render forms and validate input
without hardcoding connector knowledge - the registry is self-describing (GET /api/connectors).
Field types: string | number | json. `required` fields gate source creation.
"""
from __future__ import annotations

import re as _re

from ..config import CatalogError
from .prometheus_alerts import PrometheusAlertsConnector
from .alertmanager import AlertmanagerConnector
from .base import UNIVERSAL_CONFIG
from .claude_code import ClaudeCodeConnector
from .docker_logs import DockerLogsConnector
from .github import GithubConnector
from .loki import LokiConnector
from .memory import MemoryConnector
from .otlp import OtlpConnector
from .postgres import PostgresConnector
from .prometheus import PrometheusConnector
from .finding import FindingConnector
from .reference import ReferenceConnector
from .threat_intel import ThreatIntelConnector
from .vercel import VercelConnector
from .webhook import WebhookConnector

REGISTRY = {
    "prometheus": PrometheusConnector,
    "docker_logs": DockerLogsConnector,
    "loki": LokiConnector,
    "prometheus_alerts": PrometheusAlertsConnector,
    "alertmanager": AlertmanagerConnector,
    "reference": ReferenceConnector,
    "webhook": WebhookConnector,
    "memory": MemoryConnector,
    "otlp": OtlpConnector,
    "github": GithubConnector,
    "vercel": VercelConnector,
    "postgres": PostgresConnector,
    "claude_code": ClaudeCodeConnector,
    "finding": FindingConnector,
    "threat_intel": ThreatIntelConnector,
}

# Connector metadata for the UI. The `fields` of each are GENERATED from the connector's
# CONFIG_SCHEMA at the bottom of this module (single source of truth - no parallel definition).
SPECS = {
    "prometheus": {"label": "Prometheus", "mode": "poll", "discover": True,
                   "description": "Instant-query snapshots of PromQL expressions on every poll tick."},
    "docker_logs": {"label": "Docker logs", "mode": "poll", "discover": True,
                    "description": "Tails a running container's logs; all lines by default; "
                                   "optional match/drop regex filters."},
    "loki": {"label": "Grafana Loki", "mode": "poll", "poll": "5s",
             "description": "Polls a LogQL stream selector via query_range (cursor by "
                            "timestamp); one event per log line, with the stream's labels. "
                            "Works against any reachable Loki, including Grafana Cloud."},
    "prometheus_alerts": {"label": "Prometheus alerts", "mode": "poll", "discover": True, "poll": "30s",
                          "description": "Polls Prometheus's own /api/v1/alerts, the alerts its rules "
                                         "have fired, and emits one event per active alert (keyed by a "
                                         "label), plus a resolved event when it clears. No Alertmanager, "
                                         "no PromQL."},
    "alertmanager": {"label": "Alertmanager alerts", "mode": "push",
                     "description": "Push receiver for Alertmanager; point a webhook_configs.url at this "
                                    "source's ingest endpoint. Each alert Alertmanager routes (with its "
                                    "grouping, silencing and firing/resolved status) becomes one event, "
                                    "keyed by a label. No PromQL."},
    "reference": {"label": "Reference documents", "mode": "reference",
                  "description": "Documents (json/csv/md/txt) attached to an entity by its labels; "
                                 "project notes, schemas, runbooks. Always surfaced when correlating "
                                 "on that entity, regardless of time window. Edit to add or remove."},
    "webhook": {"label": "Inbound webhook", "mode": "push",
                "description": "Push ingestion: producers POST JSON (or NDJSON) to this source's "
                               "ingest endpoint. Lossless; declare labels to map payload fields "
                               "into the envelope."},
    "memory": {"label": "Agent memory", "mode": "push",
               "description": "The agent's own observations, written back via the `remember` MCP tool "
                              "(or POST /remember). Joinable into views like any other source."},
    "otlp": {"label": "OpenTelemetry (OTLP)", "mode": "push",
             "description": "Receives OTLP/HTTP logs, traces and metrics at POST /v1/{logs,traces,"
                            "metrics}. One source ingests every service; resource attributes "
                            "(service.name) become labels."},
    "github": {"label": "GitHub commits", "mode": "poll", "discover": True, "poll": "2m",
               "description": "Polls a repo's commits (cursor by SHA); one event per commit, "
                              "keyed by repo, with author as a label."},
    "vercel": {"label": "Vercel logs", "mode": "push",
               "description": "Push source for Vercel logs; point a Vercel log drain (JSON) at this "
                              "source's ingest endpoint; one event per log entry, keyed by project, "
                              "with environment + source labels."},
    "postgres": {"label": "Postgres table", "mode": "poll", "discover": True, "poll": "30s",
                 "description": "Polls a table incrementally (cursor by an id or updated_at); one "
                                "event per new/changed row, keyed by an entity column (tenant_id). "
                                "Your application's source-of-truth data in the timeline."},
    "threat_intel": {"label": "Threat-intel feed", "mode": "poll", "discover": True, "poll": "5m",
                     "description": "Polls a JSON indicator feed (MISP/OTX/AbuseIPDB-shaped "
                                    "export, or a local file); one event per new indicator, keyed "
                                    "by the indicator itself (an IP, domain, or hash) so it lands "
                                    "on the same timeline as anything else keyed by that value - "
                                    "auth logs, account activity, whatever else you already run."},
    "claude_code": {"label": "Claude Code sessions", "mode": "poll",
                    "description": "Tails local Claude Code session transcripts "
                                   "(~/.claude/projects/*.jsonl) into the data plane; one event per "
                                   "message, keyed by session, with repo/branch/model labels and "
                                   "token-usage fields. Sub-agents roll up into the same source. "
                                   "Secrets are redacted before storage."},
    # `internal`: provisioned by Tares itself (the first finding creates it), never offered in
    # "Add source". Still a first-class source everywhere else - it appears on timelines, in the
    # catalog and in exports like any other.
    "finding": {"label": "Agent findings", "mode": "push", "internal": True,
                "description": "What Tares agents conclude when a trigger fires; one finding per "
                               "run, keyed to the entity, on that entity's timeline."},
}


# A source's signal type is a property of its connector, not something the user authors. Mostly
# descriptive - EXCEPT "reference", which the read path treats specially (always surfaced, never
# time-windowed; see store.read_view_window).
_SOURCE_TYPES = {"docker_logs": "application_log", "loki": "application_log",
                 "memory": "agent_memory",
                 "otlp": "application_log", "vercel": "application_log",
                 "claude_code": "agent_session", "reference": "reference",
                 "finding": "finding"}


def source_type_for(connector: str) -> str:
    return _SOURCE_TYPES.get(connector, "event_stream")


# ── connector config schema = the source of truth (form, discover, YAML, API all normalize to it)

def full_schema(connector: str) -> dict | None:
    """A connector's complete config schema (its own + the universal keys), or None if it hasn't
    declared one (those connectors keep their hand-written SPECS and skip normalization)."""
    schema = getattr(REGISTRY.get(connector), "CONFIG_SCHEMA", None)
    return {**schema, **UNIVERSAL_CONFIG} if schema is not None else None


# Placeholder returned in place of a stored secret (a connector token/DSN). Chosen so it can never
# be a real credential; the update path treats a field still equal to this as "unchanged".
REDACTED_SECRET = "••••••••"


def secret_field_names(connector: str) -> set:
    """Config keys a connector marks `secret: True` in its CONFIG_SCHEMA (e.g. github `token`,
    postgres `dsn`). These must never be serialized to a client - including the built-in agent and
    MCP, which read source config over /api/sources."""
    schema = full_schema(connector)
    if not schema:
        return set()
    return {k for k, spec in schema.items() if isinstance(spec, dict) and spec.get("secret")}


def redact_config(connector: str, config: dict) -> dict:
    """A copy of `config` with secret fields masked, for any response leaving the daemon. The stored
    catalog keeps the real values (the connector reads those at runtime); only the wire form is
    redacted. Empty secrets stay empty so the UI can tell 'not set' from 'set'."""
    secrets = secret_field_names(connector)
    if not secrets or not isinstance(config, dict):
        return config
    return {k: (REDACTED_SECRET if k in secrets and v not in (None, "") else v)
            for k, v in config.items()}


def restore_secrets(connector: str, new_config: dict, existing_config: dict | None) -> dict:
    """Reconcile a saved config with the stored secrets a client never saw. Blank-to-keep semantics:
      • secret OMITTED (or echoed back as the redaction placeholder) → keep the stored value
      • secret present and EMPTY ("")                                → clear it (explicit remove)
      • secret present and non-empty                                 → replace with the new value
    So editing other fields never wipes a token, and clearing one is an explicit act."""
    secrets = secret_field_names(connector)
    if not secrets or not isinstance(new_config, dict):
        return new_config
    out = dict(new_config)
    for k in secrets:
        if k not in new_config or out.get(k) == REDACTED_SECRET:   # omitted / placeholder → keep
            existing = (existing_config or {}).get(k)
            if existing not in (None, ""):
                out[k] = existing
            else:
                out.pop(k, None)
        elif out.get(k) in (None, ""):                             # explicit empty → clear
            out.pop(k, None)
        # else: a real new value → replace (passes through)
    return out


_LABEL_NAME = _re.compile(r"^[A-Za-z0-9_]+$")


_MAX_PATTERN_LEN = 256


def _coerce_labels(val) -> list:
    out, primaries = [], 0
    for spec in val:
        if not isinstance(spec, dict) or not spec.get("name"):
            raise CatalogError(f"label needs a name (got {spec!r})")
        if not _LABEL_NAME.match(str(spec["name"])):
            raise CatalogError(f"label name {spec['name']!r} must be alphanumeric/_")
        if ("const" in spec) == ("field" in spec):
            raise CatalogError(f"label {spec['name']!r} needs exactly one of const or field")
        row = {"name": str(spec["name"]),
               **({"const": str(spec["const"])} if "const" in spec else {"field": str(spec["field"])})}
        # value normalization (field labels only): one regex substitution, then one exact-alias
        # map - validated here so a bad pattern is a save-time 400, never a per-event failure
        # (docs/design/label-value-normalization.md)
        if any(k in spec for k in ("pattern", "replace", "map")):
            if "const" in spec:
                raise CatalogError(f"label {row['name']!r}: normalization applies to field labels only")
            if spec.get("pattern"):
                pat = str(spec["pattern"])
                if len(pat) > _MAX_PATTERN_LEN:
                    raise CatalogError(f"label {row['name']!r}: pattern too long (max {_MAX_PATTERN_LEN})")
                try:
                    _re.compile(pat)
                except _re.error as e:
                    raise CatalogError(f"label {row['name']!r}: invalid pattern: {e}")
                row["pattern"] = pat
                row["replace"] = str(spec.get("replace") or "")
            elif "replace" in spec and spec.get("replace"):
                raise CatalogError(f"label {row['name']!r}: replace needs a pattern")
            if spec.get("map"):
                if not isinstance(spec["map"], dict):
                    raise CatalogError(f"label {row['name']!r}: map must be an object of "
                                       "observed-value -> canonical-value strings")
                row["map"] = {str(k): str(v) for k, v in spec["map"].items()}
        # typed labels (v0.1.25): number labels are the aggregatable ones. Preserve the declared
        # type through normalization - dropping it here silently reverts a number label to a string
        # (not aggregatable). Validation (config._validate_labels) already rejects bad types / a
        # numeric primary, so we only need to carry a valid value forward.
        if spec.get("type") in ("string", "number"):
            row["type"] = spec["type"]
        if spec.get("primary"):       # the primary label is the entity key
            row["primary"] = True
            primaries += 1
        out.append(row)
    if primaries > 1:
        raise CatalogError("at most one label can be primary (the key)")
    return out


def _coerce(spec: dict, val):
    t = spec["type"]
    if t == "string":
        return str(val)
    if t == "number":
        f = float(val)
        return int(f) if f.is_integer() else f
    if t == "bool":
        return bool(val)
    if t == "labels":
        return _coerce_labels(val)
    if t == "list":
        return [_normalize_against(spec["item"], e, "list item") for e in val]
    return val


def _normalize_against(schema: dict, raw, where: str) -> dict:
    """Terse-canonicalize `raw` against a (sub)schema: schema key order, type-coerced, required
    enforced, defaults dropped, unknown keys rejected."""
    if not isinstance(raw, dict):
        raise CatalogError(f"{where}: expected an object, got {raw!r}")
    out = {}
    for key, spec in schema.items():
        val = raw.get(key)
        if val is None or val == "" or val == []:
            if spec.get("required"):
                raise CatalogError(f"{where}: {key!r} is required")
            continue
        cval = _coerce(spec, val)
        if "default" in spec and cval == spec["default"]:
            continue  # omit values equal to their default (terse but deterministic)
        out[key] = cval
    unknown = set(raw) - set(schema)
    if unknown:
        raise CatalogError(f"{where}: unknown keys {sorted(unknown)}")
    return out


def normalize_label_specs(specs: list) -> list:
    """Validate/canonicalize label specs alone (same rules as a config save) - for callers that
    work with a label spec outside a full source config, e.g. the normalization preview."""
    return _coerce_labels(specs or [])


def normalize_config(connector: str, raw: dict) -> dict:
    """Canonical config for a source: every entry path runs through this, so a source set up by
    hand, via Discover, via YAML, or via the API all produce the identical stored config (and so
    the identical exported YAML). Connectors without a declared schema pass through unchanged."""
    schema = full_schema(connector)
    if schema is None:
        return raw or {}
    return _normalize_against(schema, raw or {}, f"connector {connector!r}")


def _fields_from_schema(schema: dict) -> list:
    """SPECS form fields generated from a config schema (labels get the form's own editor).
    A `list` field carries its `item` sub-fields so the UI can render a row-by-row builder."""
    def scalar(name, spec):
        ftype = {"number": "number", "bool": "bool"}.get(spec["type"], "string")
        return {"name": name, "type": ftype, "required": spec.get("required", False),
                "help": spec.get("help", ""), "secret": spec.get("secret", False),
                "discover_input": spec.get("discover_input", False),
                # the value the connector uses when the field is left empty; the form shows it
                **({"default": spec["default"]} if "default" in spec else {})}

    fields = []
    for name, spec in schema.items():
        if spec["type"] == "labels" or spec.get("advanced"):
            continue  # labels get the dedicated editor; advanced/legacy keys are kept in the
                      # schema (so they round-trip) but hidden from the form - set via a primary label
        if spec["type"] == "list":
            fields.append({"name": name, "type": "list", "required": spec.get("required", False),
                           "help": spec.get("help", ""),
                           "item": [scalar(k, v) for k, v in spec.get("item", {}).items()]})
        elif spec["type"] == "object":
            fields.append({"name": name, "type": "json", "required": spec.get("required", False),
                           "help": spec.get("help", "")})
        else:
            fields.append(scalar(name, spec))
    return fields


# Every connector's SPECS form fields are generated from its CONFIG_SCHEMA (single source of truth).
# `provides` (a connector's synthesized label fields) is surfaced so the form can offer them.
for _name, _conn in REGISTRY.items():
    if getattr(_conn, "CONFIG_SCHEMA", None) is not None:
        SPECS[_name]["fields"] = _fields_from_schema(full_schema(_name))
    _provides = getattr(_conn, "PROVIDES", None)
    if _provides:
        SPECS[_name]["provides"] = _provides


def build_connector(cfg, store):
    return REGISTRY[cfg.connector](cfg, store)
