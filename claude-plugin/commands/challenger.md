---
description: Make this a challenger session (a second model, Codex or Grok, challenges the plan and every commit; Tares keeps the record). "off" turns it off.
argument-hint: "[off]"
---

The user invoked /tares:challenger with: $ARGUMENTS

If the argument is `off`: call the `set_session_flow` tool of the tares MCP server with `flow` set to an empty string, then tell the user the challenger is off for this session.

Otherwise: call the `set_session_flow` tool of the tares MCP server with `flow` = `challenger`. Then tell the user, in two sentences, that this is now a challenger session: the challenger (Codex, or Grok when the plugin's `challenger_backend` option is `grok`; each review names which) will challenge the plan when you leave plan mode and every commit you make, and Tares records the exchange and writes a session summary with memory proposals when the session ends.

Do nothing else. Do not run the challenger yourself; the plugin's hooks run it.
