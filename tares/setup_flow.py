"""The guided project setup: a goal in, a whole plan in plain words out, then one apply.

The plan (the JSON the console and this module pass around) says what the project watches, when
it wakes, who does the work (Tares agents, or the person's own agent joining with a project key),
the outside tools and the know-how. The model writes it once through a forced `propose_plan` tool
and revises it on a plain-words instruction; everything after that is deterministic:
`normalize` checks a plan with the catalog's own validators, derives the trigger condition,
cooldown and every sentence from the knobs (the numbers a person tunes in place), and `apply`
creates the project and its objects in one go, undoing what it made when a step fails.

Every string here is shown to a person: plain language, no em dashes.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
from datetime import datetime, timezone

from . import goal as G
from . import skills as _skills
from .config import (CatalogError, _source_from_dict, _trigger_from_dict, check_handoff_targets,
                     normalize_handoffs, parse_duration, validate_agent_dict,
                     validate_mcp_server_dict, validate_source_dict, validate_trigger_dict)
from .connectors import SPECS, normalize_config, secret_field_names
from .models import ModelError, ModelUnavailable, add_usage, empty_usage, tool_message

STEPS = ("connect", "try", "done")
MAX_AGENTS = 3
MAX_ROUNDS = 4            # model calls for one plan: reads, then the forced propose_plan
PRACTICE_WINDOW_S = 600   # an outside agent's finding this soon after a practice firing is practice
READ_TOOL_NAMES = ("list_connectors", "list_sources", "source_fields", "list_templates")
KNOB_IDS = ("threshold", "window_minutes", "cooldown_minutes", "every_minutes")
NO_PROVIDER = ("Add a model provider under Settings to plan a project; or start from a "
               "template.")


class SetupError(Exception):
    """A plain message for the person, with the HTTP status the route answers."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ── the propose_plan tool: its input schema IS the plan ──────────────────────
_S = {"type": "string"}
_KNOB = {"type": "object", "properties": {
    "id": {"type": "string", "enum": list(KNOB_IDS),
           "description": "threshold: the number in the condition; window_minutes: the "
                          "detection window; cooldown_minutes: the quiet time before waking "
                          "again for the same entity; every_minutes: a schedule's interval"},
    "label": {"type": "string", "description": "the unit in plain words: errors, minutes"},
    "value": {"type": "number"}, "min": {"type": "number"}, "max": {"type": "number"}},
    "required": ["id", "label", "value"]}

PLAN_SCHEMA = {"type": "object", "properties": {
    "goal": {"type": "string", "description": "the person's goal, one line, at most 200 "
                                              "characters, in their words"},
    "name": {"type": "string", "description": "the project name, two to four words"},
    "summary": {"type": "string", "description": "one or two plain sentences: what wakes the "
                                                 "project and who looks"},
    "watches": {"type": "array", "description": "the sources: new ones, or existing ones reused",
                "items": {"type": "object", "properties": {
                    "key": {"type": "string", "description": "w1, w2, ..."},
                    "existing": {"type": "boolean",
                                 "description": "true reuses a source list_sources shows; then "
                                                "leave out connector and config"},
                    "name": {"type": "string", "description": "short kebab-case"},
                    "connector": {"type": "string", "description": "a name list_connectors "
                                                                  "returned"},
                    "poll": {"type": "string", "description": "poll interval for a polled "
                                                              "source, e.g. 5m; omit for push"},
                    "config": {"type": "object",
                               "description": "the connector's non-secret config, with labels "
                                              "(one primary) and, for a webhook, a "
                                              "text_template"},
                    "sentence": {"type": "string",
                                 "description": "what it is, in one plain line"},
                    "needs": {"type": "string", "enum": ["send", "credential", "none"],
                              "description": "send: their system posts events to an address; "
                                             "credential: a token or password is missing; "
                                             "none: nothing to do"},
                    "sample": {"type": ["object", "null"],
                               "description": "one realistic example event, for a push source"}},
                    "required": ["key", "existing", "name", "sentence", "needs"]}},
    "wakes": {"type": "array", "description": "the triggers, usually one",
              "items": {"type": "object", "properties": {
                  "key": {"type": "string", "description": "k1, k2, ..."},
                  "name": {"type": "string", "description": "short kebab-case"},
                  "sources": {"type": "array", "items": _S,
                              "description": "names of watches in this plan"},
                  "filters": {"type": "array", "items": {"type": "object", "properties": {
                      "field": _S,
                      "op": {"type": "string",
                             "enum": ["eq", "neq", "contains", "gt", "gte", "lt", "lte"]},
                      "value": {"type": ["string", "number"]}},
                      "required": ["field", "op", "value"]}},
                  "key_field": {"type": "string", "description": "the entity label (the "
                                                             "primary label of the sources)"},
                  "condition": {"type": "object", "properties": {
                      "aggregate": {"type": "string",
                                    "enum": ["count", "avg", "sum", "max", "min", "any"]},
                      "field": {"type": "string", "description": "a number label; omit for "
                                                                 "count"},
                      "predicate": {"type": "string", "description": "e.g. > 5"},
                      "window": {"type": "string", "description": "e.g. 5m"},
                      "every": {"type": "string", "description": "a schedule instead of a "
                                                                 "condition, e.g. 1h"}}},
                  "cooldown": {"type": "string", "description": "e.g. 10m"},
                  "window": {"type": "string", "description": "how much timeline the agents "
                                                              "get, e.g. 15m"},
                  "sentence": _S,
                  "knobs": {"type": "array", "items": _KNOB}},
                  "required": ["key", "name", "sources", "condition", "knobs"]}},
    "who": {"type": "string", "enum": ["tares", "own"]},
    "agents": {"type": "array", "description": "Tares agents (who tares), at most 3",
               "items": {"type": "object", "properties": {
                   "key": {"type": "string", "description": "a1, a2, ..."},
                   "name": {"type": "string", "description": "short kebab-case"},
                   "trigger": {"type": "string", "description": "a wake name in this plan"},
                   "on_trigger": {"type": "boolean",
                                  "description": "false for an agent that runs only when "
                                                 "another hands off to it"},
                   "prompt": {"type": "string", "description": "what to look at, what a useful "
                                                               "finding says, the verdict to "
                                                               "conclude with"},
                   "model": {"type": ["string", "null"]},
                   "handoffs": {"type": "array", "items": {"type": "object", "properties": {
                       "verdict": {"type": "string", "description": "one lowercase word"},
                       "agent": _S, "cooldown": _S}, "required": ["verdict", "agent"]}},
                   "mcp_servers": {"type": "array", "items": _S,
                                   "description": "names of tools in this plan"},
                   "sentence": _S,
                   "optional": {"type": "boolean"},
                   "enabled": {"type": "boolean"}},
                   "required": ["key", "name", "trigger", "prompt", "sentence"]}},
    "own_agent": {"type": ["object", "null"], "description": "who own: the person's agent",
                  "properties": {"name": {"type": "string", "description": "e.g. claude-code"},
                                 "wake": {"type": "string", "enum": ["webhook", "poll"]},
                                 "sentence": _S}},
    "tools": {"type": "array", "description": "outside MCP servers for the Tares agents, off",
              "items": {"type": "object", "properties": {
                  "key": _S, "name": _S, "url": _S, "why": _S,
                  "can_act": {"type": "boolean"}, "enabled": {"type": "boolean"}},
                  "required": ["key", "name", "why"]}},
    "skills": {"type": "array", "description": "know-how, at most 2",
               "items": {"type": "object", "properties": {
                   "key": _S, "name": {"type": "string", "description": "lowercase-dashed"},
                   "description": _S, "body": {"type": "string", "description": "markdown"},
                   "enabled": {"type": "boolean"}},
                   "required": ["key", "name", "description", "body"]}},
    "notes": {"type": "array", "items": _S, "description": "plain caveats, may be empty"}},
    "required": ["goal", "name", "summary", "watches", "wakes", "who", "agents", "tools",
                 "skills", "notes"]}

