"""Entry points: `tares` (the user CLI), `taresd` (the daemon), `tares-mcp` (stdio MCP proxy).

`tares up` is the one command a local install needs: it picks a data home (~/.tares), points the
daemon's store there, and starts the server (API + console UI) on http://127.0.0.1:8787.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
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
    # point the daemon at the data home: --data-dir, whose default is TARES_HOME (the daemon reads
    # these env vars at import time, so set them before run_daemon imports it)
    os.environ["TARES_HOME"] = str(home)
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
    from ._ui import NO_CONSOLE_HINT, console_built, ui_dist
    if not console_built(ui_dist()):
        print(f"tares: {NO_CONSOLE_HINT}", flush=True)
    if args.open:
        import threading
        import webbrowser
        open_url = f"{url}/?token={root_token}" if root_token else url
        threading.Timer(1.2, lambda: webbrowser.open(open_url)).start()
    run_daemon()


def _mcp(args: argparse.Namespace):
    """Run the MCP server: over HTTP for remote agents (the default), or over stdio for a client
    that spawns it, as the `tares-mcp` entry point does and as the MCP registry entry runs it."""
    os.environ["TARES_MCP_TRANSPORT"] = args.transport
    os.environ["TARES_MCP_HOST"] = args.host
    os.environ["TARES_MCP_PORT"] = str(args.port)
    if args.taresd:
        # the server prefers TARES_FACTORY_URL, so a stray one in the shell would beat --taresd
        os.environ["TARESD_URL"] = os.environ["TARES_FACTORY_URL"] = args.taresd
    target = os.getenv("TARES_FACTORY_URL") or os.getenv("TARESD_URL", "http://127.0.0.1:8787")
    path = "/sse" if args.transport == "sse" else "/mcp"
    where = "stdio" if args.transport == "stdio" else f"{args.transport} on http://{args.host}:{args.port}{path}"
    # stderr: over stdio, stdout is the MCP channel and any other line breaks the client's session
    print(f"tares-mcp: {where}  ->  {target}", file=sys.stderr, flush=True)
    run_mcp()


def _login(args: argparse.Namespace):
    """Connect this machine to a Tares Cloud workspace through the browser (TR-490)."""
    from .login import cell_address, connect_cell, login, show

    def usage(msg):
        if args.json:
            from .crew import contract
            contract.fail("usage", msg, contract.EXIT_USAGE)
        print(msg, file=sys.stderr)
        raise SystemExit(2)
    if args.cell or args.key_stdin:
        if not (args.cell and args.key_stdin):
            usage("tares login: --cell needs --key-stdin, and --key-stdin needs --cell. The key is read from "
                  "standard input, never from an argument.")
        if args.show or args.cloud or args.no_browser:
            usage("tares login: --cell cannot be combined with --show, --cloud or --no-browser.")
        url, problem = cell_address(args.cell)
        if problem:
            usage(f"tares login: --cell {problem}.")
        key = (sys.stdin.readline() or "").strip().lstrip("\ufeff").strip()
        if not key:
            usage("tares login: --key-stdin read no key: send the key as one line on standard input.")
        raise SystemExit(connect_cell(url, key, json_out=args.json))
    if args.show:
        if args.cloud or args.no_browser:
            msg = "tares login: --show only reads the saved login; it cannot be combined with --cloud or --no-browser."
            if args.json:
                from .crew import contract
                contract.fail("usage", msg, contract.EXIT_USAGE)
            print(msg, file=sys.stderr)
            raise SystemExit(2)
        raise SystemExit(show(json_out=args.json))
    raise SystemExit(login(args.cloud, open_browser=not args.no_browser, json_out=args.json))


def _logout(args: argparse.Namespace):
    from .login import logout
    raise SystemExit(logout(json_out=args.json))


def _capabilities(args: argparse.Namespace):
    from .crew.capabilities import cmd_capabilities
    raise SystemExit(cmd_capabilities(json_out=args.json))


def plugin_dir() -> Path:
    """The Claude Code plugin for Tares: inside the installed package, or claude-plugin/ in a
    checkout (an editable install). TARES_PLUGIN_DIR, when set, names the plugin to use instead: a
    test hook, so a suite that edits a copy of the plugin has `tares factory` read that copy."""
    override = os.environ.get("TARES_PLUGIN_DIR")
    if override:
        d = Path(override)
        if not (d / "hooks" / "hooks.json").is_file():
            raise SystemExit(f"tares: TARES_PLUGIN_DIR ({override}) has no hooks/hooks.json")
        return d
    here = Path(__file__).resolve().parent
    for d in (here / "claude-plugin", here.parent / "claude-plugin"):
        if (d / "hooks" / "hooks.json").is_file():
            return d
    raise SystemExit("tares: the Claude Code plugin is missing from this install "
                     f"(looked in {here / 'claude-plugin'} and {here.parent / 'claude-plugin'})")


def _hook(argv: list[str]):
    """`tares hook <name> [args]`: how the Claude Code plugin starts every hook (see tares/crew/hooks.py).
    Standard library only, and it runs before anything else loads, so a hook starts fast."""
    from .crew.hooks import run
    raise SystemExit(run(argv))


def _factory(argv: list[str]):
    """`tares factory ...`: the tares-factory crew (tares/crew). It
    finds this Tares through TARESD_URL and the token in TARES_AUTH_TOKEN, else the workspace
    `tares login` saved, else the local daemon; stations get the same address."""
    env = _factory_env(argv)
    from .crew.dispatch import run
    run(argv, env)


def _git_credential(argv: list[str]):
    """`tares factory git-credential ...`: git reads this command's output as its credential protocol, so what
    runs before the command prints nothing, and when that fails the command exits 1 without a word."""
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            env = _factory_env(argv)
            from .crew.dispatch import run
    except (Exception, SystemExit):
        raise SystemExit(1) from None
    run(argv, env)


def _factory_env(argv: list[str]) -> dict:
    """The environment a `tares factory` command runs in."""
    from .login import resolve
    plugin_dir()   # fails now, with its message, when the plugin is missing from this install
    env = dict(os.environ)
    # The environment first, then `tares login`'s saved workspace (with its key), then the local
    # daemon. TARES_FACTORY_SOURCE tells `doctor` which one it got.
    url, token, source = resolve()
    env["TARESD_URL"] = env.get("TARESD_URL") or url
    env["TARES_FACTORY_URL"] = env.get("TARES_FACTORY_URL") or env["TARESD_URL"]
    # Where the key came from, for `doctor`: a TARES_AUTH_TOKEN already set overrides the saved
    # login's key, which is worth saying when the two differ (TR-560).
    env["TARES_FACTORY_KEY_SOURCE"] = "environment" if env.get("TARES_AUTH_TOKEN") else source
    if token and not env.get("TARES_AUTH_TOKEN"):
        # The saved key goes only to its own workspace: not to an MCP endpoint set elsewhere.
        mcp = (env.get("TARES_FACTORY_MCP_URL") or "").rstrip("/")
        if source == "saved login" and mcp and mcp != f"{url}/mcp":
            print(f"tares factory: TARES_FACTORY_MCP_URL ({mcp}) is not the workspace you are "
                  f"signed in to ({url}); the saved key is not used. Unset it, or set "
                  "TARES_AUTH_TOKEN.", file=sys.stderr)
        else:
            env["TARES_AUTH_TOKEN"] = token
    env["TARES_FACTORY_SOURCE"] = source
    # tares-mcp sits next to this python; stations need it on their PATH to reach Tares.
    bindir = str(Path(sys.argv[0]).resolve().parent)
    if (Path(bindir) / "tares-mcp").exists() and bindir not in env.get("PATH", "").split(os.pathsep):
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    # The first `crew up` in a terminal asks its questions in a box like Claude Code's; the factory
    # CLI then finds them answered. Scripts and --yes get its plain prompts.
    if (argv[:2] == ["crew", "up"] and not {"--yes", "-y"} & set(argv)
            and sys.stdin.isatty() and sys.stdout.isatty()):
        import shutil

        from .factory_setup import setup
        home = Path(env.get("FACTORY_CREW_HOME") or Path.home() / "factory").expanduser()
        claude = env.get("CLAUDE_BIN") or shutil.which("claude", path=env.get("PATH")) or "claude"
        if not setup(env["TARES_FACTORY_URL"].rstrip("/"), env.get("TARES_AUTH_TOKEN", ""), home, claude):
            raise SystemExit(1)
    return env


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "hook":
        _hook(sys.argv[2:])     # first: a hook must not pay for the parser below
    if sys.argv[1:3] == ["factory", "git-credential"]:
        _git_credential(sys.argv[2:])   # before anything below can print
    if len(sys.argv) > 1 and sys.argv[1] == "factory":
        _factory(sys.argv[2:])
    p = build_parser()
    args = p.parse_args()
    if not getattr(args, "func", None):
        p.print_help()
        return
    args.func(args)


def build_parser() -> argparse.ArgumentParser:
    """The `tares` command line: every top-level word. A function of its own so a test can read the words."""
    p = argparse.ArgumentParser(prog="tares", description="Tares; a data plane for AI agents.")
    p.add_argument("--version", action="version", version=f"tares {_pkg_version()}")
    sub = p.add_subparsers(dest="cmd")

    up = sub.add_parser("up", help="start the Tares daemon (API + console UI)")
    up.add_argument("--host", default=os.getenv("TARES_HOST", "127.0.0.1"),
                    help="bind address (default 127.0.0.1; use 0.0.0.0 to expose on the network)")
    up.add_argument("--port", type=int, default=int(os.getenv("TARES_PORT", "8787")))
    up.add_argument("--data-dir", default=str(DEFAULT_HOME),
                    help=f"where to keep the cell's data (default {DEFAULT_HOME})")
    up.add_argument("--open", action="store_true", help="open the console in your browser")
    up.add_argument("--auth", nargs="?", const=_AUTH_GENERATE, default=None, metavar="TOKEN",
                    help="require a login for the console + API. Bare --auth generates and persists "
                         "a root token (printed as a login URL each launch); --auth=<token> uses "
                         "yours. Omit for an open local instance.")
    up.set_defaults(func=_up)

    m = sub.add_parser(
        "mcp", help="run the MCP server: HTTP for remote agents, or --transport stdio",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="joining one project:\n"
               "  Make a project key on the project page (Setup, Keys) and connect with it, e.g.\n"
               "    claude mcp add --transport http tares http://localhost:8788/mcp \\\n"
               "      --header \"Authorization: Bearer <project key>\"\n"
               "  The key reads that project only. Without auth there are no keys: the tools take\n"
               "  `project` by name instead.")
    m.add_argument("--transport", default="streamable-http", choices=["stdio", "sse", "streamable-http"])
    m.add_argument("--host", default=os.getenv("TARES_MCP_HOST", "127.0.0.1"))
    m.add_argument("--port", type=int, default=int(os.getenv("TARES_MCP_PORT", "8788")))
    m.add_argument("--taresd", default=os.getenv("TARESD_URL", ""),
                   help="taresd base URL to proxy to (default http://127.0.0.1:8787)")
    m.set_defaults(func=_mcp)

    sub.add_parser("factory", help="the tares-factory crew of Claude Code sessions: "
                                   "`tares factory help` lists its commands")

    sub.add_parser("hook", help="starts a Claude Code plugin hook: `tares hook <name>` (ship, challenger, guard, ...)")

    li = sub.add_parser("login", help="connect this machine to one of your Tares Cloud workspaces "
                                      "(opens your browser)")
    li.add_argument("--cloud", default="", help="the Tares Cloud console to sign in at (default "
                                                "$TARES_CLOUD_URL or https://console.tares-glassflow.com)")
    li.add_argument("--no-browser", action="store_true", help="only print the address to open")
    li.add_argument("--show", action="store_true", help="print the saved login (never its key) and whether the key works")
    li.add_argument("--cell", default="", help="connect to a Tares of your own at this address instead (with "
                                               "--key-stdin): https://, or http:// for localhost")
    li.add_argument("--key-stdin", action="store_true", help="with --cell: read the key (one line) from standard input")
    li.add_argument("--json", action="store_true", help="machine-readable output: one JSON document per line")
    li.set_defaults(func=_login)
    lo = sub.add_parser("logout", help="remove this machine's key from its workspace and forget the login")
    lo.add_argument("--json", action="store_true", help="machine-readable output")
    lo.set_defaults(func=_logout)

    ca = sub.add_parser("capabilities", help="what this tares can do and what the Tares it is signed in to can do")
    ca.add_argument("--json", action="store_true", help="machine-readable output: one JSON document")
    ca.set_defaults(func=_capabilities)
    return p
