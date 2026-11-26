"""The transcript lines only the session store holds, for the readers of DuckDB `events` (TR-1647).

In the factory edition new `claude_code` lines are written to the session store alone
(`Store._lines_skip_duckdb`). A reader of `events` asks here for what to put after FROM, and runs
the SQL it always ran. For a session born in the table (`lines_origin = 'table'`, which a verified
move sets too) the lines come from `session_lines` and the archive, and DuckDB's rows of it are
left out; every other session is read from DuckDB. The platform editions, and a read that touches
no such session, get the plain `events` table.

A born session that is not marked complete has lines the session store refused, and those are in
DuckDB until the drain copies them (TR-1665). It is read from both: DuckDB's rows, plus the stored
lines DuckDB does not hold.

A line in the table has no ingest time and no source of its own: its `ingest_time` is the line's
own time, and its `source` is the session's.

One session id under two claude_code sources has one row and its stored lines carry no source:
the lines of the other source go to DuckDB too. Such a born session (DuckDB has its id under another
source, `_duck_sources`) is read as one not marked complete, so each line is counted once and a read
of one source gives that source's lines.

A part or a row of the archive that cannot be read is left out and one line says so, as a session
page does (`session_read`). Nothing here writes, and nothing takes SQLite's write lock
(`consistent_read`), so ingest never waits for one of these reads. The reads that only count and
ask for times are answered from `session_digest`, a cache.
"""
from __future__ import annotations

import json
import threading
from collections import Counter, namedtuple

from sqlalchemy import exists, func, select

from . import edition, session_archive, session_digest, session_read
from .session_ingest import TABLE, claude_code_sources
from .session_store.tables import session_archive_parts, session_lines, sessions, settings

CHUNK = 500                   # sessions, or line numbers, one statement names
_MIN = session_read._MIN
# the columns of a born session's row: what a read needs, and all a change of its lines changes
_BORN = ("session", "source", "started_at", "last_line_at", "line_count", "tool_calls",
         "coordination_calls", "lines_complete", "updated_at", "lines_origin")
Born = namedtuple("Born", _BORN)
_creating = threading.Lock()


def _duck_state(store) -> dict:
    got = store.__dict__.get("_duck_sources")
    if got is None:
        with _creating:
            got = store.__dict__.setdefault(
                "_duck_sources", {"loaded": False, "by": {}, "lock": threading.Lock()})
    return got


def _add_pairs(state: dict, pairs) -> None:
    by = state["by"]
    with state["lock"]:
        for source, sid in pairs:
            had = by.get(sid)
            if had is None:
                by[sid] = frozenset((source,))
            elif source not in had:
                by[sid] = had | {source}


def note(store, envelopes) -> None:
    """Remember the claude_code sources of these lines' sessions, before any of them can reach
    DuckDB. More than DuckDB holds only sends a session down the line by line read."""
    _add_pairs(_duck_state(store), ((e.source, str(e.key_value)) for e in envelopes))


def _duck_sources(store) -> dict:
    """{session: the claude_code sources DuckDB has (or was given) lines of it under}. Read from
    DuckDB once per process, then kept by `note`. Call it outside a session store transaction."""
    state = _duck_state(store)
    if not state["loaded"]:
        names = sorted(claude_code_sources(store))
        marks = ", ".join("?" * len(names))
        with store._lock:
            got = store.con.execute(
                "SELECT DISTINCT source, key_value FROM events WHERE key_value IS NOT NULL "
                f"AND (source IN ({marks}) OR source_type = 'claude_code')", names).fetchall()
        _add_pairs(state, got)
        state["loaded"] = True
    return state["by"]


def _marked(store, born: list) -> list:
    """`born`, with a session whose id DuckDB has under another source marked not complete."""
    seen = _duck_state(store)["by"]
    out = []
    for b in born:
        sources = seen.get(b.session)
        if sources and b.lines_complete and (len(sources) > 1 or b.source not in sources):
            b = Born(*b)._replace(lines_complete=False)
        out.append(b)
    return out


def _chunks(values: list):
    for i in range(0, len(values), CHUNK):
        yield values[i:i + CHUNK]


def _utc(t):
    return t.replace(tzinfo=_MIN.tzinfo) if t is not None and t.tzinfo is None else t


def _row(session: str, source: str, seq: int, kind, at, labels, render=None, payload=None,
         weight: int = 1) -> dict:
    return {"session": session, "source": source, "seq": seq, "type": kind, "at": _utc(at),
            "labels": labels, "render": render, "payload": payload, "w": weight, "text": None}


