"""What the old DDL (`Store._ddl()`) still makes (TR-1618): the events table alone (the SRE tables went
with the AI SRE edition, TR-1809; the tables of the source catalog, triggers, agents, dispatch, Ask and
the read log with the source catalog, TR-1811: an old database keeps them, a new one has none). Every
other table is made by a revision of the session store, so a table added to the old DDL, or one moved
without its line here, fails this test.

    python tests/test_old_ddl_left.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tares import state_db                                      # noqa: E402
from tares.session_store.tables import MOVED_TABLE_NAMES        # noqa: E402
from tares.store import Store                                   # noqa: E402

# state_db's event tables: the ones a move to Postgres leaves in DuckDB. entity_counts and
# entity_label_state went in TR-1810, cursors and source_stats in TR-1811: an old database keeps them
EVENT_TABLES = {"events"}
GONE_EVENT_TABLES = {"cursors", "source_stats", "entity_counts", "entity_label_state"}
PLATFORM_AND_EVENT_TABLES = EVENT_TABLES

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


_NAME = r"\"?(\w+)\"?"
_FORMS = {
    "create": rf"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?{_NAME}",
    "alter": rf"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?{_NAME}",
    "index": rf"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?\w+\s+ON\s+{_NAME}",
    "drop": rf"DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?{_NAME}",
}


def read(statements) -> tuple[dict, list]:
    """The table names each kind of statement targets, and the statements of no known kind."""
    found = {kind: [] for kind in _FORMS}
    unknown = []
    for stmt in statements:
        code = state_db._strip_comments(stmt).strip()
        if not code:
            continue
        for kind, form in _FORMS.items():
            m = re.match(form, code, re.I)
            if m:
                found[kind].append(m.group(1).lower())
                break
        else:
            unknown.append(" ".join(code.split())[:80])
    return found, unknown


def main():
    found, unknown = read(Store._ddl())
    made = found["create"]
    ck("every statement of the old DDL is one this test reads", not unknown, unknown)
    ck("the old DDL makes no table outside the event and platform ones",
       not set(made) - PLATFORM_AND_EVENT_TABLES, sorted(set(made) - PLATFORM_AND_EVENT_TABLES))
    ck("and it makes every one of them", not PLATFORM_AND_EVENT_TABLES - set(made),
       sorted(PLATFORM_AND_EVENT_TABLES - set(made)))
    ck("each once", len(made) == len(set(made)), sorted(t for t in set(made) if made.count(t) > 1))
    ck("no table that went is made", not GONE_EVENT_TABLES & set(made), sorted(GONE_EVENT_TABLES & set(made)))
    touched = set(found["alter"]) | set(found["index"])
    ck("no ALTER or index of the old DDL is for another table",
       not touched - PLATFORM_AND_EVENT_TABLES, sorted(touched - PLATFORM_AND_EVENT_TABLES))
    ck("it drops no table", not found["drop"], found["drop"])
    ck("the event tables here are state_db's", EVENT_TABLES | GONE_EVENT_TABLES == set(state_db.EVENT_TABLES),
       sorted(state_db.EVENT_TABLES))
    ck("none of them is also a table of the session store",
       not PLATFORM_AND_EVENT_TABLES & MOVED_TABLE_NAMES,
       sorted(PLATFORM_AND_EVENT_TABLES & MOVED_TABLE_NAMES))

    # the reader sees what it has to see
    found, unknown = read(["-- a note's quote\nCREATE TABLE sneaky (a TEXT)",
                           "CREATE VIEW v AS SELECT 1"])
    ck("a table behind a comment is read, and a statement of another kind is reported",
       found["create"] == ["sneaky"] and unknown == ["CREATE VIEW v AS SELECT 1"], (found, unknown))


main()
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
