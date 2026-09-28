"""`tares status` (tares/status.py): the decision table, the rendering, and client detection.
No daemon: collect() is exercised in test_cli-style integration by hand; here the pure parts."""
import json, os, tempfile
from pathlib import Path

from tares.status import detect_clients, next_step, render

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


def base(**over):
    st = {"url": "http://127.0.0.1:8787", "data_dir": "/x", "version": "1.11.0",
          "daemon": {"running": True, "status": "ok", "uptime_seconds": 3700, "detail": None},
          "auth": {"on": False}, "storage": {"used_bytes": 12_000_000, "max_bytes": None},
          "sources": {"configured": 0, "receiving": 0, "last_event_at": None, "silent": []},
          "views": {"count": 0, "names": []}, "triggers": {"enabled": 0, "last_fired_at": None},
          "agents": {"enabled": 0, "configured": 0, "model": None, "key_configured": False},
          "mcp": {"url": "http://127.0.0.1:8788/mcp", "running": False},
          "clients": {}, "slack": {"configured": False}, "entities": []}
    for k, v in over.items():
        st[k] = {**st[k], **v} if isinstance(v, dict) else v
    return st


# decision table, in order
ck("down -> start", "tares up" in next_step(base(daemon={"running": False})))
ck("no sources -> add or demo", "add a source" in next_step(base()))
s = base(sources={"configured": 1, "receiving": 0, "silent": [{"name": "logs", "error": "no such container"}]})
n = next_step(s)
ck("silent source named with its error", "`logs`" in n and "no such container" in n, n)
ck("one source -> second keyed the same", "second source" in next_step(base(sources={"configured": 1, "receiving": 1})))
two = base(sources={"configured": 2, "receiving": 2})
ck("no client -> claude mcp add", "claude mcp add" in next_step(two))
conn = base(sources={"configured": 2, "receiving": 2}, clients={"Claude Code": "connected"},
            agents={"enabled": 1, "configured": 1, "key_configured": False})
ck("agent enabled, no key -> where to set it", "ANTHROPIC_API_KEY" in next_step(conn))
ready = base(sources={"configured": 2, "receiving": 2}, clients={"Cursor": "connected"},
             agents={"enabled": 1, "configured": 1, "key_configured": True}, entities=["api-server"])
ck("otherwise ready with the first entity", next_step(ready) == "Ready. Ask your agent: what happened to api-server in the last 15 minutes?", next_step(ready))

# rendering
out = render(ready)
ck("header has version and url", out.startswith("Tares 1.11.0 at http://127.0.0.1:8787"), out.splitlines()[0])
ck("uptime rendered", "uptime 1h 1m" in out)
ck("auth off wording", "off (open local instance)" in out)
ck("key set", "model provider: set" in out)
ck("ends with the next line", out.rstrip().splitlines()[-1].startswith("Ready."))
ck("no em dash anywhere", "—" not in out)
down = render(base(daemon={"running": False}))
ck("down is short and says so", "not running" in down and "Next:" in down)

# client detection, read-only over a temp home
with tempfile.TemporaryDirectory() as d:
    h = Path(d); cwd = h / "proj"; cwd.mkdir()
    ck("nothing -> empty", detect_clients(h, cwd) == {})
    (h / ".claude.json").write_text(json.dumps({"mcpServers": {}, "projects": {"/p": {"mcpServers": {"tares": {}}}}}))
    ck("claude code per-project entry", detect_clients(h, cwd)["Claude Code"] == "connected")
    (h / ".cursor").mkdir(); (h / ".cursor" / "mcp.json").write_text("{not json")
    ck("unreadable cursor config -> unknown", detect_clients(h, cwd)["Cursor"] == "unknown")
    (h / ".codex").mkdir(); (h / ".codex" / "config.toml").write_text("[mcp_servers.tares]\nurl='x'\n")
    ck("codex toml", detect_clients(h, cwd)["Codex"] == "connected")

print(f"\n{P} passed, {F} failed")
raise SystemExit(1 if F else 0)