def _text(r: dict) -> str:
    try:
        return session_read._text(r)
    except Exception as e:
        session_read._warn(r["session"], f"a line whose text cannot be rebuilt: "
                           f"{type(e).__name__}: {e}", fallback=False)
        return ""


class View:
    """What a statement reads in place of `events`: `table` goes after FROM and `params` are bound
    where it stands. `ref` is an expression that numbers a stored line (NULL for a DuckDB row), for
    `fill`; `weight` is how many lines a row stands for."""

    def __init__(self, store=None, born=(), rows=(), need_text: bool = False):
        pairs = [(b.source, b.session) for b in born if b.lines_complete]
        self.store = store
        self.rows = list(rows)
        self.held = bool(pairs or self.rows)
        if not self.held:
            self.table, self.params, self.ref, self.weight = "events", [], "NULL", "1"
            return
        self.ref, self.weight = "held_ref", "held_w"
        if need_text:
            for r in self.rows:
                r["text"] = _text(r)
        self.table = (
            "(SELECT source, source_type, key_value, event_type, text, payload, labels, "
            "event_time, ingest_time, NULL::BIGINT AS held_ref, 1::BIGINT AS held_w FROM events "
            "WHERE NOT EXISTS (SELECT 1 FROM (SELECT unnest(?::VARCHAR[]) AS s, "
            "unnest(?::VARCHAR[]) AS k) born WHERE born.s = events.source "
            "AND born.k = events.key_value)")
        self.params = [[p[0] for p in pairs], [p[1] for p in pairs]]
        if self.rows:                    # with none, DuckDB would still pay to plan the empty half
            self.table += (
                " UNION ALL SELECT unnest(?::VARCHAR[]), '', unnest(?::VARCHAR[]), "
                "unnest(?::VARCHAR[]), unnest(?::VARCHAR[]), unnest(?::VARCHAR[])::JSON, "
                "unnest(?::VARCHAR[])::JSON, unnest(?::TIMESTAMPTZ[]), unnest(?::TIMESTAMPTZ[]), "
                "unnest(?::BIGINT[]), unnest(?::BIGINT[])")
            at = [r["at"] for r in self.rows]
            self.params += [
                [r["source"] for r in self.rows], [r["session"] for r in self.rows],
                [r["type"] for r in self.rows], [r["text"] for r in self.rows],
                [r["payload"] for r in self.rows], [r["labels"] for r in self.rows],
                at, at, list(range(len(self.rows))), [r["w"] for r in self.rows]]
        self.table += ") AS events"

    def fill(self, refs) -> dict:
        """{ref: (text, payload)} for the stored lines a statement returned. The payloads a read
        did not load are fetched now, so a long window never holds every payload at once."""
        want = {n: self.rows[n] for n in refs if n is not None}
        missing: dict = {}
        for r in want.values():
            if r["payload"] is None:
                missing.setdefault(r["session"], {})[r["seq"]] = r
        t = session_lines.c
        for sid, by_seq in missing.items():
            with self.store.sessions_db.read() as c:
                for chunk in _chunks(sorted(by_seq)):
                    for got in c.execute(select(t.seq, t.render, t.payload).where(
                            t.session == sid, t.seq.in_(chunk))).all():
                        by_seq[got.seq].update(render=got.render, payload=got.payload)
            gone = {seq for seq, r in by_seq.items() if r["payload"] is None}
            if gone:                      # the hourly archive moved them away since the read
                with self.store.sessions_db.read() as c:
                    parts = _parts(c, [sid], None)
                for old in _archived(self.store, parts):
                    if old["seq"] in gone:
                        by_seq[old["seq"]].update(render=old["render"], payload=old["payload"])
        return {n: (r["text"] if r["text"] is not None else _text(r) if r["payload"] else "",
                    r["payload"]) for n, r in want.items()}


PLAIN = View()


def _born(store, conn, sources, since=None, until=None, key=None, keys=None,
          factory: bool = False) -> list:
    """The rows (`_BORN`) of the born sessions a read touches. `_duck_sources` comes first.
    With `factory`, none unless the cell's edition is factory: `session_read._factory` asked in the
    same statement, so it costs no round trip of its own."""
    c = sessions.c
    q = select(*(c[name] for name in _BORN)).where(c.lines_origin == TABLE)
    if factory:
        q = q.where(exists().where(settings.c.key == edition.EDITION_SETTING,
                                   func.lower(func.trim(settings.c.value)) == "factory"))
    if keys is not None:
        q = q.where(c.session.in_(keys))
    if sources is not None:
        q = q.where(c.source.in_(sorted({s for s in sources if s})))
    if since is not None:
        q = q.where(c.last_line_at >= since)
    if until is not None:
        q = q.where(c.started_at < until)
    if key is not None:
        q = q.where(c.session == str(key))
    return _marked(store, conn.execute(q).all())


