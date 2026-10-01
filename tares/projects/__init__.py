"""Projects: an opinionated entry point on top of the ordinary Tares objects.

A *template* is code (a Template subclass) that declares parameters and turns them into a plan of
sources, triggers, agents and MCP servers in catalog shape. An *instance* is one configured
run of a template, stored in the DB, that owns the objects it created. Owned objects are ordinary:
the user sees and edits them on their normal pages; the project page is a representation of them,
not a lock on them.
"""
from .base import PlannedObject, Template, ProjectError
from .engine import Engine
from .registry import get_template, list_templates, register
from . import shared_code_context as _shared_code_context  # noqa: F401  (registers itself)
from . import ai_sre_demo as _ai_sre_demo  # noqa: F401  (registers itself)
from . import challenger_workflow as _challenger_workflow  # noqa: F401  (registers itself)
from . import custom as _custom  # noqa: F401  (registers itself)
from . import rius_rca as _rius_rca  # noqa: F401  (registers itself)

__all__ = ["Engine", "PlannedObject", "Template", "ProjectError",
           "get_template", "list_templates", "register"]
