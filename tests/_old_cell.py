"""How a cell wrote its transcript lines before Tares 2.0 (TR-1813), for the tests of what reads or
moves lines that only DuckDB holds.

A cell of the platform edition wrote each claude_code line to DuckDB's events first, then counted it
in the session store (`session_ingest.record`). Every cell now writes the session store first
(`Store.append`), so a test that needs the old shape writes it with `append_duckdb_first`.

Use: sys.path.insert(0, os.path.dirname(__file__)); from _old_cell import append_duckdb_first
"""
from tares import session_events, session_ingest


def append_duckdb_first(store, envelopes, record: bool = True) -> None:
    """Write `envelopes` as a platform cell did: DuckDB first, then (with `record`) their counts,
    turns and facts in the session store. The lines themselves stay in DuckDB alone."""
    if not envelopes:
        return
    cc = session_ingest.claude_code_lines(store, envelopes)
    if not cc:
        store._append(envelopes)
        return
    session_events.note(store, cc)
    with store._sess_lock:
        store._append(envelopes)
        if record:
            session_ingest.record(store, cc)


FILLS = (session_ingest.USAGE_FILLED, session_ingest.CHALLENGES_FILLED, session_ingest.FACTS_FILLED,
         session_ingest.TURNS_FILLED)


def forget_fills(store) -> None:
    """Drop the marks of the fills that read DuckDB once (TR-1613), as on a cell upgraded from a
    platform edition, which never set them: the next fill reads DuckDB's events again."""
    from tares.session_store.tables import session_ingest_marks
    with store.sessions_db.begin() as conn:
        conn.execute(session_ingest_marks.delete().where(session_ingest_marks.c.name.in_(FILLS)))


def reconcile_as_before(store) -> int:
    """`session_ingest.reconcile_sessions` reading DuckDB even once a move on this store is complete,
    as an older cell's start did; a cell now reads no DuckDB line then. For a test that seeds
    DuckDB-only sessions after an earlier move finished."""
    from tares import session_move
    real = session_move.events_done
    session_move.events_done = lambda _store: False
    try:
        return session_ingest.reconcile_sessions(store)
    finally:
        session_move.events_done = real
