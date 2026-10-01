"""Load and type the catalog YAML — the MVP stand-in for the design doc's Catalog Service.

Declares the sources (what to ingest and how), the triggers (conditions over sources that fire a
push) and the agents they wake. Coral-style declarative config, not a service.
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
class Condition:
    aggregate: str          # count | sum | avg | max | min | any
    predicate: str          # "> 1.0", ">= 100", "== 0"
    window: str             # "1m", "5m"
    field: str | None = None
    group_by: list = dc_field(default_factory=lambda: ["key_value"])
    # A schedule instead of a condition (TR-320): fire once every `every` seconds for the whole
    # trigger, handing over a summary of the window counted per `summary_by` label.
    every: float | None = None
    summary_by: list = dc_field(default_factory=list)


@dataclass
class TriggerCfg:
    """A condition over a set of sources. The trigger names the sources it watches and the
    filters that narrow them itself; there is no separate named query shape to point at."""
    name: str
    sources: list
    condition: Condition
    filters: list = dc_field(default_factory=list)   # [{field, op, value}] applied on eval + payload
    key_field: str = ""       # the entity label; "" = the primary label of the first source
    emit: dict = dc_field(default_factory=dict)
    cooldown_seconds: float = 300.0
    paused: bool = False
    project: str | None = None
    description: str = ""     # what wakes it in plain words; "" = said from the rule


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
    model: str = ""           # "" = the provider's default model (TARES_AGENT_MODEL on Anthropic)
    provider: str = ""        # a provider id from Settings; "" = the cell default (TR-302)
    slack_channel: str = ""   # workspace-bot channel id; wins over slack_webhook when both set
    webhook_url: str = ""     # write-back: findings + run metadata POSTed here
    webhook_token: str = ""   # optional bearer token for the write-back (a secret)
    webhook_key_label: str = ""   # the write-back reports this label's value as `key`, else the entity
    mcp_servers: list = dc_field(default_factory=list)   # registry names this agent may use
    max_rounds: int | None = None   # model rounds per run; None = default for the agent's shape
    budget_usd: float | None = None  # lifetime spend cap in USD; None = no budget
    daily_cap: int | None = None     # runs per rolling 24h; None = the instance-wide cap
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
    triggers: list  # [TriggerCfg]
    agents: list = dc_field(default_factory=list)   # [AgentCfg]


def primary_label(src) -> str | None:
    """The label a source marks primary (its entity key), for a SourceCfg or a catalog row."""
    cfg = src.config if hasattr(src, "config") else (src or {}).get("config")
    for spec in (cfg.get("labels") or []) if isinstance(cfg, dict) else []:
        if isinstance(spec, dict) and spec.get("primary"):
            return spec.get("name")
    return None


def trigger_entity_label(trig, sources: dict) -> str | None:
    """Which label names the entity a trigger fires for: its own key_field, else the primary label
    of its first source that declares one. `sources` maps name to SourceCfg (or a catalog row)."""
    key_field = trig.key_field if hasattr(trig, "key_field") else (trig or {}).get("key_field")
    if key_field:
        return key_field
    names = trig.sources if hasattr(trig, "sources") else (trig or {}).get("sources") or []
    for name in names:
        label = primary_label(sources.get(name)) if sources.get(name) is not None else None
        if label:
            return label
    return None


def _source_from_dict(s: dict) -> SourceCfg:
    from .connectors import source_type_for  # late import: connectors import config
    poll = str(s.get("poll", "5s"))
    # type is derived from the connector, not authored (any provided `type` is ignored)
    return SourceCfg(
        name=s["name"], type=source_type_for(s["connector"]), connector=s["connector"],
        poll_seconds=parse_duration(poll), config=s.get("config", {}) or {},
        poll=poll, paused=bool(s.get("paused", False)), ingest_key=s.get("ingest_key") or "",
    )


def _condition_from_dict(c: dict) -> Condition:
    if c.get("every"):
        # a schedule: the aggregate fields get the values that read sensibly anywhere they are
        # consulted (a count over the interval), but the clock, not them, decides the firing
        return Condition(aggregate="count", predicate="> 0", window=str(c["every"]),
                         every=parse_duration(c["every"]),
                         summary_by=[str(x) for x in (c.get("summary_by") or [])])
    return Condition(aggregate=c["aggregate"], predicate=c["predicate"], window=c["window"],
                     field=c.get("field"), group_by=c.get("group_by", ["key_value"]))


def _trigger_from_dict(t: dict) -> TriggerCfg:
    return TriggerCfg(
        name=t["name"], sources=list(t.get("sources") or []),
        condition=_condition_from_dict(t["condition"]),
        filters=list(t.get("filters") or []),
        key_field=t.get("key_field") or "",
        emit=t.get("emit", {}) or {},
        cooldown_seconds=parse_duration(t.get("cooldown", "5m")),
        paused=bool(t.get("paused", False)),
        project=t.get("project") or t.get("owned_by") or None,
        description=str(t.get("description") or "").strip().rstrip(".").strip(),
    )


def _agent_from_dict(a: dict, enabled: bool = False) -> AgentCfg:
    return AgentCfg(
        name=a["name"], trigger=a["trigger"], prompt=a["prompt"],
        slack_webhook=a.get("slack_webhook") or "",
        model=a.get("model") or "",
        provider=a.get("provider") or "",
        slack_channel=a.get("slack_channel") or "",
        webhook_url=a.get("webhook_url") or "",
        webhook_token=a.get("webhook_token") or "",
        webhook_key_label=a.get("webhook_key_label") or "",
        mcp_servers=list(a.get("mcp_servers") or []),
        max_rounds=(int(a["max_rounds"]) if a.get("max_rounds") not in (None, "") else None),
        budget_usd=(float(a["budget_usd"]) if a.get("budget_usd") not in (None, "") else None),
        daily_cap=(int(a["daily_cap"]) if a.get("daily_cap") not in (None, "") else None),
        enabled=bool(a.get("enabled", enabled)),
    )


def load_catalog(path) -> Catalog:
    raw = fold_legacy_views(yaml.safe_load(Path(path).read_text()) or {})

    sources = {s["name"]: _source_from_dict(s) for s in raw.get("sources", [])}

    triggers = [_trigger_from_dict(t) for t in raw.get("triggers", [])]

    agents = [_agent_from_dict(a) for a in raw.get("agents", []) or []]

    return Catalog(sources=sources, triggers=triggers, agents=agents)


VIEWS_REMOVED = "views were removed: give the trigger `sources` (and `filters`)"


def fold_legacy_views(raw: dict) -> dict:
    """A catalog written while views existed names a view on each trigger and declares the views in a
    `views:` section. Fold each view's sources, filters and key_field onto the triggers that name
    it and drop the section, so an old export still imports. A custom project's `view` objects are
    dropped with it. Returns a new dict; the input is left alone."""
    views = {v["name"]: v for v in (raw.get("views") or []) if isinstance(v, dict) and v.get("name")}
    out = {k: v for k, v in raw.items() if k != "views"}
    triggers = []
    for t in raw.get("triggers") or []:
        if isinstance(t, dict) and t.get("view") and not t.get("sources"):
            v = views.get(t["view"])
            if v is None:
                raise CatalogError(f"trigger {t.get('name')!r}: names view {t['view']!r}, which "
                                   "this catalog does not declare; " + VIEWS_REMOVED)
            t = {**{k: x for k, x in t.items() if k != "view"},
                 "sources": list(v.get("sources") or []),
                 **({"filters": list(v["filters"])} if v.get("filters") else {}),
                 **({"key_field": v["key_field"]} if v.get("key_field") else {})}
        elif isinstance(t, dict) and "view" in t:
            t = {k: x for k, x in t.items() if k != "view"}
        if isinstance(t, dict) and isinstance(t.get("emit"), dict) and "attach_view" in t["emit"]:
            # never read: the payload always carries the timeline
            t = {**t, "emit": {k: x for k, x in t["emit"].items() if k != "attach_view"}}
        triggers.append(t)
    if "triggers" in raw:
        out["triggers"] = triggers
    for section in ("projects", "usecases"):
        if raw.get(section):
            out[section] = [
                {**u, "objects": [o for o in u["objects"]
                                  if not (isinstance(o, dict) and o.get("kind") == "view")]}
                if isinstance(u, dict) and isinstance(u.get("objects"), list) else u
                for u in raw[section]]
    return out


# ── DB-backed catalog (the YAML above becomes import/export) ─────────────────

def catalog_from_db(store) -> Catalog:
    """Build the typed Catalog from the store's catalog tables."""
    sources = {}
    for s in store.list_catalog_sources():
        sources[s["name"]] = _source_from_dict(s)

    triggers = [_trigger_from_dict(t) for t in store.list_catalog_triggers()]

    # enabled is derived: an agent is on when some project's wiring wakes it (P-TR-216)
    on = {w["agent"] for w in store.list_wakes() if w["enabled"]}
    agents = [_agent_from_dict(a, enabled=a["name"] in on) for a in store.list_catalog_agents()]

    return Catalog(sources=sources, triggers=triggers, agents=agents)


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


