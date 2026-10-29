"""The cell's edition (TR-724): factory, sre or platform, stored in the cell's settings.

TARES_EDITION seeds it on the first start; after that the stored value wins and
PUT /api/settings/edition is the only way to switch a running cell.
"""
from __future__ import annotations

import os

EDITIONS = ("factory", "sre", "platform")
DEFAULT = "platform"
EDITION_SETTING = "edition"
EDITION_ENV = "TARES_EDITION"
NAMES = {"factory": "Software Factory", "sre": "AI SRE", "platform": "Tares"}


def _valid(v) -> str | None:
    v = str(v or "").strip().lower()
    return v if v in EDITIONS else None


def store_has_data(store) -> bool:
    """A project other than the auto-made Default, or any catalog source."""
    if any(p.get("name") != store.DEFAULT_PROJECT_NAME for p in store.list_projects()):
        return True
    return bool(store.list_catalog_sources())


def settle(store) -> str | None:
    """Run once at startup, before any seeding or catalog import (a seeded empty cell would
    otherwise be settled as platform by its own seed). Returns the stored edition, or None
    when the cell has none yet and the first run has to ask."""
    stored = _valid(store.get_setting(EDITION_SETTING))
    if stored:
        return stored
    chosen = _valid(os.getenv(EDITION_ENV))
    if not chosen and store_has_data(store):
        chosen = DEFAULT
    if chosen:
        store.set_setting(EDITION_SETTING, chosen)
    return chosen


def edition(store) -> tuple[str, bool]:
    """(edition, needed): the stored edition, else platform; needed is True while nothing is
    stored. Read on every call so a switch applies on the next request."""
    stored = _valid(store.get_setting(EDITION_SETTING))
    return (stored or DEFAULT), stored is None
