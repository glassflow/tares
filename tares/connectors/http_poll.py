"""HTTP API connector: polls a JSON endpoint on the source's schedule and emits one Envelope per
item. The generic path for the APIs no other connector covers: a weather service, a status page,
a SaaS export, a home-grown endpoint. The payload is the item as received, lossless; labels,
the fields profile and trigger fields reach into nested objects through dotted names
(`current_weather.windspeed`).

config:
  url: https://api.open-meteo.com/v1/forecast?latitude=52.52&longitude=13.41&current_weather=true
  method: GET                  # or POST, with `body` as the JSON to send
  headers: {"Accept": "application/json"}   # extra request headers, not secret
  auth_header: Authorization   # the one credential header; `auth_value` is stored as a secret
  auth_value: Bearer xyz
  items_path: data.items       # dotted path to the array of items; empty = the whole body is one
                               # event, a top-level array = one event per element
  event_type: reading          # fixed, or event_type_field: a field of the item
  text_template: "{city}: wind {current_weather.windspeed} km/h"   # dotted names work
  event_time_field: current_weather.time   # ISO-8601, else the poll time
  id_field: id                 # when set, an item seen in recent polls is not emitted again

Rate limiting (TR-277): the poll interval has a floor (MIN_POLL_SECONDS); a 429 or a 5xx puts
the source into a backoff that doubles from the poll interval up to an hour, a 429 with
Retry-After uses that; polls during the backoff make no request. A 4xx other than 429 is a
config problem and raises, so the source's health shows it.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from ..envelope import Envelope, now_utc
from .base import Connector

MIN_POLL_SECONDS = 10
MAX_BACKOFF_SECONDS = 3600
# ids remembered for `id_field` dedupe: enough for a list API that returns its latest page, bounded
# so the cursor never grows with the age of the source
SEEN_IDS_KEPT = 1000
# items taken from one response; a page of a thousand is plenty for a poll, and a runaway API
# cannot flood the store
MAX_ITEMS_PER_POLL = 1000
# discover reads at most this many items to learn the fields
_SAMPLE_ITEMS = 20


def _flatten(obj, prefix: str = "", out: dict | None = None, depth: int = 0) -> dict:
    """Nested objects as dotted keys, alongside the originals: {"a": {"b": 1}} gives both
    "a" and "a.b". Lists stay as values (a label cannot point into a list)."""
    out = {} if out is None else out
    if not isinstance(obj, dict) or depth > 6:
        return out
    for k, v in obj.items():
        name = f"{prefix}{k}"
        out[name] = v
        if isinstance(v, dict):
            _flatten(v, name + ".", out, depth + 1)
    return out


def _walk(body, path: str):
    """Follow a dotted path into a parsed body; None when any step is missing."""
    cur = body
    for part in [p for p in str(path or "").split(".") if p]:
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            cur = cur[int(part)] if int(part) < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


def _as_dict(val, what: str) -> dict:
    """`headers` and `body` arrive as objects from the form and as JSON text from YAML or the API;
    accept both, reject anything else with a message that names the field."""
    if val in (None, ""):
        return {}
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except ValueError:
            raise ValueError(f"{what} must be a JSON object")
    if not isinstance(val, dict):
        raise ValueError(f"{what} must be a JSON object")
    return val


def _parse_time(val) -> datetime | None:
    if val in (None, ""):
        return None
    try:
        if isinstance(val, (int, float)):
            # epoch seconds, or milliseconds when clearly too large for seconds
            secs = float(val) / (1000 if val > 1e11 else 1)
            return datetime.fromtimestamp(secs, tz=timezone.utc)
        s = str(val).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        t = datetime.fromisoformat(s)
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


class RateLimited(Exception):
    """Raised on the poll that hit the limit so the source's health says why it went quiet."""


def _request_kwargs(config: dict) -> dict:
    headers = {str(k): str(v) for k, v in _as_dict(config.get("headers"), "headers").items()}
    if config.get("auth_value"):
        headers[str(config.get("auth_header") or "Authorization")] = str(config["auth_value"])
    kw: dict = {"headers": headers}
    if str(config.get("method") or "GET").upper() == "POST":
        kw["json"] = _as_dict(config.get("body"), "body")
    return kw


async def _fetch(config: dict):
    """One request as configured. Returns the parsed body. Raises RateLimited on 429, on any
    5xx, and ValueError on the rest, with the status and a slice of the body."""
    url = str(config.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("url must start with http:// or https://")
    method = str(config.get("method") or "GET").upper()
    if method not in ("GET", "POST"):
        raise ValueError("method must be GET or POST")
    kw = _request_kwargs(config)
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as cx:
        r = await cx.request(method, url, **kw)
    if r.status_code == 429 or r.status_code >= 500:
        raise RateLimited(r.status_code, _retry_after_seconds(r.headers.get("retry-after")))
    if r.status_code >= 400:
        raise ValueError(f"the API answered {r.status_code}: {r.text[:200]}")
    try:
        return r.json()
    except ValueError:
        raise ValueError("the API did not return JSON")


def _find_items(body: dict, prefix: str = "", depth: int = 0) -> str | None:
    """The dotted path of the first list of objects in the body, looking at most two levels
    down: `items`, `data.items`, `result.records`. None when there is no such list."""
    for k, v in body.items():
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v[:5]):
            return f"{prefix}{k}"
    if depth < 2:
        for k, v in body.items():
            if isinstance(v, dict):
                found = _find_items(v, f"{prefix}{k}.", depth + 1)
                if found:
                    return found
    return None


