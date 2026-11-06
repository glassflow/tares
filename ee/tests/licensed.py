# Tares enterprise licence, see ee/LICENSE
"""Helpers for the tests under ee/tests: a licence the test root signed, and the command line of a
daemon that trusts that root. Not a test itself.

    import licensed
    licensed.install_test_root()          # this process trusts the test root
    text = licensed.test_licence()        # a licence for 5 seats, enterprise, a year out
    licensed.daemon_argv()                # a daemon process that trusts the test root

A licensed scratch daemon, started the way `tares up` starts one, trusting the test root:

    python ee/tests/licensed.py serve --port 8799 --data-dir /tmp/x [--edition factory] [--auth] \\
        [--licence valid|expired|over_seats|none] [--seats N]

`serve` takes every `tares up` option; --licence and --seats are its own (valid is the default,
5 seats). The licence goes in TARES_LICENSE, set after the scrub below.

Importing this module puts the repo root, ee/ and the repo's tests/ folder on sys.path and drops the
TARES_* and FACTORY_* settings of the machine the test runs on, as the tests under tests/ do.
The test root is trusted only in this process (tares.licence._set_root_keys_for_tests); there is
no environment switch for it.
"""
import os
import sys
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
TESTS = os.path.join(REPO, "tests")
EE = os.path.join(REPO, "ee")   # the checkout's tares_ee, for a run that has not installed the package
for _p in (EE, TESTS, REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _env import scrub_os_environ  # noqa: E402

scrub_os_environ()

import licence_testkit as kit  # noqa: E402
from licence_testkit import daemon_argv  # noqa: E402,F401  (re-exported for tests that start a daemon)


def install_test_root() -> None:
    """Make tares.licence trust the test root in this process."""
    import tares.licence as L
    _, root_pub = kit.keypair(kit.ROOT_PHRASE)
    L._set_root_keys_for_tests({kit.kid_of(root_pub): root_pub})


def test_licence(seats: int = 5, expires: str | None = None, features=("enterprise",)) -> str:
    """Licence text signed by the test issuer. `expires` is an ISO date, a year from today by
    default. It checks out only while the test root is installed."""
    today = date.today()
    expires = expires or (today + timedelta(days=365)).isoformat()
    root_priv, _ = kit.keypair(kit.ROOT_PHRASE)
    iss_priv, iss_pub = kit.keypair(kit.ISSUER_PHRASE)
    cert = kit.issuer_cert(root_priv, iss_pub, (today + timedelta(days=5 * 365)).isoformat())
    issued = (today - timedelta(days=1)).isoformat() + "T00:00:00Z"
    return kit.sign(iss_priv, cert, "Test Org", seats, features, issued, expires, "lic_ee_tests")


def _take(args: list[str], name: str):
    """Remove `--name v` or `--name=v` from args; return (rest, value or None)."""
    rest, val, i = [], None, 0
    while i < len(args):
        a = args[i]
        if a == name and i + 1 < len(args):
            val = args[i + 1]
            i += 2
        elif a.startswith(name + "="):
            val = a.split("=", 1)[1]
            i += 1
        else:
            rest.append(a)
            i += 1
    return rest, val


def serve(args: list[str]) -> None:
    """Run `tares up` in this process with the test root installed and a test licence in
    TARES_LICENSE. Only --licence and --seats are taken here; the rest goes to `tares up`."""
    rest, kind = _take(args, "--licence")
    rest, seats = _take(rest, "--seats")
    kind = kind or "valid"
    if kind not in ("valid", "expired", "over_seats", "none"):
        raise SystemExit(f"licensed.py: --licence must be valid, expired, over_seats or none, not {kind!r}")
    try:
        n = int(seats) if seats is not None else (1 if kind == "over_seats" else 5)
    except ValueError:
        raise SystemExit(f"licensed.py: --seats must be a number, not {seats!r}")
    install_test_root()
    if kind == "expired":
        os.environ["TARES_LICENSE"] = test_licence(seats=n, expires=(date.today() - timedelta(days=1)).isoformat())
    elif kind != "none":
        os.environ["TARES_LICENSE"] = test_licence(seats=n)
    import tares.cli
    sys.argv = ["tares", "up", *rest]
    tares.cli.main()


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "serve":
        serve(sys.argv[2:])
    else:
        raise SystemExit("usage: python ee/tests/licensed.py serve [tares up options] "
                         "[--licence valid|expired|over_seats|none] [--seats N]")
