# Compatibility shim (TR-1131); it has no shebang on purpose. A session opened before the plugin moved to
# `tares hook` (TR-1103) still runs `bash .../hooks/session-start.sh`. Remove it one release after the
# switch (TR-975).
tares hook session-start "$@" || [ $? -ne 2 ]