def import_catalog_dict(store, raw: dict, engine=None, assign: bool = True) -> dict:
    """The dict form of import_yaml_to_db; also what the project engine applies a plan through,
    so a project's objects go through exactly the validation and writes a catalog import does.

    `assign`: place the imported objects in projects (the object's `project`, else the default
    project). The engine passes False: it owns what it applies and places it itself."""
    raw = fold_legacy_views(raw or {})
    sources = raw.get("sources", []) or []
    triggers = raw.get("triggers", []) or []
    agents = raw.get("agents", []) or []
    mcp_servers = raw.get("mcp_servers", []) or []
    for m in mcp_servers:
        validate_mcp_server_dict(m)
    skills = _validated_skills(raw.get("skills", []) or [])

    # validate the whole document before writing anything. Names already in the store count as
    # known (a merge import may add a trigger over existing sources).
    names = {s["name"] for s in sources} | {s["name"] for s in store.list_catalog_sources()}
    for s in sources:
        validate_source_dict(s)
    for t in triggers:
        validate_trigger_dict(t, names)
    trigger_names = {t["name"] for t in triggers} | {t["name"] for t in store.list_catalog_triggers()}
    all_triggers = {t["name"]: t for t in store.list_catalog_triggers()}
    all_triggers.update({t["name"]: t for t in triggers})
    server_names = ({m["name"] for m in mcp_servers}
                    | {m["name"] for m in store.list_mcp_servers()})
    agent_names = {a["name"] for a in agents} | {a["name"] for a in store.list_catalog_agents()}
    for a in agents:
        validate_agent_dict(a, trigger_names, all_triggers, server_names)
        check_handoff_targets(a["name"], normalize_handoffs(a["name"], a.get("handoffs")),
                              {n: None for n in agent_names})

    # projects: {template, name, params}. `usecases:` with `recipe:` is the pre-1.14 form, read
    # until two releases after 1.14
    if raw.get("projects") and raw.get("usecases"):
        raise CatalogError("this catalog has both a projects: and a usecases: section; keep one "
                           "(usecases: is the old name of projects:)")
    projects = raw.get("projects") or raw.get("usecases") or []
    if projects and engine is None:
        raise CatalogError("this catalog declares projects but no engine was given to apply them")
    declared = {str(u.get("name") or "") for u in projects}
    if assign:
        # a project reference must name a project that exists or that this document declares:
        # a typo would otherwise drop the object silently into the default project
        for kind, objs in (("source", sources), ("trigger", triggers), ("agent", agents),
                           ("mcp_server", mcp_servers)):
            for o in objs:
                for ref in _project_refs(o):
                    if _resolve_project(store, ref) is None and ref not in declared:
                        raise CatalogError(f"{kind} {o['name']!r}: unknown project {ref!r}")
        for sk in skills:
            ref = sk["project"]
            if ref and _resolve_project(store, ref) is None and ref not in declared:
                raise CatalogError(f"skill {sk['name']!r}: unknown project {ref!r}")

    from .connectors import normalize_config, source_type_for
    for s in sources:
        store.upsert_catalog_source(
            s["name"], source_type_for(s["connector"]), s["connector"], str(s.get("poll", "5s")),
            normalize_config(s["connector"], s.get("config", {}) or {}),
            bool(s.get("paused", False)), ingest_key=s.get("ingest_key"))
    for t in triggers:
        store.upsert_catalog_trigger(
            t["name"], list(t["sources"]), t["condition"], t.get("emit", {}) or {},
            str(t.get("cooldown", "5m")), filters=list(t.get("filters") or []),
            key_field=t.get("key_field") or "",
            description=(normalize_trigger_description(t["description"])
                         if "description" in t else None))
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
                                   webhook_key_label=a.get("webhook_key_label"),
                                   provider=a.get("provider"),
                                   daily_cap=(int(a["daily_cap"])
                                              if a.get("daily_cap") not in (None, "") else None),
                                   # absent keeps what is stored (see upsert_catalog_agent)
                                   handoffs=(normalize_handoffs(a["name"], a["handoffs"])
                                             if "handoffs" in a else None))
        # on/off belongs to the wiring of the project the agent is in; applied once it is placed
        # (below, or by the engine that applies a template)
        _turn_on_where_placed(store, a)

    for m in mcp_servers:
        # blank-to-keep for the secret: a YAML without auth_value (an export without secrets
        # re-imported) must not wipe a stored credential
        existing = store.get_mcp_server(m["name"]) or {}
        auth_value = m.get("auth_value") if m.get("auth_value") else existing.get("auth_value")
        store.upsert_mcp_server(m["name"], str(m["url"]).strip(),
                                m.get("auth_header"), auth_value,
                                {str(k): str(v) for k, v in (m.get("headers") or {}).items()})

    placed = set()
    if assign:
        # objects naming a project that already exists go there before the projects below are
        # applied, so a template re-plan finds them where the document put them
        placed = _place_imported(store, sources, triggers, agents, mcp_servers, only_existing=True)

    # Projects are created (or, by name, updated) through the engine so they own their objects
    # like a console-created instance would.
    for u in projects:
        if not u.get("template") and u.get("recipe"):
            u = {**u, "template": u["recipe"]}
        for field in ("template", "name"):
            if not u.get(field):
                raise CatalogError(f"project is missing required field {field!r}")
        if u.get("template") == "default":
            # every cell has its own default project (its objects carry `project`); only its
            # goal comes along
            if "goal" in u:
                engine.set_goal(store.default_project_id(), u.get("goal"))
            continue
        params = dict(u.get("params") or {})
        if u.get("template") == "custom" and "objects" in u:
            # a hand-assembled project: `objects: [{kind, name}]` is its whole configuration
            params["objects"] = u["objects"]
        existing = store.get_project_by_name(u["name"])
        if existing is None:
            engine.create(u["template"], params, name=u["name"], goal=u.get("goal"))
        else:
            engine.update(existing["id"], params)
            if "goal" in u:
                engine.set_goal(existing["id"], u.get("goal"))

    if assign:
        _place_imported(store, sources, triggers, agents, mcp_servers, only_existing=False,
                        skip=placed)
        # everything the document left without a project lands in the default project, and a
        # trigger's sources join its project (the same pass the store runs at every start)
        store.normalize_projects()
        for a in agents:
            _turn_on_where_placed(store, a)
        # the wiring each project holds, as exported; it wins over the agents' own fields
        _import_wiring(store, raw.get("wiring") or [])
        # skills last: the projects they name exist now, and a skill in the document wins over
        # the one a template planned under the same name
        for sk in skills:
            uid = _resolve_project(store, sk["project"]) if sk["project"] \
                else store.default_project_id()
            store.upsert_skill(uid, sk["name"], sk["description"], sk["body"])
            for ref in sk.get("used_by") or []:
                other = _resolve_project(store, ref)
                if other is not None:
                    store.use_skill(other, sk["name"])

    return {"sources": len(sources), "triggers": len(triggers),
            "agents": len(agents), "mcp_servers": len(mcp_servers), "projects": len(projects),
            "skills": len(skills),
            "names": {"sources": [s["name"] for s in sources],
                      "triggers": [t["name"] for t in triggers],
                      "agents": [a["name"] for a in agents],
                      "mcp_servers": [m["name"] for m in mcp_servers],
                      "projects": [u["name"] for u in projects],
                      "skills": [s["name"] for s in skills]}}


