"""`tares status`: a readiness checklist for a running instance, and the one next step.

Everything comes from the daemon's existing HTTP API plus a read-only look at the agent clients'
config files on this machine. Pure functions where it matters: `next_step` and `render` take the
collected dict, so they are testable without a daemon and reusable by `tares up`.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DOCS = "docs.glassflow.ai/tares"
_TIMEOUT = 3.0


# ── collection ────────────────────────────────────────────────────────────────────────────────

def _get(base: str, path: str, token: str | None) -> dict | list | None:
    req = urllib.request.Request(base.rstrip("/") + path)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError):
        return None


def _probe(url: str) -> bool:
    """Is anything listening? Any HTTP answer counts (an MCP endpoint answers GET with 4xx)."""
    try:
        urllib.request.urlopen(urllib.request.Request(url), timeout=1.5)
        return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError):
        return False


def _ago(ts: str | None) -> str | None:
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        s = int((datetime.now(timezone.utc) - t).total_seconds())
    except ValueError:
        return None
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _fmt_bytes(n: int | None) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}".replace(".0 ", " ")
        n /= 1024
    return f"{n:.1f} TB"


def detect_clients(home: Path | None = None, cwd: Path | None = None) -> dict:
    """Which local agent clients have a Tares MCP server registered. Best-effort, read-only:
    unreadable or malformed files read as 'unknown', a missing file as not connected."""
    home = home or Path.home()
    cwd = cwd or Path.cwd()
    out: dict[str, str] = {}

    def has_tares(servers) -> bool:
        return isinstance(servers, dict) and any("tares" in k.lower() for k in servers)

    # Claude Code: global mcpServers plus per-project entries in ~/.claude.json, and .mcp.json
    p = home / ".claude.json"
    if p.exists():
        try:
            d = json.loads(p.read_text())
            found = has_tares(d.get("mcpServers"))
            for proj in (d.get("projects") or {}).values():
                found = found or has_tares((proj or {}).get("mcpServers"))
            if not found and (cwd / ".mcp.json").exists():
                found = has_tares(json.loads((cwd / ".mcp.json").read_text()).get("mcpServers"))
            out["Claude Code"] = "connected" if found else "not connected"
        except (OSError, ValueError):
            out["Claude Code"] = "unknown"
    # Cursor: global or per-project mcp.json
    for p in (home / ".cursor" / "mcp.json", cwd / ".cursor" / "mcp.json"):
        if p.exists():
            try:
                if has_tares(json.loads(p.read_text()).get("mcpServers")):
                    out["Cursor"] = "connected"
                    break
                out.setdefault("Cursor", "not connected")
            except (OSError, ValueError):
                out["Cursor"] = "unknown"
    # Codex: TOML; a substring check is enough and avoids a parser dependency
    p = home / ".codex" / "config.toml"
    if p.exists():
        try:
            out["Codex"] = "connected" if "[mcp_servers.tares" in p.read_text() else "not connected"
        except OSError:
            out["Codex"] = "unknown"
    return out


def collect(base: str, token: str | None = None, mcp_url: str | None = None,
            data_dir: str | None = None) -> dict:
    """One dict with everything the checklist prints. `daemon.running` false means nothing else
    could be read; every other field is present (possibly null) so `--json` is stable."""
    health = _get(base, "/health", token)
    st: dict = {
        "url": base, "data_dir": data_dir, "version": None,
        "daemon": {"running": health is not None, "status": None, "uptime_seconds": None,
                   "detail": None},
        "auth": {"on": None},
        "storage": {"used_bytes": None, "max_bytes": None},
        "sources": {"configured": 0, "receiving": 0, "last_event_at": None, "silent": []},
        "views": {"count": 0, "names": []},
        "triggers": {"enabled": 0, "last_fired_at": None},
        "agents": {"enabled": 0, "configured": 0, "model": None, "key_configured": None},
        "mcp": {"url": mcp_url, "running": None},
        "clients": detect_clients(),
        "slack": {"configured": None},
        "entities": [],
    }
    if health is None:
        return st
    st["daemon"].update({"status": health.get("status"), "detail": health.get("detail"),
                         "uptime_seconds": health.get("uptime_seconds")})
    st["version"] = health.get("version")
    st["auth"]["on"] = bool(health.get("auth_required"))

    usage = _get(base, "/api/usage", token)
    if isinstance(usage, dict):
        st["storage"] = {"used_bytes": (usage.get("db_bytes") or 0) + (usage.get("wal_bytes") or 0),
                         "max_bytes": usage.get("max_bytes")}

    sources = _get(base, "/api/sources", token)
    if isinstance(sources, list):
        last = None
        for s in sources:
            h = s.get("health") or {}
            st["sources"]["configured"] += 1
            if (h.get("events_total") or 0) > 0:
                st["sources"]["receiving"] += 1
            else:
                st["sources"]["silent"].append({"name": s.get("name"), "error": h.get("last_error")})
            li = h.get("last_ingest")
            if li and (last is None or str(li) > str(last)):
                last = li
        st["sources"]["last_event_at"] = last

    views = _get(base, "/api/views", token)
    if isinstance(views, list):
        st["views"] = {"count": len(views), "names": [v.get("name") for v in views]}

    triggers = _get(base, "/api/triggers", token)
    if isinstance(triggers, list):
        st["triggers"]["enabled"] = sum(1 for t in triggers if not t.get("paused"))
    dispatches = _get(base, "/api/activity/dispatches?limit=1", token)
    if isinstance(dispatches, list) and dispatches:
        st["triggers"]["last_fired_at"] = dispatches[0].get("fired_at")

    agents = _get(base, "/api/agents/builtin", token)
    if isinstance(agents, dict):
        rows = agents.get("agents") or []
        enabled = [a for a in rows if a.get("enabled")]
        st["agents"] = {"configured": len(rows), "enabled": len(enabled),
                        "model": next((a.get("model") for a in enabled if a.get("model")), None)
                        or agents.get("default_model"),
                        "key_configured": bool(agents.get("key_configured"))}
        st["slack"]["configured"] = bool(agents.get("slack_workspace"))

    if mcp_url:
        st["mcp"]["running"] = _probe(mcp_url)

    # one entity name for the "ask your agent" line: the first source's most recent event key
    if isinstance(sources, list) and sources:
        ev = _get(base, f"/api/sources/{sources[0]['name']}/events?limit=1", token)
        if isinstance(ev, list) and ev and isinstance(ev[0], dict):
            k = ev[0].get("key") or ev[0].get("key_value")
            if k:
                st["entities"] = [k]
    return st


# ── the decision table ────────────────────────────────────────────────────────────────────────

def next_step(st: dict) -> str:
    """Exactly one line, in this order of precedence."""
    if not st["daemon"]["running"]:
        return f"Next: start Tares with `tares up`. Docs: {DOCS}/quickstart"
    src = st["sources"]
    if src["configured"] == 0:
        return (f"Next: add a source (Sources > Add source, or POST /api/sources), or run the demo "
                f"stack. Docs: {DOCS}/connectors")
    if src["receiving"] == 0:
        s = src["silent"][0]
        why = f" ({s['error']})" if s.get("error") else ""
        return f"Next: source `{s['name']}` has not received anything yet{why}. Docs: {DOCS}/connectors"
    if src["configured"] == 1:
        return (f"Next: add a second source keyed by the same label so reads correlate. "
                f"Docs: {DOCS}/connectors")
    if not any(v == "connected" for v in st["clients"].values()):
        mcp = st["mcp"].get("url") or "http://127.0.0.1:8788/mcp"
        return (f"Next: connect an agent: `tares mcp` then "
                f"`claude mcp add --transport http tares {mcp}`. Docs: {DOCS}/agents")
    ag = st["agents"]
    if ag["enabled"] > 0 and ag["key_configured"] is False:
        return ("Next: set ANTHROPIC_API_KEY before `tares up`, or add a model provider under Settings, "
                "so the enabled agents can run.")
    ent = st["entities"][0] if st["entities"] else "<entity>"
    return f"Ready. Ask your agent: what happened to {ent} in the last 15 minutes?"


# ── rendering ─────────────────────────────────────────────────────────────────────────────────

def render(st: dict) -> str:
    L = []
    ver = st.get("version") or "?"
    store = ""
    if st["storage"]["used_bytes"] is not None:
        cap = st["storage"]["max_bytes"]
        store = f"   data dir {st['data_dir'] or '?'} ({_fmt_bytes(st['storage']['used_bytes'])}"
        store += f" of {_fmt_bytes(cap)})" if cap else ")"
    L.append(f"Tares {ver} at {st['url']}{store}")
    d = st["daemon"]
    if not d["running"]:
        L.append(f"{'Daemon:':<19}not running (nothing answered at {st['url']})")
        L.append("")
        L.append(next_step(st))
        return "\n".join(L)
    up = d.get("uptime_seconds")
    up_s = f" (uptime {up // 3600}h {(up % 3600) // 60}m)" if isinstance(up, (int, float)) else ""
    status = d["status"] or "running"
    L.append(f"{'Daemon:':<19}{'running' if status == 'ok' else status}{up_s}"
             + (f"   {d['detail']}" if d.get("detail") else ""))
    L.append(f"{'Auth:':<19}{'on' if st['auth']['on'] else 'off (open local instance)'}")
    s = st["sources"]
    if s["configured"] == 0:
        L.append(f"{'Sources:':<19}none yet")
    else:
        last = _ago(s["last_event_at"])
        L.append(f"{'Sources:':<19}{s['configured']} configured, {s['receiving']} receiving"
                 + (f" (last event {last})" if last else ""))
    v = st["views"]
    L.append(f"{'Views:':<19}{v['count']}" + (f" ({', '.join(v['names'][:3])})" if v["names"] else ""))
    t = st["triggers"]
    fired = _ago(t["last_fired_at"])
    L.append(f"{'Triggers:':<19}{t['enabled']} enabled" + (f", last fired {fired}" if fired else ""))
    a = st["agents"]
    if a["configured"] == 0:
        L.append(f"{'Tares agents:':<19}none")
    else:
        key = "set" if a["key_configured"] else "missing"
        L.append(f"{'Tares agents:':<19}{a['enabled']} enabled"
                 + (f", model {a['model']}" if a.get("model") else "") + f", model provider: {key}")
    m = st["mcp"]
    if m["running"] is None:
        L.append(f"{'MCP endpoint:':<19}not checked")
    elif m["running"]:
        L.append(f"{'MCP endpoint:':<19}running at {m['url']}")
    else:
        L.append(f"{'MCP endpoint:':<19}not running (start with `tares mcp`)")
    c = st["clients"]
    if c:
        L.append(f"{'Agent clients:':<19}" + ", ".join(f"{k}: {v}" for k, v in c.items()))
    else:
        L.append(f"{'Agent clients:':<19}none detected")
    sl = st["slack"]["configured"]
    L.append(f"{'Slack:':<19}{'configured' if sl else 'not configured'}")
    L.append("")
    L.append(next_step(st))
    return "\n".join(L)
