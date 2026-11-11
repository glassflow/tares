"""The watch's readings: what the Crew page asks for between rounds (TR-1163).

Ports `serve_readings` of `claude-plugin/bin/factory`. On every tick of the watch's nap it

  * reads the plan's limits (`plan`, /usage) and sends them to Tares, every FACTORY_PLAN_EVERY seconds
    (default 600, 0 or less or not a number: never). The first tick reads at once. When it cannot read, it
    says so once ("could not read the plan's limits (... plan says why); trying again every Ns") and stays quiet
    until a read works again, because with an API key there is no plan to read;
  * answers the console's asks for a station's /context: Tares lists every crew's asks on GET
    /api/crew/context/asks, and crews repeat station names, so only an ask whose session is the one this watch's
    own station plays is read (a read for another crew's ask would fail every tick).

Standard library plus the crew modules (no duckdb, fastapi or daemon).
"""
from __future__ import annotations

import contextlib
import io
import os
import time

from . import state
from .claude import job_state
from .client import TaresClient
from .errors import CrewError
from .paths import ME, CrewPaths

DEFAULT_PLAN_EVERY = 600   # seconds between reads of the plan, as bash's FACTORY_PLAN_EVERY


class Memory:
    """What bash keeps in PLAN_READ_AT and PLAN_OK between ticks."""

    def __init__(self):
        self.plan_read_at = 0.0
        self.plan_ok = True


MEMORY = Memory()


def plan_every(env) -> int:
    """FACTORY_PLAN_EVERY in whole seconds; 0 when it is 0, negative or not a number (bash's test fails: never)."""
    text = env.get("FACTORY_PLAN_EVERY")
    if text is None or text == "":
        return DEFAULT_PLAN_EVERY
    try:
        return max(int(text), 0)
    except ValueError:
        return 0


def _quietly(fn, *args) -> int:
    """`( fn args >/dev/null 2>&1 )`: its exit code, 1 for anything it raises."""
    sink = io.StringIO()
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            return fn(*args)
    except Exception:
        return 1


def read_plan(env, say, *, now=time.time) -> None:
    every = plan_every(env)
    t = int(now())
    if every <= 0 or t - MEMORY.plan_read_at < every:
        return
    MEMORY.plan_read_at = t
    from . import person   # here, not at the top: person imports the modules that import the watch
    if _quietly(person.cmd_plan, ["plan"], env) == 0:
        MEMORY.plan_ok = True
    elif MEMORY.plan_ok:
        MEMORY.plan_ok = False
        say(f"could not read the plan's limits ({ME} plan says why); trying again every {every}s")


def _asks(client: TaresClient) -> list:
    """[(station, session)] of the asks Tares lists; nothing when it cannot be read."""
    try:
        doc = state.loads_json(client.tares_http("GET", "/api/crew/context/asks"))
    except (CrewError, ValueError):
        return []
    rows = doc.get("asks") if isinstance(doc, dict) else None
    out = []
    for a in rows if isinstance(rows, list) else []:
        if isinstance(a, dict):
            s, ask = a.get("station"), a.get("session")
            out.append(("" if s is None else str(s), "" if ask is None else str(ask)))
    return out


def _own_session(s: str, paths: CrewPaths):
    """The session the station plays now: the job's, else the one the roster keeps. None when the station is not
    on the roster."""
    job = state.stations_get(s, "id", paths=paths)
    if not job:
        return None
    try:
        data = job_state(job)
        sid = data.get("sessionId")
        sid = sid if isinstance(sid, str) else ""
    except CrewError:
        sid = ""
    return sid or state.stations_get(s, "sessionId", paths=paths)


def answer_asks(paths: CrewPaths, env, say) -> None:
    client = TaresClient(paths, env, say)
    try:
        asks = _asks(client)
    finally:
        client.close()
    from . import person
    for s, ask in asks:
        if not s:
            continue
        sid = _own_session(s, paths)
        if sid is None:
            continue   # not on the roster
        if sid != ask and state.stations_get(s, "sessionId", paths=paths) != ask:
            continue
        # bash runs each read in a subshell: one that fails never keeps the others waiting a tick
        sink = io.StringIO()
        try:
            with contextlib.redirect_stdout(sink):
                code = person.cmd_context(["context", s], env)
        except Exception:
            code = 1
        if code != 0:
            say(f"could not read the context of {s}")


def step(paths: CrewPaths, *, env=None, say=None, now=time.time) -> None:
    """One tick of the nap."""
    env = os.environ if env is None else env
    if say is None:
        from .watch import _say as say
    read_plan(env, say, now=now)
    answer_asks(paths, env, say)
