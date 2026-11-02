"""The AI SRE Timeline page (TR-752): GET /api/sre/timeline and GET /api/sre/timeline/raw.

One list of what happened to the services, newest first: how much each one received (rolled up
into windows of 10 minutes, or an hour for a long range, and the windows that were normal folded
into one line), the deploys of the repos linked to it, the alerts sent, the checks the agents ran
and the incidents opened, taken and resolved, and the metrics that stepped. Every number is
counted in SQL buckets (store.event_series_many); no event row is loaded to be counted. The raw
route lists the events behind a window, with their fields.

Nothing here writes. Both routes are plain GETs, so a read key may call them.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Query

from .. import goal, incidents, sre
from ..envelope import now_utc

PAGE = 200                      # items in one page
DEFAULT_HOURS = 6
MAX_DAYS = 31                   # a longer range is refused: it would be a lot of windows
SHORT_WINDOW = 600              # seconds: the window up to two days
LONG_WINDOW = 3600              # seconds: the window beyond that
MIN_STEP = 10                   # a change in volume needs at least this many events to count
MAX_CHECK_PAGES = 20
RAW_MAX = 1000
KIND_ORDER = {k: i for i, k in enumerate(
    ("incident", "alert", "window", "check", "deploy", "metric", "quiet"))}
GROUPS = {"logs": {"window", "quiet"}, "metrics": {"metric"}, "alerts": {"alert"},
          "deploys": {"deploy"}, "checks": {"check", "incident"}}
ALIASES = {"log": "logs", "metric": "metrics", "alert": "alerts", "deploy": "deploys",
           "check": "checks"}
ITEM_KINDS = {"window", "quiet", "deploy", "alert", "check", "incident", "metric"}
KIND_WORDS = {"logs": "log changes", "metrics": "metric changes", "alerts": "alerts",
              "deploys": "deploys", "checks": "checks"}
ALERT_CONNECTORS = ("alertmanager", "prometheus_alerts")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# ── small helpers ────────────────────────────────────────────────────────────
def _iso(t) -> str | None:
    return None if t is None else t.astimezone(timezone.utc).isoformat()


def _aware(t):
    if t is None:
        return None
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


def _when(value, name: str):
    """A time from an ISO string (a Z is read as UTC; no zone means UTC) or a datetime."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return _aware(value)
    try:
        return _aware(datetime.fromisoformat(str(value).strip().replace("Z", "+00:00")))
    except ValueError:
        raise ValueError(f"{name} must be an ISO time, like 2026-11-02T14:00:00Z") from None


def _clock(t) -> str:
    return t.astimezone(timezone.utc).strftime("%H:%M")


def _stamp(t) -> str:
    t = t.astimezone(timezone.utc)
    return f"{t.day} {_MONTHS[t.month - 1]} {t:%H:%M}"