DEFAULT_PROJECT_NAME = "Default"


def _validated_skills(raw) -> list[dict]:
    """The `skills:` section: [{project, name, description, body}], each checked like the API
    checks one (tares/skills.py). `project` is a name or id; absent means the default project."""
    from .skills import SkillError, validate as validate_skill
    if not isinstance(raw, list):
        raise CatalogError("skills must be a list of {project, name, description, body}")
    out, seen = [], set()
    for sk in raw:
        if not isinstance(sk, dict):
            raise CatalogError(f"each skill must be a mapping, got {sk!r}")
        try:
            name, description, body = validate_skill(sk.get("name"), sk.get("description"),
                                                     sk.get("body"))
        except SkillError as e:
            raise CatalogError(str(e)) from e
        project = str(sk.get("project") or "").strip()
        if (project, name) in seen:
            raise CatalogError(f"skill {name!r} appears twice for project {project or 'Default'!r}")
        seen.add((project, name))
        used_by = [str(x).strip() for x in sk.get("used_by") or [] if str(x).strip()]
        out.append({"project": project, "name": name, "description": description, "body": body,
                    "used_by": used_by})
    return out


def _project_refs(o: dict) -> list[str]:
    """The project names or ids an imported object points at: `project` for a trigger, agent or
    MCP server, `projects` (memberships) for a source."""
    out = []
    if o.get("project"):
        out.append(str(o["project"]))
    for ref in o.get("projects") or []:
        if ref:
            out.append(str(ref))
    return out


