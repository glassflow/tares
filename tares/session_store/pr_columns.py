"""tickets.pr_repo and tickets.pr_number: the pull request of a ticket as two indexed columns
(TR-1614).

`pr` (JSON text) stays the record; the two columns are what a lookup by pull request reads. This
module is the one place that says what they hold for a `pr`, so the writes,
the repair at start and the lookup cannot disagree.

`pr_repo` is the repo name in lower case, `pr_number` the number. Each is NULL when `pr` has no
value for it that a lookup could ask for, or none the column and its index can hold.
"""
from __future__ import annotations

import json
import logging

import sqlalchemy as sa
from sqlalchemy import Connection

log = logging.getLogger("tares.session_store")

_MAX_NUMBER = 2**63 - 1          # a BigInteger column
_MAX_REPO_BYTES = 1000           # Postgres refuses an index entry of a few thousand bytes

# only the columns read and written here
_TICKETS = sa.table("tickets", sa.column("id", sa.Text), sa.column("pr", sa.Text),
                    sa.column("pr_repo", sa.Text), sa.column("pr_number", sa.BigInteger))


def pr_columns(pr) -> tuple[str | None, int | None]:
    """(pr_repo, pr_number) of a decoded `pr` value. Never raises."""
    if not isinstance(pr, dict):
        return None, None
    repo = str(pr.get("repo") or "").lower()
    try:
        # Postgres text holds no NUL, and a lone surrogate is not UTF-8
        if not repo or "\x00" in repo or len(repo.encode("utf-8")) > _MAX_REPO_BYTES:
            repo = None
    except UnicodeEncodeError:
        repo = None
    n = pr.get("number")
    # a whole number only: not its text, not true or false (the range also leaves out nan and inf)
    if type(n) not in (int, float) or not 0 < n <= _MAX_NUMBER or n != int(n):
        return repo, None
    return repo, int(n)


def _of_text(text: str | None) -> tuple[str | None, int | None]:
    """The same for the stored text, which may be no JSON at all."""
    try:
        pr = json.loads(text) if text else None
    except (ValueError, RecursionError):
        return None, None
    return pr_columns(pr)


def fill_pr_columns(conn: Connection) -> int:
    """Make the two columns say what `pr` says, in the caller's transaction, and return how many
    rows differed. Only those rows are written. `pr` is read here and not with the database's JSON
    functions: one odd text would fail the whole statement, and Postgres before 16 has no way to
    ask whether a text is JSON."""
    t = _TICKETS
    rows = conn.execute(sa.select(t.c.id, t.c.pr, t.c.pr_repo, t.c.pr_number).where(sa.or_(
        t.c.pr.is_not(None), t.c.pr_repo.is_not(None), t.c.pr_number.is_not(None)))).all()
    stale = []
    for tid, text, repo, number in rows:
        want = _of_text(text)
        if want != (repo, number):
            stale.append({"tid": tid, "was": text, "repo": want[0], "number": want[1]})
    if stale:
        # only while `pr` is still what was read: a write in between has set the columns itself
        conn.execute(sa.update(t).where(
            t.c.id == sa.bindparam("tid"), t.c.pr.is_not_distinct_from(sa.bindparam("was"))
        ).values(pr_repo=sa.bindparam("repo"), pr_number=sa.bindparam("number")), stale)
        log.info("session store: set pr_repo and pr_number of %d tickets from pr", len(stale))
    return len(stale)
