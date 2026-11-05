"""The licence check (TR-908): tares/licence.py reads a licence key offline through a root key
and an issuing key, and says what the cell may do.

Covered: the fixed vector verifies with its root installed and is Invalid without it; a changed
payload, a changed signature (licence and root), an issuing key that does not match its name or
ran out before the licence was issued; garbled text; a newer format version; the 10 minute clock
allowance; the expiry boundary; unknown features; the seat count; nothing of the key in a repr;
the test root is not trusted by the shipped module.

Run: python tests/test_licence.py
"""
import base64, json, os, subprocess, sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import scrub_os_environ
scrub_os_environ()
import licence_testkit as kit
import tares.licence as L

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


HERE = os.path.dirname(os.path.abspath(__file__))
VEC = json.load(open(os.path.join(HERE, "fixtures", "licence", "vector.json")))
TEXT = VEC["licence"]
UTC = timezone.utc
NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)

root_priv, root_pub = kit.keypair(VEC["root_phrase"])
iss_priv, iss_pub = kit.keypair(VEC["issuer_phrase"])
ROOT_KID = kit.kid_of(root_pub)
CERT = kit.issuer_cert(root_priv, iss_pub, VEC["issuer_not_after"])


def install():
    L._set_root_keys_for_tests({ROOT_KID: root_pub})


def make(org="Test Org", seats=5, features=("enterprise",), issued="2026-11-05T00:00:00Z",
         expires="2027-11-05", kid="lic_x", cert=None, **over):
    p = kit.payload_of(cert or CERT, org, seats, features, issued, expires, kid)
    p.update(over)
    return kit.sign_payload(iss_priv, p)


def b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def invalid(text, now=NOW):
    return isinstance(L.verify(text, now), L.Invalid)


print("-- the vector")
vec_ok = (VEC["root_kid"] == ROOT_KID and VEC["root_public_key"] == kit.b64u(root_pub)
          and kit.sign(iss_priv, CERT, VEC["org"], VEC["seats"], VEC["features"], VEC["issued"],
                       VEC["expires"], VEC["kid"]) == TEXT)
ck("the vector file is what the testkit produces", vec_ok)
install()
lic = L.verify(TEXT, NOW)
ck("the vector verifies with its root installed", isinstance(lic, L.Licence), repr(lic))
ck("the fields come through", isinstance(lic, L.Licence) and (lic.kid, lic.org, lic.seats, lic.features) ==
   ("lic_test0001", "Test Org", 5, ("enterprise",)) and lic.expires.isoformat() == "2027-11-05"
   and lic.issued == datetime(2026, 11, 5, tzinfo=UTC) and lic.root_kid == ROOT_KID
   and lic.issuer_kid == kit.kid_of(iss_pub))
ck("surrounding whitespace is stripped", isinstance(L.verify("  \n" + TEXT + "\n ", NOW), L.Licence))
L._set_root_keys_for_tests(None)
r = L.verify(TEXT, NOW)
ck("no root installed: Invalid, unknown root", isinstance(r, L.Invalid) and "trusts" in r.reason, repr(r))
ck("the test root is not in the shipped ROOT_PUBLIC_KEYS", VEC["root_kid"] not in L.ROOT_PUBLIC_KEYS)
ck("installing a test root leaves the shipped dict alone",
   (install(), VEC["root_kid"] not in L.ROOT_PUBLIC_KEYS)[1])
out = subprocess.run([sys.executable, "-c",
                      "import json,tares.licence as l;v=json.load(open(%r));r=l.verify(v['licence']);"
                      "print(type(r).__name__)" % os.path.join(HERE, "fixtures", "licence", "vector.json")],
                     capture_output=True, text=True, cwd=os.path.dirname(HERE),
                     env={**os.environ, "PYTHONPATH": os.path.dirname(HERE)})
ck("a fresh process with no root installed: Invalid", out.stdout.strip() == "Invalid", out.stdout + out.stderr)
install()

print("-- tampering")
head, pay, sig = TEXT.split(".")
raw = b64d(pay)
ck("a changed payload byte is Invalid",
   invalid(f"TL1.{kit.b64u(raw.replace(b'Test Org', b'Test Orh'))}.{sig}"))
bad = bytearray(b64d(sig)); bad[0] ^= 1
ck("a changed licence signature byte is Invalid", invalid(f"TL1.{pay}.{kit.b64u(bytes(bad))}"))
rs = bytearray(b64d(CERT["root_signature"])); rs[0] ^= 1
ck("a changed root signature byte is Invalid (licence re-signed)",
   invalid(make(cert={**CERT, "root_signature": kit.b64u(bytes(rs))})))
