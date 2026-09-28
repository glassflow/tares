"""What a run produced (TR-220): a short list of results the runs table renders by kind.

Results are read off evidence Tares already holds, not taken from the agent's word: the tool calls
the run made (a pull request opened through a GitHub MCP server, a commit pushed, a Slack message
posted), the runner's own Slack post, and a write-back that was delivered. An agent cannot claim
work it did not do. The one exception is `custom`, for outcomes Tares cannot observe, which the
agent reports through `conclude(produced=[...])`.

A result is {kind, label, url?} with kind from a closed list the console knows how to render.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlparse

KINDS = ("pr", "commit", "slack", "email", "webhook", "custom")
MAX_RESULTS = 20

_PR_URL = re.compile(r"https://github\.com/[^\s\"'<>]+?/pull/(\d+)")
_COMMIT_URL = re.compile(r"https://github\.com/[^\s\"'<>]+?/commit/([0-9a-f]{7,40})")
_SHA = re.compile(r"\"sha\"\s*:\s*\"([0-9a-f]{40})\"")
_SLACK_LINK = re.compile(r"https://[a-z0-9-]+\.slack\.com/archives/[^\s\"'<>]+")


def _is_error(out: str) -> bool:
    head = (out or "").lstrip()[:40].lower()
    return head.startswith("tool error") or head.startswith("error")


def _tool(name: str) -> str:
    """The server's own tool name: an MCP tool reaches the agent as `<server>__<tool>`."""
    return name.split("__", 1)[-1].lower()


def from_tool_call(name: str, args: dict | None, out: str) -> dict | None:
    """One result for a successful tool call that produced something, else None."""
    if not out or _is_error(out):
        return None
    args = args or {}
    tool, full = _tool(name), name.lower()
    if "pull_request" in tool and tool.startswith("create"):
        m = _PR_URL.search(out)
        if m:
            title = str(args.get("title") or "").strip()
            label = f"PR #{m.group(1)}" + (f" {title[:60]}" if title else "")
            return {"kind": "pr", "label": label, "url": m.group(0)}
        return None
    if tool in ("push_files", "create_or_update_file") or tool.startswith("create_commit"):
        m = _COMMIT_URL.search(out)
        if m:
            return {"kind": "commit", "label": f"commit {m.group(1)[:7]}", "url": m.group(0)}
        m = _SHA.search(out)
        if m:
            return {"kind": "commit", "label": f"commit {m.group(1)[:7]}"}
        return None
    if ("slack" in full and any(w in tool for w in ("post", "send", "reply"))) or \
            tool in ("chat_postmessage", "post_message"):
        channel = str(args.get("channel") or args.get("channel_id") or "").strip()
        link = _SLACK_LINK.search(out)
        return {"kind": "slack", "label": f"Slack {channel}".strip(),
                **({"url": link.group(0)} if link else {})}
    return None


def webhook_result(url: str) -> dict:
    """A delivered write-back. The host only: the path can carry a token."""
    host = urlparse(url).netloc or "webhook"
    return {"kind": "webhook", "label": f"write-back to {host}"}


def custom_results(produced) -> list:
    if isinstance(produced, str):
        produced = [produced]
    return [{"kind": "custom", "label": str(p).strip()[:200]}
            for p in (produced or []) if str(p).strip()]


def merge(results: list) -> list:
    """Drop duplicates (the same PR opened twice reads once), keep order, cap the list."""
    seen, out = set(), []
    for r in results:
        if not r or r.get("kind") not in KINDS:
            continue
        k = (r["kind"], r.get("url") or r.get("label"))
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out[:MAX_RESULTS]


def dumps(results: list) -> str:
    return json.dumps(merge(results))
