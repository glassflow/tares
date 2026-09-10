"""Load and type the catalog YAML — the MVP stand-in for the design doc's Catalog Service.

Declares the sources (what to ingest and how), the views (named query shapes), and the triggers
(conditions that fire a push). Coral-style declarative config, not a service.
"""
from __future__ import annotations

import os


def reject_legacy_db(db_path: str) -> None:
    """Refuse to start if the database we are about to create sits next to a pre-1.0 one.

    1.0 renamed the DuckDB file to tares.duckdb. DuckDB happily CREATES a missing file, so an
    install that upgraded without moving its data would come up healthy and completely empty, with
    navflow.duckdb sitting untouched beside it — indistinguishable from data loss, and the kind of
    thing you only notice after the console shows nothing.

    Migrating is one `mv`. Not doing it silently is the whole point.
    """
    import os as _os
    if _os.path.exists(db_path):
        return                      # the new file exists: nothing to warn about
    legacy = _os.path.join(_os.path.dirname(db_path) or ".", "navflow.duckdb")
    if not _os.path.exists(legacy):
        return                      # a genuinely fresh install
    raise SystemExit(
        f"found a pre-1.0 database at {legacy}, and none at {db_path}.\n\n"
        "tares 1.0 renamed the file. Starting now would create an empty database and leave your "
        "data untouched beside it, which looks exactly like data loss.\n\n"
        f"    mv {legacy} {db_path}\n\n"
        "then start again."
    )


def reject_legacy_env(environ=None) -> None:
    """Refuse to start if the environment still uses the pre-1.0 NAVFLOW_* variables.

    1.0 renamed every variable to TARES_* with NO fallback — a compatibility shim here would be two
    code paths nobody ever deletes. But silence is the wrong failure: a daemon started with only
    NAVFLOW_DB set would quietly ignore it, open a DuckDB file somewhere else, and come up healthy
    and empty. That looks like data loss and isn't.

    So: fail immediately, and name the variable the operator should have set. Loud beats forgiving.
    """
    import os as _os
    env = _os.environ if environ is None else environ
    legacy = sorted(k for k in env if k.startswith("NAVFLOW_"))
    if not legacy:
        return
    mapping = "\n".join(f"  {k}  ->  TARES_{k[len('NAVFLOW_'):]}" for k in legacy)
    raise SystemExit(
        "tares 1.0 renamed every NAVFLOW_* environment variable to TARES_*, and does NOT read the "
        "old names.\n\nStill set in this environment:\n"
        f"{mapping}\n\n"
        "Rename them and start again. (The DuckDB file itself is unchanged; your data is where you "
        "left it.)"
    )


import re
import uuid
from dataclasses import dataclass, field as dc_field
from functools import lru_cache
from pathlib import Path

import yaml

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(s) -> float:
    """'5s' -> 5.0, '15m' -> 900.0, '6h' -> 21600.0."""
    s = str(s).strip()
    return float(s[:-1]) * _UNITS[s[-1]]


@dataclass
class SourceCfg:
    name: str
    type: str
    connector: str
    poll_seconds: float
    config: dict = dc_field(default_factory=dict)
    poll: str = "5s"          # original duration string, kept for round-tripping
    paused: bool = False
    ingest_key: str = ""      # stable unguessable path segment for push endpoints (/ingest/<key>)


@dataclass
class ViewCfg:
    name: str
    key_field: str
    sources: list
    filters: list = dc_field(default_factory=list)   # [{field, op, value}] applied on read + trigger eval
    created_by: str = "human"                        # "human" | "agent:<client>" (design doc §6.5)


@dataclass
class Condition:
    aggregate: str          # count | sum | avg | max | min | any
    predicate: str          # "> 1.0", ">= 100", "== 0"
    window: str             # "1m", "5m"
    field: str | None = None
    group_by: list = dc_field(default_factory=lambda: ["key_value"])


@dataclass
class TriggerCfg:
    name: str
    view: str
    condition: Condition
    emit: dict = dc_field(default_factory=dict)
    cooldown_seconds: float = 300.0
    paused: bool = False


@dataclass
class AgentCfg:
    """A Tares agent: a prompt attached to a trigger. When the trigger fires, the agent reads the
    correlated timeline it was handed and writes ONE finding back into Tares. It's a real agent
    (it reasons with an LLM), configured inside Tares rather than connected over a webhook; today
    its tools are read-only (query/read). The prompt is the only field a user edits; model/tools/
    budgets are Tares's decisions. `enabled` is derived — an agent is enabled exactly when it has
    a subscription to its trigger, the same wiring an external agent has (docs/design/navflow-agents.md)."""
    name: str
    trigger: str
    prompt: str
    slack_webhook: str = ""
    model: str = ""           # "" = follow the instance default (TARES_AGENT_MODEL)
    slack_channel: str = ""   # workspace-bot channel id; wins over slack_webhook when both set
    webhook_url: str = ""     # write-back: findings + run metadata POSTed here
    webhook_token: str = ""   # optional bearer token for the write-back (a secret)
    webhook_key_label: str = ""   # the write-back reports this label's value as `key`, else the entity
    mcp_servers: list = dc_field(default_factory=list)   # registry names this agent may use
    max_rounds: int | None = None   # model rounds per run; None = default for the agent's shape
    budget_usd: float | None = None  # lifetime spend cap in USD; None = no budget
    enabled: bool = False


