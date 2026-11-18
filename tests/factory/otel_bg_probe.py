"""Does a `claude --bg` station send its OpenTelemetry when its telemetry variables come by one channel or the other?

A hand check, not part of CI (it needs the `claude` CLI and starts its background service in a scratch HOME).
Everything stays on this machine: a scratch HOME, an OTLP receiver and a fake Anthropic API on 127.0.0.1, a proxy
that goes nowhere. The header is a dummy made on the spot; only whether it arrived is kept, never its value.

    python tests/factory/otel_bg_probe.py file   # the variables in the --settings file's env (what a station does)
    python tests/factory/otel_bg_probe.py env    # the variables only in the launching process's environment

A first session starts the background service. The second one, started with the variables, is the one measured:
a session handed to a service that already runs gets the settings file's env and not the launcher's, so `env`
prints no requests and exits 1, and `file` prints logs, traces and metrics and exits 0.
"""
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VIA = sys.argv[1] if len(sys.argv) > 1 else "file"
if VIA not in ("file", "env"):
    sys.exit(__doc__)
HEADER = secrets.token_hex(8)
T0 = time.time()
got = []
SIGNALS = {"/v1/logs": b"claude_code.user_prompt", "/v1/traces": b"claude_code.llm_request",
           "/v1/metrics": b"claude_code.session.count"}


class Otlp(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        got.append((self.path, self.headers.get("x-probe") == HEADER, SIGNALS.get(self.path, b"?") in body,
                    round(time.time() - T0, 1)))
        self.send_response(200)
        self.send_header("content-length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


def sse(ev, data):
    return f"event: {ev}\ndata: {json.dumps(data)}\n\n".encode()


class Api(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        msg = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [],
               "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 1}}
        if not self.path.startswith("/v1/messages"):
            out, kind = b"{}", "application/json"
        elif json.loads(body or b"{}").get("stream"):
            out, kind = b"".join([
                sse("message_start", {"type": "message_start", "message": msg}),
                sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
                sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}}),
                sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
                sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                      "usage": {"output_tokens": 2}}),
                sse("message_stop", {"type": "message_stop"})]), "text/event-stream"
        else:
            msg.update(content=[{"type": "text", "text": "hi"}], stop_reason="end_turn")
            out, kind = json.dumps(msg).encode(), "application/json"
        self.send_response(200)
        self.send_header("content-type", kind)
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST

    def log_message(self, *a):
        pass


def serve(handler):
    s = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


def main() -> int:
    if not shutil.which("claude"):
        print("the claude CLI is not on PATH")
        return 2
    otlp, api = serve(Otlp), serve(Api)
    root = Path(tempfile.mkdtemp(prefix="otel probe ")).resolve()
    home, work = root / "home", root / "work"
    home.mkdir()
    work.mkdir()
    (home / ".claude.json").write_text(json.dumps({
        "hasCompletedOnboarding": True, "numStartups": 5,
        "projects": {str(work): {"hasTrustDialogAccepted": True, "allowedTools": []}}}), encoding="utf-8")
    base_url = f"http://127.0.0.1:{api.server_port}"
    quiet = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "ANTHROPIC_BASE_URL": base_url,
             "ANTHROPIC_API_KEY": "sk-ant-fake-local-probe", "HTTPS_PROXY": "http://127.0.0.1:9",
             "HTTP_PROXY": "http://127.0.0.1:9", "NO_PROXY": "127.0.0.1,localhost"}
    otel = {"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
            "OTEL_METRICS_EXPORTER": "otlp", "OTEL_LOGS_EXPORTER": "otlp", "OTEL_TRACES_EXPORTER": "otlp",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{otlp.server_port}",
            "OTEL_EXPORTER_OTLP_HEADERS": f"x-probe={HEADER}", "OTEL_RESOURCE_ATTRIBUTES": "tares.station=probe",
            "OTEL_METRIC_EXPORT_INTERVAL": "2000", "OTEL_LOGS_EXPORT_INTERVAL": "1000",
            "OTEL_TRACES_EXPORT_INTERVAL": "1000"}
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "USERPROFILE": str(home), "FORCE_COLOR": "0", **quiet}
    jobs = []

    def start(name, settings_env, process_env):
        sfile = root / f"{name}.settings.json"
        sfile.write_text(json.dumps({"env": {**quiet, **settings_env}, "disableAllHooks": True}), encoding="utf-8")
        out = root / f"{name}.out"
        with open(out, "w") as fh:
            subprocess.run(["claude", "--settings", str(sfile), "--setting-sources", "project,local", "--no-chrome",
                            "--bg", "-n", name, "say hi"], cwd=work, env={**env, **process_env},
                           stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT, timeout=120)
        jobs.extend(w for w in out.read_text().split() if len(w) == 8 and all(c in "0123456789abcdef" for c in w))

    try:
        start("first", {}, {})
        time.sleep(8)
        before = len(got)
        start("second", otel if VIA == "file" else {}, otel if VIA == "env" else {})
        time.sleep(25)
    finally:
        for j in jobs:
            subprocess.run(["claude", "stop", j], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=60)
        subprocess.run(["claude", "daemon", "stop", "--any"], env=env, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        otlp.shutdown()
        api.shutdown()
        shutil.rmtree(root, ignore_errors=True)
    print(f"variables via {VIA}; requests before the measured session: {before}")
    for path, header, signal, at in got:
        print(f"  {path} at {at}s, header ok: {header}, signal seen: {signal}")
    seen = {p for p, header, signal, _ in got if header and signal}
    ok = seen == set(SIGNALS)
    print("PASS: logs, traces and metrics arrived" if ok else f"NO: arrived {sorted(seen) or 'nothing'}")
    return 0 if ok else 1


sys.exit(main())
