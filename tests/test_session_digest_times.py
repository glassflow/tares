"""The reads the transcript digest answers stay far cheaper than reading the stored lines (TR-1695).

A factory cell holds LINES lines of SESSIONS sessions in the session store alone, spread over four
weeks. Each read below is timed twice on that cell: from the digest, as the daemon reads it, and from
the stored lines themselves (the read as it was before the digest). It passes when the digest is at
least `times` as fast (the second column of READS) and gives the same answer. Both sides run on the
same machine, so the limit does not move with the machine's speed; each takes its fastest run, so a
busy moment does not decide it. A count that a trigger evaluates on an ingested line is timed right
after a line of one session arrives, which is the case that reads a session again.

Run: python tests/test_session_digest_times.py
Postgres: TARES_TEST_PG=postgresql://... PYTHONPATH=tests/pg_harness:. python tests/test_session_digest_times.py
"""
import json
import os
import sys
import time
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_session_readers_held as held  # noqa: E402  (sets the factory environment)
from test_session_readers_held import HIDDEN, NOW, SRC, ck, line, open_store, patched  # noqa: E402

from sqlalchemy import insert  # noqa: E402

from tares import session_digest  # noqa: E402
from tares.session_store.tables import session_lines, sessions  # noqa: E402

SESSIONS = 300
LINES_EACH = 300
LINES = SESSIONS * LINES_EACH
REPOS = ("shop", "docs", "billing", "search", "inbox")
KINDS = ("user", "assistant", "tool_use", "tool_result")


def fill(s) -> None:
    """Session i started i * 2 hours ago (a bit under four weeks for the last), a line each 10 s."""
    rows, heads = [], []
    for i in range(SESSIONS):
        sid, repo = f"s{i:04d}", REPOS[i % len(REPOS)]
        start = NOW - timedelta(hours=2 * i + 1)
        for n in range(LINES_EACH):
            kind = KINDS[n % len(KINDS)]
            labels = {"session": sid, "repo": repo, "branch": f"b{i % 7}", "model": "m",
                      "type": kind, "sidechain": "false"}
            rows.append({"session": sid, "seq": n + 1, "at": start + timedelta(seconds=10 * n),
                         "type": kind, "labels": json.dumps(labels), "payload": "{}"})
        heads.append({"session": sid, "source": SRC, "started_at": start,
                      "last_line_at": start + timedelta(seconds=10 * (LINES_EACH - 1)),
                      "line_count": LINES_EACH, "tool_calls": 0, "coordination_calls": 0,
                      "lines_complete": True, "updated_at": NOW, "lines_origin": "table"})
    with s.sessions_db.begin() as c:
        c.execute(insert(sessions), heads)
        for i in range(0, len(rows), 5000):
            c.execute(insert(session_lines), rows[i:i + 5000])


def count(s, ago, **kw):
    return s.aggregate([SRC], None, "count", NOW - ago, **kw)


MONTH, DAY = timedelta(days=30), timedelta(days=1)

# (name, how many times as fast as the stored lines, the read)
READS = [
    ("count over 30 days", 5, lambda s: count(s, MONTH)),
    ("count over 24 hours", 2, lambda s: count(s, DAY)),
    ("30 days of one event type, by repo", 5, lambda s: count(
        s, MONTH, filters=[{"field": "event_type", "op": "eq", "value": "user"}], group_by="repo")),
    ("ingest gaps over 30 days", 5, lambda s: [round(g, 5) for g in
                                                s.recent_ingest_gaps(SRC, NOW - MONTH)]),
    ("ingest hours over 30 days", 5, lambda s: sorted(s.recent_ingest_hours(SRC, NOW - MONTH))),
    ("repos, a project hidden", 2, lambda s: sorted(
        (e["value"], e["events"], str(e["last_ingest"]))
        for e in s.list_entities("repo", [SRC], exclude=HIDDEN))),
]


def best(read, runs):
    took, got = [], None
    for _ in range(runs):
        t = time.perf_counter()
        got = read()
        took.append(time.perf_counter() - t)
    return min(took), got


def from_lines(s, read, runs=3):
    """The read answered from the stored lines, without the digest."""
    with patched(s, "_from_digest", lambda *a: None):
        return best(lambda: read(s), runs)


def judge(name, times, fast, slow, same, answer) -> None:
    print(f"       {name}: {fast * 1000:.1f} ms, from the stored lines {slow * 1000:.1f} ms "
          f"({slow / fast:.0f} times)")
    ck(f"{name}: the same answer", same, str(answer)[:300])
    ck(f"{name}: at least {times} times as fast as the stored lines", slow >= times * fast,
       f"{fast * 1000:.1f} ms against {slow * 1000:.1f} ms")


def main() -> int:
    s = open_store("factory")
    t = time.perf_counter()
    fill(s)
    print(f"{LINES} lines of {SESSIONS} sessions written in {time.perf_counter() - t:.1f} s")
    t = time.perf_counter()
    done = session_digest.warm(s)
    print(f"warmed in {time.perf_counter() - t:.1f} s: {done}")
    ck("the warm-up took every session and line",
       (done["sessions"], done["lines"]) == (SESSIONS, LINES), done)
    for name, times, read in READS:
        read(s)                                     # the first read of its shape
        fast, got = best(lambda: read(s), 5)
        slow, want = from_lines(s, read)
        ck(f"{name}: there is something to count", bool(want), want)
        judge(name, times, fast, slow, got == want, got)

    # a trigger evaluates on each ingested line: one session has changed and is read again
    read = READS[0][2]
    took = []
    for n in range(5):
        s.append([line("s0007", "assistant", f"one more {n}", 30 - n)])
        t = time.perf_counter()
        got = read(s)
        took.append(time.perf_counter() - t)
    slow, want = from_lines(s, read)
    judge("count over 30 days right after a line arrived", 3, min(took), slow, got == want, got)
    ck("the new lines were counted", got.get("s0007") == LINES_EACH + 5, got.get("s0007"))

    s.close()
    print("all passed" if not held.FAILS else f"{len(held.FAILS)} failed")
    return 1 if held.FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
