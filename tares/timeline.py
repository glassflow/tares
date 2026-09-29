"""The project timeline (TR-331): everything that happened in one project, as threads.

A thread starts with a firing (a trigger fired, and the runs it woke) or with a run no firing woke
(manual, bootstrap). Inside it, runs nest what they led to: a rerun sits under the run it repeats,
a handoff under the run that handed off, and a firing tripped by a run's finding under that run,
with the runs it woke in turn. So one incident reads as one thread, however many agents it took.

The store answers indexed lookups only (roots of a project, runs by firing, runs and firings by
parent run); the shape is put together here. Filters match anywhere in a thread, so they are
applied after building: roots are read in pages until enough threads match.
"""
from __future__ import annotations

from datetime import datetime

from .config import agent_name_from_url, slack_channel_from_url

PAYLOAD_EXCERPT = 600
FINDING_MAX = 4000
MAX_DEPTH = 8            # nesting levels followed below a root; a chain deeper than this is cut
SCAN_CAP = 5000          # roots examined per request when filters are set
OUTCOMES = ("finding", "no_op", "failed", "capped", "empty", "exhausted", "running", "none")


def _delivery(d: dict) -> dict:
    url = d.get("url") or ""
    agent = agent_name_from_url(url)
    channel = slack_channel_from_url(url)
    if agent is not None:
        kind, target = "tares", agent
    elif channel is not None:
        kind, target = "slack", channel
    else:
        kind, target = "webhook", url
    return {"kind": kind, "target": target, "ok": d.get("ok"), "error": d.get("error"),
            "at": d.get("delivered_at")}


def _run_item(r: dict) -> dict:
    finding = r.get("finding")
    return {**r, "finding": finding[:FINDING_MAX] if finding else finding,
            "children": [], "firings": []}


def _fired(d: dict, scheduled: set, runs: list) -> dict:
    woken = {r.get("woken_by") for r in runs}
    kind = ("schedule" if "schedule" in woken or ("trigger" not in woken
                                                 and d["trigger"] in scheduled)
            else "condition")
    return {"dispatch_id": d["dispatch_id"], "trigger": d["trigger"], "kind": kind,
            "key": d["key"], "fired_at": d["fired_at"],
            "payload_excerpt": (d.get("payload") or "")[:PAYLOAD_EXCERPT],
            "subscribers": d.get("subscribers")}


def _build(store, project: str, roots: list, scheduled: set) -> list[dict]:
    """Threads for `roots` [(kind, id, at)], in the same order."""
    firing_ids = [i for k, i, _ in roots if k == "firing"]
    run_ids = [i for k, i, _ in roots if k == "run"]
    firings = {d["dispatch_id"]: d for d in store.dispatches_where("dispatch_id", firing_ids)}
    root_runs = {r["id"]: _run_item(r) for r in store.runs_where("id", run_ids)}

    # Walk down level by level: a firing's runs; a run's children (reruns, handoffs) and the
    # firings its finding tripped. Every lookup is one indexed query per level, not per item.
    threads_of: dict[str, dict] = {}          # dispatch_id -> thread (roots and nested)
    all_dispatch_ids: list[str] = []

    def firing_thread(d: dict) -> dict:
        t = {"id": d["dispatch_id"], "kind": "firing", "at": d["fired_at"],
             "trigger": d["trigger"], "entity": d["key"], "fired": None, "runs": [],
             "deliveries": [], "_d": d}
        threads_of[d["dispatch_id"]] = t
        all_dispatch_ids.append(d["dispatch_id"])
        return t

    out = []
    for kind, rid, _at in roots:
        if kind == "firing" and rid in firings:
            out.append(firing_thread(firings[rid]))
        elif kind == "run" and rid in root_runs:
            r = root_runs[rid]
            out.append({"id": r["id"], "kind": "run", "at": r["started_at"],
                        "trigger": r.get("trigger"), "entity": r.get("key"), "runs": [r],
                        "deliveries": []})

    pending_firings = list(threads_of.values())
    pending_runs = list(root_runs.values())
    seen_runs = set(root_runs)
    for _level in range(MAX_DEPTH):
        if pending_firings:
            ids = [t["id"] for t in pending_firings]
            for r in store.runs_where("dispatch_id", ids):
                if r["id"] in seen_runs:
                    continue
                seen_runs.add(r["id"])
                item = _run_item(r)
                threads_of[r["dispatch_id"]]["runs"].append(item)
                pending_runs.append(item)
        pending_firings = []
        if not pending_runs:
            break
        by_id = {r["id"]: r for r in pending_runs}
        ids = list(by_id)
        next_runs = []
        for r in store.runs_where("parent_run_id", ids, project=project):
            if r["id"] in seen_runs or r.get("dispatch_id"):
                continue
            seen_runs.add(r["id"])
            item = _run_item(r)
            by_id[r["parent_run_id"]]["children"].append(item)
            next_runs.append(item)
        for d in store.dispatches_where("parent_run_id", ids, project=project):
            if d["dispatch_id"] in threads_of:
                continue
            t = firing_thread(d)
            by_id[d["parent_run_id"]]["firings"].append(t)
            pending_firings.append(t)
        pending_runs = next_runs

    deliveries = store.deliveries_for_many(all_dispatch_ids)
    for did, t in threads_of.items():
        d = t.pop("_d")
        t["fired"] = _fired(d, scheduled, t["runs"])
        t["deliveries"] = [_delivery(x) for x in deliveries.get(did, [])]
    return out


