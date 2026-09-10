# Changelog

Notable changes to Tares (formerly NavFlow). Format follows [Keep a Changelog](https://keepachangelog.com/);
the project follows [Semantic Versioning](https://semver.org/).

## [1.29.1] - 2026-09-10

### Fixed
- The write-back reported the wrong delivery id when more alerts for the same service arrived
  while the agent was running. The `key` is now resolved when the run starts, so it names the
  firing that woke the agent, and the body carries that firing's labels as `labels` so a receiver
  can check the attribution instead of filing the report by `key` alone (TR-294).

## [1.29.0] - 2026-09-08

### Added
- Storage is measured against the volume the database sits on when no `TARES_MAX_DB_SIZE` is
  set, so a grown volume shows on the next mount with nothing to refresh; `/api/usage` says
  which (`max_bytes_source`). At 95% of the limit (`TARES_INGEST_PAUSE_PCT`) ingest is refused
  with 507 and poll connectors wait, with the reason on the source's health, `/health` and the
  Overview, instead of the database filling its disk and dying. Reads, agents and the console
  keep working; ingest resumes on its own once there is room.
- A run records whether its finding reached the write-back webhook: delivered, an HTTP status,
  or failed with the last error after the retries. The agent page shows it beside the run.
- Agents can report a label's value as the write-back's `key` (`webhook_key_label`, "report as
  key" on the form) instead of the entity, for a system that files reports by its own id.
- Project page: the Firings tab filters by outcome, trigger and entity.

### Changed
- The `rius_rca` template keys by service: `service` is the primary label, `delivery_id` and
  the new `rule` are labels, the trigger cools down for 5 minutes per service, and the callback
  reports the delivery id as `key` through `webhook_key_label`.
- Project page: an agent's configuration is one folded row, so its runs are in view.

## [1.28.0] - 2026-09-08

### Added
- Model access through an LLM gateway. Tares agents and Ask can talk to any server that speaks
  the Anthropic Messages format (LiteLLM, Portkey, a proxy in front of Bedrock or Vertex).
  From the environment with `ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN`, the names Claude
  Code uses, with `TARES_ANTHROPIC_BASE` still read as the old name; or from Settings, Model
  access, stored on the instance and winning over the environment, so a cloud customer with a
  mandated proxy configures it on their own cell. The gateway token is sent as a bearer header
  and never returned; a stored key is sent as the Anthropic key header. The Settings tab is now
  called Model access and leads with where model calls go. Thanks to KurochkaR for the
  environment half.

## [1.27.0] - 2026-09-08

### Added
- `http_poll` connector: polls any JSON endpoint on a schedule and stores one event per item, for
  the APIs no other connector covers (a weather service, a status page, a SaaS export, your own
  API). GET or POST with a body, extra headers, one credential header stored as a secret, a
  payload key into the reply, the webhook connector's mapping (event type, summary, event time)
  and a deduplication key with a bounded cursor. Nested values are reachable by dotted name in
  labels, the fields profile, view filters and trigger fields. A poll under 10 seconds is refused;
  a 429 or 5xx backs the source off up to an hour, honouring Retry-After, and the source's health
  says so. Discover fetches once and proposes the payload key, fields, labels, time and
  deduplication fields and a summary line.
- The source form for it reads in order: method, URL, credentials yes/no, additional headers as
  key-value rows, body for POST. The mapping appears only after Discover, as decisions already
  made with the sample value beside each, under a "What Tares fetched" panel showing the reply.
- Connector schema features any connector can use: `choices` on a string field (a dropdown,
  validated on save), a `map` type (key-value rows), `format: "url"` (checked on save; now on
  Prometheus, Prometheus alerts and Loki too) and `label` for the words the form shows.
- The builder proposes `http_poll` for goals that read a public or third-party API instead of a
  webhook plus a script; the landing lights it up for api, endpoint and weather.

### Changed
- Test and save results on the source form sit above the buttons, not at the top of the page. A
  required field the click found empty turns red and scrolls into view.

## [1.26.0] - 2026-09-07

### Added
- The running builder is two columns that fill the window: the conversation on the left with
  the answer box pinned under it, and a "To do" column on the right holding every card the
  assistant proposed, in order. A decided card folds to one line; the pointer the chat leaves
  for each card opens it. A one-line head carries the steps, the project name and the goal.
- Overview shows the Projects panel with a Create new button also on an instance with no
  project yet.

### Changed
- The builder no longer invents a trigger for the agent. The daemon refuses an agent proposal
  whose trigger does not exist and hands the model the real list; Continue on the views and
  triggers step waits until a trigger exists; the step names sources that have no events yet,
  since views and triggers are grounded in real events; the agent card never prefills an
  unknown trigger.
- Landing: the starter sentences keep a fixed order instead of reordering while you type, the
  picked one is marked, and the call to action reads "Build it".
- The assistant shows a readable line for a non-JSON error reply (a proxy's 502 page) instead of
  its HTML.

### Fixed
- Behind an nginx ingress, a builder step sent about five seconds after the previous one could
  fail with 502 "upstream prematurely closed connection": the proxy reused a pooled connection
  uvicorn was closing at that moment, and does not retry a POST. The daemon now keeps idle
  connections for 75 seconds, longer than the proxy's pool.

## [1.25.0] - 2026-09-04

### Added
- AI-guided project builder. Describe what you need; the assistant proposes sources from the
  installed connectors, then views and triggers, then an agent, one step at a time, asking
  before it guesses. Each proposal is the object's own form, prefilled, with the fields only you
  can fill (tokens, URLs, the Slack channel) marked. Every Apply creates a real object through
  its normal API and adds it to an ordinary project, so leaving part way keeps what was created.
  `POST /api/agent/chat` takes `mode: "build"` and `step`; each step gets only its own proposal
  tools. A goal that matches a template is proposed as that template, as its own form.
- A new landing screen. A cell with no project of its own opens on one question, "What do you
  want to build?", with starter sentences from the templates, a paste-your-last-incident door,
  and the demo as a one-click offer instead of a seeded project. The same screen is Create new
  under Projects; the template gallery sits behind a "Use a guided template" button. Each
  template declares its sentence (`sentence` in `describe()`); `GET /api/projects/templates/{key}`
  serves one template, hidden ones included.
- Project page: source rows fold open to the ingest endpoint or the polled target, with
  Configure. Framed empty states for runs, firings and events.

### Changed
- Deleting a source or a view no longer refuses with "remove it from those views first". The
  dialog lists what depends on it (views, triggers, agents) and, on yes, deletes them too, in
  order. `DELETE /api/sources/{name}` and `/api/views/{name}` take `cascade=true`;
  `GET /api/catalog/dependents?kind=&name=` lists what would go.
- Deleting a hand-assembled project (including one the builder made) lists its objects with
  Select all or individual picks; the chosen ones are deleted with the project, the rest stay.
  Picking a source pulls in what depends on it. `DELETE /api/projects/{uid}` takes
  `delete=kind:name,...`.

## [1.24.0] - 2026-09-04

### Added
- Runs on an agent's page and on a project's Agents tab can be filtered to successful or failed
  runs only, and "Show more" pages past the first fifty. `GET /api/agents/builtin/{name}/runs`
  takes `status` and `offset`.

### Fixed
- Edit on a project built from a template without its own wizard (Rius RCA among them) opened the
  create form and said "this instance has no project named rius_rca". The form now loads the
  template by key, hidden templates included, prefills the project's params (secrets blank,
  blank keeps the stored value) and saves in place.

### Changed
- The root span of a traced run carries `tares.instance` and `tares.agent` as span attributes,
  not only on the resource, so a backend's span view shows which instance sent it.

## [1.23.0] - 2026-09-03

### Added
- **Agent tracing.** Every Tares agent run and every Ask turn can be exported as an
  OpenTelemetry trace: a root span for the run, one span per model call (model, messages,
  tokens, stop reason) and one per tool call, in the OpenInference and gen_ai conventions the
  GenAI observability backends read. Rius is a preset (an API key is enough); any OTLP/HTTP
  endpoint works with the `otlp` provider (Langfuse, Phoenix, a collector). Each agent is its
  own service (`<instance>/<agent>`), so a backend groups per agent out of the box. Settings,
  Observability holds the switch and the provider; `TARES_TRACING_*` sets the same from the
  environment, and a console value wins. Off unless switched on.

## [1.22.0] - 2026-09-03

### Added
- **Pause a project with its sources.** Pause asks whether to pause the project's sources too,
  so nothing accumulates while it is paused (the AI SRE demo's traffic, for one). Resume brings
  back exactly the sources pause stopped; one paused by hand before stays paused. The API takes
  `{"sources": true}` on the pause endpoint.
- **The GitHub source form uses the tokens the cell already holds.** A stored token is picked
  from a dropdown, the repo from the list that token can see, and a new token can be saved in
  the cell from the same form. Field defaults are shown on a fresh source (limit starts at 20).

### Changed
- **The challenger project's sessions open in place.** The open session is marked and its
  detail sits under its row: the summary, that session's memory proposals with Accept and
  Reject, then the exchange. Commits are one line per session instead of a chip per commit,
  and the open session is in the URL. The separate proposals panel is gone; a row whose
  session still has proposals waiting says so.
- The project page shows the status badge beside the project name.

## [1.21.0] - 2026-09-02

### Added
- **The rius_rca project template.** The Tares half of the Rius RCA integration: one API call
  plans the alert source (keyed by a primary delivery_id label), the view, the per-delivery
  trigger, the Rius MCP server and the RCA agent with its callback, budget and prompt from
  params. Hidden from the console gallery; created by the Rius control plane.

### Fixed
- Unknown /api and /ingest paths answered with the console's HTML and a 200; they are a JSON
  404 now.
- PUT on a view or agent no longer requires the name in the body (the path already names it);
  a rename attempt is still refused.
- GET /api/sources/discover explains that discover is POST-only instead of failing as an
  unknown source.

## [1.20.0] - 2026-09-01

### Changed
- **The side nav has two groups.** Projects sits at the top with Overview and Ask; everything a
  project is made of is one Catalog group in pipeline order (sources, views, triggers, agents,
  firings, MCP servers, Explore). The Data and Automate sections are gone.
- The project page drops the "How it works" box, and Change history aligns with the other
  sections.
- The demo's logs source drops routine 2xx access lines and polls every 10s in hosted mode:
  errors and incident evidence still land, the always-on traffic noise does not.

### Fixed
- Deleting a template project with purge also deletes its triggers' firings (the dispatch log,
  the per-recipient deliveries, the cooldown state). They used to linger on Firings pointing at
  a trigger that no longer existed.

## [1.19.0] - 2026-09-01

### Changed
- **Every project renders on one page.** The AI SRE demo, the challenger workflow, shared code
  context and hand-assembled projects all use the page built for hand-assembled ones: Setup
  (sources, views and triggers editable in place, subscribers), Events, Firings, Agents, and a
  Sessions tab where the template reports sessions. Template actions (cause an incident,
  summarize a session), Repair for hand-deleted objects, how it works and the change history all
  moved with it.
- **Templates describe themselves as data.** The generic summary (runs, triggers, totals) is
  computed for every template; extras arrive as declared panels and cards. A new template needs
  no console work.

## [1.18.0] - 2026-09-01

### Added
- **The project page shows and manages subscribers.** A Subscribers box on the Setup tab lists
  everyone subscribed to the project's triggers (Tares agents, Slack channels, webhooks) with
  what each received, and adds or removes them right there. A project's own agent links to the
  Agents tab of the page.
- The project Firings tab replaces delivery counts with a "delivered to" column: the actual
  recipients, colored by outcome.
- /projects is now just the list of projects, with Create new; the four ways to start one moved
  to their own page at /projects/new.

### Fixed
- Deleting a trigger also removes its subscriptions. They could never fire again, but they
  showed up on Firings as subscribers waking on triggers that no longer exist. Rows left
  behind by earlier releases: remove them on the Tares agents page.

## [1.17.0] - 2026-09-01

### Changed
- **Deliveries is now Firings.** One word per thing across the console: a firing is the event, a
  delivery is what happens to it per recipient, and "dispatch" no longer appears in copy. The
  page lives at /firings (/deliveries redirects); external subscribers moved to the agents page.
- **The entity pages became clean lists with filters.** Sources gains connector and project
  filters; Views gets source and trigger filters, a capped sources column and no author column;
  Triggers gets a view filter, last-fired and subscribers columns (an active trigger nobody
  listens to says nobody); Firings names who each firing was delivered to. Doctrine subtexts
  removed across pages, and project membership is a table row on detail pages.
- **The firing page is one facts box** (trigger, project, entity, time, outcome, each recipient
  with its result and a link to the run), with the payload beneath.
- The hand-assembled project page grew operations: delete views and triggers in place, firing
  rows link to the firing page and the trigger card, Slack channels named, firings that reached
  nobody say so once with a count, events uncollapse to the whole stored line.

### Fixed
- A delivery whose Tares agent is still running showed as failed on the firing page; running,
  delivered and failed are now three distinct states.

## [1.16.0] - 2026-08-31

### Added
- **Hand-assembled projects have a new page.** Four tabs: Setup (the sources with their health,
  and every view and trigger as a card, editable in place; the watched view and trigger links
  stay on the page), Events (the newest rows across the project's sources, merged), Firings
  (every dispatch across the project's triggers, with the agent and Slack subscribers), and
  Agents (each agent inline with its runs, every delivery target, and its configuration).
  Template projects keep the existing page.

### Changed
- Trigger page: the watched view and the project badge moved from the subtitle into the first
  box, above condition, context window and cooldown.

## [1.15.1] - 2026-08-31

### Changed
- Rerun is offered on every finished agent run, not only ones that failed. An agent that uses
  all its rounds can still conclude with a partial "how far I got" report, which counts as ok;
  the rerun opens with that report and continues from it.

## [1.15.0] - 2026-08-31

### Added
- **A per-agent budget.** `budget_usd` on a Tares agent caps its lifetime spend, counted from
  its run log; once reached, runs are capped before any model call and the run says where to
  raise or clear it (Configuration, Advanced). In the catalog YAML and the API alike.
- On a hosted demo stack, the AI SRE demo's agent is born with a $2 budget: the shared stack
  fires around the clock, and an idle trial cell would otherwise burn its whole credit over a
  weekend.

## [1.14.0] - 2026-08-31

### Added
- **Projects from existing objects.** On the Projects page, next to the templates, "From existing
  objects" assembles a project from the sources, views, triggers, agents and MCP servers you
  already have (any that are not part of another project). Nothing is created: the project
  adopts them, its page shows their runs and firings, Edit changes the list, and removing an
  object or deleting the project leaves the object in place. The API is
  `POST /api/projects {"template": "custom", "name": ..., "objects": [{"kind", "name"}, ...]}`;
  catalog YAML takes the same shape.

### Added
- An agent run now opens with the agent's previous finding for the same entity, so it builds on
  its earlier conclusion instead of rediscovering it. Most useful for context-maintaining
  agents, where the last summary is a head start.
- A run that did not work (failed, out of rounds, capped, or empty) has a **Rerun** button on the
  agent page: the agent runs again with the same inputs, same trigger, same entity, and the
  firing's data when a firing woke it. `POST /api/agents/builtin/{name}/runs/{run_id}/rerun`.

### Fixed
- The challenger session thread showed only the first three findings of a review, cut mid-word
  at 500 characters. The thread now lists every finding in full (from the event payload, so
  sessions already recorded get it too), and challenge events keep their whole text.

### Changed
- **Use cases are now projects; recipes are templates.** A project is a named set of sources,
  views, triggers, agents and MCP servers with one page; the shipped recipes are Tares
  templates. The API moves to `/api/projects` and `/api/projects/templates`, catalog YAML uses
  `projects:` with `template:`, and the seed variable is `TARES_SEED_PROJECT`. The old routes
  (`/api/usecases*`, with their old response shape), the old YAML keys (`usecases:`, `recipe:`)
  and `TARES_SEED_USECASE` keep working for two releases. Ids (`uc_...`) and the database tables
  are unchanged.
- `claude_code` events label the repository the session ran in as `repo` (was `project`); the
  challenger session table, the memory proposals and the plugin's session-start memory lookup
  use the same word. On upgrade the saved `claude_code` sources and the views that read only
  them are rewritten to `repo`; events stored earlier keep their old label, like any label
  change. Memory already accepted keeps reaching Claude: it is keyed by the repo name, which
  did not change.

## [1.13.0] - 2026-08-27

### Added
- **Challenger workflow in the console**: the use case page lists every challenger session with
  the plan and per-commit review outcomes, the Claude/Codex exchange as one thread, a Summarize
  button per session, and the summarizer's memory proposals with Accept / Reject. Decisions are
  kept on the memory source (reject writes a `rejected_proposal` the plugin never hands out).
- The first session marked as a challenger session creates the `challenger_workflow` use case
  by itself; `/tares:challenger` in Claude Code is the whole setup.

### Fixed
- Plugin: the `set_session_flow` call is recognised under Claude Code's plugin-namespaced tool
  name, so marking a session works from a marketplace install.
- Plugin: commits made with options between `git` and `commit` (`git -c ... commit`) are reviewed.
- Plugin: Codex 0.150 dropped `--full-auto`; reviews run with `--sandbox read-only`.
- Plugin: only `[P1]` findings count as blocking on a plan critique.
- Use case actions that start an agent run (`summarize`) failed with "no running event loop"
  under `tares up` and left a run stuck at running.
- Challenge outcomes read "no findings" / "N findings (M blocking)" instead of Codex's PASS/FAIL,
  which elsewhere in the console means a broken run.
- Agent page: the opened run no longer scrolls back into view on every refresh.

## [1.12.0] - 2026-08-27

### Added
- **Challenger workflow**: a second model challenges Claude Code's plan and every commit,
  locally, while Tares keeps the record. The `challenger_workflow` use case adds a per-session
  view, a trigger on session end and a summarizer agent that writes a session summary with
  memory proposals. `set_session_flow` MCP tool marks a session; the `claude_code` connector
  gains the `flow` label and `challenge_plan` / `challenge_commit` / `challenge_waived` events
  with verdict, sha, round and finding counts.
- **Claude Code plugin 0.2.0**: `/tares:challenger` turns a session into a challenger session;
  the plugin runs Codex on plan exit and after each commit (blocking on P1/P2 findings by
  default, with a fix loop and `/tares:challenger-waive`), ships every review to Tares, and
  hands accepted project memory to Claude at session start. Review mechanics adapted from
  andreidavid/codex-review (MIT).

## [1.11.0] - 2026-08-26

### Added
- **Install skill for coding agents**: `skills/tares/SKILL.md`, installed with
  `npx skills add glassflow/tares --skill tares`. From one prompt, an agent installs Tares,
  starts it, adds a first source over the HTTP API, connects itself over MCP and shows one read.
  `AGENTS.md` at the repo root for contributors.
- **`tares status`**: a readiness checklist for a running instance (daemon, auth, sources
  receiving, views, triggers, Tares agents and whether a key is set, MCP endpoint, which agent
  clients on this machine have Tares registered, Slack) that ends with the one next step.
  `--json` for scripts; exit 1 when nothing answers. `tares up` prints the same "Next:" line
  right under the console URL once the daemon is up. `/health` now carries `version` and
  `uptime_seconds`.

### Changed
- An agent run without a key, and the banners on the Agents pages, now say what to do: set
  `ANTHROPIC_API_KEY` before `tares up`, or add a key under Settings (linked).
- README rewritten around the always-on agents positioning: who Tares is for, how it compares,
  common questions. Docs got a new introduction and a page for coding assistants
  (docs.glassflow.ai/tares/agents/ai-resources) with `/tares/llms.txt`.

## [1.10.0] - 2026-08-25

### Changed
- **A key stored in the console now takes precedence over `ANTHROPIC_API_KEY` in the
  environment** (it was the other way around). Saving your own key takes over immediately, and
  removing it falls back to the environment key. If you relied on the env var overriding a
  stored key, clear the stored key instead. The Slack bot token keeps its env-first order.

### Added
- **Grafana Loki connector.** Polls a LogQL stream selector via `query_range` (timestamp
  cursor, bearer/basic/tenant auth, match/drop filters); one event per log line with the
  stream's labels, and the same derived fields (level, HTTP status, JSON scalars) as
  docker_logs. Works against any reachable Loki, including Grafana Cloud.
- **The AI SRE demo works against a hosted demo stack.** When `TARES_DEMO_PROMETHEUS_URL`,
  `TARES_DEMO_LOKI_URL` and `TARES_DEMO_API_SERVER_URL` are set (a hosted deployment sets them;
  a self-hoster can point them at their own stack), the demo use case adapts: nothing to
  install, URL defaults from the environment, logs from Loki instead of a local Docker
  container, and the actions say plainly the stack is shared. With them unset, the local docker
  compose flow is unchanged.
- **The spend meter records which key paid.** Every `model_usage` row carries a `key_source`
  (environment vs console), and `GET /api/usage/model` reports a `by_key_source` split, so an
  operator-provided key's spend is separable from your own.
- **`TARES_SEED_USECASE`**: seed a named use case once on first boot, so an instance can start
  with a working setup instead of an empty catalog. One-shot by a settings marker; deleting the
  seeded use case never brings it back.

## [1.9.0] - 2026-08-24

### Added
- **Model usage and cost per agent run.** Every run records the model it used, its token usage
  (input, output, cache write/read) and a USD cost priced at write time; Ask turns (console and
  Slack `/tares ask`) are metered the same way. Runs from before this release show no cost
  (unknown, never guessed), and models without a known price record tokens with a null cost.
- **`GET /api/usage/model`**: the instance's Anthropic spend as all-time totals plus a per-day
  tail, split by surface (agent runs vs Ask). Generic metering data only, for a hosted control
  plane or your own budgeting.
- **The Tares agents page is an operational surface.** Stat cards (total cost, runs, success
  rate, average duration), Overview / Runs / Configuration tabs, and runs as a table showing
  model, rounds, tokens and cost per run, expandable to the finding. The agents list gains runs
  and cost columns, and the Overview page a Model spend panel.
- The write-back webhook body now carries the run's `usage` and `cost_usd`.

## [1.8.2] - 2026-08-19

### Added
- **Shared code context links its guide** from the Use cases card and the setup page.

### Fixed
- **A trigger no longer misses a key whose source ingested right after another.** Trigger
  evaluation is debounced to once per 10 seconds per trigger; a second source of the same view
  that ingested inside that interval was left unevaluated until the next ingest, by which time a
  short detection window could have slid past its events (two repos polled a second apart: the
  first fired, the second never did). A skipped evaluation is now re-run once the interval ends.
- **Shared code context**: the `every_commit` trigger's detection window is 5 minutes (was 2),
  matching the 60-second source poll.

## [1.8.1] - 2026-08-19

### Fixed
- **Auto-discover (Docker) works again.** The environment scan still read the Prometheus
  connector's old one-shot discover shape and failed with an internal error whenever a
  Prometheus container was running. It now proposes a reachable starter metrics source (`up`,
  keyed by `service` when the server has that label) and says how many metrics wait to be picked
  on the source page; an unreachable or unexpected server never fails the scan.

### Added
- **AI SRE demo as a use case.** Use cases > AI SRE demo (tagged demo) creates the same three sources,
  `service_timeline` view, `incident` trigger and `incident-first-look` agent that
  `demo/catalog.demo.yaml` seeds, with one click and no catalog import; an existing unowned demo
  catalog is adopted rather than duplicated. The wizard lists what to do first (start the stack,
  set an Anthropic key) with copyable commands, and the use case page has Cause an incident
  (error_spike, latency, dependency_outage) and Clear the fault buttons that flip the api-server's
  fault switch. Recipes can now declare `tags`, `SETUP` steps and `ACTIONS` (served at
  `POST /api/usecases/{id}/actions/{name}`).

## [1.8.0] - 2026-08-18

### Added
- **Use cases.** A new top-level console section and a framework behind it: a use case is a
  ready-made setup (a recipe with parameters) that creates ordinary Tares objects and owns them.
  Instances live in `usecases`; the sources, views, triggers, agents and MCP servers they create
  carry `owned_by` and show a "part of use case" badge on their normal pages, stay fully editable
  and deletable there (an edit marks the object customized and the engine keeps that version; a
  hand delete shows as missing on the use case page with a Repair action). Engine operations:
  create (all or nothing), update (re-plan and diff), pause and resume (trigger and agent off,
  sources keep ingesting), delete (optionally purging events), repair. API under `/api/usecases`;
  a `usecases:` section in catalog YAML seeds instances on first boot and export includes them.
- **Console for use cases.** The Use cases page (recipe cards, existing instances), a four-step
  wizard for the shared code context recipe (GitHub access with a token permissions guide, source
  repos picked from the token's repositories or pasted, the context repo with a look inside it and
  the page layout, trigger and agent with a preview of what Start creates), and the instance page
  with Runs and Configuration tabs, first-look runs labelled, runs deep-linked to the agent run,
  and the underlying objects with their state. The breadcrumb shows the use case name.
- **GitHub credential stored once.** Settings > GitHub holds a token by name; `github` sources
  take `credential: <name>` instead of a token per source, MCP servers take
  `credential:github/<name>` as their auth value, and rotating the token in one place rotates
  every user of it. Test shows the login and scopes; the credential lists what uses it. New API
  under `/api/integrations/github`, including the repositories the token can see and a look into a
  repository's tree.
- **MCP servers take extra headers.** A non-secret `headers` map alongside the auth header, merged
  into every request; used to send `X-MCP-Toolsets` and `X-MCP-Readonly` to GitHub's hosted MCP.
  Console form has it under Advanced; catalog YAML round-trips it.
- **Settings has tabs.** Access and API keys, Anthropic, GitHub, Slack; `?tab=` deep-links.
- **Per-agent round cap.** A Tares agent has `max_rounds` (1 to 24; blank means the default). The
  default is 6, or 12 once the agent uses external MCP servers, since a run that reads a diff or a
  file and writes back needs more than the read-and-conclude budget. Every run records the cap it
  was held to and shows `rounds/max`. When the budget runs out mid-investigation the model gets one
  final call with tools disabled to conclude from what it has; if it still cannot, the run ends
  `exhausted` (a new status, not a failure), keeps the last text as a partial note, and says to raise
  max rounds. Catalog YAML imports and exports the field; the agent form has it under Advanced.
- **Use case: shared code context.** The first recipe on the use-case framework. Given a stored
  GitHub credential, a list of source repositories and a context repository, it creates one commits
  source per repo, a `repo_activity` view keyed by repo, a trigger that fires on any new commit
  (batched per repo, 5m cooldown), GitHub's hosted MCP server registered with the same credential
  (toolsets `repos,pull_requests`), and a Tares agent that reads each diff and keeps the context repo
  current, as a pull request (default) or direct commits. Two page layouts: keep the repo's existing
  pages and update them in place (default; the wizard shows what is at the chosen path, root
  allowed), or one page per source repository plus an index. On Start it can run once per repo over
  the last 7 days (the "first look", on by default) so the context repo starts current without
  waiting for a commit. The agent writes plain sentences with no em dashes and finishes with a
  finding that says what it changed and links the pull request. `POST /api/usecases` with `recipe: shared_code_context`, or a `usecases:` entry in
  the catalog.
- **GitHub commits carry their changed files.** With a token or credential, the `github` connector
  fetches each new commit's file list (paths, status, additions, deletions, patch) into the payload,
  capped at 20 files and 4k characters per patch with a `files_truncated` flag; one extra API call
  per commit, paused for 10 minutes when the rate limit runs low. Off with `files: false`.

## [1.7.1] — 2026-08-17

No code changes; a version bump so managed cells can be re-provisioned through the upgrade path
(a same-version upgrade is a no-op there), which is how a cell picks up `TARES_WORKSPACE_URL` from a
control plane that started setting it after the cell was last applied.

## [1.7.0] — 2026-08-17

### Added
- **A cloud cell links to its workspace.** Users, the Slack app install, plan and storage are
  managed in the control plane, and a user in the data-plane console had to know that to find
  them. When the instance is given `TARES_WORKSPACE_URL` (a generic, env-gated hook the cloud
  sets per cell, surfaced on `/health` as `workspace_url` next to `login_url`), the console shows
  a **Workspace** link in the sidebar footer and a banner at the top of Settings naming what lives
  there, including the Slack split: the bot token is here, the app install is in the workspace.
  Self-host never sets it and sees nothing. (TR-142)

## [1.6.0] — 2026-08-17

### Changed
- **The console wears the GlassFlow design system.** Palette and typography now come from the
  agency's HeroUI Kit V3 variables, the same source Rius uses, so the two products share one
  palette by construction: cream canvas and surface tiers, eclipse ink, one Space Sand accent,
  both themes. Geist Mono carries labels, badges, table headers and code; the pixel voice is kept
  for the Overview's headline numerals. Square corners, hairlines and no shadows stay, now
  consistent everywhere. (TR-140)
- **The sidebar is one layer, rearranged.** Overview and Ask on top, then **Data** (Sources,
  Explore, Views), **Automate** (Triggers, Tares agents, MCP servers, Deliveries) and **Agent
  access** (Connect, Reads), with Settings in the footer. Agents is now Tares agents only; the new
  **Deliveries** page holds every subscriber with its delivery health and every trigger firing (a
  Slack channel is a destination, not an agent). MCP servers has its own entry. Security is
  Settings, which is what it always held. Old routes redirect. (TR-137)

### Fixed
- **Agent findings render properly in Slack.** They were posted as raw markdown, so headers, bold
  and pipe tables arrived as literal `##`, `**` and pipes. Both posting paths now send Block Kit
  through the same converter `/tares ask` uses; tables become aligned monospace blocks and long
  findings split under Slack's block cap. (TR-141)
- Em dashes removed from every string the daemon serves (connector descriptions, field help,
  errors, prompts, CLI output). (TR-143)

## [1.5.1] — 2026-08-17

### Fixed
- **The trigger page keeps setup out of the way.** It mixed the running system (subscribers,
  delivery status, recent firings) with always-open forms for wiring a webhook or a Slack channel.
  Those forms now sit behind **Add webhook** / **Add Slack channel** buttons beside **Add a Tares
  agent**, opening in a small panel with Cancel; on success it closes and the subscriber table
  reloads. (TR-138)

## [1.5.0] — 2026-08-14

### Added
- **Tares agents can use external MCP servers.** Register a server once under **Agents → MCP
  servers** (streamable-HTTP URL plus an optional auth header, stored as a secret; **Test** lists
  its tools), then opt individual agents in on their form. At run time the server's tools are
  offered alongside the built-in reads, prefixed by server name; every run records which external
  tools it called. A down server is skipped, a verbose one truncated, and a failed call is
  evidence for the model rather than a failed run. This moves the read-only boundary for the
  agents you opt in: tools from an external server can act on whatever that server exposes.
- **Per-agent model.** Pick a model per agent from a curated dropdown, or leave it following the
  instance default (`TARES_AGENT_MODEL`). Catalog YAML accepts any `claude-*` id.
- **Findings deliver where you already look.** The agent form's delivery options are toggled
  rows: post to a **Slack channel** via the workspace bot (picked from the bot's own channel
  list), or POST each finding to an **authenticated write-back webhook** as JSON with its run
  metadata (trigger, entity, model, rounds, tool calls, timing; never a credential), with an
  optional bearer token. The legacy per-agent Slack incoming webhook remains and can now actually
  be turned off.
- **Paste a catalog snippet.** The Add source page is a connector card grid with **Add via
  YAML**: paste a snippet from the docs or a teammate and it merges into the live catalog,
  validated first, upsert-only, nothing removed, no restart. One paste can carry sources, views,
  triggers, agents and MCP servers; the result links to everything it created.

### Fixed
- **Delivery links on a dispatch page go where the story continues.** They all pointed at a page
  that no longer shows agents. A Tares agent now links to its run for that exact firing (opened,
  highlighted and scrolled to), an external agent to its roster row; a Slack delivery is plain
  text, because the message's story continues in Slack.
- Em dashes removed from every rendered string, and redundant help lines under form fields
  trimmed.

## [1.4.0] — 2026-08-14

### Added
- **Ask keeps your conversations.** Every conversation is saved on the instance and the Ask page
  is now the familiar AI-chat shell: a sidebar of conversations next to the chat, newest resumed
  automatically when you come back — navigating away, refreshing, or closing the browser loses
  nothing. Start another with **+ New conversation**; delete one from the sidebar. Resumed
  conversations carry their full transcript back into the agent's context. The instance keeps the
  newest 50; the ⌘K palette stays a single column and picks up the same latest conversation.
  Opening an old conversation just reads it — only a sent message or an applied proposal moves it
  to the top.

## [1.3.1] — 2026-08-14

### Fixed
- **Explore no longer sticks at "reading timeline…" in a background tab.** The read is skipped
  while the tab is hidden (deliberately, to save load), but nothing re-fired it when the tab
  became visible — you stared at the spinner until the next 10-second tick. The read now fires
  the moment the tab becomes visible.
- **The Connect page no longer shows the access token in clear text.** The Claude Code snippet
  rendered the real token unmasked the moment the page opened; on a secured instance that is the
  root token on screen. Every credentialed snippet now displays the masked token while the copy
  button carries the real one — which also fixes the other tabs, where copying without hitting
  reveal first copied the mask.

## [1.3.0] — 2026-08-10

### Added
- **Connect → MCP is a client picker.** The tab showed three generic snippets (JSON, Claude Code,
  stdio) and left the translation to the reader. It now asks which client you're connecting —
  Claude Code, Codex CLI, Cursor, Claude Desktop, Other (JSON), stdio — and shows that client's
  exact command with this instance's endpoint and token filled in, mirroring the docs page
  (docs.glassflow.ai/tares/agents). A verify line says what to ask and which tool call to expect.
- **A fresh instance lands on Sources.** Opening the console with zero sources showed an Overview
  of zeros. The first landing of a page load now goes to Sources, where the next step actually is;
  clicking Overview afterwards still works, and a failed source-list load never counts as empty.
- **The trigger page links the webhook contract.** Wiring an external agent to a trigger now points
  at Connect → Webhook (push) — which is deep-linkable as `/connect?tab=push` — for what the
  endpoint receives and how to acknowledge. The webhook tab itself was tightened: a minimal
  receiver example, and a note that built-in Tares agents need no endpoint at all.

### Fixed
- **The webhook tab stated the wrong acknowledge rule.** It said any response below 500 counts as
  delivered; the dispatcher only counts 2xx. It now matches the code: 2xx acknowledges, 5xx and
  transport errors are retried with backoff, a 4xx is recorded as failed and not retried.
- The webhook tab's delivery-history link said "Agents" but pointed at Activity.

## [1.2.1] — 2026-08-07

### Fixed
- **A subscribed Slack channel is no longer presented as an agent.** It sat under "Agents woken by
  this trigger" in a column headed `agent`, but a channel is a destination — it doesn't wake up and
  do anything. The section is now "Where this trigger delivers" and the column is `subscriber`,
  which covers all three kinds honestly: a Tares agent, a connected webhook, and a channel. The
  delete-trigger warning and the trigger's summary line had inherited the same wrong noun.
- **A subscription can be removed from the console.** Subscribing was a one-way door — the
  `/unsubscribe` endpoint and the per-trigger subscription id both existed, and nothing used them.
  Removing affects only that trigger; a subscriber wired to several keeps the rest, and its delivery
  history is kept.
- A Slack row showed the raw channel ID (`#C0BNV121CRX`). It now resolves to the channel name, with
  a lock for a private one, falling back to the id — which is the honest answer when the bot has
  been removed from the channel and the name is no longer knowable.

## [1.2.0] — 2026-08-07

### Added
- **Pick a Slack channel from a list instead of pasting an ID.** Subscribing a trigger to Slack meant
  going to Slack, right-clicking the channel, Copy link, and pulling the ID out of the URL. The
  console now offers the channels the bot can actually post to.

  It lists via `users.conversations`, **not** `conversations.list`, and that distinction is the whole
  design: `conversations.list` returns every public channel including ones the bot was never invited
  to, and posting to one of those fails at the first firing with `not_in_channel` — a subscription
  that can never deliver, which is what `validate_slack_channel` exists to prevent. On a real
  workspace this was the difference between offering 32 channels and offering the 2 that work.

  Because a missing channel now means "the bot isn't in it", the console says so: a line under the
  picker and a Refresh beside it that re-fetches only the channel list, so someone half-way through
  wiring up a trigger doesn't lose it to a page reload. Private channels are included and marked with
  a lock; the value submitted is the channel ID, which survives a rename.

  Needs the `channels:read` and `groups:read` scopes. **Scopes are baked into the token at install**,
  so a token issued before them keeps posting fine but cannot list — the console detects exactly that
  and falls back to the free-text box it always had, with a prompt to reinstall. Nobody loses the
  ability to subscribe.

### Changed
- **Slack alerts no longer unfurl links.** The alert body carries every label on the event, so a
  source with a `host` label — web traffic, CDN logs and deploys nearly always have one — made Slack
  fetch that site and staple a preview card onto the alert. It roughly doubled the height with
  nothing about the incident, read as though Tares were linking somewhere relevant when it was
  echoing a label value, and meant alerting had the side effect of Slack fetching a customer's URLs.
- **A Slack alert now links the firing** (`/dispatches/<id>`) rather than the entity — "what fired
  and what did it carry" is the question the reader has. An agent's *finding* still links the
  entity's timeline, which is what a finding is about. Both require `TARES_PUBLIC_URL`; a link to
  127.0.0.1 is worse than no link.
- **Slack timestamps are readable.** The footer was a raw ISO string with microseconds and a UTC
  offset; it now uses Slack's date token, which renders in each reader's timezone. Event ages read
  `[29m ago]` instead of `[T-1734s]` — in the Slack copy only, since the payload is the agent-facing
  contract and goes out over MCP verbatim.
- **`/tares ask` answers render properly.** Markdown tables arrived as raw pipes plus a `|---|---|`
  row, and `---` rules as three literal dashes. Tables are not an edge case — the assistant is told
  to use small tables where they help — and Slack renders no tables at all, so one becomes an aligned
  monospace block, the only place Slack keeps columns lined up.
- **The last native dropdowns in the console are gone.** A native `<select>`'s open menu is drawn by
  the OS and cannot be themed, so it arrived as a light system popup in the middle of the console.
  The view filter operator, the trigger aggregate, the Explore lens, the poll interval unit, the
  normalize rule kind and the sources status filter all use the app's own picker now.

### Fixed
- The Slack channel field's placeholder promised only "the channel ID", while a lowercase channel
  name has always been accepted too.

## [1.1.1] — 2026-08-07

### Added
- **Vercel sources expose the request (`proxy`) block**: `status_code`, `url`, `method`, `referer`,
  `path_type`, `region` and `cache` join the fields you can build labels and keys from. `url` is
  what the client actually requested — the existing `path` is the *rewritten* value, a Next.js
  segment on prerendered routes (`/tares?_rsc=19hu7` logs as `tares.segments/_head.segment`), so it
  cannot answer which page failed. Label the code as `{"field": "status_code", "type": "number"}` to
  filter `>= 400` and aggregate it in a trigger. Fields are resolved in `label_context()`, so ingest
  and backfill agree. Entries without a `proxy` block (builds) get no request labels rather than
  empty ones.

### Fixed
- **The Vercel connector discarded the HTTP status code.** It was computed in `_map_one` and never
  passed to the envelope — dead since it was written. With no status field to label on, the only
  value in reach containing digits was `path`, so a status label had to be built as a regex over it:
  that fires only when the request path is *literally* `404` (a hit on the 404 page asset, not a
  request that returned 404), names the 404 page instead of the URL that failed, and is empty on
  live traffic.
- **The assistant had to guess the shape of a view filter, and got it wrong.** `propose_view` typed
  `filters` as an array of unconstrained objects, so the shape was stated nowhere the model could
  see it — producing a flat `{"service": "x"}` pair, then `"=="` for the operator, both rejected on
  Apply. The schema now requires `field`, `op` and `value` with `op` as an enum of the operators
  the code validates against (`eq`, `neq`, `contains`, `gt`, `gte`, `lt`, `lte`), which is enforced
  when the tool is called rather than asked for in prose. The prompt gains the matching rule,
  including that a filter `field` may also be one of the built-in columns `event_type`, `source`,
  `text` and `key_value`.
- **A view proposal showed `2 filter(s)` instead of the filters.** That was the one place a bad
  filter could be caught before applying it, and it was showing a count. Cards now render each
  filter, and a malformed one as its raw JSON in a marked chip.

## [1.1.0] — 2026-08-07

### Changed
- **Breaking: `/api/agent/chat` no longer accepts an `X-Anthropic-Key` header.** There is one
  Anthropic key per instance — the environment's `ANTHROPIC_API_KEY`, else the one stored under
  Security — and it is the key the console assistant, `/tares ask` in Slack and trigger-woken
  agents all resolve. The console used to keep a copy in `localStorage` and send it per request, so
  a key added on the Ask page made Ask work while Slack and Tares agents still reported none
  configured, having no browser to read it from. Callers passing the header now get the instance's
  key; a request with no key configured anywhere gets a 400. Stale browser copies are cleared on
  load.
- **Ask and Organize are one assistant.** Organize was Ask with a different system prompt — both
  surfaces posted the same tools to the same endpoint — so the judgement it carried (what makes a
  good entity key, labels come only from real fields, watch for value variants, views key on labels
  rather than raw fields) now governs every proposal Ask makes. Asking Ask to add a label
  previously got an agent with none of it. Organize's full-source sweep survives as a starter
  prompt; `/organize` redirects to `/ask`.
- **The Ask page is readable at length.** The transcript scrolls with the page instead of inside a
  64vh slot; autoscroll follows the tail only while you are at the tail and detaches on a
  deliberate upward scroll; turns are labelled and separated; tool calls draw as a pipeline of
  steps showing what ran, how long it took and what came back; the composer is pinned, takes
  multiple lines, and offers Stop, New and copy-on-hover.
- **The source page says what it already knows.** The labels table gains a `rules` column naming
  which rule rewrites a label's values (`≈ regex`, `≈ 2 renames`, `≈ regex +5`, `≈ none`), from one
  shared summary so the editor and the read-only table cannot describe the same rule differently.
  The entity key is chosen from one dropdown above the table rather than a radio per row.
  `Labels & key` collapses, defaulting to closed. `Fields` lists only what is not already a label.
- Dropdowns are ours. A native `<select>`'s open menu is drawn by the OS and cannot be themed, so
  one macOS menu appeared mid-form beside our own styled inputs. `Picker` replaces it, keeping
  keyboard control, `disabled`, and listbox semantics.

### Fixed
- **A failed tool call in the assistant reported itself as successful.** The `ok` flag was derived
  from the response body starting with `{"error"`, which only ever matched the local unknown-tool
  case — every daemon error path raises `HTTPException` and serializes as `{"detail": …}`, so a 404
  read as a success and the console drew a failed read as a completed step. It now comes from the
  status code.
- **Pressing Stop could break the next question.** A turn stopped before it produced text
  serializes to an empty string, which the Messages API rejects, so the following question failed
  with a warning pointing at nothing.
- **A label rewriting every value looked identical to a pass-through one.** The table computed the
  distinction and styled it with a bare `button.active`, which no CSS rule matched — every
  `.active` rule is scoped to another component. On one instance a rule silently collapsing 237
  distinct values to 2 was invisible from the table.
- The merge panel never said which label it was editing, and a rule that blanks a value rendered as
  a green `ok` badge followed by nothing — indistinguishable from a value that failed to render.
  Blanking is a legitimate rule and is now stated as one.
- Normalize panels no longer expand on load, a long label name no longer pushes its badge onto a
  second line, and three paragraphs describing the columns directly beneath them are gone.

## [1.0.2] — 2026-08-06

### Changed
- **Documentation moved to <https://docs.glassflow.ai/tares>.** Every link in the README, the
  security policy, the packaging metadata, the CLI's exposure warning and the console now points
  there. Two of them were already wrong: the security policy and the issue template still linked
  `navflow.ai/docs`, and the console's Agents page linked `www.tares.ai`, which does not resolve.
- Repository URLs follow the rename to `glassflow/tares`.

## [1.0.1] — 2026-08-05

### Fixed
- **The 1.0.0 container image was never published** — its Dockerfile still copied `navflow/`, so
  the build failed after the tag was cut. 1.0.0 exists on PyPI but has no image; **1.0.1 is the
  first usable image tag**.
- Everything the rename missed outside `tares/` and `ui/`: the Dockerfile set `NAVFLOW_HOME` (which
  the new guard refuses to start on), both compose files set `NAVFLOW_CATALOG` / `NAVFLOW_AUTH_TOKEN`
  (so self-host compose could not boot at all), and the Claude Code plugin invoked the removed
  `navflow-mcp` entry point.

## [1.0.0] — 2026-08-05

**Breaking. There is no compatibility layer, deliberately.**

### Changed
- **`navflow` is now `tares`** — the Python package and module, the `navflow` / `navflowd` /
  `navflow-mcp` commands (now `tares` / `taresd` / `tares-mcp`), the PyPI project, and the image
  (`ghcr.io/glassflow/tares`). Existing `navflow` releases and image tags stay resolvable; nothing
  new is published under them.
- **Every `NAVFLOW_*` environment variable is now `TARES_*`** — the prefix is the only change, e.g.
  `NAVFLOW_DB` → `TARES_DB`, `NAVFLOW_AUTH_TOKEN` → `TARES_AUTH_TOKEN`, `NAVFLOW_PORT` →
  `TARES_PORT`. The MCP proxy's target is `TARESD_URL`.
- **The daemon refuses to start if any `NAVFLOW_*` variable is set**, printing the mapping. This is
  deliberate and is the one place a breaking rename must be loud: silently ignoring
  `NAVFLOW_DB` would open a database somewhere else and come up healthy and empty.
- **The data file is `tares.duckdb`** (was `navflow.duckdb`), and the default home is `~/.tares`.
  Starting with a `navflow.duckdb` beside a missing `tares.duckdb` is refused, with the `mv` to run
  — indistinguishable from data loss otherwise.

### Upgrading from 0.3.x
Rename your environment variables, `mv navflow.duckdb tares.duckdb`, and use `tares` in place of
`navflow`. The daemon will tell you if you miss one.

## [0.1.4] — 2026-07-03

### Removed
- **`changelog` and `config` connectors** — dummy, demo-only connectors that had outlived their
  use. Also drops the demo api-server's unused `/admin/changelog` and `/admin/config` endpoints and
  renames its fault switch `/admin/inject` → `/demo/inject`.

## [0.1.3] — 2026-07-03

### Added
- **`include_payload` on `read`/`query`** — opt-in flag that returns the full lossless stored
  record as `raw` on each row, alongside the summary `text`. Exposed over HTTP and the MCP
  `read`/`query` tools; covers all connectors.

### Fixed
- **Claude Code plugin install** — publish a root `marketplace.json` (`/plugin marketplace add
  glassflow/navflow`) and fix `plugin.json` load errors on Claude Code 2.1 (duplicate hooks ref,
  missing `ingest_token` default, `navflow_url` required flag hiding its default).
- **Double-ingest for `claude_code`** — the first pushed event flips a poll-mode source to push
  mode, so a source fed by the plugin no longer also tails files and ingests every event twice.

## [0.1.2] — 2026-07-02

### Added
- `navflow --version`.

## [0.1.1] — 2026-07-02

First public (soft-launch) release.

### Added
- **`read(selector, window)` primitive** — a correlated, time-ordered read across *all* sources
  matching a strict-AND conjunction of `label=value` constraints, with no view required. Exposed
  over HTTP (`POST /read`) and MCP (the `read` tool). Views become an optional narrowing lens;
  triggers still attach to a view.
- **Console redesign** — a selector-first **Explore** (pick an entity, add filters, read across
  every source, with a human/agent view toggle), a **⌘K Ask** command palette, a three-act
  navigation, and separate Views / Triggers pages.
- **Agents → Reads** — a client filter (defaults to `mcp`) over the read activity log.

### Changed
- Package and CLI distribution renamed from `navflow-mvp` to **`navflow`**.

Earlier `0.0.x` history is in the git log.
