"""tares CLI — `tares up` wires a data home and starts the daemon (without actually serving)."""
import os, sys, types
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

import tares.cli as cli

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


def test_up_wires_data_home(tmp):
    started = {}
    cli.run_daemon = lambda: started.setdefault("ran", True)  # don't actually serve
    for k in ("TARES_CATALOG",):
        os.environ.pop(k, None)
    args = types.SimpleNamespace(host="127.0.0.1", port=9999, data_dir=str(tmp), open=False)
    cli._up(args)
    ck("creates the data home", Path(tmp).is_dir())
    ck("points DB at the data home", os.environ["TARES_HOME"] == str(Path(tmp) / "tares"), os.environ.get("TARES_HOME"))
    ck("no catalog file is pointed at (the source catalog went, TR-1811)", "TARES_CATALOG" not in os.environ)
    ck("sets host/port for the daemon", os.environ["TARES_PORT"] == "9999")
    ck("starts the daemon", started.get("ran") is True)


def test_up_respects_explicit_db():
    cli.run_daemon = lambda: None
    os.environ["TARES_HOME"] = "/custom/path"   # user override must win (setdefault)
    cli._up(types.SimpleNamespace(host="0.0.0.0", port=1, data_dir="/tmp/nf_x", open=False))
    ck("explicit TARES_HOME is not overridden", os.environ["TARES_HOME"] == "/custom/path")
    os.environ.pop("TARES_HOME", None)


def test_ui_dist_dev_fallback():
    os.environ.pop("TARES_UI_DIST", None)
    from tares.daemon import _ui_dist
    ck("dev tree resolves to ui/dist", str(_ui_dist()).endswith("ui/dist"))


def test_parser_builds():
    # no subcommand -> prints help, no crash
    sys.argv = ["tares"]
    cli.main()
    ck("main() with no args is a no-op (prints help)", True)


def test_mcp_stdio_keeps_stdout_clean():
    # over stdio, stdout is the MCP channel: a startup line there breaks the client's session
    import contextlib, io
    real_run_mcp, cli.run_mcp = cli.run_mcp, lambda: None  # don't actually serve
    out, err = io.StringIO(), io.StringIO()
    args = types.SimpleNamespace(transport="stdio", host="127.0.0.1", port=8788, taresd="")
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            cli._mcp(args)
    finally:
        cli.run_mcp = real_run_mcp
    ck("`tares mcp --transport stdio` writes nothing to stdout", out.getvalue() == "", out.getvalue())
    ck("...and says where it proxies to on stderr", "tares-mcp: stdio" in err.getvalue(), err.getvalue())


test_up_wires_data_home("/tmp/nf_cli_home")
test_up_respects_explicit_db()
test_ui_dist_dev_fallback()
test_parser_builds()
test_mcp_stdio_keeps_stdout_clean()


# ── `tares up` asks nothing about an edition (TR-1813) ──────────────────────
import contextlib, io, tempfile  # noqa: E402


class _Tty(io.StringIO):
    def isatty(self):
        return True

    def readline(self, *_a):
        raise AssertionError("tares up read the terminal")


def test_up_asks_nothing():
    d = tempfile.mkdtemp(prefix="nf_cli_up_")
    for k in ("TARES_CATALOG", "TARES_EDITION", "TARES_STATE_DSN", "TARES_AUTH_TOKEN"):
        os.environ.pop(k, None)
    cli.run_daemon = lambda: None
    old_in, old_out = sys.stdin, sys.stdout
    out = _Tty()
    import builtins
    real_input, asked = builtins.input, []
    builtins.input = lambda *_a: asked.append(1) or ""
    try:
        sys.stdin = _Tty()
        with contextlib.redirect_stdout(out):
            sys.stdout = out
            cli._up(types.SimpleNamespace(host="127.0.0.1", port=1, data_dir=d, open=False))
    finally:
        sys.stdin, sys.stdout = old_in, old_out
        builtins.input = real_input
    ck("a terminal and a fresh data dir: no question", asked == [], asked)
    ck("TARES_EDITION is not set for the daemon", os.environ.get("TARES_EDITION") is None)
    ck("the start lines name no edition", "Software Factory" not in out.getvalue()
       and "AI SRE" not in out.getvalue() and "edition" not in out.getvalue(), out.getvalue())
    for k in ("TARES_CATALOG",):
        os.environ.pop(k, None)

    sys.argv = ["tares", "up", "--help"]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            cli.main()
    except SystemExit:
        pass
    ck("up has no --edition flag", "--edition" not in buf.getvalue() and "--data-dir" in buf.getvalue(),
       buf.getvalue())
    sys.argv = ["tares", "up", "--edition", "factory"]
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            cli.main()
        bad = False
    except SystemExit as e:
        bad = e.code != 0
    ck("--edition is refused as an unknown argument", bad)


test_up_asks_nothing()


print(f"\n{P} passed, {F} failed")
raise SystemExit(1 if F else 0)
