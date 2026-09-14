"""Agent tracing: one OpenTelemetry trace per agent run, exported over OTLP/HTTP.

Every Tares agent run and every Ask turn becomes a trace: a root span for the run, one LLM span
per model call (model, messages, tokens, stop reason) and one tool span per tool call. The spans
carry the OpenInference and gen_ai attribute names, which is what the GenAI observability
backends read: Rius, Langfuse, Phoenix, an OpenTelemetry collector, anything that accepts
OTLP/HTTP. The cell has no vendor dependency; a provider is a preset that fills defaults.

    provider `rius`  endpoint defaults to Rius's ingest, the API key becomes a bearer header
    provider `otlp`  any endpoint, headers given explicitly (Langfuse, Phoenix, a collector)

Configuration is a console setting first and an environment variable second, like the
Anthropic key: a value saved in the console takes over from whatever the deployment shipped.
Tares Cloud ships the Rius preset and the shared key through the environment, so the only
control a cloud user meets is the switch.

Separation per agent: backends group every view by the `service.name` resource attribute, and
that attribute is fixed per tracer provider. So each agent gets its own provider with
`service.name = <instance>/<agent>` (`TARES_INSTANCE_NAME`, else the hostname), built on first
use and kept for the life of the process. A changed setting (endpoint, key, switch) rebuilds
them on the next run; nothing needs a restart.

Tracing must never break a run: every helper here swallows its own failures, the exporter
batches in a background thread, and a backend that is down only costs dropped spans.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

# ── conventions (OpenInference + OpenTelemetry gen_ai) ─────────────────────────
SPAN_KIND = "openinference.span.kind"          # AGENT | CHAIN | LLM | TOOL
INPUT_VALUE = "input.value"
OUTPUT_VALUE = "output.value"
SESSION_ID = "session.id"
GEN_AI_OPERATION = "gen_ai.operation.name"
GEN_AI_PROVIDER = "gen_ai.provider.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_INPUT_MESSAGES = "gen_ai.input.messages"
GEN_AI_OUTPUT_MESSAGES = "gen_ai.output.messages"
GEN_AI_USAGE_INPUT = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT = "gen_ai.usage.output_tokens"
GEN_AI_USAGE_CACHE_CREATION = "gen_ai.usage.cache_creation_input_tokens"
GEN_AI_USAGE_CACHE_READ = "gen_ai.usage.cache_read_input_tokens"
GEN_AI_FINISH_REASONS = "gen_ai.response.finish_reasons"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_FIRST_TOKEN = "gen_ai.first_token"
_OPERATION_BY_KIND = {"AGENT": "invoke_agent", "CHAIN": "chain", "LLM": "chat",
                      "TOOL": "execute_tool"}

# ── providers ──────────────────────────────────────────────────────────────────
PROVIDERS = ("rius", "otlp")
RIUS_ENDPOINT = "https://ingest.eu.console.rius-glassflow.com"
RIUS_CONSOLE_URL = "https://eu.console.rius-glassflow.com"

# settings-table keys and their environment fallbacks
SETTING_ENABLED = ("tracing_enabled", "TARES_TRACING_ENABLED")
SETTING_PROVIDER = ("tracing_provider", "TARES_TRACING_PROVIDER")
SETTING_ENDPOINT = ("tracing_endpoint", "TARES_TRACING_ENDPOINT")
SETTING_API_KEY = ("tracing_api_key", "TARES_TRACING_API_KEY")
SETTING_HEADERS = ("tracing_headers", "TARES_TRACING_HEADERS")
ENV_INSTANCE = "TARES_INSTANCE_NAME"

MAX_ATTR_CHARS = 32_768   # per string attribute; a timeline payload can be large
_TRUE = ("1", "true", "yes", "on")


def _read(store, key: str, env: str) -> tuple[str, str]:
    """(value, where-it-came-from): the console-stored value, else the environment, else ''."""
    stored = (store.get_setting(key) or "").strip() if store is not None else ""
    if stored:
        return stored, "console"
    val = os.getenv(env, "").strip()
    return (val, f"env:{env}") if val else ("", "")


def parse_headers(raw: str) -> dict[str, str]:
    """`k=v,k2=v2` (the OTEL_EXPORTER_OTLP_HEADERS shape) -> dict. Bad pairs are skipped."""
    out: dict[str, str] = {}
    for pair in (raw or "").split(","):
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        if k.strip():
            out[k.strip()] = v.strip()
    return out


def instance_name() -> str:
    return os.getenv(ENV_INSTANCE, "").strip() or socket.gethostname() or "tares"


@dataclass(frozen=True)
class TracingConfig:
    enabled: bool
    provider: str
    endpoint: str
    headers: dict[str, str]
    instance: str
    # where each value came from ("console", "env:...", "preset", "default", "")
    sources: dict[str, str] = field(default_factory=dict)
    key_stored: bool = False
    key_configured: bool = False
    headers_configured: bool = False

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.endpoint)

    @property
    def fingerprint(self) -> tuple:
        return (self.endpoint, tuple(sorted(self.headers.items())), self.instance)


def resolve(store) -> TracingConfig:
    enabled_raw, enabled_src = _read(store, *SETTING_ENABLED)
    provider, provider_src = _read(store, *SETTING_PROVIDER)
    endpoint, endpoint_src = _read(store, *SETTING_ENDPOINT)
    api_key, key_src = _read(store, *SETTING_API_KEY)
    headers_raw, headers_src = _read(store, *SETTING_HEADERS)

    provider = provider.lower()
    if provider not in PROVIDERS:
        # No provider named: an explicit endpoint means "some OTLP backend"; a key alone means
        # Rius, whose endpoint the preset knows.
        provider, provider_src = ("otlp", "default") if endpoint else ("rius", "default")
    if provider == "rius" and not endpoint:
        endpoint, endpoint_src = RIUS_ENDPOINT, "preset"

    headers = parse_headers(headers_raw)
    if api_key and not any(k.lower() == "authorization" for k in headers):
        headers["authorization"] = f"Bearer {api_key}"

    return TracingConfig(
        enabled=enabled_raw.lower() in _TRUE,
        provider=provider, endpoint=endpoint.rstrip("/"), headers=headers,
        instance=instance_name(),
        sources={"enabled": enabled_src, "provider": provider_src, "endpoint": endpoint_src,
                 "api_key": key_src, "headers": headers_src},
        key_stored=bool((store.get_setting(SETTING_API_KEY[0]) or "").strip()) if store else False,
        key_configured=bool(api_key), headers_configured=bool(headers_raw),
    )


def status(store) -> dict:
    """What the settings API returns: everything but the secrets."""
    cfg = resolve(store)
    return {
        "enabled": cfg.enabled, "enabled_source": cfg.sources["enabled"],
        "active": cfg.active,
        "provider": cfg.provider, "provider_source": cfg.sources["provider"],
        "endpoint": cfg.endpoint, "endpoint_source": cfg.sources["endpoint"],
        "key_configured": cfg.key_configured, "key_source": cfg.sources["api_key"],
        "key_stored": cfg.key_stored,
        "headers_configured": cfg.headers_configured, "headers_source": cfg.sources["headers"],
        "instance": cfg.instance,
        "providers": list(PROVIDERS),
        "rius_console_url": RIUS_CONSOLE_URL,
    }


# ── the provider cache ─────────────────────────────────────────────────────────
class Tracing:
    """One tracer provider per traced name (`<instance>/<name>`), built lazily, rebuilt when
    the configuration changes, flushed at shutdown. `tracer_for` returns None whenever tracing
    is off, so a call site holds either a tracer or nothing and the helpers below accept both."""

    def __init__(self, store, exporter_factory=None):
        self.store = store
        # exporter_factory(config) -> SpanExporter; tests hand in an in-memory one
        self._exporter_factory = exporter_factory
        self._providers: dict[str, Any] = {}
        self._fingerprint: tuple | None = None
        self._lock = threading.Lock()
        # one identity per process, shared by every provider it builds
        self._instance_id = str(uuid.uuid4())

    def tracer_for(self, name: str):
        try:
            cfg = resolve(self.store)
        except Exception as e:   # a broken settings read must not stop the run
            print(f"tracing: config read failed: {type(e).__name__}: {e}")
            return None
        if not cfg.active:
            if self._providers:
                self.shutdown()
            return None
        with self._lock:
            if cfg.fingerprint != self._fingerprint:
                self._shutdown_locked()
                self._fingerprint = cfg.fingerprint
            provider = self._providers.get(name)
            if provider is None:
                try:
                    provider = self._build(cfg, name)
                except Exception as e:   # missing SDK, bad endpoint: trace nothing, say so once
                    print(f"tracing: could not start a tracer for {name!r}: "
                          f"{type(e).__name__}: {e}")
                    return None
                self._providers[name] = provider
        return provider.get_tracer("tares")

    def _build(self, cfg: TracingConfig, name: str):
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        try:
            from importlib.metadata import version as _v
            tares_version = _v("tares")
        except Exception:
            tares_version = "dev"
        resource = Resource.create({
            "service.name": f"{cfg.instance}/{name}",
            "service.instance.id": self._instance_id,
            "tares.instance": cfg.instance,
            "tares.agent": name,
            "tares.version": tares_version,
        })
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(_SessionProcessor())
        if self._exporter_factory is not None:
            exporter = self._exporter_factory(cfg)
        else:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            exporter = OTLPSpanExporter(endpoint=f"{cfg.endpoint}/v1/traces",
                                        headers=cfg.headers or None)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        return provider

    def flush(self, timeout_ms: int = 5000) -> None:
        with self._lock:
            for provider in list(self._providers.values()):
                try:
                    provider.force_flush(timeout_ms)
                except Exception:
                    pass

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown_locked()

    def _shutdown_locked(self) -> None:
        for provider in list(self._providers.values()):
            try:
                provider.force_flush(5000)
                provider.shutdown()
            except Exception:
                pass
        self._providers.clear()
        self._fingerprint = None


# ── session propagation (session.id on every span of a run) ───────────────────
# Backends derive a per-span session id with the trace id as the fallback, so stamping the root
# alone would scatter the children. The id rides the OTel context and a processor copies it onto
# each span at start, the same way the Rius SDK does it.
try:
    from opentelemetry import context as _otel_context
    from opentelemetry.sdk.trace import SpanProcessor as _SpanProcessor
    _SESSION_KEY = _otel_context.create_key("tares-session-id")

    class _SessionProcessor(_SpanProcessor):
        def on_start(self, span, parent_context=None):
            value = _otel_context.get_value(_SESSION_KEY, context=parent_context)
            if isinstance(value, str) and value:
                span.set_attribute(SESSION_ID, value)

        def on_end(self, span):
            pass

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return True
except ImportError:   # SDK absent: Tracing._build raises and tracer_for returns None
    _otel_context = None
    _SESSION_KEY = None
    _SessionProcessor = None   # type: ignore[assignment,misc]


# ── serialisation ──────────────────────────────────────────────────────────────
def _clip(s: str) -> str:
    return s if len(s) <= MAX_ATTR_CHARS else s[:MAX_ATTR_CHARS] + "…"


def serialize(value: Any) -> str:
    if isinstance(value, str):
        return _clip(value)
    try:
        return _clip(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return _clip(repr(value))


def _part(item: Any) -> dict:
    """One Anthropic content block -> the gen_ai message part shape."""
    if isinstance(item, dict):
        t = item.get("type")
        if t == "text":
            return {"type": "text", "content": item.get("text", "")}
        if t == "tool_use":
            return {"type": "tool_call", "id": item.get("id"), "name": item.get("name"),
                    "arguments": item.get("input")}
        if t == "tool_result":
            return {"type": "tool_call_response", "id": item.get("tool_use_id"),
                    "response": item.get("content")}
        if t:
            return item
        return {"type": "text", "content": serialize(item)}
    # SDK objects (anthropic.types.*) expose the same fields as attributes
    t = getattr(item, "type", None)
    if t == "text":
        return {"type": "text", "content": getattr(item, "text", "")}
    if t == "tool_use":
        return {"type": "tool_call", "id": getattr(item, "id", None),
                "name": getattr(item, "name", None), "arguments": getattr(item, "input", None)}
    return {"type": "text", "content": item if isinstance(item, str) else serialize(item)}


def _message(message: Any, default_role: str) -> dict:
    if isinstance(message, str):
        return {"role": default_role, "parts": [{"type": "text", "content": message}]}
    if not isinstance(message, dict):
        return {"role": default_role, "parts": [{"type": "text", "content": serialize(message)}]}
    role = message.get("role", default_role)
    content = message.get("content")
    if isinstance(content, str):
        parts = [{"type": "text", "content": content}]
    elif isinstance(content, list):
        parts = [_part(c) for c in content]
    elif content is None:
        parts = []
    else:
        parts = [{"type": "text", "content": serialize(content)}]
    return {"role": role, "parts": parts}


def messages_json(messages: Any, default_role: str) -> str:
    if isinstance(messages, str):
        return serialize([_message(messages, default_role)])
    if isinstance(messages, list) and messages and not isinstance(messages[0], dict) \
            and not isinstance(messages[0], str) and hasattr(messages[0], "type"):
        # a bare list of content blocks (a model response): one message
        return serialize([{"role": default_role, "parts": [_part(c) for c in messages]}])
    if isinstance(messages, list) and messages and isinstance(messages[0], dict) \
            and "role" not in messages[0] and "type" in messages[0]:
        return serialize([{"role": default_role, "parts": [_part(c) for c in messages]}])
    return serialize([_message(m, default_role) for m in (messages or [])])


# ── span helpers ───────────────────────────────────────────────────────────────
class Observation:
    """A span with the OpenInference annotation surface. `None`-safe: every method is a no-op
    on an observation without a span, so call sites need no `if tracer` guards."""

    def __init__(self, span=None):
        self._span = span

    def set_input(self, value: Any) -> None:
        self.set_attribute(INPUT_VALUE, serialize(value))

    def set_output(self, value: Any) -> None:
        self.set_attribute(OUTPUT_VALUE, serialize(value))

    def set_attribute(self, key: str, value: Any) -> None:
        if self._span is None or value is None:
            return
        try:
            self._span.set_attribute(key, value)
        except Exception:
            pass

    def tool_error(self, detail: str) -> None:
        """A tool call that returned an error. The model gets the error text back and goes on,
        so the run is not failed: the span carries the text and a flag, but NOT the ERROR status
        (a backend rolls span status up to the trace, and a trace with one fumbled tool call
        would read as a failed run)."""
        self.set_attribute("tares.tool_error", True)
        self.set_output(detail)

    def error(self, detail: str | BaseException) -> None:
        """The run itself failed: ERROR status, which the backend rolls up to the trace."""
        if self._span is None:
            return
        try:
            from opentelemetry.trace import Status, StatusCode
            if isinstance(detail, BaseException):
                self._span.record_exception(detail)
                detail = f"{type(detail).__name__}: {detail}"
            self._span.set_status(Status(StatusCode.ERROR, str(detail)[:500]))
        except Exception:
            pass


class Generation(Observation):
    """An LLM span: messages in the gen_ai shape, usage, response model, stop reason."""

    def __init__(self, span=None):
        super().__init__(span)
        self._first_token = False

    def set_input(self, messages: Any) -> None:
        self.set_attribute(GEN_AI_INPUT_MESSAGES, messages_json(messages, "user"))

    def set_output(self, messages: Any) -> None:
        self.set_attribute(GEN_AI_OUTPUT_MESSAGES, messages_json(messages, "assistant"))

    def set_usage(self, usage: Any) -> None:
        """`usage` is Anthropic's usage block, as a dict or an SDK object."""
        get = (usage.get if isinstance(usage, dict) else
               lambda k, d=None: getattr(usage, k, d))
        for key, attr in (("input_tokens", GEN_AI_USAGE_INPUT),
                          ("output_tokens", GEN_AI_USAGE_OUTPUT),
                          ("cache_creation_input_tokens", GEN_AI_USAGE_CACHE_CREATION),
                          ("cache_read_input_tokens", GEN_AI_USAGE_CACHE_READ)):
            val = get(key)
            if val is not None:
                self.set_attribute(attr, int(val))

    def set_response_model(self, model: str | None) -> None:
        if model:
            self.set_attribute(GEN_AI_RESPONSE_MODEL, model)

    def set_finish_reason(self, reason: str | None) -> None:
        if reason:
            self.set_attribute(GEN_AI_FINISH_REASONS, [reason])

    def record_first_token(self) -> None:
        if self._first_token or self._span is None:
            return
        self._first_token = True
        try:
            self._span.add_event(GEN_AI_FIRST_TOKEN)
        except Exception:
            pass


