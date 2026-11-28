"""Model providers as a cell configures them (tares/providers.py, TR-301): the list, the default,
the Anthropic entry doubling as the old key + gateway settings, env seeding, redaction.

Run: .venv/bin/python tests/test_model_providers.py   (in-process daemon, no external services)
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = os.path.join(tempfile.mkdtemp(), "model_providers.duckdb")   # its own folder: settings live beside the file
for pth in (DB, DB + ".wal"):
    if os.path.exists(pth):
        os.remove(pth)
os.environ["TARES_DB"] = DB
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "TARES_ANTHROPIC_BASE",
            "OPENAI_API_KEY", "OPENAI_BASE_URL", "TARES_MODEL_PROVIDER", "TARES_CATALOG",
            "TARES_AUTH_TOKEN"):
    os.environ.pop(var, None)

import httpx

from tares import providers as pv
from tares.models import AnthropicProvider, OpenAIProvider

PASS = FAIL = 0


def ck(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


def by_id(d, pid):
    return next((p for p in d["providers"] if p["id"] == pid), None)


async def main():
    from tares.daemon import make_app
    app = make_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     timeout=20) as cx:
            store = app.state.store

            print("== nothing configured ==")
            d = (await cx.get("/api/settings/providers")).json()
            a = by_id(d, "anthropic")
            ck("the Anthropic entry is always listed, unconfigured", a and not a["configured"] and a["source"] == "", str(a))
            ck("no default", d["default"] is None, str(d["default"]))
            ck("kinds offered", [k["id"] for k in d["kinds"]] == ["anthropic", "openai", "openai_compatible"])
            st = (await cx.get("/api/capabilities")).json()
            ck("status: no agent key", st["agent_key_configured"] is False)
            ck("resolve_provider -> None", pv.resolve_provider(store) == (None, ""))

            print("== an OpenAI-compatible endpoint ==")
            r = await cx.put("/api/settings/providers/new", json={"kind": "openai_compatible", "name": "LiteLLM", "key": "k1"})
            ck("compatible endpoint needs a base URL", r.status_code == 400 and "base URL" in r.text, r.text[:200])
            r = await cx.put("/api/settings/providers/new", json={"kind": "openai_compatible", "name": "LiteLLM", "key": "k1", "base_url": "ftp://x"})
            ck("base URL must be http(s)", r.status_code == 400, r.text[:200])
            r = await cx.put("/api/settings/providers/new", json={"kind": "openai_compatible", "name": "openai", "key": "k1", "base_url": "http://127.0.0.1:1/v1"})
            ck("reserved names are refused", r.status_code == 400 and "reserved" in r.text, r.text[:200])
            r = await cx.put("/api/settings/providers/new", json={"kind": "openai_compatible", "name": "LiteLLM", "key": "k1", "base_url": "http://127.0.0.1:1/v1/"})
            d = r.json()
            ck("created; id is the slug of the name", r.status_code == 200 and d["id"] == "litellm", r.text[:200])
            e = by_id(d, "litellm")
            ck("listed: configured from the console, URL without trailing slash, no key in the listing",
               e and e["configured"] and e["source"] == "console" and e["base_url"] == "http://127.0.0.1:1/v1"
               and "key" not in e and "k1" not in r.text, str(e))
            ck("the first configured provider becomes the default", d["default"] == "litellm" and e["default"], str(d["default"]))
            p, origin = pv.resolve_provider(store)
            ck("resolve_provider builds an OpenAI-format provider with the bearer key",
               isinstance(p, OpenAIProvider) and p.headers == {"Authorization": "Bearer k1"}
               and p.base_url == "http://127.0.0.1:1/v1" and p.label == "litellm" and origin == "console", str(p))
            st = (await cx.get("/api/capabilities")).json()
            ck("status: agent key configured now", st["agent_key_configured"] is True)
            r = await cx.put("/api/settings/providers/litellm", json={"kind": "openai_compatible", "base_url": "http://127.0.0.1:2/v1"})
            p, _ = pv.resolve_provider(store, "litellm")
            ck("update with a blank key keeps the stored key and changes the URL",
               r.status_code == 200 and p.headers == {"Authorization": "Bearer k1"} and p.base_url == "http://127.0.0.1:2/v1", str(p.__dict__))

            print("== the Anthropic entry is the old key + gateway settings ==")
            r = await cx.put("/api/settings/providers/anthropic", json={"kind": "anthropic", "key": "sk-ant-1"})
            d = r.json()
            a = by_id(d, "anthropic")
            ck("anthropic configured from the console", r.status_code == 200 and a["configured"] and a["source"] == "console", str(a))
            ck("the default does not move on its own", d["default"] == "litellm", str(d["default"]))
            k = (await cx.get("/api/settings/anthropic-key")).json()
            ck("the old anthropic-key endpoint sees the same key", k["configured"] and k["stored"] and k["source"] == "console", str(k))
            p, _ = pv.resolve_provider(store, "anthropic")
            ck("resolves to an Anthropic provider with the key header",
               isinstance(p, AnthropicProvider) and p.headers.get("x-api-key") == "sk-ant-1" and p.base_url == pv.DEFAULT_API_BASE, str(p.__dict__))
            r = await cx.put("/api/settings/providers/anthropic", json={"kind": "anthropic", "base_url": "http://127.0.0.1:3"})
            g = (await cx.get("/api/settings/gateway")).json()
            ck("a base URL on the Anthropic entry is the gateway", r.status_code == 200 and g["stored"] and g["url"] == "http://127.0.0.1:3", str(g))
            ck("listed with the gateway URL", by_id(r.json(), "anthropic")["base_url"] == "http://127.0.0.1:3")
            r = await cx.put("/api/settings/providers/anthropic", json={"kind": "anthropic", "base_url": ""})
            g = (await cx.get("/api/settings/gateway")).json()
            ck("blank base URL on an update clears the gateway", not g["stored"], str(g))
            r = await cx.put("/api/settings/providers/anthropic", json={"kind": "openai", "key": "x"})
            ck("the Anthropic id cannot change kind", r.status_code == 400, r.text[:200])

            print("== the default ==")
            r = await cx.put("/api/settings/providers/default", json={"id": "anthropic"})
            ck("make default", r.status_code == 200 and r.json()["default"] == "anthropic", r.text[:200])
            r = await cx.put("/api/settings/providers/default", json={"id": "nope"})
            ck("unknown -> 404", r.status_code == 404, r.text[:200])
            r = await cx.put("/api/settings/providers/new", json={"kind": "openai_compatible", "name": "Ollama", "base_url": "http://127.0.0.1:1/v1"})
            o = by_id(r.json(), "ollama")
            ck("a keyless endpoint (Ollama) counts as configured", r.status_code == 200 and o["configured"] and o["source"] == "", str(o))
            p, _ = pv.resolve_provider(store, "ollama")
            ck("...and builds without an auth header", isinstance(p, OpenAIProvider) and p.headers == {}, str(p.headers))
            r = await cx.put("/api/settings/providers/new", json={"kind": "openai", "key": ""})
            r2 = await cx.put("/api/settings/providers/default", json={"id": "openai"})
            ck("an unconfigured entry cannot be the default", r2.status_code == 400, r2.text[:200])
            ck("resolve_provider on an unconfigured entry -> None", pv.resolve_provider(store, "openai")[0] is None)

            print("== removal ==")
            r = await cx.delete("/api/settings/providers/litellm")
            ck("removed", r.status_code == 200 and by_id(r.json(), "litellm") is None, r.text[:200])
            ck("the default is untouched", r.json()["default"] == "anthropic")
            r = await cx.delete("/api/settings/providers/nope")
            ck("unknown -> 404", r.status_code == 404)
            r = await cx.delete("/api/settings/providers/anthropic")
            d = r.json()
            ck("removing Anthropic clears the stored key, the entry stays listed unconfigured",
               r.status_code == 200 and by_id(d, "anthropic") and not by_id(d, "anthropic")["configured"], str(by_id(d, "anthropic")))
            ck("the default falls to the next configured entry", d["default"] == "ollama", str(d["default"]))
            k = (await cx.get("/api/settings/anthropic-key")).json()
            ck("the old endpoint agrees", not k["configured"] and not k["stored"], str(k))

            print("== environment seeding ==")
            os.environ["OPENAI_API_KEY"] = "sk-env"
            os.environ["OPENAI_BASE_URL"] = "http://127.0.0.1:4/v1/"
            os.environ["ANTHROPIC_API_KEY"] = "sk-ant-env"
            await cx.delete("/api/settings/providers/openai")
            d = (await cx.get("/api/settings/providers")).json()
            o = by_id(d, "openai"); a = by_id(d, "anthropic")
            ck("OpenAI seeded from the env, not stored",
               o and o["configured"] and o["source"] == "env:OPENAI_API_KEY" and not o["stored"] and o["base_url"] == "http://127.0.0.1:4/v1", str(o))
            ck("Anthropic from the env", a["configured"] and a["source"] == "env:ANTHROPIC_API_KEY", str(a))
            p, origin = pv.resolve_provider(store, "openai")
            ck("the env key is what the provider sends", p.headers == {"Authorization": "Bearer sk-env"} and origin == "env:OPENAI_API_KEY")
            os.environ["TARES_MODEL_PROVIDER"] = "openai"
            store.set_setting(pv.DEFAULT_SETTING, None)
            ck("TARES_MODEL_PROVIDER picks the default when none is saved",
               (await cx.get("/api/settings/providers")).json()["default"] == "openai")
            os.environ.pop("TARES_MODEL_PROVIDER")
            ck("...else Anthropic when configured", (await cx.get("/api/settings/providers")).json()["default"] == "anthropic")
            r = await cx.put("/api/settings/providers/openai", json={"kind": "openai", "key": "sk-mine"})
            o = by_id(r.json(), "openai")
            p, _ = pv.resolve_provider(store, "openai")
            ck("a key saved in the console takes over from the env one",
               o["source"] == "console" and o["stored"] and p.headers == {"Authorization": "Bearer sk-mine"}, str(o))
            ck("...and the default OpenAI base URL applies when the stored entry has none",
               p.base_url == pv.OPENAI_API_BASE, p.base_url)
            r = await cx.delete("/api/settings/providers/openai")
            o = by_id(r.json(), "openai")
            ck("removing the stored key falls back to the env key", o["configured"] and o["source"] == "env:OPENAI_API_KEY", str(o))

            print("== the platform's entry (a hosted trial cell, TR-310) ==")
            os.environ["TARES_PLATFORM_PROVIDER_URL"] = f"http://127.0.0.1:1/v1/"
            os.environ["TARES_PLATFORM_PROVIDER_KEY"] = "sk-virtual"
            os.environ["TARES_PLATFORM_PROVIDER_NAME"] = "Tares Cloud"
            store.set_setting(pv.DEFAULT_SETTING, None)
            d = (await cx.get("/api/settings/providers")).json()
            pl = by_id(d, "platform")
            ck("seeded from the env as an OpenAI-compatible entry, named by the platform",
               pl and pl["kind"] == "openai_compatible" and pl["name"] == "Tares Cloud" and pl["configured"]
               and pl["source"] == "env:TARES_PLATFORM_PROVIDER_URL" and not pl["stored"] and pl["base_url"] == "http://127.0.0.1:1/v1", str(pl))
            ck("it is the default ahead of Anthropic when nothing else was chosen", d["default"] == "platform", str(d["default"]))
            p, origin = pv.resolve_provider(store)
            ck("resolves with the virtual key as a bearer", isinstance(p, OpenAIProvider) and p.headers == {"Authorization": "Bearer sk-virtual"} and p.label == "platform", str(p.__dict__))
            r = await cx.put("/api/settings/providers/default", json={"id": "anthropic"})
            ck("a console choice still wins over it", r.json()["default"] == "anthropic")
            store.set_setting(pv.DEFAULT_SETTING, None)
            os.environ["TARES_MODEL_PROVIDER"] = "anthropic"
            ck("...and so does TARES_MODEL_PROVIDER", (await cx.get("/api/settings/providers")).json()["default"] == "anthropic")
            os.environ.pop("TARES_MODEL_PROVIDER")
            r = await cx.put("/api/settings/providers/platform", json={"kind": "openai_compatible", "key": "x"})
            ck("cannot be edited from the console", r.status_code == 400 and "deployment" in r.text, r.text[:200])
            r = await cx.put("/api/settings/providers/new", json={"kind": "openai_compatible", "name": "Platform", "key": "x", "base_url": "http://127.0.0.1:1/v1"})
            ck("its id cannot be taken by a new entry", r.status_code == 400, r.text[:200])
            r = await cx.delete("/api/settings/providers/platform")
            ck("cannot be removed from the console", r.status_code == 400, r.text[:200])
            r = await cx.post("/api/settings/providers/platform/models")
            pl = by_id(r.json(), "platform")
            ck("a refresh stores its model list without a credential in the store",
               r.status_code == 200 and pl["models_error"] != "" and not any(e.get("key") for e in pv._stored(store) if e["id"] == "platform"), str(pl)[:200])
            for v in ("TARES_PLATFORM_PROVIDER_URL", "TARES_PLATFORM_PROVIDER_KEY", "TARES_PLATFORM_PROVIDER_NAME"):
                os.environ.pop(v)
            ck("gone from the listing when the env is gone", by_id((await cx.get("/api/settings/providers")).json(), "platform") is None)

            print("== the capabilities say a provider resolves ==")
            st = (await cx.get("/api/capabilities")).json()
            ck("agent_key_configured reflects the default provider", st["agent_key_configured"] is True
               and pv.resolve_provider(store)[1] == "env:ANTHROPIC_API_KEY", str((st["agent_key_configured"], pv.resolve_provider(store)[1])))

            print("== provider plus model per agent (TR-302) ==")
            import json as _json
            import threading
            from http.server import BaseHTTPRequestHandler, HTTPServer
            SEEN: list = []

            class ChatStub(BaseHTTPRequestHandler):
                def do_POST(self):
                    raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                    SEEN.append({"body": _json.loads(raw), "auth": self.headers.get("Authorization")})
                    out = _json.dumps({"model": "llama-x", "choices": [{"finish_reason": "stop", "message": {
                        "role": "assistant", "content": "" if EMPTY[0] else "Finding: the deploy at 09:00 did it."}}],
                        "usage": {"prompt_tokens": 11, "completion_tokens": 7}}).encode()
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)

                def do_GET(self):
                    if MODELS_STATUS[0] >= 400:
                        out = b'{"error":"nope"}'
                    else:
                        out = _json.dumps({"object": "list", "data": [{"id": "llama-x"}, {"id": "mixtral"}, {"id": "llama-x"}]}).encode()
                    MODELS_SEEN.append(self.headers.get("Authorization"))
                    MODELS_KEYS.append(self.headers.get("x-api-key"))
                    self.send_response(MODELS_STATUS[0])
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)

                def log_message(self, *a):
                    pass
            MODELS_STATUS = [200]
            EMPTY = [False]
            MODELS_SEEN: list = []
            MODELS_KEYS: list = []
            chat = HTTPServer(("127.0.0.1", 0), ChatStub)
            threading.Thread(target=chat.serve_forever, daemon=True).start()
            r = await cx.put("/api/settings/providers/new", json={"kind": "openai_compatible", "name": "Local vLLM",
                                                                  "base_url": f"http://127.0.0.1:{chat.server_port}/v1", "key": "vk"})
            ck("a router entry to run on", r.status_code == 200 and by_id(r.json(), "local-vllm") is not None, r.text[:200])
            print("== model discovery (TR-303) ==")
            v = by_id(r.json(), "local-vllm")
            ck("saving an OpenAI-format entry reads its models: deduplicated, sorted",
               v["models"] == ["llama-x", "mixtral"] and v["models_error"] == "" and v["discovers"] and v["models_at"], str(v))
            ck("discovery sent the entry's key", MODELS_SEEN and MODELS_SEEN[-1] == "Bearer vk", str(MODELS_SEEN))
            ck("the save reports the discovery", r.json()["discovery"] == {"models": ["llama-x", "mixtral"], "error": ""}, str(r.json().get("discovery")))
            ck("the default model for the router is the first listed", pv.default_model_for(store, "local-vllm") == "llama-x")
            o = by_id(r.json(), "ollama")
            ck("an endpoint that did not answer keeps an empty list and the error",
               o["models"] == [] and o["models_error"] != "", str(o)[:200])
            MODELS_STATUS[0] = 500
            r = await cx.post("/api/settings/providers/local-vllm/models")
            v = by_id(r.json(), "local-vllm")
            ck("a failed refresh keeps the last list and records why",
               r.status_code == 200 and r.json()["error"] and v["models"] == ["llama-x", "mixtral"] and "500" in v["models_error"], str(v)[:200])
            MODELS_STATUS[0] = 200
            r = await cx.post("/api/settings/providers/local-vllm/models")
            ck("a refresh that works clears the error", r.status_code == 200 and by_id(r.json(), "local-vllm")["models_error"] == "")
            r = await cx.post("/api/settings/providers/nope/models")
            ck("unknown -> 404", r.status_code == 404)
            # Anthropic discovers too: its /v1/models, with the key header; the built-in list
            # stands in while the endpoint cannot be read
            a = by_id((await cx.get("/api/settings/providers")).json(), "anthropic")
            ck("Anthropic offers the built-in list until read", a["discovers"] and a["models"][0].startswith("claude-") and a["models_at"] == "", str(a)[:200])
            r = await cx.put("/api/settings/providers/anthropic", json={"kind": "anthropic", "key": "sk-ant-9", "base_url": f"http://127.0.0.1:{chat.server_port}"})
            a = by_id(r.json(), "anthropic")
            ck("saving the Anthropic entry reads its models from the (gateway) endpoint",
               a["models"] == ["llama-x", "mixtral"] and a["models_error"] == "", str(a)[:200])
            ck("...with the Anthropic key header, not a bearer", MODELS_SEEN[-1] is None and MODELS_KEYS[-1] == "sk-ant-9", str((MODELS_SEEN[-1], MODELS_KEYS[-1])))
            MODELS_STATUS[0] = 500
            r = await cx.post("/api/settings/providers/anthropic/models")
            a = by_id(r.json(), "anthropic")
            ck("a failed Anthropic refresh keeps the last list and records why", a["models"] == ["llama-x", "mixtral"] and "500" in a["models_error"], str(a)[:200])
            MODELS_STATUS[0] = 200
            await cx.put("/api/settings/providers/anthropic", json={"kind": "anthropic", "base_url": ""})
            r = await cx.delete("/api/settings/providers/anthropic")
            a = by_id(r.json(), "anthropic")
            ck("removing Anthropic drops its discovered list too", a["models"][0].startswith("claude-") and a["models_at"] == "", str(a)[:200])
            await cx.put("/api/settings/providers/anthropic", json={"kind": "anthropic", "key": "sk-ant-1"})
            await cx.put("/api/settings/providers/default", json={"id": "anthropic"})
            os.environ["OPENAI_BASE_URL"] = f"http://127.0.0.1:{chat.server_port}/v1"
            r = await cx.post("/api/settings/providers/openai/models")
            o = by_id(r.json(), "openai")
            ck("env-seeded OpenAI: models stored, key still from the env",
               r.status_code == 200 and o["models"] == ["llama-x", "mixtral"] and o["source"] == "env:OPENAI_API_KEY" and not o["stored"], str(o)[:200])
            ck("...and discovery used the env key", MODELS_SEEN[-1] == "Bearer sk-env", str(MODELS_SEEN[-1]))
            # OpenAI lists models an agent cannot run on; sorted, babbage-002 used to come first
            # and became the default for an agent that names no model
            listed = ["babbage-002", "dall-e-3", "davinci-002", "gpt-4o", "gpt-4o-mini-transcribe",
                      "gpt-4o-realtime-preview", "gpt-5", "o3-mini", "text-embedding-3-small",
                      "tts-1", "whisper-1"]
            kept = [m for m in listed if pv._openai_chat_model(m)]
            ck("OpenAI discovery keeps only chat models", kept == ["gpt-4o", "gpt-5", "o3-mini"], str(kept))
            real_entry = pv.entry
            try:
                pv.entry = lambda st, pid: {"id": "openai", "kind": "openai", "models": ["gpt-4o", "gpt-5", "o3-mini"]}
                ck("OpenAI's default model is the first of OPENAI_MODELS the key can use",
                   pv.default_model_for(store, "openai") == "gpt-5", pv.default_model_for(store, "openai"))
                pv.entry = lambda st, pid: {"id": "openai", "kind": "openai", "models": ["o3-mini", "o4-mini"]}
                ck("...else the first one listed", pv.default_model_for(store, "openai") == "o3-mini")
                # xAI lists a non-reasoning model first; agents default to grok-4.6, Triage to grok-4.3
                grok = ["grok-4.20-0309-non-reasoning", "grok-4.3", "grok-4.6"]
                pv.entry = lambda st, pid: {"id": "xai", "kind": "openai_compatible",
                                            "base_url": "https://api.x.ai/v1", "models": grok}
                ck("xAI's default model is grok-4.6", pv.default_model_for(store, "xai") == "grok-4.6",
                   pv.default_model_for(store, "xai"))
                ck("xAI's small model is grok-4.3", pv.small_model_for(store, "xai") == "grok-4.3",
                   pv.small_model_for(store, "xai"))
                pv.entry = lambda st, pid: {"id": "xai", "kind": "openai_compatible",
                                            "base_url": "https://api.x.ai/v1", "models": ["grok-5"]}
                ck("...each only while the key lists it", pv.default_model_for(store, "xai") == "grok-5"
                   and pv.small_model_for(store, "xai") == "", pv.small_model_for(store, "xai"))
                pv.entry = lambda st, pid: {"id": "local", "kind": "openai_compatible",
                                            "base_url": "http://localhost:4000/v1", "models": grok}
                ck("another endpoint listing the same ids keeps the first listed and no small model",
                   pv.default_model_for(store, "local") == grok[0] and pv.small_model_for(store, "local") == "")
            finally:
                pv.entry = real_entry
            os.environ.pop("OPENAI_BASE_URL")
            # startup discovery: an entry the environment seeded gets its list without a click
            os.environ["TARES_PLATFORM_PROVIDER_URL"] = f"http://127.0.0.1:{chat.server_port}/v1"
            os.environ["TARES_PLATFORM_PROVIDER_KEY"] = "sk-virtual"
            store.set_setting(pv.SETTING, _json.dumps([e for e in pv._stored(store) if e["id"] != "platform"]) or None)
            await pv.discover_env_entries(store)
            pl = by_id((await cx.get("/api/settings/providers")).json(), "platform")
            ck("startup discovery filled the platform entry's list", pl["models"] == ["llama-x", "mixtral"] and pl["models_error"] == "", str(pl)[:200])
            ck("...through the platform key", MODELS_SEEN[-1] == "Bearer sk-virtual", str(MODELS_SEEN[-1]))
            n = len(MODELS_SEEN)
            await pv.discover_env_entries(store)
            ck("...and does not re-read an entry already read", len(MODELS_SEEN) == n)
            os.environ.pop("TARES_PLATFORM_PROVIDER_URL"); os.environ.pop("TARES_PLATFORM_PROVIDER_KEY")
            ck("an OpenRouter URL adds the app headers",
               pv._headers({"key": "k", "base_url": "https://openrouter.ai/api/v1"}).get("X-Title") == "Tares"
               and "X-Title" not in pv._headers({"key": "k", "base_url": "http://x/v1"}))
            print("== a call through a router entry (the summaries' path) ==")
            prov, origin = pv.resolve_provider(store, "local-vllm")
            ck("the router entry resolves to an OpenAI-compatible provider", isinstance(prov, OpenAIProvider), str(prov))
            ck("default_model_for a router with no listed models is empty", pv.default_model_for(store, "ollama") == "")
            reply = await prov.complete(model="llama-x", system="investigate", tools=[],
                                        messages=[{"role": "user", "content": "the timeline"}],
                                        max_tokens=200, tools_allowed=False)
            ck("the router answered", "deploy at 09:00" in (reply.text or ""), str(reply.text)[:200])
            ck("the request went to the router with the model and the bearer key",
               SEEN and SEEN[-1]["body"]["model"] == "llama-x" and SEEN[-1]["auth"] == "Bearer vk"
               and SEEN[-1]["body"]["messages"][0] == {"role": "system", "content": "investigate"}, str(SEEN[-1:])[:300])
            ck("usage read from the router's reply", reply.usage["input_tokens"] == 11 and reply.usage["output_tokens"] == 7, str(reply.usage))
            store.record_model_usage("ask", "", "", "llama-x", 1, reply.usage["input_tokens"], reply.usage["output_tokens"],
                                     0, 0, reply.usage.get("cost_usd"), key_source=origin or None, provider="local-vllm")
            um = (await cx.get("/api/usage/model")).json()
            ck("the spend meter splits by provider", "local-vllm" in um.get("by_provider", {}) and um["by_provider"]["local-vllm"]["uncosted_calls"] >= 1, str(um.get("by_provider")))
            chat.shutdown()

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
