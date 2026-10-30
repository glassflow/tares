#!/usr/bin/env bash
# Checks the code intelligence part of doctor and crew up (TR-682) on a real Tares with a fake claude:
# the languages of each handed-over repo read from its manifests (at the root or one folder down, never
# inside node_modules), whether Claude Code has a language server for each (an enabled plugin and its
# binary on PATH), the exact commands doctor prints for a missing one, and crew up installing them only
# on a yes.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/lib.sh"
T=$(cd "$(mktemp -d)" && pwd -P)
mkdir -p "$T/kit-under-test"; cp -R "$here/../../claude-plugin/." "$T/kit-under-test/"; F="$T/kit-under-test/bin/factory"
export HOME="$T/home" FACTORY_CREW_HOME="$T/crew" FAKE_CLAUDE_STATE="$T/claude" CLAUDE_BIN="$here/fake-claude" FACTORY_PAUSE=0
unset TARES_URL TARES_AUTH_TOKEN TARES_FACTORY_URL TARES_FACTORY_TOKEN_FILE FACTORY_REPO FACTORY_WORKTREES CLAUDE_JOB_DIR TARES_FACTORY_MCP_URL TARES_FACTORY_CRED_FILE TARES_MCP_HOST TARES_MCP_PORT
mkdir -p "$HOME" "$T/bin"
chmod +x "$CLAUDE_BIN"
trap 'stop_tares; rm -rf "$T"' EXIT
fail=0
check() { if ( eval "$2" ); then :; else echo "FAIL $1"; fail=1; fi; }
calls() { grep -c "$1" "$FAKE_CLAUDE_STATE/calls.jsonl" 2>/dev/null; true; }
plugins() { echo "$1" > "$FAKE_CLAUDE_STATE/plugins.json"; }
mkdir -p "$FAKE_CLAUDE_STATE"

# Stand-ins for the installers a yes runs: each makes the binary it would install. The server binaries are
# looked up in $T/bin only, so what this machine has installed never shows.
export FACTORY_LSP_PATH="$T/bin"
for tool in npm go rustup; do
  cat > "$T/bin/$tool" <<'STUB'
#!/usr/bin/env bash
echo "$(basename "$0") $*" >> "$(dirname "$0")/installs.log"
for a in "$@"; do
  case "$a" in
    pyright) touch "$(dirname "$0")/pyright-langserver" ;;
    typescript-language-server) touch "$(dirname "$0")/typescript-language-server" ;;
    golang.org/x/tools/gopls@latest) touch "$(dirname "$0")/gopls" ;;
    rust-analyzer) touch "$(dirname "$0")/rust-analyzer" ;;
  esac
