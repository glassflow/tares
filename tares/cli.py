"""Entry points: `tares` (the user CLI), `taresd` (the daemon), `tares-mcp` (stdio MCP proxy).

`tares up` is the one command a local install needs: it picks a data home (~/.tares), points the
daemon's DuckDB + catalog there, and starts the server (API + console UI) on http://127.0.0.1:8787.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

DEFAULT_HOME = Path(os.getenv("TARES_HOME", str(Path.home() / ".tares")))


def _pkg_version() -> str:
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("tares")
    except PackageNotFoundError:
        return "0+source"   # running from a source checkout without an install


def _warn_if_exposed(host: str) -> None:
    """Loud warning when binding a non-loopback address with no auth token set — the common way to
    accidentally expose your data on the network."""
    loopback = {"127.0.0.1", "localhost", "::1", ""}
    if host not in loopback and not os.getenv("TARES_AUTH_TOKEN", "").strip():
        print(
            f"\n  ⚠  Tares is binding {host} (reachable off this machine) with NO auth token.\n"
            "     Anyone who can reach this port can read your data. Set TARES_AUTH_TOKEN and put\n"
            "     it behind TLS before exposing it; see https://docs.glassflow.ai/tares (Deployment).\n",
            flush=True,
        )


def run_daemon():
    from .config import reject_legacy_db, reject_legacy_env
    reject_legacy_env()
    reject_legacy_db(os.environ.get("TARES_DB", "tares.duckdb"))
    import uvicorn

    from .daemon import make_app

    host = os.getenv("TARES_HOST", "127.0.0.1")
    port = int(os.getenv("TARES_PORT", "8787"))
    _warn_if_exposed(host)
    # Idle keep-alive must outlast whatever proxy sits in front (nginx ingress pools upstream
    # connections for 60s). uvicorn's 5s default let nginx reuse a connection the daemon was
    # closing at that very moment: "upstream prematurely closed connection", a 502 on any POST
    # that landed ~5s after the previous response, which the step-by-step project builder did.
    uvicorn.run(make_app(), host=host, port=port, timeout_keep_alive=75)


def run_mcp():
    from .mcp_server import main as mcp_main

    mcp_main()


_AUTH_GENERATE = "\x00generate"   # sentinel: bare `--auth` (no value) → generate + persist a token


def _resolve_root_token(home: Path, flag) -> str | None:
    """The console/API login credential when auth is ON — or None (auth OFF).

    Three categories of credential in Tares: the ingest URL (a per-source address, not a secret),
    scoped API keys (minted in the console for machines), and THIS — the root login a human uses to
    reach the console. It's bootstrapped at launch, never minted in the UI, and printed to whoever
    ran the command (terminal access == operator). Precedence: an explicit --auth=<token> or the
    TARES_AUTH_TOKEN env var (hosted/scripted, stable), else a token persisted in the data dir
    (generated once on the first bare `--auth`, reused on every restart so you never get locked out
    or have to re-copy it). Auth is OFF unless --auth is passed or the env token is set."""
    env = os.getenv("TARES_AUTH_TOKEN", "").strip()
    if isinstance(flag, str) and flag != _AUTH_GENERATE:
        return flag.strip()                      # explicit --auth=<token>
    if flag is None and not env:
        return None                              # no --auth, no env → auth OFF
    if env:
        return env                               # env token (bare --auth may also be present)
    # bare --auth: reuse the persisted token, or generate and persist one now.
    import secrets
    path = home / "root_token"
    if path.exists() and (tok := path.read_text().strip()):
        return tok
    tok = f"nvf_root_{secrets.token_urlsafe(24)}"
    path.write_text(tok)
    try:
        path.chmod(0o600)                        # best-effort: readable only by the owner
    except OSError:
        pass
    return tok


def _up(args: argparse.Namespace):
    """Start the daemon with a persistent data home, so a fresh `pip install` just works."""
    home = Path(args.data_dir).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    # point the daemon at the data home unless the user already set these explicitly.
    # (the daemon reads these env vars at import time, so set them before run_daemon imports it.)
    os.environ.setdefault("TARES_DB", str(home / "tares.duckdb"))
    os.environ.setdefault("TARES_CATALOG", str(home / "catalog.yaml"))  # seed only; absent is fine
    os.environ["TARES_HOST"] = args.host
    os.environ["TARES_PORT"] = str(args.port)

    # Auth mode. Setting TARES_AUTH_TOKEN is what the daemon's guard keys off, so resolving the
    # root token here and exporting it turns the whole existing enforcement on — nothing else to wire.
    root_token = _resolve_root_token(home, getattr(args, "auth", None))
    if root_token:
        os.environ["TARES_AUTH_TOKEN"] = root_token

    shown = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{shown}:{args.port}"
    if root_token:
        # print the login the operator uses — a click-through URL, since they're at the terminal.
        print(f"tares: console at {url}  ·  data in {home}", flush=True)
        print(f"tares: auth ON; log in at {url}/?token={root_token}", flush=True)
    else:
        print(f"tares: console at {url}  ·  data in {home}  ·  auth OFF (open; "
              f"run with --auth to require a login)", flush=True)
    if args.open:
        import threading
        import webbrowser
        open_url = f"{url}/?token={root_token}" if root_token else url
        threading.Timer(1.2, lambda: webbrowser.open(open_url)).start()
    _print_next_step_when_up(url, root_token, str(home))
    run_daemon()


def _print_next_step_when_up(url: str, token: str | None, data_dir: str) -> None:
    """Once /health answers, print the same "Next:" line `tares status` ends with, so a first-run
    user sees what to do right under the console URL. A daemon thread: it must never hold the
    process open or fail the start."""
    import threading
    import time

    def wait():
        from .status import collect, next_step
        for _ in range(60):
            time.sleep(0.5)
            st = collect(url, token, data_dir=data_dir)
            if st["daemon"]["running"]:
                print(f"tares: {next_step(st)}", flush=True)
                return

    threading.Thread(target=wait, daemon=True).start()


def _status(args: argparse.Namespace):
    """Readiness checklist against a running daemon. Read-only."""
    import json
    from .status import collect, render
    home = Path(args.data_dir).expanduser()
    base = args.taresd or f"http://127.0.0.1:{os.getenv('TARES_PORT', '8787')}"
    token = os.getenv("TARES_AUTH_TOKEN", "").strip() or None
    if not token and (home / "root_token").exists():
        token = (home / "root_token").read_text().strip() or None
    mcp = f"http://{os.getenv('TARES_MCP_HOST', '127.0.0.1')}:{os.getenv('TARES_MCP_PORT', '8788')}/mcp"
    st = collect(base, token, mcp_url=mcp, data_dir=str(home))
    if args.json:
        print(json.dumps(st, indent=2, default=str))
    else:
        print(render(st))
    raise SystemExit(0 if st["daemon"]["running"] else 1)


def _mcp(args: argparse.Namespace):
    """Run the MCP server over a network transport (for remote agents). stdio is the default for
    local use and is what the `tares-mcp` entry point a client spawns uses."""
    os.environ["TARES_MCP_TRANSPORT"] = args.transport
    os.environ["TARES_MCP_HOST"] = args.host
    os.environ["TARES_MCP_PORT"] = str(args.port)
    if args.taresd:
        os.environ["TARESD_URL"] = args.taresd
    target = os.getenv("TARESD_URL", "http://127.0.0.1:8787")
    path = "/sse" if args.transport == "sse" else "/mcp"
    print(f"tares-mcp: {args.transport} on http://{args.host}:{args.port}{path}  ->  {target}",
          flush=True)
    run_mcp()


def main():
    from .config import reject_legacy_env
    reject_legacy_env()
    p = argparse.ArgumentParser(prog="tares", description="Tares; a data plane for AI agents.")
    p.add_argument("--version", action="version", version=f"tares {_pkg_version()}")
    sub = p.add_subparsers(dest="cmd")

    up = sub.add_parser("up", help="start the Tares daemon (API + console UI)")
    up.add_argument("--host", default=os.getenv("TARES_HOST", "127.0.0.1"),
                    help="bind address (default 127.0.0.1; use 0.0.0.0 to expose on the network)")
    up.add_argument("--port", type=int, default=int(os.getenv("TARES_PORT", "8787")))
    up.add_argument("--data-dir", default=str(DEFAULT_HOME),
                    help=f"where to keep the DuckDB + catalog (default {DEFAULT_HOME})")
    up.add_argument("--open", action="store_true", help="open the console in your browser")
    up.add_argument("--auth", nargs="?", const=_AUTH_GENERATE, default=None, metavar="TOKEN",
                    help="require a login for the console + API. Bare --auth generates and persists "
                         "a root token (printed as a login URL each launch); --auth=<token> uses "
                         "yours. Omit for an open local instance.")
    up.set_defaults(func=_up)

    m = sub.add_parser("mcp", help="run the MCP server for remote agents (HTTP transport)")
    m.add_argument("--transport", default="streamable-http", choices=["stdio", "sse", "streamable-http"])
    m.add_argument("--host", default=os.getenv("TARES_MCP_HOST", "127.0.0.1"))
    m.add_argument("--port", type=int, default=int(os.getenv("TARES_MCP_PORT", "8788")))
    m.add_argument("--taresd", default=os.getenv("TARESD_URL", ""),
                   help="taresd base URL to proxy to (default http://127.0.0.1:8787)")
    m.set_defaults(func=_mcp)

    st = sub.add_parser("status", help="readiness checklist for a running instance, and the next step")
    st.add_argument("--taresd", default=os.getenv("TARESD_URL", ""),
                    help="daemon URL (default http://127.0.0.1:$TARES_PORT or 8787)")
    st.add_argument("--data-dir", default=str(DEFAULT_HOME), help="where the root token lives when auth is on")
    st.add_argument("--json", action="store_true", help="machine-readable output")
    st.set_defaults(func=_status)

    args = p.parse_args()
    if not getattr(args, "func", None):
        p.print_help()
        return
    args.func(args)
