"""The integration sync runner (Tares 2.0, TR-1806): what used to be catalog sources polled by the
runtime. Every integration that keeps something in step with an outside system (the tickets slot's
Linear and GitHub Issues today; a CI or memory system later) has `linked()`, the projects it holds,
and `sync(project)`; the daemon runs one task that calls `sync` for each linked project, every
`sync_interval_s` seconds of that integration. A failure is recorded on the project by the
integration itself and never stops the others.
"""
from __future__ import annotations

import asyncio
import logging

from . import registry

log = logging.getLogger("tares.integrations.sync")

SLOTS = ("tickets",)    # the slots whose integrations sync


def _syncing(store) -> list:
    """An instance of every integration of SLOTS that syncs."""
    out = []
    for slot in SLOTS:
        for cls in registry.available(slot):
            if callable(getattr(cls, "linked", None)) and callable(getattr(cls, "sync", None)):
                try:
                    out.append(cls(store))
                except TypeError:
                    continue
    return out


async def sync_once(store, now: float | None = None, last: dict | None = None) -> list[tuple[str, str]]:
    """Sync every linked project of every syncing integration whose interval has passed (`last`
    holds when each integration last ran; None runs them all). Returns the (integration, project)
    pairs it synced."""
    loop = asyncio.get_running_loop()
    now = loop.time() if now is None else now
    done: list[tuple[str, str]] = []
    for integ in _syncing(store):
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
            try:
                await integ.sync(uid)
                done.append((key, uid))
            except Exception as e:   # noqa: BLE001 — recorded on the project by the integration
                log.info("sync %s %s: %s", key, uid, str(e)[:200])
    return done


async def run(store, stop: asyncio.Event, tick_s: float = 5.0) -> None:
    """The daemon's sync task: wakes every `tick_s` seconds and syncs what is due, until `stop`."""
    last: dict = {}
    while not stop.is_set():
        try:
            await sync_once(store, last=last)
        except Exception as e:   # noqa: BLE001 — the task never dies
            log.warning("sync: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=tick_s)
        except asyncio.TimeoutError:
            pass
