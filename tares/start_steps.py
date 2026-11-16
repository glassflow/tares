"""The Start checklist (TR-756): the few steps that take a new cell from empty to working.

`factory_steps(ctx)` is the checklist of the software factory home; `sre_steps` (TR-765) is the
one of the AI SRE home. A step is {key, title, gets, done, actions, detail?}: `gets` says what the person gains,
`actions` is a list of buttons (empty once done), each a link inside the console (`to`), a link
out (`href`) or a command to copy (`copy`). Every string is shown as it is, so
tests/test_factory_start.py checks them against tares/words.py.

`ctx` is a StartCtx (or a dict with the same names) made from reads of the store alone, never from
a call a browser made. `checklist` adds the counts and whether the block shows: it hides once
everything is done, and when this person hid it (a setting per person, `start_hidden:<who>`).
"""
from __future__ import annotations

from dataclasses import dataclass, fields

HIDDEN_PREFIX = "start_hidden:"
SETTINGS_LINK = "/settings#tools"
SETUP_LINK = "/setup"
# the page that explains keeping tickets in GitHub Issues
GITHUB_ISSUES_DOCS = "https://docs.glassflow.ai/tares/software-factory/get-started"
INSTALL_COMMAND = "uv tool install tares"
LOGIN_COMMAND = "tares login"
CREW_COMMAND = "tares factory crew up"
SPEC_COMMAND = "tares factory spec"     # the name the command line accepts (tares/crew/dispatch.py PORTED)


@dataclass
class StartCtx:
    choices_made: bool = False          # autonomy, releases and builders are saved on the cell
    github_connected: bool = False      # the cell holds a GitHub credential
    setup_text: str = ""                # "You approve each merge, 2 builders, no releases. GitHub connected as example-org"
    cli_version: str = ""               # the version `tares login` reported
    machine_name: str = ""              # the machine that signed in or runs the crew
    crew_up: bool = False               # the orchestrator and the reviewer have both reported
    crew_text: str = ""                 # "Running on <machine>"


@dataclass
class SreStartCtx:
    model: bool = False             # a model provider is configured (or the trial credit is on)
    signals: bool = False           # a logs or metrics connector exists
    services: bool = False          # the cell sees at least one service
    watching: bool = False          # at least one service is watched
    slack: bool = False             # notes have a Slack channel to land in
    demo: bool = False              # the demo project exists
    demo_finding: bool = False      # the demo project has at least one finding


def _ctx(ctx, cls=StartCtx):
    if isinstance(ctx, cls):
        return ctx
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in dict(ctx or {}).items() if k in known})


def _step(key: str, title: str, gets: str, done: bool, actions: list[dict],
          detail: str | None = None) -> dict:
    out = {"key": key, "title": title, "gets": gets, "done": bool(done),
           "actions": [] if done else actions}
    if detail:
        out["detail"] = detail
    return out


def _where(step: dict, where: str, command: str | None = None) -> dict:
    """Which half of the journey a step belongs to: `workspace` (done in the console) or `terminal`.
    A terminal step keeps its `command` once done, so /setup can always show how to run the crew."""
    step["where"] = where
    if command:
        step["command"] = command
    return step


def factory_steps(ctx) -> list[dict]:
    """The four steps of a factory's first run (TR-1312), in order: Set up your factory, done in the
    console on /setup (how the crew works, GitHub, and the integrations), then three commands in a
    terminal. Each ticks from what the cell knows. The factory is set up once the crew is up; planning
    the first project (SPEC_COMMAND) is what comes next, on the home, not a step of setup: it is a long
    conversation, not something to wait on."""
    c = _ctx(ctx)
    return [
        _where(_step("setup", "Set up your factory",
                     "How your crew works, your GitHub, and where tickets, reviews and checks live.",
                     c.choices_made and c.github_connected,
                     [{"label": "Set up your factory", "to": SETUP_LINK}], c.setup_text), "workspace"),
        _where(_step("install", "Install Tares", "The command that starts and watches the crew.",
                     bool(c.cli_version or c.machine_name or c.crew_up),
                     [{"label": "Copy the command", "copy": INSTALL_COMMAND}],
                     f"Installed: tares {c.cli_version}." if c.cli_version else None), "terminal", INSTALL_COMMAND),
        _where(_step("login", "Sign your machine in", "So the crew on your machine reports here.",
                     bool(c.machine_name or c.crew_up),
                     [{"label": "Copy the command", "copy": LOGIN_COMMAND}],
                     f"Signed in from {c.machine_name}." if c.machine_name else None), "terminal", LOGIN_COMMAND),
        _where(_step("crew", "Start the crew",
                     "It reads your choices from this workspace and asks only what belongs to your machine.",
                     c.crew_up, [{"label": "Copy the command", "copy": CREW_COMMAND}],
                     c.crew_text), "terminal", CREW_COMMAND),
    ]


SRE_SIGNAL_CONNECTORS = ("loki", "prometheus", "prometheus_alerts", "alertmanager", "otlp",
                         "docker_logs", "vercel", "http_poll")


def sre_steps(ctx) -> list[dict]:
    """The steps of the AI SRE home, in order: five, and a sixth once the demo project exists."""
    c = _ctx(ctx, SreStartCtx)
    steps = [
        _step("model", "Add a model provider",
              "The agents that watch and investigate need an AI model.", c.model,
              [{"label": "Add a provider", "to": "/settings/advanced"}]),
        _step("signals", "Connect logs or metrics", "Tares sees how your services behave.",
              c.signals or c.demo,
              [{"label": "Connect", "to": SETTINGS_LINK},
               {"label": "Try the demo service", "do": "start_demo"}]),
        _step("services", "See your services appear", "Each of your services, with its state.",
              c.services, [{"label": "Open services", "to": "/services"}]),
        _step("watching", "Turn on watching",
              "An agent looks at every change and tells you when something breaks.",
              c.watching, [{"label": "Turn on watching", "to": "/settings#watching"}]),
        _step("slack", "Choose the Slack channel for notes",
              "Incident notes land where your team talks.", c.slack,
              [{"label": "Choose a channel", "to": "/settings#notes"}]),
    ]
    if c.demo:
        steps.append(_step("test_incident", "Cause a test incident on the demo",
                           "See a full incident note written in about a minute.", c.demo_finding,
                           [{"label": "Open the demo", "to": "/incidents"}]))
    return steps


def person_key(person: dict | None, credential_name: str | None) -> str:
    """Who a hide belongs to: the person's id, so a rename keeps it and two people of one name
    keep their own; a key with no person falls back to its own name, a cell with nobody to name
    to "console"."""
    return (person or {}).get("id") or credential_name or "console"


def hidden(store, who: str) -> bool:
    return bool(store.get_setting(HIDDEN_PREFIX + who))


def hide(store, who: str) -> None:
    store.set_setting(HIDDEN_PREFIX + who, "1")


def unhide(store, who: str) -> None:
    store.set_setting(HIDDEN_PREFIX + who, None)


def checklist(store, steps: list[dict], who: str) -> dict:
    """The block the home shows: the steps, how many are done, and whether to show it."""
    done = sum(1 for s in steps if s["done"])
    pending = done < len(steps)
    mine = pending and hidden(store, who)   # this person hid it, so the page can offer it back
    return {"steps": steps, "done": done, "total": len(steps), "show": pending and not mine, "hidden": mine}
