"""What the old DDL (`Store._ddl()`) still makes (TR-1618): the event tables, the tables only the
platform uses and the SRE tables. Every other table is made by a revision of the session store, so
a table added to the old DDL, or one moved without its line here, fails this test.

    python tests/test_old_ddl_left.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tares import state_db                                      # noqa: E402
from tares.session_store.tables import MOVED_TABLE_NAMES        # noqa: E402
from tares.store import Store                                   # noqa: E402

EVENT_TABLES = {"events", "cursors", "source_stats", "entity_counts", "entity_label_state"}
PLATFORM_AND_EVENT_TABLES = EVENT_TABLES | {
    # only the platform uses these
    "trigger_state", "subscriptions", "dispatch_deliveries", "catalog_triggers", "catalog_agents",
    "agent_runs", "mcp_servers", "ask_sessions", "query_log", "dispatch_log", "project_wiring",
    "run_projects", "dispatch_projects",
    # SRE
    "services", "watch_state", "incidents", "incident_notes", "incident_log", "finding_feedback",
}
# two tables of a removed feature: the old DDL only drops them
DROPPED = {"catalog_reviews", "review_runs"}

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
    ck("the old DDL makes no table outside the event, platform and SRE ones",
       not set(made) - PLATFORM_AND_EVENT_TABLES, sorted(set(made) - PLATFORM_AND_EVENT_TABLES))
    ck("and it makes every one of them", not PLATFORM_AND_EVENT_TABLES - set(made),
       sorted(PLATFORM_AND_EVENT_TABLES - set(made)))
    ck("each once", len(made) == len(set(made)), sorted(t for t in set(made) if made.count(t) > 1))
    touched = set(found["alter"]) | set(found["index"])
    ck("no ALTER or index of the old DDL is for another table",
       not touched - PLATFORM_AND_EVENT_TABLES, sorted(touched - PLATFORM_AND_EVENT_TABLES))
    ck("it drops only the two tables of a removed feature", set(found["drop"]) == DROPPED,
       found["drop"])
    ck("the event tables here are state_db's", EVENT_TABLES == set(state_db.EVENT_TABLES),
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
