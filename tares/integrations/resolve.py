"""Which integration a slot uses, and with which integration settings (TR-840).

`effective(slot, project_uid, store=...)` is the one function every caller uses. It looks, in
this order, and stops at the first that applies:

  1. the project's own choice (slots tickets, review and ci only), source "project";
  2. the factory's choice, source "factory";
  3. a choice carried over from links the cell already has, source "carried" (`carried` returns
     None until a later ticket fills it in);
  4. the built-in default of the slot, source "default".

A project's `settings` are only its own stored row, never merged with the factory's, so a
project can never use a factory secret.

A choice whose integration is not installed, or needs an enterprise licence the cell does not
have, resolves to the slot's default, with `reason` saying which. `effective` never raises for a
stored name it cannot use.

`settings` are as the store reads them: a secret value is "" (see FactoryStore.get_choice). The
runtime that needs the real value reads it itself; it is not added here.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import licence
from . import registry
from .registry import SLOTS

# The slots a project may override, the same three the store accepts (factory_store.PROJECT_SLOTS).
PROJECT_SLOTS = ("tickets", "review", "ci")

DEFAULTS: dict[str, str] = {
    "tickets": "tares",
    "review": "tares",
    "ci": "local",
    "memory": "tares",
    "monitoring": "off",
    "guardrails": "tares",
    "runtime": "claude-code",
}

NOT_INSTALLED = "not installed"
NEEDS_LICENCE = "needs an enterprise licence"


@dataclass(frozen=True)
class Effective:
    """`integration` is the name inside the slot. `source` is "project", "factory", "carried" or
    "default". `reason` is "" unless a stored choice was passed over for the default."""
    integration: str
    settings: dict = field(default_factory=dict)
    source: str = "default"
    reason: str = ""


def carried(slot: str, project_uid: str | None) -> tuple[str, dict] | None:
    """A choice taken from links the cell already has, as (name, settings); None when there is
    none. Nothing is carried yet: a later ticket fills this in."""
    return None


def _default(slot: str, reason: str = "") -> Effective:
    return Effective(DEFAULTS[slot], {}, "default", reason)


def _usable(slot: str, name: str, settings: dict, source: str) -> Effective:
    """The choice as it stands, or the slot's default with the reason it cannot be used."""
    cls = registry.get(slot, name)
    if cls is None:
        return _default(slot, NOT_INSTALLED)
    if cls.ee and not licence.ee_allowed():
        return _default(slot, NEEDS_LICENCE)
    return Effective(name, dict(settings or {}), source)


def effective(slot: str, project_uid: str | None = None, *, store) -> Effective:
    """The integration `slot` uses for the project (the factory when `project_uid` is None).
    `store` is the cell's store. Raises ValueError only for a slot that does not exist."""
    if slot not in SLOTS:
        raise ValueError(f"slot must be one of {', '.join(SLOTS)} (got {slot!r})")
    if project_uid and slot in PROJECT_SLOTS:
        row = store.get_project_choice(project_uid, slot)
        if row is not None:
            return _usable(slot, row["name"], row["settings"], "project")
    row = store.get_choice(slot)
    if row is not None:
        return _usable(slot, row["name"], row["settings"], "factory")
    found = carried(slot, project_uid)
    if found is not None:
        return _usable(slot, found[0], found[1], "carried")
    return _default(slot)
