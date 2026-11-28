"""The template contract. A template is pure: `plan()` returns what should exist for given params and
has no side effects; the engine does the applying and the diffing."""
from __future__ import annotations

from dataclasses import dataclass, field

KINDS = ("source", "trigger", "agent", "mcp_server")
# what a template may plan: the object kinds plus skills (TR-332), which live in the project
# itself rather than in the catalog, so a hand-assembled project never lists one as an object
PLANNED_KINDS = KINDS + ("skill",)


class ProjectError(ValueError):
    pass


@dataclass
class PlannedObject:
    """One desired object. `kind` picks the catalog table; `key` is stable across re-plans for the
    same logical object (e.g. `source:owner/repo`), so a re-plan can tell "changed" from "new" and
    "removed"; `spec` is the object in catalog-import shape (the dict the YAML importer accepts,
    including `name`)."""
    kind: str
    key: str
    spec: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.kind not in PLANNED_KINDS:
            raise ProjectError(f"unknown planned object kind {self.kind!r}")
        if not self.key:
            raise ProjectError("planned object needs a key")
        if not self.spec.get("name"):
            raise ProjectError(f"planned {self.kind} {self.key!r} has no name in its spec")

    @property
    def name(self) -> str:
        return str(self.spec["name"])


class Template:
    """Subclass and register (see registry.py). PARAMS uses the connector CONFIG_SCHEMA style:
    {name: {type, required, default, help, ...}} so the console can render a form from it."""
    key: str = ""
    title: str = ""
    description: str = ""
    PARAMS: dict = {}
    # Free-form labels the console shows on the card ("demo" marks a project that needs the demo
    # stack rather than your systems).
    tags: tuple = ()
    # The goal a project made from this template starts with when its creator gives none: one
    # plain line saying what the project is for (the project page leads with it). "" = none.
    GOAL: str = ""
    # An optional walkthrough the console links next to the description: {label, url}.
    guide: dict | None = None
    # What a person would type to get this template, in the first person ("watch my checkout
    # logs and tell me in Slack when payments fail"). The landing screen offers it as a starter;
    # empty means the template is not offered there.
    sentence: str = ""
    # Steps a user must do outside Tares before Start (start a stack, export a key), each
    # {title, text?, command?}; the wizard shows them above the Start button.
    SETUP: list = []
    # Buttons the instance page offers, each {name, label, help?, params?: {name: {label, options}}};
    # the engine routes them to run_action().
    ACTIONS: list = []

    def validate(self, params: dict) -> dict:
        """Check required params and fill defaults; return the normalized params. Templates may
        override for cross-field rules; call super() first."""
        out = dict(params or {})
        for name, spec in (self.PARAMS or {}).items():
            if spec.get("required") and out.get(name) in (None, "", [], {}):
                raise ProjectError(f"{self.key}: missing required parameter {name!r}")
            if name not in out and "default" in spec:
                out[name] = spec["default"]
        return out

    def preflight(self, params: dict, store) -> None:
        """Optional checks that need the store (a referenced credential exists, ...), run by the
        engine after validate() and before plan() on create and update. Raise ProjectError."""
        return None

    def plan(self, params: dict) -> list[PlannedObject]:
        raise NotImplementedError

    def describe(self) -> dict:
        return {"key": self.key, "title": self.title, "description": self.description,
                "params": self.PARAMS, "tags": list(self.tags), "setup": list(self.SETUP),
                "actions": list(self.ACTIONS), "guide": dict(self.guide) if self.guide else None,
                "sentence": self.sentence, "goal": self.GOAL or None}

