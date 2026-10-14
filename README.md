# Tares

**The open-source platform for always-on AI agents.**

Most agents wait for a prompt and forget everything when the session ends. Tares gives agents a place to work from: events from your systems land on one timeline per thing, agents wake when something happens, and what they decide (findings, plans, tickets, reviews, your answers) is kept for the next reader, human or agent. Works with Claude Code, Cursor, Codex and your own agents over MCP.

[![PyPI](https://img.shields.io/pypi/v/tares)](https://pypi.org/project/tares/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-docs.glassflow.ai%2Ftares-orange)](https://docs.glassflow.ai/tares)
[![CI](https://github.com/glassflow/tares/actions/workflows/ci.yml/badge.svg)](https://github.com/glassflow/tares/actions/workflows/ci.yml)

<!-- TR-134: video thumbnail goes here -->

## What you can build with it

### Agents that watch your systems

Connect anything that emits events: logs, metrics, alerts, deploys, commits, database rows, webhooks. Tares merges them into one time-ordered timeline per service, repo, customer or job, and a trigger wakes an agent the moment something matters, with that timeline already in hand. The agent writes its conclusion back onto the timeline.

- **An AI SRE**: an alert fires, the agent reads the service's whole history and writes the diagnosis before anyone opens a dashboard.
- **Shared code context**: when a change matters, an agent updates the team's context repository with a pull request.
- **Anything with a failure mode**: failed jobs, sandbox runs, voice calls, each with a timeline, a trigger and a finding that says what broke.

→ [Build an AI SRE](https://docs.glassflow.ai/tares/guides/ai-sre), [Projects](https://docs.glassflow.ai/tares/projects)

### A software factory

One crew of Claude Code sessions on your machine builds every project you hand it. You plan a project with Claude; an orchestrator hands out the tickets, builders open pull requests, a reviewer holds every change to a fixed bar, and builders merge once it passes and CI is green. You talk to one session, and only for decisions that are yours.

- **Plan once**: a spec, milestones with acceptance checks, and tickets with a working doc each.
- **Build in parallel**: one crew serves every project, a builder per milestone and repo.
- **Nothing is lost**: tickets, reviews, the crew's assumptions and your answers live on Tares, so the crew carries on after a restart and you can check every choice it made.

→ [Software factory](https://docs.glassflow.ai/tares/software-factory/get-started), [A project, end to end](https://docs.glassflow.ai/tares/software-factory/a-project-end-to-end)

## Why Tares

- **One read instead of ten tool calls.** An agent asks about a thing once and gets its correlated history across every source.
- **Agents that run on events, not prompts.** Triggers wake your agent over MCP or a webhook, or run a Tares agent in-process.
- **A record that outlives the session.** Findings, plans, tickets, reviews and decisions stay on Tares for the next reader.
- **Bring your own agent.** Claude Code, Cursor, Codex or your own loop, over MCP. Any model provider for Tares's own agents.
- **Yours to run.** MIT licensed, one daemon and one DuckDB file on your machine or server. No external database, no broker, no telemetry.

## Quickstart

```bash
uv tool install tares                    # or: brew install glassflow/tap/tares
tares up                                 # daemon + console on http://127.0.0.1:8787
tares mcp --transport streamable-http    # in a second terminal: the MCP endpoint, :8788/mcp
```

Then pick what you came for.

**Give an agent your systems.** Add a source under **Sources → Add source** in the console (or start the [demo stack](demo/) to break something on purpose), and connect your agent:

```bash
claude mcp add --transport http tares http://localhost:8788/mcp
```

Ask it: *"Use tares: what happened to api-server in the last 15 minutes?"* → [Quickstart](https://docs.glassflow.ai/tares/quickstart)

**Build software with a crew.** Needs Claude Code (signed in, on a plan with background sessions) and `gh` with write access to your repos:

```bash
tares factory crew up                         # start the crew; the first run asks how far it may go
tares factory spec "what you want to build"   # in your repo: plan the project and hand it over
tares factory crew attach orchestrator        # answer the crew's questions
```

Watch it on the **Crew** page of the console. → [Software factory](https://docs.glassflow.ai/tares/software-factory/get-started)

Prefer not to run it yourself? **[Tares Cloud](https://console.tares-glassflow.com/)** is the managed version, with the MCP endpoint already running.

## Learn more

| | |
|---|---|
| [Concepts](https://docs.glassflow.ai/tares/concepts) | sources, timelines, triggers, agents and projects |
| [Connectors](https://docs.glassflow.ai/tares/connectors) | Prometheus, OTLP, GitHub, Vercel, Postgres, webhooks, Claude Code sessions and more |
| [Agents](https://docs.glassflow.ai/tares/agents) | connect any MCP client, or run Tares agents on a trigger |
| [Software factory](https://docs.glassflow.ai/tares/software-factory/get-started) | the crew, its commands, and a project from spec to merged |
| [Deployment](https://docs.glassflow.ai/tares/reference/deployment) | Docker, servers, TLS and auth |
| [AI resources](https://docs.glassflow.ai/tares/agents/ai-resources) | for coding assistants: Markdown endpoints and instructions to paste |

## Feedback

Bug reports and ideas are welcome via [GitHub issues](https://github.com/glassflow/tares/issues) or `help@glassflow.ai`.

## License

[MIT](LICENSE).
