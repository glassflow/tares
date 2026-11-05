"""The factory Settings page (TR-738): GET /api/factory/settings.

One read gives the whole page: the tools the crew works with, how far it may go and what it was
allowed, the model and budget, the keys, the edition and the links to everything else. Nothing is
stored here. Each part reads what the cell already keeps (the crew's settings, the grants
documents, the connections, the keys) and words it for people, so every string is shown as it is.
The words of a grant are the person's own and are exempt from the word check; a grant that only
says "D79 A" carries the question of that desk decision beside it.

A change goes through the routes that exist: PUT /api/crew/settings with the one field changed,
POST /api/grants, PUT /api/settings/edition. `can_change` is false for a read key; those routes
refuse it anyway, and the page dims its controls.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

from fastapi import Request

from .. import edition as edition_mod
from .. import linear as linear_mod
from .. import providers as providers_mod
from .. import builtin_agents
from .. import factory_tools
from ..envelope import now_utc
from ..factory_home import _count, _dt, _how_far, for_caller, plain_ago
from .factory_docs import ago

GRANT_LINE = re.compile(r'^\s*-\s+"(.*)"\s+\((\d{4}-\d{2}-\d{2})\)\s*$')
DESK_ANSWER = re.compile(r"^(D\d+)(?:\s+\S{1,20})?$")
MAX_GRANTS = 200
REPORTING_DAYS = 7
ADV = "/settings/advanced"
ADVANCED = [
    {"label": "All settings", "text": "Model providers, agent limits, tracing and the rest.",
     "href": ADV},
    {"label": "Connections", "text": "The services this cell reads from and how each is set up.",
     "href": "/sources"},
    {"label": "Explore your data", "text": "Search what the cell has stored.", "href": "/explore"},
    {"label": "All resources", "text": "Every part of the cell in one list.", "href": "/resources"},
    {"label": "Connect an agent", "text": "Let an outside agent read from this cell.",
     "href": "/connect"},
    {"label": "Reads", "text": "What outside agents have read.", "href": "/reads"},
]
OTHER = {"factory": "sre", "sre": "factory", "platform": "factory"}


def _text(v) -> str:
    return " ".join(str(v or "").split())


def pace_text(p: dict) -> str:
    """The pacing line in words a person uses: off, on with room, or why the crew is slowed."""
    if not p.get("on"):
        return "Pacing is off. The crew keeps starting builders whatever the plan has left."
    if not p.get("known"):
        return "Pacing is on. No reading of the plan's limits has arrived yet."
    causes = []
    session, weekly = p.get("session_pct"), p.get("weekly_pct")
    if p.get("state") in ("slow", "hold", "limit"):
        if isinstance(session, (int, float)) and session >= 85:
            causes.append(f"the 5-hour window is at {session:g}%")
        if isinstance(weekly, (int, float)) and weekly >= 90:
            causes.append(f"the weekly limit is at {weekly:g}%")
    if not causes:
        return "Pacing is on. The plan has room."
    do = ("builders finish their step and wait" if p.get("level") == 2
          else "no new builder starts")
    return f"Pacing is on, and {' and '.join(causes)}, so {do}."


def register(app, store, h, ctx):
    def safe(fn, default):
        try:
            return fn()
        except Exception:
            return default

    # ── grants ───────────────────────────────────────────────────────────────
    def grants(now) -> tuple[list[dict], int]:
        shared_id = store.get_setting("global_grants")
        docs = []   # (doc id, project name or None)
        if shared_id:
            docs.append((shared_id, None))
        hidden = h.hidden_ids()
        for p in safe(store.list_projects, []) or []:
            if p["id"] in hidden:
                continue   # a private project's grants name its repos and people
            for d in safe(lambda: store.list_docs(p["id"], "grants"), []) or []:
                if d["id"] != shared_id and (d["id"], p["name"]) not in docs:
                    docs.append((d["id"], p["name"]))
        questions = {}
        for d in safe(lambda: store.list_decisions(None), []) or []:
            if d.get("label") and d["label"] not in questions and d.get("project") not in hidden:
                questions[d["label"]] = _text(d.get("question"))
        out = []
        for doc_id, project in docs:
            body = (safe(lambda: store.get_doc(None, doc_id), None) or {}).get("body") or ""
            lines = [m for m in (GRANT_LINE.match(x) for x in str(body).splitlines()) if m]
            for i, m in reversed(list(enumerate(lines))):   # newest line of a document first
                words, day = m.group(1), m.group(2)
                try:
                    when = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
                except ValueError:
                    when = None
                g = {"id": f"{doc_id}:{i}", "text": words, "project": project,
                     "scope_text": f"In {project}" if project else "For every project",
                     "given": day,
                     "state_text": ("given " + ("today" if when and when.date() >= now.date()
                                                 else ago(when, now))) if when else "",
                     "_sort": day}
                a = DESK_ANSWER.match(words.strip())
                if a and questions.get(a.group(1)):
                    g["question"] = questions[a.group(1)]
                out.append(g)
        out.sort(key=lambda g: g["_sort"], reverse=True)
        for g in out:
            g.pop("_sort", None)
        return out[:MAX_GRANTS], len(out)

    # ── models and budget ────────────────────────────────────────────────────
    def models() -> dict:
        listing = safe(lambda: providers_mod.list_providers(store), {}) or {}
        default = next((p for p in listing.get("providers") or []
                        if p.get("id") == listing.get("default") and p.get("configured")), None)
        spent = ((safe(store.model_usage_summary, {}) or {}).get("total") or {}).get("cost_usd")
        cap = safe(lambda: builtin_agents.daily_cap(store)[0], None)
        limit = f"Each agent may start up to {_count(cap, 'time')} a day." if cap else ""
        if isinstance(spent, (int, float)) and spent >= 0.005:
            budget = f"${spent:,.2f} spent so far. {limit}".strip()
        else:
            budget = f"No budget set. {limit}".strip()
        return {"default_provider": _text(default["name"]) if default else "Not chosen yet",
                "chosen": default is not None, "budget_text": budget}

    # ── the page ─────────────────────────────────────────────────────────────
    @app.get("/api/factory/settings")
    def get_settings(request: Request):
        now = now_utc()
        scopes = getattr(request.state, "scopes", None)
        can_change = scopes is None or "admin" in scopes
        fx = for_caller(ctx.fx, request)
        s = safe(fx.settings, {}) or {}
        how = _how_far(s)
        builders = s.get("builders_max")
        builders = builders if isinstance(builders, int) and 1 <= builders <= 20 else 3
        pace = safe(fx.pace, {}) or {}
        given, total = grants(now)
        value, _ = edition_mod.edition(store)
        hidden = h.hidden_ids()   # a private project's keys are not counted for a non-member
        keys = [k for k in safe(store.list_api_keys, []) or []
                if not k.get("revoked_at") and k.get("project") not in hidden]
        team = _text(os.getenv("TARES_WORKSPACE_URL"))
        return {
            "can_change": can_change,
            "tools": factory_tools.tools(store, now, h.hidden_ids()),
            "crew": {
                "how_far": {"value": how["autonomy"], "releases": how["releases"],
                            "text": how["text"]},
                "release_profile": bool(_text(s.get("release_profile"))),
                "builders_max": builders,
                "builders_text": f"Up to {_count(builders, 'builder')} at once",
                "challenger": bool(s.get("challenger")),
                "pace": bool(s.get("pace")),
                "pace_text": pace_text(pace),
                "grants": given, "grants_total": total,
            },
            "models": safe(models, {"default_provider": "Not chosen yet", "chosen": False,
                                    "budget_text": "No budget set."}),
            "people": {"keys": {"count": len(keys),
                                "text": ("No access keys yet" if not keys
                                         else _count(len(keys), "access key"))},
                       "cloud_team_href": team or None},
            "edition": {"value": value, "label": edition_mod.NAMES[value],
                        "can_switch": can_change,
                        "options": [{"value": e, "label": edition_mod.NAMES[e]}
                                    for e in edition_mod.EDITIONS if e != value],
                        "other": {"value": OTHER[value],
                                  "label": edition_mod.NAMES[OTHER[value]]},
                        "note": ("Switching changes the screens, not your data. What only the "
                                 "other edition shows stays reachable under Settings, Advanced.")},
            "advanced": ADVANCED,
        }
