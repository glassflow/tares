"""OTLP headers helper for a crew station's Claude Code (TR-866). Claude Code runs it as its
`otelHeadersHelper` (`tares hook otel-headers`) and sends what it prints, a JSON object of string
headers, with every telemetry export.

The monitoring key never sits in the station's environment, so this fetches it at start from the
crew's Tares (`GET /api/factory/monitoring/headers`) with the crew's token, and prints it. Claude
Code runs the helper at startup and again every 29 minutes.

It never fails and never prints anything else: no Tares, no token, a refusal, a bad answer or
monitoring switched off all give `{}`. Nothing is written to stderr either, and a header value is
never put in argv. Standard library only, and 3.9-safe: it runs on a person's machine.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request


def env(name, default=""):
    return os.environ.get(name) or default


def crew_token() -> str:
    """The token in the crew's token file with every space and newline removed, or ""."""
    path = env("TARES_FACTORY_CRED_FILE")
    if not path:
        return ""
    try:
        with open(path, "rb") as f:
            return b"".join(f.read().split()).decode("utf-8")
    except (OSError, ValueError):
        return ""


def fetch(base: str, token: str) -> dict:
    """The headers Tares hands this station, only string pairs; {} on any trouble."""
    req = urllib.request.Request(base.rstrip("/") + "/api/factory/monitoring/headers")
    req.add_header("Authorization", "Bearer " + token)
    for header, var in (("X-Tares-Station", "FACTORY_STATION"), ("X-Tares-Role", "FACTORY_ROLE"),
                        ("X-Tares-Parent", "FACTORY_PARENT")):
        if env(var):
            req.add_header(header, env(var))
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            got = json.loads(r.read().decode("utf-8")).get("headers")
    except Exception:
        return {}
    if not isinstance(got, dict):
        return {}
    return {str(k): v for k, v in got.items() if isinstance(v, str) and v}


def headers_json() -> str:
    base, token = env("TARES_FACTORY_URL"), crew_token()
    got = fetch(base, token) if base and token else {}
    return json.dumps(got, separators=(",", ":"))


def main() -> int:
    out = headers_json()
    stream = getattr(sys.stdout, "buffer", None)
    if stream is None:
        sys.stdout.write(out + "\n")
        sys.stdout.flush()
    else:
        stream.write(out.encode("utf-8") + b"\n")
        stream.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