def _resolve_project(store, ref: str) -> str | None:
    """A project id from an id or a name. The default project answers to its name on every cell,
    so an export from one cell names the right project on another."""
    if not ref:
        return None
    if ref == DEFAULT_PROJECT_NAME:
        return store.default_project_id()
    if store.get_project(ref) is not None:
        return ref
    p = store.get_project_by_name(ref)
    return p["id"] if p else None


def _turn_on_where_placed(store, a: dict) -> None:
    """An imported agent's `enabled`, on its wiring in the project that made it (nothing yet
    when it is not placed)."""
    row = store.get_catalog_agent(a["name"])
    if row and row.get("owned_by"):
        store.set_agent_enabled(a["name"], bool(a.get("enabled", False)), project=row["owned_by"])


def _import_wiring(store, wiring: list) -> None:
    """`wiring:` from an export: [{project, wake, agent, enabled}] and [{project, agent, verdict,
    to, cooldown}], by project name. A row naming a project or part that is not here is skipped."""
    by_name = {p["name"]: p["id"] for p in store.list_projects()}
    hands: dict[tuple, list] = {}
    for w in wiring:
        if not isinstance(w, dict):
            continue
        uid = by_name.get(str(w.get("project") or ""))
        if uid is None:
            continue
        if w.get("wake") and w.get("agent"):
            store.set_wake(uid, str(w["wake"]), str(w["agent"]), bool(w.get("enabled", False)))
        elif w.get("agent") and w.get("verdict") and w.get("to"):
            hands.setdefault((uid, str(w["agent"])), []).append(
                {"verdict": str(w["verdict"]), "agent": str(w["to"]),
                 **({"cooldown": str(w["cooldown"])} if w.get("cooldown") else {})})
    for (uid, agent), hs in hands.items():
        store.set_handoffs(uid, agent, hs)


