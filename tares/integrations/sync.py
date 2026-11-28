"""The integration sync runner (Tares 2.0, TR-1806): what used to be catalog sources polled by the
runtime. Every integration that keeps something in step with an outside system (the tickets slot's
Linear and GitHub Issues today; a CI or memory system later) has `linked()`, the projects it holds,
and `sync(project)`; the daemon runs one task that calls `sync` for each linked project, every
`sync_interval_s` seconds of that integration.

- A sync gets SYNC_TIMEOUT_S; one slow project never holds up the others for longer.
- A sync of a project, from here or from a route (link, Sync now), holds that project's lock
  (`project_lock`), so two never read and write the same tickets at once.
- A failure is recorded on the project by the integration itself and never stops the others. The
  first one is logged as a warning; a project that keeps failing is retried less often, up to
  MAX_BACKOFF_S, and back to every interval once it syncs.
"""
from __future__ import annotations

import asyncio
import logging

from . import registry

log = logging.getLogger("tares.integrations.sync")

SLOTS = ("tickets",)       # the slots whose integrations sync
SYNC_TIMEOUT_S = 120.0     # one project's sync, at most
MAX_BACKOFF_S = 30 * 60    # a project that keeps failing is tried at least this often

_locks: dict[tuple[str, str], asyncio.Lock] = {}


def project_lock(integration: str, uid: str) -> asyncio.Lock:
    """The lock every sync of project `uid` through `integration` ("linear", "github") holds."""
    key = (integration, uid)
    lock = _locks.get(key)
    if lock is None:
        lock = _locks[key] = asyncio.Lock()
    return lock


def _syncing(store) -> list:
    """An instance of every integration of SLOTS that syncs. One that cannot be built is skipped."""
    out = []
    for slot in SLOTS:
        for cls in registry.available(slot):
            if not (callable(getattr(cls, "linked", None)) and callable(getattr(cls, "sync", None))):
                continue
            try:
                out.append(cls(store))
            except Exception as e:   # noqa: BLE001 — one integration must not stop the others
                log.warning("sync: could not set up %s %s: %s", slot, getattr(cls, "name", cls), e)
    return out


class _Failures:
    """Consecutive failures per (integration, project), for the warning and the backoff."""

    def __init__(self):
        self.count: dict[tuple[str, str], int] = {}
        self.next_at: dict[tuple[str, str], float] = {}

    def due(self, key, now: float) -> bool:
        return now >= self.next_at.get(key, float("-inf"))

    def failed(self, key, now: float, every: float, err: Exception) -> None:
        n = self.count.get(key, 0) + 1
        self.count[key] = n
        self.next_at[key] = now + min(every * (2 ** (n - 1)), MAX_BACKOFF_S)
        (log.warning if n == 1 else log.info)("sync %s %s failed (%d in a row): %s",
                                              key[0], key[1], n, str(err)[:200])

    def ok(self, key) -> None:
        self.count.pop(key, None)
        self.next_at.pop(key, None)


async def sync_once(store, now: float | None = None, last: dict | None = None,
                    failures: _Failures | None = None, integrations: list | None = None
                    ) -> list[tuple[str, str]]:
    """Sync every linked project of every syncing integration whose interval has passed (`last`
    holds when each integration last ran; None runs them all). Returns the (integration, project)
    pairs it synced."""
    loop = asyncio.get_running_loop()
    now = loop.time() if now is None else now
    failures = failures or _Failures()
    done: list[tuple[str, str]] = []
    for integ in (integrations if integrations is not None else _syncing(store)):
        key = f"{integ.slot}:{integ.name}"
        every = float(getattr(integ, "sync_interval_s", 60) or 60)
        if last is not None and now - last.get(key, float("-inf")) < every:
            continue
        if last is not None:
            last[key] = now
        try:
            projects = await asyncio.to_thread(integ.linked)
        except Exception as e:   # noqa: BLE001 — one integration must not stop the others
            log.warning("sync %s: could not list its projects: %s", key, e)
            continue
        for uid in projects:
            fkey = (key, uid)
            if not failures.due(fkey, now):
                continue
            try:
                async with project_lock(integ.name, uid):
                    await asyncio.wait_for(integ.sync(uid), timeout=SYNC_TIMEOUT_S)
                done.append(fkey)
                failures.ok(fkey)
            except Exception as e:   # noqa: BLE001 — recorded on the project by the integration
                if isinstance(e, asyncio.TimeoutError):
                    e = TimeoutError(f"no answer in {SYNC_TIMEOUT_S:.0f}s")
                failures.failed(fkey, now, every, e)
    return done


async def run(store, stop: asyncio.Event, tick_s: float = 5.0) -> None:
    """The daemon's sync task: wakes every `tick_s` seconds and syncs what is due, until `stop`."""
    last: dict = {}
    failures = _Failures()
    integrations = _syncing(store)
    while not stop.is_set():
        try:
            await sync_once(store, last=last, failures=failures, integrations=integrations)
        except Exception as e:   # noqa: BLE001 — the task never dies
            log.warning("sync: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=tick_s)
        except asyncio.TimeoutError:
            pass
