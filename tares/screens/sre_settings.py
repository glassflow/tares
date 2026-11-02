"""GET /api/sre/settings and PUT /api/sre/settings/{group} on an AI SRE cell (TR-764): the Settings
page, in one read.

The page is seven groups in the order a person changes things: watching, investigating, notes,
tools, people, edition, advanced. Each group is an object with a title and, where a person can
change something, a list of fields `{key, label, value, value_words, options, help}`. `value` is
what is stored, `value_words` the same in a sentence. `can_edit` at the end says whether the
caller may save anything; a read key may load the page and sees no revoke data.

The page reads only GET /api/sre/settings. A write is PUT /api/sre/settings/{group}:

  watching, investigating, notes  {key: value}, applied part by part in the order the page lists
                                   them; the first part that fails stops the rest and the answer
                                   says in a sentence which parts were saved and which was not.
  edition                          {value, confirm: true}; a switch changes the screens, so it needs
                                   the confirm flag.
  people                           {create: {name, scopes}} or {revoke: id}.

Every write is admin: the daemon's scope check answers 403 to any other credential before this
module sees the request. Each write answers the new GET body. The engine work (the trigger, the
agents, the handoffs) goes through the store functions the engine routes use, and only on a cell
whose edition is sre; on any other edition both routes answer 404 like the other sre screens.
"""
import asyncio
import hashlib
import re
import secrets
import time
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse

from .. import edition as edition_mod
from .. import feedback, goal, providers, sre, sre_home, watch
from .. import slack as slack_mod
from ..config import (SCHEDULE_MAX_SUMMARY_LABELS, SCHEDULE_MIN_SECONDS, CatalogError,
                      parse_duration, validate_trigger_dict)
from ..envelope import now_utc
from ..store import day_month_words

GROUPS = ("watching", "investigating", "notes", "tools", "people", "edition", "advanced")
FIELD_GROUPS = ("watching", "investigating", "notes")     # saved with {key: value}
KEY_SCOPES = ("read", "ingest", "admin")
CHANNELS_TTL = 60.0
EDITION_WARNING = ("Switching changes the screens, not your data. What only the other edition "
                   "shows stays reachable under Settings, Advanced.")

# The order the parts of a group are saved in: also the order the page lists them.
ORDER = {
    "watching": ("system_name", "every", "model", "budget_usd", "budget_period", "rule"),
    "investigating": ("model", "budget_usd", "budget_period", "cooldown"),
    "notes": ("slack_channel", "watch_channel"),
}
PART = {
    "system_name": "the name", "every": "how often it checks", "model": "the model",
    "budget_usd": "the budget", "budget_period": "when the budget resets",
    "rule": "what it compares", "cooldown": "the wait between investigations",
    "slack_channel": "the Slack channel",
    "watch_channel": "the channel for services nobody watches",
}
EVERY_CHOICES = ("1m", "5m", "10m", "15m", "30m", "1h", "6h", "1d")
COOLDOWN_CHOICES = ("5m", "15m", "30m", "1h", "2h", "6h", "1d")
PERIOD_OPTIONS = [{"value": "month", "label": "Resets on the 1st of each month"},
                  {"value": "lifetime", "label": "Never resets"}]
SCOPE_WORDS = {"read": "read", "ingest": "send data", "admin": "change settings"}
# The connectors the page lists as tools, in plain names. Anything else (findings, memory,
# references) is the cell's own and not a tool a person connected.
TOOL_KINDS = {"otlp": "Traces and logs", "loki": "Logs", "prometheus": "Metrics",
              "prometheus_alerts": "Alerts", "alertmanager": "Alerts",
              "vercel": "Vercel request logs", "github": "GitHub", "github_app": "GitHub",
              "http_poll": "A web page or API"}


def _link(label: str, address: str, **more) -> dict:
    """A link target. The address sits under `value` because the words check skips that key: an
    address such as /sources/new is a path, not a sentence."""
    return {"label": label, "value": address, **more}


