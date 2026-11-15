"""The factory setup screen (TR-871): GET /api/factory/setup.

One read gives everything the "Set up your factory" screen shows (the TR-868 mock), so the
console only renders it. The seven steps in order, each with what is connected, what could be
connected, and the settings form of every integration, with every string worded for people and
shown as it is. Nothing is stored here and nothing is run: the route reads the connections, the
projects' picks, the licence and the Tares Cloud app links, and no connection test is made.

The response:

  {title, exit_label, edition, licence, progress, steps}

- `edition` is {key, name, cloud}: "cloud" (Tares Cloud) or "self_hosted". Tares Cloud connects
  Linear and GitHub Issues with an app where the cell has the connect address; a self-hosted cell
  never has an `app` block.
- `licence` is {state, ended_on, text}: "valid", "ended" or "none".
- `progress` is {set, total, text}, text "N of 7 steps set".
- A step is {slot, number, name, why, state, set, line, note, banner, connected_heading,
  connected, available_heading, available, available_empty, footer}. `slot` is the id the
  routes use; `state` is "set" or "not_set". `line` is the step line of the rail.
- A connected integration is {id, title, description, tags, default, built_in, via, who, picked_by,
  follows, use, actions, blocked, confirm, form, last_test}. `id` is the integration's name in the
  routes. `tags` are {key, text}. `actions` are {key, label} with key edit, make_default or remove.
  `confirm` is the question asked before a removal. `last_test` is null: nothing stores a test.
- An integration that could be connected is {id, title, description, enterprise, allowed, button,
  reason, app, form}. `reason` is set when it is not licensed. `app` is the Tares Cloud block, or
  null.
- A form is {mode, title, lead, save_label, save_as_default, test_hint, fields}. A field is {name,
  label, kind, required, requirement, placeholder, help, error, options, rows, value, saved,
  saved_note}; a secret never has a `value`, only `saved`.

A change goes through the routes that exist: PUT /api/factory/integrations/{slot} (with
`default: false` to keep the default), PUT .../default, DELETE .../connections/{name} and
POST .../test. A project that picks a connection has to be moved off it first (DELETE
/api/projects/{uid}/integrations/{slot}): `picked_by` names them. Admin only.
"""
from __future__ import annotations

import os
from datetime import date

from fastapi import HTTPException, Request

from .. import licence
from .. import linear as linear_mod
from .. import github_credentials
from ..factory_api import caller
from ..factory_store import PROJECT_SLOTS, FactoryStore
from ..integrations import registry, resolve
from ..integrations.registry import SLOTS

STEPS = {
    "tickets": ("Tickets", "Where the crew's tickets live and where their status is kept."),
    "review": ("Review", "Who reviews each pull request before it can merge. This is the review gate."),
    "ci": ("CI", "Where Tares looks to see whether a pull request's checks pass."),
    "memory": ("Memory", "Where the crew keeps what it learns, so the next ticket starts better informed."),
    "monitoring": ("Monitoring", "Where the traces of crew sessions go, so you can see what each one did."),
    "guardrails": ("Guardrails", "Rules the crew follows, and checks Tares asks before a build, a merge or a release."),
    "runtime": ("Runtime", "What starts each crew member's session."),
}
GUARDRAILS_NOTE = ("Your own rules and the checks asked before a build, a merge or a release are on "
                   "the factory Guardrails page, not here.")
FOOTER = "Step {n} of {total}. Projects pick one of the connected integrations; they never hold a key."
AVAILABLE_EMPTY = "Everything installed for this step is connected."
SAVED_NOTE = "A key is saved. Leave this blank to keep it."
SAVED_PLACEHOLDER = "Saved. Leave blank to keep it."
BUILT_IN = "built_in"

