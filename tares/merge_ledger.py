"""The merge ledger (TR-1672): one `merge_records` row of the session store per merge through the
merge queue, saying what that merge went through.

merge_queue.py works out the values and writes them here as the merge happens; the Report
(report.py) reads them. A record is keyed by its queue row's id. One with no `merged_at` is a merge
that has not been seen: a person's yes noted ahead of it, or a merge the queue asked GitHub for and
has no answer on yet. The readers leave those out.

Nothing here catches an error: the merge queue does, so that a write that fails never holds a merge
up.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from .session_store.tables import merge_records


def get(store, queue_row: str) -> dict | None:
    """The record of one queue row, or None."""
    with store.sessions_db.begin() as conn:
        r = conn.execute(select(merge_records).where(
            merge_records.c.queue_row == queue_row)).mappings().first()
    return dict(r) if r else None


def save(store, row: dict, **values) -> None:
    """Write `values` on the record of the queue row `row`, making the record when there is none.
    What `values` does not name is left as it is."""
    what = {"project": row["project_uid"], "ticket": row["ticket"], "repo": row["repo"],
            "pr": row["pr"], **values}
    stmt = store.sessions_db.upsert(merge_records).values(queue_row=row["id"], **what)
    with store.sessions_db.begin() as conn:
        conn.execute(stmt.on_conflict_do_update(index_elements=["queue_row"], set_=what))


def merged_between(store, projects: list[str], since: datetime, until: datetime) -> list[dict]:
    """The merges of these projects seen from `since` up to, and not including, `until`, newest
    first. No project, no rows."""
    if not projects:
        return []
    with store.sessions_db.begin() as conn:
        rows = conn.execute(
            select(merge_records)
            .where(merge_records.c.project.in_(list(projects)),
                   merge_records.c.merged_at >= since, merge_records.c.merged_at < until)
            .order_by(merge_records.c.merged_at.desc(), merge_records.c.queue_row)).mappings().all()
    return [dict(r) for r in rows]


def recorded(store, queue_rows: list[str]) -> set[str]:
    """Which of these queue rows have a record of a merge that was seen."""
    if not queue_rows:
        return set()
    with store.sessions_db.begin() as conn:
        rows = conn.execute(select(merge_records.c.queue_row).where(
            merge_records.c.queue_row.in_(list(queue_rows)),
            merge_records.c.merged_at.is_not(None))).all()
    return {r[0] for r in rows}
