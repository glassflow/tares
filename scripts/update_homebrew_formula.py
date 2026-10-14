#!/usr/bin/env python3
"""Point Formula/tares.rb at a Tares release on PyPI.

    python scripts/update_homebrew_formula.py <version> <formula path>

Rewrites the `url` and `sha256` lines to that version's PyPI sdist. Running it twice with the
same version changes nothing. PyPI can lag a few minutes behind the upload, so the lookup
retries before it gives up.
"""
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
URL_RE = re.compile(r'^  url "[^"]*"$', re.M)
SHA_RE = re.compile(r'^  sha256 "[^"]*"$', re.M)


def rewrite(text: str, url: str, sha256: str) -> str:
    """Replace the formula's one `url` line and one `sha256` line."""
    for name, rx in (("url", URL_RE), ("sha256", SHA_RE)):
        n = len(rx.findall(text))
        if n != 1:
            raise ValueError(f"expected exactly one top-level {name} line in the formula, found {n}")
    text = URL_RE.sub(lambda _: f'  url "{url}"', text)
    return SHA_RE.sub(lambda _: f'  sha256 "{sha256}"', text)


def fetch_sdist(version: str, attempts: int = 10, wait: int = 30) -> tuple[str, str]:
    """Return the sdist url and sha256 of a version on PyPI, retrying while PyPI catches up."""
    api = f"https://pypi.org/pypi/tares/{version}/json"
    last = "no attempt made"
    for i in range(attempts):
        try:
            req = urllib.request.Request(api, headers={"Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
            for f in data.get("urls", []):
                if f.get("packagetype") == "sdist":
                    return f["url"], f["digests"]["sha256"]
            last = f"tares {version} has no sdist on PyPI yet"
        except urllib.error.HTTPError as e:
            last = f"PyPI answered {e.code} for tares {version}"
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = f"could not reach PyPI: {e}"
        if i < attempts - 1:
            print(f"{last}; retrying in {wait}s ({i + 1}/{attempts})", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"{last}, after {attempts} attempts")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Point Formula/tares.rb at a Tares release on PyPI.")
    ap.add_argument("version", help="release version, e.g. 1.46.0 (no pre-releases)")
    ap.add_argument("formula", help="path to Formula/tares.rb")
    args = ap.parse_args(argv)
    try:
        if not VERSION_RE.match(args.version):
            raise ValueError(f"{args.version!r} is not a release version like 1.2.3; pre-releases never go to brew")
        with open(args.formula) as f:
            text = f.read()
        url, sha256 = fetch_sdist(args.version)
        new = rewrite(text, url, sha256)
        if new == text:
            print("unchanged")
            return 0
        with open(args.formula, "w") as f:
            f.write(new)
        print("changed")
        return 0
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