def _count(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _times(r: float) -> str:
    return str(round(r)) if r >= 10 else f"{r:.1f}".removesuffix(".0")


def _number(v: float) -> str:
    if abs(v) >= 100:
        return f"{v:,.0f}"
    return f"{v:.2f}".rstrip("0").rstrip(".") or "0"


def _kinds(value) -> set[str] | None:
    """The item kinds asked for, or None for all of them. Accepts logs, metrics, alerts, deploys
    and checks (a group each), and the item kinds themselves."""
    if value is None or value == "":
        return None
    parts = value.split(",") if isinstance(value, str) else list(value)
    out: set[str] = set()
    for raw in parts:
        k = str(raw).strip().lower()
        if not k:
            continue
        k = ALIASES.get(k, k)
        if k in GROUPS:
            out |= GROUPS[k]
        elif k in ITEM_KINDS:
            out.add(k)
        else:
            raise ValueError(f"kinds is a list of logs, metrics, alerts, deploys and checks; "
                             f"{k!r} is none of them")
    return out or None


def range_words(start, end, now) -> str:
    """"Last 6 hours" when the range ends now, else "28 Oct 09:00 to 28 Oct 15:00" (UTC)."""
    if abs((now - end).total_seconds()) <= 60:
        mins = round((end - start).total_seconds() / 60)
        if mins < 60:
            return "Last minute" if mins <= 1 else f"Last {mins} minutes"
        if mins % 1440 == 0 and mins >= 2 * 1440:
            return f"Last {mins // 1440} days"
        if mins % 60 == 0:
            h = mins // 60
            return "Last hour" if h == 1 else f"Last {h} hours"
        return f"Last {mins} minutes"
    return f"{_stamp(start)} to {_stamp(end)}"


def _in_words(words: str) -> str:
    """"Last 6 hours" -> "in the last 6 hours"; an absolute range -> "from ... to ..."."""
    return "in the " + words[0].lower() + words[1:] if words.startswith("Last ") else "from " + words


def _empty_words(display: str | None, kinds: set[str] | None, words: str) -> str:
    where = f" for {display}" if display else ""
    if kinds:
        groups = [g for g, members in GROUPS.items() if members & kinds]
        names = [KIND_WORDS[g] for g in groups]
        what = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]
        return f"No {what}{where} {_in_words(words)}."
    return f"Nothing arrived{where} {_in_words(words)}."


# ── the items ────────────────────────────────────────────────────────────────
def _item(kind: str, id_: str, at, end, service: str, text: str, tone: str,
          link: str | None = None, raw: dict | None = None) -> dict:
    """One timeline item. `_t` (dropped before the answer) is when it sorts: the end of a span,
    else its time."""
    when = _clock(at) if end is None else f"{_clock(at)} to {_clock(end)}"
    return {"id": id_, "kind": kind, "at": _iso(at), "end": _iso(end), "time_words": when,
            "service": service, "text": text, "tone": tone, "link": link, "raw": raw,
            "_t": end or at}


def _windows(key: str, display: str, totals: list[int], errors: list[int], since, until,
             w: int) -> list[dict]:
    """The items of one service. `totals` and `errors` are per bucket of `w` seconds from `since`;
    bucket 0 is the one before the range and is only looked back at. A bucket with errors, or whose
    volume rose or fell a lot against the one before, is an item of its own; the buckets in
    between that were normal fold into one "normal from ... to ..." item."""
    prev_words = "10 minutes" if w == SHORT_WINDOW else "hour"
    out: list[dict] = []
    run = None   # [first start, last end, events, first index]

    def flush():
        nonlocal run
        if run is not None:
            a, b, n, _i = run
            out.append(_item("quiet", f"quiet:{key}:{_iso(a)}", a, b, display,
                             f"{display}: normal from {_clock(a)} to {_clock(b)}", "ok",
                             raw={"from": _iso(a), "to": _iso(b), "count": n}))
            run = None

    for i in range(1, len(totals)):
        ws = since + timedelta(seconds=i * w)
        we = min(ws + timedelta(seconds=w), until)
        frac = max((we - ws).total_seconds() / w, 0.1)
        n, e, pn, pe = totals[i], errors[i], totals[i - 1], errors[i - 1]
        exp_n, exp_e = pn * frac, pe * frac
        started = pn == 0 and n >= MIN_STEP
        rose = pn > 0 and n >= 2 * exp_n and n - exp_n >= MIN_STEP
        fell = pn > 0 and n > 0 and exp_n >= 2 * n and exp_n - n >= MIN_STEP
        stopped = n == 0 and exp_n >= MIN_STEP
        if e == 0 and not (started or rose or fell or stopped):
            if n > 0:   # a normal window; an empty one is neutral inside a run
                run = [ws, we, n, i] if run is None else [run[0], we, run[2] + n, run[3]]
            continue
        flush()
        raw = {"from": _iso(ws), "to": _iso(we), "count": n}
        if stopped and e == 0:
            text = f"{display}: nothing arrived, {_count(pn, 'event')} in the {prev_words} before"
            tone = "warn"
        else:
            head = f"{display}: {_count(n, 'event')}" + (f", {_count(e, 'error')}" if e else "")
            parts, extra = [], ""
            if started:
                extra = f"started again after nothing in the {prev_words} before"
            else:
                if e and pe == 0:
                    parts.append("errors up from none")
                elif e and e >= 2 * exp_e and e - exp_e >= 2:
                    parts.append(f"errors up {_times(e / exp_e)}x")
                elif e and exp_e >= 2 * e and exp_e - e >= 2:
                    parts.append(f"errors down {_times(exp_e / e)}x")
                if rose:
                    parts.append(f"events up {_times(n / exp_n)}x")
                elif fell:
                    parts.append(f"events down {_times(exp_n / n)}x")
                if parts == ["errors up from none"]:
                    parts, extra = [], f"no errors in the {prev_words} before"
            tail = ", ".join(x for x in ((" and ".join(parts) + f" on the {prev_words} before")
                                         if parts else "", extra) if x)
            text = head + (f", {tail}" if tail else "")
            bad = e > 0 and (((pe == 0 or e >= 2 * exp_e) and e >= 5) or e * 10 >= n)
            tone = "bad" if bad else ("warn" if e > 0 else "info")
        out.append(_item("window", f"win:{key}:{_iso(ws)}", ws, we, display, text, tone, raw=raw))
    flush()
    return out


