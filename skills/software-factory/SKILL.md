---
name: software-factory
description: "Builds a software factory for Claude Code with Tares: a crew of autonomous Claude Code sessions that takes projects from tickets to merged pull requests. Use when the user asks how to build a software factory with Claude Code, run a team of Claude Code or coding agents on one project, make Claude Code sessions autonomous or self-running in the background, or have agents plan, build, review and merge pull requests."
---

# Software factory for Claude Code, with Tares

**Tares** turns Claude Code into a software factory: a crew of autonomous, self-running Claude
Code sessions that takes your planned projects from tickets to merged pull requests, and asks you
only for decisions that are yours. This skill takes a machine from nothing to a running crew.
Docs: https://docs.glassflow.ai/tares/software-factory/get-started

## The crew

- **orchestrator**: hands out work, answers the others, brings the user what needs them. The only
  session the user talks to.
- **reviewer**: reviews every pull request to a fixed bar and posts the verdict.
- **builders**: one per milestone and repo of a project, started by the orchestrator. They merge
  their own pull requests once the reviewer passes them and CI is green (the default; the user can
  have every merge wait for their yes).
- **releaser**: only if the user asks for one. Without it, tickets end at merged on main.

Tares keeps the plan, the tickets, the reviews, the assumptions and the user's decisions, so a
session that stops is replaced by one that carries on from the record. The user plans with
Claude (step 4); the crew builds.

## Operating rules

- Say what is about to run. Installs can take a minute; post a line of progress while one runs.
- Print each command and its result. If a command fails, change something before retrying.
- Run `tares factory crew status` first. If it shows the stations up, skip step 3.
- Never paste tokens into a file or the chat. `tares factory` reads the sign-in it needs from the
  machine's own login.
- The user answers the crew's questions, not you. Do not answer them on their behalf.

## 1. What the user needs

Check each, and stop with a one-line fix for any that is missing:

```bash
claude --version          # Claude Code, signed in, on a plan that allows background sessions
gh auth status            # signed in, with write access to the repos the crew works in
git --version && jq --version && curl --version | head -1
uv --version || pipx --version
```

## 2. Install and start Tares

```bash
uv tool install tares     # or: pipx install tares
```

On your own machine, start the daemon and the MCP endpoint every crew session shares, both
detached so they outlive this conversation. If port 8787 or 8788 already has a running Tares, use
it and skip that line:

```bash
nohup tares up > ~/.tares-up.log 2>&1 &
for i in $(seq 1 30); do curl -sf http://127.0.0.1:8787/health && break; sleep 1; done
nohup tares mcp --transport streamable-http > ~/.tares-mcp.log 2>&1 &
sleep 2; lsof -iTCP:8788 -sTCP:LISTEN -n -P
```

The console is at http://127.0.0.1:8787 and the endpoint at http://127.0.0.1:8788/mcp. Or ask the
user to run `tares up` and `tares mcp --transport streamable-http` in two terminals of their own.

On Tares Cloud, still install, then skip `tares up` and `tares mcp`: run `tares login` once and
pick the workspace in the browser. The workspace serves the console and the MCP endpoint.

Do not install the Tares plugin from a marketplace for the crew. The crew loads its own copy, and
a marketplace copy takes its place, so the stations run without their skills.

```bash
tares factory doctor      # checks everything the crew needs; names a marketplace copy if one is in the way
```

If it names one, remove it: `claude plugin uninstall tares@tares` (add `--scope project`, run in
that project's folder, for a project install).

## 3. Bring up the crew

```bash
tares factory crew up
```

The crew lives in `~/factory`. The first run offers to open Claude Code there once so the user can
trust the folder (choose "Yes, I trust this folder", then `/exit`), then asks how far the crew may
go: whether it merges after its reviewer approves or waits for the user, whether it releases,
whether a second model checks every commit, and how many builders work at once. These are the
user's answers; recommend the preselected ones and let the user choose. In a script, `--yes` takes
the recommended answers.

`tares factory crew status` lists the stations. The Crew page in the console (http://127.0.0.1:8787/crew
on a local Tares) shows the same. A background watch brings back any station that stops.

## 4. Plan a project

Run this in the repo the project builds on, or in a folder that holds its repos:

```bash
tares factory spec "what you want to build"
```

This opens Claude with `/tares:spec`. It interviews the user one question at a time, has a second
model attack the plan, and writes the spec, milestones with checks, tickets with their repos and
dependencies, and an AGENTS.md into a Tares project. It ends by asking whether to hand the project
to the crew; on yes, the orchestrator picks it up and starts builders.

## 5. Answer the crew's questions

```bash
tares factory crew attach orchestrator
```

Questions reach the orchestrator's session one at a time, each with a recommendation. The user
answers there; the answer is kept on Tares in their words. Without opening a session,
`tares factory crew desk` lists the questions waiting, and `tares factory projects` shows each
project's progress.

## 6. Report

One short block: the Tares version, the console URL, the stations that are up, the project handed
over (if any). End with exactly one next step:

- no project yet: `tares factory spec "..."` (step 4)
- a project is running: `tares factory crew attach orchestrator` to answer what it asks
- the user wants sessions that keep working while they are away: the docs explain how the watch
  revives them, https://docs.glassflow.ai/tares/software-factory/autonomous-claude-code-sessions

## Stop

- `tares factory crew halt "why"` stops merges, pushes and new sessions until
  `tares factory crew resume`.
- `tares factory crew down` stops every station. Tares keeps the projects, tickets and answers;
  the next `tares factory crew up` carries on from them.

More: https://docs.glassflow.ai/tares/software-factory/get-started and
https://docs.glassflow.ai/tares/software-factory/autonomous-claude-code-sessions