PLAN_TOOL = {"name": "propose_plan",
             "description": "Propose the whole project plan. Call it once, with everything.",
             "input_schema": PLAN_SCHEMA}


def read_tools() -> list:
    from .agent import TOOLS
    return [t for t in TOOLS if t["name"] in READ_TOOL_NAMES]


SYSTEM = """You plan a Tares project from a person's goal. Tares collects events from sources, \
wakes when a condition over them is met (a trigger), and runs agents that read what happened and \
write a finding. Look with the read tools if you need to, then call propose_plan once with the \
whole plan.

Rules:
- Sources: prefer an existing source when one fits (existing true, leave out connector and \
config). Otherwise pick a connector list_connectors returned. When the person gave no address \
for a system, use a webhook their system sends events to (needs send) with one realistic sample \
event. A polled source needs its non-secret settings in config; never invent a secret, set needs \
credential instead.
- A new source's config declares labels: exactly one primary label naming the entity (service, \
customer, repo), plus a text_template for a webhook.
- Names are short lowercase kebab-case.
- Wakes: usually one trigger. key_field is the primary label. Give knobs for the numbers a \
person would tune: threshold, window_minutes and cooldown_minutes (every_minutes for a schedule), \
each with a plain label and a sensible min and max.
- who tares: at most 3 agents. For an incident goal: a triage agent that looks first and \
concludes with the verdict investigate when it is real, handing off to an optional root cause \
agent (on_trigger false, optional true). A prompt says what to look at, what a useful finding \
says, and which verdict to conclude with.
- who own: no agents; fill own_agent. A Tares agent cannot hand off to the person's own agent \
yet: say so in notes ONLY when the goal asks for Tares agents and their own agent together; \
otherwise do not mention it.
- Tools: suggest an outside MCP server only when the goal needs context Tares does not hold \
(deploys, tickets, code); enabled false, url empty unless the person gave one, can_act true when \
it can change things.
- Skills: at most 2, only when the goal implies house rules or vocabulary.
- Every sentence is plain words for someone who is not an engineer: no jargon, no em dashes."""


# ── plain text ───────────────────────────────────────────────────────────────
_PLACEHOLDER = re.compile(r"^\s*(<[^<>]*>|\[[^\[\]]*\]|unknown|todo|tbd|n/?a|your[-_][a-z0-9_-]*|xxx+)\s*$",
                          re.IGNORECASE)


def scrub(obj):
    """Every string in `obj` without em dashes: " — " and "—" become a comma. A placeholder the
    model wrote for a value it does not know ("<UNKNOWN>", "your-loki-url") becomes empty, so the
    person is asked for it instead of Tares trying it."""
    if isinstance(obj, str):
        if _PLACEHOLDER.match(obj):
            return ""
        return re.sub(r"\s*—\s*", ", ", obj)
    if isinstance(obj, list):
        return [scrub(x) for x in obj]
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items()}
    return obj


def slug(name) -> str:
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9-]+", "-", str(name or "").strip().lower())
                  ).strip("-")[:60]


def _unique(name: str, taken: set) -> str:
    if name not in taken:
        return name
    n = 2
    while f"{name}-{n}" in taken:
        n += 1
    return f"{name}-{n}"


def _num(v):
    f = float(v)
    return int(f) if f.is_integer() else f


def _minutes(duration) -> int | None:
    try:
        return max(1, math.ceil(parse_duration(duration) / 60))
    except (ValueError, KeyError, IndexError, TypeError):
        return None


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


# ── knobs: the numbers a person tunes, and what they set ─────────────────────
def _derived(kid: str, w: dict):
    """The knob's value as the wake's condition says it, or None."""
    c = w.get("condition") or {}
    if kid == "threshold":
        p = G._predicate(c.get("predicate"))
        return _num(p[1]) if p else None
    if kid == "window_minutes":
        return _minutes(c.get("window")) if c.get("window") else None
    if kid == "cooldown_minutes":
        return _minutes(w.get("cooldown") or "5m")
    if kid == "every_minutes":
        return _minutes(c.get("every")) if c.get("every") else None
    return None


_KNOB_DEFAULTS = {"threshold": (1, 100000), "window_minutes": (1, 1440),
                  "cooldown_minutes": (0, 1440), "every_minutes": (1, 10080)}


def _knob_label(kid: str, w: dict) -> str:
    c = w.get("condition") or {}
    if kid == "threshold":
        return "events" if c.get("aggregate", "count") == "count" else (c.get("field") or "value")
    return {"window_minutes": "minutes", "cooldown_minutes": "minutes before waking again",
            "every_minutes": "minutes"}[kid]


def apply_knobs(w: dict, prev: dict | None, errors: list, where: str) -> None:
    """Knobs are the truth for the numbers: they set the condition and the cooldown. A knob the
    plan lacks is derived from the condition. On an adjust (`prev` is the wake before), a number
    the model changed in the condition while leaving its knob alone moves the knob too."""
    schedule = bool((w.get("condition") or {}).get("every"))
    wanted = ("every_minutes", "cooldown_minutes") if schedule else \
        ("threshold", "window_minutes", "cooldown_minutes")
    knobs: dict[str, dict] = {}
    for k in w.get("knobs") or []:
        if not isinstance(k, dict):
            continue
        kid = str(k.get("id") or "")
        if kid not in KNOB_IDS:
            errors.append(f"{where}: unknown knob {kid!r}; knobs are {', '.join(KNOB_IDS)}")
            continue
        if kid not in wanted:
            continue
        try:
            value = float(k.get("value"))
        except (TypeError, ValueError):
            errors.append(f"{where}: knob {kid} needs a number")
            continue
        lo, hi = _KNOB_DEFAULTS[kid]
        try:
            lo = float(k["min"]) if k.get("min") is not None else lo
            hi = float(k["max"]) if k.get("max") is not None else hi
        except (TypeError, ValueError):
            pass
        if lo > hi:
            lo, hi = hi, lo
        # the threshold keeps the model's unit ("failed logins"); the time knobs always say what
        # they mean, since "minutes" alone reads the same for the window and the wait after
        label = (str(k.get("label") or "").strip() if kid == "threshold" else "") or _knob_label(kid, w)
        knobs[kid] = {"id": kid, "label": label,
                      "value": _num(min(max(value, lo), hi)), "min": _num(lo), "max": _num(hi)}
    prev_knobs = {k.get("id"): k for k in (prev or {}).get("knobs") or [] if isinstance(k, dict)}
    for kid in wanted:
        d = _derived(kid, w)
        if kid not in knobs:
            if d is None:
                continue
            lo, hi = _KNOB_DEFAULTS[kid]
            knobs[kid] = {"id": kid, "label": _knob_label(kid, w), "value": d,
                          "min": _num(min(lo, d)), "max": _num(max(hi, d))}
        elif prev is not None and kid in prev_knobs:
            pv = prev_knobs[kid].get("value")
            if knobs[kid]["value"] == pv and d is not None and d != _derived(kid, prev):
                knobs[kid]["value"] = _num(min(max(d, knobs[kid]["min"]), knobs[kid]["max"]))
    c = dict(w.get("condition") or {})
    for kid, k in knobs.items():
        v = k["value"]
        if kid == "threshold":
            p = G._predicate(c.get("predicate"))
            c["predicate"] = f"{p[0] if p else '>'} {v}"
        elif kid == "window_minutes":
            c["window"] = f"{int(v)}m"
        elif kid == "cooldown_minutes":
            w["cooldown"] = f"{int(v)}m"
        elif kid == "every_minutes":
            c["every"] = f"{int(v)}m"
    w["condition"] = c
    w["knobs"] = [knobs[k] for k in wanted if k in knobs]