ADD_TOOLS = [
    _link("Metrics", "/sources/new?connector=prometheus"),
    _link("Logs", "/sources/new?connector=loki"),
    _link("Alerts", "/sources/new?connector=alertmanager"),
    _link("Traces and logs", "/sources/new?connector=otlp"),
    _link("GitHub", "/settings/advanced?tab=github"),
    _link("Slack", "/settings/advanced?tab=slack"),
]
ADVANCED_LINKS = [
    _link("All settings", "/settings/advanced",
          help="Model providers, limits, GitHub, Linear and the rest of today's Settings page."),
    _link("Connections", "/sources",
          help="The tools and services this workspace receives data from."),
    _link("Explore your data", "/explore",
          help="Look through the events and details Tares holds."),
    _link("All resources", "/resources",
          help="Every connection, rule and agent in this workspace, in one list."),
    _link("Connect", "/connect",
          help="Give an outside agent access to your data over MCP."),
    _link("Reads", "/reads", help="See what outside agents asked for, and when."),
]
ADVANCED_NOTE = "Agents and rules live in each project, under Advanced setup."
_CHANNEL_ID = re.compile(r"^[A-Z][A-Z0-9]{4,}$")
_CHANNEL_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_LABEL = re.compile(r"^[A-Za-z0-9_.]+$")

_CHANNELS: dict = {}     # (id(store), token) -> {"at", "good", "reason"}; this screen's own


# ── small words ──────────────────────────────────────────────────────────────
def _nd(text):
    return goal._no_dash(text) if text else text


def _join(items) -> str:
    return goal.join_words(list(items))


def _money(v) -> str:
    return watch._money(float(v))


def _seconds_words(seconds: float) -> str:
    return goal.duration_words(seconds)


def _every_label(value: str) -> str:
    words = _seconds_words(parse_duration(value))
    return "Every " + (words[2:] if words.startswith("1 ") else words)


def _cooldown_label(value: str) -> str:
    return _seconds_words(parse_duration(value))


def _first_paragraph(text: str, limit: int = 600) -> str:
    para = next((p.strip() for p in str(text or "").split("\n\n") if p.strip()), "")
    return para if len(para) <= limit else para[:limit].rstrip() + "..."


def _field(key, label, value, value_words, options=None, help="", **extra) -> dict:
    return {"key": key, "label": label, "value": value, "value_words": value_words,
            "options": options, "help": help, **extra}


# ── Slack channels, kept for a minute so a page open in ten tabs asks Slack once ─
async def _slack_channels(store):
    """(channels or None, problem): the list the picker offers, None when Slack is not connected
    or did not answer. A list that worked a minute ago is served again if Slack fails now."""
    token, _origin = slack_mod.resolve_token(store)
    if not token:
        return None, "not_connected"
    key = (id(store), token)
    hit = _CHANNELS.get(key)
    if hit and time.monotonic() - hit["at"] < CHANNELS_TTL:
        return hit["good"], hit["reason"]
    try:
        channels, reason, _detail, _retry = await slack_mod.list_channels(token, timeout=5.0)
    except Exception:
        channels, reason = [], "error"
    good = (hit or {}).get("good")
    if reason is None:
        good, problem = channels, None
    else:
        problem = "missing_scope" if reason == "missing_scope" else "error"
    _CHANNELS[key] = {"at": time.monotonic(), "good": good, "reason": problem}
    return good, problem


# ── the engine objects the page edits ────────────────────────────────────────
def _cast(store, runtime):
    """The watcher and the investigator the page edits (the first of each) and the roles."""
    roles = sre.roles(store, runtime.catalog, include_paused=True)
    watcher = roles["watchers"][0] if roles["watchers"] else None
    inv = None
    if roles["investigators"]:
        handed = ({h["agent"] for h in store.list_handoffs(project=watcher["project"],
                                                          from_agent=watcher["agent"])}
                  if watcher else set())
        inv = next((i for i in roles["investigators"]
                    if watcher and i["project"] == watcher["project"] and i["agent"] in handed),
                   roles["investigators"][0])
    return roles, watcher, inv


def _agent_of(store, who):
    return store.get_catalog_agent(who["agent"], project=who["project"]) if who else None


