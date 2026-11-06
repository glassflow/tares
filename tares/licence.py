"""Read a Tares licence key and say what the cell may do. Offline and pure: no store, no HTTP,
no environment. Where the key text comes from is the caller's business.

The check is a two-step chain. A root key (its public half ships in ROOT_PUBLIC_KEYS) signs an
issuing key; the issuing key signs the licence. The licence carries its issuing key's
certificate, so the check needs nothing but the text and the shipped root keys.

Format (a shared contract, other tools produce it):

- All base64 is base64url WITHOUT '=' padding. Signed JSON is produced with
  json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(). The
  verifier checks the bytes it received and never re-serialises the payload.
- Licence text: TL1.<b64u(payload bytes)>.<b64u(Ed25519 signature of payload bytes by the issuing
  key)>. Leading and trailing whitespace is stripped before parsing.
- Payload: {"v": 1, "kid": str, "org": str, "seats": int >= 1, "features": [str],
  "issued": "YYYY-MM-DDTHH:MM:SSZ", "expires": "YYYY-MM-DD",
  "issuer": {"kid": str, "public_key": b64u(32 raw bytes), "not_after": "YYYY-MM-DD",
  "root_kid": str, "root_signature": b64u}}.
- issuer.root_signature is the root key's Ed25519 signature over the canonical JSON of
  {"kid", "public_key", "not_after"} of the issuer (those three only).
- The kid of a root or issuing key is the first 16 hex characters of sha256(raw 32-byte public
  key). A licence's own kid is any string.

Expiry is not a reason for Invalid: an expired licence still verifies, and `expired()` says so.
Unknown features are kept and ignored. verify() never raises on garbage input.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import NoReturn

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

LICENCE_URL = "https://docs.glassflow.ai/tares/enterprise"

# Root kid -> raw 32-byte Ed25519 public key. The private half is kept offline (TR-907).
ROOT_PUBLIC_KEYS: dict[str, bytes] = {
    "fd683bfc46806063": bytes.fromhex(
        "bebfb4028e29e36ee2e96a75fdf1031d94cad1e67873431807f10254c41a6137"
    ),
}

_roots: dict[str, bytes] = ROOT_PUBLIC_KEYS

_PREFIX = "TL1"
_CLOCK_SKEW = timedelta(minutes=10)
_B64U = re.compile(r"[A-Za-z0-9_-]*")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


@dataclass(frozen=True, repr=False)
class Licence:
    kid: str
    org: str
    seats: int
    features: tuple
    issued: datetime
    expires: date
    issuer_kid: str
    root_kid: str

    def __repr__(self) -> str:
        return f"Licence(org={self.org!r}, seats={self.seats}, expires={self.expires.isoformat()})"


@dataclass(frozen=True)
class Invalid:
    reason: str


class _Bad(Exception):
    """A reason, in plain words, that the text is not a good licence."""


def _set_root_keys_for_tests(keys: dict[str, bytes] | None) -> None:
    """Replace the trusted root keys in this process; None restores the shipped ones. The only
    way a test root is trusted: there is no environment variable for it."""
    global _roots
    _roots = ROOT_PUBLIC_KEYS if keys is None else dict(keys)


def _b64u_decode(s: str) -> bytes:
    if not isinstance(s, str) or not _B64U.fullmatch(s) or len(s) % 4 == 1:
        raise ValueError("bad base64")
    raw = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode() != s:
        raise ValueError("bad base64")
    return raw


def kid_of(raw_public: bytes) -> str:
    return hashlib.sha256(raw_public).hexdigest()[:16]


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _str_field(d: dict, name: str, where: str = "licence") -> str:
    v = d.get(name)
    if not isinstance(v, str):
        raise _Bad(f"the {where} has no valid {name}")
    return v


def _date_field(d: dict, name: str, where: str = "licence") -> date:
    v = d.get(name)
    if not isinstance(v, str) or not _DATE.fullmatch(v):
        raise _Bad(f"the {where} has no valid {name} date")
    try:
        return date.fromisoformat(v)
    except ValueError:
        raise _Bad(f"the {where} has no valid {name} date") from None


def _stamp_field(d: dict, name: str) -> datetime:
    v = d.get(name)
    if not isinstance(v, str) or not _STAMP.fullmatch(v):
        raise _Bad(f"the licence has no valid {name} time")
    try:
        return datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise _Bad(f"the licence has no valid {name} time") from None


def _utc(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _check_sig(raw_public: bytes, sig: bytes, data: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(raw_public).verify(sig, data)
        return True
    except (InvalidSignature, ValueError):
        return False


def _verify(text, now: datetime | None) -> Licence:
    if not isinstance(text, str):
        raise _Bad("the licence is not text")
    parts = text.strip().split(".")
    if len(parts) != 3 or parts[0] != _PREFIX:
        raise _Bad("this is not a Tares licence key")
    try:
        payload_bytes = _b64u_decode(parts[1])
        signature = _b64u_decode(parts[2])
    except ValueError:
        raise _Bad("the licence key is damaged (it was probably cut or changed when copied)") from None
    try:
        p = json.loads(payload_bytes.decode("utf-8"))
    except ValueError:
        raise _Bad("the licence key is damaged (it was probably cut or changed when copied)") from None
    if not isinstance(p, dict):
        raise _Bad("the licence key is damaged (it was probably cut or changed when copied)")

    v = p.get("v")
    if not _is_int(v):
        raise _Bad("the licence has no valid version")
    if v != 1:
        raise _Bad("this licence needs a newer Tares, update Tares and try again")
    kid = _str_field(p, "kid")
    org = _str_field(p, "org")
    seats = p.get("seats")
    if not _is_int(seats) or seats < 1:
        raise _Bad("the licence has no valid seats count")
    features = p.get("features")
    if not isinstance(features, list) or not all(isinstance(f, str) for f in features):
        raise _Bad("the licence has no valid feature list")
    issued = _stamp_field(p, "issued")
    expires = _date_field(p, "expires")
    iss = p.get("issuer")
    if not isinstance(iss, dict):
        raise _Bad("the licence has no issuing key")
    iss_kid = _str_field(iss, "kid", "issuing key")
    iss_pub_s = _str_field(iss, "public_key", "issuing key")
    not_after_s = _str_field(iss, "not_after", "issuing key")
    not_after = _date_field(iss, "not_after", "issuing key")
    root_kid = _str_field(iss, "root_kid", "issuing key")
    root_sig_s = _str_field(iss, "root_signature", "issuing key")
    try:
        iss_pub = _b64u_decode(iss_pub_s)
        root_sig = _b64u_decode(root_sig_s)
    except ValueError:
        raise _Bad("the licence key is damaged (it was probably cut or changed when copied)") from None
    if len(iss_pub) != 32:
        raise _Bad("the licence's issuing key is not a valid key")

    root_pub = _roots.get(root_kid)
    if root_pub is None:
        raise _Bad("the licence is not signed by a key this Tares trusts")
    if kid_of(iss_pub) != iss_kid:
        raise _Bad("the licence's issuing key does not match its name")
    cert = _canonical({"kid": iss_kid, "public_key": iss_pub_s, "not_after": not_after_s})
    if not _check_sig(root_pub, root_sig, cert):
        raise _Bad("the licence is not signed by a key this Tares trusts")
    if not _check_sig(iss_pub, signature, payload_bytes):
        raise _Bad("the licence signature does not match its contents")

    cutoff = datetime(not_after.year, not_after.month, not_after.day, 23, 59, 59, tzinfo=timezone.utc)
    if issued > cutoff:
        raise _Bad("the licence was issued after its issuing key ran out")
    if issued > _utc(now) + _CLOCK_SKEW:
        raise _Bad("the licence is dated in the future, check this machine's clock")
    return Licence(kid=kid, org=org, seats=seats, features=tuple(features), issued=issued,
                   expires=expires, issuer_kid=iss_kid, root_kid=root_kid)


def verify(text, now: datetime | None = None) -> Licence | Invalid:
    """The licence the text holds, or Invalid with a reason in plain words. Never raises."""
    try:
        return _verify(text, now)
    except _Bad as e:
        return Invalid(str(e))
    except Exception:
        return Invalid("the licence key could not be read")


def expired(lic: Licence, now: datetime | None = None) -> bool:
    """True from 00:00:00 UTC of the day after `expires`."""
    end = datetime(lic.expires.year, lic.expires.month, lic.expires.day, tzinfo=timezone.utc) + timedelta(days=1)
    return _utc(now) >= end


def state(text: str | None, used: int | None, now: datetime | None = None, source: str = "none") -> dict:
    """What the cell may do with this key, as a plain dict. An expired licence stays valid;
    `expired` says it ran out. `used` is the seats in use (None counts as 0)."""
    used = used or 0
    st = {"valid": False, "source": "none", "kid": None, "org": None, "seats": None, "used": used,
          "features": [], "expires": None, "days_left": None, "expired": False,
          "over_seats": False, "ee_allowed": False, "reason": None}
    if text is None or (isinstance(text, str) and not text.strip()):
        return st
    st["source"] = source
    lic = verify(text, now)
    if isinstance(lic, Invalid):
        st["reason"] = lic.reason
        return st
    today = _utc(now).date()
    st.update(valid=True, kid=lic.kid, org=lic.org, seats=lic.seats, features=list(lic.features),
              expires=lic.expires.isoformat(), days_left=(lic.expires - today).days,
              expired=expired(lic, now), over_seats=used > lic.seats,
              ee_allowed="enterprise" in lic.features)
    return st


def allows(st: dict, feature: str = "enterprise") -> bool:
    return bool(st.get("valid")) and feature in st.get("features", ())


# ── the cell side (TR-912): where the key comes from, and what the cell does about it ──
# Everything above is pure. Below, the cell reads its key from the environment or from its stored
# setting, remembers the answer for a minute, warns on the desk, and answers 402 to an action
# that needs a licence. The key text is never returned, logged or cached outside the verify call.

LICENCE_ENV = "TARES_LICENSE"
LICENCE_SETTING = "licence_key"
WARNED_SETTING = "licence_warned"   # JSON list of "<kid>:<threshold>" the desk was told about
CACHE_SECONDS = 60

# (store id, monotonic time, state) of the last answer; ee_allowed() reads the state.
# One cell has one store, so one slot is enough; the id only keeps a second store (a test) from
# reading another's answer. current() edits `used` in place; callers get copies.
_cache: tuple | None = None


def resolve(store) -> tuple[str | None, str]:
    """(key text, source). The environment wins when it is set and not blank, even when its key
    is not valid; else the stored setting; else (None, "none")."""
    env = os.environ.get(LICENCE_ENV, "").strip()
    if env:
        return env, "env"
    stored = (store.get_setting(LICENCE_SETTING) or "").strip()
    if stored:
        return stored, "settings"
    return None, "none"


def _seats_in_use(st: dict, used: int) -> dict:
    st["used"] = used
    st["over_seats"] = bool(st["valid"] and used > (st["seats"] or 0))
    return st


def current(store, now: datetime | None = None, force: bool = False) -> dict:
    """The cell's licence as `state()` says it, plus `stored` (a key is saved on the cell) and
    `env_set` (TARES_LICENSE is set). The check is remembered for CACHE_SECONDS so a request does
    not repeat it; `force` checks again now (after a key is saved or removed). The seats in use
    are always read fresh."""
    global _cache
    used = len(store.list_people())
    c = _cache
    if not force and c is not None and c[0] == id(store) and time.monotonic() - c[1] < CACHE_SECONDS:
        _seats_in_use(c[2], used)   # keep the remembered answer's seat count current for peek()
        return dict(c[2])
    text, source = resolve(store)
    st = state(text, used, now, source)
    st["stored"] = bool(store.get_setting(LICENCE_SETTING))
    st["env_set"] = bool(os.environ.get(LICENCE_ENV, "").strip())
    _cache = (id(store), time.monotonic(), st)
    return dict(st)


def peek() -> dict | None:
    """The last answer `current()` gave, without reading the state database (a metrics scrape
    must not). None before the first check. `used` is as fresh as the last `current()` call."""
    c = _cache
    return dict(c[2]) if c is not None else None


def ee_allowed(feature: str = "enterprise") -> bool:
    """Whether the last licence check allows `feature`. False before the first check."""
    c = _cache
    return c is not None and allows(c[2], feature)


class LicenceRequired(Exception):
    """An action needs a licence the cell does not have. The daemon answers it with HTTP 402."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def licence_required(message: str) -> NoReturn:
    raise LicenceRequired(message)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _warnings(st: dict) -> list[tuple[str, str]]:
    """(threshold, question) for each warning this state calls for, most urgent first."""
    org, exp = st["org"], st["expires"]
    out = []
    days = st["days_left"]
    if st["expired"] or days <= 0:
        if st["expired"]:
            out.append(("0", f"The enterprise licence for {org} ran out on {exp}. Add a new key "
                             "under Settings, Licence, or the enterprise features stop working."))
        else:
            out.append(("0", f"The enterprise licence for {org} ends today ({exp}). Add a new key "
                             "under Settings, Licence."))
    elif days <= 7:
        out.append(("7", f"The enterprise licence for {org} ends in {_plural(days, 'day')} ({exp})."))
    elif days <= 30:
        out.append(("30", f"The enterprise licence for {org} ends in {_plural(days, 'day')} ({exp})."))
    if st["over_seats"]:
        out.append(("seats", f"The enterprise licence for {org} covers {_plural(st['seats'], 'seat')} "
                             f"and {'1 person is' if st['used'] == 1 else str(st['used']) + ' people are'} on the cell."))
    return out