ck("a changed issuing key byte is Invalid (licence re-signed)",
   invalid(make(cert={**CERT, "public_key": kit.b64u(kit.keypair("other")[1])})))
ck("an issuer kid that does not match its key is Invalid", invalid(make(cert={**CERT, "kid": "0" * 16})))
ck("an issuer not_after changed after the root signed it is Invalid",
   invalid(make(cert={**CERT, "not_after": "2099-01-01"})))
early = kit.issuer_cert(root_priv, iss_pub, "2026-11-04")
ck("an issuer whose not_after is before issued is Invalid", invalid(make(cert=early)))
r = L.verify(make(cert=early), NOW)
ck("... and says why in words", isinstance(r, L.Invalid) and "issuing key" in r.reason, repr(r))
same_day = kit.issuer_cert(root_priv, iss_pub, "2026-11-05")
ck("issued late on the issuer's last day is fine",
   isinstance(L.verify(make(cert=same_day, issued="2026-11-05T23:59:59Z"), NOW), L.Licence))
ck("issued the second after the issuer's last day is Invalid",
   invalid(make(cert=same_day, issued="2026-11-06T00:00:00Z"), NOW + timedelta(days=1)))
other_root, other_pub = kit.keypair("a different root")
ck("an issuing key vouched for by an untrusted root is Invalid",
   invalid(make(cert=kit.issuer_cert(other_root, iss_pub, "2027-11-05"))))
ck("an issuer vouched for by a trusted root kid but signed by another key is Invalid",
   invalid(make(cert=kit.issuer_cert(other_root, iss_pub, "2027-11-05", root_kid=ROOT_KID))))

print("-- garbled text")
for label, t in [("wrong prefix", "TL2." + pay + "." + sig), ("two parts", "TL1." + pay),
                 ("four parts", TEXT + ".x"), ("empty", ""), ("bad base64 payload", "TL1.!!!." + sig),
                 ("padded base64", "TL1." + pay + "==." + sig),
                 ("bad base64 signature", "TL1." + pay + ".@@@"),
                 ("bad json", f"TL1.{kit.b64u(b'not json')}.{sig}"),
                 ("json not an object", f"TL1.{kit.b64u(b'[1]')}.{sig}"),
                 ("not utf-8", "TL1." + kit.b64u(bytes([255, 254])) + "." + sig)]:
    ck(f"{label} is Invalid", invalid(t))
ck("None, a number and bytes are Invalid, no raise", all(invalid(x) for x in (None, 5, b"TL1.a.b", [])))
base = kit.payload_of(CERT, "Test Org", 5, ["enterprise"], "2026-11-05T00:00:00Z", "2027-11-05", "lic_x")
for field in ("kid", "org", "seats", "features", "issued", "expires", "issuer", "v"):
    p = dict(base); del p[field]
    ck(f"missing {field} is Invalid", invalid(kit.sign_payload(iss_priv, p)))
for label, over in [("seats 0", {"seats": 0}), ("seats true", {"seats": True}), ("seats 1.5", {"seats": 1.5}),
                    ("seats as text", {"seats": "5"}), ("features as text", {"features": "enterprise"}),
                    ("features holding a number", {"features": ["enterprise", 3]}),
                    ("org a number", {"org": 5}), ("kid a number", {"kid": 5}),
                    ("issued as a date only", {"issued": "2026-11-05"}),
                    ("issued not a date", {"issued": "2026-13-45T00:00:00Z"}),
                    ("expires with a time", {"expires": "2027-11-05T00:00:00Z"}),
                    ("expires not a date", {"expires": "2027-02-30"}), ("v as text", {"v": "1"}),
                    ("v true", {"v": True}), ("issuer not an object", {"issuer": "x"})]:
    ck(f"{label} is Invalid", invalid(kit.sign_payload(iss_priv, {**base, **over})))
r = L.verify(make(v=2), NOW)
ck("v 2 is Invalid and asks for a newer Tares", isinstance(r, L.Invalid) and "newer Tares" in r.reason, repr(r))

print("-- clock allowance")
now = datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)
ck("issued 9 minutes ahead of now is valid",
   isinstance(L.verify(make(issued="2026-11-05T12:09:00Z"), now), L.Licence))
ck("issued 11 minutes ahead of now is Invalid", invalid(make(issued="2026-11-05T12:11:00Z"), now))
ck("a naive now is read as UTC", isinstance(L.verify(TEXT, datetime(2026, 11, 6)), L.Licence))

print("-- expiry")
ck("the last day at 23:59:59 UTC is not expired",
   not L.expired(lic, datetime(2027, 11, 5, 23, 59, 59, tzinfo=UTC)))
