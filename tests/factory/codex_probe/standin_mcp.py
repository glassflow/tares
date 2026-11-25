"""A stand-in for the crew's Tares MCP endpoint, for the probes that start a station through the product and are
not about Tares (TR-1596). A station on Codex does not start without that endpoint: its launch marks the
server required, and the crew asks it one read (`list_crew`) before it hands the station a prompt.

`serve(token)` starts it on 127.0.0.1 and returns (port, seen). It behaves as the real endpoint does
(tares/mcp_server.py): the first request wants a bearer, any bearer; a tool call with another key than `token`
answers with the cell's refusal as the tool's text, `error 401: authentication required`, and no error flag.
`seen` gets one row per request: whether the key was the right one, and the station's identity headers.

`key_file(home, token)` writes the crew's key where a crew home keeps it. Needs the `mcp` package, which the
checkout's own environment has.
"""
from __future__ import annotations

import contextvars
import os
import socket
import threading
import time

IDENTITY = ("x-tares-station", "x-tares-role", "x-tares-parent", "x-tares-crew", "x-tares-project")
_KEY_OK = contextvars.ContextVar("standin_key_ok", default=False)


def serve(token: str) -> tuple:
    import uvicorn
    from mcp.server.fastmcp import FastMCP

    seen: list = []
    mcp = FastMCP("tares")

    @mcp.tool()
    def list_crew(mine: bool = False) -> str:
        """The crews as Tares sees them (stand-in: none)."""
        return "[]" if _KEY_OK.get() else "error 401: authentication required"

    inner = mcp.streamable_http_app()

    async def app(scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            bearer = headers.get("authorization", "")
            ok = bearer == "Bearer " + token
            seen.append({"key": ok, **{h: headers[h] for h in IDENTITY if h in headers}})
            if not bearer:
                await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"detail":"unauthorized"}'})
                return
            _KEY_OK.set(ok)
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
    return port, seen


def key_file(home: str, token: str) -> str:
    """Write the crew's key to <home>/.tares-token, owner-only; returns the path."""
    path = os.path.join(home, ".tares-token")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    return path