def _deploy_text(d: dict) -> str:
    repo, text = d["repo"], (d.get("text") or "").strip()
    if d["kind"] == "merge":
        return f"{repo} merged: {text}" if text else f"{repo} merged a pull request"
    if d["kind"] == "commit":
        return f"{repo} commit: {text}" if text else f"{repo} commit"
    out = f"{repo} pushed"
    if d.get("branch"):
        out += f" to {d['branch']}"
    if d.get("author"):
        out += f" by {d['author']}"
    if d.get("commits"):
        out += f": {_count(d['commits'], 'commit')}"
    return out


def _alert_state(row: dict) -> str:
    labels, payload = row["labels"], row["payload"]
    state = str(labels.get("status") or payload.get("status") or payload.get("state")
                or "").strip().lower()
    if state == "resolved":
        return "resolved"
    return "pending" if state == "pending" else "active"


def _alert_items(store, catalog, start, end, settings, display_of, want_key, hidden) -> list[dict]:
    sources = [s for s in catalog.sources.values() if s.connector in ALERT_CONNECTORS]
    if not sources:
        return []
    overrides = sre._overrides(store)
    label_of = {s.name: sre.service_label(s, overrides) for s in sources}
    out = []
    for r in store.alert_events([s.name for s in sources], start, end):
        labels, payload = r["labels"], r["payload"]
        nested = payload.get("labels") if isinstance(payload.get("labels"), dict) else {}
        raw_name = ((label_of.get(r["source"]) and labels.get(label_of[r["source"]]))
                    or labels.get("service") or nested.get("service") or r["key"] or "")
        key = sre.canonical(str(raw_name), settings) if raw_name else ""
        if want_key and key != want_key:
            continue
        if key in hidden:
            continue
        name = display_of(key) if key else "an unknown service"
        severity = str(labels.get("severity") or nested.get("severity") or "").strip()
        state = _alert_state(r)
        alert = r["event_type"] or "alert"
        sev = f" (severity {severity})" if severity else ""
        if state == "resolved":
            text, tone = f"Alert {alert} on {name} resolved", "ok"
        elif state == "pending":
            text, tone = f"Alert {alert} is pending on {name}{sev}", "warn"
        else:
            text = f"Alert {alert} is active on {name}{sev}"
            tone = "bad" if severity.lower() in ("page", "critical") else "warn"
        at = _aware(r["at"])
        out.append(_item("alert", f"alert:{r['source']}:{_iso(at)}:{alert}:{name}", at, None,
                         name if key else "", text, tone))
    return out