def clause(trig, sources: dict, knobs: list) -> str:
    """"checkout-errors gets more than 5 errors in 5 minutes for one service": goal.py's
    phrasing, with the threshold knob's unit for a count."""
    if getattr(trig.condition, "every", None):
        every = next((k for k in knobs if k.get("id") == "every_minutes"), None)
        if every is None:
            return G.schedule_words(trig)
        # in the knob's own unit, so the number a person tunes is the number in the sentence
        n = G._num(every["value"])
        filt = G.filters_words(trig.filters)
        return (f"every {n} {'minute' if n == '1' else 'minutes'}"
                + (f", for events{filt}" if filt else ""))
    text = G.condition_clause(trig, sources)
    th = next((k for k in knobs if k.get("id") == "threshold"), None)
    if th and trig.condition.aggregate == "count" and th.get("label"):
        n = G._num(th["value"])
        text = re.sub(rf"\b{re.escape(n)} events?\b", f"{n} {th['label']}", text, count=1)
    return text


def _lead(trig, sources, knobs) -> str:
    c = clause(trig, sources, knobs)
    return _cap(c) if getattr(trig.condition, "every", None) else f"When {c}"


def own_agent_words(name) -> str:
    """How a sentence names the person's own agent: "your agent claude-code", or the name alone
    when it already says agent ("your own agent" reads "your agent", "support-agent" stays)."""
    raw = " ".join(str(name or "").split())
    low = " ".join(re.sub(r"[-_]+", " ", raw).split()).lower()
    if not low or re.fullmatch(r"(?:(?:my|your|our|the)\s+)?(?:own\s+)?agent", low):
        return "your agent"
    if re.search(r"\bagent\b", low):
        return raw
    return f"your agent {raw}"


def own_sentence(own: dict) -> str:
    who = _cap(own_agent_words(own.get("name")))
    return (f"{who} is woken at its webhook with what happened." if own.get("wake") != "poll"
            else f"{who} checks the project for what happened.")


def summary(plan: dict, trigs: dict, sources: dict) -> str:
    """The plan in one or two sentences, from the plan as it is now (never the model's text)."""
    out = []
    agents = [a for a in plan.get("agents") or [] if a.get("enabled", True)] \
        if plan.get("who") == "tares" else []
    by_name = {a["name"]: a for a in agents}
    own = own_agent_words((plan.get("own_agent") or {}).get("name"))
    for w in plan.get("wakes") or []:
        trig = trigs.get(w["name"])
        if trig is None:
            continue
        lead = _lead(trig, sources, w.get("knobs") or [])
        if plan.get("who") == "own":
            out.append(f"{lead}, {own} is told.")
            continue
        first = [a for a in agents if a.get("trigger") == w["name"] and a.get("on_trigger", True)]
        if not first:
            out.append(f"{lead}, no agent is turned on to look yet.")
            continue
        names = [a["name"] for a in first]
        s = f"{lead}, {G.join_words(names)} {'looks' if len(names) == 1 else 'look'} first."
        seen, queue = set(), [(a, 1) for a in first]
        while queue:
            a, depth = queue.pop(0)
            for h in a.get("handoffs") or []:
                edge = (a["name"], h.get("agent"))
                if edge in seen or h.get("agent") not in by_name:
                    continue
                seen.add(edge)
                subject = "it" if depth == 1 and len(first) == 1 else a["name"]
                s += f" If {subject} concludes {h.get('verdict')}, {h.get('agent')} digs in."
                queue.append((by_name[h["agent"]], depth + 1))
        out.append(s)
    return " ".join(out) or "Nothing wakes this project yet: it has no trigger."


def agent_sentence(a: dict, agents: list, trigs: dict, sources: dict, n_wakes: int) -> str:
    """What one Tares agent of the plan does, from its settings: looks first (on which wake-up
    when there are several), digs in on whose verdict, or is turned off."""
    name = a["name"]
    if not a.get("enabled", True):
        return f"{name} is turned off, so it is not set up."
    if a.get("on_trigger", True):
        trig = trigs.get(a.get("trigger"))
        if trig is not None and n_wakes > 1:
            return f"{name} looks first when {clause(trig, sources, [])}."
        return f"{name} looks first."
    by = [(frm, h) for frm in agents if frm.get("enabled", True) and frm is not a
          for h in frm.get("handoffs") or [] if h.get("agent") == name]
    if not by:
        return f"{name} runs only when another agent hands off to it, and none does yet."
    parts = []
    for frm, h in by:
        cd = str(h.get("cooldown") or "30m")
        try:
            cd = G.duration_words(parse_duration(cd))
        except (ValueError, KeyError, IndexError):
            pass
        parts.append(f"when {frm['name']} concludes {h.get('verdict')}, at most once every {cd}")
    return f"{name} digs in {' or '.join(parts)}."


# ── normalize: the catalog's validators in a dry run, plus the derived parts ─
def needs_for(connector: str, config: dict | None) -> str:
    """What a new source needs from the person: a push source is sent to; a polled source whose
    connector takes a secret and has none yet needs a credential."""
    spec = SPECS.get(connector, {})
    if spec.get("mode") == "push":
        return "send"
    secrets = [f["name"] for f in spec.get("fields") or [] if f.get("secret")]
    if secrets and not any((config or {}).get(n) for n in secrets):
        return "credential"
    return "none"


_ERR_PREFIX = re.compile(r"^(?:source|trigger|agent|skill)(?: name)? '[^']*'"
                         r"(?: (?:poll|every|window|cooldown))?:?\s*", re.I)


def _plain(e) -> str:
    """A catalog validator's message without its "trigger 'x':" prefix, as a sentence."""
    t = _ERR_PREFIX.sub("", str(e).strip()) or str(e).strip() or type(e).__name__
    if re.search(r"must start with http:// or https://", t, re.IGNORECASE):
        return "Add its address; it starts with http:// or https://."
    t = _cap(t)
    return t if t.endswith((".", "?", "!", ")")) else t + "."


