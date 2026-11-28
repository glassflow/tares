"""The project timeline (TR-331): what happened in one project, newest first.

It used to hold one thread per trigger firing or agent run. Triggers and agents went (TR-1810) and
their tables with the source catalog (TR-1811), so no thread is left: the page shows the hand-overs
pushed through without every sign-off (`override_events`).
"""
from __future__ import annotations

from datetime import datetime, timezone

# the outcomes a filter may name; kept so an old client's filter still answers (with no threads)
OUTCOMES = ("finding", "no_op", "failed", "capped", "empty", "exhausted", "running", "none")


def project_timeline(store, project: str, limit: int = 50, before: datetime | None = None,
                     trigger: str = "", agent: str = "", outcome: str = "", entity: str = "",
                     scheduled: set | None = None) -> dict:
    """{threads, next_before}: no threads, and nothing older to page to."""
    return {"threads": [], "next_before": None}


def _utc(v) -> datetime:
    if isinstance(v, str):
        v = datetime.fromisoformat(v.replace("Z", "+00:00"))
    return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


def override_events(overrides: list[dict], before: datetime | None,
                    next_before: str | None) -> list[dict]:
    """The hand-overs pushed through without every sign-off that belong on one page of the
    timeline, oldest first (TR-640). A page covers [next_before, before): threads there are
    older than `before` and not older than `next_before`, which is the next page's `before`.
    The first page has no `before`, so it takes everything newer than its oldest thread, and the
    last page has no `next_before`, so it takes everything older. A time sits on exactly one page."""
    hi = _utc(before) if before else None
    lo = _utc(next_before) if next_before else None
    out = []
    for o in overrides:
        at = _utc(o["at"])
        if (hi is not None and at >= hi) or (lo is not None and at < lo):
            continue
        who = o.get("by_label") or (o.get("person") or {}).get("name")
        out.append({"kind": "handover_override", "at": o["at"], "reason": o["reason"], "by": who,
                    "text": "Handed to the crew without every area owner's sign-off: "
                            + o["reason"]})
    return out
