"""Helpers that make Tares licences for tests (TR-908). Not a test itself.

Keys come from a phrase: the Ed25519 seed is sha256(phrase), so a phrase always gives the same
key and the same licence text (Ed25519 signatures are deterministic). The test root is trusted
only when a test installs it with tares.licence._set_root_keys_for_tests.

    root_priv, root_pub = keypair(ROOT_PHRASE)
    iss_priv, iss_pub = keypair(ISSUER_PHRASE)
    cert = issuer_cert(root_priv, iss_pub, "2027-11-05")
    text = sign(iss_priv, cert, "Test Org", 5, ["enterprise"], "2026-11-05T00:00:00Z", "2027-11-05", "lic_x")

daemon_argv() is the command line for a daemon process that trusts the test root, for tests that
start one (TR-912, TR-913, TR-915).
"""
import base64
import hashlib
import json
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT_PHRASE = "tares licence test root"
ISSUER_PHRASE = "tares licence test issuer"


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def keypair(phrase: str):
    """(private key, raw 32-byte public key) from sha256(phrase) as the Ed25519 seed."""
    priv = Ed25519PrivateKey.from_private_bytes(hashlib.sha256(phrase.encode()).digest())
    raw = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return priv, raw


def kid_of(raw_public: bytes) -> str:
    return hashlib.sha256(raw_public).hexdigest()[:16]


def issuer_cert(root_priv, issuer_raw_pub: bytes, not_after: str, root_kid: str | None = None) -> dict:
    """The "issuer" object of a payload: the issuing key, vouched for by the root key."""
    body = {"kid": kid_of(issuer_raw_pub), "public_key": b64u(issuer_raw_pub), "not_after": not_after}
    if root_kid is None:
        root_kid = kid_of(root_priv.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    return {**body, "root_kid": root_kid, "root_signature": b64u(root_priv.sign(canonical(body)))}


def payload_of(cert, org, seats, features, issued, expires, kid, v=1) -> dict:
    return {"v": v, "kid": kid, "org": org, "seats": seats, "features": list(features),
            "issued": issued, "expires": expires, "issuer": cert}


def sign_payload(issuer_priv, payload: dict) -> str:
    """Licence text for any payload dict, signed by the issuing key. Tests use it to build
    payloads that are wrong on purpose."""
    raw = canonical(payload)
    return f"TL1.{b64u(raw)}.{b64u(issuer_priv.sign(raw))}"


def sign(issuer_priv, cert, org, seats, features, issued, expires, kid) -> str:
    return sign_payload(issuer_priv, payload_of(cert, org, seats, features, issued, expires, kid))


def split(text: str):
    """(payload dict, signature bytes) of a licence text, for tests that edit one."""
    _, p, s = text.split(".")
    pad = lambda x: x + "=" * (-len(x) % 4)
    return json.loads(base64.urlsafe_b64decode(pad(p))), base64.urlsafe_b64decode(pad(s))


def daemon_argv(root_phrase: str = ROOT_PHRASE) -> list:
    """Command line of a daemon (what `taresd` runs) that trusts the test root, for subprocess tests."""
    code = (
        "import hashlib\n"
        "from cryptography.hazmat.primitives import serialization\n"
        "from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey\n"
        "import tares.licence as L\n"
        f"k = Ed25519PrivateKey.from_private_bytes(hashlib.sha256({root_phrase!r}.encode()).digest())\n"
        "raw = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)\n"
        "L._set_root_keys_for_tests({hashlib.sha256(raw).hexdigest()[:16]: raw})\n"
        "from tares.cli import run_daemon\n"
        "run_daemon()\n"
    )
    return [sys.executable, "-c", code]
