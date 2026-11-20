"""Seed and read the five session tables in a test (P-TR-295).

A test that needs a row in `session_stations`, `session_states`, `project_sessions`,
`session_usage` or `session_meters` writes it here instead of through `store.sc`: the rows live in
the session store (`store.sessions_db`), not in the state database. The helpers take a Store and use
the same SQLAlchemy Core tables the Store's methods do.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, insert, select, update

from tares.session_store.tables import (project_sessions, session_meters, session_stations,
                                        session_states, session_usage)

TABLES = {t.name: t for t in (project_sessions, session_meters, session_stations, session_states,
                              session_usage)}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def seed_station(store, session: str, station: str, role: str | None = None, parent: str | None = None,
                 project: str | None = None, seen_at: datetime | None = None,
                 crew_id: str | None = None) -> None:
    """A row in session_stations, as it was before the Store's own write (no upsert: a new session)."""
    with store.sessions_db.begin() as conn:
        conn.execute(insert(session_stations).values(
            session=session, station=station, role=role, parent=parent, project=project,
            seen_at=seen_at or _now(), crew_id=crew_id))


def seed_link(store, project: str, session: str, repo: str | None = None,
              linked_at: datetime | None = None) -> None:
    """A row in project_sessions."""
    with store.sessions_db.begin() as conn:
        conn.execute(insert(project_sessions).values(
            project=project, session=session, repo=repo, linked_at=linked_at or _now()))


def seed_state(store, session: str, state: str, reason: str | None = None,
               state_at: datetime | None = None) -> None:
    """A row in session_states."""
    with store.sessions_db.begin() as conn:
        conn.execute(insert(session_states).values(
            session=session, state=state, reason=reason, state_at=state_at or _now()))


def seed_usage(store, session: str, message: str, model: str | None = "claude-opus-5-5",
               replied_at: datetime | None = None, project: str | None = None, **cols) -> None:
    """A row in session_usage; `cols` are any of its other columns (input_tokens, cost_usd, ...)."""
    with store.sessions_db.begin() as conn:
        conn.execute(insert(session_usage).values(
            session=session, message=message, model=model, project=project,
            replied_at=replied_at or _now(), **cols))


def rows(store, table: str, *where) -> list[dict]:
    """Every row of the named session table (matching the Core conditions in `where`) as dicts."""
    t = TABLES[table]
    with store.sessions_db.begin() as conn:
        return [dict(r._mapping) for r in conn.execute(select(t).where(*where))]


def count(store, table: str, *where) -> int:
    return len(rows(store, table, *where))


def delete_rows(store, table: str, *where) -> None:
    with store.sessions_db.begin() as conn:
        conn.execute(delete(TABLES[table]).where(*where))


def set_cols(store, table: str, where: list, **values) -> None:
    """UPDATE the named session table: `set_cols(store, "session_usage", [cond, ...], cost_usd=None)`."""
    with store.sessions_db.begin() as conn:
        conn.execute(update(TABLES[table]).where(*where).values(**values))
