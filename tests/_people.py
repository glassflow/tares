"""Seed people into a Tares database before a daemon starts on it (not a test).

    import _people
    _people.seed(db_path, [("Ann", "ann@example.com"), ("Ben", "ben@example.com")])

From a shell script or a browser suite (before the daemon starts on that directory):

    python tests/_people.py <db_path> "Ann=ann@example.com" "Ben B=ben@example.com"

A test that needs another person on the cell, but is not about several people, seeds them here
instead of adding them over HTTP. Adding a second person over HTTP is for the tests under ee/tests,
which run with a licence.
"""
from tares.store import Store


def seed(db_path: str, people: list) -> list:
    """Create each (name, email) as a person in the store of the data directory `db_path`, as an
    admin would, and return the created rows. The store is the one a daemon on that directory opens
    (under tests/pg_harness, the same Postgres schema). It is closed before this returns."""
    store = Store(db_path)
    rows = [store.create_person(name, email=email, via="admin") for name, email in people]
    store.close()
    return rows


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        raise SystemExit('usage: python tests/_people.py <db_path> "Name=email" ...')
    pairs = []
    for arg in sys.argv[2:]:
        name, _, email = arg.rpartition("=")
        if not name or not email:
            raise SystemExit(f'_people.py: "{arg}" is not "Name=email"')
        pairs.append((name, email))
    seed(sys.argv[1], pairs)