@contextmanager
def _span(tracer, name: str, attributes: dict, cls=Observation) -> Iterator[Observation]:
    if tracer is None:
        yield cls(None)
        return
    try:
        cm = tracer.start_as_current_span(name, attributes=attributes)
        span = cm.__enter__()
    except Exception:
        yield cls(None)
        return
    obs = cls(span)
    try:
        yield obs
    except BaseException as e:
        obs.error(e)
        cm.__exit__(type(e), e, e.__traceback__)
        raise
    else:
        cm.__exit__(None, None, None)


@contextmanager
def run_span(tracer, name: str, *, kind: str = "AGENT", session: str | None = None,
             attributes: dict | None = None) -> Iterator[Observation]:
    """The root span of a run. `session` (the entity key) groups the runs that looked at the
    same entity; it is stamped on every span of the run via the OTel context."""
    attrs = {SPAN_KIND: kind, GEN_AI_OPERATION: _OPERATION_BY_KIND[kind]}
    for k, v in (attributes or {}).items():
        if v is not None and v != "":
            attrs[k] = v
    token = None
    if tracer is not None and session and _otel_context is not None:
        try:
            token = _otel_context.attach(_otel_context.set_value(_SESSION_KEY, session))
        except Exception:
            token = None
    try:
        with _span(tracer, name, attrs) as obs:
            yield obs
    finally:
        if token is not None:
            try:
                _otel_context.detach(token)
            except Exception:
                pass


@contextmanager
def generation(tracer, model: str, messages: Any = None,
               parameters: dict | None = None, provider: str = "anthropic") -> Iterator[Generation]:
    """One model call. Sets the request side up front; the caller records the response with
    `set_output` / `set_usage` / `set_response_model` / `set_finish_reason`. `provider` is the
    adapter's kind (anthropic, openai), what gen_ai.provider.name names (TR-305)."""
    attrs = {SPAN_KIND: "LLM", GEN_AI_OPERATION: "chat", GEN_AI_PROVIDER: provider or "anthropic",
             GEN_AI_REQUEST_MODEL: model}
    for k, v in (parameters or {}).items():
        if v is not None:
            attrs[f"gen_ai.request.{k}"] = v
    with _span(tracer, f"chat {model}", attrs, cls=Generation) as gen:
        if messages is not None:
            gen.set_input(messages)
        yield gen


@contextmanager
def tool_span(tracer, name: str, arguments: Any = None) -> Iterator[Observation]:
    attrs = {SPAN_KIND: "TOOL", GEN_AI_OPERATION: "execute_tool", GEN_AI_TOOL_NAME: name}
    with _span(tracer, name, attrs) as obs:
        if arguments is not None:
            obs.set_input(arguments)
        yield obs
