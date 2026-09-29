---
name: tares
description: Installs Tares, the open-source platform for always-on AI agents, starts the daemon, adds a first source over the HTTP API, connects the agent it runs inside over MCP, and proves the connection with one read. Use when the user asks an agent to install or set up Tares, add a source, connect Claude Code, Codex or Cursor to Tares, join one Tares project with a project key, or fix a local Tares install.
---

# Tares

Install and connect **Tares** (docs: https://docs.glassflow.ai/tares). Tares puts every event
from a user's systems on one timeline per thing, wakes an agent when a trigger fires, and keeps
the finding for the next reader. This skill takes a machine from nothing to: daemon running, one
source receiving, this agent connected over MCP, one successful `read`.

## Operating rules

- Act autonomously once the user asks for Tares to be installed. Ask only for what you cannot
  infer: which source to add first, and its one or two parameters (see step 4).
- Say what is about to run and that it may take a while. `uv tool install` can take a minute;
  post a line of progress while it runs so the user does not kill it.
- Print each command and its result. If a command fails, change something before retrying.
- Never paste secrets into the console, the chat, or a file you show. Read them from environment
  variables (`GITHUB_TOKEN`, `ANTHROPIC_API_KEY`). A variable exported only in your shell is not
  seen by a `tares up` started elsewhere; start the daemon from the shell that has it, or tell
  the user to set it where the daemon runs.
- Do not create what already exists: `GET /api/sources` first, and skip a source whose name is
  taken.
- Everything below goes through the HTTP API at `http://127.0.0.1:8787`, not the console. The
  console is for the user afterwards.

## 1. Prerequisites

```bash
uv --version || pipx --version        # one of the two; stop and tell the user how to install uv if neither
lsof -iTCP:8787 -sTCP:LISTEN -n -P    # empty = free; a listener here means a Tares is already running (step 3)
lsof -iTCP:8788 -sTCP:LISTEN -n -P    # same, for the MCP endpoint
docker info >/dev/null 2>&1 && echo docker-ok   # only matters if the user wants the demo stack
```

## 2. Install

```bash
tares --version 2>/dev/null && uv tool upgrade tares || uv tool install tares
# pipx: pipx install tares   (or pipx upgrade tares)
tares --version
```

If `tares` is not on PATH after installing, `uv tool update-shell` (or add `~/.local/bin` to PATH)
and open a new shell.

## 3. Start the daemon

Pick a data directory (default `~/.tares`; use another one if the user wants an isolated
install). Run detached so the session survives this conversation:

```bash
nohup tares up --data-dir ~/.tares > ~/.tares-up.log 2>&1 &
for i in $(seq 1 30); do curl -sf http://127.0.0.1:8787/health && break; sleep 1; done
```

`/health` returns `{"status":"ok","auth_required":false,"sources":[...]}`. Print the console URL:
`http://127.0.0.1:8787`. To stop later: `pkill -f "tares up"` (or `kill` the PID from
`lsof -iTCP:8787`). To restart: the same `nohup` line.

If port 8787 was already taken by a running Tares, skip this step and use it.

## 4. Add the first source

Ask the user which one, then `POST /api/sources` with the exact body below. `GET /api/sources`
first; if the name exists, move on to step 5. Every body has the same shape:
`{"name", "connector", "poll", "config"}`. Full config keys per connector:
https://docs.glassflow.ai/tares/connectors. Do not invent keys.

**Prometheus** (ask for the URL, one PromQL, and the label that names the service):

```bash
curl -sf -X POST http://127.0.0.1:8787/api/sources -H 'Content-Type: application/json' -d '{
  "name": "metrics", "connector": "prometheus", "poll": "10s",
  "config": {
    "url": "http://localhost:9090",
    "queries": [{"promql": "rate(http_requests_total[1m])", "by_name": true}],
    "labels": [
      {"name": "service", "field": "metric.service", "primary": true},
      {"name": "value", "field": "value", "type": "number"}
    ]
  }}'
```

**OTLP** (nothing to ask; print the endpoint the collector should point at):

```bash
curl -sf -X POST http://127.0.0.1:8787/api/sources -H 'Content-Type: application/json' -d '{
  "name": "otel", "connector": "otlp",
  "config": {"labels": [{"name": "service", "field": "service", "primary": true}]}}'
```

Then tell the user: `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:8787` (OTLP/HTTP, paths
`/v1/logs`, `/v1/traces`, `/v1/metrics`).

**Docker logs** (ask for the container name; `docker ps` lists them):

```bash
curl -sf -X POST http://127.0.0.1:8787/api/sources -H 'Content-Type: application/json' -d '{
  "name": "logs", "connector": "docker_logs", "poll": "5s",
  "config": {
    "container": "<container name>",
    "labels": [{"name": "service", "const": "<service name>", "primary": true}]
  }}'
```

**GitHub** (ask for `owner/repo`; the token comes from `GITHUB_TOKEN`, needed for private repos):

```bash
curl -sf -X POST http://127.0.0.1:8787/api/sources -H 'Content-Type: application/json' -d "{
  \"name\": \"commits\", \"connector\": \"github\", \"poll\": \"30s\",
  \"config\": {
    \"repo\": \"<owner/repo>\",
    \"token\": \"${GITHUB_TOKEN:-}\",
    \"labels\": [{\"name\": \"repo\", \"field\": \"repo\", \"primary\": true},
                 {\"name\": \"author\", \"field\": \"author\"}]
  }}"
```

Drop the `token` line for a public repo without a token. Never echo the body with the token in it.

**Webhook** (nothing to ask; create it and print the ingest URL):

```bash
curl -sf -X POST http://127.0.0.1:8787/api/sources -H 'Content-Type: application/json' -d '{
  "name": "events", "connector": "webhook",
  "config": {
    "event_type_field": "action",
    "text_template": "{action} by {sender}",
    "labels": [{"name": "pipeline", "field": "pipeline", "primary": true}]
  }}'
```

The response carries `ingest_key`. Print `POST http://127.0.0.1:8787/ingest/<ingest_key>` with a
JSON body, and send one test event yourself:

```bash
curl -sf -X POST http://127.0.0.1:8787/ingest/<ingest_key> -H 'Content-Type: application/json' \
  -d '{"action": "setup", "sender": "tares-skill", "pipeline": "setup"}'
```

## 5. Verify the source is receiving

```bash
for i in $(seq 1 30); do
  n=$(curl -sf http://127.0.0.1:8787/api/sources/<name> | python3 -c 'import sys,json; h=json.load(sys.stdin).get("health") or {}; print(h.get("events_total") or 0)')
  [ "$n" -gt 0 ] && break; sleep 2
done
curl -sf http://127.0.0.1:8787/api/sources/<name> | python3 -c 'import sys,json; h=json.load(sys.stdin).get("health") or {}; print("status", h.get("status"), "events", h.get("events_total"), "last_error", h.get("last_error"))'
```

Report what it saw: the event count, or the `last_error` if the count stayed 0 after 60 seconds
(then fix the config and retry; see troubleshooting.md).

## 6. Connect this agent over MCP

Start the MCP endpoint, detached:

```bash
nohup tares mcp --transport streamable-http --port 8788 --taresd http://127.0.0.1:8787 > ~/.tares-mcp.log 2>&1 &
sleep 2; lsof -iTCP:8788 -sTCP:LISTEN -n -P
```

Then register it with the client you are running inside:

- **Claude Code**: `claude mcp add --transport http tares http://localhost:8788/mcp`
- **Codex CLI**: `codex mcp add tares --url http://localhost:8788/mcp` (keep `--url`; a bare URL
  registers a stdio server)
- **Cursor**: write `~/.cursor/mcp.json` (or `.cursor/mcp.json` in the project):
  `{"mcpServers": {"tares": {"url": "http://localhost:8788/mcp"}}}` and tell the user to approve
  it under Settings > MCP
- **Claude Desktop**: the same JSON in `claude_desktop_config.json`, then restart the app
- **Anything else**: transport streamable HTTP, URL `http://localhost:8788/mcp`, no auth on a
  local instance. Details: https://docs.glassflow.ai/tares/agents

## 7. Prove it

Call the `read` tool once for the entity the new source produced, for example
`read(selector={"service": "<service name>"}, window="15m")` (for GitHub the key is `repo`, for
the webhook `pipeline`). Show the user the result. If the client needs a restart before new MCP
servers are visible, say so and show the equivalent HTTP call instead:

```bash
curl -sf -X POST http://127.0.0.1:8787/read -H 'Content-Type: application/json' \
  -d '{"selector": {"service": "<service name>"}, "window": "15m"}' | head -c 800
```

## Joining one project

When the user wants this agent to work inside one Tares project (be woken by its triggers, read
its sources, follow its skills, and leave findings next to the Tares agents' ones):

1. Get a project key. The user makes it on the project page, Setup, Keys (or an admin calls
   `POST /api/projects/<id>/keys {"name": "claude-code"}`). The key's name is the name your
   findings carry. It reads that project only and records findings in it; nothing else.
2. Connect with the key. Over HTTP the client presents it:
   `claude mcp add --transport http tares http://localhost:8788/mcp --header "Authorization: Bearer <key>"`.
   Over stdio, spawn `tares-mcp` with `TARES_AUTH_TOKEN=<key>` in its environment. Never print
   the key.
3. With a key the project is implied. On an instance without auth there are no keys: pass
   `project` (its name or id) to each tool below; it may be left out while there is only one.
4. `list_skills`, then `get_skill` for any whose description matches the task, before relying on
   it.
5. `read` (the project's sources and findings), `stats` (what moved), `list_findings` (what is
   already known about the entity), `project_timeline` (what happened lately).
6. `record_finding(entity, finding, verdict)` when you conclude: the entity is the thing the
   finding is about (a service, a repo), the finding is markdown, the verdict one lowercase word.
7. To be woken by the project's triggers, `join_project(url)` with a webhook URL your agent
   listens on. Every firing of every trigger in the project is POSTed there, with `project`,
   `dispatch_id`, `key` (the entity), `payload` (the timeline) and `run_ids`.

## 8. Report

One short block: what was installed (version), the data dir, the console URL, how to stop and
start, which source was added and how many events it has, which client is connected. End with
exactly one next step:

- one source so far: add a second one keyed by the same label so reads correlate
  (https://docs.glassflow.ai/tares/connectors)
- the user has Docker and no real source yet: run the demo stack and the AI SRE guide
  (https://docs.glassflow.ai/tares/guides/ai-sre)
- otherwise: "Ask me: what happened to <entity> in the last 15 minutes?"

## Troubleshooting

See [troubleshooting.md](troubleshooting.md).
