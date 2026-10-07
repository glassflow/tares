# Tares plugin for Claude Code

One install wires Claude Code to Tares in **both directions**:

- **Read-back (MCP):** registers the `tares` MCP server so the agent can query your data plane
  (the same `tares-mcp` proxy the console hands out).
- **Capture (hooks):** ships this session's transcript into Tares's `claude_code` source as it
  progresses — via a small hook (`scripts/ship.py`) that POSTs new transcript lines to
  `{tares_url}/ingest/claude_code`.

Both wires are configured once on install (`tares_url`, optional auth `access_token`, and a
`stream_sessions` toggle). Challenger sessions (below) add `challenger_mode`, `codex_bin` and
`codex_sandbox`.

## Prerequisites

- `tares-mcp` on PATH (from `pip install tares`) — the MCP read-back proxy.
- `python3` on PATH — runs the shipper hook.
- A Tares daemon reachable at `tares_url`.

**No manual source setup.** The shipper creates the `claude_code` push source on Tares itself the
first time it runs (on `SessionStart`), and re-creates it if it's ever missing. Installing the plugin
is the only step.

## Try it (development)

```bash
claude --plugin-dir ./claude-plugin
```

Then start a session and do some work — new transcript lines flow to Tares on each prompt/tool use
and at session end. Check the console: **Entities** (keyed by `session`), or query the `claude_code`
source.

## Distribute (marketplace)

Put `marketplace.example.json` at a repo's `.claude-plugin/marketplace.json` (pointing at this plugin
dir), then users run:

```bash
/plugin marketplace add <owner>/<repo>
/plugin install tares@<marketplace-name>
```

They're prompted for `tares_url` + token on install; the token is stored in the OS keychain.

## Challenger sessions

Say "make this a challenger session" (or type `/tares:challenger`) at the start of a session.
Claude calls the `set_session_flow` MCP tool; the shipper sees that call in the transcript, marks
the session locally and on Tares, and from then on:

- when Claude leaves plan mode, the OpenAI Codex CLI on your laptop critiques the plan and Claude
  gets the findings before you see the plan (advisory; only `[P1]` counts as blocking on a plan);
- after every `git commit` Claude makes, Codex reviews the commit. `[P1]`/`[P2]` findings block
  Claude until it fixes and amends (strict, the default) or come back as context (`challenger_mode`
  = `advise`). Errors, timeouts and inconclusive reviews never block. Consecutive failed rounds are
  capped at 8 and the fix loop at 5 turn ends;
- every review lands on the session's timeline in Tares next to the transcript, and when the
  session ends Tares's challenger project writes the session summary with memory proposals.
  Accepted memory is handed to Claude at the next session start in that repo.

`/tares:challenger off` turns it off mid-session. `/tares:challenger-waive [n|all]` suppresses a
disputed finding. `touch .git/tares-challenger-skip` disables the hooks for a repository. State and
history live under `.git/` (`tares-challenger-*`), never in the working tree.

Needs `codex` on PATH (`npm install -g @openai/codex && codex login`) or the `codex_bin` option;
reviews are billed to your OpenAI account and Tares never calls Codex. If reviews come back
INCONCLUSIVE inside an already sandboxed session, set `codex_sandbox` to `danger-full-access`.

The mechanism follows [andreidavid/codex-review](https://github.com/andreidavid/codex-review)
(MIT): post-commit review, priority-tagged findings, the blocking fix loop, plan critique, state in
`.git/`. This plugin reimplements it in Python, gates it on the session mark, and records the
exchange in Tares.

## Spec a project

Type `/tares:spec <what you want to build>` (or ask Claude to spec a new project). Claude
brainstorms it with you first, then writes everything a later session needs to build it into a
new Tares project:

- the spec, the plan, the AGENTS.md of the build and a starting prompt (docs, kept in Tares)
- the tickets, in order, each with a working doc (context, steps, files, how to verify)

The tickets live in Tares, or in Linear when you want Linear: Claude creates them there with
Linear's own MCP tools, links the Linear project, and Tares keeps its ticket list in sync (it
polls about once a minute). Working docs stay in Tares either way. For Linear, connect it once in
the Tares console (Settings, Linear) and give this session Linear's MCP server.

The spec session's whole recording lands on the project. A later session told
`Work on Tares project "<name>"` reads the starting prompt, the tickets and their working docs
with `list_docs`, `get_doc`, `list_tickets` and `get_ticket`.

Creating projects and writing docs needs an admin key in `access_token` on a secured instance.

## How capture works

Every Claude Code hook receives `transcript_path` on stdin. `ship.py` reads the new bytes since a
per-session offset (kept in `${CLAUDE_PLUGIN_DATA}`), POSTs the complete new lines as NDJSON, and only
advances the offset on success (so failures retry). It runs on `UserPromptSubmit`, `PostToolUse`
(async, non-blocking) and `SessionEnd` (flush). Local *or* remote — it just posts to `tares_url`.

## Notes / caveats (prototype)

- **Push-only source.** The `claude_code` source is fed by this plugin over `/ingest` (`config.push:
  true`, set automatically on first run). Local file tailing was removed — the plugin covers local
  and remote alike. Capture is forward-only (sessions from install onward; no backfill of old files).
- Secrets are redacted **server-side** by the `claude_code` connector before storage (the PII guard).
- `sensitive` config (the token) is passed to hook/MCP processes only as
  `CLAUDE_PLUGIN_OPTION_access_token`; the URL is also available as `${user_config.tares_url}`.
