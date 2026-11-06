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

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

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
