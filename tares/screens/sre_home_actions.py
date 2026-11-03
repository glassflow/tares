"""POST /api/home/actions/{action} on an AI SRE cell (TR-748): the buttons on the home page.

One action so far. raise_budget adds money to the watcher's budget and turns the watcher back on,
so the page's "Add $25 and restart" is one click. Other names answer 404. The route is a write,
so a read key gets 403 from the daemon's own scope check.
"""
import asyncio

from fastapi import Request
from fastapi.responses import JSONResponse

from .. import edition as edition_mod
from .. import providers, sre, sre_home, watch

MAX_AMOUNT_USD = 10000


def register(app, store, h, ctx):
    runtime = ctx.runtime

    def raise_budget(body: dict, person: str, start_who: str):
        try:
            amount = float(body.get("amount_usd", sre_home.RAISE_STEP_USD))
        except (TypeError, ValueError):
            return h.err(ValueError("amount_usd must be a number"), 400)
        if not 0 < amount <= MAX_AMOUNT_USD:
            return h.err(ValueError(f"amount_usd must be more than 0 and at most {MAX_AMOUNT_USD}"), 400)
        found = sre.roles(store, runtime.catalog, include_paused=True)["watchers"]
        if not found:
            return h.err(KeyError("no watcher is set up"), 404)
        # the watcher that stopped for its budget, else the first one
        why = watch.judge(store, runtime.catalog, None, found)
        pick = next((w for w in found
                     if why.get((w["agent"], w["project"], w["trigger"]), {}).get("reason") == "budget_used"),
                    found[0])
        name, uid = pick["agent"], pick["project"]
        agent = store.get_catalog_agent(name, project=uid)
        if agent is None:
            return h.err(KeyError(f"unknown agent {name!r}"), 404)
        if providers.resolve_for_agent(store, agent)[0] is None:
            return h.err(ValueError("no AI model is set up for the watcher, so it cannot restart"), 400)
        # the trigger is the maker's wiring: a project that only uses the agent leaves it alone,
        # as PUT /api/agents/builtin/{name} does
        trigger = (store.get_catalog_agent(name) or agent)["trigger"]
        budget = round(float(agent.get("budget_usd") or 0) + amount, 2)
        # upsert blanks a field it is given as None, so every stored field goes back as it was;
        # handoffs, concludes, verdicts and github stay as stored when passed as None
        store.upsert_catalog_agent(
            name, trigger, agent["prompt"], agent.get("slack_webhook", ""),
            agent.get("model", ""), agent.get("slack_channel", ""), agent.get("webhook_url", ""),
            agent.get("webhook_token", ""), agent.get("mcp_servers") or [], agent.get("max_rounds"),
            budget, webhook_key_label=agent.get("webhook_key_label", ""),
            provider=agent.get("provider", ""), daily_cap=agent.get("daily_cap"),
            handoffs=None, concludes=None, verdicts=None, github=None,
            budget_period=agent.get("budget_period") or "lifetime")
        store.mark_customized("agent", name)
        store.set_agent_enabled(name, True, project=uid)
        runtime.reload_catalog()
        watch.sync(store, runtime.catalog)
        sre_home.clear_cache()
        return {"ok": True, "home": sre_home.build(store, runtime.catalog, runtime.health_snapshot(),
                                                      person=person, start_who=start_who)}

    @app.post("/api/home/actions/{action}")
    async def home_action(action: str, request: Request):
        if action != "raise_budget" or edition_mod.edition(store)[0] != "sre":
            return JSONResponse({"error": "no such action"}, status_code=404)
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        return await asyncio.to_thread(raise_budget, body, h.by(request) or "console",
                                       h.start_who(request))
