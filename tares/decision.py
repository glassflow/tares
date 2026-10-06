"""Decision models (P-TR-223): a System One model as a watcher agent's judgment.

A decision model does not chat or call tools. It takes a `state` (text or JSON) and typed
`questions`, and answers each with probabilities in one pass: `noul` (how likely a statement is
true), `choice` (one of a set of options, a probability for each), `score` (a place on a scale).
Cloudflare's Clef and TypeSafe's Jev take the same request shape, so one client covers both; a
self-hosted model that speaks it (Kev, Laya behind a small server) is a custom URL.

A cell holds a list of endpoints, set under Settings like the model providers (TR-381):
- `cloudflare`: Workers AI. An account ID and an API token with Workers AI Read and Edit;
  the model is in the URL (`@cf/cloudflare/<model>`).
- `typesafe`: TypeSafe's hosted API, an API key; the model is in the body.
- `custom`: any URL that takes the same body, with an optional bearer key.
Tokens are never returned: the listing says whether one is stored. Blank keeps the stored one.

A watcher agent names an endpoint in its `decision` settings (TR-324). Each run asks two
questions about the window it was handed: `problem` (noul, the probability that something needs
a closer look; the agent's prompt says what counts) and `entity` (choice over the label values
in the window, plus none). At or above the threshold it concludes a finding with verdict
investigate on that entity; below it, or in shadow mode, it concludes no_op. The probabilities
are stored on the run either way.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

import httpx

SETTING = "decision_endpoints"
KINDS = ("cloudflare", "typesafe", "custom")
KIND_LABELS = {"cloudflare": "Cloudflare Workers AI", "typesafe": "TypeSafe Jev",
               "custom": "Custom URL"}
MODELS = {"cloudflare": ["clef-flash", "clef"], "typesafe": ["jev-latest"], "custom": []}
CLOUDFLARE_API = "https://api.cloudflare.com/client/v4/accounts"
TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
TIMEOUT = 30.0
# USD per million input tokens; a model not listed costs None (tokens recorded, cost unknown)
PRICING = {"clef-flash": 0.09}

VERDICT = "investigate"
DEFAULT_THRESHOLD = 0.5
MAX_OPTIONS = 40     # entity options offered, largest change first (the API takes up to 255)
DEFAULT_PROBLEM = (
    "Something in this window needs a closer look from an SRE: errors, failures, 4xx or 5xx "
    "codes, or a sudden new source of traffic. Routine noise such as uptime probes and health "
    "checks, and values that only went down, do not count.")


class DecisionError(Exception):
    """A call that did not produce answers; the message is meant for a person."""


# ── endpoints, as the cell stores them ───────────────────────────────────────
def _stored(store) -> list[dict]:
    raw = store.get_setting(SETTING)
    try:
        entries = json.loads(raw) if raw else []
    except ValueError:
        entries = []
    return [e for e in entries if isinstance(e, dict) and e.get("id") and e.get("kind") in KINDS]


def _save(store, entries: list[dict]) -> None:
    store.set_setting(SETTING, json.dumps(entries) if entries else None)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")[:40]


def configured(e: dict) -> bool:
    if e["kind"] == "cloudflare":
        return bool(e.get("account_id") and e.get("key"))
    if e["kind"] == "typesafe":
        return bool(e.get("key"))
    return bool(e.get("url"))


def entry(store, endpoint_id: str) -> dict | None:
    return next((e for e in _stored(store) if e["id"] == endpoint_id), None)


def list_endpoints(store) -> dict:
    """The redacted listing the console shows: no token, only whether one is stored."""
    rows = [{"id": e["id"], "kind": e["kind"], "name": e.get("name") or KIND_LABELS[e["kind"]],
             "account_id": e.get("account_id") or "", "url": e.get("url") or "",
             "key_stored": bool(e.get("key")), "configured": configured(e),
             "models": MODELS[e["kind"]] or ([e["model"]] if e.get("model") else [])}
            for e in _stored(store)]
    return {"endpoints": rows,
            "kinds": [{"id": k, "label": KIND_LABELS[k], "models": MODELS[k]} for k in KINDS]}


def _check_url(url: str) -> str:
    url = url.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("the URL must be an http(s) URL with a host")
    return url


def save_endpoint(store, endpoint_id: str, kind: str, name: str = "", key: str = "",
                  account_id: str = "", url: str = "", model: str = "") -> str:
    """Create or update one endpoint; returns its id. A blank key keeps the stored one."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    entries = _stored(store)
    eid = endpoint_id or _slug(name or KIND_LABELS[kind])
    if not eid:
        raise ValueError("the endpoint needs a name")
    current = next((e for e in entries if e["id"] == eid), None)
    if current and current["kind"] != kind:
        if not endpoint_id:
            raise ValueError(f"an endpoint named {current.get('name') or eid!r} already exists; "
                             "pick another name")
        raise ValueError(f"{current.get('name') or eid} keeps its kind; add a new endpoint instead")
    cur = current or {}
    e = {"id": eid, "kind": kind,
         "name": name.strip() or cur.get("name") or KIND_LABELS[kind],
         "key": key.strip() or cur.get("key") or ""}
    if kind == "cloudflare":
        acct = account_id.strip() or cur.get("account_id") or ""
        if not re.fullmatch(r"[0-9a-fA-F]{32}", acct):
            raise ValueError("the Cloudflare Account ID is 32 hexadecimal characters; it is on "
                             "the Workers AI page under Use REST API")
        e["account_id"] = acct.lower()
        if not e["key"]:
            raise ValueError("add the Cloudflare API token (Workers AI Read and Edit)")
    elif kind == "typesafe":
        if not e["key"]:
            raise ValueError("add the TypeSafe API key")
    else:
        raw = url.strip() or cur.get("url") or ""
        if not raw:
            raise ValueError("a custom endpoint needs its URL")
        e["url"] = _check_url(raw)
        e["model"] = model.strip() or cur.get("model") or ""
    _save(store, [x for x in entries if x["id"] != eid] + [e])
    return eid


