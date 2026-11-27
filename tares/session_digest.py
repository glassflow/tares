"""The times of the stored transcript lines, kept in memory (TR-1695).

`session_events` answers a read of DuckDB `events` by loading the lines the session store holds,
which costs by the line. A count over a window, a source's ingest rhythm and a list of label
values need less: for each session, its lines grouped by (type, labels), each group as a sorted
array of line times. This module keeps that per session and answers those reads from it. It is a
cache: losing it changes no answer, and nothing here writes.

When an entry is current. An entry carries its session's row as it was read in the same snapshot as
its lines (`version`), and is used only while the row still reads the same. Every writer of a
session's lines changes that row: an append and `take_back` change `line_count` (an append sets
`updated_at` too), the drain, the move and the reconcile set `updated_at`, and a purge removes the
row. The hourly archive changes no row and no line: it moves lines into a part. A changed session is
read again whole, never patched, so a removal followed by other lines cannot go unseen. Archive
parts never change, so a part already read is taken over by its key and MD5. Two processes on one
Postgres are covered by the same check, since it looks at the database and not at this process.

What is not answered from here: sessions not marked complete, which are matched line by line with
DuckDB (`session_events`), and any condition on a line's text or payload.

Memory. Sessions with a line in the last `HORIZON_DAYS` (or the longest claude_code trigger window,
when that is longer) keep their times: 8 bytes a line, plus the labels text once per group. An older
session keeps only a count and the newest time per labels text, which is what a list of label values
needs (it reads without a window); a window that reaches such a session reads its lines again and
does not keep them.

`MAX_BYTES` (estimated sizes) covers everything kept between reads: three quarters for the entries,
one quarter for what the query shapes remember (memos and per-session shares). An entry that does
not fit takes the room of the least recently used ones, so a session that is read stays. A read
takes no room from what it, or a later read, has used, and at most half of the entries' room, so one
wide read does not empty the cache. What does not fit is built for the read that needs it. A
replaced or dropped entry takes its shares with it. Not counted: what a read holds until it returns.
"""
from __future__ import annotations

import itertools
import json
import sys
import threading
import time
from array import array
from bisect import bisect_left, bisect_right
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

import duckdb
from sqlalchemy import BigInteger, Integer, case, cast, func, select

from . import session_archive, session_read
from .session_store.tables import session_archive_parts, session_lines, sessions

HORIZON_DAYS = 31                  # the longest window anything reads is 30 days, plus a day
MAX_BYTES = 256 * 1024 * 1024      # everything kept between reads
CHUNK = 500                        # sessions one statement names
WARM_BATCH = 50                    # sessions the warm-up reads in one transaction
SWEEP_SECONDS = 600                # how often gone and aged sessions are dropped
QUERIES = 16                       # query shapes whose evaluations are kept
MEMO_MAX = 200_000                 # evaluations kept for one query shape
EVAL_CHUNK = 20_000
QUARTER = 900 * 1_000_000          # microseconds; every time zone offset is a multiple of it
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_US = timedelta(microseconds=1)
_GROUP_BYTES = 250                 # a group's key, tuple and array objects, about
_ENTRY_BYTES = 600
_MEMO_BYTES = 200                  # one remembered evaluation, its texts apart
_SHARE_BYTES = 200                 # a session's share of a query, its values apart
_VALUE_BYTES = 250                 # one value of a share
_MISS = object()                   # a (type, labels) pair the query's conditions leave out


def _micros(t: datetime) -> int:
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (t - _EPOCH) // _US


def _moment(us: int | None):
    return None if us is None else _EPOCH + timedelta(microseconds=us)


class Entry:
    """One session's lines, as grouped times (`full`) or as counts per labels text (a rollup).
    Never changed once built, except `used`: the last read that took it."""

    __slots__ = ("version", "segments", "parts", "lo", "hi", "quarters", "by_labels", "lines",
                 "bytes", "partial", "used")

    @property
    def full(self) -> bool:
        return self.segments is not None


def _segment(groups: dict) -> dict:
    """{(type, labels): (sorted times, lines with no time)} from {(type, labels): [times, nulls]}."""
    return {k: (array("q", sorted(v[0])), v[1]) for k, v in groups.items()}


