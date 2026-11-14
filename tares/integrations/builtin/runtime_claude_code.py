"""The Claude Code runtime (TR-867): how a crew station is started on Claude Code.

The cell builds the launch spec here, from what it knows (the rendered rules, the monitoring integration,
the crew's settings) and from what the `tares factory` command line tells it. The command line fills the
`{PLACEHOLDER}`s that only its machine knows, adds `--resume`, the boot prompt and the `claude` binary, and
starts the session. A later runtime is one more integration of the `runtime` slot.

The command line (`tares factory`, `tares.crew.launch.launch_plan`) asks for this spec and starts from it, with
no launch of its own: it fills the placeholders, adds `FACTORY_TARES` (the path of the `tares` program on that
machine, which only it knows), and keeps a copy for a start while the cell is down. `tares factory launch-plan`
prints the spec after that. Notes on the spec:
  * the machine's values are placeholders (see `runtime.PLACEHOLDERS`) until the command line fills them;
  * monitoring's `station_env` and `station_settings` are in the spec;
  * the settings are the settings file without its `env`, which is `spec.env`.
The file a station launches with is `{"env": spec.env, **spec.settings}`.

No secret is in a spec. The crew's key stays in its file (`{TOKEN_FILE}`); `TARES_AUTH_TOKEN` is blank; a
monitoring secret is never written: the settings name `otelHeadersHelper`, which fetches it at run time.
"""
from __future__ import annotations

import json
import re

from ...crew import launch as crew_launch
from ...crew.rules import fill
from ...crew.state import slugify
from ..runtime import PLACEHOLDERS, Runtime
from ..types import LaunchSpec

ROLES = ("orchestrator", "reviewer", "releaser", "builder", "helper")

_FILLED = re.compile(r"\{([A-Z_]+)\}")


def _text(ctx: dict, key: str, default: str = "") -> str:
    value = ctx.get(key)
    return default if value is None else str(value)