def delete_endpoint(store, endpoint_id: str) -> None:
    _save(store, [e for e in _stored(store) if e["id"] != endpoint_id])


# ── the call ──────────────────────────────────────────────────────────────────
def _request(e: dict, model: str) -> tuple[str, dict, dict]:
    """(url, headers, the body fields besides state and questions)."""
    headers = {"Authorization": f"Bearer {e['key']}"} if e.get("key") else {}
    if e["kind"] == "cloudflare":
        return f"{CLOUDFLARE_API}/{e['account_id']}/ai/run/@cf/cloudflare/{model}", headers, \
            {"model": model}
    if e["kind"] == "typesafe":
        return TYPESAFE_URL, headers, {"model": model}
    return e["url"], headers, ({"model": model} if model else {})


def _plain_error(status: int, body: str, kind: str) -> str:
    """What went wrong, in words a person can act on."""
    detail = ""
    try:
        data = json.loads(body)
        errs = data.get("errors") if isinstance(data, dict) else None
        if errs and isinstance(errs, list) and isinstance(errs[0], dict):
            detail = str(errs[0].get("message") or "")
        elif isinstance(data, dict):
            detail = str(data.get("error") or data.get("message") or data.get("detail") or "")
    except ValueError:
        detail = body.strip()[:200]
    if status in (401, 403):
        what = ("check the token has Workers AI Read and Edit on this account"
                if kind == "cloudflare" else "check the key")
        msg = f"the endpoint refused the credential ({status}): {what}"
    elif status == 404:
        msg = ("not found (404): check the Account ID and the model name" if kind == "cloudflare"
               else "not found (404): check the URL and the model name")
    elif status == 429:
        msg = "rate limited (429): the endpoint asks to slow down"
    else:
        msg = f"the endpoint answered {status}"
    return f"{msg}{': ' + detail if detail and detail not in msg else ''}"


