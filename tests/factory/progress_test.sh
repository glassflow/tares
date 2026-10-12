#!/usr/bin/env bash
# Checks factory projects and the project lines in factory crew status: every project the crew holds
# is listed (also one planned in its own repo rather than under ~/factory/projects), one row per
# project, progress from the ledger or the ticket files, and a rough time left from the pace since
# the hand-over. Uses a fake claude.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
F="$here/../plugin/bin/factory"
T=$(cd "$(mktemp -d)" && pwd -P)
export HOME="$T/home" FACTORY_CREW_HOME="$T/crew" FAKE_CLAUDE_STATE="$T/claude" CLAUDE_BIN="$here/fake-claude" FACTORY_PAUSE=0
unset TARES_URL TARES_AUTH_TOKEN TARES_PLUGIN_DIR FACTORY_REPO
mkdir -p "$HOME" "$FACTORY_CREW_HOME"
chmod +x "$CLAUDE_BIN"
fail=0
check() { if eval "$2"; then :; else echo "FAIL $1"; fail=1; fi; }
iso() { jq -nr --argjson ago "$1" 'now - $ago | floor | todate'; }

# p1: handed over two hours ago, planned in its own repo (not under the crew home); two tickets
# finished since then, two to go, so the pace says about two hours left.
R1="$T/repos/p1"; mkdir -p "$R1/.factory"
jq -n --arg a "$(iso 3600)" --arg b "$(iso 1800)" '{tickets: [
  {id: "T1", milestone: "M1", stage: "merged", updated: $a},
  {id: "T2", milestone: "M1", status: "shipped", updated: $b},
  {id: "T3", milestone: "M2", stage: "doing", updated: $b},
  {id: "T4", milestone: "M2", stage: null, updated: ""}]}' > "$R1/.factory/ledger.json"
jq -n --arg r "$R1" --arg at "$(iso 7200)" '{p1: {repo: $r, repos: [$r], addedAt: $at}}' > "$FACTORY_CREW_HOME/projects.json"

# p2: planned in a project worktree, not handed over; progress comes from its ticket files.
W2="$FACTORY_CREW_HOME/projects/p2/repo2"; mkdir -p "$W2/.factory/tickets"
jq -n '{project: "p2"}' > "$W2/.factory/factory.json"
printf '# T1: a\nmilestone: M1\nstatus: todo\n' > "$W2/.factory/tickets/T1-a.md"
printf '# T2: b\nmilestone: M1\nstatus: merged\n' > "$W2/.factory/tickets/T2-b.md"
# A second worktree of the same project must not add a second row.
mkdir -p "$FACTORY_CREW_HOME/projects/p2/other/.factory" && jq -n '{project: "p2"}' > "$FACTORY_CREW_HOME/projects/p2/other/.factory/factory.json"

# One active builder; a retired one and a helper named like a builder don't count.
jq -n '{"p1-build-m2": {role: "builder", project: "p1", status: "active", id: "00000001"},
  "p1-build-m1": {role: "builder", project: "p1", status: "retired", id: "00000002"},
  "p1-build-m2-fixflake": {role: "helper", project: "p1", status: "active", id: "00000003"}}' > "$FACTORY_CREW_HOME/stations.json"

"$F" projects >"$T/projects.out" 2>&1
row() { grep "^$1 " "$T/projects.out"; }
check "p1, planned in its own repo, is listed" '[ -n "$(row p1)" ]'
check "p1 is held by the crew" 'row p1 | awk "{exit !(\$2 == \"yes\")}"'
check "p1 counts merged and shipped as done (2/4)" 'row p1 | awk "{exit !(\$3 == \"2/4\")}"'
check "p1 is now on M2 with 0 of 2 done" 'row p1 | grep -q "M2 0/2"'
check "p1 has one active builder" 'row p1 | awk "{exit !(\$6 == 1)}"'
check "p1 has about two hours left" 'row p1 | grep -Eq "~(1h5[0-9]|2h[0-9])m"'
check "p2 is listed once" '[ "$(grep -c "^p2 " "$T/projects.out")" = 1 ]'
check "p2 is not held, 1/2 done from its ticket files" 'row p2 | awk "{exit !(\$2 == \"no\" && \$3 == \"1/2\")}"'
check "p2 has no estimate before a hand-over" 'row p2 | awk "{exit !(\$7 == \"-\")}"'

