"""The daemon's event loop, for code that starts background work and may be called from a worker
thread: request handlers that only read and write the store run in FastAPI's thread pool, so the
cell serves several requests at once (P-TR-268), and some of them start a poll loop, an agent run
or a trigger catch-up on the way."""
from __future__ import annotations

import asyncio

_main: asyncio.AbstractEventLoop | None = None


def attach(loop: asyncio.AbstractEventLoop) -> None:
    """Remember the daemon's loop (called once it runs)."""
    global _main
    _main = loop


def spawn(coro):
    """Run `coro` as a task on the daemon's loop: directly when called on it, handed over when
    called from a thread. Returns the task, or a concurrent Future when handed over; both have
    done(), cancel() and add_done_callback()."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        return loop.create_task(coro)
    if _main is None or _main.is_closed():
        coro.close()
        raise RuntimeError("no event loop to start background work on")
    return asyncio.run_coroutine_threadsafe(coro, _main)
