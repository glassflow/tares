"""A cell's state on Postgres (P-TR-268), behind the same `execute(sql, params)` the store uses
on DuckDB.

A cell keeps its events in DuckDB. With TARES_STATE_DSN set it keeps its state (projects, tickets,
agents, runs, settings: every table but EVENT_TABLES) in Postgres instead, so the many small reads
and writes of a busy page run in parallel, each thread on its own connection. Without it the state
stays in the DuckDB file, exactly as before: nothing changes for a local install.

The store's SQL is written for DuckDB. `PgState.execute` translates it once per statement text
(cached): `?` placeholders, JSON paths, `arg_max`, `date_diff`, `INTERVAL n unit`, and the DDL
types. Values come back as DuckDB returns them: JSON as its text, timestamps as aware datetimes.

Connections are opened in autocommit mode with prepared statements off, so they work through
PgBouncer in transaction mode; an explicit BEGIN .. COMMIT keeps one connection for its span.
"""
from __future__ import annotations

import functools
import os
import re
import threading
import time

EVENT_TABLES = frozenset({"events", "cursors", "source_stats", "entity_counts",
                          "entity_label_state"})


# --- translation ---------------------------------------------------------------------------------

def _split_args(s: str, start: int) -> tuple[list[str], int]:
    """The comma-separated arguments of the call whose '(' is at s[start-1], and the index just
    past its ')'. Aware of nested parentheses and quoted strings."""
    args, depth, i, cur, quote = [], 0, start, [], None
    while i < len(s):
        c = s[i]
        if quote:
            cur.append(c)
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c
            cur.append(c)
        elif c == "(":
            depth += 1
            cur.append(c)
        elif c == ")":
            if depth == 0:
                args.append("".join(cur).strip())
                return args, i + 1
            depth -= 1
            cur.append(c)
        elif c == "," and depth == 0:
            args.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
        i += 1
    raise ValueError(f"unbalanced call in SQL: {s[start - 20:start + 40]!r}")


def _rewrite_calls(sql: str, name: str, fn) -> str:
    """Replace every call `name(args)` (case-insensitive) with fn(args)."""
    pat = re.compile(rf"\b{name}\s*\(", re.I)
    out, pos = [], 0
    while True:
        m = pat.search(sql, pos)
        if not m:
            out.append(sql[pos:])
            return "".join(out)
        args, end = _split_args(sql, m.end())
        out.append(sql[pos:m.start()])
        out.append(fn([_rewrite_calls(a, name, fn) for a in args]))
        pos = end


_PATH_STEP = re.compile(r'\."([^"]*)"|\.([^.\["]+)|\[(\d+)\]')


def _json_path(p: str) -> str:
    """'$.a.b', '$."a b"' or '$.a[0].b' -> a Postgres path literal '{a,b}' / '{"a b"}' /
    '{a,0,b}' (Postgres reads a number in the path as an array index). Anything else is refused,
    not guessed."""
    p = p.strip()
    if not (p.startswith("'$") and p.endswith("'")):
        raise ValueError(f"unsupported JSON path {p!r}")
    body, keys, pos = p[2:-1], [], 0
    for m in _PATH_STEP.finditer(body):
        if m.start() != pos:
            break
        keys.append(m.group(3) if m.group(3) is not None else (m.group(1) if m.group(1) is not None
                                                                 else m.group(2)))
        pos = m.end()
    if pos != len(body):
        raise ValueError(f"unsupported JSON path {p!r}")
    return "'{" + ",".join('"' + k.replace('"', '\\"') + '"' for k in keys) + "}'"


def _strip_comments(sql: str) -> str:
    """Drop '--' comments outside quoted strings: a quote or '?' in one would otherwise shift the
    placeholders and the call parsing."""
    out, quote, i = [], None, 0
    while i < len(sql):
        c = sql[i]
        if quote:
            out.append(c)
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c
            out.append(c)
        elif c == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            i = len(sql) if j < 0 else j
            continue
        else:
            out.append(c)
        i += 1
    return "".join(out)


def _date_diff(a: list[str]) -> str:
    unit = a[0].strip().strip("'").lower()
    start, end = f"CAST({a[1]} AS timestamptz)", f"CAST({a[2]} AS timestamptz)"
    if unit == "day":          # DuckDB counts the day boundaries crossed, in UTC
        return f"(CAST(({end}) AT TIME ZONE 'UTC' AS date) - CAST(({start}) AT TIME ZONE 'UTC' AS date))"
    if unit not in _EPOCH_UNIT:
        raise ValueError(f"date_diff in {unit!r} is not translated for Postgres (state_db.to_pg)")
    return f"(EXTRACT(EPOCH FROM ({end} - {start})) * {_EPOCH_UNIT[unit]})"