class _Problems:
    """Problems per plan item: where ("watches.w1", "wakes.k1", "agents.a1", "tools.t1",
    "skills.s1", "own_agent", a section name for the section as a whole, or "plan"), a plain
    message, and the label the model's retry reads it under ("Trigger checkout-spike")."""

    def __init__(self):
        self.items: list[dict] = []

    def add(self, where: str, message: str, label: str = "") -> None:
        p = {"where": where, "message": message, "label": label}
        if p not in self.items:
            self.items.append(p)

    def texts(self) -> list[str]:
        return [f"{p['label']}: {p['message']}" if p["label"] else p["message"]
                for p in self.items]

    def public(self) -> list[dict]:
        return [{"where": p["where"], "message": p["message"]} for p in self.items]


_NUMERIC_OPS = {"gt", "gte", "lt", "lte"}


def _filter_problems(filters: list) -> list[str]:
    out = []
    for f in filters:
        field = f["field"]
        value = f.get("value")
        words = G._FILTER_WORDS.get(f.get("op"), f.get("op"))
        if not field:
            out.append("An \"only when\" line has no field: pick one, or remove the line.")
        elif not re.fullmatch(r"[A-Za-z0-9_.]+", field):
            out.append(f"{field} is not a field name Tares can check.")
        elif f.get("op") not in G._FILTER_WORDS:
            out.append(f"\"Only when {field}\" needs a comparison, such as = or above.")
        elif value is None or str(value).strip() == "":
            out.append(f"\"Only when {field} {words}\" has no value.")
        elif f.get("op") in _NUMERIC_OPS:
            try:
                float(value)
            except (TypeError, ValueError):
                out.append(f"\"Only when {field} {words}\" needs a number, not {value!r}.")
    return out


def normalize(raw, store, catalog, prev: dict | None = None) -> tuple[dict, list[str]]:
    """(plan, errors). The plan comes back in canonical shape: names made unique against the
    catalog (references follow a rename), configs normalized, knobs applied, sentences and the
    summary derived. `errors` are plain sentences; empty means the plan applies as it is."""
    plan, probs = _normalize(raw, store, catalog, prev)
    return plan, probs.texts()


def check(raw, store, catalog) -> tuple[dict, list[dict]]:
    """(plan, problems) for the console: the plan normalized exactly as apply normalizes it, and
    what stops it from applying, per item ({where, message}). No model call."""
    plan, probs = _normalize(raw, store, catalog, None)
    return plan, probs.public()


