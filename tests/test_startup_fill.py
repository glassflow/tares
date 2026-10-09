"""The daemon starts when a one-time fill at startup changed the catalog (1.38.0-rc.2 crashed its
first start on cells with template projects: the fill reloaded the catalog before the event loop
ran, and a reload restarts sources). Builds the app outside any event loop, as `tares up` does.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HOME = tempfile.mkdtemp(prefix="tares-startup-fill-")
os.environ["TARES_DB"] = os.path.join(HOME, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(HOME, "none.yaml")
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

from tares.projects.engine import Engine as ProjectEngine  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    if cond:
        P += 1; print(f"  ok   {label}")
    else:
        F += 1; print(f"  FAIL {label}  {detail}")


# a polled source, so a reload has a source loop to start
from tares.store import Store  # noqa: E402
st = Store(os.environ["TARES_DB"])
st.upsert_catalog_source("metrics", "prometheus", "prometheus", "30s",
                         {"url": "http://127.0.0.1:9", "query": "up"})
st.con.close()

print("== a startup fill that changed something ==")
ProjectEngine.fill_template_trigger_descriptions = lambda self: True
try:
    from tares.daemon import make_app
    app = make_app()
    ck("the app builds outside an event loop", app is not None)
except Exception as e:  # noqa: BLE001
    ck("the app builds outside an event loop", False, repr(e))

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