def _trigger_of(store, watcher):
    return next((t for t in store.list_catalog_triggers() if t["name"] == watcher["trigger"]),
                None) if watcher else None


def _handoff_cooldown(store, watcher, inv):
    if not (watcher and inv):
        return None
    rows = [h for h in store.list_handoffs(project=watcher["project"], from_agent=watcher["agent"])
            if h["agent"] == inv["agent"]]
    return next((h["cooldown"] for h in rows if h.get("cooldown")), None) if rows else None


def _model_options(store) -> list:
    """The default, then every model of every provider that is set up."""
    default = providers.default_model_for(store, None)
    seen, rows = set(), []
    families: dict = {}
    for p in providers.list_providers(store)["providers"]:
        if not p["configured"]:
            continue
        for m in p["models"]:
            if m in seen:
                continue
            seen.add(m)
            rows.append((m, p["id"]))
            fam = sre_home._model_words(store, {"model": m})
            families[fam] = families.get(fam, 0) + 1
    out = [{"value": "", "label": "The usual model" + (
        f" ({sre_home._model_words(store, {'model': default})})" if default else "")}]
    for m, pid in rows:
        fam = sre_home._model_words(store, {"model": m})
        out.append({"value": m, "label": fam if families[fam] == 1 else f"{fam} ({m})",
                    "provider": pid})
    return out


def _spent_line(store, agent: dict, now) -> str:
    """"Nothing spent yet", "Spent $4.20 of $25 this month", and for a monthly budget that is
    used up "Spent $25 of $25 this month, starts again on 1 Nov"."""
    spent = store.agent_budget_spend(agent, now)
    budget = agent.get("budget_usd")
    month = agent.get("budget_period") == "month"
    if spent <= 0:
        if budget:
            return f"Nothing spent yet, a budget of {_money(budget)}" + (" a month" if month else "")
        return "Nothing spent yet"
    line = f"Spent ${spent:.2f}" + (f" of {_money(budget)}" if budget else "")
    line += " this month" if month else ""
    if month and budget and spent >= float(budget):
        resets = store.budget_resets_on(agent, now)
        if resets:
            line += ", starts again on " + day_month_words(resets)
    return line


def _agent_fields(store, agent: dict, models: list, now) -> list:
    """The model, the budget and when the budget resets: the same three for either agent."""
    model = agent.get("model") or ""
    default = providers.default_model_for(store, agent.get("provider") or None)
    budget = agent.get("budget_usd")
    month = agent.get("budget_period") == "month"
    resets = store.budget_resets_on(agent, now)
    return [
        _field("model", "Model", model,
               sre_home._model_words(store, agent) + ("" if model else " (the usual model)"),
               models, "The AI model it thinks with." if models else
               "No AI model is set up yet. Add one under All settings, Advanced.",
               default=default),
        _field("budget_usd", "Budget", float(budget) if budget else None,
               (_money(budget) + (" a month" if month else " in all")) if budget else "No budget",
               None, "It stops when it has spent this much. Leave empty for no limit.",
               spent_words=_spent_line(store, agent, now)),
        _field("budget_period", "When the budget resets", "month" if month else "lifetime",
               PERIOD_OPTIONS[0]["label"] if month else PERIOD_OPTIONS[1]["label"],
               PERIOD_OPTIONS, "A monthly budget starts again on the 1st at 00:00 UTC.",
               resets_on=resets.isoformat() if resets else None,
               resets_words=("Starts again on " + day_month_words(resets)) if resets else None),
    ]


def _choices(current: str, presets, label) -> list:
    values = list(presets) + ([current] if current and current not in presets else [])
    values.sort(key=parse_duration)
    return [{"value": v, "label": label(v)} for v in values]