def _make(version: tuple, table: dict, parts: dict, partial: bool) -> Entry:
    e = Entry()
    e.version = version
    e.parts = parts                              # {(key, md5): segment}
    e.segments = (table, *parts.values())
    e.partial = partial                          # a part could not be read: never kept
    lo = hi = None
    quarters: set = set()
    by_labels: dict = {}
    lines = size = 0
    for seg in e.segments:
        for (kind, labels), (times, nulls) in seg.items():
            n = len(times) + nulls
            lines += n
            size += _GROUP_BYTES + 8 * len(times) + len(labels or "")
            last = times[-1] if times else None
            if times:
                lo = times[0] if lo is None else min(lo, times[0])
                hi = last if hi is None else max(hi, last)
                i = 0
                while i < len(times):                # one step for each quarter hour
                    quarter = times[i] // QUARTER
                    quarters.add(quarter)
                    i = bisect_left(times, (quarter + 1) * QUARTER, i)
            had = by_labels.get(labels)
            if had is None:
                by_labels[labels] = (n, last)
            else:
                by_labels[labels] = (had[0] + n, had[1] if last is None or
                                     (had[1] is not None and had[1] >= last) else last)
    e.lo, e.hi = lo, hi
    e.quarters = array("q", sorted(quarters))
    e.by_labels = by_labels
    e.lines = lines
    e.bytes = _ENTRY_BYTES + size + 8 * len(e.quarters) + 120 * len(by_labels)
    return e


def _rollup(e: Entry) -> Entry:
    r = Entry()
    r.version, r.lo, r.hi, r.by_labels, r.lines = e.version, e.lo, e.hi, e.by_labels, e.lines
    r.segments = r.quarters = r.parts = None
    r.partial = e.partial
    r.bytes = _ENTRY_BYTES + sum(150 + len(labels or "") for labels in e.by_labels)
    return r


class _Query:
    """What one query's conditions make of each (type, labels) pair, and each session's share."""

    __slots__ = ("memo", "sessions", "memo_bytes", "bytes", "live")

    def __init__(self):
        self.memo: dict = {}
        self.sessions: dict = {}                 # {session: (entry, share, bytes)}
        self.memo_bytes = 0
        self.bytes = 0                           # the memo and the shares
        self.live = True                         # still one of the digest's


class _Read:
    """One read's turn at the cache: its place in line, and what it has evicted."""

    __slots__ = ("tick", "evicted", "warm", "refused")

    def __init__(self, tick: int, warm: bool):
        self.tick = tick
        self.warm = warm                         # the warm-up: evicts nothing
        self.evicted = 0
        self.refused = False                     # an entry of it did not fit


