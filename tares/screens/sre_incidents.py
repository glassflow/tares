"""The AI SRE Incidents screens (TR-751): GET /api/sre/incidents, GET /api/sre/incidents/{id} and
POST /api/sre/incidents/{id}/{action}.

The list holds the incidents by state, the page of one incident holds its note, the evidence the
agents read, the chart with deploys, the Slack thread, who was on it and "Was this worth raising?".
Each GET answers a whole screen in one read, and each POST answers the screen it changed, so the
page never calls /api/incidents* or /api/findings/* itself. The state changes go through the same
functions the engine routes call (incidents.take, resolve, reopen: they also tell Slack, TR-762) and
the worth answer through feedback.answer.

Words: state, time and who are worded here, for people. The evidence comes from goal.steps_of, which
the platform console shows too and which says "trigger" and "run"; the words the edition list
forbids are turned into plain ones here and nowhere else. The note and the agent's own fields
(failing, likely_cause, next_step) are the agent's words and are shown as they are.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import HTTPException, Request

from .. import feedback, goal, incidents, sre, words
from .. import slack as slack_mod
from .. import sre_home

log = logging.getLogger(__name__)

ACTIONS = ("on_it", "resolve", "reopen", "worth_yes", "worth_no")
STATE_ACTIONS = {"on_it": incidents.take, "resolve": incidents.resolve, "reopen": incidents.reopen}
REFUSED = {   # a state change the incident's state does not allow -> what the person is told
    ("open", "reopen"): "This incident is already open.",
    ("on_it", "on_it"): "Someone is already on this incident.",
    ("resolved", "on_it"): "This incident is resolved now; reopen it first.",
    ("resolved", "resolve"): "This incident is already resolved.",
}
GROUPS = (("open", "Open"), ("on_it", "Someone on it"), ("resolved", "Resolved"))
RESOLVED_PAGE = 20          # resolved incidents on a page of the list
ACTIVE_CAP = 200            # open and on_it incidents the list holds
JOINED_MAX = 50
EVIDENCE_MAX = 200
CHART_POINTS = 48
CHART_LEAD = timedelta(hours=2)    # the chart starts this long before the incident opened
CHART_TAIL = timedelta(hours=1)    # ... and runs this long after it was resolved
CHART_MAX = timedelta(days=7)
DEPLOYS_MAX = 20
SLACK_TIMEOUT = 3.0         # seconds to wait for Slack on a read: the page must not hang on it
SLACK_RETRY = 60.0          # seconds before a link Slack could not give is asked for again

_slack_failed: dict = {}    # (channel, ts) or (channel,) -> when to ask again
_channel_names: dict = {}   # channel id -> "#alerts"


# ── plain words ──────────────────────────────────────────────────────────────
def _plain_table() -> list:
    """[(pattern, replacement)] for each word the edition list forbids."""
    plural = {"source": "connections", "view": "pages", "trigger": "check rules", "run": "checks",
              "dispatch": "alerts sent", "firing": "alerts sent", "delivery": "alerts sent",
              "wiring": "setups", "catalog": "connections", "entity": "services",
              "ingest": "receives", "station": "crew members"}
    single = {"source": "connection", "view": "page", "trigger": "check rule", "run": "check",
              "dispatch": "alert sent", "firing": "alert sent", "delivery": "alert sent",
              "wiring": "setup", "catalog": "connections", "entity": "service",
              "ingest": "receive", "station": "crew member"}
    out = []
    for e in words.load()["forbidden"]:
        w = e["word"]
        if w not in single:
            continue
        for found, to in ((e["plural"], plural[w]), (w, single[w])):
            out.append((re.compile(rf"(?<![A-Za-z0-9_]){re.escape(found)}(?![A-Za-z0-9_])", re.I), to))
    return out


_PLAIN = _plain_table()


def plain(text) -> str:
    """The text with each word the edition list forbids turned into its plain word, and no
    dashes. Phrases the list allows ("open source") stay as they are."""
    text = goal._no_dash(str(text or "")) or ""
    if not words.offenders(text):
        return text
    keep: list[str] = []

    def hold(m):
        keep.append(m.group(0))
        return f"{len(keep) - 1}"

    for e in words.load()["forbidden"]:
        for a in e["allow"]:
            text = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(' '.join(a.split()))}(?![A-Za-z0-9_])",
                              re.I).sub(hold, text)

    def swap(to):
        def f(m):
            return to[:1].upper() + to[1:] if m.group(0)[:1].isupper() else to
        return f

    for pat, to in _PLAIN:
        text = pat.sub(swap(to), text)
    return re.sub("(\\d+)", lambda m: keep[int(m.group(1))], text)


# ── words for time, state and people ─────────────────────────────────────────
def _aware(dt):
    return incidents._aware(dt)


def _since(dt, now) -> str:
    """"14:20" for today, "yesterday, 14:20", else "27 Oct, 14:20" (UTC: the cell does not know
    the viewer's time zone)."""
    return goal._since_words(dt, now)


def state_words(inc: dict, now) -> str:
    """The state in a short phrase: "open 38 minutes, nobody on it", "Alex is on it since 14:20",
    "resolved itself after 12 minutes", "resolved by Alex, 2 days ago"."""
    if inc["state"] == "on_it":
        who = inc.get("owner_name") or inc.get("owner") or "Someone"
        return f"{who} is on it since {_since(inc['owner_at'] or inc['opened_at'], now)}"
    if inc["state"] == "resolved":
        how = inc.get("resolution")
        if how == "before_tracking":
            return "closed, from before incidents were tracked"
        end = _aware(inc["resolved_at"] or now)
        if how == "itself":
            return f"resolved itself after {goal.span_words((end - _aware(inc['opened_at'])).total_seconds())}"
        ago = goal.age_words((_aware(now) - end).total_seconds())
        by = inc.get("resolved_by_name")
        return f"resolved by {by}, {ago}" if by else f"resolved, {ago}"
    return f"open {goal.span_words((_aware(now) - _aware(inc['opened_at'])).total_seconds())}, nobody on it"


def resolved_words(inc: dict, now) -> str:
    """"Resolved by Alex", "Resolved itself after 12 minutes", "Closed: from before incidents
    were tracked"."""
    how = inc.get("resolution")
    if how == "before_tracking":
        return "Closed: from before incidents were tracked"
    if how == "itself":
        end = _aware(inc["resolved_at"] or now)
        return f"Resolved itself after {goal.span_words((end - _aware(inc['opened_at'])).total_seconds())}"
    by = inc.get("resolved_by_name")
    return f"Resolved by {by}" if by else "Resolved"


def who_of(inc: dict) -> str | None:
    """Who the incident is with: the one on it, or the one who resolved it."""
    if inc["state"] == "on_it":
        return inc.get("owner_name") or inc.get("owner") or "Someone"
    if inc["state"] == "resolved" and inc.get("resolution") == "person":
        return inc.get("resolved_by_name") or None
    return None


def actions_of(inc: dict) -> list[str]:
    """The console actions that apply in the incident's state."""
    return {"open": ["on_it", "resolve"], "on_it": ["resolve", "reopen"],
            "resolved": ["reopen"]}.get(inc["state"], [])


# ── Slack ────────────────────────────────────────────────────────────────────
def _ask_slack(store, method: str, params: dict) -> dict | None:
    """One Slack Web API read, or None: no token, Slack unreachable or slow, or it said no.
    Never raises."""
    try:
        token, _origin = slack_mod.resolve_token(store)
        if not token:
            return None
        r = httpx.get(f"{slack_mod.API_BASE}/{method}", params=params,
                      headers={"authorization": f"Bearer {token}"}, timeout=SLACK_TIMEOUT)
        data = r.json()
        return data if isinstance(data, dict) and data.get("ok") else None
    except Exception:
        return None


def _channel_name(store, channel: str) -> str:
    """"#alerts" for a channel id, "Slack" when Slack does not say."""
    if channel in _channel_names:
        return _channel_names[channel]
    if _slack_failed.get((channel,), 0) > time.monotonic():
        return "Slack"
    data = _ask_slack(store, "conversations.info", {"channel": channel})
    name = ((data or {}).get("channel") or {}).get("name")
    if not name:
        _slack_failed[(channel,)] = time.monotonic() + SLACK_RETRY
        return "Slack"
    _channel_names[channel] = "#" + str(name)
    return _channel_names[channel]


def slack_of(store, inc: dict, notes: list[dict]) -> dict | None:
    """{"where", "url"} for the incident's Slack message, or None when nothing was posted (a note
    posted before TR-744 has no ts). The link is asked of Slack on the first read and kept in
    incidents.slack_url; when Slack cannot give it the url is null and the next read tries again
    (after a minute)."""
    spot = inc.get("slack") or next(
        ({"channel": n["slack_channel"], "ts": n["slack_ts"]} for n in notes
         if n.get("slack_channel") and n.get("slack_ts")), None)
    if not spot or not spot.get("channel") or not spot.get("ts"):
        return None
    channel, ts = str(spot["channel"]), str(spot["ts"])
    url = inc.get("slack_url")
    if not url and _slack_failed.get((channel, ts), 0) <= time.monotonic():
        data = _ask_slack(store, "chat.getPermalink", {"channel": channel, "message_ts": ts})
        url = (data or {}).get("permalink") or None
        if url:
            try:
                store.set_incident_slack_url(inc["id"], url)
            except Exception:
                pass
        else:
            _slack_failed[(channel, ts)] = time.monotonic() + SLACK_RETRY
    return {"where": _channel_name(store, channel), "url": url}


# ── the screens ──────────────────────────────────────────────────────────────
def register(app, store, h, ctx):
    def catalog():
        return ctx.runtime.catalog

    def scheduled() -> set:
        return {t.name for t in catalog().triggers if getattr(t.condition, "every", None)}

    def person(request: Request) -> str:
        return h.by(request) or "console"

    def not_found():
        return HTTPException(404, "No incident with that id")

    def service_filter(value: str, settings: dict) -> str:
        """The incident service key a `service` filter names (its key or its display name). It
        reads every key: a private project's service resolves, and the lists it feeds leave its
        incidents out."""
        want = (value or "").strip().lower()
        if not want:
            return ""
        for s in store.incident_services():
            if want in (s.lower(), goal.service_name(s, settings).lower()):
                return s
        return want

    def item_of(inc: dict, view: dict, now) -> dict:
        return {"id": inc["id"], "incident": True, "title": view["title"], "service": view["service"],
                "state": inc["state"], "state_words": state_words(inc, now),
                "opened_at": view["opened_at"],
                "when_words": goal.age_words((now - _aware(inc["opened_at"])).total_seconds()),
                "who": who_of(inc)}

    def raised_findings(now, settings, hidden=()) -> tuple[list[dict], dict]:
        """(findings raised that did not become an incident, the quiet checks left out)."""
        try:
            out = goal.cell_results(store, scheduled(), limit=20, show="worth", now=now,
                                    hide=hidden)
        except Exception:
            return [], {"count": 0}
        items = []
        for r in out["findings"]:
            if r["outcome"] != "raised" or store.incident_for_run(r["id"]) is not None:
                continue
            at = _aware(r["at"]) if r.get("at") else None
            items.append({"id": r["id"], "incident": False, "title": r["title"],
                          "service": r["title_parts"]["service"], "state": "raised",
                          "state_words": "raised, not an incident", "opened_at": r["at"],
                          "when_words": goal.age_words((now - at).total_seconds()) if at else "",
                          "who": None})
        return items, out["quiet"]

    def quiet_words(quiet: dict) -> str | None:
        n = int((quiet or {}).get("count") or 0)
        if not n:
            return None
        return f"{n:,} quiet check{'' if n == 1 else 's'} today {'is' if n == 1 else 'are'} not shown"

    def incidents_screen(state: str = "", service: str = "", before: str = "", show: str = "",
                         now=None, hidden=()) -> dict:
        if state and state not in ("open", "on_it", "resolved", "all"):
            raise HTTPException(400, "state is open, on_it, resolved or all")
        if show and show != "all":
            raise HTTPException(400, 'show is "all" or left out')
        cut = None
        if before:
            try:
                cut = _aware(datetime.fromisoformat(before.replace("Z", "+00:00")))
            except ValueError:
                raise HTTPException(400, "before must be a time such as 2026-10-01T12:00:00Z")
        now = _aware(now or incidents.clock())
        settings = store.service_settings()
        key = service_filter(service, settings)
        wanted = [s for s, _label in GROUPS if state in ("", "all", s)]
        if cut is not None:   # a later page of the resolved ones only
            wanted = [s for s in wanted if s == "resolved"]
        rows: dict[str, list[dict]] = {}
        next_before = None
        for s in wanted:
            if s == "resolved":
                got = store.list_incidents("resolved", key or None, RESOLVED_PAGE + 1, cut,
                                           hide=hidden)
                if len(got) > RESOLVED_PAGE:   # a 21st exists: there is a next page
                    got = got[:RESOLVED_PAGE]
                    next_before = goal._iso(got[-1]["last_note_at"])
            else:
                got = (store.list_incidents(s, key or None, ACTIVE_CAP, hide=hidden)
                       if cut is None else [])
            rows[s] = got
        every = [i for got in rows.values() for i in got]
        notes = store.notes_of([i["id"] for i in every])
        groups = []
        for s, label in GROUPS:
            if s not in rows:
                continue
            items = [item_of(i, incidents.view(store, i, notes=notes[i["id"]], settings=settings,
                                               now=now), now) for i in rows[s]]
            groups.append({"state": s, "label": label, "items": items})
        quiet = {"count": 0}
        if show == "all" and cut is None:
            found, quiet = raised_findings(now, settings, hidden)
            groups.append({"state": "raised", "label": "Raised, not an incident", "items": found})
        elif cut is None:
            try:
                quiet = goal.cell_results(store, scheduled(), limit=1, show="worth",
                                          now=now)["quiet"]
            except Exception:
                quiet = {"count": 0}
        if show == "all" and cut is None:
            try:
                quiet = goal.cell_results(store, scheduled(), limit=1, show="worth",
                                          now=now)["quiet"]
            except Exception:
                pass
        return {"groups": groups, "counts": store.incident_counts(key or None, hide=hidden),
                "services": sorted({goal.service_name(s, settings)
                                    for s in store.incident_services(hide=hidden)}),
                "next_before": next_before, "quiet_checks_words": quiet_words(quiet)}

    def chart_of(inc: dict, now, settings: dict, hidden=()) -> dict:
        end = _aware(inc["resolved_at"]) + CHART_TAIL if inc["state"] == "resolved" and inc["resolved_at"] else now
        end = min(end, now)
        start = max(_aware(inc["opened_at"]) - CHART_LEAD, end - CHART_MAX)
        if end - start < timedelta(hours=3):
            start = end - timedelta(hours=3)
        bucket = max(60, -(-int((end - start).total_seconds()) // CHART_POINTS // 60) * 60)
        name = goal.service_name(inc["service"], settings)
        try:
            points = store.event_series(inc["service"], start, end, bucket, errors_only=True)
        except Exception:
            log.exception("incident chart: the error series of %s did not load", inc["service"])
            points = []
        try:   # every event of the service, to read the errors against
            traffic = store.event_series(inc["service"], start, end, bucket)
        except Exception:
            log.exception("incident chart: the traffic series of %s did not load", inc["service"])
            traffic = []
        try:
            ds = sre.deploys(store, catalog(), inc["service"], start, DEPLOYS_MAX, hide=hidden)
        except Exception:
            log.exception("incident chart: the deploys of %s did not load", inc["service"])
            ds = []
        marks = [{"at": d["at"], "label": plain(d.get("text") or f"{d.get('kind') or 'deploy'} on {d.get('repo')}")}
                 for d in reversed(ds) if _aware(d["at"]) <= end]
        return {"label": f"Errors from {name}, last {goal.span_words((end - start).total_seconds())}"
                if inc["state"] != "resolved" else f"Errors from {name}",
                "points": [[t, int(n)] for t, n in points],
                "traffic": [[t, int(n)] for t, n in traffic], "deploys": marks}

    def note_parts(inc: dict, run_id: str, run: dict) -> dict:
        """failing, likely_cause, next_step, the steps and whether a person can answer, for the
        note a run wrote: the project's own result when the run sits in it, else read off the run."""
        uid = inc.get("project")
        if uid:
            try:
                d = goal.result_detail(store, catalog(), uid, run_id, scheduled())
                return {"failing": d["failing"], "likely_cause": d["likely_cause"],
                        "next_step": d["next_step"], "steps": d["steps"],
                        "can_answer": bool(d["can_answer"])}
            except KeyError:
                pass
        parts = goal.derive_parts(run.get("finding"))
        _headline, next_step = goal.headline_and_next(run)
        return {"failing": goal._no_dash(run.get("failing") or parts["failing"]),
                "likely_cause": goal._no_dash(run.get("likely_cause") or parts["likely_cause"]),
                "next_step": next_step,
                "steps": [{"at": goal._iso(run.get("started_at")), "text": goal._run_text(run, True)}],
                "can_answer": feedback.answerable(store, run_id)}

    def worth_of(run_id: str | None, caller: str, can_answer: bool) -> dict:
        rows = store.feedback_for([run_id])[run_id] if run_id else []
        mine = next((r for r in rows if r["person"] == caller), None)
        other = max((r for r in rows if r["person"] != caller),
                    key=lambda r: str(r["answered_at"]), default=None)
        got = mine or other
        return {"answer": bool(got["worth"]) if got else None,
                "by": (got.get("person_name") or got["person"]) if got else None,
                "can_answer": bool(can_answer)}

    def incident_screen(incident_id: str, caller: str, now=None, hidden=()) -> dict:
        inc = store.get_incident(incident_id)
        if inc is None or inc.get("project") in hidden:   # a private project reads as no incident
            raise not_found()
        now = _aware(now or incidents.clock())
        settings = store.service_settings()
        notes = store.incident_notes(incident_id)
        view = incidents.view(store, inc, notes=notes, settings=settings, now=now)
        first_id = notes[0]["run_id"] if notes else None
        run = (store.get_agent_run(first_id) if first_id else None) or {}
        parts = (note_parts(inc, first_id, run) if run else
                 {"failing": None, "likely_cause": None, "next_step": None, "steps": [],
                  "can_answer": False})
        owner = view["who"]
        joined = []
        for n in notes[1:JOINED_MAX + 1]:
            r = store.get_agent_run(n["run_id"])
            if r:
                tp = goal.title_parts(r, n["joined_at"], settings)
                tp["service"] = tp["service"] or goal.service_name(inc["service"], settings)
                title = goal.title(tp, now)
            else:
                title = f"{goal.service_name(inc['service'], settings)}: another note"
            joined.append({"at": goal._iso(n["joined_at"]), "title": title})
        return {
            "id": inc["id"], "title": view["title"], "service": view["service"],
            "state": inc["state"], "state_words": state_words(inc, now),
            "opened_at": view["opened_at"],
            "who": {"name": owner["name"], "since": owner["since"]} if owner else None,
            "resolved": ({"by": inc.get("resolved_by_name") or None,
                          "at": goal._iso(inc["resolved_at"]), "words": resolved_words(inc, now)}
                         if inc["state"] == "resolved" else None),
            "failing": parts["failing"], "likely_cause": parts["likely_cause"],
            "next_step": parts["next_step"],
            "note": goal._no_dash(goal.strip_filler(run.get("finding"))) or "",
            "evidence": [{"at": s["at"], "text": plain(s["text"])}
                         for s in parts["steps"][:EVIDENCE_MAX]],
            "chart": chart_of(inc, now, settings, hidden),
            "slack": slack_of(store, inc, notes),
            "joined": joined,
            "worth": worth_of(first_id, caller, parts["can_answer"]),
            "actions": actions_of(inc),
        }

    @app.get("/api/sre/incidents")
    def list_incidents_screen(state: str = "", service: str = "", before: str = "",
                              show: str = ""):
        """The incidents by state: Open, Someone on it, Resolved (the 20 latest; page on with
        `before` = next_before). `state` shows one group. `show=all` adds the findings raised
        that did not become an incident."""
        return incidents_screen(state, service, before, show, hidden=h.hidden_ids())

    @app.get("/api/sre/incidents/{incident_id}")
    def get_incident_screen(incident_id: str, request: Request):
        """One incident: the note, the evidence, the chart, the Slack thread, who was on it."""
        return incident_screen(incident_id, person(request), hidden=h.hidden_ids())

    @app.post("/api/sre/incidents/{incident_id}/{action}")
    def incident_action(incident_id: str, action: str, request: Request):
        """on_it, resolve, reopen, worth_yes or worth_no, by the caller; answers the incident
        screen as it is now. Admin, like every write (_required_scope)."""
        if action not in ACTIONS:
            raise HTTPException(404, "No such action")
        inc = store.get_incident(incident_id)
        if inc is None or inc.get("project") in h.hidden_ids():
            raise not_found()
        who = person(request)
        if action in STATE_ACTIONS:
            # on_it on an incident someone else is on is refused by take, which names them
            held = (action == "on_it" and inc["state"] == "on_it"
                    and not (inc["owner"] == who and inc["owner_name"] == who))
            if action not in actions_of(inc) and not held:
                raise HTTPException(409, REFUSED.get((inc["state"], action)) or
                                    "That does not apply to this incident now.")
            try:
                done = STATE_ACTIONS[action](store, incident_id, who, who, "console")
            except incidents.Held as e:
                raise HTTPException(409, str(e))
            if done is None:
                raise not_found()
            sre_home.clear_cache()   # the home page counts open incidents
        else:
            notes = store.incident_notes(incident_id)
            if not notes:
                raise HTTPException(409, "This incident has no note to answer on")
            try:
                feedback.answer(store, notes[0]["run_id"], action == "worth_yes", who, who,
                                "console")
            except KeyError:
                raise HTTPException(404, "The note is gone")
            except feedback.NotRaised as e:
                raise HTTPException(409, str(e))
        return incident_screen(incident_id, who, hidden=h.hidden_ids())