def _watching(store, runtime, watcher, now, models) -> dict:
    fields = []
    name = (store.get_setting(watch.SYSTEM_NAME_SETTING) or "").strip()
    shown = watch.system_name(store)
    fields.append(_field("system_name", "What we call what is watched", name,
                         shown if name else f"{shown}, the default", None,
                         "Used in messages and on the home page. Leave empty to use the name "
                         "of this instance."))
    group = {"title": "Watching", "editable": True, "fields": fields, "empty_words": None}
    if watcher is None:
        group["empty_words"] = ("No watcher is set up yet, so nothing is being checked and "
                                "nothing has been spent.")
        return group
    agent = _agent_of(store, watcher) or {}
    trig = next((t for t in runtime.catalog.triggers if t.name == watcher["trigger"]), None)
    stored = _trigger_of(store, watcher) or {}
    every = (stored.get("condition") or {}).get("every") or ""
    if every:
        fields.append(_field("every", "How often it checks", every, _every_label(every),
                             _choices(every, EVERY_CHOICES, _every_label),
                             "It looks at your services this often. The shortest is every minute."))
    fields += _agent_fields(store, agent, models, now)
    labels = list((stored.get("condition") or {}).get("summary_by") or [])
    fields.append(_field("rule", "What it compares", labels,
                         ("Compares by " + _join(labels)) if labels
                         else "Compares everything together", None,
                         "The labels it groups events by before it decides what is unusual. "
                         f"Up to {SCHEDULE_MAX_SUMMARY_LABELS}."))
    fields.append(_field("rule_text", "The rule, as written", None, None, None,
                         "To change the wording, open the watcher's setup under Advanced.",
                         read_only=True, text=_first_paragraph(agent.get("prompt")),
                         link=_link("Open the watcher's setup", f"/projects/{watcher['project']}")))
    row = next((r for r in feedback.hit_rates(store, now=now)
                if r["agent"] == watcher["agent"] and r["project"] == watcher["project"]), None)
    fields.append(_field("hit_rate", "How often a raised case was worth it", None,
                         row["words"] if row else "0 answers so far", None,
                         "From the Worth raising? answers people give in Slack.",
                         read_only=True))
    group["agent"] = watcher["agent"]
    return group


def _investigating(store, runtime, watcher, inv, now, models) -> dict:
    group = {"title": "Investigating", "editable": True, "fields": [], "empty_words": None}
    if inv is None:
        group["empty_words"] = ("No investigator is set up yet, so nothing has been looked into "
                                "and nothing has been spent.")
        return group
    agent = _agent_of(store, inv) or {}
    group["fields"] = _agent_fields(store, agent, models, now)
    cooldown = _handoff_cooldown(store, watcher, inv)
    if cooldown:
        group["fields"].append(_field(
            "cooldown", "Wait before looking at the same service again", cooldown,
            _cooldown_label(cooldown), _choices(cooldown, COOLDOWN_CHOICES, _cooldown_label),
            "After an investigation it leaves that service alone for this long."))
    group["agent"] = inv["agent"]
    return group


def _notes(store, watcher, inv, channels, problem) -> dict:
    connected = problem != "not_connected"
    agent = _agent_of(store, inv) or _agent_of(store, watcher) or {}
    channel = (agent.get("slack_channel") or "").strip()
    named = {c["id"]: c for c in channels or []}
    if not connected:
        words = "Slack is not connected, so notes are not sent. Connect Slack under Tools."
    elif not channel:
        words = "No channel chosen, so notes are not sent to Slack."
    elif channel in named:
        words = "#" + named[channel]["name"]
    else:
        words = "A Slack channel"
    options = ([{"value": "", "label": "No channel"}]
               + [{"value": c["id"], "label": "#" + c["name"]} for c in channels]) \
        if channels is not None else None
    help_ = ("The channel the watcher and the investigator write to." if connected else
             "Connect Slack first. Until then notes stay in the console.")
    if connected and channels is None:
        help_ = ("Slack did not answer, so the list of channels is not available. "
                 "The channel set now stays as it is.")
    wc = (store.get_setting(watch.CHANNEL_SETTING) or "").strip().lstrip("#")
    return {"title": "Where notes go", "editable": True, "empty_words": None,
            "slack": {"connected": connected, "problem": None if problem in (None, "not_connected")
                      else problem,
                      "words": "Slack is connected." if connected else "Slack is not connected.",
                      "link": _link("Slack settings", "/settings/advanced?tab=slack")},
            "fields": [
                _field("slack_channel", "Slack channel for notes", channel, words, options, help_),
                _field("watch_channel", "Channel for services nobody watches", wc,
                       ("#" + wc) if wc else "No channel chosen", None,
                       "Where a message goes when a service has no watcher. A channel name, "
                       "such as #oncall, or its Slack ID."),
            ]}


