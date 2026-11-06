"""Seed people into a Tares database before a daemon starts on it (not a test).

    import _people
    _people.seed(db_path, [("Ann", "ann@example.com"), ("Ben", "ben@example.com")])

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