def _crossed(st: dict) -> list[str]:
    """Every threshold the state has passed, whether or not it was warned about."""
    days = st["days_left"]
    got = [t for t, limit in (("30", 30), ("7", 7), ("0", 0)) if days <= limit]
    if st["over_seats"]:
        got.append("seats")
    return got


def licence_desk(store, now: datetime | None = None) -> list[dict]:
    """Tell the desk about a licence that is close to its end, has ended, or covers fewer seats
    than there are people. Each threshold (30 days, 7 days, ended, over seats) is told once per
    licence (its kid); a renewed licence has a new kid and starts fresh. When a licence is first
    seen past several thresholds, only the most urgent is posted and the others count as told.
    A licence that is not valid says nothing here. Returns the desk items added."""
    st = current(store, now, force=now is not None)
    if not st["valid"]:
        return []
    kid = st["kid"]
    try:
        warned = [w for w in json.loads(store.get_setting(WARNED_SETTING) or "[]")
                  if isinstance(w, str) and w.startswith(kid + ":")]
    except ValueError:
        warned = []
    added = []
    told = list(warned)
    try:
        for threshold, question in _warnings(st):
            if f"{kid}:{threshold}" in warned:
                continue
            added.append(store.add_desk_item(None, question, "", [], "", "", "", [], False, "tares",
                                             [], _utc(now)))
            told.append(f"{kid}:{threshold}")
        # what was crossed counts as told, so a lower threshold passed together with the urgent
        # one is not posted later
        told += [f"{kid}:{t}" for t in _crossed(st) if f"{kid}:{t}" not in told]
    finally:
        # the list keeps the keys of this licence only: a renewal (a new kid) starts fresh
        if told != warned:
            store.set_setting(WARNED_SETTING, json.dumps(told))
    return added


async def run(store, stop: asyncio.Event) -> None:
    """The cell's licence watch: every minute, refresh the check and warn on the desk. Never
    stops on an error."""
    while not stop.is_set():
        try:
            await asyncio.to_thread(licence_desk, store)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"taresd: licence check failed: {type(e).__name__}", flush=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=CACHE_SECONDS)
        except asyncio.TimeoutError:
            pass
