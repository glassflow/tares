"""Helpers for a station's rules (TR-864), now that the cell builds a station's launch (TR-870).

A start asks the cell for the whole launch spec (`launch.launch_plan`), rules included, so this module no
longer decides where a station's rules come from. What it keeps:
  `fetch`: ask Tares for the rendered rules of a station (`GET /api/factory/guardrails/rules`), which
     `crew regrant` uses to tell what a grant added since the last start.
  `saved`: the rules the station last started with (read from its saved launch spec), `usable`: is an answer a
     set of rules, `added`: the sentences new since then.
  `shipped`: the defaults the plugin ships in `claude-plugin/templates/default-rules.json`, rendered for a role
     (the file the cell's own defaults are checked against).
  `fill`: puts a machine's `{name}` values into a sentence.

The sentences keep a `{name}` for what only this machine knows (the person, the crew folder, the repositories).

Standard library plus the crew modules.
"""
from __future__ import annotations

from pathlib import Path

from . import state
from .client import TaresClient, uri
from .errors import CrewError
from .paths import CrewPaths

ROLES = ("orchestrator", "builder", "reviewer", "releaser")
CAPABILITY = "guardrail_rules"
SHIPPED = "templates/default-rules.json"


def fill(text: str, values: dict) -> str:
    """`text` with each `{name}` of `values` put in; any other `{name}` stays as it is."""
    out = str(text or "")
    for key, value in values.items():
        out = out.replace("{" + key + "}", str(value))
    return out


def _sentences(doc, key: str) -> list:
    got = doc.get(key) if isinstance(doc, dict) else None
    return [s for s in got if isinstance(s, str) and s.strip()] if isinstance(got, list) else []


def usable(doc) -> bool:
    """True for a rendered answer with both kinds of sentence: what the station may do and what it must not. An
    answer that lost the deny side (the production guard) is not a set of rules to start a station under."""
    return bool(_sentences(doc, "allow") and _sentences(doc, "soft_deny"))


def shipped(kit: str, role: str) -> dict:
    """The defaults the plugin ships, rendered like Tares renders them for `role` with no grants and no own
    rules: {allow, soft_deny, rules}. A file that is missing or holds no rule is a CrewError."""
    file = Path(kit) / SHIPPED
    doc = state.read_json(file, None)
    items = doc.get("defaults") if isinstance(doc, dict) else None
    allow, soft_deny, rules = [], [], []
    for d in items if isinstance(items, list) else []:
        if not isinstance(d, dict) or not isinstance(d.get("text"), str) or d.get("kind") not in ("allow", "deny"):
            continue
        if d.get("role") not in ("all", role):
            continue
        (allow if d["kind"] == "allow" else soft_deny).append(d["text"])
        rules.append({"id": d.get("id", ""), "kind": d["kind"], "source": "default", "text": d["text"]})
    if not allow and not soft_deny:
        raise CrewError(f"the rules shipped with the plugin ({file}) are missing or empty. Reinstall the "
                        "plugin, then run this again.")
    return {"allow": allow, "soft_deny": soft_deny, "rules": rules}


def saved(paths: CrewPaths, station: str, placeholders: dict | None = None, tares: str = ""):
    """The rule sentences this station last started with: {allow, soft_deny}, read from the launch spec the cell
    gave it (<crew home>/launch/<station>.json) with this machine's `placeholders` filled in, so they read as
    `fill` writes a new sentence. A sentence that names the address of Tares as it was then (the spec's
    TARESD_URL) names `tares` instead, so a moved Tares does not make every such sentence new. None when there
    is no spec, or it holds no rules."""
    from ..integrations.runtime import fill_placeholders   # here: the runtime imports the crew modules
    try:
        spec = state.read_json(paths.launch_file(station), None)
    except (OSError, ValueError):
        return None
    if not isinstance(spec, dict):
        return None
    auto = (spec.get("settings") or {}).get("autoMode") or {}
    then = ((spec.get("env") or {}).get("TARESD_URL") or "").strip()
    doc = {}
    for key in ("allow", "soft_deny"):
        doc[key] = [fill_placeholders(s, placeholders or {}) for s in _sentences(auto, key) if s != "$defaults"]
        if then and tares and then != tares:
            doc[key] = [s.replace(then, tares) for s in doc[key]]
    return doc if usable(doc) else None


def fetch(client: TaresClient, station: str, project: str, role: str) -> dict:
    """The rules Tares renders for the station now. A CrewError when the cell is older (no `guardrail_rules`);
    TaresHTTPError when it cannot answer; ValueError for an answer that is not rules."""
    client.need_capability(CAPABILITY)
    q = f"?station={uri(station)}"
    if project:
        q += f"&project={uri(project)}"
    if role in ROLES:
        q += f"&role={uri(role)}"
    # Tares answers a station's own rules to a call that says which station it is (a person's key alone gets a
    # 403), the same labels the plugin's MCP proxy sets.
    who = {"X-Tares-Station": station}
    if role in ROLES:
        who["X-Tares-Role"] = role
    doc = state.loads_json(client.tares_http("GET", f"/api/factory/guardrails/rules{q}", headers=who))
    if not isinstance(doc, dict):
        raise ValueError("the rules are not JSON")
    return doc


def added(old, new: dict, values: dict) -> list:
    """The sentences of `new` (what Tares renders now, with their `{name}`s) that are not in `old` (what `saved` read,
    or None), each filled with `values`. Without a copy, only the ones that come from a grant."""
    have = set((old or {}).get("allow") or []) | set((old or {}).get("soft_deny") or [])
    out = []
    for r in new.get("rules") or []:
        if not isinstance(r, dict) or not r.get("text"):
            continue
        text = fill(r["text"], values)
        if text in have or (old is None and r.get("source") != "grant"):
            continue
        out.append(text)
    return out
