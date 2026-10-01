"""The `custom` template: a project assembled by hand from objects that already exist, and the
`default` template every cell's default project is an instance of.

There is no planned version of anything. A custom project's plan is the explicit object list;
create takes the objects in (triggers, agents and MCP servers move out of the default project,
sources become members), edit adds or releases them, and a released object goes back to the
default project. Nothing is ever created on behalf of a custom project, so Repair and customized
do not apply. A custom project may start empty: the builder creates it first and adds objects as
they are made."""
from __future__ import annotations

from .base import KINDS, PlannedObject, ProjectError, Template
from .registry import register


class CustomTemplate(Template):
    key = "custom"
    title = "From existing objects"
    description = ("A project assembled from sources, triggers, agents and MCP servers that "
                   "already exist. Triggers, agents and MCP servers move into it; sources are "
                   "shared with the projects that already read them.")
    PARAMS = {"objects": {"type": "list",
                          "help": "the objects, each {kind, name}; kind is source, trigger, "
                                  "agent or mcp_server"}}
    hidden = True   # the console offers it next to the templates, not as one of them

    def validate(self, params: dict) -> dict:
        out = super().validate(params)
        raw = out.get("objects")
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            raise ProjectError("custom: objects must be a list of {kind, name}")
        seen, objects = set(), []
        for o in raw:
            if not isinstance(o, dict) or not o.get("kind") or not o.get("name"):
                raise ProjectError(f"custom: each object needs a kind and a name, got {o!r}")
            kind, name = str(o["kind"]), str(o["name"]).strip()
            if kind == "view":
                raise ProjectError("custom: views were removed; list the trigger instead")
            if kind not in KINDS:
                raise ProjectError(f"custom: unknown object kind {kind!r} "
                                   f"(one of {', '.join(KINDS)})")
            if (kind, name) in seen:
                continue
            seen.add((kind, name))
            objects.append({"kind": kind, "name": name})
        out["objects"] = objects
        return out

    def plan(self, params: dict) -> list[PlannedObject]:
        return [PlannedObject(o["kind"], f"{o['kind']}:{o['name']}", {"name": o["name"]})
                for o in params["objects"]]


class DefaultTemplate(Template):
    """The default project: every cell has exactly one (the store creates it), holding what no
    other project does. Its objects are placed in it, never planned, so its plan is empty."""
    key = "default"
    title = "Default"
    description = ("Everything that is not in another project: objects created without naming "
                   "a project, and objects a project let go of.")
    PARAMS = {}
    hidden = True

    def plan(self, params: dict) -> list[PlannedObject]:
        return []


register(CustomTemplate())
register(DefaultTemplate())
