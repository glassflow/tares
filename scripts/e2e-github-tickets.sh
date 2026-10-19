#!/usr/bin/env bash
# Proves that a Tares project mirrors GitHub Issues as tickets against a real repo: the link
# builds the tickets, milestones and dependencies from the issues carrying one label, a change on
# GitHub shows up in the tickets on sync, the minute poll picks up a reopened issue without anyone
# asking, and a sync asked for during a rate-limit backoff answers 429 with Retry-After. GitHub is
# the judge for what the issues look like, Tares's own API for what the tickets look like.
#
# Needs `gh` logged in with the `repo` scope, `jq`, and the worktree's .venv (uv venv && uv pip
# install -e .). Creates issues in a test repo and closes them at the end (the labels and
# milestones it makes stay; each run uses a fresh issue label). Takes about three minutes.
#   E2E_REPO=owner/name   the test repo; default <your login>/tares-gh-tickets-test, created
#                         private if it does not exist
#   KEEP=1                leave the throwaway taresd running and print how to reach it
# Never touches the live Tares on 8787: the daemon runs on a free port in a temp directory.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$REPO/.venv/bin/python"
[ -x "$PY" ] || { echo "FAIL  no $PY: run uv venv && uv pip install -e . in $REPO"; exit 1; }
command -v jq >/dev/null || { echo "FAIL  jq is not on PATH"; exit 1; }
command -v gh >/dev/null || { echo "FAIL  gh is not on PATH"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "FAIL  gh is not logged in: run gh auth login"; exit 1; }
E2E_REPO="${E2E_REPO:-$(gh api user -q .login)/tares-gh-tickets-test}"
KEEP="${KEEP:-0}"
if ! gh repo view "$E2E_REPO" >/dev/null 2>&1; then
  echo "creating the private test repo $E2E_REPO"
  gh repo create "$E2E_REPO" --private --add-readme >/dev/null || { echo "FAIL  could not create $E2E_REPO"; exit 1; }
fi

