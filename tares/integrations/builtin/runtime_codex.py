"""The Codex runtime (TR-1592): how a crew station is started on Codex.

A station on Codex is one `codex app-server` of its own. It runs detached and listens on a private Unix
socket in the crew home, so it outlives whatever talks to it (TR-1576 measured this on Codex 0.171.0). The
cell builds the launch spec here; the `tares factory` command line fills the `{PLACEHOLDER}`s only its
machine knows, checks the Codex version, and starts the process (`tares.crew.codex`). The spec:

  command   the `codex` arguments: the app-server on `{STATION_SOCKET}`, with what the person's account
            would bring along switched off (see LEAN_FLAGS);
  env       the station's environment: the same FACTORY_* and TARES_* variables a Claude Code station gets
            (they reach the station's hooks and commands from the app-server's own environment), and
            CODEX_HOME, the station's own Codex folder;
  settings  what the station's conversation is started with. Codex takes these per thread (`thread/start`),
            so nothing is written to the repository or to the person's `~/.codex`:
              thread       the `thread/start` parameters: model, approval policy, sandbox, the identity
                           text with `{ROLE_MANUAL}` after it (the station's operating rules, which the
                           crew renders from its kit: a Codex station reads no skills), and `config`
                           (the guard's hook, for every tool call and for the start of every turn, and
                           the thread's trust for it);
              effort       the reasoning effort of a turn;
              mcp_servers  the MCP servers the station's conversation is started with, as Codex takes
                           them (`config.mcp_servers`), and the only ones it may load: a station that loads
                           another is stopped at its start. The one server is the crew's Tares endpoint
                           (see "The Tares MCP server" below);
              mcp_check    one read the crew asks of that server, as the station, before it hands the
                           station a prompt: {server, tool, arguments};
              rules        {allow, soft_deny}: the rule sentences of the station's work role;
              autonomy     the level the station runs at.

What a station does not load (the rule, not a list of names): nothing of the person's own Codex setup.
CODEX_HOME is a folder of the station's own, so no plugin, MCP server, notify program, skill, rule file,
memory or trusted-project record of the person's is read; the folder holds a link to their login and nothing
else of theirs. The account brings connectors and remote plugins into any Codex folder, so those three
features are off. In TR-1576 a notify program of a personal plugin was started at the end of each turn and
was handed the prompt; with this spec nothing of the kind is configured.

The guard (TR-1594) is `tares hook codex-guard`: the Claude plugin's guard.py asked by
`tares.crew.codex_guard`, which also closes a command's standard input and leaves a record of every run.
It is registered twice, for `PreToolUse` (every tool call) and for `UserPromptSubmit` (the start of every
turn, where its record is the proof, before the model is asked, that the thread runs its hooks). The crew
adds one thing to `config` when it starts the conversation, since only its machine knows the folder: the
station's folder is marked not trusted for the thread, so the repository's own `.codex/config.toml` cannot
add a hook or an MCP server to the station (`tares.crew.codex_station.hand`).

The Tares MCP server (TR-1596) is the endpoint a Claude Code station reaches through the plugin's
`.mcp.json`, with the same five identity headers, each read by Codex from the station's environment
(`env_http_headers`; one whose variable is empty or unset is left off, measured on 0.171.0). The crew's key
is added the way the plugin adds it: by the headers helper, `tares hook mcp-headers`, which reads the key
file and prints the `Authorization` header. Codex runs the helper through a shell with an environment of its
own (HOME, PATH and a few more; none of the station's variables), so the helper is told the file on its
command line, as `TARES_FACTORY_CRED_FILE=<file>`. The key is then in no environment, argument list or
config: the spec and the conversation's settings hold the file's path. `required` makes Codex refuse to
start or join the conversation when the server does not answer its first request: an endpoint that is
down, a helper that fails, no key at an endpoint that wants one. It does not catch a wrong key: the Tares
endpoint takes any bearer at the first request and passes it to the cell with each call, and the cell's
refusal comes back as a tool's text ("error 401: ..."), not as a failed call. So the crew asks one read
itself before every prompt (`mcp_check`, see `tares.crew.codex_station`). A crew with no key file (a cell
with no login) gets no helper. The server's tools are approved as a set (`default_tools_approval_mode`):
with the conversation's approval policy Codex would stop every call to Tares on a question to the person
(measured: a first turn that calls `builder_brief` waits for an approval and never ends).

Not here yet: recording (TR-1595), the role instructions (TR-1598) and monitoring. The approval policy and the sandbox are the tightest pair TR-1576 ran a guarded command
under; whether a builder can work in them is for TR-1598 to find.

No secret is in a spec.
"""
from __future__ import annotations