def _items(body, items_path: str) -> list:
    target = _walk(body, items_path) if items_path else body
    if target is None:
        raise ValueError(f"nothing at items_path {items_path!r} in the response")
    if isinstance(target, list):
        return target[:MAX_ITEMS_PER_POLL]
    return [target]


class HttpPollConnector(Connector):
    MIN_POLL_SECONDS = MIN_POLL_SECONDS   # validate_source_dict reads it: no source polls faster
    CONFIG_SCHEMA = {
        "url": {"type": "string", "required": True, "discover_input": True,
                "help": "the endpoint, query string included, e.g. "
                        "https://api.open-meteo.com/v1/forecast?latitude=52.52&longitude=13.41"
                        "&current_weather=true"},
        "method": {"type": "string", "default": "GET", "choices": ["GET", "POST"],
                   "discover_input": True, "help": "GET, or POST with a body"},
        "body": {"type": "object", "discover_input": True,
                 "help": "POST only: the JSON object to send"},
        "headers": {"type": "object", "discover_input": True,
                    "help": 'extra request headers as a JSON object, e.g. {"Accept": '
                            '"application/json"}; put a credential in auth_value instead'},
        "auth_header": {"type": "string", "default": "Authorization", "discover_input": True,
                        "help": "the header the credential goes in"},
        "auth_value": {"type": "string", "secret": True, "discover_input": True,
                       "help": "the credential, e.g. Bearer xyz or an API key; stored as a "
                               "secret and never returned"},
        "items_path": {"type": "string", "discover_input": True,
                       "help": "dotted path to the array of items in the response, e.g. "
                               "data.items; leave empty when the whole body is one event"},
        "event_type": {"type": "string", "default": "api_reading",
                       "help": "fixed event type"},
        "event_type_field": {"type": "string",
                             "help": "field of the item to read the event type from"},
        "text_template": {"type": "string",
                          "help": "the line an agent reads, over the item's fields, e.g. "
                                  "'wind {current_weather.windspeed} km/h'; empty renders the item"},
        "event_time_field": {"type": "string",
                             "help": "field holding the event's time (ISO-8601 or epoch); "
                                     "else the poll time"},
        "id_field": {"type": "string",
                     "help": "field that identifies an item, so one seen in recent polls is not "
                             "stored again: an id for a list API, the reading's own time field "
                             "for a single reading. Leave empty to store every poll"},
    }

    def __init__(self, cfg, store):
        super().__init__(cfg, store)
        self._not_before = 0.0       # monotonic time before which no request is made
        self._backoff = 0.0          # the last wait, doubled on the next limit

    # ── polling ──────────────────────────────────────────────────────────────
    async def poll(self) -> list[Envelope]:
        now = time.monotonic()
        if now < self._not_before:
            return []
        c = self.cfg.config
        try:
            body = await _fetch(c)
        except RateLimited as e:
            status, retry_after = e.args
            wait = retry_after if retry_after is not None else max(
                self.cfg.poll_seconds, min(self._backoff * 2 or self.cfg.poll_seconds,
                                           MAX_BACKOFF_SECONDS))
            wait = min(float(wait), MAX_BACKOFF_SECONDS)
            self._backoff = wait
            self._not_before = time.monotonic() + wait
            at = datetime.now(timezone.utc).timestamp() + wait
            when = datetime.fromtimestamp(at, tz=timezone.utc).strftime("%H:%M UTC")
            what = "rate limited by the API" if status == 429 else f"the API answered {status}"
            raise RateLimited(f"{what}; next try at {when}")
        self._backoff = 0.0
        items = _items(body, c.get("items_path") or "")
        out = []
        seen = self._seen_ids() if c.get("id_field") else None
        new_seen: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                item = {"value": item}
            if seen is not None:
                ident = _walk(item, c["id_field"])
                if ident is None:
                    continue
                ident = str(ident)
                if ident in seen:
                    continue
                seen.add(ident)
                new_seen.append(ident)
            out.append(self._envelope(item))
        if new_seen:
            self._remember_ids(new_seen)
        return out

    def _seen_ids(self) -> set:
        cur = self.store.get_cursor(self.cfg.name)
        return set(cur.split("\n")) if cur else set()

    def _remember_ids(self, new: list[str]) -> None:
        cur = self.store.get_cursor(self.cfg.name)
        ids = (cur.split("\n") if cur else []) + new
        self.store.set_cursor(self.cfg.name, "\n".join(ids[-SEEN_IDS_KEPT:]))

    # ── mapping ──────────────────────────────────────────────────────────────
    def label_context(self, payload: dict | None) -> dict:
        """The item with nested objects also reachable by dotted name, the same at ingest and on
        relabel."""
        return _flatten(payload or {})

    def _envelope(self, item: dict) -> Envelope:
        c = self.cfg.config
        ctx = self.label_context(item)
        labels, key = self.keyed(ctx, fallback=self.cfg.name)
        event_type = ""
        if c.get("event_type_field"):
            v = ctx.get(c["event_type_field"])
            event_type = str(v) if v not in (None, "") else ""
        event_type = event_type or str(c.get("event_type") or "api_reading")
        text = _render(str(c.get("text_template") or ""), ctx)
        if not text:
            text = json.dumps(item, default=str)[:500]
        event_time = None
        if c.get("event_time_field"):
            event_time = _parse_time(ctx.get(c["event_time_field"]))
        return Envelope(
            source=self.cfg.name, source_type=self.cfg.type, key_value=key,
            event_type=event_type, text=text[:500], event_time=event_time or now_utc(),
            payload=item, labels=labels,
        )

    # ── discover ─────────────────────────────────────────────────────────────
    @classmethod
    async def discover(cls, config: dict) -> dict | None:
        """Fetch once and propose the rest: where the items are, the fields they carry, labels
        worth declaring, the time and id fields. The user gives a URL and, if needed, a
        credential; nothing else is typed by hand."""
        if not str(config.get("url") or "").strip():
            raise ValueError("enter the url first, then Discover")
        try:
            body = await _fetch(config)
        except RateLimited as e:
            raise ValueError(f"the API answered {e.args[0]}; try again in a moment")
        items_path = str(config.get("items_path") or "")
        if not items_path and isinstance(body, dict):
            items_path = _find_items(body) or ""
        target = _walk(body, items_path) if items_path else body
        if target is None:
            raise ValueError(f"nothing at items_path {items_path!r} in the response")
        per_poll = len(target) if isinstance(target, list) else 1
        sample = [x for x in (target if isinstance(target, list) else [target])[:_SAMPLE_ITEMS]
                  if isinstance(x, dict)]
        if not sample:
            raise ValueError("the response holds no JSON objects to make events from")
        flat = [_flatten(x) for x in sample]
        names = sorted({k for f in flat for k, v in f.items() if not isinstance(v, (dict, list))})
        fields = [{"name": n, "example": next((f[n] for f in flat if n in f), None)} for n in names]

        labels, time_field, id_field = [], None, None
        for n in names:
            vals = [f.get(n) for f in flat if n in f]
            if not vals:
                continue
            first = vals[0]
            if isinstance(first, bool):
                continue
            if isinstance(first, (int, float)):
                if _parse_time(first) and n.lower() in ("time", "timestamp", "ts", "created_at",
                                                        "updated_at", "date"):
                    time_field = time_field or n
                    continue
                labels.append({"name": n.replace(".", "_"), "field": n, "type": "number"})
            elif isinstance(first, str):
                last = n.rsplit(".", 1)[-1].lower()
                if not time_field and _parse_time(first) and any(
                        w in last for w in ("time", "date", "at", "ts")):
                    time_field = n
                    continue
                if not id_field and (last == "id" or last.endswith("_id")) \
                        and len(set(map(str, vals))) == len(vals):
                    id_field = n
                    continue
                # short strings are names of things (a service, a city, a status) and make
                # labels; long ones are messages and stay in the payload
                if all(isinstance(v, str) and len(v) <= 64 for v in vals):
                    labels.append({"name": n.replace(".", "_"), "field": n})
        # the first string label is the entity; a source with none keys by its own name
        primary_set = False
        for lab in labels:
            if lab.get("type") != "number" and not primary_set:
                lab["primary"] = True
                primary_set = True
        proposed = {"url": config["url"]}
        for k in ("method", "headers", "auth_header", "body"):
            if config.get(k) not in (None, "", {}):
                proposed[k] = config[k]
        if items_path:
            proposed["items_path"] = items_path
        if time_field:
            proposed["event_time_field"] = time_field
        if id_field and per_poll > 1:
            proposed["id_field"] = id_field
        elif per_poll == 1 and time_field:
            # a reading carries its own time; polling faster than the API updates would store
            # the same reading again and again, so the time is the id
            proposed["id_field"] = time_field
        proposed["labels"] = labels[:8]
        return {
            "connector": "http_poll",
            "summary": (f"{per_poll} item{'s' if per_poll != 1 else ''} per poll, "
                        f"{len(names)} field{'s' if len(names) != 1 else ''}"
                        + (f", items at {items_path}" if items_path else "")),
            "sample_fields": names,
            "fields": fields,
            "proposed_config": proposed,
        }


_FIELD_REF = re.compile(r"\{([A-Za-z0-9_.]+)\}")


def _render(template: str, ctx: dict) -> str:
    """`{name}` and `{a.b}` read the flattened context; str.format cannot, a dot there means
    attribute access. A reference to a missing field leaves the line empty so the item's JSON
    is shown instead of a half-rendered sentence."""
    if not template:
        return ""
    missing = False

    def sub(m):
        nonlocal missing
        v = ctx.get(m.group(1))
        if v is None:
            missing = True
            return ""
        return str(v)
    out = _FIELD_REF.sub(sub, template)
    return "" if missing else out
