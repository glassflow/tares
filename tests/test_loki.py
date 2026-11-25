"""The loki connector: query_range polling, auth headers, envelope mapping, the nanosecond
cursor, match/drop filters, and secret redaction.
Run: .venv/bin/python tests/test_loki.py   (no Loki needed; a stub answers query_range)
"""
import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import remove_db
DB = "/tmp/tares-loki-test.duckdb"
os.environ["TARES_DB"] = DB

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


REQUESTS = []      # (params, headers) per query_range call
RESPONSES = []     # queued response bodies; the last one repeats


class FakeLoki(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path != "/loki/api/v1/query_range":
            self.send_response(404); self.end_headers(); return
        REQUESTS.append(({k: v[0] for k, v in parse_qs(u.query).items()}, dict(self.headers)))
        body = json.dumps(RESPONSES.pop(0) if len(RESPONSES) > 1 else RESPONSES[0]).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def loki_response(entries):
    """entries: [(ts_ns, line, stream_labels)] -> a query_range body, one stream per label set."""
    streams = {}
    for ts, line, labels in entries:
        streams.setdefault(json.dumps(labels, sort_keys=True), []).append([str(ts), line])
    return {"status": "success", "data": {"resultType": "streams", "result": [
        {"stream": json.loads(k), "values": v} for k, v in streams.items()]}}


async def main():
    remove_db(DB)
    srv = HTTPServer(("127.0.0.1", 0), FakeLoki)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"

    from tares.config import SourceCfg
    from tares.connectors import build_connector, redact_config, source_type_for
    from tares.store import Store
    store = Store(DB)

    def connector(config):
        cfg = SourceCfg(name="demo_logs", type=source_type_for("loki"), connector="loki",
                        poll_seconds=5.0, config=config)
        return build_connector(cfg, store)

    print("== request shape and auth ==")
    import time
    # entries must fall inside the first poll's 30s lookback, or a real Loki would not have
    # returned them and the connector rightly skips them
    T0 = int(time.time() * 1_000_000_000) - 10_000_000_000
    RESPONSES[:] = [loki_response([
        (T0 + 1, '2026-08-24T10:00:00Z INFO GET /api/orders "GET /api/orders HTTP/1.1" 200',
         {"service": "api-server"}),
        (T0 + 2, 'ERROR db timeout', {"service": "api-server"}),
    ])]
    # level/http_status are DERIVED context (derive_fields) — a label declared against them
    # extracts, exactly like docker_logs; undeclared derived fields are candidates, not labels.
    c = connector({"url": base, "query": '{service="api-server"}',
                   "bearer_token": "tok", "tenant_id": "t1",
                   "username": "demo", "password": "pw",
                   "labels": [{"name": "service", "const": "api-server", "primary": True},
                              {"name": "level", "field": "level"},
                              {"name": "http_status", "field": "http_status"}]})
    out = await c.poll()
    params, headers = REQUESTS[-1]
    check("query_range called with the selector", params.get("query") == '{service="api-server"}',
          json.dumps(params))
    check("forward direction with a limit", params.get("direction") == "forward"
          and int(params.get("limit", 0)) > 0, json.dumps(params))
    check("first poll starts in the recent past", int(params["start"]) < int(params["end"]),
          json.dumps(params))
    # bearer and basic auth share the Authorization header (use one or the other in practice);
    # what matters is that configured auth reaches the wire at all, plus the tenant header.
    check("tenant header sent", headers.get("X-Scope-OrgID") == "t1", str(headers))
    check("an Authorization header was sent", bool(headers.get("Authorization")), str(headers))

    print("== envelopes ==")
    check("one envelope per line", len(out) == 2, str(len(out)))
    e1, e2 = sorted(out, key=lambda e: e.event_time)
    check("text is the line", "db timeout" in e2.text, e2.text)
    check("event_type is log", e1.event_type == "log" and e2.event_type == "log")
    check("keyed by the primary label", e1.key_value == "api-server", e1.key_value)
    check("derived level from the line", e2.labels.get("level") == "ERROR", str(e2.labels))
    check("http fields derived when present", e1.labels.get("http_status") == "200", str(e1.labels))
    check("payload keeps raw + stream", e1.payload.get("stream") == {"service": "api-server"}
          and "raw" in e1.payload, json.dumps(e1.payload)[:200])
    check("event_time from the entry timestamp (ns)",
          abs(e1.event_time.timestamp() - (T0 + 1) / 1e9) < 1, str(e1.event_time))

    print("== cursor ==")
    cur = store.get_cursor("demo_logs")
    check("cursor advanced to the last entry", cur == str(T0 + 2), str(cur))
    RESPONSES[:] = [loki_response([])]
    REQUESTS.clear()
    out = await c.poll()
    params, _ = REQUESTS[-1]
    check("second poll starts after the cursor", params["start"] == str(T0 + 3), json.dumps(params))
    check("empty answer ingests nothing and keeps the cursor",
          out == [] and store.get_cursor("demo_logs") == str(T0 + 2))

    print("== filters ==")
    store.set_cursor("demo_logs", str(T0))
    RESPONSES[:] = [loki_response([
        (T0 + 10, "ERROR boom", {"service": "api-server"}),
        (T0 + 11, "INFO fine", {"service": "api-server"}),
        (T0 + 12, 'GET /health "GET /health HTTP/1.1" 200', {"service": "api-server"}),
    ])]
    c2 = connector({"url": base, "query": '{service="api-server"}',
                    "match": "ERROR|INFO", "drop": "fine",
                    "labels": [{"name": "service", "const": "api-server", "primary": True}]})
    out = await c2.poll()
    check("match keeps, drop skips", [e.text for e in out] == ["ERROR boom"],
          str([e.text for e in out]))
    check("cursor still advances over filtered lines",
          store.get_cursor("demo_logs") == str(T0 + 12), store.get_cursor("demo_logs"))

    print("== failure is a quiet poll ==")
    c3 = connector({"url": "http://127.0.0.1:1", "query": "{a=\"b\"}",
                    "labels": [{"name": "service", "const": "x", "primary": True}]})
    check("unreachable loki returns no events", await c3.poll() == [])

    print("== secrets ==")
    red = redact_config("loki", {"url": base, "query": "{}", "bearer_token": "tok",
                                 "username": "u", "password": "pw"})
    check("bearer and password redacted, username kept",
          red["bearer_token"] != "tok" and red["password"] != "pw" and red["username"] == "u",
          json.dumps(red))

    store.con.close()
    srv.shutdown()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