DESCRIPTIONS = {
    ("tickets", "tares"): "Tickets live in Tares.",
    ("tickets", "linear"): "Tickets live in a Linear project. Each project picks its Linear project "
                           "on its own Integrations page.",
    ("tickets", "github"): "Tickets are issues in one repository.",
    ("tickets", "jira"): "Tickets live in a Jira project. Works with Jira Cloud and Jira Data Center.",
    ("review", "tares"): "The built-in reviewer on your crew.",
    ("review", "coderabbit"): "CodeRabbit reviews each pull request.",
    ("review", "qodo"): "Qodo reviews each pull request.",
    ("ci", "local"): "Uses the checks recorded on Tares.",
    ("ci", "github"): "Reads check results from GitHub.",
    ("memory", "tares"): "Memory lives in Tares docs.",
    ("memory", "cognee"): "Memory is kept in Cognee.",
    ("monitoring", "off"): "Nothing is sent.",
    ("monitoring", "rius"): "Traces go to Rius.",
    ("monitoring", "otlp"): "Traces go to any OpenTelemetry backend.",
    ("guardrails", "tares"): "Built-in rules, always on.",
    ("guardrails", "webhook"): "Ask your own policy service before an action.",
    ("runtime", "claude_code"): "Crew members start as Claude Code background sessions.",
}

# The words the schema does not carry: a label, help, placeholder and the line shown for a blank
# required field. Keyed by (slot, integration, field). The schema's own label and help are the
# fallback, so an integration that is not listed here still shows its own.
FIELD_COPY = {
    ("tickets", "linear", "api_key"): {
        "label": "API key",
        "help": "A Linear personal API key. In Linear, open your account, then Security and access, "
                "then Personal API keys.",
        "error": "Enter the API key."},
    ("tickets", "github", "repo"): {
        "placeholder": "owner/name",
        "help": "The repository that holds the issues, as owner/name, for example "
                "northwind/orders-api.",
        "error": "Enter the repository as owner/name, for example northwind/orders-api."},
    ("tickets", "github", "credential"): {
        "help": "The name of a GitHub credential stored on this cell. It needs to write issues. "
                "Left empty, every stored credential is tried."},
    ("tickets", "github", "issue_label"): {
        "help": "The label every issue of the project carries. Left empty, it is tares: followed "
                "by the project's name."},
    ("tickets", "jira", "site"): {
        "label": "Site address", "placeholder": "https://northwind.atlassian.net",
        "help": "For Jira Cloud the address ends in atlassian.net. For Jira Data Center, use the "
                "address you open Jira at.",
        "error": "Enter the site address."},
    ("tickets", "jira", "email"): {
        "label": "Email", "help": "The email of the Jira account that owns the API token.",
        "error": "Enter the email of the Jira account."},
    ("tickets", "jira", "token"): {
        "label": "API token",
        "help": "A Jira API token for that account. Jira Data Center calls it a personal access token.",
        "error": "Enter the API token."},
    ("tickets", "jira", "status"): {
        "label": "Status mapping",
        "help": "Match each Tares stage to the Jira status that means it.",
        "error": "Enter the status for each stage."},
    ("review", "coderabbit", "key"): {
        "label": "API key", "help": "A CodeRabbit API key for this organisation.",
        "error": "Enter the API key."},
    ("review", "qodo", "key"): {
        "label": "API key", "help": "A Qodo API key for this organisation.",
        "error": "Enter the API key."},
    ("ci", "github", "no_ci"): {
        "label": "This repository has no CI",
        "help": "Tick this when a repository has no checks at all, so a pull request is not held "
                "back waiting for results."},
    ("memory", "cognee", "url"): {
        "label": "Address", "placeholder": "https://cognee.northwind.example",
        "help": "Where your Cognee server answers.",
        "error": "Enter the address of your Cognee server."},
    ("memory", "cognee", "key"): {
        "label": "API key", "help": "Needed only when your Cognee server asks for one.",
        "error": "Enter the API key."},
    ("monitoring", "rius", "api_key"): {
        "label": "Rius API key",
        "help": "Sent to Rius as a bearer key. A change reaches sessions that start after you save.",
        "error": "Enter the API key."},
    ("monitoring", "rius", "endpoint"): {
        "label": "Address", "help": "Leave empty to use Rius's own address.",
        "error": "Enter the address."},
    ("monitoring", "otlp", "endpoint"): {
        "label": "Address", "placeholder": "https://collector.northwind.example:4318",
        "help": "An http or https address that accepts OTLP over HTTP.",
        "error": "Enter the address of the OTLP endpoint."},
    ("monitoring", "otlp", "api_key"): {
        "label": "API key", "help": "Sent as a bearer key.", "error": "Enter the API key."},
    ("guardrails", "webhook", "url"): {
        "label": "Address", "placeholder": "https://policy.northwind.example/check",
        "help": "Where your policy service answers.",
        "error": "Enter the address of your policy service."},
    ("guardrails", "webhook", "secret"): {
        "label": "Signing secret",
        "help": "Used to sign each request, so your service can tell it came from this cell.",
        "error": "Enter the signing secret."},
}