def _runs_in(thread: dict):
    """Every run of a thread, nested ones included."""
    stack = list(thread["runs"])
    while stack:
        r = stack.pop()
        yield r
        stack.extend(r["children"])
        for t in r["firings"]:
            stack.extend(t["runs"])


def _firings_in(thread: dict):
    if thread["kind"] == "firing":
        yield thread
    for r in _runs_in(thread):
        yield from r["firings"]


def _matches(thread: dict, trigger: str, agent: str, outcome: str, entity: str) -> bool:
    runs = list(_runs_in(thread)) if (agent or outcome or entity) else []
    if trigger and not (thread.get("trigger") == trigger
                        or any(t["trigger"] == trigger for t in _firings_in(thread))):
        return False
    if agent and not any(r["agent"] == agent for r in runs):
        return False
    if entity and not (thread.get("entity") == entity or any(r.get("key") == entity for r in runs)):
        return False
    if outcome == "none":
        return thread["kind"] == "firing" and not thread["runs"]
    if outcome and not any(_outcome(r) == outcome for r in runs):
        return False
    return True


def _outcome(r: dict) -> str:
    """finding or no_op for a run that concluded, else its status. A run from before outcomes
    were recorded that ended ok with text counts as a finding."""
    if r.get("status") == "ok":
        return r.get("outcome") or ("finding" if r.get("finding") else "no_op")
    return r.get("status") or ""


def project_timeline(store, project: str, limit: int = 50, before: datetime | None = None,
                     trigger: str = "", agent: str = "", outcome: str = "", entity: str = "",
                     scheduled: set | None = None) -> dict:
    """{threads, next_before}: up to `limit` threads older than `before`, newest first.
    `next_before` is the `before` of the next page, None when there is nothing older."""
    limit = max(1, min(int(limit), 200))
    filtered = bool(trigger or agent or outcome or entity)
    chunk = max(limit, 200) if filtered else limit
    scheduled = scheduled or set()
    threads: list[dict] = []
    cursor = before
    scanned = 0
    more = True
    while len(threads) < limit and scanned < SCAN_CAP:
        roots = store.timeline_roots(project, cursor, chunk)
        if not roots:
            more = False
            break
        built = {t["id"]: t for t in _build(store, project, roots, scheduled)}
        full = True
        for _kind, rid, at in roots:
            scanned += 1
            cursor = at
            t = built.get(rid)
            if t is not None and (not filtered or _matches(t, trigger, agent, outcome, entity)):
                threads.append(t)
                if len(threads) >= limit:
                    full = False
                    break
        if full and len(roots) < chunk:
            more = False
            break
    return {"threads": threads,
            "next_before": cursor.isoformat() if more and cursor is not None else None}
