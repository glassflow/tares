"""The AI SRE Services page and a page per service (TR-750).

GET /api/sre/services              every service as one row (?hidden=1 includes the hidden ones)
GET /api/sre/services/{name}       one service: chart, incidents, what the watcher raised, blind
                                   spots, linked repos and its settings (?window=1h|24h|7d)
PUT /api/sre/services/{name}/settings   {display_name, hidden, merged_into, repos}, any of them;
                                   answers the service's page. A write, so a read key gets 403 from
                                   the daemon's own scope check.

A row's state and the words under it come from `sre_home.service_row`, the one place that decides
them, so the home page and this page cannot disagree. The reads are the home page's reads, once.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from fastapi import Body, HTTPException, Request

from .. import goal, incidents, sre, sre_home, watch
from ..envelope import now_utc

WINDOWS = ("1h", "24h", "7d")
INCIDENTS_SHOWN = 10
RAISED_SHOWN = 8
RAISED_FETCH = 100          # what goal.cell_results returns at most
DEPLOYS_SHOWN = 50
WORTH = {"raised": "Worth waking someone", "investigated": "Worth a look",
         "note_written": "For the record"}
NAME_MATCH = "Matched by name"
LINKED_IN_SETTINGS = "Linked in settings"
SETTINGS_FIELDS = {"display_name": "display", "hidden": "hidden", "merged_into": "merged_into",
                   "repos": "repos"}


def _count_words(n: int, one: str, many: str | None = None) -> str:
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def _context(store, catalog, now, include_hidden: bool, hide=()) -> dict:
    """What the home page reads before it words a service, read once."""
    t = now or now_utc()
    items = sre.list_services(store, catalog, now, include_hidden=include_hidden, hide=hide)
    state = watch.compute(store, catalog, now)
    roles = sre.roles(store, catalog, include_paused=True, hide=hide)
    why = watch.judge(store, catalog, t, roles["watchers"])
    pairs = sre_home._watchers(store, catalog, roles, why, t)
    spots = sre.blind_spots(store, catalog, now, hide)
    active = store.list_incidents("active", limit=50, hide=hide)
    return {
        "t": t, "items": items, "state": state, "spots": spots, "pairs": pairs,
        "blocks": {b["agent"]: b for _w, b in pairs},
        "last_by_agent": {b["agent"]: b["last_check_at"] for _w, b in pairs},
        "inc_views": [{"service": i["service"], "state": i["state"],
                       "who": ({"name": i.get("owner_name")} if i["state"] == "on_it" else None)}
                      for i in active]}


def _watch_row(ctx: dict, name: str):
    row = ctx["state"].get(name)
    if row is not None:
        row = {**row, "last_check_at": ctx["last_by_agent"].get(row.get("agent"))}
    return row


def _watch_words(store, ctx: dict, name: str, hidden: bool = False) -> str:
    if hidden:
        return "Not checked, it is hidden"
    row = ctx["state"].get(name)
    if row is None:
        return "Nothing to watch, it has no events of its own"
    if row["watched"]:
        every = (ctx["blocks"].get(row.get("agent")) or {}).get("every_words")
        return "Watched " + every if every else "Watched"
    agent = store.get_catalog_agent(row["agent"], project=row.get("project")) if row.get("agent") else None
    why = watch.reason_words(row["reason"], agent, store, ctx["t"])
    return sre_home._up(f"Not watched, {why}")


def _feeds_words(item: dict) -> str:
    names = sorted({f["name"] for f in item.get("feeds") or []})
    return "from " + goal.join_words(names) if names else "no feeds yet"


def _row(store, ctx: dict, item: dict, errors: list) -> dict:
    name = item["name"]
    row = sre_home.service_row(item, _watch_row(ctx, name), ctx["spots"], ctx["inc_views"],
                               errors, sre_home._spark(errors), ctx["t"])
    state = ctx["state"].get(name)
    if item.get("hidden"):
        row["last_words"] = "not checked, it is hidden"
    return {**row, "watched": None if state is None else bool(state["watched"]),
            "watch_words": _watch_words(store, ctx, name, bool(item.get("hidden"))), "feeds_words": _feeds_words(item),
            "events_24h_words": _count_words(item["events_24h"], "event") + " in 24 hours",
            "hidden": bool(item.get("hidden"))}


# the order the ticket asks for: what needs a look first, then by name
STATE_ORDER = ("incident", "not_watched", "unusual", "silent", "no_own_signal", "normal")


def _rank(row: dict) -> tuple:
    state = row["state"]
    return (STATE_ORDER.index(state) if state in STATE_ORDER else len(STATE_ORDER), row["name"])


def services_screen(store, catalog, now=None, include_hidden: bool = False, hide=()) -> dict:
    """The Services page: one row per service, what needs a look first. Hidden ones are left out and
    counted, or listed with hidden true when `include_hidden`."""
    ctx = _context(store, catalog, now, True, hide)
    everything = ctx["items"]
    shown = everything if include_hidden else [i for i in everything if not i.get("hidden")]
    errors = sre_home._errors_24h(store, [i["name"] for i in shown], ctx["t"], now)
    merged = [f"{goal.join_words(i['merged'])} {'is' if len(i['merged']) == 1 else 'are'} shown as part of {i['name']}"
              for i in sorted(shown, key=lambda i: i["name"]) if i["merged"]]
    rows = sorted((_row(store, ctx, i, errors.get(i["name"])) for i in shown), key=_rank)
    return {"services": rows,
            "hidden_count": sum(1 for i in everything if i.get("hidden")),
            "merged_words": ". ".join(merged) if merged else None}


# ── one service ──────────────────────────────────────────────────────────────
def _chart(store, catalog, name: str, window: str, t, hide=()) -> dict:
    since, until, bucket_s = sre.series_for(window, t)
    gone = sre.hidden_sources(store, hide)
    left_out = {"skip": gone} if gone else {}
    traffic = [n for _at, n in store.event_series(name, since, until, bucket_s, **left_out)]
    errors = [n for _at, n in store.event_series(name, since, until, bucket_s, errors_only=True,
                                                 **left_out)]
    # an error is an event: never more errors than traffic in a bucket
    errors = [min(e, a) for e, a in zip(errors, traffic)]
    marks = sre.deploys(store, catalog, name, since, limit=DEPLOYS_SHOWN, hide=hide)
    return {"window": window, "since": sre._iso(since), "until": sre._iso(until),
            "bucket_seconds": bucket_s, "errors": errors, "traffic": traffic,
            "deploys": [{"at": d["at"], "label": sre_home._deploy_label(d), "repo": d.get("repo")}
                        for d in marks]}


def _incidents(store, catalog, names: list, settings: dict, t, hide=()) -> tuple:
    """(the newest INCIDENTS_SHOWN as the page shows them, how many in all, how many still open)."""
    found: list = []
    total = open_ = 0
    for n in names:
        counts = store.incident_counts(n, hide=hide)
        total += sum(counts.values())
        open_ += counts["open"] + counts["on_it"]
        found += store.list_incidents(None, n, limit=INCIDENTS_SHOWN, hide=hide)
    found.sort(key=lambda i: goal._aware(i["last_note_at"]), reverse=True)
    found = found[:INCIDENTS_SHOWN]
    notes = store.notes_of([i["id"] for i in found])
    out = []
    for inc in found:
        v = incidents.view(store, inc, notes=notes[inc["id"]], settings=settings, now=t)
        out.append({"id": v["id"], "title": v["title"], "state": v["state"],
                    "state_words": v["state_words"], "opened_words": v["age_words"]})
    return out, total, open_


def _raised(store, catalog, name: str, t, hide=()) -> tuple:
    scheduled = {x.name for x in catalog.triggers if getattr(x.condition, "every", None)}
    found = goal.cell_results(store, scheduled, limit=RAISED_FETCH, show="worth", service=name,
                              now=t, hide=hide)["findings"]
    out = [{"id": r["id"], "when_words": sre_home._when_words(r["at"], t),
            "what": r["title_parts"]["what"], "outcome_words": r["outcome_words"],
            "worth": WORTH.get(r["outcome"], "For the record")} for r in found[:RAISED_SHOWN]]
    return out, len(found)


def _repos(store, catalog, name: str, settings: dict, item: dict, t, hide=()) -> list:
    linked = sre._linked_repos(settings, name)
    if not linked:
        return []
    last = sre.github_repos(store, catalog, hide)
    own = {name, *item.get("merged", [])}
    out = []
    for repo in linked:
        sent = last.get(repo)
        out.append({"name": repo,
                    "linked_by": NAME_MATCH if repo.rsplit("/", 1)[-1] in own else LINKED_IN_SETTINGS,
                    "linked_words": ("Last event " + goal.age_words((t - goal._aware(sent)).total_seconds())
                                     if sent else "Nothing received yet")})
    return out


def _blind_words(item: dict, spots: list, watch_row) -> str:
    if item.get("hidden"):
        return "Not checked while the service is hidden"
    if spots:
        return _count_words(len(spots), "blind spot") + " found"
    if watch_row is not None and not watch_row.get("last_check_at"):
        return "Not checked yet"
    return "No blind spots found"


def service_screen(store, catalog, name: str, window: str = "24h", now=None, hide=()) -> dict | None:
    """One service's page, None when the cell knows no such service. A name merged into another
    answers with the service it is merged into. ValueError for a window that is not 1h, 24h or 7d."""
    if window not in WINDOWS:
        raise ValueError(f"no window {window!r}: use one of {', '.join(WINDOWS)}")
    found = sre.get_service(store, catalog, name, now, hide)
    if found is None:
        return None
    name = found["name"]
    ctx = _context(store, catalog, now, True, hide)
    t = ctx["t"]
    item = next((i for i in ctx["items"] if i["name"] == name), None) or found
    settings = store.service_settings()
    errors = sre_home._errors_24h(store, [name], t, now)
    row = _row(store, ctx, item, errors.get(name))
    mine = [s for s in ctx["spots"] if s.get("service") == name]
    names = [name, *item.get("merged", [])]
    shown, total, open_ = _incidents(store, catalog, names, settings, t, hide)
    raised, raised_total = _raised(store, catalog, name, t, hide)
    own = settings.get(name) or {}
    options = []
    for other in ctx["items"]:
        if other["name"] == name or other.get("hidden"):
            continue
        try:
            sre.check_merge(settings, name, other["name"])
        except ValueError:
            continue
        options.append(other["name"])
    return {
        "name": name, "about": row["about"], "state": row["state"], "state_words": row["state_words"],
        "hidden": row["hidden"], "watched": row["watched"], "watch_words": row["watch_words"],
        "signals_words": row["signals_words"], "feeds_words": row["feeds_words"],
        "last_words": row["last_words"], "events_24h_words": row["events_24h_words"],
        "chart": _chart(store, catalog, name, window, t, hide),
        "incidents": shown, "incidents_total": total, "incidents_open": open_,
        "raised": raised, "raised_total": raised_total,
        "blind_spots_words": _blind_words(item, mine, _watch_row(ctx, name)),
        "blind_spots": [{"kind": s["kind"], "title": s["title"], "detail": s["detail"],
                         "action": {"label": s["fix"]["label"],
                                    "to": sre_home.NOTES_PLACE.get(s["fix"]["action"], "/settings")}}
                        for s in mine],
        "repos": _repos(store, catalog, name, settings, item, t, hide),
        "settings": {"display_name": own.get("display") or "", "hidden": bool(own.get("hidden")),
                     "merged_into": own.get("merged_into"), "merged_names": list(item.get("merged", [])),
                     "merge_options": options,
                     "repo_options": sorted(sre.github_repos(store, catalog, hide)),
                     "updated_by": own.get("updated_by")}}


def register(app, store, h, ctx):
    runtime = ctx.runtime

    @app.get("/api/sre/services")
    async def sre_services(hidden: int = 0):
        return await asyncio.to_thread(services_screen, store, runtime.catalog, None, bool(hidden),
                                     h.hidden_ids())

    # the settings route is declared first: {name:path} would take a PUT for a name with a "/" in it
    @app.put("/api/sre/services/{name:path}/settings")
    async def sre_service_settings(name: str, request: Request, body: Any = Body(...)):
        """{display_name?, hidden?, merged_into?, repos?}: a field left out stays, null clears it."""
        return await _save_settings(name, request, body)

    @app.get("/api/sre/services/{name:path}")
    async def sre_service(name: str, window: str = "24h"):
        try:
            body = await asyncio.to_thread(service_screen, store, runtime.catalog, name, window,
                                          None, h.hidden_ids())
        except ValueError as e:
            return h.err(e, 400)
        if body is None:
            raise HTTPException(404, f"No service called {name}")
        return body

    async def _save_settings(name: str, request: Request, body: Any):
        if not isinstance(body, dict):
            return h.err(ValueError("the body must be a JSON object such as {\"hidden\": true}"), 400)
        unknown = sorted(set(body) - set(SETTINGS_FIELDS))
        if unknown:
            return h.err(ValueError(f"cannot change {', '.join(unknown)}: the settings are "
                                    + ", ".join(SETTINGS_FIELDS)), 400)
        who = h.by(request) or "console"
        hide = h.hidden_ids()

        def write():
            try:
                sre.patch_service(store, runtime.catalog, name,
                                  {SETTINGS_FIELDS[k]: v for k, v in body.items()}, who)
            except LookupError:
                raise HTTPException(404, f"No service called {name}")
            except ValueError as e:
                return h.err(e, 400)
            sre_home.clear_cache()
            return service_screen(store, runtime.catalog, name, hide=hide)
        return await asyncio.to_thread(write)
