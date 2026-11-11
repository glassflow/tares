# Tares

**Always-on AI agents, as a Software Factory or an AI SRE.**

Tares comes in two editions, and each Tares you create is one of them. As a **Software Factory**, a crew of Claude Code sessions takes your projects from a spec to pull requests merged on main. As an **AI SRE**, agents watch your services and tell you what broke and why. Both keep what they decide (plans, tickets, reviews, incident notes, your answers) for the next reader, human or agent.

[![PyPI](https://img.shields.io/pypi/v/tares)](https://pypi.org/project/tares/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-docs.glassflow.ai%2Ftares-orange)](https://docs.glassflow.ai/tares)
[![CI](https://github.com/glassflow/tares/actions/workflows/ci.yml/badge.svg)](https://github.com/glassflow/tares/actions/workflows/ci.yml)

<!-- TR-134: video thumbnail goes here -->

<!-- mcp-name: ai.glassflow/tares -->

## Software Factory

You plan a project with Claude Code. A crew of Claude Code sessions on your machine builds it: an orchestrator hands out the tickets, builders open pull requests, a reviewer holds every change to a fixed bar, and a change merges once it passes review and CI. You talk to one session, and only for the decisions that are yours.

- **Plan once**: a spec, milestones with acceptance checks, and tickets with a working doc each, kept on Tares, Linear or GitHub Issues.
- **Build in parallel**: one crew serves every project, with a builder per milestone and repo.
- **See it all in one place**: the console shows what waits for you, each project's progress, the tickets, the pull requests in review and the crew at work.
- **Nothing is lost**: tickets, reviews, the crew's calls and your answers live on Tares, so the crew carries on after a restart and you can check every choice it made.
- **Work as a team**: each person runs their own crew on the same Tares, with project leads, private projects and repo owners the spec asks instead of guessing.

```bash
uv tool install tares                         # or: brew install glassflow/tap/tares
tares login                                   # sign in to your Tares Cloud workspace (or run `tares up`)
tares factory crew up                         # start the crew; the first run asks how far it may go
tares factory spec "what you want to build"   # in your repo: plan the project and hand it over
```

Needs Claude Code (signed in, on a plan with background sessions) and `gh` with write access to your repos.

→ [Get started](https://docs.glassflow.ai/tares/software-factory/get-started), [The console](https://docs.glassflow.ai/tares/software-factory/console), [Teams](https://docs.glassflow.ai/tares/software-factory/teams), [A project, end to end](https://docs.glassflow.ai/tares/software-factory/a-project-end-to-end)

## AI SRE

Connect your logs, metrics, alerts, traces and deploys. Tares puts every event about a service on one timeline, a watcher checks every service on a schedule, and when something looks wrong an investigator writes an incident note: what is failing, the likely cause, the evidence and the next step. The note lands in Slack, where your team takes it, resolves it, or says whether it was worth raising.

```bash
uv tool install tares     # or: brew install glassflow/tap/tares
tares up                  # choose 2) AI SRE; console on http://127.0.0.1:8787
```

→ [Get started](https://docs.glassflow.ai/tares/ai-sre/get-started), [Tutorial: build an AI SRE](https://docs.glassflow.ai/tares/ai-sre/tutorial)

## Tares Cloud or your own machine

**[Tares Cloud](https://console.tares-glassflow.com/)** is the managed version: create a Software Factory or AI SRE workspace, and it is ready in about a minute. Team features are included.

On your own machine, `tares up` runs the same open source Tares: one daemon and one DuckDB file, with no external database, no broker and no telemetry. To run it on your private cloud for a team, use the enterprise version: write to [help@glassflow.ai](mailto:help@glassflow.ai).

## Why Tares

- **A record that outlives the session.** Plans, tickets, reviews, findings and decisions stay on Tares for the next reader.
- **Agents that run on events, not prompts.** Work continues while you are away, and you are asked only what is yours to decide.
- **One read instead of ten tool calls.** An agent asks about a service once and gets its correlated history across every source.
- **Bring your own agent.** Claude Code, Cursor, Codex or your own loop, over MCP. Any model provider for Tares's own agents.

## Learn more

| | |
|---|---|
| [Software Factory](https://docs.glassflow.ai/tares/software-factory) | the crew, planning, tickets, the console, teams and commands |
| [AI SRE](https://docs.glassflow.ai/tares/ai-sre) | connecting your data, watching, incidents, services and runbooks |
| [Platform](https://docs.glassflow.ai/tares/platform) | the engine underneath: connectors, timelines, triggers, agents and projects |
| [Deployment](https://docs.glassflow.ai/tares/reference/deployment) | Docker, servers, TLS and auth |
| [AI resources](https://docs.glassflow.ai/tares/platform/agents/ai-resources) | for coding assistants: Markdown endpoints and instructions to paste |

## Feedback

Bug reports and ideas are welcome via [GitHub issues](https://github.com/glassflow/tares/issues) or `help@glassflow.ai`.

## License

[MIT](LICENSE). This licence covers everything in this repository except directories named `ee`, which are under the licence in [ee/LICENSE](ee/LICENSE). Without an enterprise licence key, a self-hosted Tares runs for one person; see [Enterprise licence](https://docs.glassflow.ai/tares/enterprise).