# A Tares agent is wired to its trigger through an ordinary subscription whose URL uses this
# scheme; the dispatcher runs it in-process instead of POSTing. Everything downstream (the roster,
# a trigger's woken-agents list, recent firings) then treats it identically to an external agent.
AGENT_URL_PREFIX = "tares://agent/"


def agent_url(name: str) -> str:
    return AGENT_URL_PREFIX + name


def agent_name_from_url(url: str) -> str | None:
    return url[len(AGENT_URL_PREFIX):] if url.startswith(AGENT_URL_PREFIX) else None


# Slack is the third sink, wired the same way: a subscription whose URL names a channel instead of
# an endpoint. The dispatcher posts it via chat.postMessage with the instance's bot token, so the
# channel is addressable without the operator holding a per-channel incoming-webhook URL.
SLACK_URL_PREFIX = "slack://channel/"

# Channel IDs are C/G/D + uppercase alphanumerics. A human-typed `#general` is accepted too —
# chat.postMessage still resolves names — but is normalized to the bare name.
_SLACK_ID = re.compile(r"^[CGD][A-Z0-9]{6,}$")
_SLACK_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")


def slack_url(channel: str) -> str:
    return SLACK_URL_PREFIX + channel.lstrip("#")


def slack_channel_from_url(url: str) -> str | None:
    """The channel a `slack://channel/<id>` subscription addresses, else None. Returns None for a
    malformed channel as well: `fire` must fall through to the webhook path only for URLs that are
    genuinely not Slack, so validation happens at subscribe time (see `validate_slack_channel`)."""
    if not url.startswith(SLACK_URL_PREFIX):
        return None
    chan = url[len(SLACK_URL_PREFIX):].strip().lstrip("#")
    return chan or None


def validate_slack_channel(channel: str) -> str:
    """Normalize and check a channel, raising ValueError with a usable message. Called on the
    subscribe path — a subscription that can never deliver is worse than a 400."""
    chan = (channel or "").strip().lstrip("#")
    if not chan:
        raise ValueError("slack:// subscription needs a channel, e.g. slack://channel/C0123456789")
    if _SLACK_ID.match(chan) or _SLACK_NAME.match(chan):
        return chan
    raise ValueError(
        f"{channel!r} is not a Slack channel; use the channel ID (C0123456789, from the channel's "
        "'Copy link') or its lowercase name")


@dataclass
class Catalog:
    sources: dict   # name -> SourceCfg
    views: dict     # name -> ViewCfg
    triggers: list  # [TriggerCfg]
    agents: list = dc_field(default_factory=list)   # [AgentCfg]


def _source_from_dict(s: dict) -> SourceCfg:
    from .connectors import source_type_for  # late import: connectors import config
    poll = str(s.get("poll", "5s"))
    # type is derived from the connector, not authored (any provided `type` is ignored)
    return SourceCfg(
        name=s["name"], type=source_type_for(s["connector"]), connector=s["connector"],
        poll_seconds=parse_duration(poll), config=s.get("config", {}) or {},
        poll=poll, paused=bool(s.get("paused", False)), ingest_key=s.get("ingest_key") or "",
    )


def _view_from_dict(v: dict) -> ViewCfg:
    return ViewCfg(
        name=v["name"], key_field=v["key_field"], sources=v["sources"],
        filters=v.get("filters", []) or [],
        created_by=v.get("created_by") or "human",
    )


def _trigger_from_dict(t: dict) -> TriggerCfg:
    c = t["condition"]
    return TriggerCfg(
        name=t["name"], view=t["view"],
        condition=Condition(
            aggregate=c["aggregate"], predicate=c["predicate"], window=c["window"],
            field=c.get("field"), group_by=c.get("group_by", ["key_value"]),
        ),
        emit=t.get("emit", {}) or {},
        cooldown_seconds=parse_duration(t.get("cooldown", "5m")),
        paused=bool(t.get("paused", False)),
    )


def _agent_from_dict(a: dict, enabled: bool = False) -> AgentCfg:
    return AgentCfg(
        name=a["name"], trigger=a["trigger"], prompt=a["prompt"],
        slack_webhook=a.get("slack_webhook") or "",
        model=a.get("model") or "",
        slack_channel=a.get("slack_channel") or "",
        webhook_url=a.get("webhook_url") or "",
        webhook_token=a.get("webhook_token") or "",
        webhook_key_label=a.get("webhook_key_label") or "",
        mcp_servers=list(a.get("mcp_servers") or []),
        max_rounds=(int(a["max_rounds"]) if a.get("max_rounds") not in (None, "") else None),
        budget_usd=(float(a["budget_usd"]) if a.get("budget_usd") not in (None, "") else None),
        enabled=bool(a.get("enabled", enabled)),
    )


