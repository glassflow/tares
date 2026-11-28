"""What the cell reads from its configuration and the small validators the staying code shares.

The source catalog went in Tares 2.0 (TR-1811): no catalog YAML, no sources, triggers or agents are
declared here any more. What is left: durations, the in-process agent and Slack subscription URLs the
project timeline still reads, the skill validator and the model API base.
"""
from __future__ import annotations

import os
import re

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(s) -> float:
    """'5s' -> 5.0, '15m' -> 900.0, '6h' -> 21600.0."""
    s = str(s).strip()
    return float(s[:-1]) * _UNITS[s[-1]]


# A Tares agent is wired to its trigger through an ordinary subscription whose URL uses this
# scheme; the dispatcher runs it in-process instead of POSTing. Everything downstream (the roster,
# a trigger's woken-agents list, recent firings) then treats it identically to an external agent.
AGENT_URL_PREFIX = "tares://agent/"


def agent_url(name: str) -> str:
    return AGENT_URL_PREFIX + name


def agent_name_from_url(url: str) -> str | None:
    return url[len(AGENT_URL_PREFIX):] if url.startswith(AGENT_URL_PREFIX) else None


# Slack is the third sink, wired the same way: a subscription whose URL names a channel instead of
# an endpoint. The dispatcher posts it via chat.postMessage with the instance's bot token, so the
# channel is addressable without the operator holding a per-channel incoming-webhook URL.
SLACK_URL_PREFIX = "slack://channel/"

# Channel IDs are C/G/D + uppercase alphanumerics. A human-typed `#general` is accepted too —
# chat.postMessage still resolves names — but is normalized to the bare name.
_SLACK_ID = re.compile(r"^[CGD][A-Z0-9]{6,}$")
_SLACK_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")


def slack_url(channel: str) -> str:
    return SLACK_URL_PREFIX + channel.lstrip("#")


def slack_channel_from_url(url: str) -> str | None:
    """The channel a `slack://channel/<id>` subscription addresses, else None. Returns None for a
    malformed channel as well: `fire` must fall through to the webhook path only for URLs that are
    genuinely not Slack, so validation happens at subscribe time (see `validate_slack_channel`)."""
    if not url.startswith(SLACK_URL_PREFIX):
        return None
    chan = url[len(SLACK_URL_PREFIX):].strip().lstrip("#")
    return chan or None


def validate_slack_channel(channel: str) -> str:
    """Normalize and check a channel, raising ValueError with a usable message. Called on the
    subscribe path — a subscription that can never deliver is worse than a 400."""
    chan = (channel or "").strip().lstrip("#")
    if not chan:
        raise ValueError("slack:// subscription needs a channel, e.g. slack://channel/C0123456789")
    if _SLACK_ID.match(chan) or _SLACK_NAME.match(chan):
        return chan
    raise ValueError(
        f"{channel!r} is not a Slack channel; use the channel ID (C0123456789, from the channel's "
        "'Copy link') or its lowercase name")


# A skill: a name, a one-paragraph description and a markdown body, kept per project. Agents read
# them once; the catalog still imports and templates still plan them until the catalog goes.
SKILL_NAME_RE = re.compile(r"^[a-z0-9-]{1,64}$")
SKILL_MAX_DESCRIPTION = 1024
SKILL_MAX_BODY_BYTES = 64 * 1024


class SkillError(ValueError):
    pass


def validate_skill(name, description, body) -> tuple[str, str, str]:
    """The skill's fields, trimmed, or a SkillError saying what is wrong in plain words."""
    name = str(name or "").strip()
    if not SKILL_NAME_RE.match(name):
        raise SkillError("name must be 1 to 64 lowercase letters, digits or dashes, "
                         f"got {name!r}")
    description = str(description or "").strip()
    if not description:
        raise SkillError(f"skill {name!r}: a description is required")
    if len(description) > SKILL_MAX_DESCRIPTION:
        raise SkillError(f"skill {name!r}: the description is {len(description)} characters, "
                         f"the limit is {SKILL_MAX_DESCRIPTION}")
    if re.search(r"\n\s*\n", description):
        raise SkillError(f"skill {name!r}: the description must be one paragraph; put the "
                         "detail in the body")
    body = str(body or "").strip("\n")
    if not body.strip():
        raise SkillError(f"skill {name!r}: the body is empty")
    size = len(body.encode("utf-8"))
    if size > SKILL_MAX_BODY_BYTES:
        raise SkillError(f"skill {name!r}: the body is {size} bytes, the limit is "
                         f"{SKILL_MAX_BODY_BYTES} (64 KB)")
    return name, description, body


# Overridable so the end-to-end test can point at a stub instead of the real API.
API_BASE = os.getenv(
    "ANTHROPIC_BASE_URL",
    os.getenv("TARES_ANTHROPIC_BASE", "https://api.anthropic.com"),
).rstrip("/")