def _normalize(raw, store, catalog, prev: dict | None) -> tuple[dict, _Problems]:
    probs = _Problems()
    if not isinstance(raw, dict):
        probs.add("plan", "The plan is not an object.")
        return {}, probs
    plan = scrub(copy.deepcopy(raw))
    out: dict = {}

    try:
        out["goal"] = G.normalize_goal(plan.get("goal"))
    except ValueError as e:
        probs.add("plan", str(e)[:1].upper() + str(e)[1:] + ".")
        out["goal"] = " ".join(str(plan.get("goal")).split())
    if not out["goal"]:
        probs.add("plan", "The plan has no goal.")
    name = " ".join(str(plan.get("name") or "").split())[:80]
    if not name:
        probs.add("plan", "The plan has no project name.")
    taken_projects = {p["name"] for p in store.list_projects()}
    if name in taken_projects:
        n = 2
        while f"{name} {n}" in taken_projects:
            n += 1
        name = f"{name} {n}"
    out["name"] = name
    who = plan.get("who") if plan.get("who") in ("tares", "own") else None
    if who is None:
        probs.add("plan", "Say who does the work: tares or own.")
        who = "tares"
    out["who"] = who
    prev_wakes = {w.get("key"): w for w in (prev or {}).get("wakes") or [] if isinstance(w, dict)}

    # watches
    catalog_sources = catalog.sources
    src_map: dict[str, str] = {}
    taken = set(catalog_sources)
    watches, phr_sources, seen_watch = [], dict(catalog_sources), set()
    for i, w in enumerate(x for x in plan.get("watches") or [] if isinstance(x, dict)):
        w = dict(w)
        w["key"] = str(w.get("key") or f"w{i + 1}")
        w["existing"] = bool(w.get("existing"))
        orig = str(w.get("name") or "").strip()
        where, label = f"watches.{w['key']}", f"Source {orig or w['key']}"
        if orig and orig in seen_watch:
            probs.add(where, f"The plan already has a source called {orig}.", label)
        seen_watch.add(orig)
        if w["existing"]:
            cfg = catalog_sources.get(orig)
            if not orig:
                probs.add(where, "Pick the source to use.", label)
            elif cfg is None:
                probs.add(where, f"There is no source called {orig} on Tares; use one "
                                 "list_sources shows, or plan a new one.", label)
            else:
                w["connector"] = cfg.connector
            w["name"] = orig
            w["config"] = None
            w.pop("poll", None)
            src_map.setdefault(orig, orig)
            w["needs"] = "none"   # it is already connected
        else:
            nm = _unique(slug(orig) or f"source-{i + 1}", taken)
            taken.add(nm)
            src_map.setdefault(orig, nm)
            w["name"] = nm
            conn = str(w.get("connector") or "")
            spec = {"name": nm, "connector": conn, "config": w.get("config") or {}}
            if w.get("poll"):
                spec["poll"] = str(w["poll"])
            if not conn:
                probs.add(where, "Pick what kind of source this is.", label)
            elif SPECS.get(conn, {}).get("internal"):
                probs.add(where, f"{conn} is filled by Tares itself; pick another connector.",
                          label)
            else:
                try:
                    # a label the model gave neither a field nor a fixed value reads the event
                    # field of its own name (label service -> field service)
                    for lb in (spec["config"].get("labels") or []) if isinstance(spec["config"], dict) else []:
                        if isinstance(lb, dict) and lb.get("name") and not lb.get("field") \
                                and lb.get("const") in (None, ""):
                            lb.pop("const", None)
                            lb["field"] = lb["name"]
                    validate_source_dict(spec)
                    w["config"] = normalize_config(conn, spec["config"])
                    phr_sources[nm] = _source_from_dict({**spec, "config": w["config"]})
                except CatalogError as e:
                    probs.add(where, _plain(e), label)
                except Exception as e:  # noqa: BLE001 — a bad value in a config is the plan's error
                    probs.add(where, f"{type(e).__name__}: {e}", label)
            if w.get("needs") not in ("send", "credential", "none"):
                w["needs"] = needs_for(conn, w.get("config"))
        spec = SPECS.get(str(w.get("connector") or ""), {})
        sample = w.get("sample")
        w["sample"] = sample if isinstance(sample, dict) and spec.get("mode") == "push" else None
        kind = f"a new {spec['label']} source" if spec.get("label") else "a new source"
        w["sentence"] = " ".join(str(w.get("sentence") or "").split()) or (
            f"{w['name']}, a source already on Tares" if w["existing"] else f"{w['name']}, {kind}")
        watches.append(w)
    if not watches:
        probs.add("watches", "The plan watches nothing: add a source.")
    out["watches"] = watches
    watch_names = {w["name"] for w in watches}

    # tools
    servers = {m["name"]: m for m in store.list_mcp_servers()}
    default_uid = store.default_project_id()
    tool_map: dict[str, str] = {}
    tools, taken_tools = [], set()
    for i, t in enumerate(x for x in plan.get("tools") or [] if isinstance(x, dict)):
        t = dict(t)
        t["key"] = str(t.get("key") or f"t{i + 1}")
        orig = str(t.get("name") or "").strip()
        where = f"tools.{t['key']}"
        t["url"] = str(t.get("url") or "").strip()
        t["enabled"] = bool(t.get("enabled", False))
        t["can_act"] = bool(t.get("can_act", False))
        t["why"] = " ".join(str(t.get("why") or "").split())
        if t.get("existing"):
            # attached by name: a server already on the cell, used as it is
            nm = orig
            found = servers.get(nm)
            if found is None:
                probs.add(where, f"There is no MCP server called {nm or 'that'} on Tares; pick "
                                 "another.", f"Tool {nm}")
            else:
                t["url"] = found["url"]
            t["existing"] = True
        else:
            nm = slug(orig) or f"tool-{i + 1}"
            found = servers.get(nm)
            # an MCP server of that name already registered is reused when the plan names no
            # other address for it
            free = found is not None and found.get("owned_by") in (None, "", default_uid)
            if found is not None and t["url"] in ("", found["url"]) and free:
                t["url"] = found["url"]
                t["existing"] = True
            else:
                # another project's server of that name: this project gets its own, same address
                if found is not None and not t["url"]:
                    t["url"] = found["url"]
                nm = _unique(nm, set(servers) | taken_tools)
                t["existing"] = False
        # an MCP server belongs to one project: one already on Tares moves into this project when
        # it is unowned or in the default project, never out of another project that uses it
        if t["existing"] and t["enabled"] and found is not None:
            owner = found.get("owned_by")
            if owner and owner != default_uid:
                other = (store.get_project(owner) or {}).get("name") or owner
                probs.add(where, f"{nm} belongs to the project {other}; add a new one here, or "
                                 "turn it off.", f"Tool {nm}")
        taken_tools.add(nm)
        tool_map.setdefault(orig, nm)
        t["name"] = nm
        if t["enabled"] and not t["existing"]:
            if not t["url"]:
                probs.add(where, "It is turned on but has no address; add its URL or turn it "
                                 "off.", f"Tool {nm}")
            else:
                try:
                    validate_mcp_server_dict({"name": nm, "url": t["url"]})
                except CatalogError as e:
                    probs.add(where, _plain(e), f"Tool {nm}")
        tools.append(t)
    out["tools"] = tools

    # skills
    skills, seen_skills = [], set()
    for i, sk in enumerate(x for x in plan.get("skills") or [] if isinstance(x, dict)):
        sk = dict(sk)
        sk["key"] = str(sk.get("key") or f"s{i + 1}")
        sk["name"] = _unique(slug(sk.get("name")) or f"skill-{i + 1}", seen_skills)
        seen_skills.add(sk["name"])
        sk["enabled"] = bool(sk.get("enabled", True))
        try:
            _n, sk["description"], sk["body"] = _skills.validate(sk["name"], sk.get("description"),
                                                                 sk.get("body"))
        except _skills.SkillError as e:
            probs.add(f"skills.{sk['key']}", _plain(e), f"Skill {sk['name']}")
        skills.append(sk)
    out["skills"] = skills

    # wakes
    trig_map: dict[str, str] = {}
    taken = {t.name for t in catalog.triggers}
    wakes, trig_dicts, trigs, seen_wake = [], {}, {}, set()
    for i, w in enumerate(x for x in plan.get("wakes") or [] if isinstance(x, dict)):
        w = dict(w)
        w["key"] = str(w.get("key") or f"k{i + 1}")
        orig = str(w.get("name") or "").strip()
        nm = _unique(slug(orig) or f"wake-{i + 1}", taken)
        taken.add(nm)
        trig_map.setdefault(orig, nm)
        w["name"] = nm
        where, label = f"wakes.{w['key']}", f"Trigger {nm}"
        if orig and orig in seen_wake:
            probs.add(where, f"Another wake-up is already called {orig}.", label)
        seen_wake.add(orig)
        w["sources"] = [src_map.get(str(s), str(s)) for s in (w.get("sources") or [])]
        plain: list[str] = []
        if not w["sources"]:
            plain.append("It has no source to watch: pick one, or remove this wake-up.")
        for s in w["sources"]:
            if s not in watch_names:
                plain.append(f"{s} is not a source of this plan.")
        filters = []
        for f in w.get("filters") or []:
            if not isinstance(f, dict):
                continue
            f = {"field": str(f.get("field") or "").strip(), "op": f.get("op") or "eq",
                 "value": f.get("value")}
            if f["op"] in _NUMERIC_OPS and isinstance(f["value"], str):
                try:
                    f["value"] = _num(f["value"])
                except ValueError:
                    pass
            filters.append(f)
        w["filters"] = filters
        plain += _filter_problems(filters)
        w["key_field"] = str(w.get("key_field") or "").strip()
        w["cooldown"] = str(w.get("cooldown") or "5m")
        w["window"] = str(w.get("window") or "15m")
        if not isinstance(w.get("condition"), dict):
            plain.append("It has no condition.")
            w["condition"] = {"aggregate": "count", "predicate": "> 0", "window": "5m"}
        c = w["condition"]
        if not c.get("every"):
            agg = c.get("aggregate") or "count"
            if agg != "count" and not str(c.get("field") or "").strip():
                plain.append(f"Pick the number to take the {G._AGG_WORDS.get(agg, agg)} of.")
        knob_errors: list[str] = []
        apply_knobs(w, prev_wakes.get(w["key"]), knob_errors, label)
        plain += [e.split(": ", 1)[-1] for e in knob_errors]
        for msg in plain:
            probs.add(where, _cap(msg), label)
        if plain:
            wakes.append(w)
            continue
        tdict = {"name": nm, "sources": w["sources"], "condition": w["condition"],
                 "filters": w["filters"], "key_field": w["key_field"], "cooldown": w["cooldown"],
                 "emit": {"context_window": w["window"]}}
        try:
            validate_trigger_dict(tdict, watch_names | set(catalog_sources))
            parse_duration(w["window"])
            trig = _trigger_from_dict(tdict)
            trig_dicts[nm], trigs[nm] = tdict, trig
            w["sentence"] = _cap(clause(trig, phr_sources, w["knobs"])) + "."
            w["cooldown_sentence"] = G.cooldown_sentence(trig, phr_sources)
        except CatalogError as e:
            probs.add(where, _plain(e), label)
        except Exception as e:  # noqa: BLE001
            probs.add(where, f"{type(e).__name__}: {e}", label)
        wakes.append(w)
    if not wakes:
        probs.add("wakes", "Nothing wakes the project: add a wake-up.")
    out["wakes"] = wakes
    wake_names = [w["name"] for w in wakes]

    # agents
    agent_map: dict[str, str] = {}
    taken = {a["name"] for a in store.list_catalog_agents()}
    agents, names = [], []
    raw_agents = [x for x in plan.get("agents") or [] if isinstance(x, dict)]
    for i, a in enumerate(raw_agents):
        orig = str(a.get("name") or "").strip()
        nm = _unique(slug(orig) or f"agent-{i + 1}", taken)
        taken.add(nm)
        names.append(nm)
        agent_map.setdefault(orig, nm)
        # a model sometimes names the handoff target by the plan's key ("a2") instead
        if a.get("key"):
            agent_map.setdefault(str(a["key"]), nm)
    seen_agent = set()
    for i, a in enumerate(raw_agents):
        a = dict(a)
        a["key"] = str(a.get("key") or f"a{i + 1}")
        orig = str(a.get("name") or "").strip()
        a["name"] = names[i]
        if who == "tares" and orig and orig in seen_agent:
            probs.add(f"agents.{a['key']}", f"Another agent is already called {orig}; give "
                                             "this one its own name.", f"Agent {a['name']}")
        seen_agent.add(orig)
        a["trigger"] = trig_map.get(str(a.get("trigger") or ""), str(a.get("trigger") or ""))
        a["on_trigger"] = bool(a.get("on_trigger", True))
        if not a["on_trigger"] and a["trigger"] not in wake_names and wake_names:
            # a handoff-only agent is never woken by the trigger it sits on: Tares picks one
            a["trigger"] = wake_names[0]
        a["optional"] = bool(a.get("optional", False))
        a["enabled"] = bool(a.get("enabled", True))
        a["model"] = (str(a["model"]).strip() or None) if a.get("model") else None
        a["provider"] = (str(a["provider"]).strip() or None) if a.get("provider") else None
        a["prompt"] = str(a.get("prompt") or "").strip()
        a["mcp_servers"] = list(dict.fromkeys(tool_map.get(str(x), str(x))
                                              for x in a.get("mcp_servers") or []))
        hs = []
        for h in a.get("handoffs") or []:
            if isinstance(h, dict):
                target = str(h.get("agent") or "").strip()
                hs.append({**h, "agent": agent_map.get(target, target)})
        a["handoffs"] = hs
        agents.append(a)
    out["agents"] = agents
    if who == "tares":
        if len(agents) > MAX_AGENTS:
            probs.add("agents", f"At most {MAX_AGENTS} agents; this plan has {len(agents)}.")
        live = [a for a in agents if a["enabled"]]
        if not any(a["on_trigger"] for a in live):
            probs.add("agents", "No agent looks first: turn on an agent that runs when the "
                                "project wakes.")
        known_servers = set(servers) | {t["name"] for t in tools}
        all_names = {x["name"] for x in agents}
        for a in live:
            where, label = f"agents.{a['key']}", f"Agent {a['name']}"
            plain = []
            if not a["prompt"]:
                plain.append(f"Say what {a['name']} should do: its instructions are empty.")
            if a["trigger"] not in wake_names:
                plain.append("Pick the wake-up that starts it." if not a["trigger"] else
                             f"It starts on {a['trigger']}, which is not a wake-up of this plan.")
            for h in a["handoffs"]:
                if not h["agent"]:
                    plain.append("A handoff names no agent: pick one, or remove it.")
                elif h["agent"] not in all_names:
                    plain.append(f"It hands off to {h['agent']}, which is not an agent of this "
                                 "plan.")
            for x in a["mcp_servers"]:
                if x not in known_servers:
                    plain.append(f"It uses the tool {x}, which is not in this plan.")
            for msg in plain:
                probs.add(where, msg, label)
            if plain or a["trigger"] not in trig_dicts:
                continue
            try:
                a["handoffs"] = normalize_handoffs(a["name"], a["handoffs"])
                check_handoff_targets(a["name"], a["handoffs"], {n: None for n in all_names})
                validate_agent_dict({**a, "model": a["model"] or "",
                                     "provider": a["provider"] or ""}, set(trig_dicts),
                                    trig_dicts, known_servers)
            except CatalogError as e:
                probs.add(where, _plain(e), label)
        for a in agents:
            a["sentence"] = agent_sentence(a, agents, trigs, phr_sources, len(wakes))

    own = plan.get("own_agent")
    if who == "own":
        own = dict(own) if isinstance(own, dict) else {}
        own["name"] = " ".join(str(own.get("name") or "").split())[:64] or "my-agent"
        own["wake"] = own.get("wake") if own.get("wake") in ("webhook", "poll") else "webhook"
        own["sentence"] = own_sentence(own)
        if store.get_catalog_agent(own["name"]) is not None:
            probs.add("own_agent", f"The name {own['name']} is taken by a Tares agent; pick "
                                   "another.", "Your agent")
    out["own_agent"] = own if who == "own" else (own if isinstance(own, dict) else None)
    out["notes"] = [" ".join(str(n).split()) for n in plan.get("notes") or [] if str(n).strip()]
    out["summary"] = summary(out, trigs, phr_sources)
    return out, probs