def _place_imported(store, sources, triggers, agents, mcp_servers, only_existing: bool,
                    skip: set | None = None) -> set:
    """Put each imported object that names a project into it. Returns the (kind, name) placed."""
    placed = set()
    skip = skip or set()
    for kind, objs in (("source", sources), ("trigger", triggers), ("agent", agents),
                       ("mcp_server", mcp_servers)):
        for o in objs:
            if (kind, o["name"]) in skip:
                continue
            for ref in _project_refs(o):
                uid = _resolve_project(store, ref)
                if uid is None:
                    if only_existing:
                        continue
                    raise CatalogError(f"{kind} {o['name']!r}: unknown project {ref!r}")
                store.put_in_project(kind, o["name"], uid)
                placed.add((kind, o["name"]))
    return placed


def export_db_to_yaml(store, sources: list | None = None, include_secrets: bool = False) -> str:
    """Serialize the catalog to portable YAML.

    `sources`: optional allow-list of source names to include (None = all). A partial export stays
    self-consistent: a trigger is kept only if ALL its sources are included, and an agent only if
    its trigger is kept, so it re-imports cleanly.

    Projects are named, not referenced by id, so the file imports into another cell: `project` on
    a trigger, agent and MCP server, `projects` (memberships) on a source.

    `include_secrets`: when False (default), connector secrets (github `token`, postgres `dsn`) are
    OMITTED — the export is safe to share/commit, and the operator re-enters them on the target
    (the source form's blank-to-keep handles this). When True, real secret values are emitted."""
    from .connectors import secret_field_names
    want = set(sources) if sources is not None else None
    project_names = {p["id"]: p["name"] for p in store.list_projects()}
    members = store.source_memberships()

    def _pname(uid):
        return project_names.get(uid) if uid else None

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
        in_projects = [n for n in (_pname(u) for u in members.get(s["name"], [])) if n]
        src_out.append({"name": s["name"], "connector": s["connector"],  # type is derived
                        "poll": s["poll"], "config": config,
                        **({"paused": True} if s.get("paused") else {}),
                        **({"projects": in_projects} if in_projects else {})})

    trig_out = []
    for t in store.list_catalog_triggers():
        if want is not None and not set(t["sources"]).issubset(kept_sources):
            continue
        if not t["sources"]:
            # left by the upgrade when its view was already gone: it could not be imported
            continue
        trig_out.append({"name": t["name"],
                         **({"project": _pname(t.get("owned_by"))}
                            if _pname(t.get("owned_by")) else {}),
                         "sources": t["sources"],
                         **({"filters": t["filters"]} if t.get("filters") else {}),
                         **({"key_field": t["key_field"]} if t.get("key_field") else {}),
                         **({"description": t["description"]} if t.get("description") else {}),
                         "condition": t["condition"], "emit": t["emit"], "cooldown": t["cooldown"],
                         **({"paused": True} if t.get("paused") else {})})
    kept_triggers = {t["name"] for t in trig_out}

    # A Tares agent follows its trigger. Its Slack webhook URL is a credential (anyone holding it
    # can post to the channel), so it's omitted unless secrets are explicitly requested — same rule
    # as connector secrets above; the operator re-enters it on the target. enabled is derived from
    # the presence of the agent's internal subscription.
    # MCP connections: the URL and header name are configuration, the value is a credential —
    # same rule as every other secret here.
    # A `credential:github/<name>` reference is not a secret (the token lives in the credential),
    # so it is exported either way; extra headers are plain configuration.
    mcp_out = [
        {"name": m["name"],
         **({"project": _pname(m.get("owned_by"))} if _pname(m.get("owned_by")) else {}),
         "url": m["url"],
         **({"auth_header": m["auth_header"]} if m.get("auth_header") else {}),
         **({"auth_value": m["auth_value"]}
            if m.get("auth_value") and (include_secrets
                                        or str(m["auth_value"]).startswith("credential:github/"))
            else {}),
         **({"headers": m["headers"]} if m.get("headers") else {})}
        for m in store.list_mcp_servers()
    ]

    agent_out = [
        {"name": a["name"],
         **({"project": _pname(a.get("owned_by"))} if _pname(a.get("owned_by")) else {}),
         "trigger": a["trigger"], "prompt": a["prompt"],
         **({"model": a["model"]} if a.get("model") else {}),
         **({"provider": a["provider"]} if a.get("provider") else {}),
         **({"slack_channel": a["slack_channel"]} if a.get("slack_channel") else {}),
         **({"webhook_url": a["webhook_url"]} if a.get("webhook_url") else {}),
         **({"webhook_key_label": a["webhook_key_label"]} if a.get("webhook_key_label") else {}),
         **({"mcp_servers": a["mcp_servers"]} if a.get("mcp_servers") else {}),
         **({"max_rounds": a["max_rounds"]} if a.get("max_rounds") else {}),
         **({"budget_usd": a["budget_usd"]} if a.get("budget_usd") else {}),
         **({"daily_cap": a["daily_cap"]} if a.get("daily_cap") else {}),
         **({"handoffs": a["handoffs"]} if a.get("handoffs") else {}),
         **({"slack_webhook": a["slack_webhook"]}
            if include_secrets and a.get("slack_webhook") else {}),
         **({"webhook_token": a["webhook_token"]}
            if include_secrets and a.get("webhook_token") else {}),
         **({"enabled": True} if a.get("enabled") else {})}
        for a in store.list_catalog_agents()
        if a["trigger"] in kept_triggers
    ]

    doc = {"sources": src_out, "triggers": trig_out}
    if agent_out:
        doc["agents"] = agent_out
    if mcp_out:
        doc["mcp_servers"] = mcp_out
    # Projects: template + name + params. Their objects are already in the sections above (they are
    # ordinary objects); on import the engine re-plans over them and re-claims ownership. Params
    # may hold references to credentials but never credential values, so this is safe to share.
    # The default project is every cell's own; its objects say `project: Default` above.
    uc_out = []
    owner = {"source": {x["name"]: x.get("owned_by") for x in store.list_catalog_sources()},
             "trigger": {x["name"]: x.get("owned_by") for x in store.list_catalog_triggers()},
             "agent": {x["name"]: x.get("owned_by") for x in store.list_catalog_agents()},
             "mcp_server": {x["name"]: x.get("owned_by") for x in store.list_mcp_servers()}}
    for u in store.list_projects():
        if u.get("status") == "draft":   # still being planned: nothing of it exists yet
            continue
        goal = {"goal": u["goal"]} if u.get("goal") else {}
        if u["template"] == "default":
            if goal:   # only its goal: the default project's objects say `project` above
                uc_out.append({"template": "default", "name": u["name"], **goal})
            continue
        if u["template"] == "custom":
            # only objects that are in the sections above and still this project's: one deleted
            # by hand (or recreated under another project) would make the file fail to import.
            # A source is shared, so membership, not its creator, says whether it is still here.
            # membership, not the maker, says whether a part is still here (P-TR-216)
            objs = [o for o in (u["params"].get("objects") or [])
                    if o.get("name") in owner.get(o.get("kind"), {})
                    and u["id"] in store.projects_using(o["kind"], o["name"])]
            uc_out.append({"template": "custom", "name": u["name"], **goal, "objects": objs})
        else:
            uc_out.append({"template": u["template"], "name": u["name"], **goal,
                           "params": u["params"]})
    if uc_out and want is None:
        doc["projects"] = uc_out
    # Skills belong to a project, not to a source: a full export carries them, a partial one
    # (a subset of sources) does not.
    # a skill once, by the project that made it (else its first user), with the others using it
    skill_out = []
    for sk in store.list_all_skills():
        users = [n for n in (_pname(p) for p in sk.get("projects") or []) if n]
        home = _pname(sk["project"]) if _pname(sk["project"]) in users else (users[0] if users else None)
        if home is None:
            continue
        others = [n for n in users if n != home]
        skill_out.append({"project": home, "name": sk["name"], "description": sk["description"],
                          "body": sk["body"], **({"used_by": others} if others else {})})
    if skill_out and want is None:
        doc["skills"] = skill_out
    # the wiring every project holds (P-TR-216): what wakes which agent, who digs in on what
    if want is None:
        wiring_out = [{"project": _pname(w["project"]), "wake": w["trigger"], "agent": w["agent"],
                       **({"enabled": True} if w["enabled"] else {})}
                      for w in store.list_wakes() if _pname(w["project"])]
        wiring_out += [{"project": _pname(h["project"]), "agent": h["from_agent"],
                        "verdict": h["verdict"], "to": h["agent"],
                        **({"cooldown": h["cooldown"]} if h.get("cooldown") else {})}
                       for h in store.list_handoffs() if _pname(h["project"])]
        if wiring_out:
            doc["wiring"] = wiring_out
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