class RuntimeClaudeCode(Runtime):
    """Starts a station as a Claude Code background session (`claude --bg`).

    `launch_spec(role, station, ctx)`: `role` is orchestrator, reviewer, releaser, builder or helper; any
    other role is a ValueError. `ctx` is a dict of plain text (and the two objects marked); a key left out
    is "". Nothing in it is a secret. The keys:

      crew             the crew's name (FACTORY_CREW)
      orchestrator     the crew's orchestrator station; default `<crew>-orchestrator`
      parent           the station that started this one, "" for none
      parent_role      the role a helper works under (orchestrator, reviewer, releaser, builder); the rules
                       and the skill of a helper are its parent's. Needed for a helper, else a ValueError
      project          the project's name, "" for none
      project_id       the project's id (TARES_PROJECT); the header that carries it takes ASCII only
      repo             the project's repository folder as the settings file writes it (forward slashes on
                       Windows), "" for none
      human            the person's name, for the rule sentences
      authorized_at    when the person started the crew
      autonomy         the autonomy level recorded for the station, or its parent's; "" for the crew's
      crew_autonomy    the crew's autonomy level (L3-review or L4-ship); a ValueError when it is neither
      model, effort    the model and effort the station starts with
      autocompact      the --autocompact value ("auto" or a number as text)
      tares_url        the address the station reaches Tares at
      mcp_url          the address of the crew's MCP endpoint
      cluster          the release cluster the releaser's rule names
      token_file       true when the crew has a key file: the token variables then point at {TOKEN_FILE}
      rules            {allow, soft_deny}: the rule sentences as the guardrails integration renders them
                       for the station's work role, with their `{name}`s still in; a ValueError when empty
      monitoring       the active monitoring integration (an instance), or None; its `station_env` and
                       `station_settings` go into the spec

    Returns a LaunchSpec: `command` (the `claude` arguments up to and including --plugin-dir), `env`,
    `settings` and `placeholders` (each one the spec uses, with what it stands for)."""
    name = "claude_code"
    title = "Claude Code"
    ee = False
    settings_schema: list = []

    async def check_connection(self, settings: dict) -> tuple[bool, str]:
        return True, "Built in"

    def launch_spec(self, role: str, station: str, ctx: dict) -> LaunchSpec:
        if role not in ROLES:
            raise ValueError(f"the Claude Code runtime cannot run a {role or 'blank'} station; "
                             f"a role is one of {', '.join(ROLES)}")
        station = str(station or "").strip()
        if not station:
            raise ValueError("name the station")
        ctx = ctx or {}
        parent = _text(ctx, "parent")
        project = _text(ctx, "project")
        repo = _text(ctx, "repo")
        work_role = role
        if role == "helper":
            work_role = _text(ctx, "parent_role")
            if work_role not in ("orchestrator", "reviewer", "releaser", "builder"):
                raise ValueError("a helper works under its parent's role: give parent_role "
                                 "(orchestrator, reviewer, releaser or builder)")
        level = self._level(station, _text(ctx, "autonomy"), _text(ctx, "crew_autonomy"))

        env = self._env(station, role, parent, project, repo, ctx)
        monitoring = ctx.get("monitoring")
        extra_settings: dict = {}
        if monitoring is not None:
            env.update(monitoring.station_env({"station": station, "role": role, "project": project}))
            extra_settings = dict(monitoring.station_settings() or {})
        settings = self._settings(station, role, work_role, parent, project, repo, level, ctx)
        settings.update(extra_settings)

        command = ["--bg", "-n", station, "--model", _text(ctx, "model"), "--effort", _text(ctx, "effort"),
                   "--permission-mode", "auto", "--settings", "{SETTINGS_FILE}",
                   "--autocompact", _text(ctx, "autocompact"),
                   "--append-system-prompt", crew_launch.identity(role, station, parent, project, repo, work_role),
                   "--plugin-dir", "{PLUGIN_DIR}"]
        used = set(_FILLED.findall(json.dumps([command, env, settings])))
        return LaunchSpec(command=command, env=env, settings=settings,
                          placeholders={k: v for k, v in PLACEHOLDERS.items() if k in used})

    # ---- the parts ----

    @staticmethod
    def _level(station: str, own: str, crew: str) -> str:
        """The autonomy the station runs at: its own (or its parent's) level, else the crew's; a crew
        lowered below it takes it down."""
        if crew_launch.level_rank(crew) is None:
            raise ValueError(f"no valid crew autonomy (got {crew!r}); give crew_autonomy")
        level = own or crew
        if crew_launch.level_rank(level) is None:
            raise ValueError(f"no valid autonomy level for {station} (got {level!r})")
        if crew_launch.level_rank(crew) < crew_launch.level_rank(level):
            level = crew
        return level

    @staticmethod
    def _env(station: str, role: str, parent: str, project: str, repo: str, ctx: dict) -> dict:
        crew = _text(ctx, "crew")
        tares = _text(ctx, "tares_url")
        env = {
            "FACTORY_STATION": station, "FACTORY_ROLE": role, "FACTORY_PARENT": parent,
            "FACTORY_CREW": crew, "FACTORY_ORCHESTRATOR": _text(ctx, "orchestrator") or f"{crew}-orchestrator",
            "FACTORY_HOME": "{CREW_HOME}", "FACTORY_CREW_HOME": "{CREW_HOME}",
            "FACTORY_WORKTREES": f"{repo}/.factory/worktrees" if repo else "{CREW_HOME}/worktrees",
            "TARES_FACTORY_URL": tares, "TARESD_URL": tares, "TARES_FACTORY_MCP_URL": _text(ctx, "mcp_url"),
            "FACTORY_AUTOCOMPACT": _text(ctx, "autocompact"),
            # blank over any key Claude Code itself inherited: stations use the crew file
            "TARES_AUTH_TOKEN": "",
        }
        if ctx.get("token_file"):
            env["TARES_FACTORY_TOKEN_FILE"] = "{TOKEN_FILE}"
            env["TARES_FACTORY_CRED_FILE"] = "{TOKEN_FILE}"
        if project:
            env.update(TARES_PROJECT=_text(ctx, "project_id"), TARES_PROJECT_NAME=project,
                       FACTORY_PROJECT=slugify(project))
        if repo:
            env["FACTORY_REPO"] = repo
        return env

    @staticmethod
    def _settings(station: str, role: str, work_role: str, parent: str, project: str, repo: str, level: str,
                  ctx: dict) -> dict:
        rules = ctx.get("rules") or {}
        allow = [s for s in rules.get("allow") or [] if isinstance(s, str) and s.strip()]
        soft_deny = [s for s in rules.get("soft_deny") or [] if isinstance(s, str) and s.strip()]
        if not allow and not soft_deny:
            raise ValueError(f"no rules for {station}: a station never starts without rules; give rules")
        # what only this machine knows stays a placeholder; the rest is put in here
        values = {
            "human": _text(ctx, "human", "the user") or "the user",
            "tares": _text(ctx, "tares_url"), "cluster": _text(ctx, "cluster"),
            "home": "{CREW_HOME}", "stations_file": "{CREW_HOME}/stations.json",
            "repos": "{REPOS}", "wt_repos": "{WT_REPOS}",
            "where": (f"the repository of project {project}, checked out at {repo}" if repo else "{REPOS}"),
        }
        env_line = crew_launch.environment_sentence(
            role, station, parent, project, repo, _text(ctx, "human", "the user") or "the user",
            _text(ctx, "authorized_at"), level)
        return {
            "crossSessionInbound": "accept",
            "enabledPlugins": {"tares@inline": True},
            "statusLine": {"type": "command", "command": f"{crew_launch.STATUS_LINE} {{PLUGIN_DIR_SHELL}}",
                           "padding": 0},
            "worktree": {"bgIsolation": "none"},
            "permissions": crew_launch.permissions(role),
            "autoMode": {"environment": ["$defaults", env_line],
                         "allow": ["$defaults", *[fill(s, values) for s in allow]],
                         "soft_deny": ["$defaults", *[fill(s, values) for s in soft_deny]]},
        }