def stored_plan(plan: dict) -> dict:
    """The plan as the project keeps it: a secret typed into a new source's settings is left
    out, since the console reads the setup back."""
    p = copy.deepcopy(plan)
    for w in p.get("watches") or []:
        cfg = w.get("config")
        if isinstance(cfg, dict):
            for n in secret_field_names(w.get("connector") or ""):
                if cfg.get(n):
                    cfg[n] = ""
    return p


# ── the model ────────────────────────────────────────────────────────────────
def _context(store, catalog, existing_sources: bool) -> str:
    """What is here, so most plans need no read at all: connectors, sources and templates."""
    conns = [f"- {n}: {s.get('label')}, {s.get('mode')}" for n, s in SPECS.items()
             if not s.get("internal")]
    srcs = []
    if existing_sources:
        for n, cfg in sorted(catalog.sources.items()):
            if SPECS.get(cfg.connector, {}).get("internal"):
                continue
            labels = [l.get("name") + (" (primary)" if l.get("primary") else "")
                      for l in (cfg.config.get("labels") or []) if isinstance(l, dict)]
            srcs.append(f"- {n}: {cfg.connector}"
                        + (f", labels {', '.join(labels)}" if labels else ""))
    out = "Connectors:\n" + "\n".join(conns)
    out += "\n\nExisting sources:\n" + ("\n".join(srcs) if srcs else
                                        ("- none" if existing_sources else
                                         "- do not reuse any; plan new sources"))
    return out


async def _run_model(provider, model: str, convo: list, read_tool, tracer, usage: dict,
                     forced_only: bool = False) -> tuple[dict, object]:
    """Model calls until propose_plan comes back: reads first when the model wants them, the
    last call forced to propose_plan. Returns (the plan it proposed, that reply)."""
    tools = read_tools() + [PLAN_TOOL]
    rounds = 1 if forced_only else MAX_ROUNDS
    for i in range(rounds):
        last = i == rounds - 1
        reply = await provider.complete(model=model, system=SYSTEM, tools=tools, messages=convo,
                                        max_tokens=8000, tracer=tracer,
                                        tool_choice="propose_plan" if last else "any")
        add_usage(usage, reply.usage)
        convo.append(reply.as_message())
        call = next((c for c in reply.tool_calls if c.name == "propose_plan"), None)
        if call is not None:
            return (call.arguments if isinstance(call.arguments, dict) else {}), (reply, call)
        results = []
        for c in reply.tool_calls:
            ok, text = await read_tool(c.name, c.arguments or {})
            results.append((c.id, text if ok else f"error: {text}"))
        if results:
            convo.append(tool_message(results))
        else:
            convo.append({"role": "user", "content": "Call propose_plan now with the whole plan."})
    raise SetupError(502, "The model did not return a plan. Try again, or start from a template.")