def _check_items(store, catalog, start, end, now, settings, display, want_key, hidden) -> list[dict]:
    """The checks the agents ran in the range. Quiet ones that follow each other for a service
    are one item; one that raised something, looked into it or wrote a note is an item each."""
    scheduled = {t.name for t in catalog.triggers if getattr(t.condition, "every", None)}
    found, before = [], end   # page back from the end of the range, so a past range reads its own rows
    for _ in range(MAX_CHECK_PAGES):
        page = goal.cell_results(store, scheduled, limit=100, before=before, show="all",
                                 service=display or "", now=now)
        rows = page["findings"]
        found += [r for r in rows if start <= _when(r["at"], "at") < end]
        if not page["next_before"] or (rows and _when(rows[-1]["at"], "at") < start):
            break
        before = _when(page["next_before"], "next_before")
    found.sort(key=lambda r: r["at"])
    out, runs = [], {}

    def flush(svc):
        run = runs.pop(svc, None)
        if run:
            a, b = _when(run[0]["at"], "at"), _when(run[-1]["at"], "at")
            head = f"{svc}: " if svc else ""
            text = head + ("1 check, quiet" if len(run) == 1 else f"{len(run)} checks, all quiet")
            out.append(_item("check", f"checks:{run[0]['id']}", a, b if b > a else None, svc, text,
                             "ok", raw=None))

    for r in found:
        svc = (r.get("title_parts") or {}).get("service") or ""
        if goal.service_key(r.get("entity"), settings) in hidden:
            continue
        if r["outcome"] == "quiet":
            runs.setdefault(svc, []).append(r)
            continue
        flush(svc)
        tone = "info" if r["outcome"] == "investigated" else "warn"
        out.append(_item("check", f"check:{r['id']}", _when(r["at"], "at"), None, svc,
                         r["title"], tone))
    for svc in list(runs):
        flush(svc)
    return out


INCIDENT_TONE = {"opened": "bad", "on_it": "warn", "resolved": "ok", "resolved_itself": "ok",
                 "reopened": "bad"}


def _incident_items(store, start, end, now, settings, display_of, want_key, hidden) -> list[dict]:
    # the chosen service by every name merged into it, since an incident keeps the name it was opened with
    names = [n for n in settings if sre.canonical(n, settings) == want_key] + [want_key] \
        if want_key else None
    incs, log = store.incident_changes(start, end, services=names)
    out = []
    for inc in incs:
        key = sre.canonical(inc["service"] or "", settings)
        if inc.get("resolution") == "before_tracking" or key in hidden:
            continue
        if want_key and key != want_key:
            continue
        events = []
        opened = _aware(inc["opened_at"])
        if opened is not None and start <= opened < end:
            events.append(("opened", opened, None))
        for row in log.get(inc["id"], []):
            if row["action"] == "opened":
                continue   # the opening is the incident's opened_at, not a second item
            events.append((row["action"], _aware(row["at"]), row["by_name"]))
        if not events:
            continue
        title = incidents.view(store, inc, settings=settings, now=now)["title"]
        for action, at, who in events:
            who = (who or "").strip() or "Someone"
            text = {"opened": f"Incident opened: {title}",
                    "on_it": f"{who} is on it: {title}",
                    "resolved": f"{who} resolved: {title}",
                    "resolved_itself": f"Resolved itself: {title}",
                    "reopened": f"{who} reopened: {title}"}.get(action)
            if text is None:
                continue
            out.append(_item("incident", f"inc:{inc['id']}:{action}:{_iso(at)}", at, None,
                             display_of(key), text, INCIDENT_TONE[action],
                             link=f"/incidents/{inc['id']}"))
    return out