ck("the next day at 00:00:00 UTC is expired", L.expired(lic, datetime(2027, 11, 6, 0, 0, 0, tzinfo=UTC)))
st = L.state(TEXT, 1, datetime(2027, 11, 6, 0, 0, 0, tzinfo=UTC), "env")
ck("an expired licence is still valid and ee_allowed", st["valid"] and st["expired"] and st["ee_allowed"], str(st))
ck("... allows() still says yes", L.allows(st))
ck("expired licence: days_left is negative", st["days_left"] == -1, str(st["days_left"]))
st = L.state(TEXT, 1, datetime(2027, 11, 5, 23, 59, 59, tzinfo=UTC), "env")
ck("on the last day days_left is 0 and not expired", st["days_left"] == 0 and not st["expired"], str(st))
st = L.state(TEXT, 1, NOW, "settings")
ck("days_left counts whole UTC days", st["days_left"] == (datetime(2027, 11, 5).date() - NOW.date()).days
   and st["source"] == "settings" and st["expires"] == "2027-11-05", str(st))

print("-- features")
ck("an unknown feature next to enterprise is kept and enterprise is allowed",
   L.state(make(features=["sso-magic", "enterprise"]), 0, NOW)["ee_allowed"]
   and L.verify(make(features=["sso-magic", "enterprise"]), NOW).features == ("sso-magic", "enterprise"))
st = L.state(make(features=["sso-magic"]), 0, NOW)
ck("only an unknown feature: valid but ee_allowed is False", st["valid"] and not st["ee_allowed"], str(st))
ck("allows() on a feature the key does not hold is False", not L.allows(st) and L.allows(st, "sso-magic"))
ck("no features at all: valid, not allowed", not L.state(make(features=[]), 0, NOW)["ee_allowed"])

print("-- seats")
ck("used == seats is not over", L.state(TEXT, 5, NOW)["over_seats"] is False)
ck("used == seats + 1 is over", L.state(TEXT, 6, NOW)["over_seats"] is True)

print("-- state with no good key")
KEYS = {"valid", "source", "kid", "org", "seats", "used", "features", "expires", "days_left",
        "expired", "over_seats", "ee_allowed", "reason"}
for label, t in [("no text", None), ("empty text", ""), ("blank text", "  \n")]:
    st = L.state(t, 2, NOW, "env")
    ck(f"{label}: valid False, source none, no reason", set(st) == KEYS and st["valid"] is False
       and st["source"] == "none" and st["reason"] is None and st["used"] == 2 and not st["ee_allowed"], str(st))
st = L.state("TL1.junk", 2, NOW, "env")
ck("garbage: not valid, reason in words, source kept, nothing allowed",
   set(st) == KEYS and st["valid"] is False and st["source"] == "env" and bool(st["reason"])
   and not st["ee_allowed"] and not L.allows(st), str(st))
L._set_root_keys_for_tests(None)
st = L.state(TEXT, 2, NOW, "settings")
ck("a licence from an untrusted root: not valid, ee_allowed False",
   not st["valid"] and not st["ee_allowed"] and "trusts" in st["reason"], str(st))
install()
ck("allows() on an empty dict is False", L.allows({}) is False)

print("-- nothing of the key leaks")
st = L.state(TEXT, 3, NOW, "env")
parts = [p for p in TEXT.split(".")] + [pay[:24], sig[:24], CERT["root_signature"][:24]]
dump = repr(L.verify(TEXT, NOW)) + repr(st) + repr(L.Invalid("x")) + repr(L.verify("TL1.junk", NOW))
ck("repr of Licence and of state() hold no part of the text", not any(p in dump for p in parts), dump)
ck("repr of Licence names org, seats, expires only",
   repr(L.verify(TEXT, NOW)) == "Licence(org='Test Org', seats=5, expires=2027-11-05)", repr(L.verify(TEXT, NOW)))
ck("the state dict has no text in it", TEXT not in json.dumps(st))
ck("LICENCE_URL is the enterprise page", L.LICENCE_URL == "https://docs.glassflow.ai/tares/enterprise")

print("-- testkit")
argv = kit.daemon_argv()
ck("daemon_argv runs python -c with the test root and run_daemon",
   argv[0] == sys.executable and argv[1] == "-c" and "_set_root_keys_for_tests" in argv[2]
   and "run_daemon" in argv[2])
src = argv[2].rsplit("from tares.cli import run_daemon", 1)[0]
ns = {}
L._set_root_keys_for_tests(None)
exec(src, ns)
ck("... and its code installs exactly the test root", list(L._roots) == [ROOT_KID] and L._roots[ROOT_KID] == root_pub)
L._set_root_keys_for_tests(None)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