def load_catalog(path) -> Catalog:
    raw = yaml.safe_load(Path(path).read_text())

    sources = {s["name"]: _source_from_dict(s) for s in raw.get("sources", [])}

    views = {v["name"]: _view_from_dict(v) for v in raw.get("views", [])}

    triggers = [_trigger_from_dict(t) for t in raw.get("triggers", [])]

    agents = [_agent_from_dict(a) for a in raw.get("agents", []) or []]

    return Catalog(sources=sources, views=views, triggers=triggers, agents=agents)


# ── DB-backed catalog (the YAML above becomes import/export) ─────────────────

def catalog_from_db(store) -> Catalog:
    """Build the typed Catalog from the store's catalog tables."""
    sources = {}
    for s in store.list_catalog_sources():
        sources[s["name"]] = _source_from_dict(s)

    views = {v["name"]: _view_from_dict(v) for v in store.list_catalog_views()}

    triggers = []
    for t in store.list_catalog_triggers():
        triggers.append(_trigger_from_dict(
            {"name": t["name"], "view": t["view"], "condition": t["condition"],
             "emit": t["emit"], "cooldown": t["cooldown"], "paused": t.get("paused", False)}))

    # enabled is derived: an agent is enabled exactly when it has a subscription to its trigger.
    enabled_urls = {s["url"] for s in store.all_subscriptions()}
    agents = [_agent_from_dict(a, enabled=agent_url(a["name"]) in enabled_urls)
              for a in store.list_catalog_agents()]

    return Catalog(sources=sources, views=views, triggers=triggers, agents=agents)


def validate_mcp_server_dict(m: dict) -> None:
    for field in ("name", "url"):
        if not str(m.get(field) or "").strip():
            raise CatalogError(f"mcp_server is missing required field {field!r}")
    if not _AGENT_NAME_RE.match(str(m["name"])):
        raise CatalogError(f"mcp_server name {m['name']!r} must be alphanumeric/_/-")
    url = str(m["url"]).strip()
    if not url.startswith("https://") and not url.startswith("http://"):
        raise CatalogError(f"mcp_server {m['name']!r}: url must be an http(s) URL "
                           "(stdio servers are not supported)")
    header = str(m.get("auth_header") or "").strip()
    if header and not re.fullmatch(r"[A-Za-z0-9-]+", header):
        raise CatalogError(f"mcp_server {m['name']!r}: auth_header must be a header name")
    headers = m.get("headers") or {}
    if not isinstance(headers, dict):
        raise CatalogError(f"mcp_server {m['name']!r}: headers must be a map of name to value")
    for k in headers:
        if not re.fullmatch(r"[A-Za-z0-9-]+", str(k).strip()):
            raise CatalogError(f"mcp_server {m['name']!r}: header {k!r} must be a header name")


def import_yaml_to_db(store, text: str, engine=None) -> dict:
    """Validate and write a YAML catalog into the store. Returns counts.

    `engine` (a projects.engine.Engine) is needed only when the document carries a `projects:`
    section; without one, projects in the document are an error rather than silently skipped."""
    return import_catalog_dict(store, yaml.safe_load(text) or {}, engine=engine)