async def model_plan(provider, model: str, first_message: str, store, catalog, read_tool,
                     tracer=None, on_usage=None, prev: dict | None = None,
                     who: str | None = None) -> dict:
    """The plan from the model: generate, normalize, and on errors one retry with them. Raises
    SetupError(422) when the second plan is still invalid. `who`: the choice the person made,
    which the plan must keep."""
    from . import tracing as _tracing
    with _tracing.run_span(tracer, "project-setup", kind="CHAIN", agent="project-setup") as obs:
        obs.set_input(first_message)
        plan = await _model_plan(provider, model, first_message, store, catalog, read_tool,
                                 tracer, on_usage, prev, who)
        obs.set_output(json.dumps(plan, default=str)[:4000])
        return plan


def _check(raw, store, catalog, prev, who) -> tuple[dict, list]:
    plan, errors = normalize(raw, store, catalog, prev)
    if who and plan.get("who") != who:
        errors.append(f"The person chose who {who}; keep it.")
    return plan, errors


async def _model_plan(provider, model, first_message, store, catalog, read_tool, tracer,
                      on_usage, prev, who) -> dict:
    usage = empty_usage()
    convo = [{"role": "user", "content": first_message}]
    try:
        raw, (reply, call) = await _run_model(provider, model, convo, read_tool, tracer, usage)
        plan, errors = _check(raw, store, catalog, prev, who)
        if errors:
            convo.append(tool_message([(call.id, "The plan cannot be applied:\n- "
                                        + "\n- ".join(errors)
                                        + "\nCall propose_plan again with the whole plan, "
                                          "fixed.")]))
            raw, _ = await _run_model(provider, model, convo, read_tool, tracer, usage,
                                      forced_only=True)
            plan, errors = _check(raw, store, catalog, prev, who)
            # still not right: the plan opens anyway when there is something to show, and the
            # plan screen names each problem on its card for the person to fix in place
            # (who the person chose is not something to fix by hand: a plan that ignores it is refused)
            wrong_who = bool(who and plan.get("who") != who)
            if errors and (wrong_who or not (plan.get("watches") or plan.get("wakes")
                                             or plan.get("agents"))):
                raise SetupError(422, (f"The plan did not keep who does the work (who {who}). "
                                       "Try again." if wrong_who else
                                       "The plan did not come out right. Try saying the goal "
                                       "another way, or start from a template."))
        return plan
    except (ModelError, ModelUnavailable) as e:
        raise SetupError(502, f"The model provider did not answer: {e}") from e
    finally:
        if usage["calls"] and on_usage is not None:
            try:
                on_usage(usage)
            except Exception as e:  # metering never breaks the setup
                print(f"setup: usage record failed: {type(e).__name__}: {e}")


def plan_message(goal: str, who: str | None, existing_sources: bool, store, catalog) -> str:
    who_line = {"tares": "Tares agents do the work (who tares).",
                "own": "The person's own agent does the work (who own)."}.get(
        who or "", "Pick who does the work: Tares agents unless the goal says otherwise.")
    return (f"Goal: {goal}\n{who_line}\n\n{_context(store, catalog, existing_sources)}")


def adjust_message(plan: dict, instruction: str) -> str:
    return ("Here is the current plan:\n" + json.dumps(plan, indent=1, default=str)
            + f"\n\nChange it as the person asks: {instruction}\n"
            "Keep everything else and every key as it is. Change numbers through the knob "
            "values. Call propose_plan with the whole revised plan.")


# ── apply ────────────────────────────────────────────────────────────────────
_STEP_WORDS = {"project": "creating the project", "sources": "adding the sources",
               "tools": "adding the tools", "skills": "adding the know-how",
               "triggers": "adding the triggers", "agents": "adding the agents",
               "key": "making your agent's key"}


def apply(store, engine, plan: dict, make_key) -> tuple[str, dict | None]:
    """Create the project and its objects from a normalized plan, in one go: sources, tools, know
    how, triggers, agents (or, for the person's own agent, a project key). Returns (project id,
    the new key or None). A failing step undoes everything this call made and raises
    SetupError naming the step."""
    from .projects.base import PlannedObject, ProjectError
    step = "project"
    try:
        proj = engine.create("custom", {"objects": []}, name=plan["name"], goal=plan["goal"])
    except ProjectError as e:
        raise SetupError(409 if "already exists" in str(e) else 400,
                         f"Setting up stopped while creating the project: {e}") from e
    uid = proj["id"]
    made: list = []      # PlannedObjects this call created, for the undo
    objects: list = []   # the custom project's {kind, name} list
    key = None
    try:
        step = "sources"
        new = []
        for w in plan["watches"]:
            if w["existing"]:
                store.put_in_project("source", w["name"], uid, key=f"source:{w['name']}")
            else:
                spec = {"name": w["name"], "connector": w["connector"], "config": w["config"]}
                if w.get("poll"):
                    spec["poll"] = w["poll"]
                new.append(PlannedObject("source", f"source:{w['name']}", spec))
            objects.append({"kind": "source", "name": w["name"]})
        made += new
        engine._apply(uid, new)

        step = "tools"
        tools = [t for t in plan["tools"] if t["enabled"] and not t.get("existing")]
        objs = [PlannedObject("mcp_server", f"mcp_server:{t['name']}",
                              {"name": t["name"], "url": t["url"]}) for t in tools]
        made += objs
        engine._apply(uid, objs)
        objects += [{"kind": "mcp_server", "name": t["name"]} for t in tools]
        # one already on Tares moves into this project (check refused one another project owns)
        for t in plan["tools"]:
            if t["enabled"] and t.get("existing"):
                store.put_in_project("mcp_server", t["name"], uid, key=f"mcp_server:{t['name']}")
                objects.append({"kind": "mcp_server", "name": t["name"]})
        usable_tools = {t["name"] for t in plan["tools"] if t["enabled"]}

        step = "skills"
        objs = [PlannedObject("skill", f"skill:{s['name']}",
                              {"name": s["name"], "description": s["description"],
                               "body": s["body"]})
                for s in plan["skills"] if s["enabled"]]
        made += objs
        engine._apply(uid, objs)

        step = "triggers"
        objs = []
        for w in plan["wakes"]:
            objs.append(PlannedObject("trigger", f"trigger:{w['name']}", {
                "name": w["name"], "sources": w["sources"], "condition": w["condition"],
                "filters": w["filters"], "key_field": w["key_field"], "cooldown": w["cooldown"],
                "emit": {"context_window": w["window"]}}))
            objects.append({"kind": "trigger", "name": w["name"]})
        made += objs
        engine._apply(uid, objs)

        if plan["who"] == "tares":
            step = "agents"
            live = [a for a in plan["agents"] if a["enabled"]]
            names = {a["name"] for a in live}
            objs = []
            for a in live:
                spec = {"name": a["name"], "trigger": a["trigger"], "prompt": a["prompt"],
                        "mcp_servers": [s for s in a["mcp_servers"] if s in usable_tools],
                        "handoffs": [h for h in a["handoffs"] if h["agent"] in names],
                        # on the trigger it runs when the project wakes; a handoff-only agent
                        # is left off for it and runs when another hands off
                        "enabled": bool(a["on_trigger"])}
                if a.get("model"):
                    spec["model"] = a["model"]
                if a.get("provider"):
                    spec["provider"] = a["provider"]
                objs.append(PlannedObject("agent", f"agent:{a['name']}", spec))
                objects.append({"kind": "agent", "name": a["name"]})
            made += objs
            engine._apply(uid, objs)
        else:
            step = "key"
            key = make_key(plan["own_agent"]["name"], ["findings", "read"], uid)

        store.update_project(uid, params={"objects": objects})
        engine._do_reload()
    except Exception as e:
        try:
            engine._delete_objects(made, purge_events=False, uid=uid)
            store.delete_project(uid)   # revokes a key made here too
            store.normalize_projects()  # a reused source left in no project goes home
            engine._do_reload()
        except Exception as undo:  # noqa: BLE001
            print(f"setup: undo after a failed apply: {type(undo).__name__}: {undo}")
        detail = str(e) or type(e).__name__
        raise SetupError(400, f"Setting up stopped while {_STEP_WORDS[step]}: {detail}. "
                              "Nothing was kept, so you can change the plan and try "
                              "again.") from e
    return uid, key