def _tools(store, runtime, now) -> dict:
    catalog = runtime.catalog
    names = [s.name for s in catalog.sources.values()]
    got = goal._health_for(runtime.health_snapshot() or {}, store, names)
    rows = []
    for s in sorted(catalog.sources.values(), key=lambda x: x.name):
        kind = TOOL_KINDS.get(s.connector)
        if kind is None:
            continue
        st = goal.source_state(store, s, got.get(s.name), now)
        ok = st["state"] in ("receiving", "waiting")
        rows.append({"name": s.name, "kind": kind, "ok": ok, "state": st["state"],
                     "state_words": _nd(st["detail"]), "problem_words": None if ok else _nd(st["detail"]),
                     "link": _link("Open", f"/sources/{s.name}")})
    token = slack_mod.resolve_token(store)[0]
    rows.append({"name": "Slack", "kind": "Slack", "ok": bool(token), "state": "connected" if token else "off",
                 "state_words": "Connected" if token else "Not connected",
                 "problem_words": None if token else "Not connected, so notes are not sent.",
                 "link": _link("Slack settings", "/settings/advanced?tab=slack")})
    empty = None if len(rows) > 1 else "Nothing is connected yet. Add a tool below to start."
    return {"title": "Tools", "editable": False, "rows": rows, "add": ADD_TOOLS,
            "empty_words": empty}


def _people(store, can_edit, workspace) -> dict:
    keys = []
    now = now_utc()
    for k in store.list_api_keys():
        if k.get("revoked_at"):
            continue
        scopes = [s for s in k.get("scopes") or []]
        used = k.get("last_used_at")
        row = {"name": k["name"],
               "scopes_words": ("Can " + _join(SCOPE_WORDS.get(s, s) for s in scopes)) if scopes else "No access",
               "last_used_words": ("Used " + goal.age_words((now - goal._aware(used)).total_seconds()))
               if used else "Never used"}
        if k.get("project"):
            row["scopes_words"] = "Can read one project"
        if can_edit:
            row.update(id=k["id"], can_revoke=True)
        keys.append(row)
    group = {"title": "People and keys", "editable": can_edit, "keys": keys,
             "scope_options": [{"value": s, "label": SCOPE_WORDS[s]} for s in KEY_SCOPES],
             "empty_words": None if keys else "No keys yet.",
             "workspace": ({"words": "Manage people in your workspace",
                            "link": _link("Workspace settings", "/settings/advanced?tab=workspace")}
                           if workspace else None)}
    return group


def _edition(store, can_edit) -> dict:
    value, _needed = edition_mod.edition(store)
    names = edition_mod.NAMES
    return {"title": "Edition", "editable": can_edit, "value": value, "label": names[value],
            "can_switch": bool(can_edit),
            "options": [{"value": "factory", "label": names["factory"]},
                        {"value": "sre", "label": names["sre"]}],
            "warning": EDITION_WARNING}


def _advanced() -> dict:
    return {"title": "Advanced", "editable": False, "links": ADVANCED_LINKS, "note": ADVANCED_NOTE}


def settings_screen(store, runtime, can_edit: bool, channels, problem, workspace: bool = False,
                    now=None) -> dict:
    """The body of GET /api/sre/settings."""
    t = now or now_utc()
    _roles, watcher, inv = _cast(store, runtime)
    models = _model_options(store)
    return {
        "watching": _watching(store, runtime, watcher, t, models),
        "investigating": _investigating(store, runtime, watcher, inv, t, models),
        "notes": _notes(store, watcher, inv, channels, problem),
        "tools": _tools(store, runtime, t),
        "people": _people(store, can_edit, workspace),
        "edition": _edition(store, can_edit),
        "advanced": _advanced(),
        "can_edit": bool(can_edit),
    }


# ── writes ───────────────────────────────────────────────────────────────────
class Refused(Exception):
    """A part that cannot be saved; the message is a sentence for the person."""