def import_catalog_dict(store, raw: dict, engine=None) -> dict:
    """The dict form of import_yaml_to_db; also what the project engine applies a plan through,
    so a project's objects go through exactly the validation and writes a catalog import does."""
    sources = raw.get("sources", []) or []
    views = raw.get("views", []) or []
    triggers = raw.get("triggers", []) or []
    agents = raw.get("agents", []) or []
    mcp_servers = raw.get("mcp_servers", []) or []
    for m in mcp_servers:
        validate_mcp_server_dict(m)

    # validate the whole document before writing anything. Names already in the store count as
    # known (a merge import may add a view over existing sources).
    names = {s["name"] for s in sources} | {s["name"] for s in store.list_catalog_sources()}
    for s in sources:
        validate_source_dict(s)
    for v in views:
        validate_view_dict(v, names)
    view_names = {v["name"] for v in views} | {v["name"] for v in store.list_catalog_views()}
    for t in triggers:
        validate_trigger_dict(t, view_names)
    trigger_names = {t["name"] for t in triggers} | {t["name"] for t in store.list_catalog_triggers()}
    all_views = {v["name"]: v for v in store.list_catalog_views()}
    all_views.update({v["name"]: v for v in views})
    all_triggers = {t["name"]: t for t in store.list_catalog_triggers()}
    all_triggers.update({t["name"]: t for t in triggers})
    server_names = ({m["name"] for m in mcp_servers}
                    | {m["name"] for m in store.list_mcp_servers()})
    for a in agents:
        validate_agent_dict(a, trigger_names, all_triggers, all_views, server_names)

    from .connectors import normalize_config, source_type_for
    for s in sources:
        store.upsert_catalog_source(
            s["name"], source_type_for(s["connector"]), s["connector"], str(s.get("poll", "5s")),
            normalize_config(s["connector"], s.get("config", {}) or {}),
            bool(s.get("paused", False)), ingest_key=s.get("ingest_key"))
    for v in views:
        store.upsert_catalog_view(v["name"], v["key_field"], v["sources"],
                                  v.get("filters", []) or [],
                                  v.get("created_by") or "human")
    for t in triggers:
        store.upsert_catalog_trigger(
            t["name"], t["view"], t["condition"], t.get("emit", {}) or {},
            str(t.get("cooldown", "5m")))
        # upsert doesn't touch paused (it's toggled separately); reflect the document's state so a
        # paused trigger round-trips. Sources carry paused through upsert_catalog_source already.
        store.set_trigger_paused(t["name"], bool(t.get("paused", False)))
    for a in agents:
        store.upsert_catalog_agent(a["name"], a["trigger"], a["prompt"], a.get("slack_webhook"),
                                   a.get("model"), a.get("slack_channel"),
                                   a.get("webhook_url"), a.get("webhook_token"),
                                   a.get("mcp_servers"),
                                   (int(a["max_rounds"]) if a.get("max_rounds") not in (None, "")
                                    else None),
                                   (float(a["budget_usd"]) if a.get("budget_usd") not in (None, "")
                                    else None),
                                   webhook_key_label=a.get("webhook_key_label"))
        # enabled ⟺ a subscription to the trigger. Reflect the document's state so an enabled agent
        # round-trips: add the internal subscription if enabled, remove it if not.
        url = agent_url(a["name"])
        if bool(a.get("enabled", False)):
            if not store.subscription_by_url(url):
                store.add_subscription("sub_" + uuid.uuid4().hex[:8], a["trigger"], url,
                                       created_by="tares")
        else:
            store.remove_subscription_by_url(url)

    for m in mcp_servers:
        # blank-to-keep for the secret: a YAML without auth_value (an export without secrets
        # re-imported) must not wipe a stored credential
        existing = store.get_mcp_server(m["name"]) or {}
        auth_value = m.get("auth_value") if m.get("auth_value") else existing.get("auth_value")
        store.upsert_mcp_server(m["name"], str(m["url"]).strip(),
                                m.get("auth_header"), auth_value,
                                {str(k): str(v) for k, v in (m.get("headers") or {}).items()})

    # projects: {template, name, params}. Created (or, by name, updated) through the engine so
    # they own their objects like a console-created instance would.
    # `usecases:` with `recipe:` is the pre-1.14 form, read until two releases after 1.14
    if raw.get("projects") and raw.get("usecases"):
        raise CatalogError("this catalog has both a projects: and a usecases: section; keep one "
                           "(usecases: is the old name of projects:)")
    projects = raw.get("projects") or raw.get("usecases") or []
    if projects and engine is None:
        raise CatalogError("this catalog declares projects but no engine was given to apply them")
    for u in projects:
        if not u.get("template") and u.get("recipe"):
            u = {**u, "template": u["recipe"]}
        for field in ("template", "name"):
            if not u.get(field):
                raise CatalogError(f"project is missing required field {field!r}")
        params = dict(u.get("params") or {})
        if u.get("template") == "custom" and "objects" in u:
            # a hand-assembled project: `objects: [{kind, name}]` is its whole configuration
            params["objects"] = u["objects"]
        existing = store.get_project_by_name(u["name"])
        if existing is None:
            engine.create(u["template"], params, name=u["name"])
        else:
            engine.update(existing["id"], params)

    return {"sources": len(sources), "views": len(views), "triggers": len(triggers),
            "agents": len(agents), "mcp_servers": len(mcp_servers), "projects": len(projects),
            "names": {"sources": [s["name"] for s in sources],
                      "views": [v["name"] for v in views],
                      "triggers": [t["name"] for t in triggers],
                      "agents": [a["name"] for a in agents],
                      "mcp_servers": [m["name"] for m in mcp_servers],
                      "projects": [u["name"] for u in projects]}}


