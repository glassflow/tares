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
    for k in ("TARES_DB", "TARES_CATALOG"):
        os.environ.pop(k, None)
    args = types.SimpleNamespace(host="127.0.0.1", port=9999, data_dir=str(tmp), open=False)
    cli._up(args)
    ck("creates the data home", Path(tmp).is_dir())
    ck("points DB at the data home", os.environ["TARES_DB"] == str(Path(tmp) / "tares.duckdb"), os.environ.get("TARES_DB"))
    ck("points catalog at the data home", os.environ["TARES_CATALOG"] == str(Path(tmp) / "catalog.yaml"))
    ck("sets host/port for the daemon", os.environ["TARES_PORT"] == "9999")
    ck("starts the daemon", started.get("ran") is True)


def test_up_respects_explicit_db():
    cli.run_daemon = lambda: None
    os.environ["TARES_DB"] = "/custom/path.duckdb"   # user override must win (setdefault)
    cli._up(types.SimpleNamespace(host="0.0.0.0", port=1, data_dir="/tmp/nf_x", open=False))
    ck("explicit TARES_DB is not overridden", os.environ["TARES_DB"] == "/custom/path.duckdb")
    os.environ.pop("TARES_DB", None)


def test_ui_dist_dev_fallback():
    os.environ.pop("TARES_UI_DIST", None)
    from tares.daemon import _ui_dist
    ck("dev tree resolves to ui/dist", str(_ui_dist()).endswith("ui/dist"))


def test_parser_builds():
    # no subcommand -> prints help, no crash
    sys.argv = ["tares"]
    cli.main()
    ck("main() with no args is a no-op (prints help)", True)


test_up_wires_data_home("/tmp/nf_cli_home")
test_up_respects_explicit_db()
test_ui_dist_dev_fallback()
test_parser_builds()


# ── first run: which Tares is this? (TR-755) ─────────────────────────────────
import contextlib, io, tempfile  # noqa: E402


class _Tty(io.StringIO):
    def isatty(self):
        return True


def _up_with(tty, edition=None, env_edition=None, answers=(), seed_db=None, state_dsn=None):
    """Run `tares up` against a fresh temp data dir with run_daemon stubbed; returns
    (env TARES_EDITION after, questions asked, printed text, data dir)."""
    d = tempfile.mkdtemp(prefix="nf_cli_ed_")
    for k in ("TARES_DB", "TARES_CATALOG", "TARES_EDITION", "TARES_STATE_DSN", "TARES_AUTH_TOKEN"):
        os.environ.pop(k, None)
    if env_edition:
        os.environ["TARES_EDITION"] = env_edition
    if state_dsn:
        os.environ["TARES_STATE_DSN"] = state_dsn
    if seed_db:
        seed_db(str(Path(d) / "tares.duckdb"))
    asked = []
    feed = list(answers)
    real_ask = cli._ask_edition
    cli._ask_edition = lambda: asked.append(1) or real_ask(read=lambda _p="": feed.pop(0), write=lambda *_a: None)
    cli.run_daemon = lambda: None
    old_in, old_out = sys.stdin, sys.stdout
    out = _Tty() if tty else io.StringIO()
    try:
        if tty:
            sys.stdin = _Tty()
        with contextlib.redirect_stdout(out):
            if tty:
                sys.stdout = out
            cli._up(types.SimpleNamespace(host="127.0.0.1", port=1, data_dir=d, open=False, edition=edition))
    finally:
        sys.stdin, sys.stdout = old_in, old_out
        cli._ask_edition = real_ask
    got = os.environ.pop("TARES_EDITION", None)
    for k in ("TARES_DB", "TARES_CATALOG", "TARES_STATE_DSN"):
        os.environ.pop(k, None)
    return got, len(asked), out.getvalue(), d


def _seed_db(path, edition=None):
    from tares.store import Store
    s = Store(path)
    if edition:
        s.set_setting("edition", edition)
    s.con.close()


def test_first_run_edition():
    got, n, out, _ = _up_with(False, edition="sre")
    ck("--edition sre sets TARES_EDITION for the daemon", got == "sre", got)
    ck("--edition never asks", n == 0)
    ck("--edition names the Tares", "tares: AI SRE" in out, out)

    got, n, out, _ = _up_with(False, env_edition="sre")
    ck("TARES_EDITION is kept and never asked", got == "sre" and n == 0, (got, n))

    got, n, out, _ = _up_with(False)
    ck("no terminal and a fresh data dir: no question", n == 0, n)
    ck("no terminal: TARES_EDITION is left unset for the console to ask", got is None, got)

    got, n, out, _ = _up_with(True, answers=["2"])
    ck("terminal + fresh + answer 2 -> sre", got == "sre" and n == 1, (got, n))
    ck("the chosen Tares is named", "tares: AI SRE" in out, out)

    got, n, out, _ = _up_with(True, answers=[""])
    ck("terminal + fresh + Enter -> factory", got == "factory" and n == 1, (got, n))
    ck("factory is named", "tares: Software Factory" in out, out)

    got, n, out, _ = _up_with(True, edition="factory", answers=["2"])
    ck("terminal + --edition: no question", n == 0 and got == "factory", (got, n))

    got, n, out, _ = _up_with(True, answers=["2"], seed_db=_seed_db)
    ck("an existing data file is never asked", n == 0, n)
    ck("an existing data file leaves TARES_EDITION unset (the daemon settles it)", got is None, got)

    got, n, out, _ = _up_with(True, answers=["2"], state_dsn="postgresql://x@localhost/none")
    ck("a Postgres state store is never asked in the terminal", n == 0, n)

    got, n, out, _ = _up_with(False, edition="sre", seed_db=lambda p: _seed_db(p, "factory"))
    ck("a stored edition wins over --edition", got is None, got)
    ck("and the difference is said in words", "tares: this Tares is Software Factory; change it in Settings" in out, out)
    ck("a stored edition is named", "tares: Software Factory" in out, out)


