"""Can a Codex station reach an HTTP MCP endpoint as itself, the way the Tares MCP expects? A by-hand probe
(TR-1576), not in CI: it needs a signed-in `codex`.

    TARES_PROBE_CODEX=/path/to/codex python tests/factory/codex_probe/probe_mcp.py [--out DIR]

The Claude plugin connects stations to the crew's MCP endpoint over HTTP with a bearer token and the station's
identity in headers (claude-plugin/.mcp.json: X-Tares-Station, X-Tares-Role, X-Tares-Parent, X-Tares-Crew,
X-Tares-Project). Here a local MCP server on 127.0.0.1 stands in for it: it refuses a request without the right
bearer token and writes down the identity headers it was sent. Codex is configured the way a station would be,
with the token and the identity taken from environment variables by name (`bearer_token_env_var`,
`env_http_headers`), so neither is in an argument list or a config file. Three things are checked: the right
token gets a tool call through with the identity headers on it; a wrong token gets nothing; and whether the
token is visible to the commands the model runs.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from appserver import AppServer, AppServerError, codex_bin, items  # noqa: E402
from fixture import World, guard_hooks  # noqa: E402
from probe_guard import EFFORT, MODEL, QUIET, RULES  # noqa: E402

TOKEN = "probe-token-" + os.urandom(6).hex()      # made for this run, good for nothing else
IDENTITY = ("x-tares-station", "x-tares-role", "x-tares-crew")


def serve(seen: list) -> int:
    import uvicorn
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("tares_probe")

    @mcp.tool()
    def whoami() -> str:
        """Say which station is calling (probe)."""
        return "station ok"

    inner = mcp.streamable_http_app()

    async def app(scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            ok = headers.get("authorization") == "Bearer " + TOKEN
            seen.append({"path": scope.get("path"), "authorized": ok, **{h: headers.get(h) for h in IDENTITY}})
            if not ok:
                await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await inner(scope, receive, send)

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    return port


def station(out: str, name: str, w: World, port: int, token: str) -> tuple[AppServer, str, list]:
    args = QUIET + ["-c", f'mcp_servers.tares_probe.url="http://127.0.0.1:{port}/mcp"',
                    "-c", 'mcp_servers.tares_probe.bearer_token_env_var="PROBE_MCP_TOKEN"',
                    "-c", 'mcp_servers.tares_probe.env_http_headers={"X-Tares-Station"="FACTORY_STATION","X-Tares-Role"="FACTORY_ROLE","X-Tares-Crew"="FACTORY_CREW"}']
    app = AppServer.spawn(codex_bin(), args=args, cwd=w.work, env=w.env("builder", extra={"PROBE_MCP_TOKEN": token}),
                          transcript_path=os.path.join(out, f"{name}.wire.jsonl"), stderr_path=os.path.join(out, f"{name}.stderr.log"))
    app.initialize()
    started = app.request("thread/start", {"cwd": w.work, "ephemeral": True, "config": {"hooks": guard_hooks(), "bypass_hook_trust": True},
                                           "approvalPolicy": "never", "sandbox": "danger-full-access", "model": MODEL,
                                           "developerInstructions": RULES})
    status = app.request("mcpServerStatus/list", {})
    tools = [sorted((s.get("tools") or {}).keys()) for s in status.get("data", []) if s.get("name") == "tares_probe"]
    return app, started["thread"]["id"], (tools[0] if tools else [])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(os.getcwd(), "codex-probe-out"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    seen: list = []
    port = serve(seen)

    w = World()
    app, thread, tools = station(args.out, "mcp.right_token", w, port, TOKEN)
    app.run_turn(thread, "Step 1: call the tares_probe tool whoami. Step 2: run `printenv PROBE_MCP_TOKEN | wc -c`. "
                         "Step 3: run `printenv FACTORY_STATION`.", timeout=180, effort=EFFORT)
    calls = [{"tool": i.get("tool"), "status": i.get("status")} for i in items(app.log, kind="mcpToolCall")]
    cmds = [{"command": i.get("command"), "exit": i.get("exitCode"), "output": (i.get("aggregatedOutput") or "").strip()[:60]}
            for i in items(app.log, kind="commandExecution")]
    app.close()
    good = [s for s in seen if s["authorized"]]
    seen_right = list(seen)
    seen.clear()

    w2 = World()
    try:
        app2, thread2, tools2 = station(args.out, "mcp.wrong_token", w2, port, "not-the-token")
        app2.close()
        wrong = {"thread_started": True, "tools_offered": tools2}
    except AppServerError as e:
        wrong = {"thread_started": False, "error": str(e)[:200]}
    refused = [s for s in seen if not s["authorized"]]

    token_cmd = next((c for c in cmds if "PROBE_MCP_TOKEN" in (c["command"] or "")), {})
    token_visible = token_cmd.get("output", "").strip() not in ("", "0")
    result = {
        "tools_offered_with_the_right_token": tools, "mcp_calls": calls, "commands": cmds,
        "requests_with_the_right_token": len(good), "identity_headers_sent": good[-1] if good else None,
        "wrong_token": wrong, "requests_refused_with_the_wrong_token": len(refused),
        "token_visible_to_the_model_shell": token_visible,
        "checks": {
            "the tool call went through": any(c["tool"] == "whoami" and c["status"] == "completed" for c in calls),
            "the station's identity was on the request": bool(good) and good[-1].get("x-tares-station") == "crew-builder"
                and good[-1].get("x-tares-role") == "builder" and good[-1].get("x-tares-crew") == "crew",
            "a wrong token was refused and offered no tools": bool(refused) and not wrong.get("tools_offered"),
        }}
    result["pass"] = all(result["checks"].values())
    for s in seen_right[:1]:
        result["first_request"] = s
    with open(os.path.join(args.out, "mcp.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1, ensure_ascii=False)
    print("PASS" if result["pass"] else "FAIL", "station identity on an HTTP MCP endpoint")
    for k, v in result["checks"].items():
        print(f"      {'ok ' if v else 'NO '} {k}")
    print("      token visible to the model's shell:", token_visible, "| commands:", cmds)
    print("      wrong token:", wrong, "refused:", len(refused))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
