#!/usr/bin/env bash
# Checks that factory reads the session id from `claude --bg` output, with and without colour codes.
set -uo pipefail
F="$(cd "$(dirname "$0")/../.." && pwd)/claude-plugin/bin/factory"
cd "$(dirname "$0")/../.."
fail=0
t() { local got; got=$(printf "$1" | "$F" _parse-bg-id); [ "$got" = "$2" ] || { echo "FAIL: expected '$2', got '$got' for: $1"; fail=1; }; }
t 'backgrounded · feb401c6 · tfspike-alpha\n' feb401c6
t 'backgrounded · \033[36m3bd5cd67\033[39m · vetme-build-t1\n\033[2m  claude agents   list sessions\033[22m\n' 3bd5cd67
t 'Workspace not trusted. Run `claude` in /x once\n' ''
[ $fail = 0 ] && echo "parse: all cases pass"
exit $fail
