"""Column types for the session store.

Every timestamp column in the store is a `UTCDateTime`. SQLite keeps no time zone, so the plain
DateTime type would hand back naive values there and aware ones on Postgres. This one stores UTC
and returns an aware UTC datetime on both.

JSON the store hands back to callers as text is declared `Text` (a JSON string the caller parses),
never a database JSON type, so both databases read it the same way. JSON the store parses itself
is a `JSONText`: a value is written as `json.dumps` text and read back with `json.loads`, in a TEXT
column on both engines (the old DDL made such a column TEXT on Postgres, so a table taken over
keeps its type).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import DateTime, Text
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """A timestamp stored as UTC and always read back as an aware UTC datetime.

    An aware input is converted to UTC, so +05:30 and the same instant in UTC are one value. A
    naive input is taken to be UTC (the store never holds local time). Anything that is not a
    datetime raises TypeError instead of being stored as text."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"UTCDateTime takes a datetime, not {type(value).__name__}")
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class JSONText(TypeDecorator):
    """A JSON value kept as text: `json.dumps` on the way in, `json.loads` on the way out, on
    SQLite and Postgres alike. Python None is SQL NULL (not the text `null`), and a NULL reads
    back as None. The column is TEXT, never a database JSON type."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else json.dumps(value)

    def process_result_value(self, value, dialect):
        return None if value is None else json.loads(value)