async def decide(e: dict, model: str, state, questions: dict,
                 timeout: float = TIMEOUT) -> dict:
    """Ask `questions` about `state`. Returns {answers, input_tokens, raw}; raises DecisionError
    with a sentence for a person when there are no answers."""
    if not configured(e):
        raise DecisionError(f"{e.get('name') or e['id']} has no credential yet (Settings, "
                            "Decision models)")
    url, headers, extra = _request(e, model)
    body = {**extra, "state": state, "questions": questions}
    try:
        async with httpx.AsyncClient(timeout=timeout) as cx:
            r = await cx.post(url, json=body, headers=headers)
    except httpx.TimeoutException:
        raise DecisionError(f"the endpoint did not answer within {int(timeout)}s")
    except httpx.HTTPError as ex:
        raise DecisionError(f"could not reach the endpoint: {type(ex).__name__}: {ex}"[:300])
    if not 200 <= r.status_code < 300:
        raise DecisionError(_plain_error(r.status_code, r.text, e["kind"]))
    try:
        data = r.json()
    except ValueError:
        raise DecisionError("the endpoint answered with something that is not JSON")
    # Workers AI wraps the model's output in {result, success, errors}
    if isinstance(data, dict) and isinstance(data.get("result"), dict):
        data = data["result"]
    answers = data.get("answers") if isinstance(data, dict) else None
    if not isinstance(answers, dict) or not answers:
        raise DecisionError("the endpoint answered without `answers`: "
                            + json.dumps(data)[:300])
    usage = data.get("usage") or {}
    tokens = 0
    if isinstance(usage, dict):
        for k in ("input_tokens", "prompt_tokens", "total_tokens"):
            if isinstance(usage.get(k), (int, float)):
                tokens = int(usage[k])
                break
    return {"answers": answers, "input_tokens": tokens, "raw": data}


def probability(answer) -> float | None:
    """The probability a noul answer gives its statement. Read leniently: a bare number, or one
    of the field names the providers use."""
    if isinstance(answer, bool):
        return None
    if isinstance(answer, (int, float)):
        return float(answer)
    if isinstance(answer, dict):
        # Workers AI (Clef) answers {"type": "noul", "noul": 0.90}
        for k in ("noul", "probability", "p", "true", "value", "score"):
            if isinstance(answer.get(k), (int, float)) and not isinstance(answer.get(k), bool):
                return float(answer[k])
        probs = answer.get("probabilities")
        if isinstance(probs, dict):
            for k in ("true", "yes", "True"):
                if isinstance(probs.get(k), (int, float)):
                    return float(probs[k])
    return None


def choice(answer) -> tuple[str | None, dict]:
    """(the chosen option id, {option id: probability}) of a choice answer."""
    if not isinstance(answer, dict):
        return (answer if isinstance(answer, str) else None), {}
    probs = answer.get("probabilities") if isinstance(answer.get("probabilities"), dict) else {}
    probs = {str(k): float(v) for k, v in probs.items() if isinstance(v, (int, float))}
    picked = answer.get("choice")
    if not isinstance(picked, str) and probs:
        picked = max(probs, key=probs.get)
    return picked, probs


def price(model: str, input_tokens: int) -> float | None:
    rate = PRICING.get(model)
    return None if rate is None else round(input_tokens * rate / 1_000_000, 8)


async def test(e: dict, model: str) -> dict:
    """One small request, to tell a person whether the endpoint works."""
    out = await decide(e, model, "The checkout service returned 500 errors for ten minutes.",
                       {"problem": {"type": "noul",
                                    "instructions": "Something here needs an engineer's attention."}},
                       timeout=20.0)
    p = probability(out["answers"].get("problem"))
    if p is None:
        raise DecisionError("the endpoint answered, but not with a probability: "
                            + json.dumps(out["answers"])[:300])
    return {"ok": True, "probability": round(p, 4), "input_tokens": out["input_tokens"]}


