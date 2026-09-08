"""The AI SRE demo as a project: the same six objects `demo/catalog.demo.yaml` seeds (three
sources keyed by `service`, the `service_timeline` view, the `incident` trigger, the
`incident-first-look` agent), created with one click in the console instead of importing the
catalog. Same names on purpose, so the docs guide (docs.glassflow.ai/tares/guides/ai-sre) reads
the same whichever way you set it up; an unowned object of the same name is adopted, never
duplicated.

The demo stack itself (api-server, Prometheus, traffic) is Docker on the user's machine and stays
outside Tares: SETUP shows the two commands, and the `inject` action calls the api-server's fault
switch so an incident can be caused and cleared from the project page.

Hosted mode: when the instance is pointed at a hosted demo stack (TARES_DEMO_* env vars, set by
whoever runs the instance — the cloud chart, or a self-hoster with their own stack), the same
template adapts: URL defaults come from the env, logs come from the stack's Loki through the loki
connector instead of a local Docker container, SETUP loses the docker steps, detect() probes the
endpoints instead of shelling docker ps, and the actions say plainly that the stack is shared.
The env vars are read per call, never at import, so one process can be tested in both modes.
"""
from __future__ import annotations

import os

import httpx

from .base import PlannedObject, Template, ProjectError
from .registry import register

SCENARIOS = ("error_spike", "latency", "dependency_outage", "clear")


def _hosted() -> dict | None:
    """The hosted demo stack this instance is pointed at, or None. Hosted mode needs all three
    URLs (TARES_DEMO_PROMETHEUS_URL, TARES_DEMO_LOKI_URL, TARES_DEMO_API_SERVER_URL); the shared
    read credential (TARES_DEMO_USERNAME / TARES_DEMO_PASSWORD) rides along when set."""
    urls = {k: os.getenv(f"TARES_DEMO_{k.upper()}", "").strip().rstrip("/")
            for k in ("prometheus_url", "loki_url", "api_server_url")}
    if not all(urls.values()):
        return None
    return {**urls,
            "username": os.getenv("TARES_DEMO_USERNAME", "").strip(),
            "password": os.getenv("TARES_DEMO_PASSWORD", "").strip()}

PROMPT = """You are an SRE taking the first look when Prometheus fires an alert on api-server. You are \
handed the correlated timeline: the firing alert (HighErrorRate / HighLatency / DependencyDown) \
plus the signals behind it (5xx request rate, p99 latency, active DB connections, dependency \
health) and the service's request logs. That is your evidence.

Produce a tight incident note: (1) what is failing and since when, (2) the most likely cause, \
tied to the specific evidence lines (which signal crossed, what the logs show), (3) a suggested \
next action. If the timeline is too narrow, read a wider window (1h) once before concluding. Do \
not speculate beyond the evidence; if it is inconclusive, say what you would look at next. Plain \
sentences, no em dashes. The incident note is your final message and nothing else: reason through \
tool calls, and only write the note once you have concluded; never narrate what you are about to \
check.
"""


