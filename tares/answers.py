"""The record of every answer a person gives to what waited for them (TR-1451).

Each route that takes such an answer (a desk reply or permission, calls kept, a project put off or
closed, a grant agreed or declined, a spec question answered) records one row: the home item it
answered (`question:D7`, `grant:G2`), its ask as it read then, yes or no where the item has two
ways, the words or option given, who, and where. GET /api/answers reads them back, for the feed's
"Answered today".

Where: the console's own calls are `console`; the feed sends the same calls with the header
`X-Tares-Via: feed`, so its answers take Home's path and still say where they came from. A desk
answer matched in the person's session is `session`. The answer itself never depends on its record:
a record that cannot be written is logged, and the answer stands.
"""

from __future__ import annotations

import logging

from .envelope import now_utc

log = logging.getLogger("tares.answers")

HEADER = "x-tares-via"
# the surfaces a console call may name; `session` is set by the route that matched the words
FROM_CONSOLE = ("feed", "message")


def surface(request) -> str:
    """Where a console call was made: `feed` or `message` when it says so, else `console`."""
    v = str(request.headers.get(HEADER) or "").strip().lower() if request is not None else ""
    return v if v in FROM_CONSOLE else "console"


def record(store, request, *, item: str, kind: str, project: str | None, ask: str,
           said: str, choice: str | None = None, via: str | None = None,
           person_id: str | None = None) -> dict | None:
    """Record one answer. `via` None takes it from the request. Returns the row, or None when it
    could not be written (logged). Every kept home is forgotten either way: what waited was just
    answered, and a page opened next (Home after the feed) must not show it waiting for a poll more."""
    try:
        return store.add_answer(item, kind, project, (ask or "")[:300], choice,
                                (said or "")[:500], via or surface(request), person_id, now_utc())
    except Exception:
        log.exception("could not record the answer to %s", item)
        return None
    finally:
        from . import factory_home   # imported here: factory_home imports the routes that import this
        factory_home.clear_cache()
