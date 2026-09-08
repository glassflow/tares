"""The `rius_rca` template: the Tares half of the Rius RCA integration (TR-223).

One project per Rius workspace, created remotely by rius-cp via POST /api/projects against the
workspace's dedicated cell. When an alert fires in Rius, its region POSTs the alert context to
this project's source; the trigger wakes the agent; the agent investigates over the Rius MCP
server and the write-back webhook POSTs the finding to Rius's callback, keyed by the delivery id.

Every knob here is the hand-built configuration proven on a live cell on 2026-09-01 (TR-223
comment: run_7a954505c5c7 and friends — 4/4 green against staging Rius), with the five traps from
that session baked in:
  1. the KEY is stamped at ingest by the source's PRIMARY LABEL, never by view.key_field
     (TR-226/TR-228). The entity is the SERVICE (TR-285): a delivery id is new on every firing,
     so a cooldown keyed by it never held and one noisy service woke the agent on every alert;
     keyed by service, one investigation per service per 5 minutes, and the timeline the agent
     reads is that service's history. The delivery id stays a label on every event;
  2. the prompt names the Rius tool chain and demands tool-grounded claims — it must not carry
     the demo agents' "no live access" language, which makes an MCP-equipped agent refuse to
     query;
  3. the text_template is the agent's entire rendered evidence, so it carries every identifier
     the agent needs to query on;
  4. the agent is planned enabled;
  5. max_rounds defaults to 10 (a real MCP chain used 3 rounds / 5 tool calls; the global
     default of 6 is too tight for deep traces).
"""
from __future__ import annotations

from .base import PlannedObject, ProjectError, Template
from .registry import register

SOURCE = "rius_alerts"
VIEW = "rius_alerts_view"
TRIGGER = "rius_alert_fired"
AGENT = "rius_rca_agent"
MCP = "rius"

# The rendered line is all the agent sees of the payload; it must carry every identifier the
# agent needs to query Rius on. The raw JSON is stored losslessly but reaches the agent only
# through this template.
TEXT_TEMPLATE = ("{rule} on {service}: {summary} "
                 "[workspace={workspace_id} window={window_start}..{window_end} "
                 "delivery={delivery_id}]")

# The prompt proven in live testing. Overridable per project (PARAMS.prompt) so Rius can evolve
# their tool names without waiting on a Tares release.
PROMPT = """You are an SRE performing root-cause analysis for the Rius agent-observability platform.

You are handed ONE alert firing from Rius. The firing payload carries identifiers, not bulk
evidence: the delivery id, the alert, the affected workspace and service, and the window.

You DO have live access to Rius through the `rius` MCP server. Use it. Start from
`agent_traces_summary` and `workspace_metrics_overview` for the window, narrow with
`list_agent_traces` (status "Error"), then pull the specific failures with `get_agent_trace`,
passing include_content=true when you need to see what a span actually said or returned.

Produce a short incident note in markdown:
1. **What is failing** and since when.
2. **Most likely root cause**, tied to specific evidence you fetched (name the trace, the span,
   the error, the numbers).
3. **Suggested next action.**

Ground every claim in something a tool actually returned. If a query comes back empty, say so
rather than speculating. No em dashes."""


