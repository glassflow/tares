#!/usr/bin/env bash
# Checks the idle wake (TR-673) on a real Tares with a fake claude: with the crew setting off (unset or
# false) the watch does exactly what it did, no wake, no state file; with it on the watch wakes a
# station only when Tares has work for it (the reviewer's queue, a builder's ticket, a RELEASE in the
# orchestrator's report, a desk answer or a new project), once per thing, never a busy, attached or
# halted station, and sends a liveness wake after 15, 20 or 30 minutes of quiet, then 30, then every 45.
# Time is stepped with FACTORY_NOW, so two hours take a few seconds.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/lib.sh"
T=$(cd "$(mktemp -d)" && pwd -P)
mkdir -p "$T/kit-under-test"; cp -R "$here/../../claude-plugin/." "$T/kit-under-test/"; F="$T/kit-under-test/bin/factory"
export HOME="$T/home" FACTORY_CREW_HOME="$T/crew" FAKE_CLAUDE_STATE="$T/claude" CLAUDE_BIN="$here/fake-claude" FACTORY_PAUSE=0
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
unset TARES_URL TARES_AUTH_TOKEN TARES_FACTORY_URL TARES_FACTORY_TOKEN_FILE FACTORY_REPO FACTORY_WORKTREES CLAUDE_JOB_DIR TARES_FACTORY_MCP_URL TARES_FACTORY_CRED_FILE TARES_MCP_HOST TARES_MCP_PORT FACTORY_NOW FACTORY_WAKE_SETTLE
mkdir -p "$HOME"
chmod +x "$CLAUDE_BIN"
STUB=""; ATT=""
trap '[ -n "$STUB" ] && kill "$STUB" 2>/dev/null; [ -n "$ATT" ] && kill "$ATT" 2>/dev/null; stop_tares; rm -rf "$T"' EXIT
fail=0
check() { if eval "$2"; then :; else echo "FAIL $1"; fail=1; [ -z "${IW_DEBUG:-}" ] || { api GET "/api/projects/$P/tickets" | jq -c ".tickets[] | {label, stage, holder, pr, verdict}"; tail -4 "$FACTORY_CREW_HOME/events.jsonl"; for f in "$FACTORY_CREW_HOME"/stations/*.wake; do echo "$f"; cat "$f"; echo; done; tail -5 "$T/watch.log"; }; fi; }
st() { jq -r --arg s "$1" ".[\$s]$2 // empty" "$FACTORY_CREW_HOME/stations.json"; }
agent() { local tmp; tmp=$(mktemp); jq --arg id "$(st "$1" .id)" "map(if .id == \$id then $2 else . end)" "$FAKE_CLAUDE_STATE/agents.json" > "$tmp" && mv "$tmp" "$FAKE_CLAUDE_STATE/agents.json"; }
launches() { grep -c '"--bg"' "$FAKE_CLAUDE_STATE/calls.jsonl" 2>/dev/null; true; }
stops() { grep -c '^{"cwd":"[^"]*","args":\["stop"' "$FAKE_CLAUDE_STATE/calls.jsonl" 2>/dev/null; true; }
last() { jq -c "select(.args[2] == \"$1\") | .args" "$FAKE_CLAUDE_STATE/calls.jsonl" | tail -1; }
lastprompt() { last "$1" | jq -r '.[-1]'; }
# wake events of a station: all of them, or those whose reason starts with a word (work, liveness, rearm)
wakes() { jq -c --arg s "$1" --arg w "${2:-}" 'select(.event == "idle_wake" and .station == $s and (.detail | startswith($w)))' "$FACTORY_CREW_HOME/events.jsonl" 2>/dev/null | grep -c . ; true; }
allwakes() { jq -r 'select(.event == "idle_wake") | .station' "$FACTORY_CREW_HOME/events.jsonl" 2>/dev/null | grep -c . ; true; }
wakefile() { echo "$FACTORY_CREW_HOME/stations/$1.wake"; }
wk() { jq -r "$2 // empty" "$(wakefile "$1")" 2>/dev/null; }
wait_for() { local _; for _ in $(seq 40); do eval "$1" && return 0; sleep 0.5; done; return 1; }

N0=2000000000   # the clock the watch sees, in epoch seconds; rounds step it
SEC=0           # seconds since N0 of the latest round
B=links-app-build-m1
STATIONS="crew-orchestrator crew-reviewer crew-releaser $B"
idle_all() { local s; for s in $STATIONS; do agent $s '.state = "done"'; done; }
# One watch round at SEC seconds, then every station idle again (as a woken one is when its turn ends).
round() { SEC=$1; FACTORY_NOW=$((N0 + SEC)) "$F" crew watch --once >>"$T/watch.log" 2>&1; idle_all; }
# Rounds every $2 seconds up to $1 seconds, noting when each station was woken (the state file's last_wake).
sim() { # until-seconds step
  local t s lw
  for s in $STATIONS; do : > "$T/times-$s"; done
  for t in $(seq $((SEC + $2)) "$2" "$1"); do
    round "$t"
    for s in $STATIONS; do
      lw=$(wk $s .last_wake)
      if [ -n "$lw" ] && [ "$lw" != "$(cat "$T/lw-$s" 2>/dev/null)" ]; then echo "$lw" > "$T/lw-$s"; echo $((lw - N0)) >> "$T/times-$s"; fi
    done
  done
}
times() { tr '\n' ' ' < "$T/times-$1" | sed 's/ $//'; }
iso() { "$PY" -c 'import sys, datetime; print(datetime.datetime.fromtimestamp(int(sys.argv[1]), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))' "$1"; }
touched() { # station seconds-since-N0: the session did something then (its job file's updatedAt)
  local f="$HOME/.claude/jobs/$(st "$1" .id)/state.json" tmp; tmp=$(mktemp)
  jq --arg u "$(iso $((N0 + $2)))" '.updatedAt = $u' "$f" > "$tmp" && mv "$tmp" "$f"
}
touched_at() { # station iso-time: the session's job file says it last did something then
  local f="$HOME/.claude/jobs/$(st "$1" .id)/state.json" tmp; tmp=$(mktemp)
  jq --arg u "$2" '.updatedAt = $u' "$f" > "$tmp" && mv "$tmp" "$f"
}
set_idle_wake() { api PUT /api/crew/settings "$(jq -cn --argjson v "$1" '{idle_wake: $v}')" >/dev/null; }

start_tares "$T/tares"
trust "$FACTORY_CREW_HOME"
api PUT /api/crew/settings '{"autonomy": "L3-review", "releaser": true, "release_profile": "kind", "challenger": false, "builders_max": 2}' >/dev/null
"$F" crew up --yes >"$T/up.out" 2>&1 || { echo "FAIL crew up"; cat "$T/up.out"; exit 1; }
repo="$T/links"; mkdir -p "$repo"; git -C "$repo" init -q; repo=$(git -C "$repo" rev-parse --show-toplevel)
P=$(hand_over "Links App" "$repo")
"$F" spawn builder "Links App" m1 "$repo" "work T1" >/dev/null 2>&1
check "the crew and a builder are up" '[ "$(launches)" = 4 ] && [ -n "$(st $B .id)" ]'
idle_all
api POST "/api/projects/$P/tickets" '{"title": "Store"}' >/dev/null
api POST "/api/projects/$P/tickets/T1/assign" "{\"holder\": \"$B\"}" >/dev/null
api POST "/api/projects/$P/tickets/T1/stage" '{"stage": "review"}' >/dev/null
check "setup: a ticket waits in the review queue" '[ "$(api GET /api/review-queue | jq ".queue | length")" = 1 ]'

# ---- the setting off: nothing changes ----
check "the setting starts unset" '[ "$(api GET /api/crew/settings | jq -c .idle_wake)" = null ]'
l0=$(launches); s0=$(stops)
for t in 600 1800 3600 7200 14400; do round $t; done
check "unset: five rounds, hours apart, with an unreviewed PR: no wake" '[ "$(allwakes)" = 0 ] && [ "$(launches)" = "$l0" ] && [ "$(stops)" = "$s0" ]'
check "unset: no wake state is written" '[ -z "$(ls "$FACTORY_CREW_HOME"/stations/*.wake 2>/dev/null)" ]'
set_idle_wake false
for t in 21600 28800; do round $t; done
check "false: the same" '[ "$(allwakes)" = 0 ] && [ "$(launches)" = "$l0" ] && [ "$(stops)" = "$s0" ] && [ -z "$(ls "$FACTORY_CREW_HOME"/stations/*.wake 2>/dev/null)" ]'
"$F" status >"$T/status-off.out" 2>&1; "$F" doctor >"$T/doctor-off.out" 2>&1
check "off: status and doctor have no wake line" '! grep -q "idle_wake: on" "$T/status-off.out" && ! grep -q "idle_wake is on" "$T/doctor-off.out"'
check "off: a restart still says to re-arm the schedule" 'last crew-reviewer >/dev/null; "$F" restart crew-reviewer >/dev/null 2>&1; lastprompt crew-reviewer | grep -q "re-arm your schedule if you have one" && ! lastprompt crew-reviewer | grep -q "idle_wake"'
idle_all

# ---- on, nothing to do: only liveness wakes, backing off ----
api POST "/api/projects/$P/tickets/T1/stage" '{"stage": "doing"}' >/dev/null
check "setup: the queue is empty" '[ "$(api GET /api/review-queue | jq ".queue | length")" = 0 ]'
set_idle_wake true
check "the setting reads back true" '[ "$(api GET /api/crew/settings | jq -c .idle_wake)" = true ]'
"$F" status >"$T/status-on.out" 2>&1; "$F" doctor >"$T/doctor-on.out" 2>&1
check "on: status says so, and doctor warns that no watch is running" 'grep -q "^idle_wake: on" "$T/status-on.out" && grep -q "warn  idle_wake is on but the watch is not running" "$T/doctor-on.out" && ! grep -q "ok    idle_wake is on" "$T/doctor-on.out"'
bash -c "exec -a 'tares factory crew watch' sleep 300" & WPID=$!
echo "$WPID" > "$FACTORY_CREW_HOME/watch.pid"; sleep 1
"$F" doctor >"$T/doctor-on2.out" 2>&1
check "with the watch running, doctor says ok" 'grep -q "ok    idle_wake is on" "$T/doctor-on2.out" && ! grep -q "idle_wake is on but" "$T/doctor-on2.out"'
kill "$WPID" 2>/dev/null; wait "$WPID" 2>/dev/null; rm -f "$FACTORY_CREW_HOME/watch.pid"
SEC=28800; N0=$((N0 + SEC)); SEC=0
round 0
check "switched on under running stations: the first round tells each one that holds a tick to drop it, once (the builder has no ticket in review, so no tick)" '[ "$(allwakes)" = 3 ] && [ "$(wakes crew-reviewer release)" = 1 ] && [ "$(wakes crew-orchestrator release)" = 1 ] && [ "$(wakes crew-releaser release)" = 1 ] && [ "$(wakes $B)" = 0 ] && [ -f "$(wakefile $B)" ]'
check "and the release prompt says to arm no tick and to delete the one it holds" 'lastprompt crew-reviewer | grep -q "^idle_wake was switched on." && lastprompt crew-reviewer | grep -q "CronList, then CronDelete"'
for s in $STATIONS; do wk $s .last_wake > "$T/lw-$s"; done   # the release wakes are not liveness wakes
sim 7200 180
check "reviewer: woken at 15, then 30 later, then 45 later (minutes 15, 45, 90)" '[ "$(times crew-reviewer)" = "900 2700 5400" ]'
check "orchestrator: 20, then 30, then 45 later" '[ "$(times crew-orchestrator)" = "1260 3060 5760" ]'
check "releaser: 30, then 30, then 45 later" '[ "$(times crew-releaser)" = "1800 3600 6300" ]'
check "a builder with no ticket in review is not woken" '[ -z "$(times $B)" ] && [ "$(wakes $B)" = 0 ]'
check "no station is woken more than 3 times in two hours (reviewer: 8 ticks of 15 minutes before)" '( for s in crew-orchestrator crew-reviewer crew-releaser; do [ "$(wakes $s liveness)" -le 3 ] || exit 1; done ) && [ "$(wakes crew-reviewer liveness)" = 3 ]'
check "every one of them is a liveness wake" '[ "$(wakes crew-reviewer liveness)" = 3 ] && [ "$(allwakes)" = 12 ]'
check "the liveness prompt says nothing is pending, and not to arm a tick" 'p=$(lastprompt crew-reviewer); grep -q "^Liveness check, nothing is known to be pending: check your queue once, then end the turn." <<<"$p" && grep -q "do not arm a CronCreate tick" <<<"$p"'
sim 12600 180
check "the gap stays at 45 minutes: reviewer next at 135 and 180 minutes, orchestrator 141 and 186, releaser 150 and 195" '[ "$(times crew-reviewer)" = "8100 10800" ] && [ "$(times crew-orchestrator)" = "8460 11160" ] && [ "$(times crew-releaser)" = "9000 11700" ]'
check "each wake is a restart in the same conversation" 'last crew-reviewer | jq -e "index(\"--resume\") != null" >/dev/null && [ "$(launches)" -gt "$l0" ]'

# ---- the reviewer: a PR without a verdict, once per head ----
mkdir -p "$T/gh"; echo 1111111aaaaaaaa > "$T/gh/head"; echo success > "$T/gh/ci"
cat >"$T/gh/stub.py" <<'PY'
import http.server, json, sys
d = sys.argv[2]
rd = lambda n: open(f"{d}/{n}").read().strip()
class H(http.server.BaseHTTPRequestHandler):
    def reply(self, o):
        b = json.dumps(o).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/health": self.reply({"ok": True})
        elif p == "/user": self.reply({"login": "stub"})
        elif p.startswith("/repos/o/r/pulls/"):
            self.reply({"head": {"sha": rd("head"), "ref": "factory/T1"}, "state": "open", "merged": False,
                        "title": "Store", "html_url": "https://github.com" + p.replace("/repos", "").replace("/pulls/", "/pull/")})
        elif p.endswith("/status"): self.reply({"statuses": []})
        elif p.endswith("/check-runs"): self.reply({"check_runs": [{"status": "completed", "conclusion": rd("ci")}]})
        else: self.send_error(404)
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
PY
sp=$("$PY" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
"$PY" "$T/gh/stub.py" "$sp" "$T/gh" >/dev/null 2>&1 &
STUB=$!
wait_for "curl -sf http://127.0.0.1:$sp/health >/dev/null"
api POST /api/integrations/github "{\"name\": \"stub\", \"kind\": \"token\", \"token\": \"t\", \"api_url\": \"http://127.0.0.1:$sp\"}" >/dev/null
pr_to() { # stage: the ticket in that stage with its PR read from the stub now
  api POST "/api/projects/$P/tickets/T1/stage" '{"stage": "'"$1"'", "pr": "o/r#1"}' >/dev/null
}
rm -f "$FACTORY_CREW_HOME"/stations/*.wake
SEC=0; N0=$((N0 + 20000)); round 0   # fresh clocks
a0=$(allwakes); w0=$(wakes crew-reviewer work); r0=$(st crew-reviewer .id)
pr_to review
check "setup: the queue has the PR at its head" '[ "$(api GET /api/review-queue | jq -r ".queue[0].head")" = 1111111aaaaaaaa ]'
round 60
check "an unreviewed PR wakes the reviewer" '[ "$(wakes crew-reviewer work)" = $((w0 + 1)) ] && [ "$(st crew-reviewer .id)" != "$r0" ]'
check "and only the reviewer" '[ "$(allwakes)" = $((a0 + 1)) ]'
check "the prompt names the work and says not to arm a tick" 'p=$(lastprompt crew-reviewer); grep -q "There is work in the review queue: Links App T1 at 1111111 have no current verdict" <<<"$p" && grep -q "idle_wake is on: the factory wakes you when there is work and holds your tick" <<<"$p"'
round 120; round 180
check "the same head wakes it once, not every round" '[ "$(wakes crew-reviewer work)" = $((w0 + 1)) ]'
echo 2222222bbbbbbbb > "$T/gh/head"; pr_to review
round 240
check "a new head wakes it again" '[ "$(wakes crew-reviewer work)" = $((w0 + 2)) ] && [ "$(allwakes)" = $((a0 + 2)) ]'
echo 3333333cccccccc > "$T/gh/head"; pr_to review
agent crew-reviewer '.state = "working"'
n=$(launches)
FACTORY_NOW=$((N0 + 300)) "$F" crew watch --once >>"$T/watch.log" 2>&1
check "a reviewer in the middle of a turn is not woken" '[ "$(launches)" = "$n" ] && [ "$(wakes crew-reviewer work)" = $((w0 + 2)) ]'
agent crew-reviewer '.state = "done"'
round 360
check "the next round, once it is idle, wakes it" '[ "$(wakes crew-reviewer work)" = $((w0 + 3)) ]'
check "the wake marks that head as told" 'wk crew-reviewer ".seen | length" | grep -qx 1'
api POST "/api/projects/$P/tickets/T1/reviews" '{"head": "3333333cccccccc", "verdict": "changes", "verified": "read the diff", "findings": [{"text": "name the flag", "blocking": true}]}' >"$T/review.out"
check "setup: a current verdict is recorded" '[ "$(api GET /api/review-queue | jq -r ".queue[0].verdict.current")" = true ]'
round 420
check "a PR with a current verdict is no work for the reviewer" '[ "$(wakes crew-reviewer work)" = $((w0 + 3)) ]'

# ---- the builder: changes asked for, a verdict on the current head, failed CI ----
b0=$(wakes $B work)
check "the builder was woken for the verdict on its ticket" '[ "$b0" = 1 ] && lastprompt $B | grep -q "Your tickets changed: T1 (verdict changes)"'
round 480; round 540
check "once per verdict" '[ "$(wakes $B work)" = 1 ]'
api POST "/api/projects/$P/tickets/T1/stage" '{"stage": "changes", "reason": "see the review"}' >/dev/null
round 600
check "changes asked for on the head the verdict is about is the same thing: still once" '[ "$(wakes $B work)" = 1 ]'
api POST "/api/projects/$P/tickets" '{"title": "Cache"}' >/dev/null
api POST "/api/projects/$P/tickets/T2/assign" '{"holder": "someone-else-build-m1"}' >/dev/null
api POST "/api/projects/$P/tickets/T2/stage" '{"stage": "changes", "reason": "needs a rebase"}' >/dev/null
a1=$(allwakes)
round 660
check "a ticket held by another builder wakes this one not at all" '[ "$(wakes $B work)" = 1 ] && [ "$(allwakes)" = "$a1" ]'
api POST "/api/projects/$P/tickets/T2/assign" "{\"holder\": \"$B\"}" >/dev/null
round 720
check "changes asked for on its own ticket wakes the holder" '[ "$(wakes $B work)" = 2 ] && lastprompt $B | grep -q "T2 (changes asked for)"'
round 780
check "once" '[ "$(wakes $B work)" = 2 ]'
echo 5555555eeeeeeee > "$T/gh/head"; echo failure > "$T/gh/ci"; pr_to review
a1=$(allwakes)
round 840
check "failed CI on the head wakes the holder (and the reviewer for the new head)" '[ "$(wakes $B work)" = 3 ] && lastprompt $B | grep -q "T1 (CI failed)" && ! lastprompt $B | grep -q "T2" && [ "$(wakes crew-reviewer work)" = $((w0 + 4)) ] && [ "$(allwakes)" = $((a1 + 2)) ]'
round 900
check "and only once" '[ "$(wakes $B work)" = 3 ]'

# ---- the orchestrator: a desk answer, a new project; the releaser: a RELEASE line ----
o0=$(wakes crew-orchestrator work)
check "the first look only sets the baseline" '[ "$o0" = 0 ] && [ -n "$(wk crew-orchestrator ".baseline.projects[0]")" ]'
api POST /api/desk "$(jq -cn --arg p "$P" '{question: "Keep the old endpoint?", recommendation: "keep it", why: "two callers", options: ["keep it", "drop it"], project: $p}')" >/dev/null
round 840
check "an open question is not an answer" '[ "$(wakes crew-orchestrator work)" = 0 ]'
api POST /api/sources '{"name": "claude_code", "connector": "claude_code", "poll": "10s", "config": {"push": true}}' >/dev/null
sleep 1
ts=$("$PY" -c 'import datetime; print((datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z")')
printf '%s\n' "{\"type\":\"user\",\"sessionId\":\"o1\",\"cwd\":\"/w\",\"timestamp\":\"$ts\",\"message\":{\"role\":\"user\",\"content\":\"Keep it, please\"}}" |
  curl -sf -X POST -H 'Content-Type: application/x-ndjson' --data-binary @- "$TARESD_URL/ingest/claude_code" >/dev/null
sleep 2
api POST /api/desk/D1/answer '{"words": "Keep it, please", "choice": "keep it"}' >"$T/answer.out"
check "setup: the question is answered" '[ "$(api GET "/api/desk?state=answered" | jq ".desk | length")" = 1 ]'
touched_at crew-orchestrator 2020-01-01T00:00:00Z   # it did nothing since the answer was closed: someone else closed it
a2=$(allwakes)
round 900
check "a desk answer wakes the orchestrator, and only it" '[ "$(wakes crew-orchestrator work)" = 1 ] && [ "$(allwakes)" = $((a2 + 1)) ] && lastprompt crew-orchestrator | grep -q "desk answer D1 is closed"'
round 960
check "once" '[ "$(wakes crew-orchestrator work)" = 1 ]'
api POST /api/desk "$(jq -cn --arg p "$P" '{question: "Ship the cache?", recommendation: "yes", why: "tested", options: ["yes", "no"], project: $p}')" >/dev/null
ts=$("$PY" -c 'import datetime; print((datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z")')
printf '%s\n' "{\"type\":\"user\",\"sessionId\":\"o1\",\"cwd\":\"/w\",\"timestamp\":\"$ts\",\"message\":{\"role\":\"user\",\"content\":\"Yes, ship it\"}}" |
  curl -sf -X POST -H 'Content-Type: application/x-ndjson' --data-binary @- "$TARESD_URL/ingest/claude_code" >/dev/null
sleep 2
api POST /api/desk/D2/answer '{"words": "Yes, ship it", "choice": "yes"}' >"$T/answer2.out" 2>&1
check "setup: a second question is answered" '[ "$(api GET "/api/desk?state=answered" | jq ".desk | length")" = 2 ] || { cat "$T/answer2.out" >&2; false; }'
touched_at crew-orchestrator 2099-01-01T00:00:00Z   # it was active after the answer was closed: it recorded the answer itself
round 1000
check "an answer the orchestrator recorded itself is no news to it" '[ "$(wakes crew-orchestrator work)" = 1 ]'
mid=$("$PY" -c 'import datetime; print(datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))'); sleep 2   # after the answer above, before the one below
api POST /api/desk "$(jq -cn --arg p "$P" '{question: "Allow crew-reviewer to run gh?", recommendation: "allow", why: "needed", station: "crew-reviewer", command: "gh pr view 1", project: $p}')" >/dev/null
api POST /api/desk/D3/allow '{"scope": "all", "words": "crew-reviewer may run gh pr view"}' >"$T/allow.out"
check "setup: a permission request is allowed in the console" '[ "$(api GET "/api/desk?state=answered" | jq "[.desk[] | select(.command != null)] | length")" = 1 ]'
round 1010   # the grant it made is handed to each station first (a regrant restart, not a wake)
touched_at crew-orchestrator "$mid"   # active since the second answer was closed, not the third
round 1015
check "an allowed permission request is the watch's to act on, not a wake for the orchestrator" '[ "$(wakes crew-orchestrator work)" = 1 ]'
repo2="$T/blog"; mkdir -p "$repo2"; git -C "$repo2" init -q
hand_over "Blog" "$(git -C "$repo2" rev-parse --show-toplevel)" >/dev/null
round 1020
check "a project handed over wakes the orchestrator" '[ "$(wakes crew-orchestrator work)" = 2 ] && lastprompt crew-orchestrator | grep -q "project Blog was handed over"'
mkdir -p "$FACTORY_CREW_HOME/reports"
printf '# crew-orchestrator\n\n## Undelivered\n- T4 done, waiting on nothing\n\n## Notes\n- RELEASE later maybe\n' > "$FACTORY_CREW_HOME/reports/crew-orchestrator.md"
round 1080
check "a RELEASE line outside Undelivered, or none inside it, wakes nobody" '[ "$(wakes crew-releaser work)" = 0 ]'
printf '# crew-orchestrator\n\n## Undelivered\n- T4 done, waiting on nothing\n- RELEASE Links App milestone M1 (T1, T2)\n- RELEASED M0 last week\n\n## Notes\n' > "$FACTORY_CREW_HOME/reports/crew-orchestrator.md"
a3=$(allwakes)
round 1140
check "a RELEASE line under Undelivered wakes the releaser, and only it" '[ "$(wakes crew-releaser work)" = 1 ] && [ "$(allwakes)" = $((a3 + 1)) ] && lastprompt crew-releaser | grep -q "RELEASE Links App milestone M1"'
round 1200; round 1260
check "once per line" '[ "$(wakes crew-releaser work)" = 1 ]'
printf '%s\n- RELEASE Links App milestone M2 (T3)\n' "$(cat "$FACTORY_CREW_HOME/reports/crew-orchestrator.md")" > "$FACTORY_CREW_HOME/reports/crew-orchestrator.md"
printf '# crew-orchestrator\n\n## Undelivered\n- RELEASE Links App milestone M1 (T1, T2)\n- RELEASE Links App milestone M2 (T3)\n' > "$FACTORY_CREW_HOME/reports/crew-orchestrator.md"
round 1320
check "a second line is a second wake" '[ "$(wakes crew-releaser work)" = 2 ]'

# ---- activity pushes the liveness clock back ----
rm -f "$FACTORY_CREW_HOME"/stations/*.wake
printf '# crew-orchestrator\n\n## Undelivered\n' > "$FACTORY_CREW_HOME/reports/crew-orchestrator.md"
api POST "/api/projects/$P/tickets/T1/stage" '{"stage": "doing"}' >/dev/null
N0=$((N0 + 20000)); SEC=0; round 0
l1=$(wakes crew-reviewer liveness)
round 600; round 840
check "before 15 minutes: no liveness wake" '[ "$(wakes crew-reviewer liveness)" = "$l1" ]'
round 900
check "at 15 minutes: the first" '[ "$(wakes crew-reviewer liveness)" = $((l1 + 1)) ] && [ "$(wk crew-reviewer .n)" = 1 ]'
touched crew-reviewer 1600   # someone talked to it, long after the wake and its settle time
round 1700
check "activity later than the wake and the settle time resets the count and the clock to it" '[ "$(wk crew-reviewer .n)" = 0 ] && [ "$(wk crew-reviewer .clock)" = $((N0 + 1600)) ] && [ "$(wakes crew-reviewer liveness)" = $((l1 + 1)) ]'
round 2400
check "so the next wake is not at 45 minutes" '[ "$(wakes crew-reviewer liveness)" = $((l1 + 1)) ]'
round 2500
check "but 15 minutes after the activity, at the base again" '[ "$(wakes crew-reviewer liveness)" = $((l1 + 2)) ] && [ "$(wk crew-reviewer .n)" = 1 ]'
touched crew-reviewer 2550   # its own turn after the wake, inside the settle time: no reset
round 2600
check "a session's own turn after a wake does not reset it" '[ "$(wk crew-reviewer .n)" = 1 ]'

# ---- who is never woken ----
rm -f "$FACTORY_CREW_HOME"/stations/*.wake
N0=$((N0 + 20000)); SEC=0; round 0
w1=$(allwakes); n=$(launches)
"$F" crew halt "testing the wake" >/dev/null 2>&1
round 3600; round 7200
check "a halted crew is not woken" '[ "$(allwakes)" = "$w1" ] && [ "$(launches)" = "$n" ]'
"$F" crew resume >/dev/null 2>&1
bash -c "exec -a 'claude attach $(st crew-reviewer .id)' sleep 120" & ATT=$!
sleep 1
rw=$(wakes crew-reviewer); ow=$(wakes crew-orchestrator)
round 10800
check "a station someone has open in a terminal is not woken, the others are" '[ "$(wakes crew-reviewer)" = "$rw" ] && [ "$(wakes crew-orchestrator)" = $((ow + 1)) ]'
kill "$ATT" 2>/dev/null; wait "$ATT" 2>/dev/null; ATT=""
round 11400
check "it is, once they leave" '[ "$(wakes crew-reviewer)" = $((rw + 1)) ]'
touch "$FACTORY_CREW_HOME/stations/$B.done"
echo 6666666ffffffff > "$T/gh/head"; echo success > "$T/gh/ci"; pr_to review
api POST "/api/projects/$P/tickets/T1/reviews" '{"head": "6666666ffffffff", "verdict": "changes", "verified": "read the diff", "findings": [{"text": "name the flag", "blocking": true}]}' >/dev/null
check "setup: a head nobody has seen yet, with a current verdict" '[ "$(api GET /api/review-queue | jq -r ".queue[0].head")" = 6666666ffffffff ] && [ "$(api GET /api/review-queue | jq -r ".queue[0].verdict.current")" = true ]'
n=$(launches); bw=$(wakes $B); rw=$(wakes crew-reviewer work)
round 12000
check "a builder that is done is not woken, whatever its ticket says" '[ "$(wakes $B)" = "$bw" ]'
check "a head that already has a current verdict is no work for the reviewer, even though it was never seen" '[ "$(wakes crew-reviewer work)" = "$rw" ]'
rm -f "$FACTORY_CREW_HOME/stations/$B.done"
round 12060
check "(the ticket was real work: once it is not done, the builder is woken for it)" '[ "$(wakes $B)" = $((bw + 1)) ] && lastprompt $B | grep -q "T1 (verdict changes)"'
"$F" spawn helper "$B" probe "look at T1" >/dev/null 2>&1
h=$B-probe; hs=$(jq -r 'to_entries[] | select(.value.role == "helper") | .key' "$FACTORY_CREW_HOME/stations.json" | head -1)
check "setup: a helper is running" '[ -n "$hs" ]'
STATIONS="$STATIONS $hs"; idle_all
round 40000
check "a helper is never woken" '[ "$(wakes $hs)" = 0 ]'
STATIONS="crew-orchestrator crew-reviewer crew-releaser $B"

# ---- a restart that fails is not a delivered wake ----
echo 7777777aaaaaaaa > "$T/gh/head"; pr_to review
rw=$(wakes crew-reviewer work)
FAKE_CLAUDE_FAIL_RESUME=1 round 40050
check "a wake whose restart fails sends no wake event and marks nothing told" '[ "$(wakes crew-reviewer work)" = "$rw" ] && ! wk crew-reviewer ".seen[]" | grep -q 7777777 && grep -q "could not wake crew-reviewer" "$T/watch.log"'
round 40060; round 40120   # the first of these revives the station the failed restart left stopped
check "once it runs again the wake is delivered" '[ "$(wakes crew-reviewer work)" = $((rw + 1)) ] && wk crew-reviewer ".seen[]" | grep -q 7777777'

# a bare failure deep in the restart (here, the grants file of the new session cannot be written) must stop it just the same
api POST /api/grants '{"words": "the reviewer may label pull requests", "scope": "all"}' >/dev/null
round 40150; round 40160   # the watch hands the new grant to each station first
echo 8888888bbbbbbbb > "$T/gh/head"; pr_to review
gf="$FACTORY_CREW_HOME/stations/crew-reviewer.grants"
rw=$(wakes crew-reviewer work)
rm -f "$gf"; mkdir "$gf"
round 40200
rmdir "$gf"
check "a restart that fails midway is not marked delivered either" '[ "$(wakes crew-reviewer work)" = "$rw" ] && ! wk crew-reviewer ".seen[]" | grep -q 8888888 && grep -q "could not wake crew-reviewer" "$T/watch.log"'
round 40260; round 40320
check "and it is sent once the failure is gone" '[ "$(wakes crew-reviewer work)" = $((rw + 1)) ]'

# ---- turning it off: each station that gave up its tick is told once to take it back ----
api POST "/api/projects/$P/tickets/T1/stage" '{"stage": "doing"}' >/dev/null
set_idle_wake false
a4=$(allwakes)
round 40100
check "off again: every station that gave up its tick is woken to re-arm it" '( for s in crew-orchestrator crew-reviewer crew-releaser $B; do [ "$(wakes $s rearm)" = 1 ] || { echo "no re-arm wake for $s" >&2; exit 1; }; done )'
check "the prompt tells it to re-arm as its manual says" 'lastprompt crew-releaser | grep -q "^idle_wake is off again: re-arm your tick as your manual says."'
round 40200; round 90000
check "and once: nothing more is sent" '[ "$(allwakes)" = $((a4 + 4)) ] && [ "$(jq -r .released "$(wakefile crew-reviewer)")" = false ]'
l2=$(launches)
round 130000
check "off: the watch is back to doing nothing" '[ "$(launches)" = "$l2" ]'
check "a restart now says to re-arm the schedule" '"$F" restart crew-releaser >/dev/null 2>&1; lastprompt crew-releaser | grep -q "re-arm your schedule if you have one"'
set_idle_wake true
"$F" restart crew-releaser >/dev/null 2>&1
check "on: a restart says to arm no tick" 'lastprompt crew-releaser | grep -q "arm no tick of your own" && ! lastprompt crew-releaser | grep -q "re-arm your schedule if you have one" && [ "$(jq -r .released "$(wakefile crew-releaser)")" = true ]'

[ $fail = 0 ] && echo "idle_wake: all cases pass"
exit $fail
