"""Templates register by key. Built-in templates import this module and call register() at import
time; tests register their own. Keep the registry a plain dict so it is inspectable."""
from __future__ import annotations

from .base import Template, ProjectError

_TEMPLATES: dict[str, Template] = {}


def register(template: Template) -> Template:
    if not template.key:
        raise ProjectError("a template needs a key")
    _TEMPLATES[template.key] = template
    return template


def unregister(key: str) -> None:
    _TEMPLATES.pop(key, None)


# Templates Tares no longer ships, read as the template that stands in for them, so a project made
# from one still opens, changes, pauses and resumes (the AI SRE demo went with the AI SRE edition,
# TR-1809; the challenger workflow went with triggers and agents, TR-1810; shared code context and
# Rius RCA went with the source catalog, TR-1811).
RETIRED = {"ai_sre_demo": "custom", "challenger_workflow": "custom",
           "shared_code_context": "custom", "rius_rca": "custom"}


def get_template(key: str) -> Template:
    try:
        return _TEMPLATES[RETIRED.get(key, key)]
    except KeyError:
        raise ProjectError(f"unknown template {key!r} (available: {', '.join(sorted(_TEMPLATES))})")


def list_templates() -> list[Template]:
    """The templates a user picks from; `hidden` ones (custom) are reachable by key only."""
    return [_TEMPLATES[k] for k in sorted(_TEMPLATES) if not getattr(_TEMPLATES[k], "hidden", False)]