class AiSreDemo(Template):
    key = "ai_sre_demo"
    title = "AI SRE demo"
    description = ("Watch a small demo system, correlate its metrics, logs and alerts on one timeline, "
                   "and let an agent write the first incident note when Prometheus fires.")
    tags = ("demo",)
    sentence = ("show me an agent watching a small service: metrics, logs and alerts on one "
                "timeline, and a first incident note when something breaks")
    guide = {"label": "Build an AI SRE guide", "url": "https://docs.glassflow.ai/tares/guides/ai-sre"}

    PARAMS = {
        "prometheus_url": {"type": "string", "default": "http://localhost:9090", "label": "Prometheus URL",
                           "help": "the demo stack's Prometheus, as reachable from this Tares"},
        "api_server_url": {"type": "string", "default": "http://localhost:8080", "label": "api-server URL",
                           "help": "the demo api-server; used by the Cause an incident action"},
        "loki_url": {"type": "string", "default": "", "label": "Loki URL",
                     "help": "set to read the api-server's logs from a Loki instead of a local "
                             "Docker container (a hosted demo stack sets this for you)"},
        "container": {"type": "string", "default": "tares-demo-api-server", "label": "Log container",
                      "help": "the api-server's Docker container name (fixed in docker-compose.yml)"},
        "model": {"type": "string", "default": "", "label": "Model",
                  "help": "model for the agent (empty = the instance default)"},
    }

    def _params(self) -> dict:
        """PARAMS with hosted defaults substituted, so the wizard's form and validate() both start
        from the stack this instance is actually pointed at. Locally the Loki field is hidden
        entirely — the local flow reads logs from the container and an extra URL field only
        raises questions (pointing at your own Loki still works via the API/YAML)."""
        h = _hosted()
        if not h:
            return {k: v for k, v in self.PARAMS.items() if k != "loki_url"}
        # Hosted: no container field either — logs come from Loki, and a Docker container name
        # is meaningless on a cell with no Docker.
        out = {k: dict(v) for k, v in self.PARAMS.items() if k != "container"}
        for k in ("prometheus_url", "loki_url", "api_server_url"):
            out[k]["default"] = h[k]
        return out

    SETUP = [
        {"title": "Start the demo stack", "check": "detect",
         "text": "Docker Desktop running. Two files, no checkout needed. The form below fills itself "
                 "from what is running.",
         "command": "curl -O https://raw.githubusercontent.com/glassflow/tares/main/demo/docker-compose.yml\n"
                    "docker compose up -d"},
        {"title": "Check it is alive",
         "command": "curl -s localhost:8080/api/stats\ncurl -s 'localhost:9090/api/v1/query?query=up'"},
        {"title": "Give the agent a key", "check": "anthropic_key",
         "text": "The incident-first-look agent is a real agent: it needs an Anthropic key. Set one here "
                 "or under Settings > Anthropic (or ANTHROPIC_API_KEY before tares up). Without one its "
                 "runs log \"no key\"."},
    ]

    ACTIONS = [
        {"name": "inject", "label": "Cause an incident",
         "intro": "Break the demo api-server on purpose and watch the AI SRE work: the fault shows up "
                  "in the metrics and logs, Prometheus fires an alert, the incident trigger wakes "
                  "the agent, and its incident note lands under Runs.",
         "help": "flips the api-server's fault switch; the matching Prometheus alert fires within "
                 "about 30 seconds and the agent wakes",
         "docs": {"label": "Build an AI SRE guide, step 4", "url": "https://docs.glassflow.ai/tares/guides/ai-sre#cause-an-incident-and-watch-the-sre-work"},
         "params": {"scenario": {"label": "scenario", "options": [
             {"value": "error_spike", "label": "error spike",
              "help": "a share of requests start returning 5xx; the 5xx rate climbs and HighErrorRate fires"},
             {"value": "latency", "label": "latency",
              "help": "responses slow past the timeout; p99 climbs and HighLatency fires"},
             {"value": "dependency_outage", "label": "dependency outage",
              "help": "the api-server's database dependency goes down; dependency_up drops to 0 and DependencyDown fires"},
         ]}}},
        {"name": "clear", "label": "Clear the fault",
         "help": "rolls the fault back; the alert resolves and a resolved event lands on the timeline"},
    ]

    def _setup(self) -> list:
        if not _hosted():
            return list(self.SETUP)
        # Hosted: nothing to install. One detect-checked step (the wizard marks it done when the
        # stack answers) plus the key step, verbatim.
        return [
            {"title": "Shared demo environment", "check": "detect",
             "text": "This instance is pointed at a hosted demo stack, shared by everyone trying "
                     "the demo. Nothing to install; the form below fills itself once the stack "
                     "answers."},
            self.SETUP[-1],
        ]

    def _actions(self) -> list:
        if not _hosted():
            return list(self.ACTIONS)
        acts = [dict(a) for a in self.ACTIONS]
        acts[0]["intro"] = (
            "Break the demo api-server on purpose and watch the AI SRE work: the fault shows up "
            "in the metrics and logs, Prometheus fires an alert, the incident trigger wakes the "
            "agent, and its incident note lands under Runs. The demo stack is shared by everyone "
            "trying Tares: causing an incident shows up on every demo timeline, and it also "
            "breaks itself every 30 minutes (cleared 5 minutes later), so you can simply wait "
            "for one.")
        acts[1]["help"] = ("rolls the fault back for everyone on the shared stack; the alert "
                           "resolves and a resolved event lands on the timeline")
        return acts

    def _facts(self) -> dict:
        """The card's you/Tares bullets, in the mode's own words (the console falls back to a
        hardcoded copy for older daemons)."""
        tares = [
            "three sources keyed by service: Prometheus metrics, the api-server's logs, the alerts Prometheus fires",
            "one timeline per service to explore",
            "a trigger that wakes the agent when an alert fires",
            "an agent that writes the first incident note back onto the timeline",
        ]
        if _hosted():
            return {"you": ["give the agent an Anthropic key",
                            "cause an incident from the project page, or wait: the shared demo "
                            "stack breaks itself every 30 minutes"],
                    "tares": tares}
        return {"you": ["start the demo stack with docker compose",
                        "give the agent an Anthropic key",
                        "cause an incident from the project page"],
                "tares": tares}

    def describe(self) -> dict:
        d = super().describe()
        d["params"] = self._params()
        d["setup"] = self._setup()
        d["actions"] = self._actions()
        d["facts"] = self._facts()
        return d

    # ── params ───────────────────────────────────────────────────────────────
    def validate(self, params: dict) -> dict:
        P = self._params()
        p = dict(params or {})
        for name, spec in P.items():
            if name not in p and "default" in spec:
                p[name] = spec["default"]
        for k in ("prometheus_url", "api_server_url"):
            v = str(p.get(k) or P[k]["default"]).strip().rstrip("/")
            if not v.startswith(("http://", "https://")):
                raise ProjectError(f"{k} must start with http:// or https://")
            p[k] = v
        loki = str(p.get("loki_url") or "").strip().rstrip("/")
        if loki and not loki.startswith(("http://", "https://")):
            raise ProjectError("loki_url must start with http:// or https://")
        p["loki_url"] = loki
        # the form may not have offered container (hosted mode); the stored default still applies
        p["container"] = str(p.get("container") or self.PARAMS["container"]["default"]).strip()
        p["model"] = str(p.get("model") or "").strip()
        return p

    # ── plan: the demo catalog, verbatim ─────────────────────────────────────
    def plan(self, params: dict) -> list[PlannedObject]:
        prom = params["prometheus_url"]
        # The shared read credential comes from the env, not the params: it belongs to the
        # instance's wiring (like the stack URLs' defaults), and a secret must not live in the
        # stored project params, which any console reader can see.
        h = _hosted()
        cred = ({"username": h["username"], "password": h["password"]}
                if h and h["username"] else {})
        # Routine 2xx access lines are dropped: the traffic generator produces ~3 of them a second
        # around the clock, they carry nothing the request-rate metrics do not, and on a hosted
        # stack every demo cell would ingest them forever. Errors, 4xx/5xx and non-access lines
        # (the incident evidence) all stay.
        drop_ok = 'HTTP/1.1" 2'
        if params.get("loki_url"):
            logs = PlannedObject("source", "logs", {
                "name": "demo_logs", "connector": "loki", "poll": "10s",
                "config": {"url": params["loki_url"], "query": '{service="api-server"}', **cred,
                           "drop": drop_ok,
                           "labels": [{"name": "service", "const": "api-server", "primary": True}]}})
        else:
            logs = PlannedObject("source", "logs", {
                "name": "demo_logs", "connector": "docker_logs", "poll": "5s",
                "config": {"container": params["container"], "drop": drop_ok,
                           "labels": [{"name": "service", "const": "api-server", "primary": True}]}})
        objs = [
            PlannedObject("source", "metrics", {
                "name": "demo_metrics", "connector": "prometheus", "poll": "5s",
                "config": {
                    "url": prom, **cred,
                    "queries": [
                        {"promql": 'sum(rate(http_requests_total{status=~"5.."}[1m])) by (service)',
                         "event_type": "5xx_rate", "text": "5xx rate {service}={val}/s"},
                        {"promql": 'histogram_quantile(0.99, sum(rate(http_request_duration_milliseconds_bucket[2m])) by (le, service))',
                         "event_type": "p99", "text": "p99 {service}={val}ms"},
                        {"promql": "db_connections_active", "event_type": "db_active",
                         "text": "db connections active={val}"},
                        {"promql": "dependency_up", "event_type": "dependency",
                         "text": "dependency {dependency} up={val}"},
                    ],
                    "labels": [{"name": "service", "const": "api-server", "primary": True},
                               {"name": "value", "field": "value", "type": "number"}],
                }}),
            logs,
            PlannedObject("source", "alerts", {
                "name": "demo_alerts", "connector": "prometheus_alerts", "poll": "10s",
                "config": {"url": prom, **cred,
                           "labels": [{"name": "service", "field": "labels.service", "primary": True},
                                      {"name": "alertname", "field": "labels.alertname"},
                                      {"name": "severity", "field": "labels.severity"},
                                      {"name": "state", "field": "state"},
                                      {"name": "alert_active", "const": 1, "type": "number"}]}}),
            PlannedObject("view", "view", {
                "name": "service_timeline", "key_field": "service",
                "sources": ["demo_logs", "demo_metrics", "demo_alerts"]}),
            PlannedObject("trigger", "trigger", {
                "name": "incident", "view": "service_timeline",
                "condition": {"aggregate": "sum", "field": "alert_active", "predicate": "> 0",
                              "window": "1m", "group_by": ["key_value"]},
                "emit": {"kind": "incident", "attach_view": True, "context_window": "15m"},
                "cooldown": "5m"}),
        ]
        agent = {"name": "incident-first-look", "trigger": "incident", "enabled": True, "prompt": PROMPT}
        if params.get("model"):
            agent["model"] = params["model"]
        if _hosted():
            # The hosted demo stack is shared and fires around the clock, so an idle cell would
            # keep running the agent every cooldown until its trial credit was gone. $2 covers a
            # real look at the demo; the run that hits the cap says where to raise it.
            agent["budget_usd"] = 2.0
        objs.append(PlannedObject("agent", "agent", agent))
        return objs

    # ── detect: ask Docker what is running instead of assuming ports ─────────
    async def detect(self, store, runtime) -> dict:
        h = _hosted()
        if h:
            return await self._detect_hosted(h)
        from ..discovery import _docker_ps, _published_port
        out = {"params": {}, "found": {}, "missing": {}, "notes": []}
        try:
            containers = await _docker_ps()
        except ValueError as e:
            out["notes"].append(str(e))
            for k in ("prometheus_url", "api_server_url", "container"):
                out["missing"][k] = "Docker not reachable; using the default"
            return out
        api = next((c for c in containers if "tares-demo-api-server" in c["image"]
                    or c["name"] == "tares-demo-api-server"), None)
        prom = next((c for c in containers if "prom/prometheus" in c["image"]
                     or _published_port(c["ports"], 9090)), None)
        if api:
            port = _published_port(api["ports"], 8080)
            if port:
                out["params"]["api_server_url"] = f"http://localhost:{port}"
                out["found"]["api_server_url"] = f"container {api['name']} publishes port {port}"
            else:
                out["missing"]["api_server_url"] = f"container {api['name']} runs but publishes no port 8080"
            out["params"]["container"] = api["name"]
            out["found"]["container"] = f"container {api['name']} (image {api['image'].split('/')[-1]})"
        else:
            out["missing"]["api_server_url"] = "no demo api-server container is running; start the demo stack"
            out["missing"]["container"] = "no demo api-server container is running"
        if prom:
            port = _published_port(prom["ports"], 9090) or 9090
            out["params"]["prometheus_url"] = f"http://localhost:{port}"
            out["found"]["prometheus_url"] = f"container {prom['name']} publishes port {port}"
        else:
            out["missing"]["prometheus_url"] = "no Prometheus container is running; start the demo stack"
        if not containers:
            out["notes"].append("Docker is running but no compose-managed containers were found")
        return out

    async def _detect_hosted(self, h: dict) -> dict:
        """Hosted mode: no Docker to ask. Probe the stack's three endpoints (with the shared
        credential) and fill the URL params from the env; a down endpoint lands in `missing` with
        the error, so the wizard says which half of the stack is unreachable."""
        out = {"params": {}, "found": {}, "missing": {}, "notes": []}
        auth = (h["username"], h["password"]) if h["username"] else None
        probes = {
            "prometheus_url": f"{h['prometheus_url']}/api/v1/query?query=up",
            "loki_url": f"{h['loki_url']}/loki/api/v1/labels",
            "api_server_url": f"{h['api_server_url']}/api/stats",
        }
        async with httpx.AsyncClient(timeout=10, auth=auth) as cx:
            for param, probe in probes.items():
                out["params"][param] = h[param]
                try:
                    r = await cx.get(probe)
                    if r.status_code < 300:
                        out["found"][param] = "hosted demo stack answered"
                    else:
                        out["missing"][param] = f"hosted demo stack answered HTTP {r.status_code}"
                except Exception as e:
                    out["missing"][param] = f"could not reach the hosted demo stack: {type(e).__name__}"
        return out

    # ── actions ──────────────────────────────────────────────────────────────
    def run_action(self, instance: dict, action: str, args: dict, store, runtime) -> dict:
        params = self.validate(instance["params"])
        if action == "inject":
            scenario = str(args.get("scenario") or "error_spike")
            if scenario not in SCENARIOS or scenario == "clear":
                raise ProjectError(f"scenario must be one of {[s for s in SCENARIOS if s != 'clear']}")
        elif action == "clear":
            scenario = "clear"
        else:
            raise ProjectError(f"{self.key}: no action {action!r}")
        url = params["api_server_url"] + "/demo/inject"
        h = _hosted()
        auth = (h["username"], h["password"]) if h and h["username"] else None
        try:
            r = httpx.post(url, json={"scenario": scenario}, timeout=10, auth=auth)
        except Exception as e:
            raise ProjectError(f"could not reach the demo api-server at {url}: {e}") from e
        if r.status_code >= 300:
            raise ProjectError(f"api-server answered {r.status_code} for {scenario}")
        msg = ("fault cleared; the alert resolves within about a minute" if scenario == "clear"
               else f"{scenario} injected; in about 30 seconds the alert fires, the trigger wakes the "
                    f"agent, and its run appears under Runs below")
        return {"scenario": scenario, "message": msg}

    # ── summary ──────────────────────────────────────────────────────────────
    def summary(self, instance: dict, store) -> dict:
        out = super().summary(instance, store)
        params = self.validate(instance["params"])
        out["panels"] = [{
            "title": "Demo stack",
            "rows": [
                {"label": "Prometheus", "value": params["prometheus_url"], "mono": True,
                 "url": params["prometheus_url"]},
                {"label": "api-server", "value": params["api_server_url"], "mono": True,
                 "url": params["api_server_url"]},
                {"label": "log container", "value": params["container"], "mono": True},
            ]}]
        return out


register(AiSreDemo())
