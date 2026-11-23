Fixed
Claude Code sessions connected to a Tares cell lost the Tares tools now and then, with "connection closed" on a plain read. The MCP endpoint kept a session for every client and never let one go, so its memory grew until the container was killed. It now keeps none.