cp "$here/../plugin/templates/crew.json" "$FACTORY_CREW_HOME/crew.json"
"$F" crew status >"$T/status.out" 2>&1
check "crew status ends with a line for p1" 'grep -Eq "^project p1: 2/4 tickets done, now M2 0/2, about ~" "$T/status.out"'
check "crew status has no line for p2 (not held)" '! grep -q "^project p2" "$T/status.out"'

# A ledger that covers only the first milestone doesn't hide the tickets still in files.
R3="$T/repos/p3"; mkdir -p "$R3/.factory/tickets"
printf '# T1: a\nmilestone: M1\nstatus: todo\n' > "$R3/.factory/tickets/T1-a.md"
printf '# T2: b\nmilestone: M2\nstatus: todo\n' > "$R3/.factory/tickets/T2-b.md"
jq -n '{tickets: [{id: "T1", milestone: "M1", stage: "merged", updated: "2026-10-08T01:00:00+00:00"}]}' > "$R3/.factory/ledger.json"
# A ledger in a shape the kit doesn't write must not break the listing.
R4="$T/repos/p4"; mkdir -p "$R4/.factory"; echo '{"tickets": {"T1": "merged"}}' > "$R4/.factory/ledger.json"
jq --arg r3 "$R3" --arg r4 "$R4" '. + {p3: {repo: $r3, repos: [$r3], addedAt: "2026-10-08"}, p4: {repo: $r4, repos: [$r4]}}' \
  "$FACTORY_CREW_HOME/projects.json" > "$T/pj" && mv "$T/pj" "$FACTORY_CREW_HOME/projects.json"
"$F" projects >"$T/projects.out" 2>&1
check "a partial ledger keeps the later tickets (1/2, now M2)" 'row p3 | awk "{exit !(\$3 == \"1/2\" && \$4 == \"M2\")}"'
check "an odd ledger still lists the project" '[ -n "$(row p4)" ] && [ -n "$(row p1)" ]'
"$F" crew status >"$T/status2.out" 2>&1; rc=$?
check "an odd ledger doesn't break crew status" '[ $rc = 0 ] && grep -q "^project p1:" "$T/status2.out" && grep -q "^project p4:" "$T/status2.out"'

# A ticket whose work moved to others is superseded and left out; a stage the kit doesn't know (here a
# sentence, as the orchestrator once wrote) counts as open and is named, never silently.
R5="$T/repos/p5"; mkdir -p "$R5/.factory"
jq -n --arg a "$(iso 3600)" --arg b "$(iso 1800)" '{tickets: [
  {id: "T1", milestone: "M1", stage: "merged", updated: $a},
  {id: "T2", milestone: "M1", stage: " Superseded ", by: ["T3"], updated: $a},
  {id: "T3", milestone: "M1", stage: "Shipped", updated: $b}]}' > "$R5/.factory/ledger.json"
R6="$T/repos/p6"; mkdir -p "$R6/.factory"
jq -n --arg a "$(iso 3600)" --arg b "$(iso 1800)" '{tickets: [
  {id: "T1", milestone: "M1", stage: "merged", updated: $a},
  {id: "T2", milestone: "M1", stage: "shipped", updated: $b},
  {id: "T25", milestone: "M1", stage: "done in M13 (T26, T27), shipped 977c3bb", updated: $b}]}' > "$R6/.factory/ledger.json"
