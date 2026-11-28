# Tares

**A software factory: a crew of Claude Code or Codex sessions that takes your projects from a spec to pull requests merged on main.**

Tares keeps what the crew decides (plans, tickets, reviews, findings, your answers) for the next reader, human or agent.

[![PyPI](https://img.shields.io/pypi/v/tares)](https://pypi.org/project/tares/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-docs.glassflow.ai%2Ftares-orange)](https://docs.glassflow.ai/tares)
[![CI](https://github.com/glassflow/tares/actions/workflows/ci.yml/badge.svg)](https://github.com/glassflow/tares/actions/workflows/ci.yml)

<!-- TR-134: video thumbnail goes here -->

<!-- mcp-name: ai.glassflow/tares -->

## Software Factory

You plan a project with Claude Code or Codex. A crew of sessions on your machine builds it: an orchestrator hands out the tickets, builders open pull requests, a reviewer holds every change to a fixed bar, and a change merges once it passes review and CI. You talk to one session, and only for the decisions that are yours.

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

Needs Claude Code (signed in, on a plan with background sessions) or Codex 0.171.0 or newer (signed in; see [the Software Factory on Codex](docs/codex.md)), and `gh` with write access to your repos.

→ [Get started](https://docs.glassflow.ai/tares/software-factory/get-started), [The console](https://docs.glassflow.ai/tares/software-factory/console), [Teams](https://docs.glassflow.ai/tares/software-factory/teams), [A project, end to end](https://docs.glassflow.ai/tares/software-factory/a-project-end-to-end)

## Tares Cloud or your own machine

**[Tares Cloud](https://console.tares-glassflow.com/)** is the managed version: create a workspace, and it is ready in about a minute. Team features are included.

On your own machine, `tares up` runs the same open source Tares: one daemon and one SQLite file (or a Postgres database for a team), with no broker and no telemetry. To run it on your private cloud for a team, use the enterprise version: write to [help@glassflow.ai](mailto:help@glassflow.ai).

## Why Tares

- **A record that outlives the session.** Plans, tickets, reviews, findings and decisions stay on Tares for the next reader.
- **Work continues while you are away.** The crew picks up where it stopped after a restart, and you are asked only what is yours to decide.
- **Every choice can be checked.** Each ticket keeps its working doc, its checks, its reviews and the assumptions the crew made.
- **Bring your own runtime.** Claude Code or Codex, over MCP. Any model provider for the cell's summaries.

## Learn more

| | |
|---|---|
| [Software Factory](https://docs.glassflow.ai/tares/software-factory) | the crew, planning, tickets, the console, teams and commands |
| [Deployment](https://docs.glassflow.ai/tares/reference/deployment) | Docker, servers, TLS and auth |
| [AI resources](https://docs.glassflow.ai/tares/reference/ai-resources) | for coding assistants: Markdown endpoints and instructions to paste |

## Feedback

Bug reports and ideas are welcome via [GitHub issues](https://github.com/glassflow/tares/issues) or `help@glassflow.ai`.

## License

[MIT](LICENSE). This licence covers everything in this repository except directories named `ee`, which are under the licence in [ee/LICENSE](ee/LICENSE). Without an enterprise licence key, a self-hosted Tares runs for one person; see [Enterprise licence](https://docs.glassflow.ai/tares/enterprise).