class RiusRca(Template):
    key = "rius_rca"
    title = "Rius RCA"
    description = ("Root-cause analysis for a Rius workspace: alert firings flow in, the agent "
                   "investigates over the Rius MCP server, and the report posts back to Rius.")
    tags = ("partner",)
    # Hidden from the console's template gallery: this template is created by the Rius control
    # plane over the API, not picked by a person browsing cards.
    hidden = True

    PARAMS = {
        "mcp_url": {"type": "string", "required": True, "label": "Rius MCP URL",
                    "help": "the workspace's MCP endpoint the agent reads traces from"},
        "mcp_token": {"type": "string", "required": True, "secret": True, "label": "MCP token",
                      "help": "read-only, workspace-scoped; sent as Authorization: Bearer"},
        "callback_url": {"type": "string", "required": True, "label": "Callback URL",
                         "help": "where the finding is POSTed when a run concludes"},
        "callback_token": {"type": "string", "required": True, "secret": True,
                           "label": "Callback token",
                           "help": "bearer token for the callback POST"},
        "budget_usd": {"type": "number", "default": None, "label": "Budget (USD)",
                       "help": "lifetime spend cap for the agent; empty = no cap"},
        "max_rounds": {"type": "number", "default": 10, "label": "Max rounds",
                       "help": "model rounds per run; a real MCP investigation uses 3 to 5"},
        "model": {"type": "string", "default": "", "label": "Model",
                  "help": "empty = the instance default"},
        "prompt": {"type": "string", "default": "", "label": "Prompt override",
                   "help": "replaces the built-in RCA prompt when set"},
    }

    def validate(self, params: dict) -> dict:
        p = super().validate(params)
        for k in ("mcp_url", "callback_url"):
            if not str(p[k]).startswith(("http://", "https://")):
                raise ProjectError(f"{self.key}: {k} must start with http:// or https://")
        if p.get("budget_usd") not in (None, ""):
            p["budget_usd"] = float(p["budget_usd"])
            if p["budget_usd"] <= 0:
                raise ProjectError(f"{self.key}: budget_usd must be positive")
        else:
            p["budget_usd"] = None
        p["max_rounds"] = int(p.get("max_rounds") or 10)
        return p

    def plan(self, params: list) -> list[PlannedObject]:
        p = self.validate(params)
        objs = [
            PlannedObject("source", "alerts", {
                "name": SOURCE, "connector": "webhook", "poll": "5s",
                "config": {
                    "event_type": "alert_firing",
                    "text_template": TEXT_TEMPLATE,
                    # The primary label stamps the stored key at ingest, the axis the trigger
                    # cools down per and the `key` the callback carries back to Rius.
                    "labels": [
                        {"name": "service", "field": "service", "primary": True},
                        {"name": "delivery_id", "field": "delivery_id"},
                        {"name": "workspace_id", "field": "workspace_id"},
                        {"name": "alert_id", "field": "alert_id"},
                        {"name": "rule", "field": "rule"},
                    ],
                }}),
            PlannedObject("view", "view", {
                "name": VIEW, "key_field": "service", "sources": [SOURCE]}),
            # one investigation per service per 5 minutes: a service that keeps alerting does
            # not wake the agent again until the cooldown is over (TR-285)
            PlannedObject("trigger", "trigger", {
                "name": TRIGGER, "view": VIEW,
                "condition": {"aggregate": "count", "predicate": "> 0", "window": "5m"},
                "cooldown": "5m"}),
            PlannedObject("mcp_server", "mcp", {
                "name": MCP, "url": p["mcp_url"], "auth_header": "Authorization",
                "auth_value": f"Bearer {p['mcp_token']}"}),
        ]
        agent = {"name": AGENT, "trigger": TRIGGER,
                 "prompt": p.get("prompt") or PROMPT,
                 "mcp_servers": [MCP],
                 "webhook_url": p["callback_url"], "webhook_token": p["callback_token"],
                 "max_rounds": p["max_rounds"], "enabled": True}
        if p.get("budget_usd") is not None:
            agent["budget_usd"] = p["budget_usd"]
        if p.get("model"):
            agent["model"] = p["model"]
        objs.append(PlannedObject("agent", "agent", agent))
        return objs

    def summary(self, instance: dict, store) -> dict:
        out = super().summary(instance, store)
        params = instance.get("params") or {}
        ingest = next((s.get("ingest_key") for s in store.list_catalog_sources()
                       if s.get("name") == SOURCE), None)
        out["panels"] = [{
            "title": "Wiring",
            "rows": [
                {"label": "alerts land at", "value": f"/ingest/{ingest}" if ingest else "unknown",
                 "mono": True},
                {"label": "agent reads", "value": params.get("mcp_url", ""), "mono": True},
                {"label": "reports go to", "value": params.get("callback_url", ""), "mono": True},
            ]}]
        return out


register(RiusRca())