jq --arg r5 "$R5" --arg r6 "$R6" --arg at "$(iso 7200)" '. + {p5: {repo: $r5, repos: [$r5], addedAt: $at}, p6: {repo: $r6, repos: [$r6], addedAt: $at}}' \
  "$FACTORY_CREW_HOME/projects.json" > "$T/pj" && mv "$T/pj" "$FACTORY_CREW_HOME/projects.json"
"$F" projects >"$T/projects.out" 2>&1
check "a superseded ticket is left out, so a finished project is done (2/2)" 'row p5 | awk "{exit !(\$3 == \"2/2\" && \$4 == \"all\" && \$7 == \"done\")}"'
check "stages are read whatever their case and spacing" '! grep -q "^project p5:" "$T/projects.out"'
check "an unknown stage counts as open" 'row p6 | awk "{exit !(\$3 == \"2/3\")}"'
check "and is named, with the stages the kit knows" 'grep -qF "project p6: T25 has an unknown stage (done in M13 (T26, T27), shipped 977c3bb); counted as open. Stages are todo, doing, review, changes, blocked, merged, shipped, superseded" "$T/projects.out"'
"$F" crew status >"$T/status3.out" 2>&1
check "crew status names it too" 'grep -q "^project p6: T25 has an unknown stage" "$T/status3.out" && ! grep -q "^project p5: .*counted as open" "$T/status3.out"'
"$F" doctor >"$T/doctor.out" 2>&1
check "doctor names it once" '[ "$(grep -c "warn  project p6: T25 has an unknown stage" "$T/doctor.out")" = 1 ]'

# Every stage the kit knows passes without a word. A ticket superseded by tickets not planned yet is still
# open work, and named. A project whose every ticket was superseded is done. A stage over two lines stays
# on one.
R7="$T/repos/p7"; mkdir -p "$R7/.factory"
jq -n '{tickets: ([("todo", "Doing", "review", "changes", "blocked", "merged", "shipped") as $s | {id: "T-\($s)", milestone: "M1", stage: $s}]
  + [{id: "T9", milestone: "M1", stage: "superseded", by: "T-merged, T-shipped"}])}' > "$R7/.factory/ledger.json"
R8="$T/repos/p8"; mkdir -p "$R8/.factory"
jq -n '{tickets: [{id: "T1", milestone: "M1", stage: "merged"}, {id: "T2", milestone: "M1", stage: "superseded", by: ["T3", "T4"]},
  {id: "T5", milestone: "M1", stage: "parked\nfor now"}]}' > "$R8/.factory/ledger.json"
R9="$T/repos/p9"; mkdir -p "$R9/.factory"
jq -n '{tickets: [{id: "T1", milestone: "M1", stage: "superseded"}, {id: "T2", milestone: "M1", stage: "superseded", by: []}]}' > "$R9/.factory/ledger.json"
jq --arg r7 "$R7" --arg r8 "$R8" --arg r9 "$R9" '. + {p7: {repo: $r7, repos: [$r7]}, p8: {repo: $r8, repos: [$r8]}, p9: {repo: $r9, repos: [$r9]}}' \
  "$FACTORY_CREW_HOME/projects.json" > "$T/pj" && mv "$T/pj" "$FACTORY_CREW_HOME/projects.json"
"$F" projects >"$T/projects.out" 2>&1
check "every known stage passes without a word" '! grep -q "^project p7:" "$T/projects.out" && row p7 | awk "{exit !(\$3 == \"2/7\")}"'
check "superseded by tickets not planned yet: still open" 'row p8 | awk "{exit !(\$3 == \"1/3\")}"'
check "and named, with the missing tickets" 'grep -qF "project p8: T2 is superseded by T3, T4, not planned yet; T5 has an unknown stage (parked for now); counted as open" "$T/projects.out"'
check "every ticket superseded: done" 'row p9 | awk "{exit !(\$4 \" \" \$5 == \"all done\" && \$7 == \"done\")}" && ! grep -q "^project p9:" "$T/projects.out"'

[ $fail = 0 ] && echo "progress: all cases pass"
exit $fail
