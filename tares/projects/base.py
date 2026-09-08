"""The template contract. A template is pure: `plan()` returns what should exist for given params and
has no side effects; the engine does the applying and the diffing."""
from __future__ import annotations

from dataclasses import dataclass, field

KINDS = ("source", "view", "trigger", "agent", "mcp_server")


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
        if self.kind not in KINDS:
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

    def summary(self, instance: dict, store) -> dict:
        """What the project page shows beyond the object list: the recent runs of its agents and
        when its triggers last fired. Works for any template because it only reads the instance's
        objects. Templates override to add `panels` (label/value tables the page renders on Setup,
        each {title, rows: [{label, value, url?, mono?}]}), `cards` (counters, each {label, value})
        and per-run decorations (`result_url`, `result_label`, `badges`); call super() first and
        extend what it returns."""
        objects = instance.get("objects") or []
        runs, total, ok = [], 0, 0
        for o in objects:
            if o["kind"] != "agent":
                continue
            n, n_ok = store.count_agent_runs(o["name"])
            total += n
            ok += n_ok
            for r in store.list_agent_runs(o["name"], limit=20):
                runs.append({"id": r.get("id"), "started_at": _iso(r.get("started_at")),
                             "key": r.get("key"), "agent": o["name"], "status": r.get("status"),
                             "rounds": r.get("rounds"), "max_rounds": r.get("max_rounds"),
                             "finding": r.get("finding"), "error": r.get("error")})
        runs.sort(key=lambda r: r["started_at"] or "", reverse=True)
        runs = runs[:20]
        triggers = []
        for o in objects:
            if o["kind"] != "trigger":
                continue
            triggers.append({"name": o["name"], "last_fired": _iso(store.last_fired_any(o["name"]))})
        fired = [t["last_fired"] for t in triggers if t["last_fired"]]
        return {"runs": runs, "triggers": triggers, "runs_total": total, "runs_ok": ok,
                "trigger_last_fired": max(fired) if fired else None}

    def after_create(self, instance: dict, store, runtime) -> None:
        """Optional hook the engine calls once after a successful create, with the runtime (None
        while the daemon boots from a catalog file). Best effort: an error here is logged on the
        instance and never undoes the create."""
        return None

    async def detect(self, store, runtime) -> dict:
        """Optional: look at the environment (running containers, reachable services) and propose
        parameter values. Returns {"params": {name: value}, "found": {name: "where it came from"},
        "missing": {name: "why not"}, "notes": [str]}; the wizard prefills what it can and says the
        rest. Default: nothing detected."""
        return {"params": {}, "found": {}, "missing": {}, "notes": []}

    def run_action(self, instance: dict, action: str, args: dict, store, runtime) -> dict:
        """Perform one of ACTIONS for a running instance; return what the page should show.
        Raise ProjectError for a bad action or arguments."""
        raise ProjectError(f"{self.key}: no action {action!r}")

    def describe(self) -> dict:
        return {"key": self.key, "title": self.title, "description": self.description,
                "params": self.PARAMS, "tags": list(self.tags), "setup": list(self.SETUP),
                "actions": list(self.ACTIONS), "guide": dict(self.guide) if self.guide else None,
                "sentence": self.sentence}


def _iso(v):
    if v is None:
        return None
    return v.isoformat() if hasattr(v, "isoformat") else str(v)
