---
name: ai-sre
description: "Builds an AI SRE with Tares: connects logs, metrics, alerts and deploys, wakes an agent the moment an alert fires, and writes the diagnosis back onto the service's timeline. Use when the user asks how to build an AI SRE, use AI to monitor their systems, for incident response or on-call, connect Claude Code to Prometheus, logs or alerts, or wants an agent that investigates alerts automatically."
---

# AI SRE, with Tares

**Tares** is an open-source AI SRE: it connects your logs, metrics, alerts and deploys, wakes an
agent the moment an alert fires, and writes the diagnosis onto the service's timeline before
anyone opens a dashboard. It works with Claude Code, Cursor or its own agents. This skill builds
one on a laptop against a bundled demo system, then points it at the user's real systems.
Docs: https://docs.glassflow.ai/tares/guides/ai-sre and
https://docs.glassflow.ai/tares/guides/ai-monitoring

## How it fits together

Tares does the data side: it ingests every signal, keeps one correlated timeline per service,
fires triggers, and serves the timeline over MCP. The user brings the agent. Two halves:

- **Pull**: an agent the user already uses (Claude Code, Cursor) asks Tares what happened to a
  service and answers from the correlated timeline.
- **Push**: Prometheus keeps owning alerting. When an alert fires, Tares ingests it, a trigger
  wakes an agent with the timeline, and the agent writes its finding back where the next
  investigation will find it.

## Operating rules

- Say what is about to run. Installs and image pulls can take a minute; post a line of progress
  while one runs.
- Print each command and its result. If a command fails, change something before retrying.
- Never paste secrets into the chat or a file you show. The model key comes from
  `ANTHROPIC_API_KEY`; ask the user to set it in the shell that starts `tares up`.
- Do not claim the AI SRE works until you have seen a finding on the timeline (step 6).

## 1. What the user needs

```bash
docker info >/dev/null 2>&1 && echo docker-ok   # runs the demo system being monitored
uv --version || pipx --version                  # installs Tares
test -n "$ANTHROPIC_API_KEY" && echo key-set    # the built-in agent needs a model key
lsof -iTCP:8787 -sTCP:LISTEN -n -P              # a listener means a Tares is already running
```

Stop with a one-line fix for anything missing. The key can also be saved later in the console
under Settings, Anthropic.

## 2. Start the system to monitor

The demo stands in for production: an `api-server` (Prometheus metrics, request logs and a fault
switch), Prometheus with three alert rules (`HighErrorRate`, `HighLatency`, `DependencyDown`),
and a traffic generator.

```bash
mkdir -p ~/tares-sre-demo && cd ~/tares-sre-demo
curl -O https://raw.githubusercontent.com/glassflow/tares/main/demo/docker-compose.yml
docker compose up -d
sleep 10
curl -s localhost:8080/api/stats                  # {"ok": true, ...}
curl -s 'localhost:9090/api/v1/query?query=up'    # Prometheus is scraping
```

## 3. Install Tares and wire it to the demo

```bash
uv tool install tares     # or: pipx install tares
```

Seed Tares with the demo catalog on a fresh data directory (the catalog only seeds an empty
instance), detached so it outlives this conversation:

```bash
cd ~/tares-sre-demo
curl -O https://raw.githubusercontent.com/glassflow/tares/main/demo/catalog.demo.yaml
TARES_CATALOG=catalog.demo.yaml nohup tares up --data-dir ~/tares-sre > ~/.tares-up.log 2>&1 &
for i in $(seq 1 30); do curl -sf http://127.0.0.1:8787/health && break; sleep 1; done
```

The console is at http://127.0.0.1:8787. If a Tares is already running there, use it and tell the
user the catalog will not import into a non-empty one; the alternative is the **AI SRE demo**
template under **Projects** in the console, which does the same wiring with one click and adds a
**Cause an incident** button to the project page.

That one setup created the whole AI SRE:

