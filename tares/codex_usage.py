"""The Codex account's limits, as a crew on Codex reads them and the pace step takes them (TR-1597).

A crew on Codex has no Claude plan. Its watch asks one of its stations' app-servers for the account's limits
(`account/rateLimits/read`, which uses no model) and sends the answer here (PUT /api/crew/codex-limits). This
file keeps the reading and turns it into the plan `tares.factory.pace_now` takes, so the crew's thresholds and
the whole pace pipeline stay the ones a Claude Code crew has. Nothing here reads Claude's plan, and a Codex
crew is never paced from it.

What Codex gives (0.171.0): `rateLimits`, one bucket with a `primary` and a `secondary` window, each with
`usedPercent`, `windowDurationMins` and `resetsAt` (unix seconds), and `rateLimitsByLimitId`, the same shape for
each further bucket (a limit of one model). On a ChatGPT Plus account the two windows are 300 and 10080 minutes
long. Which window is which is told by its length, never by its place:

  300 minutes    the 5-hour window  (`session`)
  10080 minutes  the week           (`weekly_all`)

A window of any other length, and every window of a further bucket, is kept and shown under a name of its own
(`codex_<minutes>m`, `codex_<bucket>_<minutes>m`), which the pace step does not know: it never slows or holds
the crew.

A reading is one of four things, and each is said (`reading` in what GET /api/crew/pace answers):

  ok           windows arrived, less than MAX_AGE ago
  none         no reading has arrived
  stale        the last one is older than MAX_AGE: it is not used, and the pace is unknown
  unsupported  the account gave no window at all (an API key has no plan to read): the pace is unknown

Unknown never slows the crew: it then runs on its concurrency settings, as a Claude Code crew with no reading.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

PLAN_SETTING = "codex_plan"          # the last reading a crew sent, with when it arrived and from whom
SOURCE = "codex account/rateLimits/read"
WINDOW_KIND = {300: "session", 10080: "weekly_all"}   # a window's length in minutes: the limit it is
MAX_AGE = timedelta(minutes=30)      # an older reading is not trusted
MAX_WINDOWS = 40                     # of one reading that are kept
MAIN = ""                            # the bucket of `rateLimits`

READINGS = ("ok", "none", "stale", "unsupported")


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _text(v, limit: int = 80) -> str | None:
    return str(v)[:limit] if isinstance(v, str) and v else None


def _when(v) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if not isinstance(v, str):
        return None
    try:
        t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _windows(bucket, snapshot) -> list[dict]:
    """The windows of one bucket, whatever they are called in it: every value that carries `usedPercent`."""
    out = []
    if not isinstance(snapshot, dict):
        return out
    name = _text(snapshot.get("limitName"))
    for slot in sorted(snapshot):
        w = snapshot[slot]
        if not isinstance(w, dict) or _num(w.get("usedPercent")) is None:
            continue
        mins, resets = _num(w.get("windowDurationMins")), _num(w.get("resetsAt"))
        try:
            resets_at = _iso(datetime.fromtimestamp(resets, timezone.utc)) if resets is not None else None
        except (OverflowError, OSError, ValueError):
            resets_at = None
        out.append({"bucket": bucket, "name": name, "minutes": int(mins) if mins is not None else None,
                    "percent": w["usedPercent"], "resets_at": resets_at})
    return out


def compact_reading(doc: dict) -> dict:
    """What a crew sent, as it is kept: {account: {type, plan}, windows: [{bucket, name, minutes, percent,
    resets_at}], reached, taken_at}. `doc` is {account, limits, taken_at}: `limits` is the answer to
    `account/rateLimits/read` and `account` the `account` of `account/read`. Account ids, addresses and
    workspace names are not kept."""
    limits = doc.get("limits") if isinstance(doc.get("limits"), dict) else {}
    account = doc.get("account") if isinstance(doc.get("account"), dict) else {}
    main = limits.get("rateLimits") if isinstance(limits.get("rateLimits"), dict) else {}
    windows = _windows(MAIN, main)
    more = limits.get("rateLimitsByLimitId")
    for bucket in sorted(more) if isinstance(more, dict) else []:
        snap = more[bucket]
        if not isinstance(snap, dict) or not isinstance(bucket, str):
            continue
        if main and snap.get("limitId") == main.get("limitId") and bucket == main.get("limitId"):
            continue   # the main bucket again, under its id
        windows += _windows(bucket[:60], snap)
    return {"account": {"type": _text(account.get("type"), 40),
                        "plan": _text(account.get("planType") or main.get("planType"), 60)},
            "windows": windows[:MAX_WINDOWS], "reached": _text(main.get("rateLimitReachedType"), 60),
            "taken_at": _text(doc.get("taken_at"), 40)}


def window_kind(w: dict) -> str:
    """The limit a kept window is: by its length for the main bucket, a name of its own for any other."""
    mins = w.get("minutes")
    if not w.get("bucket"):
        return WINDOW_KIND.get(mins) or f"codex_{mins if mins is not None else 'unknown'}m"
    return f"codex_{w['bucket']}_{mins if mins is not None else 'unknown'}m"


def _length(mins) -> str:
    if not mins:
        return ""
    for size, word in ((1440, "day"), (60, "hour"), (1, "minute")):
        if mins % size == 0:
            n = mins // size
            return f"{n} {word}{'' if n == 1 else 's'}"
    return f"{mins} minutes"


def window_label(w: dict) -> str:
    kind = window_kind(w)
    if kind == "session":
        return "5-hour window"
    if kind == "weekly_all":
        return "This week"
    length = _length(w.get("minutes"))
    who = w.get("name") or w.get("bucket")
    if who:
        return f"{who}, {length}" if length else f"{who}, a window of unknown length"
    return f"A window of {length}" if length else "A window of unknown length"


def plan_now(saved: dict | None, now: datetime, max_age: timedelta = MAX_AGE) -> tuple[dict | None, str]:
    """(the plan `pace_now` takes, what the reading is: one of READINGS). The plan is None for every reading
    but `ok`. `saved` is what `save` stored: {at, reading, person_id}. Each limit carries `kind`, `label`,
    `percent`, `resets_at`, `reset` (its reset time has passed), `at`, `from` and `person`; `paces` says whether
    the pace step knows it."""
    reading = saved.get("reading") if isinstance(saved, dict) else None
    at = _when(saved.get("at")) if isinstance(saved, dict) else None
    if not isinstance(reading, dict) or at is None:
        return None, "none"
    if now - at > max_age:
        return None, "stale"
    windows = [w for w in reading.get("windows") or [] if isinstance(w, dict) and _num(w.get("percent")) is not None]
    if not windows:
        return None, "unsupported"
    limits = []
    for w in windows:
        kind = window_kind(w)
        t = _when(w.get("resets_at"))
        limits.append({"kind": kind, "label": window_label(w), "percent": w["percent"], "resets_at": w.get("resets_at"),
                       "reset": bool(t and t <= now), "at": at, "from": SOURCE, "person": saved.get("person_id"),
                       "paces": kind in WINDOW_KIND.values()})
    order = {"session": 0, "weekly_all": 1}
    limits.sort(key=lambda x: (order.get(x["kind"], 2), x["kind"]))
    return {"limits": limits, "extra_usage": None, "at": at}, "ok"


def save(store, doc: dict, person_id, now: datetime) -> dict:
    """Keep what a crew sent, with the cell's own time (a crew's clock is not trusted for a reading's age)."""
    reading = compact_reading(doc)
    store.set_setting(PLAN_SETTING, json.dumps({"at": _iso(now), "reading": reading, "person_id": person_id}))
    return reading


