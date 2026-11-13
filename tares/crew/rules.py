"""The rules a station starts under (TR-864): the sentences Tares renders for it, a copy kept for a start while
Tares is down, and the defaults the plugin ships as the last resort.

`for_station` is what `launch.write_settings` calls. In order:
  1. Tares answers: `GET /api/factory/guardrails/rules?station=&project=&role=` (the cell must list the
     `guardrail_rules` capability, else the start stops with the upgrade message). The answer is saved to
     `<crew home>/rules/<station>.json` as it came, placeholders and all.
  2. Tares does not answer or cannot give them: the saved copy of this station.
  3. No usable copy: the defaults in `claude-plugin/templates/default-rules.json`, for the station's role.
     They carry no grants and none of the cell's own deny rules.
Rules that come out empty stop the start with a message; a station never runs on no rules.

The sentences keep a `{name}` for what only this machine knows (the person, the crew folder, the repositories);
`fill` puts them in at the start, so a copy is current when it is used.

Standard library plus the crew modules.
"""
from __future__ import annotations

from pathlib import Path

from . import state
from .client import TaresClient, TaresHTTPError, uri
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


def saved(paths: CrewPaths, station: str):
    """The copy of the rules Tares last gave this station, or None when there is none, or it holds none."""
    try:
        doc = state.read_json(paths.rules_file(station), None)
    except (OSError, ValueError):
        return None
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


def for_station(client: TaresClient, station: str, project: str, role: str, kit: str, values: dict) -> dict:
    """The rendered rules for a station, placeholders filled: {allow, soft_deny, rules, source}, where
    source is `tares`, `copy` or `shipped`. Saves what Tares gave. CrewError when nothing gives any rule."""
    paths = client.paths
    doc, source = None, ""
    if client.tares_on():
        try:
            doc, source = fetch(client, station, project, role), "tares"
        except TaresHTTPError as e:
            if e.status in (401, 403):
                raise CrewError(f"Tares refused to give the rules for {station} (HTTP {e.status}). A station never "
                                "starts without its rules; check the crew's key on Tares, then run this again.")
            client.say(f"could not read the rules for {station} from Tares ({e}); using the copy kept from its "
                       "last start, else the ones shipped with the plugin")
        except ValueError as e:
            client.say(f"could not read the rules for {station} from Tares ({e}); using the copy kept from its "
                       "last start, else the ones shipped with the plugin")
        if doc is not None and not usable(doc):
            raise CrewError(f"Tares gave no rules for {station}. A station never starts without rules; check the "
                            "guardrails on the cell, then run this again.")
    if doc is None:
        doc, source = saved(paths, station), "copy"
    if doc is None:
        doc, source = shipped(kit, role), "shipped"
    elif source == "tares":
        paths.rules_dir.mkdir(parents=True, exist_ok=True)
        state.write_json(paths.rules_file(station), doc)
    rules = [{**r, "text": fill(r.get("text", ""), values)} for r in doc.get("rules") or [] if isinstance(r, dict)]
    out = {"allow": [fill(s, values) for s in _sentences(doc, "allow")],
           "soft_deny": [fill(s, values) for s in _sentences(doc, "soft_deny")],
           "rules": rules, "source": source}
    if not out["allow"] and not out["soft_deny"]:
        raise CrewError(f"no rules for {station}: Tares gave none, and there is no copy or shipped default. "
                        "A station never starts without rules.")
    return out


def added(old, new: dict) -> list:
    """The sentences of `new` (what Tares renders now) that are not in `old` (a saved copy, or None), both as Tares
    gave them, before `fill`. Without a copy, only the ones that come from a grant."""
    have = {r.get("text") for r in (old or {}).get("rules") or [] if isinstance(r, dict)}
    out = []
    for r in new.get("rules") or []:
        if not isinstance(r, dict) or r.get("text") in have:
            continue
        if old is None and r.get("source") != "grant":
            continue
        out.append(r.get("text", ""))
    return [t for t in out if t]
