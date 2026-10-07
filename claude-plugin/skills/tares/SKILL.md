---
name: tares
description: Use when you need cross-source context about this project's systems (recent deploys, logs, metrics, entities, correlated timelines), OR when organizing Tares itself by creating sources, labels and triggers. Tares is the data plane; read it via the tares MCP tools for one correlated timeline per entity, and author its catalog with the create tools following the rules below.
---

# Tares

Tares is a data plane for agents: it ingests many sources (logs, metrics, deploys, database rows,
agent sessions) and serves **one correlated, time-ordered timeline per entity** (a service, customer,
session, …). The **tares** MCP server is connected (this plugin registers it).

## Reading

When a question needs real context about running systems — "what changed before this error", "is
this service healthy", "what has this customer done recently" — read Tares instead of guessing:

- Start by discovering what's there: `list_sources`, `catalog_list`, `entities`.
- Read a timeline with `read` (a `{label: value}` selector), narrowed to a `project`'s sources or
  to named `sources` when you know which matter.
- Treat the timeline as ground truth for *what happened and when*; cite specific events.

This plugin also streams the current Claude Code session into Tares (the `claude_code` source) when
session streaming is enabled, so prior sessions are queryable as a source.

## Projects with docs and tickets

A project can hold docs (spec, plan, AGENTS.md, a starting prompt, notes, working docs),
milestones with acceptance checks, and a ticket list (in Tares, or synced from Linear) where each
ticket has a milestone and the tickets it depends on. To spec a new project, use the
`spec-project` skill. When told to work on a Tares project, start with its `start` doc
(`list_docs`, `get_doc`), then work one milestone at a time (`list_milestones`): the ready tickets
from `list_tickets`, in order, each read with `get_ticket`, which carries the working doc.
If a ticket is already in progress when you start, an earlier session stopped on it: call
`pick_up` first, and finish that ticket. While you build, keep a `## Progress` section at the end
of the ticket's working doc (done, next, how to run things), so the next session can pick up.
Record each check you run with `add_check` (it adds one line to the working doc), what you
learn about the project with `remember_for_project`, and, in a tares-factory crew, an assumption
you make so as not to block with `assume`.

Two docs belong to every project: the shared AGENTS.md (standing rules) and memory (facts that
hold everywhere). Read them with `read_global_docs`. When the user states a rule or fact that
holds beyond the current project, offer to add it with `add_to_global`.

## Challenger sessions

When the user asks to make this a challenger session (any wording: "challenger session",
"let Codex challenge this", "/tares:challenger"), call the `set_session_flow` tool with
`flow="challenger"` and say so in one sentence. Do not run Codex yourself: the plugin's hooks
challenge the plan when you leave plan mode and every commit you make, and hand you the findings.
`set_session_flow` with an empty `flow` turns it off.

## Authoring: sources, labels, and triggers

Tares's data model has three layers. Get them right and correlation just works; guess and it
silently doesn't:

- **Fields** — the *candidate menu*. A source's raw payload is stored losslessly; `source_fields`
  (and `discover_source` for a new source) profiles the fields it actually contains, with coverage
  and top values. Fields are not queryable on their own.
- **Labels** — the *declared axes* you promote from fields (or a const, or a regex over a field).
  Labels are what you filter, group, key, and correlate by. One label is the **primary key**.
- **Triggers**: watch one or more sources (narrowed by filters) for a condition, keyed by a
  **label** the sources share, and wake agents with the correlated timeline. Every trigger belongs
  to a project (the default project when you name none); its sources join that project.

### Rule 1 — Labels must come from real fields (never invent one)

Before choosing a source's labels, call **`source_fields`** (existing source) or use
**`discover_source`**'s `proposed_config` (new source) to see the fields that actually exist. A
label reads from one of three things, all grounded in real data:

- `field`: a profiled field name — must match a field `source_fields` shows, exactly.
- `const`: a fixed value stamped on every event (for a source with no natural field for an axis).
- a **regex** over a field — set `pattern` + `replace` (and/or `map`) on the label to normalize
  messy values. Regex/alias normalization is a first-class feature: use it instead of guessing at a
  clean field. `type: "number"` makes a label aggregatable (for triggers); the primary key must be a
  string.

Never name a `field` that isn't in the profile — it extracts nothing. Set labels by creating the
source (`create_source`) or updating its config with the full `labels` list.

### Rule 2: triggers key and filter on LABELS only, never raw fields

A trigger's `key_field` (and any filters) must be a **label the chosen sources expose**, not an
arbitrary payload field. If you want to correlate on something that isn't a label yet, **promote it
to a label first** (Rule 1), then `create_trigger` over the sources, keyed by that label. Confirm a
source's current labels with `catalog_describe("source:<name>")` before creating it.

### Rule 3 — To match a label across sources, add a NEW label — don't rename

To join source B to source A on a shared axis (say `service`), source B needs a label **named
`service`** too. Declare a **new** label on B (reading B's matching field) — do **not** try to
rename B's field or existing label. There is no rename; a source's label set is declared whole, so
add the shared-named label to B and keep its others. The values must agree **literally** across
sources for correlation to work — if B's raw values differ (e.g. `checkout-svc` vs `checkout`),
normalize them with `pattern`/`replace`/`map` on B's label so they match A's.
