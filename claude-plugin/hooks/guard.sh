# Compatibility shim (TR-1131); it has no shebang on purpose. A Claude Code session reads hooks.json when it
# starts, so a session opened before the plugin moved to `tares hook` (TR-1103) still runs
# `bash .../hooks/guard.sh`. Without this file that call fails with exit 127, which Claude Code does not
# treat as a block, and the guard silently stops. Remove it one release after the switch (TR-975).
# An older tares has no `hook` and exits 2, which Claude Code reads as "block this call"; hooks.json maps 2 to 1.
tares hook guard "$@" || [ $? -ne 2 ]
