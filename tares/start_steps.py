"""The Start checklist (TR-756): the few steps that take a new cell from empty to working.

`factory_steps(ctx)` is the checklist of the software factory home; TR-765 adds `sre_steps` beside
it. A step is {key, title, gets, done, actions, detail?}: `gets` says what the person gains,
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
# the page that explains keeping tickets in GitHub Issues
GITHUB_ISSUES_DOCS = "https://docs.glassflow.ai/tares/software-factory/get-started"


@dataclass
class StartCtx:
    cloud: bool = False                 # this cell is a Tares Cloud one
    github_connect_url: str = ""        # cloud: where Connect GitHub goes
    linear_connect_url: str = ""        # cloud: where Connect Linear goes
    key_used: bool = False              # a key (not the root token) has been used
    claude_code_events: bool = False    # the claude_code connector has received events
    github_connected: bool = False      # a GitHub credential is saved
    linear_connected: bool = False
    github_issues_linked: bool = False  # a project keeps its tickets in GitHub Issues
    stations: int = 0                   # crew stations that have reported
    spec_with_tickets: bool = False     # a factory project has tickets


def _ctx(ctx) -> StartCtx:
    if isinstance(ctx, StartCtx):
        return ctx
    known = {f.name for f in fields(StartCtx)}
    return StartCtx(**{k: v for k, v in dict(ctx or {}).items() if k in known})


def _step(key: str, title: str, gets: str, done: bool, actions: list[dict],
          detail: str | None = None) -> dict:
    out = {"key": key, "title": title, "gets": gets, "done": bool(done),
           "actions": [] if done else actions}
    if detail:
        out["detail"] = detail
    return out


def factory_steps(ctx) -> list[dict]:
    """The five steps of the factory home, in order."""
    c = _ctx(ctx)
    connect_github = ({"label": "Connect GitHub", "href": c.github_connect_url}
                      if c.cloud and c.github_connect_url
                      else {"label": "Connect GitHub", "to": SETTINGS_LINK})
    connect_linear = ({"label": "Connect Linear", "href": c.linear_connect_url}
                      if c.cloud and c.linear_connect_url
                      else {"label": "Connect Linear", "to": SETTINGS_LINK})
    return [
        _step("claude_code", "Sign in from Claude Code",
              "Your Claude Code sessions and the crew report here.",
              c.key_used or c.claude_code_events,
              [{"label": "Copy the command", "copy": "tares login" if c.cloud else "tares mcp"}]),
        _step("github", "Connect GitHub",
              "The crew opens pull requests and Tares follows their reviews and checks.",
              c.github_connected, [connect_github]),
        _step("tickets", "Connect Linear or GitHub Issues",
              "Tickets stay where your team already tracks work.",
              c.linear_connected or c.github_issues_linked,
              [connect_linear, {"label": "Use GitHub Issues", "href": GITHUB_ISSUES_DOCS}]),
        _step("crew", "Start the crew",
              "Claude Code sessions that build, review and merge your tickets.",
              c.stations > 0, [{"label": "Copy the command", "copy": "tares factory crew up"}]),
        _step("spec", "Write your first spec",
              "A plan with milestones and tickets the crew can build.",
              c.spec_with_tickets,
              [{"label": "Copy the command",
                "copy": 'tares factory spec "what you want to build"'}]),
    ]


def hidden(store, who: str) -> bool:
    return bool(store.get_setting(HIDDEN_PREFIX + who))


def hide(store, who: str) -> None:
    store.set_setting(HIDDEN_PREFIX + who, "1")


def unhide(store, who: str) -> None:
    store.set_setting(HIDDEN_PREFIX + who, None)


def checklist(store, steps: list[dict], who: str) -> dict:
    """The block the home shows: the steps, how many are done, and whether to show it."""
    done = sum(1 for s in steps if s["done"])
    show = done < len(steps) and not hidden(store, who)
    return {"steps": steps, "done": done, "total": len(steps), "show": show}
