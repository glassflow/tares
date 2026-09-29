# Tares demo

A self-contained stack to see Tares in action. It stands up a small **`api-server`** (Prometheus
metrics, request logs, and a `/demo/inject` fault switch) plus **Prometheus** and a **traffic generator** — the
*upstream systems Tares ingests from*. Tares itself is installed separately (below) and reads
from this stack, exactly as it would read from your real systems.

```
 demo stack (docker)                                Tares (on your machine)

 traffic-generator ──▶ api-server ◄── scrape ── Prometheus
                        :8080                    :9090
                          │                        │
                          │ container logs         │ metrics + fired alerts
                          └───────────┬────────────┘
                                      ▼
                                    taresd ──▶ one correlated timeline + triggers
```

Two files, no checkout needed: `docker-compose.yml` (the stack) and `catalog.demo.yaml` (Tares's
view of it). The commands below curl them; from a checkout, run them from `demo/` instead.

## 1. Start the stack

```bash
curl -O https://raw.githubusercontent.com/glassflow/tares/main/demo/docker-compose.yml
docker compose up -d              # api-server + prometheus + traffic
```

Give it ~10s, then check it's alive:

```bash
curl -s localhost:8080/api/stats            # {"ok": true, ...}
curl -s 'localhost:9090/api/v1/query?query=up'   # prometheus is scraping
```

## 2. Install and run Tares

Two ways to wire Tares to the stack: one click in the console, or the catalog file.

**In the console:** start Tares (`tares up`), open **Projects**, pick the **AI SRE demo** template (tagged
demo), click Start. That creates the same three sources, view, trigger and agent the catalog below
does, and the project page gets a "Cause an incident" button so you never need `curl` for step 4.

**With the catalog file:**

Install Tares the normal way (not in Docker) and point it at the demo catalog — from any
directory:

```bash
uv tool install tares          # or: pipx install tares  (or from source: uv pip install -e .)

# seed the demo sources/views/triggers/agent and start the daemon + console
curl -O https://raw.githubusercontent.com/glassflow/tares/main/demo/catalog.demo.yaml
export ANTHROPIC_API_KEY=sk-ant-…     # so the shipped Tares agent can run (or set one later in the console)
TARES_CATALOG=catalog.demo.yaml tares up
```

The console opens at http://127.0.0.1:8787. (`tares up` imports the catalog while the catalog is
still empty; been running Tares already? Restart on a fresh `--data-dir`.)

The catalog also ships a **Tares agent** (`incident-first-look`) wired to the `incident` trigger —
so the loop closes with nothing to deploy. It needs an Anthropic key (the `ANTHROPIC_API_KEY` above,
or set one in the console → Settings); without a key it stays enabled but each run is logged as
"no key" and no finding is written.

## 3. Look around

- **Explore** — pick the `api-server` entity and watch metrics, logs, and the alerts Prometheus
  fires merge into one time-ordered timeline. Flip **Agent view** to see the exact read an agent
  gets over MCP.
- **Triggers**: the `incident` trigger watches the three demo sources together and fires when
  Prometheus fires an alert, pushing the whole correlated timeline to a subscribed agent.

Prometheus owns alerting here (the demo ships three rules — `HighErrorRate`, `HighLatency`,
`DependencyDown`); Tares ingests the fired alerts (`prometheus_alerts`), correlates them, and wakes
the agent to **diagnose** — it doesn't re-implement the thresholds.

## 4. Cause an incident

Flip a fault:

```bash
curl -s -XPOST localhost:8080/demo/inject -H 'content-type: application/json' \
  -d '{"scenario": "error_spike"}'
```

(`inject.sh` in this directory wraps the same call: `./inject.sh error_spike`.)

- `error_spike` — 5xx storm → Prometheus fires `HighErrorRate`
- `latency` — p99 > 1s → Prometheus fires `HighLatency`
- `dependency_outage` — a dependency goes down → Prometheus fires `DependencyDown`
- `clear` — roll back; the alerts resolve (a `resolved` event lands in the timeline)

Give it ~30s (the rules have a 15s `for:`, then Tares polls the alert). The alert lands in
**Explore** (the timeline turns red) next to the metric that tripped it and the error logs, and the
`incident` trigger fires. The shipped **`incident-first-look` agent** wakes on that firing, reads the
correlated timeline, and writes its diagnosis back as a **finding** on api-server's timeline (watch
it appear in Explore, or under **Agents → `incident-first-look` → Runs & findings**). Any external
agent you've subscribed is woken by the same firing.

Prefer to drive it yourself? Connect an agent over MCP (see
[connecting AI agents](https://docs.glassflow.ai/tares/agents), or the
[root README](../README.md#connect-an-agent-over-mcp) for the two commands) and ask
*"what's wrong with api-server?"*. It diagnoses from the same one correlated read.

## 5. Stop

```bash
docker compose down              # from the directory with docker-compose.yml
```

Stop Tares itself with Ctrl-C. Its data lives in `~/.tares` (or the `--data-dir` you passed);
delete that directory for a clean slate next time.

## Files
- `docker-compose.yml` — the whole stack, self-contained (Prometheus config inlined).
- `docker-compose.build.yml` — override to build `api-server` from source instead of pulling.
- `api-server/` — the monitored app (`app.py`): metrics, logs, and the `/demo/inject` fault switch.
  Published as `ghcr.io/glassflow/tares-demo-api-server`.
- `catalog.demo.yaml` — Tares's view of the stack (sources, views, trigger, and the shipped agent).
- `inject.sh` — cause/clear an incident.
