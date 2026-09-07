"""The http_poll connector (TR-276, TR-277, TR-278): request shape and auth, items_path, the
mapping into envelopes, id_field dedupe with a bounded cursor, rate limiting and backoff,
discover, and the poll floor plus secret redaction through the daemon.
Run: .venv/bin/python tests/test_http_poll.py   (a stub HTTP server answers every request)
"""
import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = "/tmp/tares-http-poll-test.duckdb"
os.environ["TARES_DB"] = DB
os.environ["TARES_CATALOG"] = "/tmp/none-http-poll.yaml"

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


REQUESTS = []    # (method, path, headers, body) per request
RESPONSES = []   # queued (status, headers, body); the last one repeats


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _serve(self):
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n) if n else b""
        REQUESTS.append((self.command, self.path, dict(self.headers), raw.decode() if raw else ""))
        status, headers, body = RESPONSES.pop(0) if len(RESPONSES) > 1 else RESPONSES[0]
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = _serve
    do_POST = _serve


WEATHER = {"latitude": 52.52, "longitude": 13.41, "timezone": "GMT",
           "current_weather": {"time": "2026-09-07T13:00", "windspeed": 71.3, "winddirection": 250,
                               "weathercode": 3, "temperature": 18.2}}
FEED = {"meta": {"page": 1}, "data": {"items": [
    {"id": "a1", "service": "checkout", "status": "degraded", "updated_at": "2026-09-07T13:00:00Z"},
    {"id": "a2", "service": "search", "status": "operational", "updated_at": "2026-09-07T13:01:00Z"},
]}}


