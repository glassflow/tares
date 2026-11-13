"""The integration registry: finds the integrations a cell can use.

Integrations come from BUILTINS (import paths, so a source checkout works without a reinstall)
and from the `tares.integrations` entry point group (packages installed next to Tares, the ee
package among them). BUILTINS load first and the first class to take a (slot, name) wins.
A broken entry point or a class that is not an integration of its slot is logged and skipped;
it never fails start.
"""
from __future__ import annotations

import importlib
import logging
from importlib import metadata

from .base import Integration
from .ci import CISource
from .guardrails import Guardrail
from .memory import MemoryStore
from .monitoring import Monitoring
from .review import ReviewGate
from .runtime import Runtime
from .tickets import TicketBackend

log = logging.getLogger("tares.integrations")

GROUP = "tares.integrations"

SLOT_BASES: dict[str, type[Integration]] = {
    "tickets": TicketBackend,
    "review": ReviewGate,
    "ci": CISource,
    "memory": MemoryStore,
    "monitoring": Monitoring,
    "guardrails": Guardrail,
    "runtime": Runtime,
}
SLOTS = tuple(SLOT_BASES)

# "package.module:ClassName" import paths of the integrations shipped in tares/.
BUILTINS: list[str] = [
    "tares.integrations.builtin.tickets_tares:TicketsTares",
    "tares.integrations.builtin.tickets_linear:TicketsLinear",
    "tares.integrations.builtin.tickets_github:TicketsGithub",
    "tares.integrations.builtin.review_tares:TaresReview",
    "tares.integrations.builtin.ci_github:GithubCI",
    "tares.integrations.builtin.ci_local:LocalCI",
    "tares.integrations.builtin.guardrails_tares:TaresGuardrails",
    "tares.integrations.builtin.monitoring_tares:MonitoringOff",
    "tares.integrations.builtin.monitoring_tares:MonitoringRius",
    "tares.integrations.builtin.monitoring_tares:MonitoringOtlp",
]

_loaded: dict[tuple[str, str], type[Integration]] | None = None


def _where(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


def _load_path(path: str):
    module, _, attr = path.partition(":")
    return getattr(importlib.import_module(module), attr)


def _add(found: dict, cls, source: str) -> None:
    if not isinstance(cls, type) or not issubclass(cls, Integration):
        log.warning("integration %s skipped: not a subclass of Integration", source)
        return
    base = SLOT_BASES.get(cls.slot)
    if base is None:
        log.warning("integration %s skipped: unknown slot %r", source, cls.slot)
        return
    if not issubclass(cls, base):
        log.warning("integration %s skipped: slot %r needs a subclass of %s",
                    source, cls.slot, base.__name__)
        return
    if not cls.name:
        log.warning("integration %s skipped: it has no name", source)
        return
    key = (cls.slot, cls.name)
    held = found.get(key)
    if held is None:
        found[key] = cls
    elif held is not cls:
        log.warning("integration %s skipped: %s/%s is already taken by %s",
                    _where(cls), cls.slot, cls.name, _where(held))


def load(refresh: bool = False) -> dict[tuple[str, str], type[Integration]]:
    """Every integration found, keyed by (slot, name). Cached; `refresh=True` looks again."""
    global _loaded
    if _loaded is not None and not refresh:
        return _loaded
    found: dict[tuple[str, str], type[Integration]] = {}
    for path in list(BUILTINS):
        try:
            _add(found, _load_path(path), path)
        except Exception as e:  # noqa: BLE001 - a broken built-in must not fail start
            log.warning("integration %s skipped: %s: %s", path, type(e).__name__, e)
    try:
        points = list(metadata.entry_points(group=GROUP))
    except Exception as e:  # noqa: BLE001
        log.warning("integration entry points not read: %s: %s", type(e).__name__, e)
        points = []
    for ep in points:
        try:
            _add(found, ep.load(), f"{ep.name} ({ep.value})")
        except Exception as e:  # noqa: BLE001 - a broken entry point must not fail start
            log.warning("integration %s (%s) skipped: %s: %s",
                        ep.name, ep.value, type(e).__name__, e)
    _loaded = found
    return found


def available(slot: str) -> list[type[Integration]]:
    """The integration classes of `slot`, in the order they loaded. Empty for an unknown slot."""
    return [cls for (s, _), cls in load().items() if s == slot]


def get(slot: str, name: str) -> type[Integration] | None:
    """The integration class `name` of `slot`, or None."""
    return load().get((slot, name))
