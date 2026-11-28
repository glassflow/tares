"""The `custom` template: a project assembled by hand, and the `default` template every cell's
default project is an instance of.

Since the source catalog went (Tares 2.0, TR-1811) a project holds no sources, triggers, agents or
MCP servers: its object list is empty, and naming an object answers that it does not exist."""
from __future__ import annotations

from .base import PlannedObject, ProjectError, Template
from .registry import register


class CustomTemplate(Template):
    key = "custom"
    title = "From existing objects"
    description = "A project assembled by hand."
    PARAMS = {"objects": {"type": "list", "help": "always empty: a project holds no catalog objects"}}
    hidden = True   # reachable by key only

    def validate(self, params: dict) -> dict:
        out = super().validate(params)
        raw = out.get("objects")
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            raise ProjectError("custom: objects must be a list of {kind, name}")
        for o in raw:
            if not isinstance(o, dict) or not o.get("kind") or not o.get("name"):
                raise ProjectError(f"custom: each object needs a kind and a name, got {o!r}")
            raise ProjectError(f"{o['kind']} {str(o['name']).strip()!r} does not exist")
        out["objects"] = []
        return out

    def plan(self, params: dict) -> list[PlannedObject]:
        return []


class DefaultTemplate(Template):
    """The default project: every cell has exactly one (the store creates it), holding what no
    other project does. Nothing is planned in it."""
    key = "default"
    title = "Default"
    description = "Everything that is not in another project."
    PARAMS = {}
    hidden = True

    def plan(self, params: dict) -> list[PlannedObject]:
        return []


register(CustomTemplate())
register(DefaultTemplate())
