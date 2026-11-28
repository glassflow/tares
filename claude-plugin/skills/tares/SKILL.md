---
name: tares
description: Use when working on a Tares project (its docs, milestones and tickets), when picking up a build a session stopped on, or when the user asks for a challenger session. Tares keeps the project; read and write it through the tares MCP tools.
---

# Tares

Tares keeps your projects: their docs, milestones, tickets and the sessions that built them. The
**tares** MCP server is connected (this plugin registers it), and this plugin streams the current
Claude Code session into Tares when session streaming is enabled, so each ticket shows the work
done on it. On a Software Factory cell, `what_needs_me`, `how_is_project`, `start_project` and
`hand_project_to_crew` are the place to start.

## Projects with docs and tickets

A project can hold docs (spec, plan, AGENTS.md, a starting prompt, notes, working docs),
milestones with acceptance checks, and a ticket list (in Tares, or synced from Linear or GitHub Issues) where each
ticket has a milestone and the tickets it depends on. To spec a new project, use the
`spec-project` skill. When told to work on a Tares project, start with its `start` doc
(`list_docs`, `get_doc`), then work one milestone at a time (`list_milestones`): the ready tickets
from `list_tickets`, in order, each read with `get_ticket`, which carries the working doc (a long one as its Goal, Steps and Progress, and `section="all"` gives all of it; if the tool refuses `section`, call it without: an older Tares sends the whole doc).
If a ticket is already in progress when you start, an earlier session stopped on it: call
`pick_up` first, and finish that ticket. While you build, keep a `## Progress` section at the end
of the ticket's working doc (done, next, how to run things), so the next session can pick up.
Record each check you run with `add_check` (it adds one line to the working doc), what you
learn about the project with `remember_for_project`, and, in a tares-factory crew, an assumption
you make so as not to block with `assume`.

Two docs belong to every project: the shared AGENTS.md (standing rules) and memory (facts that
hold everywhere). Read them with `read_global_docs`. When the user states a rule or fact that
holds beyond the current project, offer to add it with `add_to_global`.

## Challenger sessions

When the user asks to make this a challenger session (any wording: "challenger session",
"let Codex challenge this", "let Grok review my commits", "/tares:challenger"), call the `set_session_flow` tool with
`flow="challenger"` and say so in one sentence. Do not run the challenger yourself: the plugin's hooks run Codex, or Grok
when the `challenger_backend` option is `grok`, and each review names which. They
challenge the plan when you leave plan mode and every commit you make, and hand you the findings.
`set_session_flow` with an empty `flow` turns it off.
