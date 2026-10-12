#!/usr/bin/env bash
# Checks crew settings and grants with Tares connected, against a local stub of the Tares routes:
# the first crew up asks and saves on Tares, a second machine asks nothing and writes the same settings.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
F="$here/../plugin/bin/factory"
T=$(mktemp -d)
export FAKE_CLAUDE_STATE="$T/claude" CLAUDE_BIN="$here/fake-claude" FACTORY_PAUSE=0
export TARES_AUTH_TOKEN=stub-token TARES_PLUGIN_DIR="$T/tares-plugin"
chmod +x "$CLAUDE_BIN"
mkdir -p "$T/stub" "$TARES_PLUGIN_DIR"
STUB_TOKEN=stub-token python3 "$here/tares-stub.py" "$T/stub" &
stub=$!
trap 'kill $stub 2>/dev/null; rm -rf "$T"' EXIT
for _ in $(seq 50); do [ -s "$T/stub/port" ] && break; sleep 0.1; done
TARES_URL="http://127.0.0.1:$(cat "$T/stub/port")"; export TARES_URL
fail=0
check() { if eval "$2"; then :; else echo "FAIL $1"; fail=1; fi; }
allow_has() { jq -e --arg s "$2" 'any(.autoMode.allow[]; contains($s))' "$FACTORY_CREW_HOME/stations/$1.settings.json" >/dev/null; }
reqs() { jq -c "select($1)" "$T/stub/requests.jsonl"; }

machine() { # name → sets HOME and FACTORY_CREW_HOME for one "machine"
  export HOME="$T/$1/home" FACTORY_CREW_HOME="$T/$1/crew"; mkdir -p "$HOME"
}

machine one
printf 'L4-ship\n\n\nyes\n2\nthe reviewer may comment on draft PRs\n\n' | "$F" crew up >"$T/up1.out" 2>&1
rc=$?
check "first crew up exits 0" '[ $rc -eq 0 ] && grep -q "started crew-releaser" "$T/up1.out"'
check "settings saved on Tares" '[ -n "$(reqs ".method == \"PUT\" and .path == \"/api/crew/settings\" and .body.autonomy == \"L4-ship\" and .body.challenger == true and .body.builders_max == 2")" ]'
check "grant saved on Tares" '[ -n "$(reqs ".method == \"POST\" and .path == \"/api/grants\" and .body.words == \"the reviewer may comment on draft PRs\" and .body.scope == \"all\"")" ]'
check "every call carries the token" '[ -z "$(reqs ".path != \"/health\" and .auth != \"Bearer stub-token\"")" ]'
check "settings not kept locally" '[ "$(jq -c .settings.autonomy "$FACTORY_CREW_HOME/crew.json")" = null ]'
check "no local desk files with Tares" '[ ! -f "$FACTORY_CREW_HOME/DESK.md" ] && [ ! -f "$FACTORY_CREW_HOME/GRANTS.md" ]'
check "crew desk points at Tares" 'grep -q "The desk is on Tares" <<<"$("$F" crew desk 2>&1)"'
check "FACTORY_TARES on" '[ "$(jq -r .env.FACTORY_TARES "$FACTORY_CREW_HOME/stations/crew-reviewer.settings.json")" = on ]'
check "stations load the Tares plugin" '[ "$(jq -r "select(.args[0] == \"--bg\") | .args | index(\"$TARES_PLUGIN_DIR\") != null" "$FAKE_CLAUDE_STATE/calls.jsonl" | sort -u)" = true ]'
today=$(date -u +%Y-%m-%d)
check "grant from Tares quoted with its date" 'allow_has crew-reviewer "granted this on $today for all projects, in their own words: \"the reviewer may comment on draft PRs\". Acting"'
check "no undated or doubly quoted grants" '! jq -e "any(.autoMode.allow[]; contains(\"undated\") or contains(\"\\\"\\\"\"))" "$FACTORY_CREW_HOME/stations/crew-reviewer.settings.json" >/dev/null'
check "stations reach the same Tares" '[ "$(jq -r .env.TARESD_URL "$FACTORY_CREW_HOME/stations/crew-reviewer.settings.json")" = "$TARES_URL" ]'
check "no token in settings files" '! grep -rq stub-token "$FACTORY_CREW_HOME/stations"'
one_home=$FACTORY_CREW_HOME

# A second machine: nothing is asked again and the station settings come out the same.
machine two
"$F" crew up </dev/null >"$T/up2.out" 2>&1
check "second machine asks nothing" '! grep -q "recommended:" "$T/up2.out" && grep -q "started crew-releaser" "$T/up2.out"'
for r in orchestrator reviewer releaser; do
  check "same settings for crew-$r" 'diff <(sed "s|$one_home|CREW|g" "$one_home/stations/crew-$r.settings.json") <(sed "s|$FACTORY_CREW_HOME|CREW|g" "$FACTORY_CREW_HOME/stations/crew-$r.settings.json") >/dev/null'
