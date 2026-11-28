"""What a daemon starts in the background (Tares 2.0, TR-1813): the names in app.state.started_tasks
after the lifespan has run are exactly the list below, in order. The cell has one kind, so the list
does not depend on any setting: a task added or removed changes this list in the same change.

The session threads start in make_app (the sessions reconcile, the one-time move, the digest warm-up);
the lifespan starts the rest. An ee team adds no task here: the brain's tasks start when first asked.

Run: python tests/test_factory_startup.py
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from _env import scrub_os_environ
scrub_os_environ()

TMP = tempfile.mkdtemp(prefix="tares-factory-startup-")
os.environ["TARES_DB"] = os.path.join(TMP, "startup.duckdb")
os.environ.pop("TARES_AUTH_TOKEN", None)

EXPECTED = [
    "session_reconcile", "session_move", "session_digest",
    "provider_models", "event_loop_watch", "pr_sync", "morning_status", "licence", "waiting_slack",
    "session_archive", "report", "integration_sync",
]

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


async def main():
    from tares.daemon import make_app
    app = make_app()
    async with app.router.lifespan_context(app):
        got = list(getattr(app.state, "started_tasks", None) or [])
        ck("the background tasks are exactly the expected list, in order", got == EXPECTED,
           f"added {[n for n in got if n not in EXPECTED]}, missing {[n for n in EXPECTED if n not in got]},"
           f" got {got}")
        ck("each is named once", len(set(got)) == len(got), got)
    print(f"\n{P} passed, {F} failed")
    raise SystemExit(1 if F else 0)


asyncio.run(main())
