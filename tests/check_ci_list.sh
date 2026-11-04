#!/usr/bin/env bash
# Fail with a clear message when a CI test list is missing, renamed or has no test names.
# ci.yml runs this before it loops over the list: a loop over a list that is not there runs
# nothing and the job would pass without testing anything.
# Usage: bash tests/check_ci_list.sh tests/ci_list.txt
list="${1:?usage: check_ci_list.sh <list file>}"
if [ ! -f "$list" ]; then
  echo "CI test list $list is missing (was it renamed or deleted?). CI reads it to know which tests to run." >&2
  exit 1
fi
if ! grep -qvE '^\s*(#|$)' "$list"; then
  echo "CI test list $list has no test names. CI would run nothing and pass." >&2
  exit 1
fi