# ── validation (shared by the API and YAML import) ───────────────────────────

class CatalogError(ValueError):
    pass


_AGGREGATES = {"count", "sum", "avg", "max", "min", "any"}
SCHEDULE_MIN_SECONDS = 60.0
SCHEDULE_MAX_SUMMARY_LABELS = 5
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


def validate_filters(filters, owner: str) -> None:
    """The {field, op, value} filters a trigger narrows its sources with. `owner` prefixes the
    error ("trigger 'x'")."""
    if not isinstance(filters or [], list):
        raise CatalogError(f"{owner}: filters must be a list of {{field, op, value}}")
    for f in filters or []:
        if not isinstance(f, dict) or not all(k in f for k in ("field", "op", "value")):
            raise CatalogError(
                f"{owner}: each filter needs field, op and value (got {f!r})")
        if not _FILTER_FIELD_RE.match(str(f["field"])):
            raise CatalogError(
                f"{owner}: filter field {f['field']!r} must be alphanumeric/_/.")
        if f["op"] not in _FILTER_OPS:
            raise CatalogError(
                f"{owner}: filter op must be one of {sorted(_FILTER_OPS)}")
        if f["op"] in ("gt", "lt", "gte", "lte"):
            try:
                float(f["value"])
            except (TypeError, ValueError):
                raise CatalogError(
                    f"{owner}: filter op {f['op']!r} needs a numeric value")