class Digest:
    def __init__(self):
        self.entries: OrderedDict = OrderedDict()    # least recently used first
        self.bytes = 0                           # everything kept
        self.query_bytes = 0                     # of which the queries'
        self.lock = threading.Lock()             # held to publish or drop, never while reading
        self.queries: OrderedDict = OrderedDict()
        self.ticks = itertools.count(1)
        self.horizon = timedelta(days=HORIZON_DAYS)
        self.swept = None

    def turn(self, warm: bool = False) -> _Read:
        return _Read(next(self.ticks), warm)

    def use(self, read: _Read, sessions_taken) -> None:
        with self.lock:
            for sid in sessions_taken:
                e = self.entries.get(sid)
                if e is not None:
                    e.used = read.tick
                    self.entries.move_to_end(sid)

    def publish(self, session: str, entry: Entry, read: _Read) -> None:
        with self.lock:
            self.drop(session)
            if entry.partial:
                return
            if not self._room(entry.bytes, read):
                read.refused = True
                return
            entry.used = read.tick
            self.entries[session] = entry
            self.bytes += entry.bytes
            if read.warm:                        # warmed newest first: the newest stays longest
                self.entries.move_to_end(session, last=False)

    def _room(self, need: int, read: _Read) -> bool:
        """Evict least recently used entries until `need` bytes more fit, as far as `read` may."""
        limit = MAX_BYTES - MAX_BYTES // 4
        if need > limit:
            return False
        while self.bytes - self.query_bytes + need > limit:
            sid = next(iter(self.entries), None)
            if (sid is None or read.warm or self.entries[sid].used >= read.tick
                    or read.evicted >= limit // 2):
                return False
            read.evicted += self.entries[sid].bytes
            self.drop(sid)
        return True

    # the lock is held for the rest
    def drop(self, session: str) -> None:
        old = self.entries.pop(session, None)
        if old is not None:
            self.bytes -= old.bytes
            self.unshare(session)

    def unshare(self, session: str) -> None:
        """No share keeps an entry that is no longer the session's."""
        for q in self.queries.values():
            had = q.sessions.pop(session, None)
            if had is not None:
                self._count(q, -had[2])

    def _count(self, query: _Query, n: int) -> None:
        query.bytes += n
        if query.live:
            self.bytes += n
            self.query_bytes += n

    def _forget_oldest_query(self) -> None:
        _signature, q = self.queries.popitem(last=False)
        q.live = False
        self.bytes -= q.bytes
        self.query_bytes -= q.bytes

    def _bound_queries(self) -> None:
        while self.queries and self.query_bytes > MAX_BYTES // 4:
            self._forget_oldest_query()

    # these take it themselves
    def query(self, signature) -> _Query:
        with self.lock:
            q = self.queries.get(signature)
            if q is None:
                q = self.queries[signature] = _Query()
                while len(self.queries) > QUERIES:
                    self._forget_oldest_query()
            else:
                self.queries.move_to_end(signature)
            return q

    def learn(self, query: _Query, found: dict) -> None:
        """Remember what the conditions make of these pairs."""
        with self.lock:
            if len(query.memo) > MEMO_MAX:
                self._count(query, -query.memo_bytes)
                query.memo, query.memo_bytes = {}, 0
            size = 0
            for pair, got in found.items():
                if pair not in query.memo:
                    query.memo[pair] = got
                    size += _MEMO_BYTES + len(pair[1] or "") + (
                        0 if got is _MISS else sum(len(v) for v in got if isinstance(v, str)))
            query.memo_bytes += size
            self._count(query, size)
            self._bound_queries()

    def keep(self, query: _Query, shares: list) -> None:
        """Keep these [(session, entry, share, bytes)], of the entries still the session's."""
        with self.lock:
            for sid, entry, share, size in shares:
                if self.entries.get(sid) is not entry:
                    continue
                had = query.sessions.get(sid)
                query.sessions[sid] = (entry, share, size)
                self._count(query, size - (had[2] if had is not None else 0))
            self._bound_queries()


_creating = threading.Lock()


def of(store) -> Digest:
    got = store.__dict__.get("_session_digest")
    if got is None:
        with _creating:
            got = store.__dict__.setdefault("_session_digest", Digest())
    return got


# ── keeping it bounded ───────────────────────────────────────────────────────
def _horizon(store) -> timedelta:
    """`HORIZON_DAYS`, or the longest window a trigger over a claude_code source asks for."""
    longest = timedelta(days=HORIZON_DAYS)
    try:
        from .config import parse_duration
        from .session_ingest import claude_code_sources
        ours = claude_code_sources(store)
        for trig in store.list_catalog_triggers():
            sources, cond = trig.get("sources") or [], trig.get("condition") or {}
            sources = json.loads(sources) if isinstance(sources, str) else sources
            cond = json.loads(cond) if isinstance(cond, str) else cond
            window = cond.get("window") or cond.get("every")
            if window and ours & set(sources):
                longest = max(longest, timedelta(seconds=parse_duration(window)) + timedelta(days=1))
    except Exception:
        pass                                     # a longer window is then read again each time
    return longest


def _old(digest: Digest, entry: Entry) -> bool:
    return entry.hi is None or entry.hi < _micros(datetime.now(timezone.utc) - digest.horizon)


def sweep(store, force: bool = False) -> None:
    """Drop the sessions that are gone, and keep only the rollup of those past the horizon."""
    digest = of(store)
    now = time.monotonic()
    if not force and digest.swept is not None and now - digest.swept < SWEEP_SECONDS:
        return
    digest.swept = now
    digest.horizon = _horizon(store)
    from .session_ingest import TABLE
    with store.sessions_db.read() as c:
        with digest.lock:
            before = list(digest.entries)        # taken first: a session born meanwhile stays
        live = set(c.execute(select(sessions.c.session).where(
            sessions.c.lines_origin == TABLE)).scalars())
    gone = [s for s in before if s not in live]
    with digest.lock:
        for sid in gone:
            digest.drop(sid)
        for sid, entry in list(digest.entries.items()):
            if entry.full and _old(digest, entry):
                small = _rollup(entry)
                small.used = entry.used
                digest.entries[sid] = small      # in its place in line
                digest.bytes += small.bytes - entry.bytes
                digest.unshare(sid)


# ── reading the lines ────────────────────────────────────────────────────────
class _Plan:
    __slots__ = ("born", "have", "stale", "table", "parts", "need_full", "read", "old")


def load(store, conn, born: list, need_full: bool = True, read: _Read | None = None) -> _Plan:
    """The part of a read that needs `conn`, the transaction `born` was read in: which entries are
    still current, and the rows and part records of the rest. `finish` does the others."""
    digest = of(store)
    plan = _Plan()
    plan.born, plan.need_full = born, need_full
    plan.read = read or digest.turn()
    plan.have, plan.stale, plan.table, plan.parts = {}, [], {}, {}
    plan.old = {}                    # held here: a read beside this one may evict them
    for b in born:
        e = digest.entries.get(b.session)
        if e is not None and e.version == tuple(b) and (e.full or not need_full):
            plan.have[b.session] = e
        else:
            plan.stale.append(b)
            plan.old[b.session] = e
    if not plan.read.warm:
        # all of them, before this read may evict: it never puts out what it is reading
        digest.use(plan.read, [b.session for b in born])
    if not plan.stale:
        return plan
    t = session_lines.c
    if conn.dialect.name == "postgresql":
        us = cast(func.extract("epoch", t.at) * 1000000, BigInteger)
        times = func.array_agg(us).filter(t.at.is_not(None))
    else:   # SQLite keeps UTC as 'YYYY-MM-DD HH:MM:SS.ffffff'
        us = (cast(func.strftime("%s", func.substr(t.at, 1, 19)), Integer) * 1000000
              + cast(func.substr(t.at, 21, 6), Integer))
        times = func.group_concat(us)
    nulls = func.sum(case((t.at.is_(None), 1), else_=0))
    sids = [b.session for b in plan.stale]
    p = session_archive_parts
    for i in range(0, len(sids), CHUNK):
        chunk = sids[i:i + CHUNK]
        for sid, kind, labels, got, none in conn.execute(
                select(t.session, t.type, t.labels, times, nulls).where(t.session.in_(chunk))
                .group_by(t.session, t.type, t.labels)).all():
            plan.table.setdefault(sid, []).append((kind, labels, got, none))
        for part in conn.execute(select(p).where(p.c.session.in_(chunk))
                                 .order_by(p.c.session, p.c.part)).all():
            plan.parts.setdefault(part.session, []).append(part)
    return plan


def _read_parts(digest: Digest, plan: _Plan, session: str) -> dict:
    """{(key, md5): segment} already read for this session: by the entry `load` held (a read beside
    this one may evict it) and by the one published since (the warm-up)."""
    parts: dict = {}
    for e in (plan.old.get(session), digest.entries.get(session)):
        if e is not None and e.full:
            parts.update(e.parts)
    return parts


def finish(store, plan: _Plan) -> dict:
    """{session: entry} for every session of the plan, reading the archive parts not read before."""
    digest = of(store)
    out = dict(plan.have)
    backend = None
    shared: dict = {}                            # one copy of each labels text of this read
    for b in plan.stale:
        sid = b.session
        table: dict = {}
        for kind, labels, got, none in plan.table.pop(sid, ()):
            if isinstance(got, str):
                got = map(int, got.split(","))
            labels = shared.setdefault(labels, labels)
            table[(kind if kind is None else sys.intern(kind), labels)] = [got or [], int(none or 0)]
        table = _segment(table)
        known = _read_parts(digest, plan, sid)
        parts: dict = {}
        partial = False
        for p in plan.parts.get(sid, ()):
            seg = known.get((p.key, p.md5))
            if seg is None:
                try:
                    if backend is None:
                        backend = session_archive.backend_of(store)
                    if backend is None:
                        raise session_archive.ArchiveError("no archive is configured for this cell")
                    groups: dict = {}
                    for raw in session_archive.iter_part(backend, p):
                        r = session_read._archived(raw)
                        kind, labels = r["type"], r["labels"]
                        key = (kind if kind is None else sys.intern(kind),
                               shared.setdefault(labels, labels))
                        g = groups.get(key)
                        if g is None:
                            g = groups[key] = [[], 0]
                        if r["at"] is None:
                            g[1] += 1
                        else:
                            g[0].append(_micros(r["at"]))
                    seg = _segment(groups)
                except Exception as e:
                    session_read._warn(sid, f"part {p.part}: {type(e).__name__}: {e}",
                                       fallback=False)
                    partial = True
                    continue
            parts[(p.key, p.md5)] = seg
        entry = _make(tuple(b), table, parts, partial)
        out[sid] = entry
        digest.publish(sid, _rollup(entry) if _old(digest, entry) else entry, plan.read)
    plan.stale = []
    return out


def matching(store, conn, born: list, since, types, needles: list) -> list:
    """The sessions of `born` (all complete) that may hold a line of these `types` (lower case)
    whose labels text contains every needle: what `session_events.view` need load. All of them when
    `since` reaches past what is kept."""
    digest = of(store)
    if since.tzinfo is None or since < datetime.now(timezone.utc) - digest.horizon:
        return born
    plan = load(store, conn, born)
    # a session with an archive part not read before is not read here, inside the transaction: it
    # counts as one that may match
    unread = set()
    for b in plan.stale:
        old = plan.old.get(b.session)
        had = old.parts if old is not None and old.full else {}
        if any((p.key, p.md5) not in had for p in plan.parts.get(b.session, ())):
            unread.add(b.session)
    plan.stale = [b for b in plan.stale if b.session not in unread]
    entries = finish(store, plan)
    return [b for b in born
            if b.session in unread
            or entries[b.session].partial          # a part not read: the plain read decides
            or any((types is None or str(kind).lower() in types)
                   and all(n in (labels or "") for n in needles)
                   for seg in entries[b.session].segments for kind, labels in seg)]


def _split(store, sources, since=None, until=None, key=None, need_full: bool = True,
           grouped: bool = False):
    """A read in two parts, both from one snapshot: ({session: entry} of the complete sessions,
    born rows, the view of everything else). None when there is nothing of the store's to read."""
    from . import session_events
    if sources is not None and not sources:
        return None
    # before DuckDB is read for `_duck_sources`, once, ask which edition this is. After that the
    # statement for the born sessions asks it, and finds none in another edition.
    if not session_events._duck_state(store)["loaded"]:
        if not session_read._factory(store):
            return None
        session_events._duck_sources(store)
    sweep(store)
    with store.sessions_db.consistent_read() as c:
        born = session_events._born(store, c, sources, since, until, key, factory=True)
        if not born:
            return None
        plan = load(store, c, [b for b in born if b.lines_complete], need_full)
        short = [b for b in born if not b.lines_complete]
        if grouped:
            rows, parts = session_events.load_grouped(c, short)
        else:
            rows, parts = session_events.load(store, c, short, since, until)
    entries = finish(store, plan)
    if grouped:
        rows = session_events.finish_grouped(store, short, rows, parts)
    else:
        rows = session_events.finish(store, short, rows, parts, since, until)
    return entries, born, session_events.View(store, born, rows)


def _evaluate(store, query: _Query, pairs: set, select_sql: str, where_sql: str, params: list) -> dict:
    """{pair: the columns `select_sql` gives for it, or _MISS when `where_sql` leaves it out}, by
    DuckDB, so the conditions mean what they mean for a row of `events`. A pair is
    (type, labels, session or None, source or None)."""
    memo = query.memo
    out = {p: memo[p] for p in pairs if p in memo}
    todo = [p for p in pairs if p not in out]
    for i in range(0, len(todo), EVAL_CHUNK):
        chunk = todo[i:i + EVAL_CHUNK]
        sql = ("SELECT i, " + select_sql + " FROM (SELECT unnest(?::BIGINT[]) AS i, "
               "unnest(?::VARCHAR[]) AS source, '' AS source_type, unnest(?::VARCHAR[]) AS key_value, "
               "unnest(?::VARCHAR[]) AS event_type, NULL::VARCHAR AS text, NULL::JSON AS payload, "
               "unnest(?::VARCHAR[])::JSON AS labels) AS events WHERE TRUE" + where_sql)
        args = [list(range(len(chunk))),
                [p[3] if p[3] is not None else "" for p in chunk],
                [p[2] if p[2] is not None else "k" for p in chunk],
                [p[0] for p in chunk], [p[1] for p in chunk]]
        with store._lock:
            got = store.con.execute(sql, [*args, *params]).fetchall()
        found = {r[0]: r[1:] for r in got}
        new = {pair: found.get(n, _MISS) for n, pair in enumerate(chunk)}
        out.update(new)
        of(store).learn(query, new)
    return out


# ── the reads ────────────────────────────────────────────────────────────────
class NotANumber(Exception):
    """A value in the window is not a number: the statement over the lines fails on it, with the
    DuckDB error kept as the cause."""


def _not_a_number(store, text: str):
    try:
        with store._lock:
            store.con.execute("SELECT CAST(? AS DOUBLE)", [text])
    except duckdb.ConversionException as e:
        raise NotANumber(str(e)) from e


def aggregate(store, sources, since, until, filters, where, names: list, gexprs: list,
              raw: str | None, scope, sql: tuple) -> dict | None:
    """What `Store.aggregate` needs, as {group values: [lines, lines with a value, sum, max, min]}.
    None when this is not a read the digest answers. `gexprs` is the statement's own SQL for the
    group labels `names`, `raw` that for the text of the value it casts to a number (None: it only
    counts lines), and `sql` its conditions:
    (filter SQL, its params, where SQL, its params, scope SQL, its params). Raises `NotANumber`
    where the statement itself would fail on a value."""
    value = "1" if raw is None else f"CAST({raw} AS DOUBLE)"
    if any(f.get("field") == "text" for f in filters or []):
        return None
    if since is None or since.tzinfo is None or (until is not None and until.tzinfo is None):
        return None
    from . import session_events
    key = session_events._pushdown(filters, where)[0]
    got = _split(store, sources, since, until, key)
    if got is None:
        return None
    entries, born, rest = got
    if scope and set(scope.get("sources") or []) & {b.source for b in born}:
        return None                              # a project's scope looks at the payload
    fsql, fparams, wsql, wparams, ssql, sparams = sql
    acc: dict = {}

    def add(group, lines, n_value, total, high, low):
        a = acc.get(group)
        if a is None:
            acc[group] = [lines, n_value, total, high, low]
            return
        a[0] += lines
        if n_value:
            a[1] += n_value
            a[2] = total if a[2] is None else a[2] + total
            # as DuckDB orders them: NaN is above every number
            if a[3] is None or high > a[3] or high != high:
                a[3] = high
            if a[4] is None or low < a[4] or a[4] != a[4]:
                a[4] = low

    # everything the digest does not hold: DuckDB's rows and the incomplete sessions
    marks = ", ".join(["?"] * len(sources))
    w = rest.weight
    sel = ", ".join(f"{e} AS g{i}" for i, e in enumerate(gexprs))
    with store._lock:
        try:
            rows = store.con.execute(
                f"SELECT {sel}, SUM({w})::BIGINT, "
                f"SUM({w}) FILTER (WHERE {value} IS NOT NULL)::BIGINT, "
                f"SUM({value} * {w}), MAX({value}), MIN({value}) FROM {rest.table} "
                f"WHERE source IN ({marks}) AND event_time >= ?"
                f"{' AND event_time < ?' if until is not None else ''}{fsql}{wsql}{ssql} "
                f"GROUP BY {', '.join(f'g{i}' for i in range(len(gexprs)))} "
                f"HAVING {' AND '.join(f'g{i} IS NOT NULL' for i in range(len(gexprs)))}",
                [*rest.params, *sources, since, *([until] if until is not None else []), *fparams,
                 *wparams, *sparams]).fetchall()
        except duckdb.ConversionException as e:
            raise NotANumber(str(e)) from e      # its rows are among the statement's own
    for r in rows:
        lines, n_value, total, high, low = r[len(gexprs):]
        add(tuple(r[:len(gexprs)]), lines, n_value or 0, total, high, low)

    refs = {f.get("field") for f in filters or []} | set(where or {})
    by_session, by_source = "key_value" in refs, "source" in refs
    query = of(store).query(("agg", raw, tuple(gexprs), fsql, tuple(fparams), wsql,
                             tuple(wparams)))
    shares: dict = {}
    stale = []
    for b in born:
        e = entries.get(b.session)
        if e is None:
            continue
        had = query.sessions.get(b.session)
        if had is not None and had[0] is e:
            shares[b.session] = had[1]
        else:
            stale.append((b, e))

    def pair(b, kind, labels):
        return (kind, labels, b.session if by_session else None, b.source if by_source else None)

    if stale:
        pairs = {pair(b, *k) for b, e in stale for seg in e.segments for k in seg}
        # TRY_CAST: a line outside the window may hold a text that is no number, and the statement
        # never casts that one. The text is kept, and fails the read only if the window has it.
        number = "1, NULL" if raw is None else (
            f"TRY_CAST({raw} AS DOUBLE), CASE WHEN TRY_CAST({raw} AS DOUBLE) IS NULL THEN {raw} END")
        seen = _evaluate(store, query, pairs, f"{number}, " + ", ".join(gexprs), fsql + wsql,
                         [*fparams, *wparams])
        kept = []
        for b, e in stale:
            share: dict = {}           # {(group, value, bad text): [lines with a time, arrays]}
            for seg in e.segments:
                for k, (times, _nulls) in seg.items():
                    res = seen[pair(b, *k)]
                    if res is _MISS or not times:
                        continue
                    group = tuple(b.session if n == "key_value" else g
                                  for n, g in zip(names, res[2:]))
                    if any(g is None for g in group):
                        continue
                    key = (group, res[0], res[1])
                    s = share.get(key)
                    if s is None:
                        s = share[key] = [0, []]
                    s[0] += len(times)
                    s[1].append(times)
            shares[b.session] = share
            kept.append((b.session, e, share, _SHARE_BYTES + sum(
                _VALUE_BYTES + len(k[2] or "") + 8 * len(v[1]) for k, v in share.items())))
        of(store).keep(query, kept)

    lo = _micros(since)
    hi = None if until is None else _micros(until)
    for sid, share in shares.items():
        e = entries[sid]
        if not share or e.hi < lo or (hi is not None and e.lo >= hi):
            continue
        whole = e.lo >= lo and (hi is None or e.hi < hi)
        for (group, v, bad), (n, arrays) in share.items():
            if not whole:
                n = 0
                for times in arrays:
                    n += (len(times) if hi is None else bisect_left(times, hi)) - bisect_left(times, lo)
                if n <= 0:
                    continue
            if bad is not None:
                _not_a_number(store, bad)
            if v is None:
                add(group, n, 0, None, None, None)
            else:
                add(group, n, n, v * n, v, v)
    return acc


def newest_times(store, source: str, since, limit: int) -> list | None:
    """The times (seconds since 1970) of the `limit` newest lines of `source` since `since`, newest
    first. None when this is not a read the digest answers."""
    if since is None or since.tzinfo is None or limit <= 0:
        return None
    got = _split(store, [source], since)
    if got is None:
        return None
    entries, born, rest = got
    lo = _micros(since)
    top: list = []
    floor = None                                 # the oldest of `limit` times, once there are
    for e in sorted((e for e in entries.values() if e.hi is not None and e.hi >= lo),
                    key=lambda e: e.hi, reverse=True):
        if floor is not None and e.hi <= floor:
            break
        for seg in e.segments:
            for times, _nulls in seg.values():
                i = max(bisect_left(times, lo), len(times) - limit)
                if floor is not None:
                    i = max(i, bisect_right(times, floor))
                top.extend(times[i:])
        if len(top) >= limit:
            top.sort()
            del top[:-limit]
            floor = top[0]
    with store._lock:
        rows = store.con.execute(
            f"SELECT epoch(ingest_time) FROM {rest.table} WHERE source = ? AND ingest_time >= ? "
            "ORDER BY ingest_time DESC LIMIT ?",
            [*rest.params, source, since, int(limit)]).fetchall()
    out = [t / 1e6 for t in top] + [float(r[0]) for r in rows if r[0] is not None]
    out.sort(reverse=True)
    return out[:limit]


def hours(store, source: str, since) -> list | None:
    """The distinct hours (as DuckDB truncates them) `source` has lines in since `since`. None when
    this is not a read the digest answers."""
    if since is None or since.tzinfo is None:
        return None
    got = _split(store, [source], since)
    if got is None:
        return None
    entries, born, rest = got
    lo = _micros(since)
    first = lo // QUARTER
    quarters: set = set()
    for e in entries.values():
        if e.hi is None or e.hi < lo:
            continue
        if e.lo >= lo:
            quarters.update(e.quarters)
            continue
        quarters.update(e.quarters[bisect_right(e.quarters, first):])
        end = (first + 1) * QUARTER              # the quarter `since` cuts: is a line after it?
        for seg in e.segments:
            if first in quarters:
                break
            for times, _nulls in seg.values():
                i = bisect_left(times, lo)
                if i < len(times) and times[i] < end:
                    quarters.add(first)
                    break
    with store._lock:
        rows = store.con.execute(
            "SELECT DISTINCT date_trunc('hour', t) FROM ("
            f"SELECT ingest_time AS t FROM {rest.table} WHERE source = ? AND ingest_time >= ? "
            "UNION ALL SELECT to_timestamp(unnest(?::BIGINT[]))) AS x",
            [*rest.params, source, since, [q * 900 for q in quarters]]).fetchall()
    return [r[0] for r in rows]


# ── warming it at start ──────────────────────────────────────────────────────
def warm(store, stop: threading.Event | None = None) -> dict | None:
    """Build the entry of every session born in the table, newest first, a few sessions in each
    transaction, so ingest is never held. Returns what was built, or None when stopped. A read
    that comes first builds what it needs itself and answers the same."""
    from . import session_events
    digest = of(store)
    t0 = time.perf_counter()
    sweep(store, force=True)
    session_events._duck_sources(store)
    with store.sessions_db.consistent_read() as c:
        born = session_events._born(store, c, None)
    todo = [b.session for b in sorted(born, key=lambda b: str(b.last_line_at or ""), reverse=True)
            if b.lines_complete]
    lines = 0
    turn = digest.turn(warm=True)
    for i in range(0, len(todo), WARM_BATCH):
        if (stop is not None and stop.is_set()) or turn.refused:      # refused: it is full
            break
        keys = todo[i:i + WARM_BATCH]
        with store.sessions_db.consistent_read() as c:
            batch = session_events._born(store, c, None, keys=keys)
            batch.sort(key=lambda b: keys.index(b.session))       # newest first, as `todo`
            plan = load(store, c, [b for b in batch if b.lines_complete], False, turn)
        lines += sum(e.lines for e in finish(store, plan).values())
        time.sleep(0.001)                        # let a waiting request in
    if stop is not None and stop.is_set():
        return None
    return {"sessions": len(todo), "lines": lines, "bytes": digest.bytes,
            "seconds": time.perf_counter() - t0}


def start(store) -> threading.Thread:
    """Warm the digest in a background thread, in the factory edition. Nothing waits for it.
    `Store.close` stops it."""
    stop = store._digest_stop = threading.Event()

    def run():
        try:
            if stop.is_set() or not session_read._factory(store):
                return
            done = warm(store, stop)
            if done and done["sessions"]:
                print(f"taresd: transcript digest warmed in {done['seconds']:.1f}s: "
                      f"{done['sessions']} session(s), {done['lines']} line(s), "
                      f"{done['bytes'] / 1048576:.1f} MiB", flush=True)
        except Exception as e:
            if not stop.is_set():
                print(f"taresd: the transcript digest was not warmed ({type(e).__name__}: {e}); "
                      "reads build it as they go", flush=True)

    th = store._digest_thread = threading.Thread(target=run, daemon=True,
                                                 name="session-digest-warm")
    th.start()
    return th
