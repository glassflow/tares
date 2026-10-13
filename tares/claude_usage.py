"""What Claude Code sessions spend: their context windows, the plan's limits, and tokens by project.

The pieces come from three places on the crew's machine, all through the claude_code source:
- the status line Claude Code runs for each station (claude-plugin/scripts/statusline.py) reports
  the session's context window and, on a Pro or Max plan, the 5-hour and weekly limits;
- `factory context <station>` reads a station's /context (the window by category) without a model call;
- the factory watch reads /usage every few minutes: every limit of the plan, with its reset time.
Token counts per reply come from the transcript lines the plugin already ships. This module keeps
the parts of each report the console shows, and answers /api/claude-code/usage.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .claude_usage_store import PLAN_SETTING
from .envelope import now_utc

SESSION_WINDOW = timedelta(hours=5)
MAX_SKILLS = 10


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _dict(v) -> dict:
    return v if isinstance(v, dict) else {}


def _when(v) -> datetime | None:
    """A reset time as Claude Code gives it: epoch seconds (status line) or ISO 8601 (/usage)."""
    if _num(v) is not None:
        return datetime.fromtimestamp(float(v), tz=timezone.utc)
    if isinstance(v, str) and v:
        try:
            t = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    return None


# ── the reports, kept to what the console shows ─────────────────────────────────

def compact_status(s: dict) -> dict:
    """A status line report: the model, the context window, Claude Code's own cost estimate, and
    the plan's limits when the account has them."""
    cw, model = _dict(s.get("context_window")), _dict(s.get("model"))
    cu = _dict(cw.get("current_usage"))
    rl = {}
    for k in ("five_hour", "seven_day", "spend_limit"):
        w = _dict(_dict(s.get("rate_limits")).get(k))
        if _num(w.get("used_percentage")) is not None:
            rl[k] = {"used_percentage": w["used_percentage"], "resets_at": w.get("resets_at")}
    return {
        "model": {"id": model.get("id"), "display_name": model.get("display_name")},
        "context_window": {
            "size": _num(cw.get("context_window_size")),
            "used_percentage": _num(cw.get("used_percentage")),
            "input_tokens": _num(cw.get("total_input_tokens")),
            "output_tokens": _num(cw.get("total_output_tokens")),
            "current": {k: _num(cu.get(k)) for k in ("input_tokens", "output_tokens",
                                                     "cache_creation_input_tokens",
                                                     "cache_read_input_tokens")} if cu else None},
        "cost_usd": _num(_dict(s.get("cost")).get("total_cost_usd")),
        "rate_limits": rl or None,
        "autocompact_tokens": _num(s.get("autocompact_tokens")),
        "session_name": s.get("session_name") if isinstance(s.get("session_name"), str) else None,
        "version": s.get("version") if isinstance(s.get("version"), str) else None,
    }


def compact_context(c: dict) -> dict:
    """A /context reading: the total and the categories, MCP tools summed by server, the memory
    files, and the largest skills."""
    servers: dict[str, dict] = {}
    for t in c.get("mcp_tools") or []:
        if isinstance(t, dict):
            s = servers.setdefault(str(t.get("server_name") or "?"), {"tools": 0, "tokens": 0})
            s["tools"] += 1
            s["tokens"] += int(_num(t.get("tokens")) or 0)
    skills = sorted((x for x in c.get("skills") or [] if isinstance(x, dict)),
                    key=lambda x: -(_num(x.get("tokens")) or 0))
    agents = [x for x in c.get("agents") or [] if isinstance(x, dict)]
    return {
        "model": c.get("model"),
        "total_tokens": _num(c.get("total_tokens")),
        "max_tokens": _num(c.get("raw_max_tokens")) or _num(c.get("max_tokens")),
        "percentage": _num(c.get("percentage")),
        "categories": [{"name": str(x.get("name")), "tokens": int(_num(x.get("tokens")) or 0),
                        "kind": x.get("kind")}
                       for x in c.get("categories") or [] if isinstance(x, dict)],
        "mcp_servers": sorted(({"name": k, **v} for k, v in servers.items()),
                              key=lambda x: -x["tokens"]),
        "memory_files": [{"path": x.get("path"), "type": x.get("type"),
                          "tokens": int(_num(x.get("tokens")) or 0)}
                         for x in c.get("memory_files") or [] if isinstance(x, dict)],
        "skills": {"count": len(skills),
                   "tokens": sum(int(_num(x.get("tokens")) or 0) for x in skills),
                   "largest": [{"name": x.get("name"), "tokens": int(_num(x.get("tokens")) or 0)}
                               for x in skills[:MAX_SKILLS]]},
        "agents": {"count": len(agents),
                   "tokens": sum(int(_num(x.get("tokens")) or 0) for x in agents)},
    }


def limit_label(kind: str, scope) -> str:
    if kind == "session":
        return "Current session (5 hours)"
    if kind == "weekly_all":
        return "This week, all models"
    name = _dict(_dict(scope).get("model")).get("display_name")
    if kind == "weekly_scoped" and name:
        return f"This week, {name}"
    return kind.replace("_", " ").capitalize()


