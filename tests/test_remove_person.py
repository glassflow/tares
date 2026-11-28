"""A person who leaves is taken off every open ticket, including one with no stage or no status
(TR-1692).

`remove_person` clears the assignee on open tickets only: a finished ticket (status done or
canceled, or stage merged or shipped) keeps the name of who did it. A ticket whose stage or status
is NULL is open, so it loses the assignee too. Before the fix `stage NOT IN (...)` was not true for
a NULL stage, and such a ticket kept the person who had left.

Run: .venv/bin/python tests/test_remove_person.py
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

from sqlalchemy import select, update

from tares.store import Store
from tares.session_store.tables import tickets

P = F = 0


def eq(label, got, want):
    global P, F
    ok = got == want
    P += ok
    F += not ok
    print(("  ok   " if ok else "  FAIL ") + label + ("" if ok else f"  got {got!r}, want {want!r}"))


def main():
    tmp = tempfile.mkdtemp(prefix="tares-remove-person-")
    store = Store(os.path.join(tmp, "t"))
    try:
        uid = "uc_rm"
        store.create_project(uid, "rm")
        gus = store.create_person("Gus", email="gus@example.com")["id"]
        cases = {
            "no stage": {"status": "todo", "stage": None},
            "no status": {"status": None, "stage": "building"},
            "no stage, no status": {"status": None, "stage": None},
            "open": {"status": "in_progress", "stage": "building"},
            "done": {"status": "done", "stage": None},
            "merged": {"status": None, "stage": "merged"},
        }
        ids = {}
        for label, cols in cases.items():
            tid = store.create_ticket(uid, "tares", label, assignee_person_id=gus)
            with store.sessions_db.begin() as conn:
                conn.execute(update(tickets).where(tickets.c.id == tid).values(**cols))
            ids[label] = tid
        eq("the person is removed", store.remove_person(gus), True)
        with store.sessions_db.read() as conn:
            held = dict(conn.execute(select(tickets.c.id, tickets.c.assignee_person_id)).all())
        for label in ("no stage", "no status", "no stage, no status", "open"):
            eq(f"an open ticket ({label}) loses the assignee", held[ids[label]], None)
        for label in ("done", "merged"):
            eq(f"a finished ticket ({label}) keeps the assignee", held[ids[label]], gus)
    finally:
        store.close()
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    main()