def export_db_to_yaml(store, sources: list | None = None, include_secrets: bool = False) -> str:
    """Serialize the catalog to portable YAML.

    `sources`: optional allow-list of source names to include (None = all). A partial export stays
    self-consistent — a view is kept only if ALL its sources are included, and a trigger only if its
    view is kept — so it re-imports cleanly.

    `include_secrets`: when False (default), connector secrets (github `token`, postgres `dsn`) are
    OMITTED — the export is safe to share/commit, and the operator re-enters them on the target
    (the source form's blank-to-keep handles this). When True, real secret values are emitted."""
    from .connectors import secret_field_names
    want = set(sources) if sources is not None else None

    src_out, kept_sources = [], set()
    for s in store.list_catalog_sources():
        if want is not None and s["name"] not in want:
            continue
        kept_sources.add(s["name"])
        config = s["config"]
        if not include_secrets and isinstance(config, dict):
            secrets = secret_field_names(s["connector"])
            if secrets:
                config = {k: v for k, v in config.items() if k not in secrets}
        src_out.append({"name": s["name"], "connector": s["connector"],  # type is derived
                        "poll": s["poll"], "config": config,
                        **({"paused": True} if s.get("paused") else {})})

    view_out, kept_views = [], set()
    for v in store.list_catalog_views():
        if want is not None and not set(v["sources"]).issubset(kept_sources):
            continue
        kept_views.add(v["name"])
        view_out.append({"name": v["name"], "key_field": v["key_field"], "sources": v["sources"],
                         **({"filters": v["filters"]} if v.get("filters") else {}),
                         **({"created_by": v["created_by"]} if v.get("created_by", "human") != "human" else {})})

    trig_out = [
        {"name": t["name"], "view": t["view"], "condition": t["condition"],
         "emit": t["emit"], "cooldown": t["cooldown"],
         **({"paused": True} if t.get("paused") else {})}
        for t in store.list_catalog_triggers()
        if want is None or t["view"] in kept_views
    ]
    kept_triggers = {t["name"] for t in trig_out}

    # A Tares agent follows its trigger. Its Slack webhook URL is a credential (anyone holding it
    # can post to the channel), so it's omitted unless secrets are explicitly requested — same rule
    # as connector secrets above; the operator re-enters it on the target. enabled is derived from
    # the presence of the agent's internal subscription.
    enabled_urls = {s["url"] for s in store.all_subscriptions()}
    # MCP connections: the URL and header name are configuration, the value is a credential —
    # same rule as every other secret here.
    # A `credential:github/<name>` reference is not a secret (the token lives in the credential),
    # so it is exported either way; extra headers are plain configuration.
    mcp_out = [
        {"name": m["name"], "url": m["url"],
         **({"auth_header": m["auth_header"]} if m.get("auth_header") else {}),
         **({"auth_value": m["auth_value"]}
            if m.get("auth_value") and (include_secrets
                                        or str(m["auth_value"]).startswith("credential:github/"))
            else {}),
         **({"headers": m["headers"]} if m.get("headers") else {})}
        for m in store.list_mcp_servers()
    ]

    agent_out = [
        {"name": a["name"], "trigger": a["trigger"], "prompt": a["prompt"],
         **({"model": a["model"]} if a.get("model") else {}),
         **({"slack_channel": a["slack_channel"]} if a.get("slack_channel") else {}),
         **({"webhook_url": a["webhook_url"]} if a.get("webhook_url") else {}),
         **({"webhook_key_label": a["webhook_key_label"]} if a.get("webhook_key_label") else {}),
         **({"mcp_servers": a["mcp_servers"]} if a.get("mcp_servers") else {}),
         **({"max_rounds": a["max_rounds"]} if a.get("max_rounds") else {}),
         **({"budget_usd": a["budget_usd"]} if a.get("budget_usd") else {}),
         **({"slack_webhook": a["slack_webhook"]}
            if include_secrets and a.get("slack_webhook") else {}),
         **({"webhook_token": a["webhook_token"]}
            if include_secrets and a.get("webhook_token") else {}),
         **({"enabled": True} if agent_url(a["name"]) in enabled_urls else {})}
        for a in store.list_catalog_agents()
        if a["trigger"] in kept_triggers
    ]

    doc = {"sources": src_out, "views": view_out, "triggers": trig_out}
    if agent_out:
        doc["agents"] = agent_out
    if mcp_out:
        doc["mcp_servers"] = mcp_out
    # Projects: template + name + params. Their objects are already in the sections above (they are
    # ordinary objects); on import the engine re-plans over them and re-claims ownership. Params
    # may hold references to credentials but never credential values, so this is safe to share.
    uc_out = []
    owner = {"source": {x["name"]: x.get("owned_by") for x in store.list_catalog_sources()},
             "view": {x["name"]: x.get("owned_by") for x in store.list_catalog_views()},
             "trigger": {x["name"]: x.get("owned_by") for x in store.list_catalog_triggers()},
             "agent": {x["name"]: x.get("owned_by") for x in store.list_catalog_agents()},
             "mcp_server": {x["name"]: x.get("owned_by") for x in store.list_mcp_servers()}}
    for u in store.list_projects():
        if u["template"] == "custom":
            # only objects that are in the sections above and still this project's: one deleted
            # by hand (or recreated under another project) would make the file fail to import
            objs = [o for o in (u["params"].get("objects") or [])
                    if o.get("name") in owner.get(o.get("kind"), {})
                    and owner[o["kind"]][o["name"]] in (None, u["id"])]
            if objs:   # a project with nothing left to list is not worth a failing import
                uc_out.append({"template": "custom", "name": u["name"], "objects": objs})
        else:
            uc_out.append({"template": u["template"], "name": u["name"], "params": u["params"]})
    if uc_out and want is None:
        doc["projects"] = uc_out
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