def saved(store) -> dict | None:
    raw = store.get_setting(PLAN_SETTING)
    try:
        doc = json.loads(raw) if raw else None
    except ValueError:
        doc = None
    return doc if isinstance(doc, dict) else None


def _note(reading: str, saved_doc: dict | None, max_age: timedelta) -> str:
    if reading == "none":
        return "Pacing is on, and no reading of the Codex account's limits has arrived yet."
    if reading == "stale":
        at = _when((saved_doc or {}).get("at"))
        when = f" (taken {at.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC)" if at else ""
        return (f"Pacing is on, but the last reading of the Codex account's limits{when} is older than "
                f"{int(max_age.total_seconds() // 60)} minutes and is not used.")
    kind = ((saved_doc or {}).get("reading") or {}).get("account", {}).get("type")
    what = "an API key account" if kind == "apiKey" else f"a {kind} account" if kind else "this account"
    return f"Pacing is on, but Codex reports no limits for {what}, so there is nothing to pace by."


def pace(store, settings: dict, now: datetime, pace_now) -> dict:
    """What GET /api/crew/pace answers for a crew on Codex: `pace_now`'s answer for the Codex reading, and
    with it `source` ("codex"), `reading` (one of READINGS), `read_at` (when the reading arrived, or null),
    `limits` (every window, the ones the pace step does not know too: {kind, label, percent, resets_at, reset,
    paces}), `account` ({type, plan}) and `reached` (what Codex names when the account is at a limit, or
    null). `known` is true only for an `ok` reading."""
    doc = saved(store)
    plan, reading = plan_now(doc, now)
    out = pace_now(settings, plan, now)
    kept = (doc or {}).get("reading") if isinstance((doc or {}).get("reading"), dict) else {}
    out.update(source="codex", reading=reading, read_at=(doc or {}).get("at") if doc else None,
               limits=[{k: x[k] for k in ("kind", "label", "percent", "resets_at", "reset", "paces")}
                       for x in (plan or {}).get("limits") or []],
               account=kept.get("account") or {"type": None, "plan": None},
               reached=kept.get("reached") if reading == "ok" else None)
    if out.get("on") and reading != "ok":
        out["note"] = _note(reading, doc, MAX_AGE)
    return out