MAX_TRIGGER_DESCRIPTION = 160


def normalize_trigger_description(value, owner: str = "trigger") -> str:
    """A trigger's plain-words description: one line of at most 160 characters, phrased to
    follow "When" ("an alert fires in the demo service"). "" when not given. A trailing full
    stop is dropped, since the sentence around it adds its own."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise CatalogError(f"{owner}: description must be text")
    text = value.strip()
    if "\n" in text or "\r" in text:
        raise CatalogError(f"{owner}: description must be one line")
    if "—" in text:
        raise CatalogError(f"{owner}: description must not use an em dash; use a comma")
    text = text.rstrip(".").strip()
    if len(text) > MAX_TRIGGER_DESCRIPTION:
        raise CatalogError(f"{owner}: description is longer than "
                           f"{MAX_TRIGGER_DESCRIPTION} characters")
    return text


def validate_trigger_dict(t: dict, source_names: set) -> None:
    if "view" in t and t.get("view") not in (None, ""):
        raise CatalogError(VIEWS_REMOVED)
    for field in ("name", "condition"):
        if not t.get(field):
            raise CatalogError(f"trigger is missing required field {field!r}")
    normalize_trigger_description(t.get("description"), f"trigger {t['name']!r}")
    srcs = t.get("sources")
    if not isinstance(srcs, list) or not srcs or not all(isinstance(x, str) and x for x in srcs):
        raise CatalogError(f"trigger {t['name']!r}: sources must name at least one source")
    unknown = set(srcs) - set(source_names)
    if unknown:
        raise CatalogError(f"trigger {t['name']!r}: unknown sources {sorted(unknown)}")
    validate_filters(t.get("filters"), f"trigger {t['name']!r}")
    kf = t.get("key_field") or ""
    if kf and not re.fullmatch(r"[A-Za-z0-9_.]+", str(kf)):
        raise CatalogError(f"trigger {t['name']!r}: key_field must be a label name")
    c = t["condition"]
    if c.get("every"):
        _check_duration(c["every"], f"trigger {t['name']!r} every")
        if parse_duration(c["every"]) < SCHEDULE_MIN_SECONDS:
            raise CatalogError(f"trigger {t['name']!r}: every must be at least "
                               f"{int(SCHEDULE_MIN_SECONDS)}s")
        by = c.get("summary_by") or []
        if not isinstance(by, list) or len(by) > SCHEDULE_MAX_SUMMARY_LABELS or not all(
                isinstance(x, str) and re.match(r"^[A-Za-z0-9_.]+$", x) for x in by):
            raise CatalogError(f"trigger {t['name']!r}: summary_by must be a list of up to "
                               f"{SCHEDULE_MAX_SUMMARY_LABELS} label names")
        return
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


MAX_HANDOFFS = 10
HANDOFF_COOLDOWN = "30m"
_VERDICT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def normalize_handoffs(name: str, raw) -> list[dict]:
    """An agent's `handoffs` (TR-334) as [{verdict, agent, cooldown}], checked for shape: at most
    ten, each a one-word verdict (lowercased), another agent's name, and a cooldown duration
    (default 30m). Whether the target exists and is in the same project is the caller's check
    (see check_handoff_targets): it needs the store."""
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise CatalogError(f"agent {name!r}: handoffs must be a list of {{verdict, agent, cooldown}}")
    if len(raw) > MAX_HANDOFFS:
        raise CatalogError(f"agent {name!r}: at most {MAX_HANDOFFS} handoffs")
    out, seen = [], set()
    for h in raw:
        if not isinstance(h, dict):
            raise CatalogError(f"agent {name!r}: each handoff is a mapping of verdict, agent "
                               "and cooldown")
        verdict = str(h.get("verdict") or "").strip().lower()
        target = str(h.get("agent") or "").strip()
        cooldown = str(h.get("cooldown") or "").strip() or HANDOFF_COOLDOWN
        if not verdict or not _VERDICT_RE.match(verdict):
            raise CatalogError(f"agent {name!r}: a handoff verdict is one word, such as "
                               f"investigate (got {h.get('verdict')!r})")
        if not target:
            raise CatalogError(f"agent {name!r}: the handoff on verdict {verdict!r} names no agent")
        if target == name:
            raise CatalogError(f"agent {name!r}: an agent cannot hand off to itself")
        try:
            if parse_duration(cooldown) < 0:
                raise ValueError
        except (KeyError, ValueError, IndexError):
            raise CatalogError(f"agent {name!r}: handoff cooldown {cooldown!r} must be a "
                               "duration such as 30m or 2h")
        if (verdict, target) in seen:
            raise CatalogError(f"agent {name!r}: the handoff to {target!r} on verdict "
                               f"{verdict!r} is listed twice")
        seen.add((verdict, target))
        out.append({"verdict": verdict, "agent": target, "cooldown": cooldown})
    return out


def check_handoff_targets(name: str, handoffs: list[dict], projects: dict,
                          own_project: str | None = None) -> None:
    """Each handoff names an agent that exists and, when projects are known, is in the handing
    agent's project. `projects` is {agent name: project id or None}."""
    for h in handoffs:
        if h["agent"] not in projects:
            raise CatalogError(f"agent {name!r}: the handoff on verdict {h['verdict']!r} names "
                               f"an unknown agent {h['agent']!r}")
        # any agent on the cell may take a handoff (P-TR-216: parts are shared; the project
        # whose wiring holds the handoff runs it)


