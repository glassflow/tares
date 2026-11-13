"""Where the crew's memory lines go (TR-865): the Tares doc first, always, then the cell's memory
integration when it is not Tares.

`write` is what `remember_for_project` and `add_to_global(doc="memory")` call. The Tares doc takes
the line in every case, so what a session reads on its next start never depends on another
service being up. A line the other integration does not take is kept in `pending_memory`, and
`retry_pending` offers it again on every round of the daemon's background sync, until the
integration takes it or the cell goes back to Tares memory (the doc already has the line).

`recall` asks the integration the cell uses. When a non-Tares integration does not answer, the
Tares docs answer instead, and the answer says so.
"""
from __future__ import annotations

import asyncio

from .integrations import registry, resolve
from .integrations.builtin import memory_tares as mt


def _integration(store):
    """(name, an instance of the memory integration in use) for a non-Tares choice; (name, None)
    for Tares, or when the chosen integration cannot be built."""
    eff = resolve.effective("memory", None, store=store)
    if eff.integration == "tares":
        return "tares", None
    cls = registry.get("memory", eff.integration)
    if cls is None:
        return "tares", None
    try:
        return eff.integration, cls(store)
    except Exception:
        return eff.integration, None


async def write(store, scope: str, text, note: str = "", by: str = "tares") -> dict:
    """Keep `text` in the memory of `scope` (a project's id, or `factory`). Returns {doc_id, added,
    line, integration, pending}: `pending` is True when a non-Tares integration did not take the
    line and it waits for a retry. Raises docs.DocError for a bad line and KeyError for a project
    that does not exist, with nothing written."""
    kept = await asyncio.to_thread(mt.write_line, store, scope, text, note, by)
    name, inst = await asyncio.to_thread(_integration, store)
    out = {**kept, "integration": name, "pending": False}
    if not kept["added"] or name == "tares":
        return out
    try:
        if inst is None:
            raise RuntimeError(f"the {name} memory integration could not be started")
        await inst.add(scope, kept["line"])
    except asyncio.CancelledError:
        raise
    except Exception as e:
        await asyncio.to_thread(store.add_pending_memory, scope, kept["line"],
                                f"{type(e).__name__}: {e}")
        out["pending"] = True
    return out


async def retry_pending(store) -> int:
    """Offer every waiting line to the integration again; returns how many it took. With the cell
    on Tares memory (or the integration gone) the rows are dropped: the Tares doc has them."""
    rows = await asyncio.to_thread(store.list_pending_memory)
    if not rows:
        return 0
    name, inst = await asyncio.to_thread(_integration, store)
    if name == "tares":
        for r in rows:
            await asyncio.to_thread(store.delete_pending_memory, r["id"])
        return 0
    taken = 0
    for r in rows:
        try:
            if inst is None:
                raise RuntimeError(f"the {name} memory integration could not be started")
            await inst.add(r["scope"], r["line"])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await asyncio.to_thread(store.retried_pending_memory, r["id"], f"{type(e).__name__}: {e}")
            continue
        await asyncio.to_thread(store.delete_pending_memory, r["id"])
        taken += 1
    return taken


async def recall(store, scope: str, question: str, limit: int = 10) -> dict:
    """{lines, integration, note}: what the memory of `scope` holds about `question`. `note` is ""
    unless the integration did not answer and the Tares docs did."""
    name, inst = await asyncio.to_thread(_integration, store)
    note = ""
    if inst is not None:
        try:
            return {"lines": list(await inst.recall(scope, question, limit))[:mt.MAX_LIMIT],
                    "integration": name, "note": ""}
        except asyncio.CancelledError:
            raise
        except Exception as e:
            note = f"The {name} memory did not answer ({type(e).__name__}); this is from the Tares docs."
    elif name != "tares":
        note = f"The {name} memory is not available; this is from the Tares docs."
    lines = await mt.MemoryTares(store).recall(scope, question, limit)
    return {"lines": lines, "integration": "tares", "note": note}