def _metric_items(store, catalog, keys, start_w, until, since, w, display_of) -> list[dict]:
    prom = [s.name for s in catalog.sources.values() if s.connector == "prometheus"]
    series: dict[tuple, list] = {}
    for r in store.metric_buckets(keys, since, until, w, prom):
        series.setdefault((r["service"], r["metric"]), []).append(r)
    out = []
    for (svc, metric), rows in series.items():
        rows.sort(key=lambda r: r["bucket"])
        for old, new in zip(rows, rows[1:]):
            b = _aware(new["bucket"])
            if b < start_w:
                continue
            o, v = old["value"], new["value"]
            if o == 0:
                if v == 0:
                    continue
            elif abs(v - o) / abs(o) < 0.5:
                continue
            unit = new["unit"] if new["unit"] and new["unit"] != "1" \
                and not new["unit"].startswith("{") else ""
            suffix = f" {unit}" if unit else ""
            word = "up" if v > o else "down"
            name = display_of(svc)
            out.append(_item("metric", f"metric:{svc}:{metric}:{_iso(b)}", b, None, name,
                             f"{name}: {metric} {_number(v)}{suffix}, {word} from "
                             f"{_number(o)}{suffix}", "info"))
    return out


# ── the screen ───────────────────────────────────────────────────────────────
class _Scope:
    """The range and the services both routes read."""

    def __init__(self, store, catalog, service, start, end, now):
        self.now = now or now_utc()
        self.end = _when(end, "to") or self.now
        self.start = _when(start, "from") or self.end - timedelta(hours=DEFAULT_HOURS)
        if self.end <= self.start:
            raise ValueError("to must be after from")
        if self.end - self.start > timedelta(days=MAX_DAYS):
            raise ValueError(f"the range is at most {MAX_DAYS} days")
        c = sre._compute_cached(store, catalog, now)
        self.settings = c["settings"]
        items = [i for n, r in c["recs"].items()
                 if (i := sre._item(n, r, self.settings, False)) is not None]
        self.hidden = {n for n, st in self.settings.items() if st.get("hidden")}
        self.display = {i["name"]: i["display"] for i in items}
        self.names = sorted(self.display.values(), key=lambda d: (d.lower(), d))
        self.service = (service or "").strip()
        self.keys = sorted(self.display)
        self.want_key = ""
        if self.service:
            low = self.service.lower()
            match = [k for k, d in self.display.items() if d.lower() == low] \
                or [k for k in self.display if k.lower() == low]
            if not match:
                canon = sre.canonical(self.service, self.settings)
                match = [canon] if canon in self.display else []
            self.want_key = match[0] if match else "\0"
            self.keys = match

    def display_of(self, key: str) -> str:
        return self.display.get(key) or goal.service_name(key, self.settings)

    @property
    def shown(self) -> str | None:
        return self.display_of(self.want_key) if self.want_key and self.keys else \
            (self.service or None)