def _placeholders(sql: str) -> str:
    """'?' -> '%s' and a literal '%' -> '%%', outside quoted strings only."""
    out, quote = [], None
    for c in sql:
        if quote:
            out.append("%%" if c == "%" else c)
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c
            out.append(c)
        elif c == "?":
            out.append("%s")
        elif c == "%":
            out.append("%%")
        else:
            out.append(c)
    return "".join(out)


_DDL_TYPES = [(re.compile(r"\bDOUBLE\b(?!\s+PRECISION)", re.I), "DOUBLE PRECISION"),
              (re.compile(r"\bJSON\b", re.I), "TEXT")]   # JSON is stored and read back as text
_INTERVAL = re.compile(r"\bINTERVAL\s+(\d+)\s+(MICROSECOND|MILLISECOND|SECOND|MINUTE|HOUR|DAY|"
                       r"WEEK|MONTH|YEAR)S?\b", re.I)


@functools.lru_cache(maxsize=4096)
def to_pg(sql: str) -> str:
    """DuckDB SQL as the store writes it -> the same statement for Postgres."""
    s = _strip_comments(sql)
    s = _rewrite_calls(s, "json_extract_string",
                       lambda a: f"(CAST({a[0]} AS jsonb) #>> {_json_path(a[1])})")
    s = _rewrite_calls(s, "json_extract",
                       lambda a: f"(CAST({a[0]} AS jsonb) #> {_json_path(a[1])})")
    # DuckDB's arg_max skips rows whose ordering value is NULL
    s = _rewrite_calls(s, "arg_max", lambda a: (
        f"(array_agg({a[0]} ORDER BY {a[1]} DESC) FILTER (WHERE ({a[1]}) IS NOT NULL))[1]"))
    s = _rewrite_calls(s, "date_diff", _date_diff)
    s = _rewrite_calls(s, "octet_length", lambda a: (
        "octet_length(" + re.sub(r"^encode\((.*)\)$", r"\1", a[0], flags=re.I) + ")"))
    s = _rewrite_calls(s, "json_type",                # DuckDB names the types in capitals
                       lambda a: f"upper(jsonb_typeof(CAST({a[0]} AS jsonb)))")
    s = _rewrite_calls(s, "regexp_matches", lambda a: f"(({a[0]}) ~ ({a[1]}))")
    # an empty IN list: DuckDB takes it, Postgres needs a subquery that returns nothing
    s = re.sub(r"\bNOT\s+IN\s*\(\s*\)", "<> ALL('{}')", s, flags=re.I)
    s = re.sub(r"\bIN\s*\(\s*\)", "= ANY('{}')", s, flags=re.I)
    s = _INTERVAL.sub(lambda m: f"INTERVAL '{m.group(1)} {m.group(2).lower()}'", s)
    for pat, rep in _DDL_TYPES:
        if re.search(r"^\s*(CREATE|ALTER)\b", s, re.I | re.M):   # after any -- comments
            s = pat.sub(rep, s)
    return _placeholders(s)


_EPOCH_UNIT = {"millisecond": 1000, "second": 1, "minute": 1 / 60, "hour": 1 / 3600}


# --- the connection --------------------------------------------------------------------------------

class _Result:
    """What the store reads from `execute(...)`: fetchone, fetchall, rowcount."""

    __slots__ = ("_rows", "rowcount", "_i")

    def __init__(self, rows, rowcount):
        self._rows, self.rowcount, self._i = rows, rowcount, 0

    def fetchone(self):
        if self._i >= len(self._rows):
            return None
        self._i += 1
        return self._rows[self._i - 1]

    def fetchall(self):
        rows, self._i = self._rows[self._i:], len(self._rows)
        return rows