def _text(value, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise Refused("it must be text.")
    value = " ".join(value.split())
    if len(value) > limit:
        raise Refused(f"it can be at most {limit} characters.")
    return value


def _duration(value, minimum: float) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Refused("it must be a length of time such as 10m or 1h.")
    value = value.strip()
    try:
        seconds = parse_duration(value)
    except (KeyError, ValueError, IndexError):
        raise Refused("it must be a length of time such as 10m or 1h.")
    if seconds < minimum:
        raise Refused(f"it must be at least {_seconds_words(minimum)}.")
    return value


def _amount(value):
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise Refused("it must be a number of dollars, such as 25.")
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise Refused("it must be a number of dollars, such as 25.")
    if not 0 < n <= 1_000_000:
        raise Refused("it must be more than 0 dollars, or empty for no limit.")
    return round(n, 2)


def _save_agent(store, who, **changes) -> None:
    """Write fields of one agent the way PUT /api/agents/builtin/{name} does. The store blanks any
    field it is given as None, so every stored field goes back as it was; handoffs, concludes,
    verdicts and github stay as stored when passed as None."""
    name, uid = who["agent"], who["project"]
    agent = store.get_catalog_agent(name, project=uid)
    if agent is None:
        raise Refused("that agent is gone, so reload the page.")
    fields = {**agent, **changes}
    trigger = (store.get_catalog_agent(name) or agent)["trigger"]
    handoffs = changes.get("handoffs")
    store.upsert_catalog_agent(
        name, trigger, agent["prompt"], agent.get("slack_webhook", ""), fields.get("model", ""),
        fields.get("slack_channel", ""), agent.get("webhook_url", ""),
        agent.get("webhook_token", ""), agent.get("mcp_servers") or [], agent.get("max_rounds"),
        fields.get("budget_usd"), webhook_key_label=agent.get("webhook_key_label", ""),
        provider=fields.get("provider", ""), daily_cap=agent.get("daily_cap"),
        handoffs=handoffs, concludes=None, verdicts=None, github=None,
        budget_period=fields.get("budget_period") or "lifetime")
    if handoffs is not None:
        store.set_handoffs(uid, name, handoffs)    # the project this page reads the wait from
    store.mark_customized("agent", name)


def _save_trigger(store, runtime, watcher, **condition) -> None:
    stored = _trigger_of(store, watcher)
    if stored is None:
        raise Refused("the check rule is gone, so reload the page.")
    cond = {**stored["condition"], **condition}
    try:
        validate_trigger_dict({**stored, "condition": cond}, set(runtime.catalog.sources))
    except CatalogError:
        raise Refused("the check rule does not accept that.")
    store.upsert_catalog_trigger(stored["name"], stored["sources"], cond, stored["emit"],
                                 stored["cooldown"], filters=stored["filters"],
                                 key_field=stored["key_field"], description=None)
    store.mark_customized("trigger", stored["name"])


def _need(who, kind: str):
    if who is None:
        raise Refused(f"no {kind} is set up yet, so there is nothing to change.")
    return who


def _model_change(store, value) -> dict:
    value = _text(value, 200)
    if not value:
        return {"model": "", "provider": ""}
    for p in providers.list_providers(store)["providers"]:
        if p["configured"] and value in p["models"]:
            default = providers.default_id(store)
            return {"model": value, "provider": "" if p["id"] == default else p["id"]}
    raise Refused("it is not one of the models set up here, so pick one from the list.")


def _apply(store, runtime, group: str, key: str, value, watcher, inv) -> None:
    """One part of a group, saved on its own."""
    if key == "system_name":
        store.set_setting(watch.SYSTEM_NAME_SETTING, _text(value, 80) or None)
    elif key == "watch_channel":
        channel = _text(value, 80).lstrip("#")
        if channel and not (_CHANNEL_NAME.match(channel) or _CHANNEL_ID.match(channel)):
            raise Refused("a channel is its name, such as #oncall, or its Slack ID.")
        store.set_setting(watch.CHANNEL_SETTING, channel or None)
    elif key == "slack_channel":
        channel = _text(value, 80).lstrip("#")
        if channel and not _CHANNEL_ID.match(channel):
            raise Refused("pick a channel from the list, which saves it by its Slack ID, "
                          "such as C0123456789.")
        targets = [w for w in (watcher, inv) if w]
        if not targets:
            raise Refused("no watcher or investigator is set up yet, so there is nothing to change.")
        for who in {(w["agent"], w["project"]): w for w in targets}.values():
            _save_agent(store, who, slack_channel=channel)
    elif group == "watching":
        w = _need(watcher, "watcher")
        if key == "every":
            _save_trigger(store, runtime, w,
                          every=_duration(value, SCHEDULE_MIN_SECONDS))
        elif key == "rule":
            labels = ([x.strip() for x in value.split(",") if x.strip()] if isinstance(value, str)
                      else value)
            if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
                raise Refused("it must be a list of labels, such as service.")
            labels = [x.strip() for x in labels]
            if len(labels) > SCHEDULE_MAX_SUMMARY_LABELS or not all(_LABEL.match(x) for x in labels):
                raise Refused(f"name up to {SCHEDULE_MAX_SUMMARY_LABELS} labels, using letters, "
                              "digits, dots and underscores.")
            _save_trigger(store, runtime, w, summary_by=labels)
        else:
            _apply_agent(store, key, value, w)
    elif group == "investigating":
        i = _need(inv, "investigator")
        if key == "cooldown":
            w = _need(watcher, "watcher")
            wait = _duration(value, 0)
            rows = store.list_handoffs(project=w["project"], from_agent=w["agent"])
            mine = [h for h in rows if h["agent"] == i["agent"]]
            if not mine:
                raise Refused("the watcher does not hand anything to the investigator, so there "
                              "is no wait to change.")
            new = [{"verdict": h["verdict"], "agent": h["agent"],
                    "cooldown": wait if h["agent"] == i["agent"] else h["cooldown"]} for h in rows]
            _save_agent(store, w, handoffs=new)
        else:
            _apply_agent(store, key, value, i)


def _apply_agent(store, key: str, value, who) -> None:
    if key == "model":
        _save_agent(store, who, **_model_change(store, value))
    elif key == "budget_usd":
        _save_agent(store, who, budget_usd=_amount(value))
    elif key == "budget_period":
        if value not in ("month", "lifetime"):
            raise Refused("pick either resets each month or never resets.")
        _save_agent(store, who, budget_period=value)


def _write_fields(store, runtime, group: str, body: dict):
    """Apply the parts of `body` in page order. Returns None when all were saved, else the 400."""
    unknown = [k for k in body if k not in ORDER[group]]
    if unknown:
        return JSONResponse({"detail": f"{unknown[0]!r} is not something to change here. "
                                       f"Nothing was saved.", "saved": [], "failed": unknown[0]},
                            status_code=400)
    if not body:
        return JSONResponse({"detail": "Nothing to save.", "saved": [], "failed": None},
                            status_code=400)
    parts = [k for k in ORDER[group] if k in body]
    _roles, watcher, inv = _cast(store, runtime)
    saved = []
    try:
        for n, key in enumerate(parts):
            try:
                _apply(store, runtime, group, key, body[key], watcher, inv)
            except Refused as e:
                said = (f"Saved {_join(PART[k] for k in saved)}." if saved else "Nothing was saved.")
                said += f" Did not save {PART[key]}: {e}"
                rest = parts[n + 1:]
                if rest:
                    said += f" Not tried: {_join(PART[k] for k in rest)}."
                return JSONResponse({"detail": said, "saved": saved, "failed": key,
                                     "not_tried": rest}, status_code=400)
            saved.append(key)
    finally:
        if saved:
            runtime.reload_catalog()
            watch.sync(store, runtime.catalog)
            sre_home.clear_cache()
    return None


def _write_edition(store, body: dict):
    value = edition_mod._valid(body.get("value"))
    if value is None:
        return JSONResponse({"detail": "Pick one of " + _join(edition_mod.NAMES[e] for e in edition_mod.EDITIONS)
                             + "."}, status_code=400)
    current, _needed = edition_mod.edition(store)
    if value == current:
        return None
    if body.get("confirm") is not True:
        return JSONResponse({"detail": EDITION_WARNING, "needs_confirm": True}, status_code=409)
    store.set_setting(edition_mod.EDITION_SETTING, value)
    sre_home.clear_cache()
    return {"ok": True, "edition": value, "edition_name": edition_mod.NAMES[value],
            "warning": EDITION_WARNING}


def _write_people(store, body: dict):
    if "revoke" in body:
        kid = str(body.get("revoke") or "").strip()
        if not store.revoke_api_key(kid):
            return JSONResponse({"detail": "That key is not active, so there is nothing to revoke."},
                                status_code=404), None
        return None, None
    create = body.get("create")
    if not isinstance(create, dict):
        return JSONResponse({"detail": "Send create with a name and scopes, or revoke with a key id."},
                            status_code=400), None
    try:
        name = _text(create.get("name"), 80)
    except Refused as e:
        return JSONResponse({"detail": f"The key's name: {e}"}, status_code=400), None
    raw = create.get("scopes") or []
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        return JSONResponse({"detail": "Pick what the key may do: " + _join(SCOPE_WORDS[s] for s in KEY_SCOPES) + "."},
                            status_code=400), None
    scopes = sorted(set(raw))
    if not name:
        return JSONResponse({"detail": "Give the key a name."}, status_code=400), None
    if not scopes or not set(scopes) <= set(KEY_SCOPES):
        return JSONResponse({"detail": "Pick what the key may do: " + _join(SCOPE_WORDS[s] for s in KEY_SCOPES) + "."},
                            status_code=400), None
    kid = uuid.uuid4().hex[:8]
    secret = f"nvf_{kid}_{secrets.token_urlsafe(24)}"
    store.insert_api_key(kid, name, f"nvf_{kid}", hashlib.sha256(secret.encode()).hexdigest(),
                         scopes, project=None)
    # the secret exists only in this answer; the store keeps its hash
    return None, {"id": kid, "name": name, "scopes_words": "Can " + _join(SCOPE_WORDS[s] for s in scopes), "secret": secret}


def register(app, store, h, ctx):
    runtime = ctx.runtime

    def on_sre() -> bool:
        return edition_mod.edition(store)[0] == "sre"

    def allowed(request: Request) -> bool:
        scopes = getattr(request.state, "scopes", None)
        return scopes is None or "admin" in scopes      # None: the instance is open

    def workspace() -> bool:
        import os
        return bool(os.getenv("TARES_WORKSPACE_API_URL", "").strip())

    async def screen(request: Request, extra: dict | None = None) -> dict:
        channels, problem = await _slack_channels(store)
        body = await asyncio.to_thread(settings_screen, store, runtime, allowed(request), channels,
                                       problem, workspace())
        return {**body, **(extra or {})}

    @app.get("/api/sre/settings")
    async def get_settings(request: Request):
        if not on_sre():
            return JSONResponse({"error": "no such page"}, status_code=404)
        return await screen(request)

    @app.put("/api/sre/settings/{group}")
    async def put_settings(group: str, request: Request):
        if not on_sre() or group not in ("watching", "investigating", "notes", "people", "edition"):
            return JSONResponse({"error": "no such page"}, status_code=404)
        try:
            body = await request.json()
        except Exception:
            body = None
        if not isinstance(body, dict):
            return JSONResponse({"detail": "Send the settings as an object."}, status_code=400)
        extra = None
        if group in FIELD_GROUPS:
            refused = await asyncio.to_thread(_write_fields, store, runtime, group, body)
            if refused is not None:
                return refused
        elif group == "people":
            refused, created = await asyncio.to_thread(_write_people, store, body)
            if refused is not None:
                return refused
            extra = {"created_key": created} if created else None
        else:
            switched = await asyncio.to_thread(_write_edition, store, body)
            if isinstance(switched, JSONResponse):
                return switched
            if switched is not None:
                return switched         # the cell is no longer sre: the other edition's answer
        return await screen(request, extra)
