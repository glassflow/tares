"""A throwaway MCP server for the guard probe (TR-1576): two tools named like Slack's, so the guard's Slack
rules apply to them. The write tool appends a line to PROBE_SIDE_EFFECTS when it really runs; the read tool
only answers. It talks to nothing.

Run over stdio by Codex: `python slack_probe_server.py`.
"""
import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("slack_probe")


@mcp.tool()
def slack_send_message(channel: str, text: str) -> str:
    """Post a message to a channel (probe: only writes a line to a local file)."""
    log = os.environ.get("PROBE_SIDE_EFFECTS")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"mcp slack_send_message {channel} {text}\n")
    return "sent"


@mcp.tool()
def slack_read_channel(channel: str) -> str:
    """Read a channel (probe: a fixed answer)."""
    return f"#{channel}: nothing new"


if __name__ == "__main__":
    mcp.run()
