"""Seed a scratch factory Tares for crew-onboarding.e2e.cjs (TR-1312): a brand-new factory cell, the
state the first run starts from. Invented names only.
  nothing saved: no crew choices, no GitHub, no Linear, no project, no machine report, no crew sessions;
  one access key named "login-key" that may only ingest, the kind a machine signs in with, so the suite
  can show it reports its machine but cannot change the crew's choices.
Starts a daemon with TARES_EDITION=factory and a root token, makes the key through its API, and stops it.
The e2e runs its own daemon on the same data.
Prints a JSON map of names to secrets on the last line.
usage: python seed-crew-onboarding.py <data-dir> <port> <mcp-port> <ui-dist> <tares-bin> <root-token>"""
import json, os, subprocess, sys, time, urllib.error, urllib.request

home, port, mcp, dist, tares, root = sys.argv[1:7]
env = {**os.environ, "PYTHONPATH": os.getcwd(), "TARES_HOME": home, "TARES_PORT": port, "TARES_MCP_PORT": mcp,
       "TARES_UI_DIST": dist, "TARES_EDITION": "factory", "TARES_OTLP_GRPC_PORT": "off", "TARES_AUTH_TOKEN": root}
for k in ("TARES_DB", "TARES_CATALOG", "TARESD_URL", "TARES_LOGIN_URL", "TARES_GITHUB_CONNECT_URL",
          "TARES_LINEAR_CONNECT_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
    env.pop(k, None)
base = f"http://127.0.0.1:{port}"


def call(path, body=None, method=None):
    r = urllib.request.Request(base + path, json.dumps(body).encode() if body is not None else None,
                               {"content-type": "application/json", "authorization": f"Bearer {root}"}, method=method)
    try:
        return json.load(urllib.request.urlopen(r))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{path} -> {e.code} {e.read().decode()[:300]}")


out = {}
daemon = subprocess.Popen([tares, "up", "--port", port, "--data-dir", home], env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/health", timeout=1)
            break
        except Exception:
            time.sleep(0.3)
    else:
        raise SystemExit("daemon did not start")
    made = call("/api/keys", {"name": "login-key", "scopes": ["ingest"]})
    out["login_key"] = made["secret"]
finally:
    daemon.terminate()
    daemon.wait()
print(json.dumps(out))