done

# A builder gets its project's grants as well.
repo="$T/links"; mkdir -p "$repo"; git -C "$repo" init -q
"$F" spawn builder "Links App" m1 "$repo" "work T1" >/dev/null 2>&1
check "project grants asked for" '[ -n "$(reqs ".path == \"/api/grants?project=Links%20App\"")" ]'
check "project grant quoted for the builder" 'allow_has links-app-build-m1 "granted this on 2026-10-01 for project Links App, in their own words: \"builders may bump patch versions of dependencies\". Acting"'
check "L4: builder may merge on the gate" 'jq -e ".autoMode.allow | any(test(\"pre-authorized at level L4-ship\"))" "$FACTORY_CREW_HOME/stations/links-app-build-m1.settings.json" >/dev/null'

# A project handed over at a stricter level keeps it; a looser one is refused.
"$F" spawn builder --autonomy L3-review shop m1 "$repo" "work T1" >/dev/null 2>&1
check "unknown project falls back to the grants for all" '[ -n "$(reqs ".path == \"/api/grants?project=shop\"")" ] && allow_has shop-build-m1 "in their own words: \"the reviewer may comment on draft PRs\""'
check "stricter project level for its builder" 'jq -e ".autoMode.allow | any(test(\"relayed .*yes\"))" "$FACTORY_CREW_HOME/stations/shop-build-m1.settings.json" >/dev/null'
"$F" retire shop-build-m1 >/dev/null 2>&1
"$F" spawn builder --autonomy L5-dark shop m2 "$repo" "work T2" >"$T/loose.out" 2>&1
check "looser project level refused" 'grep -q "only be stricter" "$T/loose.out" && [ ! -f "$FACTORY_CREW_HOME/stations/shop-build-m2.settings.json" ]'

# The orchestrator runs `factory spawn` with its settings env, not the human's shell: it still finds
# the crew home and gives the builder the Tares plugin.
orch_env=(HOME="$HOME" PATH="$PATH" CLAUDE_BIN="$CLAUDE_BIN" FAKE_CLAUDE_STATE="$FAKE_CLAUDE_STATE" FACTORY_PAUSE=0)
while IFS= read -r kv; do orch_env+=("$kv"); done < <(jq -r '.env | to_entries[] | "\(.key)=\(.value)"' "$FACTORY_CREW_HOME/stations/crew-orchestrator.settings.json")
env -i "${orch_env[@]}" TARES_AUTH_TOKEN=stub-token "$F" spawn builder "Links App" m3 "$repo" "work T3" >"$T/orch.out" 2>&1
check "orchestrator's own spawn finds the crew" 'grep -q "started links-app-build-m3" "$T/orch.out"'
check "its builder loads the Tares plugin" '[ "$(jq -r "select(.args[2] == \"links-app-build-m3\") | .args | index(\"$TARES_PLUGIN_DIR\") != null" "$FAKE_CLAUDE_STATE/calls.jsonl")" = true ]'
"$F" retire links-app-build-m3 >/dev/null 2>&1
env -i "${orch_env[@]}" "$F" spawn builder "Links App" m4 "$repo" "work T4" >"$T/notoken.out" 2>&1
check "unreadable crew settings stop a spawn plainly" 'grep -q "could not read the crew settings" "$T/notoken.out" && ! grep -q "integer expression" "$T/notoken.out" && [ ! -f "$FACTORY_CREW_HOME/stations/links-app-build-m4.settings.json" ]'

# TARESD_URL, the Tares plugin's own name for the URL, also connects the CLI.
TARESD_URL=$TARES_URL TARES_URL= "$F" settings crew-reviewer >"$T/taresd.json" 2>/dev/null
check "TARESD_URL connects" '[ "$(jq -r .env.FACTORY_TARES "$T/taresd.json")" = on ]'

# With Tares on, stages are Tares's own: an odd local ledger is not checked.
mkdir -p "$T/odd/.factory"; echo '{"tickets": [{"id": "T1", "stage": "parked"}]}' > "$T/odd/.factory/ledger.json"
jq -n --arg r "$T/odd" '{odd: {repo: $r, repos: [$r]}}' > "$FACTORY_CREW_HOME/projects.json"
"$F" projects >"$T/odd.out" 2>&1
check "with Tares on, a local ledger's stages are not judged" 'grep -q "on Tares" "$T/odd.out" && ! grep -q "counted as open" "$T/odd.out"'

# Tares down: the CLI says so and falls back to the local files.
kill $stub 2>/dev/null; wait $stub 2>/dev/null
"$F" settings crew-reviewer >"$T/down.json" 2>"$T/down.err"
check "unreachable Tares is reported" 'grep -q "does not answer" "$T/down.err"'
check "falls back to local" '[ "$(jq -r .env.FACTORY_TARES "$T/down.json")" = off ]'

[ $fail = 0 ] && echo "tares: all cases pass"
exit $fail
