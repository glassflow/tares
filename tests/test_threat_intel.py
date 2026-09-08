"""Threat-intel connector: incremental poll against a local feed file, the fixed-size bookmark
cursor, the hash fallback for feeds with no first_seen, and end-to-end ingestion through the
daemon (same shape as tests/test_vercel.py, tests/test_postgres.py).
"""
import asyncio
import json
import os
import tempfile

TMP = tempfile.gettempdir()

os.environ["TARES_DB"] = os.path.join(TMP, "threat_intel.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
for _p in (os.environ["TARES_DB"], os.environ["TARES_DB"] + ".wal"):
    if os.path.exists(_p):
        os.remove(_p)

from tares.config import SourceCfg
from tares.connectors import full_schema
from tares.connectors.threat_intel import ThreatIntelConnector

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


class FakeStore:
    def __init__(self): self.cur = {}
    def get_cursor(self, n): return self.cur.get(n)
    def set_cursor(self, n, v): self.cur[n] = v


# "type" is the feed's own field name; the connector normalizes it to "indicator_type" by default.
FEED = [
    {"indicator": "185.220.101.7", "type": "ip", "threat_type": "credential_stuffing_proxy",
     "confidence": 92, "source": "sample-feed", "first_seen": "2026-08-11"},
    {"indicator": "45.155.205.12", "type": "ip", "threat_type": "known_scanner",
     "confidence": 71, "source": "sample-feed", "first_seen": "2026-07-30"},
]

LABELS = [{"name": "indicator", "field": "indicator", "primary": True},
          {"name": "indicator_type", "field": "indicator_type"},
          {"name": "threat_type", "field": "threat_type"}]


def cfg(config):
    return SourceCfg(name="ti", type="event_stream", connector="threat_intel",
                     poll_seconds=300, config=config)


def write_feed(path, items):
    with open(path, "w") as f:
        json.dump(items, f)


async def test_poll_and_cursor():
    feed_path = os.path.join(TMP, "ioc_feed.json")
    write_feed(feed_path, FEED)
    store = FakeStore()
    conn = ThreatIntelConnector(cfg({"feed_path": feed_path, "labels": LABELS}), store)

    envs = await conn.poll()
    ck("first poll emits one envelope per indicator", len(envs) == 2, len(envs))
    ck("keyed by indicator (primary label)", envs[0].key_value == "185.220.101.7", envs[0].key_value)
    ck("labels include indicator_type", envs[0].labels.get("indicator_type") == "ip", str(envs[0].labels))
    ck("labels include threat_type", envs[0].labels.get("threat_type") == "credential_stuffing_proxy",
       str(envs[0].labels))
    ck("event_type reflects indicator_type, not a constant", envs[0].event_type == "ip", envs[0].event_type)
    ck("payload keeps original entry losslessly", envs[0].payload == FEED[0], str(envs[0].payload))
    ck("text mentions confidence", "confidence 92" in envs[0].text, envs[0].text)

    # cursor after the first poll is a small fixed-size bookmark, not a growing seen-set
    cursor = json.loads(store.cur["ti"])
    ck("cursor is timestamp-bookmark mode", cursor["mode"] == "timestamp", cursor)
    ck("cursor tracks the newest first_seen only", cursor["max_first_seen"] == "2026-08-11", cursor)
    ck("bookmark's tie-break set holds only entries at that timestamp, not the whole feed",
       cursor["at_max"] == ["185.220.101.7"], cursor)

    # second poll, same feed: nothing new
    envs2 = await conn.poll()
    ck("second poll against unchanged feed emits nothing", envs2 == [], len(envs2))

    # third poll: one indicator newer than the current bookmark appended
    write_feed(feed_path, FEED + [
        {"indicator": "89.248.165.74", "type": "ip", "threat_type": "botnet_c2",
         "confidence": 88, "source": "sample-feed", "first_seen": "2026-08-12"},
    ])
    envs3 = await conn.poll()
    ck("third poll emits only the newly-arrived indicator", len(envs3) == 1, len(envs3))
    ck("new indicator is the one appended", envs3[0].key_value == "89.248.165.74", envs3[0].key_value)

    cursor3 = json.loads(store.cur["ti"])
    ck("bookmark advances to the new newest first_seen", cursor3["max_first_seen"] == "2026-08-12", cursor3)
    ck("cursor size stays bounded (still one entry, not a growing history)",
       len(cursor3["at_max"]) == 1, cursor3)


async def test_hash_fallback_when_no_first_seen():
    feed_path = os.path.join(TMP, "ioc_feed_no_ts.json")
    items = [{"indicator": "9.9.9.9", "type": "ip", "threat_type": "known_scanner", "confidence": 40}]
    write_feed(feed_path, items)
    store = FakeStore()
    conn = ThreatIntelConnector(cfg({"feed_path": feed_path, "labels": LABELS}), store)

    envs = await conn.poll()
    ck("first poll (no first_seen in feed) still emits", len(envs) == 1, len(envs))
    cursor = json.loads(store.cur["ti"])
    ck("falls back to hash mode without first_seen", cursor["mode"] == "hash", cursor)

    envs2 = await conn.poll()
    ck("unchanged feed hash: second poll emits nothing", envs2 == [], len(envs2))

    write_feed(feed_path, items + [{"indicator": "8.8.8.8", "type": "ip", "threat_type": "botnet_c2"}])
    envs3 = await conn.poll()
    ck("changed feed hash: re-emits (no timestamp to filter precisely)", len(envs3) == 2, len(envs3))


async def test_field_map():
    feed_path = os.path.join(TMP, "ioc_feed_mapped.json")
    write_feed(feed_path, [{"ioc": "1.2.3.4", "ioc_type": "ip", "threat_type": "botnet_c2",
                            "confidence": 50}])
    store = FakeStore()
    conn = ThreatIntelConnector(
        cfg({"feed_path": feed_path,
             "field_map": {"indicator": "ioc", "indicator_type": "ioc_type"},
             "labels": LABELS}),
        store,
    )
    envs = await conn.poll()
    ck("field_map remaps non-standard field names", len(envs) == 1 and envs[0].key_value == "1.2.3.4",
       [e.key_value for e in envs])
    ck("field_map applies to event_type too", envs[0].event_type == "ip", envs[0].event_type)


async def test_missing_source():
    store = FakeStore()
    conn = ThreatIntelConnector(cfg({}), store)
    try:
        await conn.poll()
        ck("raises without feed_url or feed_path", False, "no error raised")
    except ValueError as e:
        ck("raises without feed_url or feed_path", True, str(e))


def test_schema_registered():
    ck("threat_intel is schema-backed", full_schema("threat_intel") is not None)
    schema = full_schema("threat_intel")
    ck("token is marked secret", schema["token"]["secret"] is True)


async def test_ingest_end_to_end():
    """Register a threat_intel source through the daemon's own API, poll it, and confirm the
    envelope reaches the store and is readable on the entity's timeline. Not wrapped in a
    try/except: a broken daemon path must fail the test run, not print SKIP."""
    from tares.daemon import make_app
    from tares.connectors import build_connector
    import httpx

    feed_path = os.path.join(TMP, "ioc_feed_e2e.json")
    write_feed(feed_path, FEED)

    app = make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
        r = await cx.post("/api/sources", json={
            "name": "ti-e2e", "connector": "threat_intel",
            "config": {"feed_path": feed_path, "labels": LABELS},
        })
        ck("source created via daemon API", r.status_code in (200, 201), r.text)

        # ingestion runs on the background poll loop (interval-driven, not a manual-trigger
        # endpoint); poll the connector directly against the daemon's own store, exactly as
        # runtime._loop does, so this test doesn't depend on wall-clock timing.
        store = app.state.store
        cfg_obj = app.state.runtime.catalog.sources["ti-e2e"]
        conn = build_connector(cfg_obj, store)
        envelopes = await conn.poll()
        store.append(envelopes)
        ck("connector produced envelopes for the daemon's store", len(envelopes) == len(FEED),
           len(envelopes))

        r = await cx.post("/read", json={"selector": {"indicator": "185.220.101.7"}, "window": "15m"})
        ck("entity readable after poll", r.status_code == 200, r.text)
        body = r.json()
        rows = body.get("rows", [])
        ck("read includes the threat_intel row", any(
            row.get("source") == "ti-e2e" and "credential_stuffing_proxy" in row.get("text", "")
            for row in rows
        ), str(rows)[:300])


async def main():
    await test_poll_and_cursor()
    await test_hash_fallback_when_no_first_seen()
    await test_field_map()
    await test_missing_source()
    test_schema_registered()
    await test_ingest_end_to_end()
    print(f"\n{P} passed, {F} failed")
    if F:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