done
chmod +x "$(dirname "$0")"/* 2>/dev/null
STUB
done
chmod +x "$T/bin/npm" "$T/bin/go" "$T/bin/rustup"
export PATH="$T/bin:$PATH"
installs() { cat "$T/bin/installs.log" 2>/dev/null | wc -l | tr -d ' '; }

# Repos: Python at the root with a console one folder down, Go, Rust one folder down, and one whose only
# manifests are in places that do not count (node_modules, .factory, .venv, three folders down).
A="$T/api"; mkdir -p "$A/ui"; touch "$A/pyproject.toml" "$A/ui/package.json" "$A/ui/tsconfig.json"
B="$T/gw"; mkdir -p "$B"; touch "$B/go.mod"
C="$T/docs"; mkdir -p "$C/node_modules" "$C/.factory" "$C/.venv" "$C/a/b/c"; touch "$C/node_modules/package.json" "$C/.factory/Cargo.toml" "$C/.venv/requirements.txt" "$C/a/b/c/go.mod" "$C/README.md"
D="$T/engine"; mkdir -p "$D/native"; touch "$D/native/Cargo.toml"
F2="$T/site"; mkdir -p "$F2"; touch "$F2/package.json"   # JavaScript only: the same server as TypeScript
E="$T/gw2"; mkdir -p "$E"; touch "$E/go.mod"   # a second Go repo: one plugin and one binary install serve both

start_tares "$T/tares"
trust "$FACTORY_CREW_HOME" "$A" "$B" "$C" "$D" "$E" "$F2"
api PUT /api/crew/settings '{"autonomy": "L3-review", "releaser": false, "challenger": false, "builders_max": 2}' >/dev/null
"$F" crew up --yes >"$T/up0.out" 2>&1 || { echo "FAIL crew up"; cat "$T/up0.out"; exit 1; }
hand_over Mix "$A" "$B" "$C" "$D" "$E" "$F2" >/dev/null
plugins '[]'

# Doctor names each language of each repo with its status, and the exact commands for a missing one.
"$F" doctor >"$T/doc1.out" 2>&1
check "doctor names Python and TypeScript for the repo and says neither has a server" \
  'grep -q "Python in $A has no language server" "$T/doc1.out" && grep -q "TypeScript in $A has no language server" "$T/doc1.out"'
check "a package.json alone is JavaScript, and a repo with a tsconfig.json is not also listed as JavaScript" \
  'grep -q "JavaScript in $F2 has no language server" "$T/doc1.out" && ! grep -q "TypeScript in $F2" "$T/doc1.out" && ! grep -q "JavaScript in $A" "$T/doc1.out"'
check "doctor names Go and Rust for the other repos" \
  'grep -q "Go in $B has no language server" "$T/doc1.out" && grep -q "Rust in $D has no language server" "$T/doc1.out"'
check "doctor prints the install command and the plugin that wires it in" \
  'grep -q "npm install -g pyright. then .claude plugin install pyright-lsp@claude-plugins-official" "$T/doc1.out" && grep -q "npm install -g typescript-language-server typescript" "$T/doc1.out" && grep -q "go install golang.org/x/tools/gopls@latest" "$T/doc1.out" && grep -q "rustup component add rust-analyzer" "$T/doc1.out"'
check "doctor leaves out manifests under node_modules, .factory and three folders down" \
  '! grep -q "in $C " "$T/doc1.out"'
check "a missing language server is advice, not a FIX" '! grep -E "FIX.*language server" "$T/doc1.out"'

# Ready needs both an enabled plugin and its binary; either alone is not enough.
touch "$T/bin/pyright-langserver" "$T/bin/typescript-language-server"; chmod +x "$T/bin/"*
plugins '[{"id":"typescript-lsp@claude-plugins-official","enabled":true,"scope":"user"},{"id":"pyright-lsp@claude-plugins-official","enabled":false,"scope":"user"}]'
"$F" doctor >"$T/doc2.out" 2>&1
check "an enabled plugin with its binary is ready" 'grep -q "ok .*TypeScript in $A has a language server for Claude Code (typescript-lsp plugin, typescript-language-server on PATH)" "$T/doc2.out"'
check "a disabled plugin is not a server, and doctor says the binary is the part that is there" \
  'grep -q "Python in $A has no language server.*pyright-langserver is installed, the pyright-lsp plugin is not on" "$T/doc2.out" && grep -q "claude plugin install pyright-lsp@claude-plugins-official" "$T/doc2.out" && ! grep -q "npm install -g pyright" "$T/doc2.out"'
plugins '[{"id":"pyright-lsp@claude-plugins-official","enabled":true,"scope":"local","projectPath":"/somewhere/else"}]'
"$F" doctor >"$T/doc3.out" 2>&1
check "a plugin enabled for another project is not a server here" 'grep -q "Python in $A has no language server" "$T/doc3.out"'
plugins '[{"id":"pyright-lsp@claude-plugins-official","enabled":true,"scope":"local","projectPath":"'"$A"'"}]'
"$F" doctor >"$T/doc3b.out" 2>&1
check "a plugin enabled for the repo path only is not a server for the crew, whose stations run elsewhere" 'grep -q "Python in $A has no language server" "$T/doc3b.out" && grep -q "claude plugin install pyright-lsp@claude-plugins-official" "$T/doc3b.out"'
rm -f "$T/bin/typescript-language-server"
plugins '[{"id":"typescript-lsp@claude-plugins-official","enabled":true,"scope":"user"}]'
"$F" doctor >"$T/doc4.out" 2>&1
check "an enabled plugin without its binary says the binary is missing, and prints only its install" \
  'grep -q "TypeScript in $A has no language server.*the typescript-lsp plugin is on, typescript-language-server is not on PATH" "$T/doc4.out" && ! grep -q "claude plugin install typescript-lsp" "$T/doc4.out"'
FAKE_CLAUDE_PLUGIN_FAIL=1 "$F" doctor >"$T/doc5.out" 2>&1
check "when claude cannot list its plugins doctor says it cannot tell, and recommends no install" \
  'grep -q "TypeScript in $A: cannot tell whether Claude Code has a language server, because claude plugin list --json failed" "$T/doc5.out" && ! grep -q "has no language server" "$T/doc5.out" && ! grep -q "claude plugin install" "$T/doc5.out"'

FAKE_CLAUDE_PLUGIN_FAIL=1 "$F" crew up --yes </dev/null >"$T/up_unk.out" 2>&1
check "when claude cannot list its plugins crew up offers no install" '! grep -q "Code intelligence" "$T/up_unk.out"'

# crew up offers the install; --yes, a refusal and a closed input install nothing.
rm -f "$T/bin/pyright-langserver" "$T/bin/typescript-language-server" "$T/bin/installs.log"; plugins '[]'
"$F" crew up --yes </dev/null >"$T/up1.out" 2>&1
check "crew up --yes recommends and installs nothing" \
  'grep -q "Code intelligence" "$T/up1.out" && grep -q "Python in $A" "$T/up1.out" && grep -q "not installing language servers without a yes" "$T/up1.out" && [ "$(installs)" = 0 ] && [ "$(calls plugin.,.install)" = 0 ]'
"$F" crew up </dev/null >"$T/up2.out" 2>&1
check "a closed input is a no" 'grep -q "no language servers installed" "$T/up2.out" && [ "$(installs)" = 0 ] && [ "$(calls plugin.,.install)" = 0 ]'
echo no | "$F" crew up >"$T/up3.out" 2>&1
check "answering no installs nothing" 'grep -q "no language servers installed" "$T/up3.out" && [ "$(installs)" = 0 ] && [ "$(calls plugin.,.install)" = 0 ]'

# A yes runs the installers and installs each plugin once, then doctor finds every server ready.
echo yes | "$F" crew up >"$T/up4.out" 2>&1
check "a yes installs each binary" 'grep -q "^npm install -g pyright" "$T/bin/installs.log" && grep -q "^npm install -g typescript-language-server typescript" "$T/bin/installs.log" && grep -q "^go install golang.org/x/tools/gopls@latest" "$T/bin/installs.log"'
check "a yes installs each binary once, however many repos need it" '[ "$(grep -c "^go install" "$T/bin/installs.log")" = 1 ] && [ "$(grep -c "^npm install -g typescript-language-server" "$T/bin/installs.log")" = 1 ]'
check "a yes installs each plugin once" \
  'for p in pyright-lsp typescript-lsp gopls-lsp rust-analyzer-lsp; do [ "$(jq "[.[] | select(.id == \"$p@claude-plugins-official\")] | length" "$FAKE_CLAUDE_STATE/plugins.json")" = 1 ] || exit 1; done'
"$F" doctor >"$T/doc6.out" 2>&1
check "doctor finds Python, TypeScript and Go ready after the yes" \
  'grep -q "ok .*Python in $A has a language server" "$T/doc6.out" && grep -q "ok .*TypeScript in $A has a language server" "$T/doc6.out" && grep -q "ok .*Go in $B has a language server" "$T/doc6.out"'
"$F" crew up --yes </dev/null >"$T/up5.out" 2>&1
check "crew up offers nothing for languages that have a server" '! grep -q "Python in" "$T/up5.out" && ! grep -q "Go in $B" "$T/up5.out"'

[ $fail = 0 ] && echo "lsp: all cases pass"
exit $fail
