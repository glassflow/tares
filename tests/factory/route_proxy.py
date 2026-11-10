#!/usr/bin/env python3
"""A pass-through in front of a Tares that answers one route with an error, so a test can make a single
lookup fail in a way every engine sees (both go through TARESD_URL). Standard library only.

    route_proxy.py <port> <upstream url> <route> [status]

Every request goes on to the upstream, with its headers and body, except a request whose path (without
the query) is <route>: that one gets <status> (default 500) and an empty JSON error. Stop it with a signal.
"""
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

port, upstream, route = int(sys.argv[1]), sys.argv[2].rstrip("/"), sys.argv[3]
status = int(sys.argv[4]) if len(sys.argv) > 4 else 500
SKIP = {"host", "connection", "content-length", "transfer-encoding"}


class Proxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def _go(self):
        if urlparse(self.path).path == route:
            body = b'{"detail": "route failed on purpose"}'
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        data = self.rfile.read(int(self.headers.get("Content-Length") or 0)) or None
        req = urllib.request.Request(upstream + self.path, data=data, method=self.command,
                                     headers={k: v for k, v in self.headers.items() if k.lower() not in SKIP})
        try:
            resp = urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as e:
            resp = e
        raw = resp.read()
        self.send_response(resp.status if hasattr(resp, "status") else resp.code)
        self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _go

    def log_message(self, *a):
        pass


ThreadingHTTPServer(("127.0.0.1", port), Proxy).serve_forever()
