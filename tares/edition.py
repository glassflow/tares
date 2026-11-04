"""The cell's edition (TR-724): factory, sre or platform, stored in the cell's settings.

TARES_EDITION seeds it on the first start; after that the stored value wins and
PUT /api/settings/edition switches a running cell; the AI SRE Settings page does the same through
PUT /api/sre/settings/edition, which asks for a confirmation first.
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
    raw = (os.getenv(EDITION_ENV) or "").strip()
    if raw and not _valid(raw):
        print(f"taresd: {EDITION_ENV}={raw!r} is not an edition "
              f"(use {', '.join(EDITIONS[:-1])} or {EDITIONS[-1]}); ignored")
    stored = _valid(store.get_setting(EDITION_SETTING))
    if stored:
        return stored
    chosen = _valid(raw)
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


OBJECT_TOOLS_SETTING = "mcp_object_tools"
OBJECT_TOOLS_ENV = "TARES_MCP_OBJECT_TOOLS"


def object_tools(store, edition_value: str) -> bool:
    """Whether the MCP server lists the building-block tools (connections, raw data, rules) to a
    person's agent: the stored choice, else TARES_MCP_OBJECT_TOOLS, else on only for platform."""
    stored = str(store.get_setting(OBJECT_TOOLS_SETTING) or "").strip()
    if stored in ("1", "0"):
        return stored == "1"
    raw = (os.getenv(OBJECT_TOOLS_ENV) or "").strip().lower()
    if raw in ("1", "true"):
        return True
    if raw in ("0", "false"):
        return False
    return edition_value == "platform"