# ── validation (shared by the API and YAML import) ───────────────────────────

class CatalogError(ValueError):
    pass


_AGGREGATES = {"count", "sum", "avg", "max", "min", "any"}
_PREDICATE_SYMS = (">=", "<=", "==", ">", "<")


def _check_duration(value, label: str) -> None:
    try:
        parse_duration(value)
    except Exception:
        raise CatalogError(f"{label}: {value!r} is not a duration (use e.g. '5s', '15m', '1h')")


@lru_cache(maxsize=256)
def _compiled(pattern: str):
    return re.compile(pattern)


# Group-reference styles accepted in a label's `replace`: sed/Python (\1, \g<1>) and JS/PCRE
# ($1, ${1}). The $-forms are translated to Python before substituting.
_DOLLAR_GROUP = re.compile(r"\$\{(\d+)\}|\$(\d+)")
_GROUP_REF = re.compile(r"\$\{?\d|\\g?<?\d")


def _to_py_replace(replace: str) -> str:
    """Accept JS/PCRE `$1`/`${1}` (and `$$` → a literal `$`) in a replacement, alongside `\\1`."""
    protected = replace.replace("$$", "\x00")
    subbed = _DOLLAR_GROUP.sub(lambda m: f"\\g<{m.group(1) or m.group(2)}>", protected)
    return subbed.replace("\x00", "$")


def _normalize_value(spec: dict, raw: str) -> str | None:
    """Value normalization for one field label: regex substitution first, exact-alias map second
    (map keys are written against the cleaned form). Fail-open: any error keeps the raw value —
    the lossless payload always preserves the original, so normalization is never destructive.

    A `replace` that references a capture group (`\\1` or `$1`) is treated as an EXTRACTION: if the
    pattern doesn't match this value, the label doesn't apply, so return None (the label is dropped
    for this event). A `replace` with no group reference is a plain substitution/cleanup and keeps
    the original value on no-match, as before."""
    v = raw
    try:
        pattern = spec.get("pattern")
        if pattern:
            raw_replace = spec.get("replace", "")
            v, n = _compiled(pattern).subn(_to_py_replace(raw_replace), v)
            if n == 0 and _GROUP_REF.search(raw_replace):
                return None                       # extraction whose pattern didn't match → omit
        m = spec.get("map")
        if m:
            v = m.get(v, v)
    except Exception:
        return raw
    return v


def extract_labels(specs, context: dict | None = None) -> dict:
    """Build an event's label map from a source's `labels` config and a per-event context dict.

    Each spec is {name, const} (fixed value) or {name, field} (read context[field]). The context
    is whatever the connector exposes per event — a webhook payload, a Prometheus series' label
    set, named groups parsed from a log line. A label whose source value is absent is omitted.
    """
    ctx = context or {}
    out = {}
    for spec in specs or []:
        name = spec.get("name")
        if not name:
            continue
        if "const" in spec:
            tv = _coerce_type(spec, str(spec["const"]))
            if tv is not None:
                out[name] = tv
        elif "field" in spec:
            v = ctx.get(spec["field"]) if isinstance(ctx, dict) else None
            if v is None and isinstance(ctx, dict) and "." in str(spec["field"]):
                # dotted path into a nested context (the field profile shows e.g. metric.service;
                # promoting that name must extract it too)
                head, _, tail = str(spec["field"]).partition(".")
                sub = ctx.get(head)
                if isinstance(sub, dict):
                    v = sub.get(tail)
            if v is not None:
                nv = _normalize_value(spec, str(v))
                if nv is not None:               # extraction that didn't match drops the label
                    tv = _coerce_type(spec, nv)
                    if tv is not None:           # number-typed but not a number → drop the label
                        out[name] = tv
    return out


def _coerce_type(spec: dict, value: str):
    """Apply a label's declared `type`. Default is string. A `number` label stores an actual
    number (so it can be aggregated) — or None if the value isn't numeric, which drops the label
    for this event (numbers are chosen intentionally, never guessed)."""
    if spec.get("type") == "number":
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        return int(f) if f.is_integer() else f
    return str(value)


