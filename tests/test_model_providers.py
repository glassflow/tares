"""Model providers as a cell configures them (tares/providers.py, TR-301): the list, the default,
the Anthropic entry doubling as the old key + gateway settings, env seeding, redaction.

Run: .venv/bin/python tests/test_model_providers.py   (in-process daemon, no external services)
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = "/tmp/model_providers.duckdb"
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
            store.upsert_catalog_agent("a1", "t1", "prompt")
            r = await cx.post("/api/agents/builtin/a1/enable")
            ck("enabling an agent without a provider -> 400 naming the fix",
               r.status_code == 400 and "no model provider" in r.text, r.text[:200])

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
            r = await cx.put("/api/settings/providers/new", json={"kind": "openai_compatible", "name": "Ollama", "base_url": "http://127.0.0.1:11434/v1"})
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

            print("== the builtin agents listing ==")
            d = (await cx.get("/api/agents/builtin")).json()
            ck("key_configured reflects the default provider", d["key_configured"] is True and d["key_source"] == "env:ANTHROPIC_API_KEY", str((d["key_configured"], d["key_source"])))

            print("== provider plus model per agent (TR-302) ==")
            import json as _json
            import threading
            import yaml
            from http.server import BaseHTTPRequestHandler, HTTPServer
            SEEN: list = []

            class ChatStub(BaseHTTPRequestHandler):
                def do_POST(self):
                    raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                    SEEN.append({"body": _json.loads(raw), "auth": self.headers.get("Authorization")})
                    out = _json.dumps({"model": "llama-x", "choices": [{"finish_reason": "stop", "message": {
                        "role": "assistant", "content": "Finding: the deploy at 09:00 did it."}}],
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
                    self.send_response(MODELS_STATUS[0])
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)

                def log_message(self, *a):
                    pass
            MODELS_STATUS = [200]
            MODELS_SEEN: list = []
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
            r = await cx.post("/api/settings/providers/anthropic/models")
            ck("Anthropic's list ships with Tares", r.status_code == 400)
            r = await cx.post("/api/settings/providers/nope/models")
            ck("unknown -> 404", r.status_code == 404)
            ck("Anthropic is marked as not discovering", by_id((await cx.get("/api/settings/providers")).json(), "anthropic")["discovers"] is False)
            os.environ["OPENAI_BASE_URL"] = f"http://127.0.0.1:{chat.server_port}/v1"
            r = await cx.post("/api/settings/providers/openai/models")
            o = by_id(r.json(), "openai")
            ck("env-seeded OpenAI: models stored, key still from the env",
               r.status_code == 200 and o["models"] == ["llama-x", "mixtral"] and o["source"] == "env:OPENAI_API_KEY" and not o["stored"], str(o)[:200])
            ck("...and discovery used the env key", MODELS_SEEN[-1] == "Bearer sk-env", str(MODELS_SEEN[-1]))
            os.environ.pop("OPENAI_BASE_URL")
            ck("an OpenRouter URL adds the app headers",
               pv._headers({"key": "k", "base_url": "https://openrouter.ai/api/v1"}).get("X-Title") == "Tares"
               and "X-Title" not in pv._headers({"key": "k", "base_url": "http://x/v1"}))
            store.upsert_catalog_source("evt", "webhook", "webhook", "5s", {"labels": [{"name": "service", "field": "service", "primary": True}]})
            store.upsert_catalog_view("svc", "service", ["evt"])
            store.upsert_catalog_trigger("t1", "svc", {"field": "service", "aggregate": "count", "predicate": ">= 1", "window": "5m"}, {}, "5m")
            app.state.runtime.reload_catalog()

            body = {"name": "on-vllm", "trigger": "t1", "prompt": "investigate", "provider": "local-vllm", "model": "llama-x"}
            r = await cx.post("/api/agents/builtin", json=body)
            ck("agent created with a provider and a model", r.status_code == 201, r.text[:200])
            r = await cx.post("/api/agents/builtin", json={**body, "name": "bad-prov", "provider": "Not Valid!"})
            ck("a provider id must be a slug", r.status_code == 400, r.text[:200])
            r = await cx.post("/api/agents/builtin", json={**body, "name": "bad-model", "provider": "anthropic", "model": "gpt-5"})
            ck("a non-Claude model on the Anthropic entry is refused", r.status_code == 400 and "Claude" in r.text, r.text[:200])
            r = await cx.post("/api/agents/builtin", json={**body, "name": "gone-prov", "provider": "not-here", "model": "m"})
            ck("an agent may name a provider the cell lacks (a template, an import)", r.status_code == 201, r.text[:200])
            d = (await cx.get("/api/agents/builtin")).json()
            a = next(x for x in d["agents"] if x["name"] == "on-vllm")
            g = next(x for x in d["agents"] if x["name"] == "gone-prov")
            ck("listing carries provider and effective provider", a["provider"] == "local-vllm" and a["effective_provider"] == "local-vllm", str((a["provider"], a["effective_provider"])))
            ck("a missing provider resolves to the default", g["effective_provider"] == d["default_provider"] == "anthropic", str((g["effective_provider"], d["default_provider"])))
            ck("the listing offers the providers with per-provider default models",
               any(p["id"] == "local-vllm" for p in d["providers"]) and d["default_models"]["anthropic"].startswith("claude-")
               and d["default_models"]["local-vllm"] == "llama-x" and d["default_models"]["ollama"] == "", str(d["default_models"]))
            p, origin, pid, note = pv.resolve_for_agent(store, g)
            ck("resolve_for_agent: fallback with a note", isinstance(p, AnthropicProvider) and pid == "anthropic" and "not-here" in note and "does not exist" in note, note)
            p, origin, pid, note = pv.resolve_for_agent(store, a)
            ck("resolve_for_agent: the named provider, no note", isinstance(p, OpenAIProvider) and pid == "local-vllm" and note == "", note)
            ck("default_model_for a router with no listed models is empty", pv.default_model_for(store, "ollama") == "")
            r = await cx.post("/api/agents/builtin/on-vllm/enable")
            ck("an agent on a configured provider can be enabled", r.status_code == 200, r.text[:200])

            runner = app.state.agents
            runner.attach_loop()
            rid = runner.run_now("on-vllm", "t1", "billing", "the timeline")
            run = None
            for _ in range(100):
                run = next((x for x in store.list_agent_runs("on-vllm") if x["id"] == rid), None)
                if run and run["status"] not in (None, "running"):
                    break
                await asyncio.sleep(0.1)
            ck("the run concluded on the router", run is not None and run["status"] == "ok" and "deploy at 09:00" in (run["finding"] or ""), str(run)[:300])
            ck("the run records provider and model", run is not None and run["provider"] == "local-vllm" and run["model"] == "llama-x", str((run and run["provider"], run and run["model"])))
            ck("the request went to the router with the agent's model and the bearer key",
               SEEN and SEEN[-1]["body"]["model"] == "llama-x" and SEEN[-1]["auth"] == "Bearer vk"
               and SEEN[-1]["body"]["messages"][0] == {"role": "system", "content": "investigate"}, str(SEEN[-1:])[:300])
            ck("usage recorded from the router's reply", run is not None and run["input_tokens"] == 11 and run["output_tokens"] == 7, str((run and run["input_tokens"], run and run["output_tokens"])))
            ck("an unpriced router model records tokens with no cost", run is not None and run["cost_usd"] is None, str(run and run["cost_usd"]))
            um = (await cx.get("/api/usage/model")).json()
            ck("the spend meter splits by provider", "local-vllm" in um.get("by_provider", {}) and um["by_provider"]["local-vllm"]["uncosted_calls"] >= 1, str(um.get("by_provider")))

            doc = yaml.safe_load((await cx.get("/api/catalog/export")).text)
            ag = next((x for x in doc.get("agents", []) if x["name"] == "on-vllm"), None)
            ck("catalog export carries the agent's provider", ag is not None and ag.get("provider") == "local-vllm", str(ag))
            chat.shutdown()

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
