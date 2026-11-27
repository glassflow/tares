"""The transcript digest reads archive parts a line at a time (TR-1792).

After a restart the warm-up added over a gigabyte to a daemon's memory: each archive part was
unpacked, split into lines and parsed as a whole before the first line was counted.

A factory cell holds BIG sessions archived as one part of BIG_LINES lines each, more archived sessions
than one warm-up batch, and a few still in the session store. The most memory the warm-up holds at
once, above what it started with, stays under LIMIT, which is less than one big part unpacked; so
does a read that finds nothing kept. The measure is `tracemalloc`'s peak: it counts what Python
allocates (the file, the unpacked text, the parsed lines, zlib's buffers), so it follows what the
code holds and not how the machine's allocator gives memory back. It is the same on Linux and macOS.
It does not see the database driver's own buffers.

Run: python tests/test_session_digest_memory.py
Postgres: TARES_TEST_PG=postgresql://... PYTHONPATH=tests/pg_harness:. python tests/test_session_digest_memory.py
"""
import contextlib
import gc
import io
import json
import os
import sys
import tracemalloc
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_session_readers_held as held  # noqa: E402  (sets the factory environment)
from test_session_readers_held import NOW, SRC, ck, open_store, patched  # noqa: E402

from sqlalchemy import insert, select, update  # noqa: E402

from tares import session_archive, session_digest  # noqa: E402
from tares.session_store.tables import session_archive_parts, session_lines, sessions  # noqa: E402

BIG, BIG_LINES = 2, 6000
SMALL, SMALL_LINES = session_digest.WARM_BATCH + 10, 40
TABLE, TABLE_LINES = 5, 100
PAYLOAD = 5000                      # characters in a line's payload
LIMIT = 10 * 1024 * 1024
KINDS = ("user", "assistant", "tool_use", "tool_result")
LINES = {**{f"big{i}": BIG_LINES for i in range(BIG)},
         **{f"small{i:03d}": SMALL_LINES for i in range(SMALL)},
         **{f"table{i}": TABLE_LINES for i in range(TABLE)},
         "empty": 0}                # a session with no lines
MONTH = NOW - timedelta(days=30)


def put(s, sid: str, n: int, days_ago: float) -> None:
    start = NOW - timedelta(days=days_ago)
    body = json.dumps({"message": {"content": "word " * (PAYLOAD // 5)}})
    labels = {k: json.dumps({"session": sid, "repo": "shop", "type": k}) for k in KINDS}
    with s.sessions_db.begin() as c:
        c.execute(insert(sessions).values(
            session=sid, source=SRC, started_at=start, last_line_at=start + timedelta(seconds=n),
            line_count=n, tool_calls=0, coordination_calls=0, lines_complete=True, updated_at=NOW,
            lines_origin="table"))
        for i in range(0, n, 1000):
            c.execute(insert(session_lines), [
                {"session": sid, "seq": j + 1, "line_key": f"{sid}-{j}",
                 "at": start + timedelta(seconds=j), "type": KINDS[j % 4], "labels": labels[KINDS[j % 4]],
                 "render": "{}", "payload": body} for j in range(i, min(i + 1000, n))])


def archive(s, sid: str) -> None:
    assert session_archive._archive_session(
        s.sessions_db, session_archive.backend_of(s), sid, NOW)


def fill(s) -> None:
    for i, (sid, n) in enumerate(LINES.items()):
        put(s, sid, n, 1 + i % 5 if sid.startswith("table") else 8 + i % 15)
        if sid.startswith(("big", "small")):
            archive(s, sid)


def forget(s) -> None:
    """As a process that has just started: nothing kept."""
    s.__dict__.pop("_session_digest", None)


def peak_of(read):
    """(the most bytes `read` held at once above what was there before it, what it gave)."""
    gc.collect()
    tracemalloc.start()
    try:
        got = read()
        return tracemalloc.get_traced_memory()[1], got
    finally:
        tracemalloc.stop()


def count(s) -> dict:
    return s.aggregate([SRC], None, "count", MONTH)


def part_of(s, sid: str):
    with s.sessions_db.begin() as c:
        return c.execute(select(session_archive_parts).where(
            session_archive_parts.c.session == sid)).one()


def mib(n: int) -> str:
    return f"{n / 1048576:.1f} MiB"


def main() -> int:
    s = open_store("factory")
    done = session_digest.warm(s)
    ck("an empty store warms to nothing", (done["sessions"], done["lines"]) == (0, 0), done)
    fill(s)
    big = len(session_archive.backend_of(s).get(part_of(s, "big0").key))
    unpacked = BIG_LINES * PAYLOAD
    ck("one big part unpacked is over the limit, its file well under", unpacked > 2 * LIMIT > 8 * big,
       (unpacked, big))
    want = {sid: n for sid, n in LINES.items() if n}

    session_digest.warm(s)                      # once unmeasured: imports and compiled statements
    forget(s)
    peak, done = peak_of(lambda: session_digest.warm(s))
    print(f"       the warm-up held at most {mib(peak)} more, a big part unpacked is {mib(unpacked)}")
    ck("the warm-up took every session and line",
       (done["sessions"], done["lines"]) == (len(LINES), sum(LINES.values())), done)
    ck(f"and held less than {mib(LIMIT)} more at once", peak < LIMIT, mib(peak))
    got = count(s)
    ck("every session is counted", got == want, {k: (got.get(k), v) for k, v in want.items()
                                                 if got.get(k) != v})
    with patched(s, "_from_digest", lambda *a: None):
        ck("as the stored lines count it", count(s) == got)

    forget(s)
    peak, got = peak_of(lambda: count(s))
    print(f"       a read with nothing kept held at most {mib(peak)} more")
    ck("a read with nothing kept gives the same", got == want, len(got))
    ck(f"and held less than {mib(LIMIT)} more at once", peak < LIMIT, mib(peak))

    print("== a part that cannot be read ==")
    t = session_archive_parts.c
    # every line of this file is counted before the record is found to be wrong about it
    with s.sessions_db.begin() as c:
        c.execute(update(session_archive_parts).where(t.session == "small000").values(
            lines=SMALL_LINES + 1))
    os.remove(os.path.join(session_archive.backend_of(s).root, part_of(s, "small001").key))
    forget(s)
    with contextlib.redirect_stdout(io.StringIO()) as out:
        done = session_digest.warm(s)
    ck("none of its lines are counted",
       done["lines"] == sum(LINES.values()) - 2 * SMALL_LINES, (done, out.getvalue()[:300]))
    kept = session_digest.of(s).entries
    ck("and its session is not kept, the others are",
       "small000" not in kept and "small001" not in kept and "small002" in kept
       and "big0" in kept, sorted(kept)[:5])

    s.close()
    print("all passed" if not held.FAILS else f"{len(held.FAILS)} failed")
    return 1 if held.FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