def compact_plan(report: dict) -> dict:
    """/usage's report: each limit of the plan and the extra usage setting."""
    rl = _dict(report.get("rate_limits"))
    limits = []
    for x in rl.get("limits") or []:
        if not isinstance(x, dict) or _num(x.get("percent")) is None:
            continue
        kind = str(x.get("kind") or "limit")
        limits.append({"kind": kind, "label": limit_label(kind, x.get("scope")),
                       "percent": x["percent"], "resets_at": x.get("resets_at"),
                       "severity": x.get("severity"), "active": bool(x.get("is_active"))})
    ex = _dict(rl.get("extra_usage"))
    return {"limits": limits,
            "extra_usage": {k: ex.get(k) for k in ("is_enabled", "monthly_limit", "used_credits",
                                                   "utilization", "currency")} if ex else None}


# Status line windows and the /usage kinds they are the same limit as
_STATUS_KINDS = {"five_hour": "session", "seven_day": "weekly_all"}


def plan_now(report: dict | None, report_at, status: tuple | None, now=None) -> dict | None:
    """The plan's limits now: /usage's list, each limit the status line saw more recently taken
    from it instead. A limit whose reset time has passed is marked `reset`: it started over, and
    nothing has read it since."""
    now = now or now_utc()
    limits: dict[str, dict] = {}
    for x in (report or {}).get("limits") or []:
        limits[x["kind"]] = {**x, "at": report_at, "from": "usage"}
    if status:
        at, rl = status
        for k, kind in _STATUS_KINDS.items():
            w = _dict(_dict(rl).get(k))
            if _num(w.get("used_percentage")) is None:
                continue
            cur = limits.get(kind)
            if cur is None or cur["at"] is None or (at is not None and at >= cur["at"]):
                when = _when(w.get("resets_at"))
                limits[kind] = {**(cur or {"kind": kind, "label": limit_label(kind, None),
                                           "severity": None, "active": False}),
                                "percent": w["used_percentage"],
                                "resets_at": when.isoformat() if when else None,
                                "at": at, "from": "status line"}
    if not limits:
        return None
    order = {"session": 0, "weekly_all": 1}
    out = []
    for x in sorted(limits.values(), key=lambda x: (order.get(x["kind"], 2), x["label"])):
        when = _when(x.get("resets_at"))
        out.append({**x, "reset": bool(when and when <= now)})
    ats = [x["at"] for x in out if x.get("at") is not None]
    return {"limits": out, "extra_usage": (report or {}).get("extra_usage"),
            "at": max(ats) if ats else None}


def context_now(meters: dict | None) -> dict | None:
    """A station's context window from its latest status line report, else from its latest /context
    reading (a station started before it had the status line)."""
    st = _dict((meters or {}).get("status"))
    cw = _dict(st.get("context_window"))
    if not st or cw.get("size") is None:
        r = _dict((meters or {}).get("context"))
        if not r.get("max_tokens"):
            return None
        return {"used": r.get("total_tokens"), "size": r["max_tokens"], "pct": r.get("percentage"),
                "compact_at": None, "compact_pct": None, "model": r.get("model"), "cost_usd": None,
                "at": (meters or {}).get("context_at")}
    cur = _dict(cw.get("current"))
    used = (sum(int(cur.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                "cache_read_input_tokens"))
            if cur else cw.get("input_tokens"))
    pct = cw.get("used_percentage")
    if pct is None and used is not None and cw["size"]:
        pct = round(used * 100 / cw["size"])
    # a station started with --autocompact compacts there, before the window is full
    at = st.get("autocompact_tokens")
    compact_at = at if at and at < cw["size"] else None
    return {"used": used, "size": cw["size"], "pct": pct, "compact_at": compact_at,
            "compact_pct": round(used * 100 / compact_at) if compact_at and used is not None else None,
            "model": _dict(st.get("model")).get("display_name") or _dict(st.get("model")).get("id"),
            "cost_usd": st.get("cost_usd"), "at": (meters or {}).get("status_at")}


# ── /api/claude-code/usage ──────────────────────────────────────────────────────

def plan(store) -> dict | None:
    raw = store.get_setting(PLAN_SETTING)
    try:
        saved = json.loads(raw) if raw else None
    except ValueError:
        saved = None
    saved = saved if isinstance(saved, dict) else None
    at = _when(saved.get("at")) if saved else None
    return plan_now(saved.get("plan") if saved else None, at, store.latest_rate_limits())


def by_project(store, since=None) -> list[dict]:
    names = {p["id"]: p["name"] for p in store.list_projects()}
    out = []
    for r in store.usage_by_project(since):
        name = (names.get(r["project"], "a deleted project") if r["kind"] == "project"
                else "The crew (shared)" if r["kind"] == "crew" else "Other sessions")
        out.append({**r, "name": name})
    total = sum(r["cost_usd"] for r in out) or 0
    for r in out:
        r["share"] = (r["cost_usd"] / total) if total else None
    return out


def usage(store, days: int = 7) -> dict:
    now = now_utc()
    p = plan(store)
    sess = next((x for x in (p or {}).get("limits", []) if x["kind"] == "session"), None)
    end = _when(sess.get("resets_at")) if sess and not sess.get("reset") else None
    start = (end - SESSION_WINDOW) if end else now - SESSION_WINDOW
    return {"plan": p,
            "window": {"since": start, "until": end, "projects": by_project(store, start)},
            "days": days, "projects": by_project(store, now - timedelta(days=days)),
            "now": now}


def register(app, store) -> None:
    @app.get("/api/claude-code/usage")
    async def get_claude_code_usage(days: int = 7):
        """What the Claude Code sessions reporting here spend: the plan's limits (as the crew's
        machine last read them), the current 5-hour window by project, and tokens and estimated
        cost by project over the last `days` (1 to 90). Cost is at API list prices."""
        return usage(store, min(max(days, 1), 90))