def _parts(conn, sids: list, since) -> list:
    """The archive records of `sids`. A part is written a week after its session went quiet, so one
    written before `since` holds nothing from `since` on."""
    p = session_archive_parts
    out: list = []
    for chunk in _chunks(sids):
        q = select(p).where(p.c.session.in_(chunk)).order_by(p.c.session, p.c.part)
        if since is not None:
            q = q.where(p.c.written_at >= since)
        out += conn.execute(q).all()
    return out


def _archived(store, parts: list):
    """The lines of `parts` as dicts (`session`, `seq`, `at`, `type`, `labels`, `render`,
    `payload`). A part that cannot be read is left out, with one line saying so."""
    backend = None
    for p in parts:
        try:
            if backend is None:
                backend = session_archive.backend_of(store)
            if backend is None:
                raise session_archive.ArchiveError("no archive is configured for this cell")
            rows = [session_read._archived(r) for r in session_archive.read_part(backend, p)]
        except Exception as e:
            session_read._warn(p.session, f"part {p.part}: {type(e).__name__}: {e}", fallback=False)
            continue
        for r in rows:
            r["session"], r["at"] = p.session, _utc(r["at"])
            yield r


def _not_in_duckdb(store, born: list, rows: list, since) -> list:
    """`rows` without the lines DuckDB also holds, for the sessions not marked complete."""
    short = sorted(b.session for b in born if not b.lines_complete)
    if not short:
        return rows
    there: Counter = Counter()
    tail, params = ("", []) if since is None else (" AND event_time >= ?", [since])
    for chunk in _chunks(short):
        marks = ", ".join("?" * len(chunk))
        with store._lock:
            got = store.con.execute(
                "SELECT key_value, event_time, event_type, payload FROM events "
                f"WHERE key_value IN ({marks}){tail}", [*chunk, *params]).fetchall()
        there.update((k, at, kind, payload) for k, at, kind, payload in got)
    if not there:
        return rows
    out = []
    short_set = set(short)
    for r in rows:
        if r["session"] in short_set:
            line = (r["session"], r["at"], r["type"], r["payload"])
            if there[line] > 0:
                there[line] -= 1
                continue
        out.append(r)
    return out


def _pushdown(filters, where):
    """What the stored lines can be narrowed by before the statement runs, never more than the
    statement itself would: (key, event types lower case or None, label texts)."""
    key, types, needles = None, None, []

    def needle(value):
        if isinstance(value, str):
            needles.append(json.dumps(value)[1:-1])

    for name, value in (where or {}).items():
        if name == "key_value":
            key = str(value)
        else:
            needle(str(value))
    for f in filters or []:
        name, op, value = f.get("field"), f.get("op"), f.get("value")
        if name == "event_type" and op in ("eq", "in"):
            values = value if isinstance(value, list) else [value]
            types = {str(v).lower() for v in values}
        elif name == "key_value" and op == "eq":
            key = str(value)
        elif op == "eq" and name not in ("event_type", "source", "text", "key_value"):
            needle(str(value))
    return key, types, needles


def view(store, sources, since=None, until=None, key=None, filters=None, where=None,
         need_text: bool = False, need_payload: bool = False, counted: bool = False) -> View:
    """`events` for a statement that reads `sources` (None: every source) in a window, narrowed to
    one session by `key`. `filters` and `where` are the statement's own, used only to load less.
    `need_text` / `need_payload`: the statement looks at the column in SQL; otherwise it is NULL
    for a stored line and `View.fill` gives it for the rows returned. `counted`: the statement only
    counts and adds up labels, weighing each row by `View.weight`, so lines of one session with the
    same type and labels may come as one row (a trigger asks this on every ingest)."""
    if (sources is not None and not sources) or not session_read._factory(store):
        return PLAIN
    pushed, types, needles = _pushdown(filters, where)
    key = key if key is not None else pushed
    need_text = need_text or any(f.get("field") == "text" for f in filters or [])
    _duck_sources(store)
    full = need_text or need_payload
    with store.sessions_db.consistent_read() as c:
        born = _born(store, c, sources, since, until, key)
        if not born:
            return PLAIN
        mine = born
        if (types is not None or needles) and key is None and since is not None and not counted:
            # a read narrowed by type or label loads no session that has no such line
            mine = (session_digest.matching(store, c, [b for b in born if b.lines_complete],
                                            since, types, needles)
                    + [b for b in born if not b.lines_complete])
        rows, parts = load(store, c, mine, since, until, types, needles, full, counted)
    rows = finish(store, mine, rows, parts, since, until, types, needles)
    return View(store, born, rows, need_text)