import json
import re

from ...crew.rules import fill
from ..runtime import PLACEHOLDERS, Runtime
from ..types import LaunchSpec
from .runtime_claude_code import ROLES, RuntimeClaudeCode, _text, rule_values

# What the person's account would bring into a station: its connectors (the `apps` feature, one MCP
# server with every connected app's tools, write tools among them) and the plugins installed from the
# account (`plugins`, `remote_plugin`). Measured on 0.171.0 in a folder that held only the login: 248
# tools and 8 plugins with the features on, none with them off.
LEAN_FLAGS = ("--disable", "apps", "--disable", "plugins", "--disable", "remote_plugin")

GUARD_HOOK = "tares hook codex-guard --plugin-root {PLUGIN_DIR_SHELL}"
GUARD_TIMEOUT = 5  # seconds, as claude-plugin/hooks/hooks.json gives the guard

# The identity headers a station's MCP calls carry, and the variable of its environment each is read from.
MCP_IDENTITY = {"X-Tares-Station": "FACTORY_STATION", "X-Tares-Role": "FACTORY_ROLE",
                "X-Tares-Parent": "FACTORY_PARENT", "X-Tares-Crew": "FACTORY_CREW",
                "X-Tares-Project": "TARES_PROJECT", "X-Tares-Runtime": "FACTORY_RUNTIME"}
MCP_SERVER = "tares"
# The plugin's headers helper, told the crew's key file on its own command line: Codex gives a helper none
# of the station's environment.
MCP_HELPER = "TARES_FACTORY_CRED_FILE={TOKEN_FILE_SHELL} tares hook mcp-headers --plugin-root {PLUGIN_DIR_SHELL}"

_FILLED = re.compile(r"\{([A-Z_]+)\}")


def identity(role: str, station: str, parent: str, project: str, repo: str, work_role: str) -> str:
    """The text a station's conversation starts under: who it is. Pure."""
    text = f"You are a tares-factory station: role {role}, session name {station}"
    if project:
        text += f", project {project}, repo {repo}"
    if parent:
        text += f", started by {parent}"
    text += "."
    if role == "helper":
        text += f" You work under the {work_role} role and follow its rules."
    return text + " Act only within your role."