def test_ask_edition():
    def run(answers):
        feed, said = list(answers), []
        r = cli._ask_edition(read=lambda _p="": feed.pop(0), write=said.append)
        return r, said, feed
    r, said, _ = run(["1"])
    ck("1 -> factory", r == "factory")
    ck("the question names both and says what each is for",
       "Software Factory" in said[0] and "AI SRE" in said[0] and "Choose 1 or 2 [1]:" in said[0], said)
    ck("2 -> sre", run(["2"])[0] == "sre")
    ck("Enter -> factory", run([""])[0] == "factory")
    r, said, left = run(["x", "2"])
    ck("a bad answer asks again", r == "sre" and len(said) == 2 and not left, (r, said))
    r, said, left = run(["x", "y", "z"])
    ck("three bad answers -> factory", r == "factory" and not left, (r, left))
    ck("no em dash in the question", "\u2014" not in cli.EDITION_QUESTION)

    def eof(_p=""):
        raise EOFError
    ck("input ending early -> factory", cli._ask_edition(read=eof, write=lambda *_a: None) == "factory")
    sys.argv = ["tares", "up", "--help"]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            cli.main()
    except SystemExit:
        pass
    ck("--edition is a flag of up, with its help", "--edition" in buf.getvalue() and "factory" in buf.getvalue(), buf.getvalue())
    sys.argv = ["tares", "up", "--edition", "nope"]
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            cli.main()
        bad = False
    except SystemExit as e:
        bad = e.code != 0
    ck("--edition rejects a value that is not an edition", bad)


test_first_run_edition()
test_ask_edition()



# ── 1.0 renamed every env var, with NO fallback ──────────────────────────────
# Deliberately breaking. What must NOT happen is a SILENT break: a daemon started with only
# NAVFLOW_DB set would ignore it, open a DuckDB file somewhere else, and come up healthy and EMPTY
# — indistinguishable from data loss. So it refuses, and names the variable to set instead.
from tares.config import reject_legacy_db, reject_legacy_env  # noqa: E402

ck("no legacy vars -> starts normally", reject_legacy_env({"TARES_DB": "/tmp/x.duckdb"}) is None)

try:
    reject_legacy_env({"NAVFLOW_DB": "/tmp/x.duckdb"})
    ck("a legacy var refuses to start", False, "it started anyway")
except SystemExit as e:
    ck("a legacy var refuses to start", True)
    ck("...and names the variable to use instead", "TARES_DB" in str(e), str(e)[:120])
    ck("...and says the data is still there", "data is where you left it" in str(e), str(e)[:200])

try:
    reject_legacy_env({"NAVFLOW_AUTH_TOKEN": "t", "NAVFLOW_PORT": "8787"})
    ck("every legacy var is listed, not just the first", False, "it started anyway")
except SystemExit as e:
    ck("every legacy var is listed, not just the first",
       "TARES_AUTH_TOKEN" in str(e) and "TARES_PORT" in str(e), str(e)[:200])

# ── the DuckDB file was renamed too, and DuckDB would happily create a new empty one ─────────
import tempfile as _tf  # noqa: E402

with _tf.TemporaryDirectory() as _d:
    _new = os.path.join(_d, "tares.duckdb")
    _old = os.path.join(_d, "navflow.duckdb")

    ck("a genuinely fresh install starts", reject_legacy_db(_new) is None)

    open(_old, "w").close()          # upgraded install: old file present, new one absent
    try:
        reject_legacy_db(_new)
        ck("a pre-1.0 database next door refuses to start", False, "it started anyway")
    except SystemExit as e:
        ck("a pre-1.0 database next door refuses to start", True)
        ck("...and gives the exact mv command", f"mv {_old} {_new}" in str(e), str(e)[:200])

    open(_new, "w").close()          # already migrated: both present, new one wins
    ck("once migrated it starts normally", reject_legacy_db(_new) is None)

print(f"\n{P} passed, {F} failed")
raise SystemExit(1 if F else 0)