def load(store, c, born: list, since=None, until=None, types=None, needles=(), full: bool = False,
         counted: bool = False) -> tuple[list, list]:
    """The part of `view` that needs `c`: the rows of the `born` sessions from the table and the
    records of their archive parts. `finish` does the rest, outside the transaction."""
    find = func.strpos if store.sessions_db.dialect == "postgresql" else func.instr
    t = session_lines.c
    source_of = {b.session: b.source for b in born}
    short = {b.session for b in born if not b.lines_complete}
    rows: list = []

    def narrow(q):
        if since is not None:
            q = q.where(t.at >= since)
        if until is not None:
            q = q.where(t.at < until)
        if types is not None:
            q = q.where(func.lower(t.type).in_(sorted(types)))
        for n in needles:
            q = q.where(find(t.labels, n) > 0)
        return q

    whole = [s for s in source_of if s not in short]
    if counted and not full:
        by = [t.session, t.type, t.labels]
        for chunk in _chunks(whole):
            q = select(*by, func.count(), func.max(t.at)).where(t.session.in_(chunk))
            for sid, kind, labels, n, at in c.execute(narrow(q).group_by(*by)).all():
                rows.append(_row(sid, source_of[sid], 0, kind, at, labels, weight=n))
        whole = []
    # a session read from both stores is matched line by line, which takes the payload
    for sids, with_payload in ((whole, full), (sorted(short), True)):
        cols = [t.session, t.seq, t.type, t.at, t.labels]
        if with_payload:
            cols += [t.render, t.payload]
        for chunk in _chunks(sids):
            for got in c.execute(narrow(select(*cols).where(t.session.in_(chunk)))).all():
                rows.append(_row(got[0], source_of[got[0]], *got[1:]))
    return rows, _parts(c, sorted(source_of), since)


def finish(store, born: list, rows: list, parts: list, since=None, until=None, types=None,
           needles=()) -> list:
    """The rows of `load` with the archived lines, without those DuckDB holds, oldest first."""
    source_of = {b.session: b.source for b in born}
    for r in _archived(store, parts):
        if (r["at"] is None or (since is not None and r["at"] < since)
                or (until is not None and r["at"] >= until)
                or (types is not None and str(r["type"]).lower() not in types)
                or any(n not in (r["labels"] or "") for n in needles)):
            continue
        rows.append(_row(r["session"], source_of[r["session"]], r["seq"], r["type"], r["at"],
                         r["labels"], r["render"], r["payload"]))
    rows = _not_in_duckdb(store, born, rows, since)
    rows.sort(key=lambda r: (r["at"] or _MIN, r["session"], r["seq"]))
    return rows


def newest(store, sources, limit: int, hidden_sessions=frozenset(), hidden_line=None,
           need_text: bool = False) -> View:
    """`events` for a statement that takes the newest `limit` rows of `sources` (None: every
    source) with no window. Only the stored lines that can be among them are loaded: the sessions
    are walked newest first by their last line, until none left can hold a newer one. The caller's
    hidden sessions and lines (`hidden_line(source, labels, payload)`) are not counted."""
    if (sources is not None and not sources) or not session_read._factory(store):
        return PLAIN
    db = store.sessions_db
    _duck_sources(store)
    with db.read() as c:
        born = _born(store, c, sources)
    if not born:
        return PLAIN
    t = session_lines.c
    cols = [t.seq, t.type, t.at, t.labels, t.render, t.payload]
    kept: list = []                                         # newest first
    for b in sorted(born, key=lambda b: (_utc(b.last_line_at) or _MIN, b.session), reverse=True):
        if limit <= 0 or (len(kept) >= limit
                          and (_utc(b.last_line_at) or _MIN) <= (kept[-1]["at"] or _MIN)):
            break
        if b.session in hidden_sessions:
            continue

        def shown(r):
            return hidden_line is None or not hidden_line(b.source, r["labels"], r["payload"])

        mine: list = []
        start = 0
        with db.consistent_read() as c:
            while len(mine) < limit:
                got = c.execute(select(*cols).where(t.session == b.session)
                                .order_by(t.at.desc(), t.seq.desc())
                                .limit(limit).offset(start)).all()
                mine += [r for r in (_row(b.session, b.source, *g) for g in got) if shown(r)]
                if len(got) < limit:
                    break
                start += limit
            parts = _parts(c, [b.session], None) if len(mine) < limit else []
        for r in _archived(store, parts):
            old = _row(b.session, b.source, r["seq"], r["type"], r["at"], r["labels"],
                       r["render"], r["payload"])
            if shown(old):
                mine.append(old)
        mine = _not_in_duckdb(store, [b], mine, None)
        kept = sorted(kept + mine, key=lambda r: (r["at"] or _MIN, r["seq"]), reverse=True)[:limit]
    return View(store, born, kept[::-1], need_text)