# ── a watcher's run ──────────────────────────────────────────────────────────
def normalize(name: str, d) -> dict:
    """An agent's decision settings: {endpoint, model, threshold, shadow}; {} when unset."""
    if d in (None, "", {}):
        return {}
    if not isinstance(d, dict):
        raise ValueError(f"agent {name!r}: decision must be a mapping of endpoint, model, "
                         "threshold and shadow")
    endpoint = str(d.get("endpoint") or "").strip()
    if not endpoint:
        raise ValueError(f"agent {name!r}: decision needs an endpoint (Settings, Decision models)")
    t = d.get("threshold", DEFAULT_THRESHOLD)
    try:
        t = float(DEFAULT_THRESHOLD if t in (None, "") else t)
    except (TypeError, ValueError):
        raise ValueError(f"agent {name!r}: the threshold is a number between 0 and 1")
    if not 0 < t < 1:
        raise ValueError(f"agent {name!r}: the threshold is a number between 0 and 1")
    return {"endpoint": endpoint, "model": str(d.get("model") or "").strip(),
            "threshold": t, "shadow": bool(d.get("shadow"))}


def model_for(e: dict, settings: dict) -> str:
    return settings.get("model") or (MODELS[e["kind"]] or [e.get("model") or ""])[0]


def questions(problem: str, label: str | None, rows: list[dict]) -> tuple[dict, dict]:
    """The questions for one window, and the entity option ids mapped back to their values.
    Option ids are o1..oN, not the values: a value can be any string (a path, an address)."""
    qs = {"problem": {"type": "noul", "instructions": problem.strip() or DEFAULT_PROBLEM}}
    ids: dict[str, dict] = {}
    if label and rows:
        criteria = {}
        for i, r in enumerate(rows[:MAX_OPTIONS], 1):
            oid = f"o{i}"
            ids[oid] = r
            criteria[oid] = (f"{label}={r['value']} (now {r['now']}, before {r['before']}, "
                             f"change {r['change']})")
        criteria["none"] = f"no {label} stands out"
        qs["entity"] = {"type": "choice", "criteria": criteria,
                        "instructions": f"Which {label} does the problem concern?"}
    return qs, ids


def outcome(answers: dict, settings: dict, label: str | None, ids: dict,
            key: str) -> tuple[dict, dict]:
    """(the conclude outcome, the scores stored on the run) for one decision call."""
    p = probability(answers.get("problem"))
    if p is None:
        raise DecisionError("the endpoint answered, but `problem` carries no probability: "
                            + json.dumps(answers.get("problem"))[:300])
    t = settings["threshold"]
    row, entity_p, options = None, None, []
    if ids:
        picked, probs = choice(answers.get("entity"))
        row = ids.get(picked or "")
        entity_p = probs.get(picked) if picked else None
        options = sorted(({"value": ids[k]["value"] if k in ids else k, "p": round(v, 4)}
                          for k, v in probs.items()), key=lambda o: -o["p"])[:5]
        value = row["value"] if row else None
    else:
        value = key    # a trigger that fired on one entity: that entity
    escalate = p >= t and value is not None
    scores = {"problem": round(p, 4), "threshold": t, "shadow": settings["shadow"],
              "escalate": escalate, "label": label, "entity": value,
              **({"entity_p": round(entity_p, 4)} if entity_p is not None else {}),
              **({"options": options} if options else {})}
    pct = f"{p:.0%}"
    if not escalate:
        why = (f"probability {p:.2f} is below the threshold {t:g}" if p < t
               else f"probability {p:.2f}, but no {label or 'entity'} stands out")
        return {"outcome": "no_op", "summary": why}, scores
    who = f"{label}={value}" if label else str(value)
    moved = (f"now {row['now']}, before {row['before']}, change {row['change']}" if row else "")
    if settings["shadow"]:
        return {"outcome": "no_op",
                "summary": f"shadow: would have escalated {who} (probability {p:.2f}, "
                           f"threshold {t:g}){'; ' + moved if moved else ''}"}, scores
    summary = (f"{who} needs a closer look: the decision model gives {pct} that this window "
               f"shows a problem (threshold {t:g})"
               + (f"; {moved}" if moved else "")
               + (f". It picked {value} with {entity_p:.0%}." if entity_p is not None else "."))
    return {"outcome": "finding", "summary": summary, "verdict": VERDICT, "key": str(value),
            "label": label, "headline": f"{who}: {pct} likely a problem"
            + (f" ({row['change']})" if row else "")}, scores
