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
