# Tares

**The open-source platform for always-on AI agents.**

Agents that run on what happens, not on what you ask. Connect anything that emits events. Tares puts every event on one timeline per thing, wakes an agent the moment it matters, and keeps the answer for the next reader. Works with Claude Code, Cursor and your own agents over MCP.

[![PyPI](https://img.shields.io/pypi/v/tares)](https://pypi.org/project/tares/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-docs.glassflow.ai%2Ftares-orange)](https://docs.glassflow.ai/tares)
[![CI](https://github.com/glassflow/tares/actions/workflows/ci.yml/badge.svg)](https://github.com/glassflow/tares/actions/workflows/ci.yml)

<!-- TR-134: video thumbnail goes here -->

Tares is MIT licensed, runs on your own machine or server, and starts with two commands. No external database, no broker, no telemetry.

**Documentation:** [docs.glassflow.ai/tares](https://docs.glassflow.ai/tares) for the quickstart, concepts, connectors, MCP setup and deployment.

## What teams build with it

- **An AI SRE.** An alert fires; the agent reads the service's whole timeline (logs, metrics, deploys, alerts) and writes the diagnosis onto it before anyone opens a dashboard. → [Build an AI SRE](https://docs.glassflow.ai/tares/guides/ai-sre)
- **Shared code context.** Commits land; when a change matters to the team, the agent opens a pull request against the shared context repository. → [Projects](https://docs.glassflow.ai/tares/projects)
- **Anything with a failure mode.** Failed jobs, sandbox runs, voice calls: each gets a timeline, a trigger on failure, and a finding that says what broke.
- **A better read path for the agents you have.** Claude Code, Cursor or your own loop asks one question and gets the correlated history of an entity instead of ten tool calls.

Pick a template under **Projects** in the console, answer a few questions, click Start; Tares creates the sources, view, trigger and agent, each on its own page and editable there.

## Who Tares is for

**Use Tares if**

- You run Prometheus, OTLP, GitHub, Vercel or Postgres and want Claude Code, Cursor or your own agent to answer "what happened to this service?" in one call.
- You want an agent woken by an alert, with the full context already in front of it.
- You want findings that persist for the next reader, human or agent.

**Skip Tares if**

- You have one source and no agent. Your existing dashboard is enough.
- You need long-term metrics storage. Tares is a correlation layer, not a time-series database.
- You need many writers on one instance. Tares runs one DuckDB writer per instance.

## How Tares compares

|  | Agent + one MCP server per tool | Observability vendor AI | Tares |
|---|---|---|---|
| One correlated read across sources | no, one call per tool | within that vendor's data | yes |
| Wakes an agent on a condition | no | vendor workflows | yes, any agent over MCP or webhook |
| Findings written back onto the timeline | no | no | yes |
| Runs locally with no external DB | depends on each server | no | yes, one DuckDB file |
| Works with any MCP client | yes | no | yes |
| Open source | mixed | no | MIT |
| You keep your alerting | yes | yes | yes |

Measured on the same incident with the same agent: 6 reads and 3 turns per diagnosis with one MCP server per tool, 1 read and 2 turns with Tares, 0 reads when the trigger pushes the timeline. Single runs, directional. Details and code in [tares-cookbooks](https://github.com/glassflow/tares-cookbooks/tree/main/cookbooks/01_sre_incident_response).

## Get running

```bash
uv tool install tares        # or: pipx install tares
tares up                     # daemon + console on http://127.0.0.1:8787
```

Or let your coding agent do it. In Claude Code, Codex or Cursor, paste:

> Run `npx skills add glassflow/tares --skill tares` and use the tares skill to install Tares and connect it to this agent.

The [skill](skills/tares/SKILL.md) installs Tares, starts it, adds a first source, connects the agent over MCP and shows one read.

Docker images and server deployment (TLS, auth) are covered in the [server deployment guide](https://docs.glassflow.ai/tares/deployment).

Prefer not to run it yourself? **[Tares Cloud](https://console.tares-glassflow.com/)** is the managed version: sign up, connect your sources, and get the same correlated timeline and MCP endpoint without operating anything.

## See it work

The fastest way to have something in the timeline is the [demo stack](demo/): a small stack (api-server, Prometheus, traffic) with fault injection, so you can break something on purpose and watch Tares catch it. No checkout needed, just two files:

```bash
curl -O https://raw.githubusercontent.com/glassflow/tares/main/demo/docker-compose.yml
docker compose up -d                      # start the stack to ingest from
```

In the console, open **Projects**, pick the **AI SRE demo** template, click Start: the setup page detects the running stack and creates the three sources, the correlated view, the trigger and the agent, and gives you a Cause an incident button. Or do the same from a file: stop the daemon from the previous step (Ctrl-C), then restart it seeded with the demo catalog:

```bash
curl -O https://raw.githubusercontent.com/glassflow/tares/main/demo/catalog.demo.yaml
TARES_CATALOG=catalog.demo.yaml tares up
```

(The catalog imports only while your catalog is still empty. Already added a source? Restart on a fresh data directory instead: `TARES_CATALOG=catalog.demo.yaml tares up --data-dir ~/tares-demo`.)

Open **Explore** and pick `api-server`: request logs, latency and error-rate metrics, and alerts: three sources merged into one time-ordered timeline. That timeline is exactly what an agent gets, so connect one next and break the demo on purpose.

Skip the demo? Add one of your own sources instead: **Sources → Add source** in the console. See the [list of supported connectors](https://docs.glassflow.ai/tares/connectors).

## Connect an agent over MCP

Tares serves its read/watch surface as an [MCP server for AI agents](https://docs.glassflow.ai/tares/agents). Run the MCP endpoint and point a client (Claude Code, Cursor, Claude Desktop, …) at it:

```bash
# 1) the MCP endpoint - a second process (or use the stdio transport and skip this)
tares mcp --transport streamable-http --port 8788 --taresd http://localhost:8787

# 2) connect Claude Code
claude mcp add --transport http tares http://localhost:8788/mcp
```

Running the demo? Now cause the incident, a 5xx storm, and give it ~30 seconds to be ingested:

```bash
curl -O https://raw.githubusercontent.com/glassflow/tares/main/demo/inject.sh && chmod +x inject.sh
./inject.sh error_spike
```

Then ask your agent:

> Use tares: what happened to api-server in the last 15 minutes?

The agent calls `read` and gets the incident correlated: the `HighErrorRate` alert Prometheus fired, the 5xx request logs, and the error-rate spike in one time-ordered response. It has nothing to stitch together across systems. (The `incident` trigger fires too, and the catalog ships a Tares agent that wakes on it and writes its diagnosis back as a **finding** on the timeline. Set `ANTHROPIC_API_KEY` first, or see [`demo/`](demo/). `./inject.sh clear` rolls the fault back.)

A built-in agent on a real incident. The prompt is the whole configuration, and the finding it writes is a structured incident note on the service's timeline:

![A built-in Tares agent: its prompt, its run, and the incident note it wrote back onto the timeline](.github/assets/tares-agent.png)

Other clients, stdio transport, and auth are covered in [connecting AI agents over MCP](https://docs.glassflow.ai/tares/agents).

## What you get

- **Connectors** for the systems you already run: Prometheus (metrics and alerts), Alertmanager, Docker logs, GitHub, Postgres, Vercel, OpenTelemetry (OTLP), any JSON API by polling, a generic webhook, reference documents, agent memory, and Claude Code sessions. Add sources at runtime from the console; a **Discover** step proposes the config for you where it can. → [Connectors](https://docs.glassflow.ai/tares/connectors)
- **Correlated reads**: `read(selector, window)` returns any entity's timeline across *all* sources with no setup; `query(view, …)` reads through a saved, narrowed view; agents `subscribe` to be pushed the timeline when a trigger fires. → [Reads, views, and triggers](https://docs.glassflow.ai/tares/concepts)
- **Tares agents**: attach a prompt to a trigger and Tares runs it in-process when the trigger fires. It reads the correlated timeline and writes a **finding** back onto the entity's timeline. Read-only: it concludes, it doesn't act. → [Tares agents](https://docs.glassflow.ai/tares/tares-agents)
- **Slack**: subscribe a channel to any trigger and every firing is posted there, retried, logged, and visible in the console like any other subscriber. Ask back from the channel with `/tares ask <question>`. → [Slack setup](https://docs.glassflow.ai/tares)
- **Console**: Sources (health + setup), **Explore** (pick an entity, read its timeline), Views & Triggers, **Agents**, and **Ask**, an in-console assistant over your data, summonable with ⌘K.
- **MCP tools**: `read`, `query`, `subscribe`, `catalog_list` / `catalog_describe`, `derive` (an agent authors its own view), `remember` (write observations back), and source-setup tools. → [MCP tools reference](https://docs.glassflow.ai/tares/agents)

## How it works

Sources bring events in, views join them per entity, triggers watch the views, and agents (yours over MCP, or Tares agents in-process) read the timeline and write findings back onto it. Underneath, Tares is a data plane: a single daemon (`taresd`) with a thin MCP proxy (`tares-mcp`), storing everything losslessly in one embedded DuckDB file, which is why there is no external database or broker to set up. The full design, including the ingest and trigger pipeline, is in the [architecture docs](https://docs.glassflow.ai/tares/concepts).

## Common questions

**Does my data leave my machine?** No. One local DuckDB file. The only outbound traffic is what the agents you configure send to their model provider.

**Do I need an Anthropic key?** Only for the built-in Tares agents and Ask. MCP reads need none.

**Is it read-only?** By default. Registering external MCP servers moves that boundary per agent, deliberately.

**Does it replace Prometheus, Grafana or Datadog?** No. It ingests what they emit and correlates it. Everything you run stays where it is.

**Does it replace my alerting?** No. Keep alerting where it is; Tares reacts to what fired.

**What about backups?** One DuckDB file in the data directory. Copy it.

**I am a coding assistant. Where do I start?** [AI resources](https://docs.glassflow.ai/tares/agents/ai-resources): Markdown endpoints, a task router, and instructions to paste.

## Feedback

Bug reports and ideas are very welcome via [GitHub issues](https://github.com/glassflow/tares/issues) or `help@glassflow.ai`. **No telemetry.** Tares collects and sends no usage data.

## License

[MIT](LICENSE).