async def main():
    for p in (DB, DB + ".wal"):
        if os.path.exists(p):
            os.remove(p)
    srv = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"

    from tares.config import SourceCfg, CatalogError, validate_source_dict
    from tares.connectors import build_connector, redact_config, source_type_for
    from tares.connectors import http_poll as hp
    from tares.store import Store
    store = Store(DB)

    def connector(config, name="wx", poll=300.0):
        cfg = SourceCfg(name=name, type=source_type_for("http_poll"), connector="http_poll",
                        poll_seconds=poll, config=config)
        return build_connector(cfg, store)

    print("== request shape and auth ==")
    RESPONSES[:] = [(200, {}, WEATHER)]
    c = connector({"url": f"{base}/v1/forecast?latitude=52.52&current_weather=true",
                   "headers": {"Accept": "application/json", "X-Client": "tares"},
                   "auth_header": "X-Api-Key", "auth_value": "k-123",
                   "text_template": "wind {current_weather.windspeed} km/h from {current_weather.winddirection}",
                   "event_time_field": "current_weather.time", "event_type": "weather",
                   "labels": [{"name": "city", "const": "berlin", "primary": True},
                              {"name": "windspeed", "field": "current_weather.windspeed", "type": "number"}]})
    out = await c.poll()
    method, path, headers, body = REQUESTS[-1]
    check("GET with the query string as given", method == "GET" and path.endswith("&current_weather=true"), path)
    check("extra headers sent", headers.get("Accept") == "application/json" and headers.get("X-Client") == "tares", str(headers))
    check("credential sent in the configured header", headers.get("X-Api-Key") == "k-123", str(headers))
    check("Authorization not sent when the header is another", "Authorization" not in headers, str(headers))

    print("== one event per poll from a single object ==")
    check("one envelope", len(out) == 1, str(len(out)))
    e = out[0]
    check("keyed by the const primary label", e.key_value == "berlin", e.key_value)
    check("nested value reachable by dotted label", e.labels.get("windspeed") == 71.3, str(e.labels))
    check("template renders dotted names", e.text == "wind 71.3 km/h from 250", e.text)
    check("event_time from a nested field", e.event_time.isoformat().startswith("2026-09-07T13:00"), e.event_time.isoformat())
    check("event_type fixed", e.event_type == "weather", e.event_type)
    check("payload is the item as received", e.payload == WEATHER, str(e.payload)[:80])
    check("label_context flattens for relabel", c.label_context(WEATHER).get("current_weather.temperature") == 18.2)

    print("== second poll emits the reading again (no id_field) ==")
    out2 = await c.poll()
    check("a reading is new on every poll", len(out2) == 1, str(len(out2)))

    print("== items_path into a nested array, id_field dedupe ==")
    RESPONSES[:] = [(200, {}, FEED)]
    f = connector({"url": f"{base}/status", "items_path": "data.items", "id_field": "id",
                   "event_type_field": "status", "event_time_field": "updated_at",
                   "text_template": "{service} is {status}",
                   "labels": [{"name": "service", "field": "service", "primary": True}]}, name="status")
    outf = await f.poll()
    check("one envelope per item", len(outf) == 2, str(len(outf)))
    check("keyed by the item's field", sorted(x.key_value for x in outf) == ["checkout", "search"], str([x.key_value for x in outf]))
    check("event_type from a field", outf[0].event_type == "degraded", outf[0].event_type)
    check("Z timestamps parse", outf[1].event_time.isoformat().startswith("2026-09-07T13:01:00+00:00"), outf[1].event_time.isoformat())
    outf2 = await f.poll()
    check("same items again: nothing new", outf2 == [], str(len(outf2)))
    RESPONSES[:] = [(200, {}, {"data": {"items": FEED["data"]["items"] + [
        {"id": "a3", "service": "payments", "status": "down", "updated_at": "2026-09-07T13:02:00Z"}]}})]
    outf3 = await f.poll()
    check("only the appended item", len(outf3) == 1 and outf3[0].key_value == "payments", str([x.key_value for x in outf3]))
    cursor = store.get_cursor("status")
    check("cursor holds the seen ids", cursor.split("\n") == ["a1", "a2", "a3"], cursor)
    hp_kept = hp.SEEN_IDS_KEPT
    hp.SEEN_IDS_KEPT = 2
    try:
        RESPONSES[:] = [(200, {}, {"data": {"items": [{"id": f"b{i}", "service": "s", "status": "ok"} for i in range(5)]}})]
        await f.poll()
        check("cursor is bounded", len(store.get_cursor("status").split("\n")) == 2, store.get_cursor("status"))
    finally:
        hp.SEEN_IDS_KEPT = hp_kept

    print("== POST with a body; a top-level array; a scalar list ==")
    RESPONSES[:] = [(200, {}, [{"n": 1}, {"n": 2}])]
    p = connector({"url": f"{base}/q", "method": "POST", "body": {"query": "all"}}, name="q")
    outp = await p.poll()
    method, path, headers, body = REQUESTS[-1]
    check("POST sends the JSON body", method == "POST" and json.loads(body) == {"query": "all"}, body)
    check("top-level array: one event per element", len(outp) == 2 and outp[0].text == '{"n": 1}', str([x.text for x in outp]))
    check("falls back to the source name as key", outp[0].key_value == "q", outp[0].key_value)
    RESPONSES[:] = [(200, {}, [3, 4])]
    outs = await p.poll()
    check("scalars wrap as value", outs[0].payload == {"value": 3}, str(outs[0].payload))

    print("== errors the user must see ==")
    RESPONSES[:] = [(200, {}, {"data": {}})]
    bad = connector({"url": f"{base}/x", "items_path": "data.items"}, name="bad")
    try:
        await bad.poll(); check("missing items_path raises", False)
    except ValueError as ex:
        check("missing items_path raises", "items_path" in str(ex), str(ex))
    RESPONSES[:] = [(404, {}, {"error": "nope"})]
    try:
        await bad.poll(); check("4xx raises with the status", False)
    except ValueError as ex:
        check("4xx raises with the status", "404" in str(ex), str(ex))
    RESPONSES[:] = [(200, {}, "not json")]
    try:
        await bad.poll(); check("non-JSON raises", False)
    except ValueError as ex:
        check("non-JSON raises", "JSON" in str(ex), str(ex))
    try:
        await connector({"url": "ftp://x"}, name="ftp").poll(); check("scheme checked", False)
    except ValueError as ex:
        check("scheme checked", "http" in str(ex), str(ex))

    print("== rate limiting ==")
    RESPONSES[:] = [(429, {"Retry-After": "2"}, {"error": "slow down"}), (200, {}, WEATHER)]
    r = connector({"url": f"{base}/wx"}, name="rl", poll=60.0)
    before = len(REQUESTS)
    try:
        await r.poll(); check("429 raises so health shows it", False)
    except hp.RateLimited as ex:
        check("429 raises so health shows it", "rate limited" in str(ex) and "next try" in str(ex), str(ex))
    outr = await r.poll()
    check("no request while backing off", outr == [] and len(REQUESTS) == before + 1, str(len(REQUESTS) - before))
    check("Retry-After sets the wait", 1.5 <= r._not_before - __import__("time").monotonic() <= 2.0)
    r._not_before = 0.0
    outr = await r.poll()
    check("after the wait a poll succeeds and clears the backoff", len(outr) == 1 and r._backoff == 0.0)
    RESPONSES[:] = [(500, {}, {"error": "boom"})]
    try:
        await r.poll(); check("5xx backs off too", False)
    except hp.RateLimited as ex:
        check("5xx backs off too", "answered 500" in str(ex), str(ex))
    first_wait = r._backoff
    r._not_before = 0.0
    try:
        await r.poll()
    except hp.RateLimited:
        pass
    check("backoff starts at the poll interval and doubles", first_wait == 60.0 and r._backoff == 120.0, f"{first_wait} {r._backoff}")

    print("== poll floor ==")
    try:
        validate_source_dict({"name": "fast", "connector": "http_poll", "poll": "2s", "config": {"url": base}})
        check("2s poll refused", False)
    except CatalogError as ex:
        check("2s poll refused", "at least 10s" in str(ex), str(ex))
    validate_source_dict({"name": "ok", "connector": "http_poll", "poll": "10s", "config": {"url": base}})
    check("10s poll accepted", True)

    print("== secrets ==")
    red = redact_config("http_poll", {"url": base, "auth_header": "Authorization", "auth_value": "Bearer t",
                                      "headers": {"Accept": "application/json"}})
    check("auth_value redacted, headers kept", red["auth_value"] != "Bearer t" and red["headers"] == {"Accept": "application/json"}, str(red))

    print("== discover ==")
    RESPONSES[:] = [(200, {}, WEATHER)]
    d = await hp.HttpPollConnector.discover({"url": f"{base}/forecast"})
    check("single object: one item per poll", d["summary"].startswith("1 item per poll"), d["summary"])
    check("nested fields listed with dotted names", "current_weather.windspeed" in d["sample_fields"], str(d["sample_fields"]))
    check("time field proposed", d["proposed_config"].get("event_time_field") == "current_weather.time", str(d["proposed_config"]))
    check("a single reading dedupes on its own time", d["proposed_config"].get("id_field") == "current_weather.time", str(d["proposed_config"]))
    check("number fields proposed as number labels", any(
        l.get("field") == "current_weather.windspeed" and l.get("type") == "number" for l in d["proposed_config"]["labels"]), str(d["proposed_config"]["labels"]))
    RESPONSES[:] = [(200, {}, FEED)]
    d2 = await hp.HttpPollConnector.discover({"url": f"{base}/status"})
    check("nested array found", d2["proposed_config"].get("items_path") == "data.items", str(d2["proposed_config"]))
    check("id field proposed for a list", d2["proposed_config"].get("id_field") == "id", str(d2["proposed_config"]))
    check("string field with few values becomes the primary label", any(
        l.get("primary") and l.get("field") in ("service", "status") for l in d2["proposed_config"]["labels"]), str(d2["proposed_config"]["labels"]))
    check("summary counts items", d2["summary"].startswith("2 items per poll"), d2["summary"])
    RESPONSES[:] = [(200, {}, [{"n": 1}])]
    d3 = await hp.HttpPollConnector.discover({"url": f"{base}/arr"})
    check("top-level array: no items_path", "items_path" not in d3["proposed_config"], str(d3["proposed_config"]))
    try:
        await hp.HttpPollConnector.discover({"url": ""}); check("discover needs a url", False)
    except ValueError as ex:
        check("discover needs a url", "url" in str(ex), str(ex))

    print("== through the daemon ==")
    import httpx
    from tares.daemon import make_app
    RESPONSES[:] = [(200, {}, WEATHER)]
    app = make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
        r1 = await cx.post("/api/sources", json={"name": "wx_api", "connector": "http_poll", "poll": "1s",
                                                 "config": {"url": f"{base}/forecast"}})
        check("daemon refuses a poll under the floor", r1.status_code == 400 and "at least 10s" in r1.text, r1.text[:120])
        r2 = await cx.post("/api/sources", json={"name": "wx_api", "connector": "http_poll", "poll": "5m",
                                                 "config": {"url": f"{base}/forecast", "auth_value": "Bearer t",
                                                            "headers": {"Accept": "application/json"}}})
        check("source created", r2.status_code in (200, 201), r2.text[:120])
        listed = (await cx.get("/api/sources")).json()
        src = next(s for s in listed if s["name"] == "wx_api")
        check("secret never on the wire", "Bearer t" not in json.dumps(src), json.dumps(src)[:120])
        check("headers object round-trips", src["config"].get("headers") == {"Accept": "application/json"}, str(src["config"]))
        r3 = await cx.post("/api/sources/discover", json={"connector": "http_poll", "config": {"url": f"{base}/forecast"}})
        check("discover reachable through the API", r3.status_code == 200 and r3.json()["connector"] == "http_poll", r3.text[:120])
        await cx.aclose()

    print(f"\n{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)


asyncio.run(main())