# ── connect: what only the person can do ─────────────────────────────────────
def public_base(request_base: str) -> str:
    return (os.getenv("TARES_PUBLIC_URL", "").strip() or request_base).rstrip("/")


def mcp_url(base: str) -> str:
    return os.getenv("TARES_MCP_URL", "").strip() or f"{base}/mcp"


def _credential_hint(w: dict) -> str:
    fields = sorted(secret_field_names(w.get("connector") or ""))
    what = G.join_words(fields) if fields else "a credential"
    return (f"{w['name']} needs {what} to collect events. Add it under Setup, Advanced setup, "
            f"on the source {w['name']}.")


def connect_info(store, catalog, uid: str, plan: dict, base: str, key: dict | None) -> dict:
    sources = []
    for w in plan.get("watches") or []:
        if w.get("needs") not in ("send", "credential"):
            continue
        cfg = catalog.sources.get(w["name"])
        push = SPECS.get(w.get("connector") or "", {}).get("mode") == "push"
        ingest = f"{base}/ingest/{(cfg.ingest_key if cfg else '') or w['name']}" if push else None
        sources.append({"name": w["name"], "needs": w["needs"], "ingest_url": ingest,
                        "sample": w.get("sample"),
                        "credential_hint": _credential_hint(w)
                        if w["needs"] == "credential" else None})
    tools = []
    for t in plan.get("tools") or []:
        if not t.get("enabled"):
            continue
        m = store.get_mcp_server(t["name"]) or {}
        tools.append({"name": t["name"], "url": m.get("url") or t.get("url") or "",
                      "needs_token": not bool(m.get("auth_value"))})
    own = None
    # on a resume (no key in hand: it was shown once) the own agent's details still come back,
    # with a placeholder where the key goes
    if key is not None or (plan.get("who") == "own" and plan.get("own_agent")):
        secret = key["secret"] if key is not None else None
        url = mcp_url(base)
        wake = (plan.get("own_agent") or {}).get("wake") or "webhook"
        hint = (f"Your agent subscribes once: POST {base}/api/projects/{uid}/subscribe with the "
                "body {\"url\": \"<its webhook>\"} and the key. Tares then posts to that URL each "
                "time the project wakes." if wake == "webhook" else
                f"Your agent checks the project with GET {base}/api/projects/{uid}/timeline and "
                f"records what it finds with POST {base}/api/projects/{uid}/findings, both "
                "with the key.")
        own = {"key": secret, "mcp_url": url,
               "claude_command": (f"claude mcp add --transport http tares {url} --header "
                                  f"\"Authorization: Bearer {secret or '<your project key>'}\""),
               "subscribe_hint": hint}
    return {"sources": sources, "tools": tools, "own_agent": own}


# ── checks: live status for the Connect step ─────────────────────────────────
def _aware(dt):
    return G._aware(dt)


def checks(store, catalog, runtime_health: dict, uid: str, setup: dict, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    plan = setup.get("plan") or {}
    health = G._health_for(runtime_health, store, [w["name"] for w in plan.get("watches") or []])
    sources = []
    for w in plan.get("watches") or []:
        cfg = catalog.sources.get(w["name"])
        if cfg is None:
            sources.append({"name": w["name"], "state": "error",
                            "detail": "This source was deleted", "last_event_at": None,
                            "fields_seen": []})
            continue
        st = G.source_state(store, cfg, health.get(w["name"]), now)
        state = {"error": "error", "waiting": "waiting", "paused": "waiting"}.get(st["state"],
                                                                                 "receiving")
        fields = []
        if st["last_event_at"] is not None:
            seen = set()
            for p in store.recent_payloads(w["name"], 50):
                if isinstance(p, dict):
                    seen.update(str(k) for k in p)
            fields = sorted(seen)
        sources.append({"name": w["name"], "state": state, "detail": st["detail"],
                        "last_event_at": st["last_event_at"], "fields_seen": fields})
    tools = []
    tests = setup.get("tool_tests") or {}
    for t in plan.get("tools") or []:
        if not t.get("enabled"):
            continue
        m = store.get_mcp_server(t["name"])
        rec = tests.get(t["name"])
        if m is None:
            tools.append({"name": t["name"], "state": "error", "detail": "This tool was deleted"})
        elif rec is None or (m.get("updated_at") and rec.get("at")
                             and _aware(m["updated_at"]) > _aware(rec["at"])):
            tools.append({"name": t["name"], "state": "untested",
                          "detail": "Not tested yet" if rec is None else
                          "Changed since the last test"})
        elif rec.get("ok"):
            n = int(rec.get("tools") or 0)
            tools.append({"name": t["name"], "state": "ok",
                          "detail": f"Connected, {n} tool{'s' if n != 1 else ''} offered"})
        else:
            tools.append({"name": t["name"], "state": "error",
                          "detail": f"Could not connect: {rec.get('error') or 'no answer'}"})
    own = None
    if plan.get("who") == "own":
        key = store.get_api_key(setup.get("own_key_id") or "") if setup.get("own_key_id") else None
        used = _aware(key.get("last_used_at")) if key else None
        if key is None or key.get("revoked_at"):
            own = {"state": "waiting", "detail": "The key was revoked; make a new one under "
                                                 "Setup, Advanced setup"}
        elif used is None:
            own = {"state": "waiting", "detail": "Waiting for your agent to use its key"}
        else:
            subscribed = any(s.get("created_by") == f"key:{key['id']}"
                             for s in store.list_project_subscriptions(uid))
            age = G.age_words((now - used).total_seconds())
            own = {"state": "joined",
                   "detail": f"Your agent used its key {age}"
                             + (" and subscribed to the project" if subscribed else
                                ", it has not subscribed to the project yet")}
    return {"sources": sources, "tools": tools, "own_agent": own}
