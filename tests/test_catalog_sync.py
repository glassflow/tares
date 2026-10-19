"""TARES_CATALOG_SYNC — the YAML catalog is the source of truth. Default is import-once (first
boot only); with sync set, every boot re-imports, so editing the file + restarting manages sources.
"""
import asyncio, os, signal, subprocess, sys
import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import clean_env

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

DB = "/tmp/sync.duckdb"
CAT = "/tmp/sync_catalog.yaml"
PORT = "8803"

def write_catalog(*names):
    with open(CAT, "w") as fh:
        for n in names:
            fh.write(f"sources:\n" if n == names[0] else "")
        fh.write("sources:\n")
        for n in names:
            fh.write(f"  - name: {n}\n    connector: webhook\n    poll: 5s\n    config: {{}}\n")


async def boot_and_count(sync):
    env = {**clean_env(), "TARES_DB": DB, "TARES_CATALOG": CAT, "TARES_PORT": PORT,
           "TARES_OTLP_GRPC_PORT": "off"}
    if sync:
        env["TARES_CATALOG_SYNC"] = "1"
    proc = subprocess.Popen([sys.executable, "-c", "from tares.cli import run_daemon; run_daemon()"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(80):
            try:
                async with httpx.AsyncClient() as cx:
                    r = await cx.get(f"http://127.0.0.1:{PORT}/api/sources", timeout=1)
                    if r.status_code == 200:
                        return {s["name"] for s in r.json()}
            except Exception:
                pass
            await asyncio.sleep(0.25)
        return None
    finally:
        proc.send_signal(signal.SIGTERM)
        try: proc.wait(timeout=5)
        except Exception: proc.kill()


async def main():
    for p in (DB, DB + ".wal"):
        if os.path.exists(p):
            os.remove(p)

    write_catalog("a")
    s1 = await boot_and_count(sync=False)
    ck("first boot imports the catalog", s1 == {"a"}, str(s1))

    write_catalog("a", "b")
    s2 = await boot_and_count(sync=False)
    ck("default: catalog NOT re-imported (import-once)", s2 == {"a"}, str(s2))

    s3 = await boot_and_count(sync=True)
    ck("CATALOG_SYNC: edited YAML re-imported on boot", s3 == {"a", "b"}, str(s3))

    print(f"\n{P} passed, {F} failed")

asyncio.run(main())
sys.exit(1 if F else 0)