def timeline_screen(store, catalog, service: str = "", start=None, end=None, kinds=None,
                    before=None, now=None) -> dict:
    """The timeline page. `start` and `end` default to the last 6 hours, `kinds` to all kinds,
    `before` pages (the previous answer's next_before). Raises ValueError for a bad input."""
    want = _kinds(kinds)
    cut = _when(before, "before")
    sc = _Scope(store, catalog, service, start, end, now)
    span = (sc.end - sc.start).total_seconds()
    w = SHORT_WINDOW if span <= 2 * 86400 else LONG_WINDOW
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    start_w = epoch + timedelta(seconds=(sc.start - epoch).total_seconds() // w * w)
    since = start_w - timedelta(seconds=w)
    keys, items = sc.keys, []

    def on(*kind):
        return want is None or bool(want & set(kind))

    if keys and on("window", "quiet"):
        totals = store.event_series_many(keys, since, sc.end, w)
        errors = store.event_series_many(keys, since, sc.end, w, errors_only=True)
        for k in keys:
            items += _windows(k, sc.display_of(k), [n for _t, n in totals[k]],
                              [n for _t, n in errors[k]], since, sc.end, w)
    if keys and on("deploy"):
        for k in keys:
            for d in sre.deploys(store, catalog, k, sc.start, limit=PAGE, detail=True,
                                 until=sc.end):
                items.append(_item("deploy", f"deploy:{d['repo']}:{d['at']}:{d['kind']}:{k}",
                                   _when(d["at"], "at"), None, sc.display_of(k), _deploy_text(d),
                                   "info"))
    if keys and on("alert"):
        items += _alert_items(store, catalog, sc.start, sc.end, sc.settings, sc.display_of,
                              sc.want_key, sc.hidden)
    if keys and on("check"):
        items += _check_items(store, catalog, sc.start, sc.end, sc.now, sc.settings,
                              sc.shown if sc.want_key else "", sc.want_key, sc.hidden)
    if keys and on("incident"):
        items += _incident_items(store, sc.start, sc.end, sc.now, sc.settings, sc.display_of,
                                 sc.want_key, sc.hidden)
    if keys and on("metric"):
        items += _metric_items(store, catalog, keys, start_w, sc.end, since, w, sc.display_of)

    if want is not None:
        items = [i for i in items if i["kind"] in want]
    # newest end first; at one moment a point comes before a span that ends there (the console puts
    # that span under the hour before the point), then by kind, then by id
    items.sort(key=lambda i: (i["end"] is not None, KIND_ORDER[i["kind"]], i["id"]))
    items.sort(key=lambda i: i["_t"], reverse=True)
    # a cursor per item, strictly falling: items that share a moment (every service's window ends
    # on the same one) are told apart by a microsecond each, so a page cut inside them is exact
    prev = None
    for i in items:
        i["_c"] = i["_t"] if prev is None or i["_t"] < prev else prev - timedelta(microseconds=1)
        prev = i["_c"]
    if cut is not None:
        items = [i for i in items if i["_c"] < cut]
    words = range_words(sc.start, sc.end, sc.now)
    empty = _empty_words(sc.shown, want, words) if not items and cut is None else None
    page = items[:PAGE]
    next_before = _iso(page[-1]["_c"]) if len(items) > PAGE else None
    for i in page:
        i.pop("_t")
        i.pop("_c")
    return {"range_words": words, "services": sc.names, "items": page, "next_before": next_before,
            "empty_words": empty}


def _feed_of(catalog, row: dict) -> str:
    src = catalog.sources.get(row["source"])
    kind = sre.signal_kind(src.connector if src else "", row["event_type"],
                           row["payload"].get("metricType"))
    return f"{row['source']} {kind}"


def raw_screen(store, catalog, service: str = "", start=None, end=None, limit: int = PAGE,
               now=None) -> dict:
    """The events behind the timeline, newest first: {rows: [{at, feed, text, fields}], count (all
    of them in the range), more (count is past the limit)}."""
    limit = max(1, min(int(limit), RAW_MAX))
    sc = _Scope(store, catalog, service, start, end, now)
    rows, total = store.service_events(sc.keys, sc.start, sc.end, limit) if sc.keys else ([], 0)
    out = []
    for r in rows:
        fields = {**r["labels"]}
        for k, v in r["payload"].items():
            fields.setdefault(k, v)
        fields.setdefault("event_type", r["event_type"])
        out.append({"at": _iso(_aware(r["at"])), "feed": _feed_of(catalog, r), "text": r["text"],
                    "fields": fields})
    return {"rows": out, "count": total, "more": total > limit}


def register(app, store, h, ctx):
    def run(fn, *args, **kw):
        try:
            return fn(store, ctx.runtime.catalog, *args, **kw)
        except ValueError as e:
            h.err(e)

    @app.get("/api/sre/timeline")
    async def sre_timeline(service: str = "", from_: str = Query("", alias="from"), to: str = "",
                           kinds: str = "", before: str = ""):
        """What happened to the services, newest first, in windows with the normal ones folded.
        `kinds` is a list of logs, metrics, alerts, deploys and checks; page with `before` = the
        previous answer's next_before."""
        return await asyncio.to_thread(run, timeline_screen, service, from_, to, kinds, before)

    @app.get("/api/sre/timeline/raw")
    async def sre_timeline_raw(service: str = "", from_: str = Query("", alias="from"),
                               to: str = "", limit: int = PAGE):
        """The events in a range (a window's raw {from, to}), newest first, with their fields."""
        return await asyncio.to_thread(run, raw_screen, service, from_, to, limit)
