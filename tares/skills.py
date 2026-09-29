"""Skills (TR-332, TR-333): instructions a project's agents load when a task calls for them.

A skill is a name, a one-paragraph description and a markdown body, stored per project. An agent
whose project has skills sees the names and descriptions in its system prompt and gets a `skill`
tool that returns a body; it pays for a body only when it loads one. An agent in a project with
no skills gets exactly the prompt and tools it had before. Ask never gets skills.

This module holds what the API, the catalog importer, the template engine and the agent runner
share: validation, SKILL.md parsing, and the prompt section and tool.
"""
from __future__ import annotations

import re

import yaml

NAME_RE = re.compile(r"^[a-z0-9-]{1,64}$")
MAX_DESCRIPTION = 1024
MAX_BODY_BYTES = 64 * 1024

TOOL = "skill"
TOOL_DEF = {
    "name": TOOL,
    "description": ("Load one of this project's skills by name. Returns its instructions in "
                    "full; follow them for the task they describe."),
    "input_schema": {"type": "object", "properties": {
        "name": {"type": "string", "description": "the skill's name, from the list in your "
                                                  "instructions"}},
        "required": ["name"]},
}


class SkillError(ValueError):
    pass


def validate(name, description, body) -> tuple[str, str, str]:
    """The skill's fields, trimmed, or a SkillError saying what is wrong in plain words."""
    name = str(name or "").strip()
    if not NAME_RE.match(name):
        raise SkillError("name must be 1 to 64 lowercase letters, digits or dashes, "
                         f"got {name!r}")
    description = str(description or "").strip()
    if not description:
        raise SkillError(f"skill {name!r}: a description is required; the agent reads it to "
                         "decide when to load the skill")
    if len(description) > MAX_DESCRIPTION:
        raise SkillError(f"skill {name!r}: the description is {len(description)} characters, "
                         f"the limit is {MAX_DESCRIPTION}")
    if re.search(r"\n\s*\n", description):
        raise SkillError(f"skill {name!r}: the description must be one paragraph; put the "
                         "detail in the body")
    body = str(body or "").strip("\n")
    if not body.strip():
        raise SkillError(f"skill {name!r}: the body is empty")
    size = len(body.encode("utf-8"))
    if size > MAX_BODY_BYTES:
        raise SkillError(f"skill {name!r}: the body is {size} bytes, the limit is "
                         f"{MAX_BODY_BYTES} (64 KB)")
    return name, description, body


def parse_skill_md(text: str) -> tuple[str, str, str]:
    """A SKILL.md file: YAML front matter with `name` and `description` between `---` lines,
    then the body. Other front matter keys are ignored. Returns validated (name, description,
    body); raises SkillError with a plain message."""
    text = (text or "").lstrip("﻿").replace("\r\n", "\n")
    if not text.startswith("---\n"):
        raise SkillError("a SKILL.md starts with front matter: a line with ---, then name: and "
                         "description:, then another ---")
    end = text.find("\n---", 4)
    if end < 0 or text[end + 4:end + 5] not in ("", "\n"):
        raise SkillError("the front matter is not closed: add a line with --- after "
                         "description:")
    try:
        meta = yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError as e:
        raise SkillError(f"the front matter is not valid YAML: {e}".split("\n")[0]) from e
    if not isinstance(meta, dict):
        raise SkillError("the front matter must be name: and description: lines")
    for field in ("name", "description"):
        if not isinstance(meta.get(field), str) or not meta[field].strip():
            raise SkillError(f"the front matter has no {field}:")
    return validate(meta["name"], meta["description"], text[end + 4:])


def prompt_section(skills: list[dict]) -> str:
    """What follows the agent's own prompt when its project has skills."""
    lines = "\n".join(f"- {s['name']}: {' '.join(s['description'].split())}" for s in skills)
    return ("\n\n## Skills available in this project\n\n" + lines + "\n\n"
            f"Load a skill with the `{TOOL}` tool before relying on it, and only when the task "
            "matches its description.")


def load(store, project: str, name, available: list[str]) -> str:
    """The body the `skill` tool returns; an unknown name is an error that lists the names."""
    name = str(name or "").strip()
    skill = store.get_skill(project, name) if name else None
    if skill is None:
        raise ValueError(f"no skill named {name!r}; the skills are: {', '.join(available)}")
    return skill["body"]
