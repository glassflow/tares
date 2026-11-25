"""A station's operating rules as its runtime reads them (TR-1598).

The rules have one source: the kit's skills (`claude-plugin/skills/factory-protocol`, `station-<role>` and
their pages). Claude Code reads those files itself, as skills, so for Claude Code the rendering of a file is
the file, byte for byte. A Codex station has no skills, no subagents, no schedule of its own, no slash
commands and no way to message another station, so for Codex the same files are rendered:

  * `claude-plugin/runtimes/codex/manual.toml` says, for each role that has a rendering, which files make
    its manual (`parts`) and which pages go with it (`pages`), and holds the lines the manual opens with;
  * next to it, `<skill>/<file>.toml` holds the edits of one source file: `[[edit]]` tables, each a passage
    of the source (`find`) and what a Codex station is told instead (`with`, which may be empty). A passage
    must be in the source exactly once. A source that changed under an edit stops the rendering with the
    passage named, so the two cannot drift apart unseen (tests/test_role_text.py renders every role);
  * what comes out names nothing of CLAUDE_ONLY. A rendering that still does is refused, at a start too.

The manual reaches a Codex station as part of its developer instructions (`{ROLE_MANUAL}` in the Codex
runtime's launch spec, filled by `tares.crew.launch`), so it stays with the station after a compaction and
nothing is written to the repository's AGENTS.md or to the person's `~/.codex`.

Another role gets its rendering by a `[roles.<role>]` table in manual.toml and an edits file for each of
its source files that names something of CLAUDE_ONLY. No code changes. A role with no table has no manual
on Codex yet: `manual` gives "".

Standard library only.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

CLAUDE_CODE = "claude_code"
CODEX = "codex"

# What only a Claude Code station has. None of these may be in what a Codex station is told (compared
# without regard to case): the schedule tools, station messages, subagents and their agent types, skills
# and their loading, slash commands, the plugin's tool prefix, the LSP tool, the challenger's switch.
CLAUDE_ONLY = ("CronCreate", "CronList", "CronDelete", "SendMessage", "subagent", "general-purpose", "`Explore`",
               "skill", "/tares:", "Claude Code", "mcp__plugin_tares_tares__", "LSP tool", "set_session_flow",
               "re-attached")

_FRONT = re.compile(r"\A---\n.*?\n---\n+", re.S)


class RenderError(Exception):
    """The kit's rules cannot be rendered for a runtime: a source file or an edits file is missing or broken,
    an edit's passage is not in the source exactly once, or the result names something of CLAUDE_ONLY."""


def _read(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, ValueError) as e:
        raise RenderError(f"cannot read {path}: {e}") from None


def _toml(path: Path) -> dict:
    try:
        return tomllib.loads(_read(path))
    except tomllib.TOMLDecodeError as e:
        raise RenderError(f"{path} is not valid TOML: {e}") from None


def runtime_dir(kit, runtime: str) -> Path:
    return Path(kit) / "runtimes" / runtime


def edits(kit, runtime: str, name: str) -> list:
    """The edits `runtime` makes to the skills file `name` (`station-builder/SKILL.md`), in order: a list of
    {find, with}. [] when the runtime has no edits file for it."""
    path = runtime_dir(kit, runtime) / f"{name}.toml"
    if not path.is_file():
        return []
    rows = _toml(path).get("edit")
    if not isinstance(rows, list) or not rows:
        raise RenderError(f"{path} holds no [[edit]]")
    for n, row in enumerate(rows, 1):
        if not isinstance(row, dict) or not isinstance(row.get("find"), str) or not row["find"] \
                or not isinstance(row.get("with"), str):
            raise RenderError(f"{path}: edit {n} needs a `find` passage and a `with` text")
    return rows


def named(text: str) -> list:
    """The names of CLAUDE_ONLY that `text` holds, in their order there."""
    low = text.lower()
    return [name for name in CLAUDE_ONLY if name.lower() in low]


def render_file(kit, runtime: str, name: str) -> str:
    """The skills file `name` as a station on `runtime` reads it. For Claude Code that is the file itself. For
    another runtime: without the front matter (it tells Claude Code when to load a skill), with the runtime's
    edits made, each passage found exactly once, and naming nothing of CLAUDE_ONLY. Raises RenderError."""
    text = _read(Path(kit) / "skills" / name)
    if runtime == CLAUDE_CODE:
        return text
    text = _FRONT.sub("", text, count=1)
    for n, row in enumerate(edits(kit, runtime, name), 1):
        found = text.count(row["find"])
        if found != 1:
            raise RenderError(
                f"the {runtime} wording of {name} is out of step with it: the passage of edit {n} "
                f"({row['find'].strip()[:70]!r}) is there {found} times, not once. Bring "
                f"runtimes/{runtime}/{name}.toml in line with the file.")
        text = text.replace(row["find"], row["with"])
    left = named(text)
    if left:
        raise RenderError(f"the {runtime} wording of {name} still names what only Claude Code has "
                          f"({', '.join(left)}): add an edit for it in runtimes/{runtime}/{name}.toml")
    return text


def roles(kit, runtime: str) -> dict:
    """{role: {parts, pages}} for every role that has a manual on `runtime`; {} for Claude Code, whose
    stations read the skills themselves, and for a runtime the kit has no folder for."""
    path = runtime_dir(kit, runtime) / "manual.toml"
    if runtime == CLAUDE_CODE or not path.is_file():
        return {}
    table = _toml(path).get("roles")
    if not isinstance(table, dict):
        raise RenderError(f"{path} names no role")
    for role, row in table.items():
        ok = isinstance(row, dict) and all(
            isinstance(row.get(key, []), list) and all(isinstance(x, str) and x for x in row.get(key, []))
            for key in ("parts", "pages"))
        if not ok or not row.get("parts"):
            raise RenderError(f"{path}: the role {role} needs `parts`, and `pages` when it has any, as lists of files")
    return table


def manual(kit, runtime: str, role: str) -> str:
    """The whole manual of a `role` station on `runtime`, as one text: the opening lines, each part, then
    each page under a heading that carries its file name (the parts name their pages by it). `role` is the
    role whose rules the station follows (its parent's, for a helper). "" for Claude Code and for a role
    that has no rendering on the runtime yet. Raises RenderError for a rendering that cannot be made."""
    row = roles(kit, runtime).get(role)
    if not row:
        return ""
    doc = _toml(runtime_dir(kit, runtime) / "manual.toml")
    out = []
    opening = doc.get("opening")
    if isinstance(opening, str) and opening.strip():
        out.append(opening.strip())
    out += [render_file(kit, runtime, name).strip() for name in row["parts"]]
    for name in row.get("pages") or []:
        out.append(f"# Page `{name.rsplit('/', 1)[-1]}`\n\n{render_file(kit, runtime, name).strip()}")
    text = "\n\n".join(out) + "\n"
    left = named(text)
    if left:
        raise RenderError(f"the {runtime} manual of the {role} station names what only Claude Code has "
                          f"({', '.join(left)}): see runtimes/{runtime}/manual.toml")
    return text
