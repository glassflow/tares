"""Which integration a slot uses, and with which integration settings (TR-840).

`effective(slot, project_uid, store=...)` is the one function every caller uses. It looks, in
this order, and stops at the first that applies:

  1. the project's own choice (slots tickets, review and ci only), source "project";
  2. the factory's choice, source "factory";
  3. a choice carried over from links and settings the cell already has (a project's Linear or
     GitHub issues link, its GitHub repos, the tracing settings), source "carried";
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


def _has_github_repos(project_uid: str, store) -> bool:
    """The project works in a repo on GitHub: its issues link names one, or it was handed to the
    crew with an owner/name repo."""
    link = store.get_project_github(project_uid)
    if link and link.get("repo"):
        return True
    repos = (store.get_handover(project_uid) or {}).get("repos") or []
    return any("/" in str(r.get("name") or "") for r in repos)


def carried(slot: str, project_uid: str | None, store=None) -> tuple[str, dict] | None:
    """A choice taken from links and settings the cell already has, as (name, settings); None when
    there is none, or when it would be the slot's default anyway. Read when asked: nothing is
    copied, and no secret is carried (the integration reads its own from the cell).

      tickets     the project's Linear link, else its GitHub issues link (the order
                  `project_ticket_home` uses when a project has both);
      ci          github for a project with a repo on GitHub;
      monitoring  the tracing provider (rius or otlp) while tracing is on and has an endpoint.

    The other slots have nothing to carry. A slot's name is only used when that integration is
    installed; `effective` passes over a name that is not.
    """
    if store is None:
        return None
    found: str | None = None
    if slot == "tickets" and project_uid:
        found = store.project_ticket_home(project_uid)
    elif slot == "ci" and project_uid:
        found = "github" if _has_github_repos(project_uid, store) else None
    elif slot == "monitoring":
        from .. import tracing
        cfg = tracing.resolve(store)
        found = cfg.provider if cfg.active else None
    if found is None or found == DEFAULTS[slot]:
        return None
    return found, {}


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
    found = carried(slot, project_uid, store)
    if found is not None:
        return _usable(slot, found[0], found[1], "carried")
    return _default(slot)
