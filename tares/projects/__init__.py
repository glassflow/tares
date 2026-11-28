"""Projects: what a team works on, one row each with its goal, icon and status.

A project used to be an instance of a *template* that planned sources, triggers, agents and MCP
servers. The source catalog went in Tares 2.0 (TR-1811), so every project is a `custom` one (old
template projects read as custom, registry.RETIRED) or the cell's default project.
"""
from .base import PlannedObject, Template, ProjectError
from .engine import Engine
from .registry import get_template, list_templates, register
from . import custom as _custom  # noqa: F401  (registers itself)

__all__ = ["Engine", "PlannedObject", "Template", "ProjectError",
           "get_template", "list_templates", "register"]