class RuntimeCodex(Runtime):
    """Starts a station as a Codex app-server of its own (`codex app-server --listen unix://...`).

    `launch_spec(role, station, ctx)` takes the roles and the `ctx` of the Claude Code runtime (see
    RuntimeClaudeCode); `autocompact` and `monitoring` are not used. It raises ValueError for the same
    things: a role that is not one, a blank station, a helper without its parent's role, an autonomy level
    that is not one, and no rules.

    Returns a LaunchSpec: `command`, `env`, `settings` (see the module's note) and `placeholders` (each one
    the spec uses, with what it stands for)."""
    name = "codex"
    title = "Codex"
    ee = False
    settings_schema: list = []

    async def check_connection(self, settings: dict) -> tuple[bool, str]:
        return True, "Built in"

    def launch_spec(self, role: str, station: str, ctx: dict) -> LaunchSpec:
        if role not in ROLES:
            raise ValueError(f"the Codex runtime cannot run a {role or 'blank'} station; "
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
        level = RuntimeClaudeCode._level(station, _text(ctx, "autonomy"), _text(ctx, "crew_autonomy"))
        mcp_url = _text(ctx, "mcp_url")
        if not mcp_url:
            raise ValueError(f"no MCP address for {station}: a Codex station does not start without the crew's "
                             "Tares MCP endpoint; give mcp_url")

        rules = ctx.get("rules") or {}
        allow = [s for s in rules.get("allow") or [] if isinstance(s, str) and s.strip()]
        soft_deny = [s for s in rules.get("soft_deny") or [] if isinstance(s, str) and s.strip()]
        if not allow and not soft_deny:
            raise ValueError(f"no rules for {station}: a station never starts without rules; give rules")
        values = rule_values(ctx, project, repo)

        env = RuntimeClaudeCode._env(station, role, parent, project, repo, ctx)
        env.pop("FACTORY_AUTOCOMPACT", None)   # Claude Code's --autocompact; Codex compacts by itself
        env["CODEX_HOME"] = "{CODEX_HOME}"
        env["FACTORY_RUNTIME"] = "codex"   # the station's runtime, sent with each MCP call (X-Tares-Runtime)

        guard = {"type": "command", "command": GUARD_HOOK, "timeout": GUARD_TIMEOUT}
        thread = {
            "approvalPolicy": "on-request",
            "sandbox": "workspace-write",
            # who the station is, then its operating rules: the kit's skills as a Codex station reads them
            # (tares.crew.role_text), which only the machine that holds the kit can render
            "developerInstructions": identity(role, station, parent, project, repo, work_role) + "{ROLE_MANUAL}",
            "config": {
                "hooks": {
                    # an empty matcher is every tool; the guard ignores what it has no rule for
                    "PreToolUse": [{"matcher": "", "hooks": [dict(guard)]}],
                    # the start of every turn: the guard's record of it is the proof that it is on
                    "UserPromptSubmit": [{"hooks": [dict(guard)]}],
                },
                # a hook Codex was not told to trust is skipped without an error (TR-1576)
                "bypass_hook_trust": True,
                # the station writes its report (`tares factory report`) in the crew home, which is not the
                # workspace of a station that works in a repository (a builder, a helper): without this every
                # write of it would wait for the person's approval (TR-1598). For such a station only the
                # reports are added. A crew-wide station (orchestrator, reviewer, releaser) is started in the
                # crew home itself, so its sandbox reaches all of it already. A .git is never writable to a
                # sandboxed command, so a fetch, a commit or a push still asks for approval.
                "sandbox_workspace_write": {"writable_roots": ["{CREW_HOME}/reports"]},
            },
        }
        if _text(ctx, "model"):
            thread["model"] = _text(ctx, "model")
        tares = {"url": mcp_url, "env_http_headers": dict(MCP_IDENTITY), "required": True,
                 # Codex asks the person before each call of an MCP tool unless told otherwise, and a station
                 # has nobody to ask. A call to Tares passes the guard, and the cell checks who may do what.
                 "default_tools_approval_mode": "approve"}
        if ctx.get("token_file"):
            tares["http_headers_helper"] = MCP_HELPER
        settings = {
            "thread": thread,
            "mcp_servers": {MCP_SERVER: tares},
            # a read the cell answers for any key it takes, on a cell with no station yet too
            "mcp_check": {"server": MCP_SERVER, "tool": "list_crew", "arguments": {"mine": True}},
            "rules": {"allow": [fill(s, values) for s in allow], "soft_deny": [fill(s, values) for s in soft_deny]},
            "autonomy": level,
        }
        if _text(ctx, "effort"):
            settings["effort"] = _text(ctx, "effort")

        command = ["app-server", "--listen", "unix://{STATION_SOCKET}", *LEAN_FLAGS]
        used = set(_FILLED.findall(json.dumps([command, env, settings])))
        return LaunchSpec(command=command, env=env, settings=settings,
                          placeholders={k: v for k, v in PLACEHOLDERS.items() if k in used})