def validate_agent_dict(a: dict, trigger_names: set, triggers: dict | None = None,
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
    provider = str(a.get("provider") or "").strip()
    if provider and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", provider):
        raise CatalogError(f"agent {a['name']!r}: provider must be a provider id from Settings "
                           "(letters, digits, dashes), or empty for the cell default")
    # Loose on purpose: the console offers a curated list, but YAML/API users may name a model
    # newer than this build knows. The API rejects a wrong name at run time either way. A model
    # on a non-Anthropic provider is whatever that provider calls it.
    if model and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/\-]*", model):
        raise CatalogError(f"agent {a['name']!r}: model must be a model id (or empty for the "
                           "provider's default)")
    if model and provider == "anthropic" and not model.startswith("claude-"):
        raise CatalogError(f"agent {a['name']!r}: model {model!r} is not a Claude model; name the "
                           "provider it belongs to (`provider`) or pick a claude model id")
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
    dc = a.get("daily_cap")
    if dc not in (None, ""):
        try:
            dc = int(str(dc).strip())
        except ValueError:
            raise CatalogError(f"agent {a['name']!r}: daily_cap must be a whole number")
        if dc <= 0:
            raise CatalogError(f"agent {a['name']!r}: daily_cap must be above zero "
                               "(or empty for the instance-wide cap)")
    normalize_handoffs(str(a["name"]), a.get("handoffs"))

    # Loop guard: a Tares agent writes a finding into the `findings` source. If its trigger
    # watches that source, its own finding re-fires the trigger, which runs the agent again,
    # forever. The one valid form is a chain through findings (TR-322): the trigger keeps only
    # ANOTHER agent's findings (`agent` eq that agent), so this agent's own findings never match.
    # A chain that loops back through two agents is bounded by their cooldowns, daily cap and
    # budgets. The simpler way to chain is the agent's own `handoffs` (TR-334), which stop at a
    # depth of three.
    if triggers is not None:
        trig = triggers.get(a["trigger"])
        if trig and FINDINGS_SOURCE in (trig.get("sources") or []):
            others = {str(f.get("value")) for f in (trig.get("filters") or [])
                      if f.get("field") == "agent" and f.get("op") == "eq"}
            if not others or a["name"] in others:
                raise CatalogError(
                    f"agent {a['name']!r}: trigger {a['trigger']!r} watches the "
                    f"{FINDINGS_SOURCE!r} source; an agent can be woken by findings only through "
                    f"a trigger filtered to another agent's findings (agent eq <name>), or it "
                    f"would fire itself forever")
