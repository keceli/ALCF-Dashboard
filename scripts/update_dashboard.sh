#!/usr/bin/env bash
# Collect a fresh dashboard snapshot and, with --push, publish it.
#
# Usage: scripts/update_dashboard.sh [--push] [--every SECONDS] [-- COLLECTOR_OPTIONS...]
#
# The snapshot goes to docs/data/dashboard.json, merged with the previous one so
# that a source that fails (e.g. an SSH host without an open session) keeps its
# last good data. --push uploads it as the only commit of the dashboard-data
# branch (force-pushed, so history does not grow) and starts the Dashboard
# workflow to deploy it; without an authenticated `gh` the next scheduled run
# deploys it instead. The checked-out branch, index and working tree are never
# touched.
#
# Run it periodically from cron (SSH needs open ControlMaster sessions, see README):
#   */30 * * * * /path/to/ALCF-Dashboard/scripts/update_dashboard.sh --push >> ~/alcf-dashboard.log 2>&1
# or keep it running, e.g. in tmux:
#   scripts/update_dashboard.sh --push --every 1800
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
snapshot=$repo/docs/data/dashboard.json
branch=dashboard-data
push=0
every=0
collector_args=()

usage() { sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d; s/^# \{0,1\}//'; }
stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

while [ $# -gt 0 ]; do
  case $1 in
    --push) push=1 ;;
    --every)
      [ $# -ge 2 ] || { echo "--every needs a number of seconds" >&2; exit 2; }
      every=$2
      shift ;;
    --) shift; collector_args=("$@"); break ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1 (collector options go after --)" >&2; exit 2 ;;
  esac
  shift
done
case $every in
  ''|*[!0-9]*) echo "--every needs a number of seconds, got '$every'" >&2; exit 2 ;;
esac

# Skip this run while a previous one is still going (e.g. a slow SSH host).
if command -v flock >/dev/null 2>&1; then
  exec 9>"${TMPDIR:-/tmp}/alcf-dashboard-$(id -u).lock"
  flock -n 9 || { echo "another update is still running; skipping"; exit 0; }
fi

publish() {
  local blob tree commit
  # A single parentless commit holding only the snapshot. Check every step:
  # pushing an empty commit id would delete the branch instead.
  blob=$(git -C "$repo" hash-object -w "$snapshot") &&
    tree=$(printf '100644 blob %s\tdashboard.json\n' "$blob" | git -C "$repo" mktree) &&
    commit=$(git -C "$repo" commit-tree "$tree" -m "Dashboard snapshot $(stamp)") &&
    [ -n "$commit" ] || return 1
  # Never wait for a password prompt (e.g. under cron).
  GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND=${GIT_SSH_COMMAND:-ssh -o BatchMode=yes} \
    git -C "$repo" push --quiet --force origin "$commit:refs/heads/$branch" || return 1
  if command -v gh >/dev/null 2>&1 &&
      (cd "$repo" && GH_PROMPT_DISABLED=1 gh workflow run pages.yml --ref main) >/dev/null 2>&1; then
    echo "pushed $branch; deployment started"
  else
    echo "pushed $branch; the next scheduled workflow run deploys it"
  fi
}

update() {
  echo "== $(stamp)"
  # errexit is off inside `update || ...`, hence the explicit checks.
  "${PYTHON:-python3}" "$repo/collector/alcf_dashboard.py" --merge "$snapshot" -o "$snapshot" \
    ${collector_args[@]+"${collector_args[@]}"} || return 1
  if [ "$push" = 1 ]; then
    publish || return 1
  fi
}

if [ "$every" -gt 0 ]; then
  while true; do
    update || echo "update failed; retrying in ${every}s" >&2
    # Without the lock fd, so that a sleep orphaned by `kill` cannot keep holding it.
    sleep "$every" 9>&-
  done
fi
update