def grouped(store, sources, need_payload: bool = False) -> View:
    """`events` for a statement that counts the labels of every line of `sources` (None: every
    source). The stored lines of a session that carry the same labels (and payload, when the
    statement looks into it) come as one row with their number in `View.weight`."""
    if (sources is not None and not sources) or not session_read._factory(store):
        return PLAIN
    _duck_sources(store)
    with store.sessions_db.consistent_read() as c:
        born = _born(store, c, sources)
        if not born:
            return PLAIN
        rows, parts = load_grouped(c, born, need_payload)
    return View(store, born, finish_grouped(store, born, rows, parts))


def load_grouped(c, born: list, need_payload: bool = False) -> tuple[list, list]:
    """The part of `grouped` that needs `c`: rows and archive part records of the `born` sessions."""
    t = session_lines.c
    source_of = {b.session: b.source for b in born}
    short = {b.session for b in born if not b.lines_complete}
    rows: list = []
    by = [t.session, t.labels] + ([t.payload] if need_payload else [])
    for chunk in _chunks([s for s in source_of if s not in short]):
        for got in c.execute(select(*by, func.count(), func.max(t.at))
                             .where(t.session.in_(chunk)).group_by(*by)).all():
            rows.append(_row(got[0], source_of[got[0]], 0, None, got[-1], got[1],
                             payload=got[2] if need_payload else None, weight=got[-2]))
    for chunk in _chunks(sorted(short)):
        for got in c.execute(select(t.session, t.seq, t.type, t.at, t.labels, t.render,
                                    t.payload).where(t.session.in_(chunk))).all():
            rows.append(_row(got[0], source_of[got[0]], *got[1:]))
    return rows, _parts(c, sorted(source_of), None)


def finish_grouped(store, born: list, rows: list, parts: list) -> list:
    source_of = {b.session: b.source for b in born}
    for r in _archived(store, parts):
        rows.append(_row(r["session"], source_of[r["session"]], r["seq"], r["type"], r["at"],
                         r["labels"], r["render"], r["payload"]))
    return _not_in_duckdb(store, born, rows, None)


def held_count(store, source: str) -> int:
    """The lines of `source` the events table does not count: what the born sessions hold, less
    the lines DuckDB has of the same sessions."""
    if not session_read._factory(store):
        return 0
    t = session_lines.c
    _duck_sources(store)
    with store.sessions_db.read() as conn:
        born = _born(store, conn, [source])
    counts = {b.session: b.line_count or 0 for b in born if b.lines_complete}
    for chunk in _chunks(list(counts)):
        marks = ", ".join("?" * len(chunk))
        with store._lock:
            got = store.con.execute(
                "SELECT key_value, COUNT(*) FROM events "
                f"WHERE source = ? AND key_value IN ({marks}) GROUP BY key_value",
                [source, *chunk]).fetchall()
        for sid, n in got:
            counts[sid] = max(0, counts[sid] - n)
    total = sum(counts.values())
    # an incomplete session's line_count does not say what it holds: its lines are matched one by one
    short = [b for b in born if not b.lines_complete]
    if short:
        sids = sorted(b.session for b in short)
        rows: list = []
        with store.sessions_db.consistent_read() as conn:
            for chunk in _chunks(sids):
                for got in conn.execute(select(t.session, t.seq, t.type, t.at, t.labels, t.render,
                                               t.payload).where(t.session.in_(chunk))).all():
                    rows.append(_row(got[0], source, *got[1:]))
            parts = _parts(conn, sids, None)
        rows += [_row(r["session"], source, r["seq"], r["type"], r["at"], r["labels"],
                      r["render"], r["payload"]) for r in _archived(store, parts)]
        total += len(_not_in_duckdb(store, short, rows, None))
    return total