def _validate_labels(s: dict) -> None:
    specs = s.get("config", {}).get("labels") if isinstance(s.get("config"), dict) else None
    for spec in specs or []:
        if not isinstance(spec, dict) or not spec.get("name"):
            raise CatalogError(f"source {s['name']!r}: each label needs a name (got {spec!r})")
        if not re.match(r"^[A-Za-z0-9_]+$", str(spec["name"])):
            raise CatalogError(
                f"source {s['name']!r}: label name {spec['name']!r} must be alphanumeric/_")
        if ("const" in spec) == ("field" in spec):
            raise CatalogError(
                f"source {s['name']!r}: label {spec['name']!r} needs exactly one of "
                f"const (fixed value) or field (extract from the event)")
        if spec.get("type") not in (None, "string", "number"):
            raise CatalogError(
                f"source {s['name']!r}: label {spec['name']!r} type must be 'string' or 'number'")
        if spec.get("type") == "number" and spec.get("primary"):
            raise CatalogError(
                f"source {s['name']!r}: the primary (key) label {spec['name']!r} must be a string")
    if sum(1 for spec in (specs or []) if spec.get("primary")) > 1:
        raise CatalogError(f"source {s['name']!r}: at most one label can be primary (the key)")


def validate_source_dict(s: dict) -> None:
    from .connectors import REGISTRY  # late import: connectors import config
    # `type` is no longer required — it's derived from the connector.
    for field in ("name", "connector"):
        if not s.get(field):
            raise CatalogError(f"source is missing required field {field!r}")
    if s["connector"] not in REGISTRY:
        raise CatalogError(
            f"source {s['name']!r}: unknown connector {s['connector']!r} "
            f"(available: {', '.join(sorted(REGISTRY))})")
    _check_duration(s.get("poll", "5s"), f"source {s['name']!r} poll")
    floor = getattr(REGISTRY[s["connector"]], "MIN_POLL_SECONDS", None)
    if floor and parse_duration(s.get("poll", "5s")) < floor:
        raise CatalogError(f"source {s['name']!r}: {s['connector']} polls a third party API; "
                           f"poll must be at least {floor}s")
    if not isinstance(s.get("config", {}) or {}, dict):
        raise CatalogError(f"source {s['name']!r}: config must be a mapping")
    _validate_labels(s)


_FILTER_OPS = {"eq", "neq", "contains", "gt", "lt", "gte", "lte"}
_FILTER_FIELD_RE = re.compile(r"^[A-Za-z0-9_.]+$")   # dots: raw payload fields (OTLP et al.)


def validate_view_dict(v: dict, source_names: set) -> None:
    # key_field is optional now — labels make a single primary key non-essential; a view just
    # correlates its sources, and the query picks which label(s) to slice by.
    for field in ("name", "sources"):
        if not v.get(field):
            raise CatalogError(f"view is missing required field {field!r}")
    unknown = set(v["sources"]) - source_names
    if unknown:
        raise CatalogError(f"view {v['name']!r}: unknown sources {sorted(unknown)}")
    for f in v.get("filters", []) or []:
        if not isinstance(f, dict) or not all(k in f for k in ("field", "op", "value")):
            raise CatalogError(
                f"view {v['name']!r}: each filter needs field, op and value (got {f!r})")
        if not _FILTER_FIELD_RE.match(str(f["field"])):
            raise CatalogError(
                f"view {v['name']!r}: filter field {f['field']!r} must be alphanumeric/_/.")
        if f["op"] not in _FILTER_OPS:
            raise CatalogError(
                f"view {v['name']!r}: filter op must be one of {sorted(_FILTER_OPS)}")
        if f["op"] in ("gt", "lt", "gte", "lte"):
            try:
                float(f["value"])
            except (TypeError, ValueError):
                raise CatalogError(
                    f"view {v['name']!r}: filter op {f['op']!r} needs a numeric value")


def validate_trigger_dict(t: dict, view_names: set) -> None:
    for field in ("name", "view", "condition"):
        if not t.get(field):
            raise CatalogError(f"trigger is missing required field {field!r}")
    if t["view"] not in view_names:
        raise CatalogError(f"trigger {t['name']!r}: unknown view {t['view']!r}")
    c = t["condition"]
    if c.get("aggregate") not in _AGGREGATES:
        raise CatalogError(
            f"trigger {t['name']!r}: aggregate must be one of {sorted(_AGGREGATES)}")
    pred = str(c.get("predicate", "")).strip()
    for sym in _PREDICATE_SYMS:
        if pred.startswith(sym):
            try:
                float(pred[len(sym):].strip())
            except ValueError:
                raise CatalogError(
                    f"trigger {t['name']!r}: predicate {pred!r} has no numeric threshold")
            break
    else:
        raise CatalogError(
            f"trigger {t['name']!r}: predicate {pred!r} must start with one of "
            f"{', '.join(_PREDICATE_SYMS)}")
    _check_duration(c.get("window", "1m"), f"trigger {t['name']!r} window")
    _check_duration(t.get("cooldown", "5m"), f"trigger {t['name']!r} cooldown")
    if c.get("aggregate") != "count" and not c.get("field"):
        raise CatalogError(
            f"trigger {t['name']!r}: condition needs a field for aggregate {c['aggregate']!r}")