class PgState:
    """A DuckDB-connection lookalike over Postgres: one connection per thread."""

    def __init__(self, dsn: str, wait: float | None = None):
        import psycopg                                     # optional: only a cloud cell needs it
        from psycopg.types.string import TextLoader
        self._psycopg, self._dsn = psycopg, dsn
        self._local = threading.local()
        self._adapt = psycopg.adapt.AdaptersMap(psycopg.adapters)
        for name in ("json", "jsonb"):                     # DuckDB hands JSON back as its text
            self._adapt.register_loader(name, TextLoader)
        # fail at startup, not on first read; but wait a while first: a new cell's login reaches
        # PgBouncer a minute or so after its Secret does (the kubelet syncs the user list late);
        # 120 s stays inside the cloud liveness probe, whose restart then simply tries again
        if wait is None:
            wait = float(os.getenv("TARES_STATE_CONNECT_WAIT", "120"))
        deadline, said = time.monotonic() + wait, False
        while True:
            try:
                self.conn()
                break
            except psycopg.OperationalError as e:
                if time.monotonic() >= deadline:
                    raise
                if not said:
                    print(f"taresd: waiting for the state database: {str(e).strip().splitlines()[0][:120]}",
                          flush=True)
                    said = True
                time.sleep(5)

    def conn(self):
        c = getattr(self._local, "c", None)
        if c is None or c.closed:
            # a dropped SYN (no egress to PgBouncer yet) waits seconds, not the kernel's minutes
            extra = {} if "connect_timeout" in self._dsn else {"connect_timeout": 10}
            c = self._psycopg.connect(self._dsn, autocommit=True, prepare_threshold=None,
                                      context=self._adapt, **extra)
            self._local.c = c
        return c

    def execute(self, sql: str, params=None) -> _Result:
        q = to_pg(sql)
        c = getattr(self._local, "c", None)
        # read before the statement: once the connection drops its status is UNKNOWN, not IDLE
        idle = c is None or c.closed or c.info.transaction_status == c.info.transaction_status.IDLE
        try:
            return self._run(q, params)
        except self._psycopg.OperationalError:
            c = getattr(self._local, "c", None)
            if not idle or (c is not None and not c.closed):
                raise                  # mid-transaction, or the server refused the statement
            self._local.c = None                           # a dropped connection: one retry
            return self._run(q, params)

    def _run(self, q: str, params) -> _Result:
        with self.conn().cursor() as cur:
            cur.execute(q, list(params or []))
            rows = cur.fetchall() if cur.description is not None else []
            return _Result(rows, cur.rowcount)

    def executemany(self, sql: str, seq) -> None:
        with self.conn().cursor() as cur:
            cur.executemany(to_pg(sql), [list(p) for p in seq])

    def close(self) -> None:
        c = getattr(self._local, "c", None)
        if c is not None:
            c.close()


def state_ddl(statements) -> list[str]:
    """The CREATE / ALTER / CREATE INDEX statements among `statements` that name a state table,
    i.e. what the Postgres state database needs from the store's DuckDB schema."""
    out = []
    for stmt in statements:
        m = re.search(r"\b(?:TABLE(?:\s+IF\s+(?:NOT\s+)?EXISTS)?|\bON)\s+([a-z_][a-z0-9_]*)",
                      stmt, re.I)
        if m and m.group(1).lower() not in EVENT_TABLES and stmt.strip():
            out.append(stmt)
    return out


# --- the one-time move -----------------------------------------------------------------------------

_META = ("CREATE TABLE IF NOT EXISTS tares_state_meta (key TEXT PRIMARY KEY, value TEXT, "
         "updated_at TIMESTAMPTZ DEFAULT now())")


def moved(pg: PgState) -> bool:
    """Whether this state database already holds the cell's state (the move ran to the end)."""
    pg.execute(_META)
    return pg.execute("SELECT 1 FROM tares_state_meta WHERE key = 'moved_from_duckdb'"
                      ).fetchone() is not None


def move(duck, pg: PgState, tables: list[str]) -> dict:
    """Copy every state table from the DuckDB connection into Postgres, in one transaction: the
    tables are emptied, filled, counted against DuckDB, and the move is recorded. Anything going
    wrong rolls it all back, and the cell keeps its state in DuckDB. Returns {table: rows}."""
    counts = {}
    c = pg.conn()
    with c.transaction():
        with c.cursor() as cur:
            for t in tables:
                pg_cols = [r[0] for r in cur.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema = "
                    "current_schema() AND table_name = %s ORDER BY ordinal_position", [t])]
                if not pg_cols:
                    raise RuntimeError(f"state table {t} is missing in the state database")
                duck_cols = {r[0] for r in duck.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema = "
                    "current_schema() AND table_name = ?", [t]).fetchall()}
                cols = [col for col in pg_cols if col in duck_cols]
                rows = duck.execute(f"SELECT {', '.join(cols)} FROM {t}").fetchall()
                cur.execute(f"TRUNCATE {t}")
                quoted = ", ".join(f'"{col}"' for col in cols)
                with cur.copy(f"COPY {t} ({quoted}) FROM STDIN") as cp:
                    for r in rows:
                        cp.write_row(r)
                n = cur.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                if n != len(rows):
                    raise RuntimeError(f"state table {t}: {len(rows)} rows read, {n} written")
                counts[t] = n
            cur.execute("INSERT INTO tares_state_meta (key, value) VALUES "
                        "('moved_from_duckdb', %s)", [str(sum(counts.values()))])
    print(f"taresd: moved the state into Postgres: {len(counts)} tables, "
          f"{sum(counts.values())} rows", flush=True)
    return counts