# The rows of a field that maps Tares's stages to another system's, by the keys the field lists.
STAGE_LABELS = {"todo": "To do", "in_progress": "In progress", "in_review": "In review",
                "done": "Done"}

# Tares Cloud connects these two with an app instead of a key. The connect address comes from the
# environment (the control plane sets it), read when asked, so a cell without one has no app.
APPS = {
    ("tickets", "linear"): {
        "env": "TARES_LINEAR_CONNECT_URL", "service": "Linear", "app_name": "GlassFlow Linear app",
        "button": "Connect Linear", "repo_step": False,
        "steps": ["Linear opens in a new window.", "Sign in and choose your workspace.",
                  "Approve the GlassFlow Linear app. You come back to this page."],
        "note": "Tares keeps you signed in and renews the access for you, so there is no key to "
                "create. Disconnect on the Tickets step takes the access away in Linear again.",
        "use_key_label": "Use an API key instead",
        "use_app_label": "Connect with the Linear app instead", "key_tag": "API key"},
    ("tickets", "github"): {
        "env": "TARES_GITHUB_CONNECT_URL", "service": "GitHub", "app_name": "GlassFlow GitHub App",
        "button": "Connect GitHub", "repo_step": True,
        "steps": ["GitHub opens in a new window.",
                  "Choose your account or organisation and the repositories the crew may use.",
                  "Approve the GlassFlow GitHub App. You come back here to choose the repository."],
        "note": "Tares reads and writes issues through the app, so there is no credential to "
                "create. Disconnect on the Tickets step takes the access away in GitHub again.",
        "use_key_label": "Use a GitHub credential instead",
        "use_app_label": "Connect with the GitHub App instead", "key_tag": "Credential"},
}


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _day(iso) -> str:
    try:
        d = date.fromisoformat(str(iso)[:10])
    except ValueError:
        return ""
    return f"{d.day} {d:%B}"


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def register(app, store, h, ctx):
    def safe(fn, default):
        try:
            return fn()
        except Exception:
            return default

    # ── the integrations of a slot ───────────────────────────────────────────
    def title_of(slot: str, name: str) -> str:
        cls = registry.get(slot, name)
        return (cls.title or name) if cls else name

    def default_of(slot: str) -> str:
        if slot == "monitoring":   # its one connection lives in the tracing settings
            rows = store.list_connections(slot)
            return rows[0]["name"] if rows else resolve.DEFAULTS[slot]
        row = store.get_choice(slot)
        return row["name"] if row else resolve.DEFAULTS[slot]

    def connected_names(slot: str, default: str, rows: dict) -> list[str]:
        """The integrations connected for the slot: installed ones in install order, then any
        stored connection or default whose integration is no longer installed. A slot that keeps
        one connection lists only that one."""
        if slot in FactoryStore.ONE_CONNECTION:
            return [default]
        names = [c.name for c in registry.available(slot) if store.is_connected(slot, c.name)]
        for n in [*rows, default]:
            if n not in names:
                names.append(n)
        return names

    # ── who picks what ───────────────────────────────────────────────────────
    def project_picks(slot: str) -> tuple[dict[str, list[dict]], int]:
        """({integration name: [{uid, name}]}, how many projects follow the default). Private
        projects the caller is not on are left out, as everywhere else."""
        picks: dict[str, list[dict]] = {}
        follow = 0
        if slot not in PROJECT_SLOTS:
            return picks, follow
        hidden = h.hidden_ids()
        for p in safe(store.list_projects, []) or []:
            if p["id"] in hidden:
                continue
            row = safe(lambda: store.get_project_choice(p["id"], slot), None)
            if row:
                picks.setdefault(row["name"], []).append({"uid": p["id"], "name": p.get("name") or p["id"]})
            elif p.get("kind") == "software_factory":
                follow += 1
        return picks, follow

    # ── Tares Cloud apps ─────────────────────────────────────────────────────
    def cloud() -> bool:
        """Tares Cloud: read when asked, as the connect addresses are."""
        return licence.hosted()

    def app_url(slot: str, name: str) -> str:
        spec = APPS.get((slot, name))
        return (os.getenv(spec["env"], "").strip() if spec and cloud() else "")

    def linear_state() -> dict:
        pc = safe(lambda: linear_mod.public_connection(store), {}) or {}
        acct = pc.get("account") or {}
        who = acct.get("name") or acct.get("email") or ""
        if who and acct.get("org"):
            who += f", workspace {acct['org']}"
        return {"connected": bool(pc.get("connected")), "kind": pc.get("kind"),
                "who": f"as {who}" if who else ""}

    def github_apps() -> list[dict]:
        return [c for c in safe(store.list_github_credentials, []) or []
                if github_credentials.is_app(c)]

    def github_who(cred: dict) -> str:
        """Where the app is installed, from what the cell stored: no call to GitHub is made by a
        read, so the number of repositories is not given."""
        acct = str(cred.get("account") or (cred.get("config") or {}).get("account") or "")
        if not acct:
            return ""
        kind = str((cred.get("config") or {}).get("account_type") or "").lower()
        what = {"organization": " organisation", "organisation": " organisation",
                "user": " account"}.get(kind, "")
        return f"on the {acct}{what}" if what else f"on {acct}"

    def via_of(slot: str, name: str, row: dict | None) -> tuple[str | None, str]:
        """("app" | "key" | None, who) for a connected integration on a cell that has the app."""
        if not app_url(slot, name):
            return None, ""
        if name == "linear":
            ls = linear_state()
            kept = bool((((row or {}).get("secrets") or {}).get("api_key") or {}).get("has_value"))
            if ls["connected"] and ls["kind"] == "cloud" and not kept:
                return "app", ls["who"]
            return "key", ""
        apps = github_apps()
        named = str(((row or {}).get("settings") or {}).get("credential") or "").strip()
        if named:
            cred = next((c for c in apps if c["name"] == named), None)
            return ("app", github_who(cred)) if cred else ("key", "")
        return ("app", github_who(apps[0])) if apps else ("key", "")

    def app_block(slot: str, name: str) -> dict | None:
        """The block a connect form shows on Tares Cloud, or None where the cell has no app."""
        url = app_url(slot, name)
        if not url:
            return None
        spec = APPS[(slot, name)]
        block = {"url": url, **{k: spec[k] for k in ("service", "app_name", "button", "repo_step",
                                                     "steps", "note", "use_key_label",
                                                     "use_app_label")},
                 "stage": "start", "done": None}
        if name == "linear":
            ls = linear_state()
            if ls["connected"] and ls["kind"] == "cloud":
                block["stage"] = "done"
                block["done"] = (f"Linear is connected through the {spec['app_name']}"
                                 + (f" {ls['who']}." if ls["who"] else "."))
        else:
            apps = github_apps()
            if apps:
                who = github_who(apps[0])
                block["stage"] = "done"
                block["done"] = (f"GitHub is connected. The {spec['app_name']} is installed"
                                 + (f" {who}." if who else "."))
        return block

    # ── forms ────────────────────────────────────────────────────────────────
    def field_of(slot: str, name: str, f, settings: dict, secrets: dict, title: str) -> dict:
        copy = FIELD_COPY.get((slot, name, f.name), {})
        label = copy.get("label") or f.label
        out = {"name": f.name, "label": label, "kind": f.kind, "required": bool(f.required),
               "requirement": "Required" if f.required else "Optional",
               "placeholder": copy.get("placeholder", ""),
               "help": copy.get("help") or f.help,
               "error": "" if f.kind == "bool" else copy.get("error") or f"Enter {label.lower()}.",
               "options": list(f.options) if f.kind == "select" else [], "rows": [],
               "value": None, "saved": False, "saved_note": ""}
        value = settings.get(f.name)
        if f.kind == "secret":
            out["saved"] = bool((secrets.get(f.name) or {}).get("has_value"))
            if out["saved"]:
                out["placeholder"] = SAVED_PLACEHOLDER
                out["saved_note"] = SAVED_NOTE
            del out["value"]      # a secret never travels
        elif f.kind == "bool":
            out["value"] = bool(value)
        elif f.kind == "mapping":
            have = value if isinstance(value, dict) else {}
            out["rows"] = [{"key": k, "label": STAGE_LABELS.get(k, k.replace("_", " ").capitalize()),
                            "aria_label": f"{title} status for "
                                          f"{STAGE_LABELS.get(k, k.replace('_', ' '))}",
                            "value": str(have.get(k) or "")} for k in f.options]
        else:
            out["value"] = str(value) if value not in (None, False) else ""
        return out

    def form_of(slot: str, name: str, mode: str, row: dict | None, others: list[str],
                default: str, chosen: bool = True) -> dict:
        """`chosen` is False while the built-in default is only implied (nothing saved at the
        factory): the new connection then becomes the default, as the first one."""
        cls = registry.get(slot, name)
        title = title_of(slot, name)
        settings = (row or {}).get("settings") or {}
        secrets = (row or {}).get("secrets") or {}
        fields = [field_of(slot, name, f, settings, secrets, title)
                  for f in (cls.settings_schema if cls else [])]
        desc = DESCRIPTIONS.get((slot, name)) or ""
        if mode == "edit":
            after = "Projects that pick it use the new settings from the next time they read them."
            as_default = False
        elif slot in FactoryStore.ONE_CONNECTION:
            after = f"It replaces {title_of(slot, default)}." if default and default != name \
                else "It becomes the default."
            as_default = True
        elif others and chosen:
            after = (f"It is added next to {_join([title_of(slot, n) for n in others])}. "
                     f"The default stays {title_of(slot, default)}.")
            as_default = False
        else:
            after = "It becomes the default."
            as_default = True
        return {"mode": mode, "title": ("Connect " if mode == "connect" else "Edit ") + title,
                "lead": f"{desc} {after}".strip(),
                "save_label": "Save and connect" if mode == "connect" else "Save",
                "save_as_default": as_default,
                "test_hint": f"Test connection checks the settings above with {title}. "
                             "Nothing is saved by it.",
                "fields": fields}

    # ── one step ─────────────────────────────────────────────────────────────
    def step_of(slot: str, number: int, lic: dict, allowed: bool) -> dict:
        name, why = STEPS[slot]
        default = default_of(slot)
        rows = {c["name"]: c for c in store.list_connections(slot)}
        names = connected_names(slot, default, rows)
        picks, follow = project_picks(slot)
        pairs = ([p for p in store.list_guardrail_pairs() if p["integration"]]
                 if slot == "guardrails" else [])
        project_slot = slot in PROJECT_SLOTS
        one = slot in FactoryStore.ONE_CONNECTION
        default_title = title_of(slot, default)
        chosen = store.get_choice(slot) is not None or any(
            not (registry.get(slot, n) and not registry.get(slot, n).settings_schema) for n in names)

        connected = []
        for n in names:
            cls = registry.get(slot, n)
            title = title_of(slot, n)
            row = rows.get(n)
            built_in = bool(cls) and not cls.settings_schema
            is_default = n == default
            by = picks.get(n, [])
            via, who = via_of(slot, n, row)
            ee = bool(cls and cls.ee)
            desc = DESCRIPTIONS.get((slot, n)) or ("" if cls else
                                                   f"{title} is not installed on this cell.")
            if via == "app":
                desc = f"{title} is connected through the {APPS[(slot, n)]['app_name']}" \
                       + (f" {who}." if who else ".")
            # what uses it
            if project_slot:
                use = (f"Picked by {', '.join(p['name'] for p in by)}." if by
                       else "No project picks it.")
                if is_default:
                    use += f" Every project that picks none ({follow}) follows it."
            elif slot == "guardrails":
                mine = [p for p in pairs if p["integration"] == n]
                use = (f"{len(mine)} {_plural(len(mine), 'check uses', 'checks use')} it."
                       if mine else "No check uses it.")
            else:
                use = "Every project uses it." if is_default else ""
            if via == "app" and is_default:
                use += " To disconnect it, make another integration the default first."
            tags = []
            if is_default:
                tags.append({"key": "default", "text": "Default"})
            if built_in:
                tags.append({"key": BUILT_IN, "text": "Built in"})
            if ee and lic["state"] == "ended":
                tags.append({"key": "licence_ended", "text": "Licence ended"})
            elif ee and not allowed:
                tags.append({"key": "not_licensed", "text": "Not licensed"})
            if via == "app":
                tags.append({"key": "app", "text": "Connected with the app"})
            elif via == "key":
                tags.append({"key": "key", "text": APPS[(slot, n)]["key_tag"]})
            actions, blocked = [], None
            if cls and cls.settings_schema and via != "app":
                actions.append({"key": "edit", "label": "Edit settings"})
            if not is_default and not one:
                actions.append({"key": "make_default", "label": "Make default"})
            if not is_default and not built_in and not one:
                if slot == "guardrails" and any(p["integration"] == n for p in pairs):
                    blocked = {"key": "remove",
                               "text": "Remove its checks on the Guardrails page first."}
                else:
                    actions.append({"key": "remove", "label": "Disconnect" if via == "app" else "Remove"})
            confirm = None
            if any(a["key"] == "remove" for a in actions):
                gone = (f"Tares takes the access away in {APPS[(slot, n)]['service']} and deletes the "
                        f"settings of {title}." if via == "app"
                        else f"The saved settings of {title} are deleted.")
                text = gone
                if by:
                    text = (f"{len(by)} {_plural(len(by), 'project picks', 'projects pick')} {title}: "
                            f"{', '.join(p['name'] for p in by)}. They move to the default, "
                            f"{default_title}. {gone}")
                verb = "Disconnect" if via == "app" else "Remove"
                confirm = {"title": f"{verb} {title}", "text": text, "yes": f"{verb} it",
                           "no": "Keep it", "moves_projects": bool(by)}
            others = [x for x in names if x != n]
            connected.append({
                "id": n, "title": title, "description": desc, "tags": tags,
                "default": is_default, "built_in": built_in, "via": via, "who": who,
                "picked_by": by, "follows": follow if is_default and project_slot else 0,
                "use": use, "actions": actions, "blocked": blocked, "confirm": confirm,
                "form": (form_of(slot, n, "edit", row, others, default)
                         if any(a["key"] == "edit" for a in actions) else None),
                "last_test": None})

        # what is installed and not connected
        available = []
        for cls in registry.available(slot):
            if cls.name in names:
                continue
            ee = bool(cls.ee)
            ok = (not ee) or allowed
            reason = None
            if not ok:
                why_not = (licence.CLOUD_WORDS + " " if cloud() else
                           "Your licence ended on " + lic["ended_on"] + ". " if lic["state"] == "ended"
                           and lic["ended_on"] else "This cell has no licence. ")
                reason = (f"{why_not}Without it, {title_of(slot, cls.name)} cannot be connected. "
                          "Your crew keeps using the built-in choice for this step.")
            block = app_block(slot, cls.name) if ok else None
            available.append({
                "id": cls.name, "title": title_of(slot, cls.name),
                "description": DESCRIPTIONS.get((slot, cls.name)) or "",
                "enterprise": ee, "allowed": ok,
                "button": block["button"] if block else "Connect",
                "reason": reason, "app": block,
                "form": form_of(slot, cls.name, "connect", None, names, default, chosen) if ok else None})

        banner = None
        if slot == "tickets":
            eff = resolve.effective(slot, None, store=store)
            kept = store.get_choice(slot)
            if eff.reason == resolve.NEEDS_LICENCE and kept and not cloud():
                lead = (f"Your licence ended on {lic['ended_on']}." if lic["state"] == "ended"
                        and lic["ended_on"] else "This cell has no licence.")
                kept_title = title_of(slot, kept["name"])
                banner = {"lead": lead,
                          "text": f"{lead} {kept_title} is still connected, but the crew now keeps "
                                  f"its tickets in Tares until you renew. Your {kept_title} "
                                  "settings are kept."}
        # "set" means someone chose something here: a saved default, a connection that is not
        # built in, or more than one. Nothing stores that a person looked at a step and kept the
        # built-in choice, so that case reads "not set yet" until they connect something.
        set_ = (slot == "runtime" or store.get_choice(slot) is not None or len(names) > 1
                or any(not c["built_in"] for c in connected))
        line = default_title + (f" and {len(names) - 1} more" if len(names) > 1 else "")
        last = number == len(STEPS)
        nxt = "Finish setup" if last else "Next: " + STEPS[SLOTS[number]][0]
        return {
            "slot": slot, "number": number, "name": name, "why": why,
            "state": "set" if set_ else "not_set", "set": set_,
            "line": line + ("" if set_ else " (not set yet)"),
            "note": GUARDRAILS_NOTE if slot == "guardrails" else None,
            "banner": banner,
            "connected_heading": f"Connected ({len(connected)})",
            "connected": connected,
            "available_heading": "Connect another", "available": available,
            "available_empty": AVAILABLE_EMPTY if not available else None,
            "footer": {"hint": FOOTER.format(n=number, total=len(STEPS)), "next": nxt}}

    # ── the page ─────────────────────────────────────────────────────────────
    @app.get("/api/factory/setup")
    def get_setup(request: Request):
        if caller(request)["station"]:
            raise HTTPException(status_code=403,
                                detail="a crew station reads integrations through the "
                                       "get_integrations tool")
        h.require_admin(request)
        st = licence.current(store)         # ee_allowed() is False until the first check
        allowed = licence.ee_allowed()
        ended = bool(st.get("valid") and st.get("expired"))
        state = "ended" if ended else "valid" if st.get("valid") else "none"
        lic = {"state": state, "ended_on": _day(st.get("expires")) if ended else ""}
        steps = [step_of(slot, i + 1, lic, allowed) for i, slot in enumerate(STEPS)]
        done = sum(1 for s in steps if s["set"])
        return {
            "title": "Set up your factory", "exit_label": "Exit setup",
            "edition": {"key": "cloud" if cloud() else "self_hosted",
                        "name": "Tares Cloud" if cloud() else "Self-hosted", "cloud": cloud()},
            "licence": {"state": state, "ended_on": lic["ended_on"] or None,
                        "text": ("" if cloud() else
                                 "Your licence ended on " + lic["ended_on"] + "." if ended and lic["ended_on"]
                                 else "This cell has no licence." if state == "none" else "")},
            "progress": {"set": done, "total": len(steps), "text": f"{done} of {len(steps)} steps set"},
            "steps": steps}
