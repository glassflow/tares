"""Signature checks for push sources: GitHub, Linear and generic HMAC-SHA256 webhooks.

A push source's ingest URL ends up in a third party's settings page and delivery logs, so for those
senders the URL should not be the only guard. A source that names a signature scheme gets every
delivery checked against its secret here, on the raw body, before anything is parsed:

* `github`: `X-Hub-Signature-256: sha256=<hex HMAC-SHA256(secret, body)>`.
* `linear`: `Linear-Signature: <hex HMAC-SHA256(secret, body)>`, and the body's `webhookTimestamp`
  (milliseconds) no older than 60 seconds, so a captured delivery cannot be replayed.
* `hmac_sha256`: a configurable header holding the hex digest, with or without a `sha256=` prefix.

The same three rules as slack_verify.py make it real protection: the HMAC covers the bytes exactly
as sent (callers pass `await request.body()`), digests are compared with `hmac.compare_digest`, and
a source that requires a signature but has no secret refuses everything rather than accepting
everything. A source that checks signatures needs no Tares key on its ingest URL (GitHub cannot
send one); the signature is its authentication.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

SCHEMES = ("github", "linear", "hmac_sha256")
LINEAR_MAX_AGE_MS = 60_000

HEADERS = {"github": "x-hub-signature-256", "linear": "linear-signature"}


def sign(scheme: str, secret: str, body: bytes) -> str:
    """The header value a sender would put on `body`; also the tests' known-good vectors."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}" if scheme == "github" else digest


def header_for(scheme: str, header: str | None = None) -> str:
    return (header or HEADERS.get(scheme) or "x-signature").lower()


def verify(scheme: str, secret: str, body: bytes, headers: dict, header: str | None = None) -> str | None:
    """None when the delivery is genuine, else the reason it was refused (for the 401 and the log;
    never the body or the secret)."""
    if scheme not in SCHEMES:
        return f"unknown signature scheme {scheme!r}"
    if not secret:
        return f"no {scheme} signing secret is configured for this source"
    hdrs = {k.lower(): v for k, v in (headers or {}).items()}
    got = str(hdrs.get(header_for(scheme, header)) or "").strip()
    if not got:
        return f"missing {header_for(scheme, header)} header"
    want = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if scheme == "github":
        if not got.startswith("sha256="):
            return "signature is not sha256=<hex>"
        got = got[len("sha256="):]
    elif got.lower().startswith("sha256="):
        got = got[len("sha256="):]
    if not hmac.compare_digest(want, got.lower()):
        return f"{scheme} signature does not match"
    if scheme == "linear":
        try:
            ts = int((json.loads(body or b"{}") or {}).get("webhookTimestamp") or 0)
        except (ValueError, TypeError, AttributeError):
            ts = 0
        if not ts or abs(time.time() * 1000 - ts) > LINEAR_MAX_AGE_MS:
            return "linear webhookTimestamp missing or older than 60 seconds"
    return None
