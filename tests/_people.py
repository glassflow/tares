"""Seed people into a Tares database before a daemon starts on it (not a test).

    import _people
    _people.seed(db_path, [("Ann", "ann@example.com"), ("Ben", "ben@example.com")])

From a shell script or a browser suite (before the daemon starts on that file):

    python tests/_people.py <db_path> "Ann=ann@example.com" "Ben B=ben@example.com"

A test that needs another person on the cell, but is not about several people, seeds them here
instead of adding them over HTTP. Adding a second person over HTTP is for the tests under ee/tests,
which run with a licence. Call it with no daemon running on that file: DuckDB has one writer.
"""
from tares.store import Store


def seed(db_path: str, people: list) -> list:
    """Create each (name, email) as a person in the database at `db_path`, as an admin would,
    and return the created rows. The store is closed before this returns. It opens DuckDB only
    (state_dsn=""), so it never follows a Postgres state env the runner sets."""
    store = Store(db_path, state_dsn="")
    rows = [store.create_person(name, email=email, via="admin") for name, email in people]
    del store
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