# The built-in source findings are written to. Named here (not in builtin_agents.py) because the
# loop guard below is a catalog-validation concern and config must not import the runtime.
FINDINGS_SOURCE = "findings"
# Overridable so the end-to-end test can point at a stub instead of the real API.
API_BASE = os.getenv(
    "ANTHROPIC_BASE_URL",
    os.getenv("TARES_ANTHROPIC_BASE", "https://api.anthropic.com"),
).rstrip("/")

_AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
MAX_PROMPT_CHARS = 8000
MAX_AGENT_ROUNDS = 24   # upper bound for a per-agent max_rounds (see builtin_agents.MAX_ROUNDS_LIMIT)


def validate_agent_dict(a: dict, trigger_names: set, triggers: dict | None = None,
                        views: dict | None = None,
                        mcp_server_names: set | None = None) -> None:
    for field in ("name", "trigger", "prompt"):
        if not str(a.get(field) or "").strip():
            raise CatalogError(f"agent is missing required field {field!r}")
    if not _AGENT_NAME_RE.match(str(a["name"])):
        raise CatalogError(f"agent name {a['name']!r} must be alphanumeric/_/-")
    if a["trigger"] not in trigger_names:
        raise CatalogError(f"agent {a['name']!r}: unknown trigger {a['trigger']!r}")
    if len(str(a["prompt"])) > MAX_PROMPT_CHARS:
        raise CatalogError(
            f"agent {a['name']!r}: prompt is longer than {MAX_PROMPT_CHARS} characters")
    hook = str(a.get("slack_webhook") or "").strip()
    if hook and not hook.startswith("https://"):
        raise CatalogError(f"agent {a['name']!r}: slack_webhook must be an https URL")
    model = str(a.get("model") or "").strip()
    # Loose on purpose: the console offers a curated list, but YAML/API users may name a model
    # newer than this build knows. The API rejects a wrong name at run time either way.
    if model and not re.fullmatch(r"claude-[a-z0-9.\-]+", model):
        raise CatalogError(f"agent {a['name']!r}: model must be a claude model id (or empty "
                           "for the instance default)")
    channel = str(a.get("slack_channel") or "").strip()
    if channel and not re.fullmatch(r"[A-Z][A-Z0-9]{4,}", channel):
        raise CatalogError(f"agent {a['name']!r}: slack_channel must be a Slack channel ID "
                           "(e.g. C0123456789), not a name")
    wurl = str(a.get("webhook_url") or "").strip()
    if wurl and not wurl.startswith("https://") and not wurl.startswith("http://"):
        raise CatalogError(f"agent {a['name']!r}: webhook_url must be an http(s) URL")
    wkl = str(a.get("webhook_key_label") or "").strip()
    if wkl and not re.fullmatch(r"[A-Za-z0-9_.-]+", wkl):
        raise CatalogError(f"agent {a['name']!r}: webhook_key_label must be a label name")
    servers = a.get("mcp_servers") or []
    if not isinstance(servers, list) or not all(isinstance(x, str) for x in servers):
        raise CatalogError(f"agent {a['name']!r}: mcp_servers must be a list of server names")
    if mcp_server_names is not None:
        for x in servers:
            if x not in mcp_server_names:
                raise CatalogError(f"agent {a['name']!r}: unknown mcp server {x!r} "
                                   "(add it to the MCP servers registry first)")
    mr = a.get("max_rounds")
    if mr not in (None, ""):
        try:
            mr = int(mr)
        except (TypeError, ValueError):
            raise CatalogError(f"agent {a['name']!r}: max_rounds must be a whole number")
        if not 1 <= mr <= MAX_AGENT_ROUNDS:
            raise CatalogError(f"agent {a['name']!r}: max_rounds must be between 1 and "
                               f"{MAX_AGENT_ROUNDS} (or empty for the default)")
    b = a.get("budget_usd")
    if b not in (None, ""):
        try:
            b = float(b)
        except (TypeError, ValueError):
            raise CatalogError(f"agent {a['name']!r}: budget_usd must be a number")
        if b <= 0:
            raise CatalogError(f"agent {a['name']!r}: budget_usd must be above zero "
                               "(or empty for no budget)")

    # Loop guard: a Tares agent writes a finding into the `findings` source. If its trigger
    # watches a view containing that source, the finding re-fires the trigger, which runs the agent
    # again — forever. Reject at definition time; there is no valid form of this.
    if triggers is not None and views is not None:
        trig = triggers.get(a["trigger"])
        view = views.get(trig.get("view")) if trig else None
        if view and FINDINGS_SOURCE in (view.get("sources") or []):
            raise CatalogError(
                f"agent {a['name']!r}: trigger {a['trigger']!r} watches view "
                f"{trig['view']!r}, which includes the {FINDINGS_SOURCE!r} source; an agent "
                f"cannot be woken by findings (it would fire itself forever)")