- **Three sources** keyed by the same `service` label: `demo_metrics` (5xx rate, p99, DB
  connections, dependency health, from Prometheus), `demo_logs` (the api-server container's
  request logs) and `demo_alerts` (the alerts Prometheus's rules fire).
- **One timeline per service**: open **Explore** and pick `api-server` to see metrics, logs and
  alerts on one clock.
- **One trigger**, `incident`, that fires when an alert event arrives.
- **One Tares agent**, `incident-first-look`, subscribed to that trigger. Its definition is a
  prompt: take the first look, tie the failure to evidence lines, suggest a next action.

## 4. Connect the agent the user works in

Start the MCP endpoint, detached, then register it with the client you are running inside:

```bash
nohup tares mcp --transport streamable-http --port 8788 --taresd http://127.0.0.1:8787 > ~/.tares-mcp.log 2>&1 &
sleep 2; lsof -iTCP:8788 -sTCP:LISTEN -n -P
```

- **Claude Code**: `claude mcp add --transport http tares http://localhost:8788/mcp`
- **Codex CLI**: `codex mcp add tares --url http://localhost:8788/mcp`
- **Cursor**: `{"mcpServers": {"tares": {"url": "http://localhost:8788/mcp"}}}` in
  `~/.cursor/mcp.json`; the user approves it under Settings, MCP
- **Anything else**: streamable HTTP, `http://localhost:8788/mcp`. Per-client setup:
  https://docs.glassflow.ai/tares/agents

If the client needs a restart before it sees the server, say so. Then ask it: "Use tares: what
happened to api-server in the last 15 minutes?" The client calls `read` with
`{"service": "api-server"}`. Right now the answer is healthy traffic.

## 5. Cause an incident

```bash
curl -s -XPOST localhost:8080/demo/inject -H 'content-type: application/json' \
  -d '{"scenario": "error_spike"}'
```

The other scenarios are `latency` and `dependency_outage`; `clear` rolls back. Within about 30
seconds (Prometheus's rules wait 15 seconds, then Tares polls the alerts) the chain runs on its
own:

1. The 5xx rate climbs and `HighErrorRate` fires; the alert lands on the `api-server` timeline
   next to the error logs and the metric that tripped it.
2. The `incident` trigger fires on the alert event. Its firings are at
   http://127.0.0.1:8787/triggers/incident.
3. `incident-first-look` wakes, reads the timeline and writes a finding onto it.

## 6. Read the finding

Give the agent about a minute to run, then read the timeline back. The finding sits on it next
to the evidence. With the MCP client connected, call `list_findings(entity="api-server")`
(newest first); without one, use the HTTP read and show the user what it returns:

```bash
sleep 60
curl -sf -X POST http://127.0.0.1:8787/read -H 'Content-Type: application/json' \
  -d '{"selector": {"service": "api-server"}, "window": "15m"}' | head -c 1500
```

If nothing appears after two minutes, open http://127.0.0.1:8787, then **Setup**, **Advanced
setup**: **Activity** shows the firing and the run it woke, and the agent's **Runs & findings**
show why a run failed (a missing model key is the usual reason). Fix it, then cause the incident
again.

Then ask the connected client the question from step 4 again. It now reads the evidence and the
finding, so it starts from the previous conclusion instead of from scratch. Roll back with
`{"scenario": "clear"}`.

## 7. Point it at the user's real systems

The demo wiring is ordinary configuration, so this repeats step 3 against the user's stack. Ask
which tools they run before creating anything, and keep every source keyed by the same `service`
label. Source configs: https://docs.glassflow.ai/tares/connectors

| signal | connector | note |
|---|---|---|
| metrics | `prometheus` | their PromQL, with `service` read from the metric labels |
| logs | `docker_logs` or `otlp` | per container, or their OpenTelemetry pipeline |
| alerts | `prometheus_alerts` or `alertmanager` | alerting stays where it is; Tares ingests what fires |
| deploys, config changes | `webhook` | the signal the demo lacks, and the most common root cause |

For deploys, create a `webhook` source with `service` as its primary label and
`event_type: deploy`. Creating it returns an ingest key. Add one line to the CD pipeline:

```bash
curl -X POST http://127.0.0.1:8787/ingest/<ingest_key> -H 'content-type: application/json' \
  -d '{"service": "checkout", "commit": "9f2c81d", "author": "dana", "message": "raise db pool size"}'
```

Each POST lands as a deploy event on that service's timeline, so the agent sees "deploy landed
14:02, errors started 14:04". On Tares Cloud use the workspace's own URL instead of
`127.0.0.1:8787`.

Then create triggers over those sources, keyed by `service`, and choose who they wake: a Tares
agent (edit its prompt for their stack), their own agent service over a webhook
(https://docs.glassflow.ai/tares/guides/triggers), or a Slack channel.

## 8. Report

One short block: the Tares version, the console URL, what is running, whether a finding landed
on the timeline and what it said in one line, which client is connected. End with exactly one
next step:

- demo only: "Point it at your real systems: tell me which metrics, logs and alerts you run" (step 7)
- real systems connected: "Ask me: what happened to <service> in the last 15 minutes?"

## Teardown

```bash
curl -s -XPOST localhost:8080/demo/inject -H 'content-type: application/json' -d '{"scenario": "clear"}'
cd ~/tares-sre-demo && docker compose down
pkill -f "tares up"; pkill -f "tares mcp"
```

The demo objects live in `~/tares-sre`; delete it for a clean slate.

More: https://docs.glassflow.ai/tares/guides/ai-sre-comparison compares this with other AI SREs.