T=$(cd "$(mktemp -d)" && pwd -P)
DPID=""
ISSUES=""
fail=0
stop_daemon() { [ -n "$DPID" ] && { kill "$DPID" 2>/dev/null || true; wait "$DPID" 2>/dev/null || true; DPID=""; }; return 0; }
cleanup() {
  local n
  for n in $ISSUES; do gh issue close "$n" -R "$E2E_REPO" >/dev/null 2>&1 || true; done
  if [ "$KEEP" = 1 ] && [ -n "$DPID" ]; then
    echo "DAEMON=$DAEMON TOKEN=$ROOT_TOKEN PROJECT=${UID_P:-}"
    echo "console: $DAEMON/projects/${UID_P:-}   (stop it with: kill $DPID)"
    return
  fi
  stop_daemon
  rm -rf "$T"
}
trap cleanup EXIT
free_port() { "$PY" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'; }
ok() { echo "ok    $1"; }
bad() { echo "FAIL  $1"; [ -n "${2:-}" ] && echo "      $2"; fail=1; }

# a. a throwaway taresd
ROOT_TOKEN="e2e-$("$PY" -c 'import secrets; print(secrets.token_hex(16))')"
dport=$(free_port); DAEMON="http://127.0.0.1:$dport"
mkdir -p "$T/tares"
start_daemon() {
  env -u TARES_FACTORY_URL TARES_DB="$T/tares/tares.duckdb" TARES_CATALOG="$T/tares/none.yaml" TARES_OTLP_GRPC_PORT=off \
    TARES_HOST=127.0.0.1 TARES_PORT="$dport" TARES_AUTH_TOKEN="$ROOT_TOKEN" HOME="$T/tares" \
    "$PY" -c "from tares.cli import run_daemon; run_daemon()" >>"$T/taresd.log" 2>&1 &
  DPID=$!
  local up=0 _
  for _ in $(seq 150); do curl -sf "$DAEMON/health" >/dev/null 2>&1 && { up=1; break; }; sleep 0.2; done
  [ $up = 1 ] || { echo "FAIL  taresd did not start"; cat "$T/taresd.log"; exit 1; }
}
start_daemon
# api METHOD PATH [JSON]: the body on stdout, the status in $T/status; never stops the script
api() {
  local m="$1" p="$2" body="${3:-}" args=()
  [ -n "$body" ] && args=(-H 'Content-Type: application/json' --data "$body")
  printf 'Authorization: Bearer %s\n' "$ROOT_TOKEN" | curl -s -o "$T/body" -D "$T/headers" -w '%{http_code}' -X "$m" -H @- \
    ${args[@]+"${args[@]}"} "$DAEMON$p" >"$T/status" || echo 000 >"$T/status"
  cat "$T/body"
}
status() { cat "$T/status"; }

TOKEN=$(gh auth token)
api POST /api/integrations/github "$(jq -n --arg t "$TOKEN" '{name: "e2e", token: $t}')" >/dev/null
[ "$(status)" = 201 ] || { echo "FAIL  could not store the GitHub credential ($(status))"; cat "$T/body"; exit 1; }
UID_P=$(api POST /api/projects '{"template":"custom","name":"gh-e2e","objects":[]}' | jq -r .id)
[ "$(status)" = 201 ] && [ -n "$UID_P" ] || { echo "FAIL  could not create the project ($(status))"; cat "$T/body"; exit 1; }
ok "taresd on $DAEMON, project $UID_P"

# b. seed GitHub: a label, two milestones, four labeled issues, one unlabeled, one dependency
RUN=$(date +%s)
LBL="tares:e2e-$RUN"
gh label create "$LBL" -R "$E2E_REPO" --force >/dev/null
gh label create "in progress" -R "$E2E_REPO" --force >/dev/null
MA="e2e-$RUN-A"; MB="e2e-$RUN-B"
gh api "repos/$E2E_REPO/milestones" -f title="$MA" >/dev/null
gh api "repos/$E2E_REPO/milestones" -f title="$MB" >/dev/null
mkissue() {   # TITLE [gh issue create flags...]: prints the new issue's number
  local title="$1" url; shift
  url=$(gh issue create -R "$E2E_REPO" --title "$title" --body "tares e2e run $RUN, closed when the run ends" "$@")
  echo "${url##*/}"
}
N1=$(mkissue "e2e $RUN one" --label "$LBL" --milestone "$MA")
N2=$(mkissue "e2e $RUN two" --label "$LBL" --milestone "$MA")
N3=$(mkissue "e2e $RUN three" --label "$LBL" --milestone "$MB")
N4=$(mkissue "e2e $RUN four" --label "$LBL" --milestone "$MB")
NX=$(mkissue "e2e $RUN unlabeled")
ISSUES="$N1 $N2 $N3 $N4 $NX"   # set here: mkissue runs in a subshell
ID2=$(gh issue view "$N2" -R "$E2E_REPO" --json id -q .id)
ID3=$(gh issue view "$N3" -R "$E2E_REPO" --json id -q .id)
gh api graphql -f query='mutation($i:ID!,$b:ID!){addBlockedBy(input:{issueId:$i,blockingIssueId:$b}){issue{number}}}' \
  -f i="$ID3" -f b="$ID2" >/dev/null
ok "seeded $E2E_REPO: #$N1 #$N2 #$N3 #$N4 labeled $LBL, #$NX unlabeled, #$N3 blocked by #$N2"

# c. link, and judge the tickets
tickets() { api GET "/api/projects/$UID_P/tickets"; }
field() {   # IDENTIFIER JQ-FIELD: the ticket's field from the last tickets answer
  jq -r --arg i "$1" ".tickets[] | select(.identifier == \$i) | $2" <<<"$TICKETS"
}
api POST "/api/projects/$UID_P/github" "$(jq -n --arg r "$E2E_REPO" --arg l "$LBL" '{repo: $r, issue_label: $l}')" >/dev/null
if [ "$(status)" = 200 ] || [ "$(status)" = 201 ]; then ok "linked $E2E_REPO with $LBL"
else echo "FAIL  linking answered $(status)"; cat "$T/body"; exit 1; fi
TICKETS=$(tickets)
ids=$(jq -r '[.tickets[].identifier] | join(" ")' <<<"$TICKETS")
want="GH-$N1 GH-$N2 GH-$N3 GH-$N4"
if [ "$ids" = "$want" ]; then ok "exactly four tickets, in order: $ids"; else bad "exactly four tickets, in order" "got '$ids', want '$want'"; fi
all=$(jq -r '[.tickets[] | select(.status == "todo" and .owner == "github")] | length' <<<"$TICKETS")
[ "$all" = 4 ] && ok "all four are todo and owned by github" || bad "all four are todo and owned by github" "$all of 4"
[ -z "$(field "GH-$NX" .id)" ] && ok "the unlabeled issue #$NX is not a ticket" || bad "the unlabeled issue #$NX is not a ticket"
ms=$(api GET "/api/projects/$UID_P/milestones" | jq -r '[.milestones[].name] | sort | join(" ")')
[ "$ms" = "$MA $MB" ] && ok "both milestones exist on the project" || bad "both milestones exist on the project" "got '$ms'"
m1=$(field "GH-$N1" .milestone_name); m2=$(field "GH-$N2" .milestone_name)
m3=$(field "GH-$N3" .milestone_name); m4=$(field "GH-$N4" .milestone_name)
[ "$m1/$m2/$m3/$m4" = "$MA/$MA/$MB/$MB" ] && ok "the tickets point at their milestones" || bad "the tickets point at their milestones" "$m1/$m2/$m3/$m4"
dep=$(field "GH-$N3" '.depends_on | join(" ")')
[ "$dep" = "GH-$N2" ] && ok "GH-$N3 depends on GH-$N2" || bad "GH-$N3 depends on GH-$N2" "depends_on '$dep'"

# d. change GitHub, sync now, judge. GitHub's lists can lag a moment, so a sync that does not show
# the change yet is asked again a few times before it counts as wrong.
sync_expect() {   # IDENTIFIER STATUS WHAT
  local i got=""
  for i in 1 2 3 4 5; do
    api POST "/api/projects/$UID_P/github/sync" >/dev/null
    TICKETS=$(tickets)
    got=$(field "$1" .status)
    [ "$got" = "$2" ] && { ok "$3: $1 is $2"; return; }
    sleep 3
  done
  bad "$3: $1 is $2" "it is '$got' (last sync answered $(status))"
}
gh issue close "$N1" -R "$E2E_REPO" --reason completed >/dev/null 2>&1
gh issue close "$N2" -R "$E2E_REPO" --reason "not planned" >/dev/null 2>&1
gh issue edit "$N3" -R "$E2E_REPO" --add-label "in progress" >/dev/null 2>&1
gh issue edit "$N4" -R "$E2E_REPO" --remove-label "$LBL" >/dev/null 2>&1
sync_expect "GH-$N1" done "closed as completed"
sync_expect "GH-$N2" canceled "closed as not planned"
sync_expect "GH-$N3" in_progress "labeled in progress"
sync_expect "GH-$N4" canceled "label removed"

# e. the minute poll: reopen without asking for a sync and wait for the ticket to follow
gh issue reopen "$N1" -R "$E2E_REPO" >/dev/null 2>&1
got=""
for _ in $(seq 9); do
  sleep 10
  TICKETS=$(tickets); got=$(field "GH-$N1" .status)
  [ "$got" = todo ] && break
done
[ "$got" = todo ] && ok "the poll alone brought the reopened GH-$N1 back to todo" \
  || bad "the poll alone brought the reopened GH-$N1 back to todo" "it is '$got' after 90 s"

# f. a sync asked for during a rate-limit backoff. The daemon holds the database, so stop it, put
# the backoff on the project's link the way a rate-limited sync does, and start it again.
stop_daemon
TARES_DB="$T/tares/tares.duckdb" UID_P="$UID_P" "$PY" - <<'PY'
import os
from datetime import timedelta
from tares.envelope import now_utc
from tares.store import Store
s = Store(os.environ["TARES_DB"]); uid = os.environ["UID_P"]
s.set_project_github(uid, {**s.get_project_github(uid), "error": "GitHub rate limit reached",
                           "backoff_until": (now_utc() + timedelta(seconds=120)).isoformat()})
PY
start_daemon
api POST "/api/projects/$UID_P/github/sync" >/dev/null
retry=$(tr -d '\r' <"$T/headers" | awk 'tolower($1) == "retry-after:" {print $2}')
if [ "$(status)" = 429 ] && [ "${retry:-0}" -ge 100 ] && [ "${retry:-0}" -le 125 ]; then
  ok "sync now during the backoff answered 429, Retry-After $retry"
else bad "sync now during the backoff answered 429 with Retry-After" "status $(status), Retry-After '${retry:-}'"; fi
[ "$(tickets | jq -r '.github.error')" = "GitHub rate limit reached" ] && ok "the link still says why" || bad "the link still says why"

# g. the verdict; the trap closes the issues
if [ $fail = 0 ]; then echo "PASS github tickets e2e"; else echo "FAIL github tickets e2e"; exit 1; fi
