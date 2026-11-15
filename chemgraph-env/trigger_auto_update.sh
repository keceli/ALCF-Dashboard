#!/bin/bash
# ===========================================================================
# trigger_auto_update.sh -- LOGIN-node entry point for the auto-updater, called
# over SSH from an off-site cron (see cron_trigger.sh):
#   ssh polaris 'bash /grand/catalyst/world-shared/chemgraph/trigger_auto_update.sh'
#
# Cheap check first: fetches ChemGraph and IQC and submits auto_update.pbs
# (which does the update on a compute node) only when one of them has a new
# commit. It does not resubmit a target that already failed verification, and
# never queues a second job while one is queued or running.
#
# Env passthrough (optional): TRACK_MODE, ON_FAIL, FORCE, CHEMGRAPH_EXTRAS.
# ===========================================================================
set -o pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
EXTRAS_OVERRIDE=${CHEMGRAPH_EXTRAS:-}   # machine.conf sets the default
# shellcheck disable=SC1091
source "$HERE/machine.conf" || { echo "trigger: no machine.conf in $HERE" >&2; exit 1; }
# shellcheck disable=SC1091
source "$HERE/cg_common.sh"
JOBNAME=chemgraph_auto_update
LOG=$RES/auto_update.log
export PATH=/opt/pbs/bin:$PATH
mkdir -p "$RES"
say(){ echo "[$(date '+%F %T')] $MACHINE: $*"; }
cg_proxy

# already queued/running? -> skip (precise match by name+owner via qselect)
EXISTING=$(qselect -u "$USER" -N "$JOBNAME" 2>/dev/null | tr '\n' ' ')
if [ -n "$EXISTING" ]; then
  say "$JOBNAME already in queue ($EXISTING); skipping." | tee -a "$LOG"
  exit 0
fi

TARGETS=""; NEW=""
for r in $REPOS; do
  d="$SRC/$r"
  git -C "$d" fetch -q origin || { say "git fetch failed for $r" | tee -a "$LOG"; exit 1; }
  read -r ref sha <<<"$(cg_target "$d")"
  TARGETS="$TARGETS$r $sha"$'\n'
  [ "$(git -C "$d" rev-parse HEAD)" != "$sha" ] && NEW="$NEW $r@${sha:0:9}"
done
date '+%F %T %Z' > "$RES/last_check"; chmod a+r "$RES/last_check" 2>/dev/null

if [ "${FORCE:-0}" != "1" ]; then
  if [ -z "$NEW" ]; then
    say "up to date."
    bash "$HERE/env_status.sh" >/dev/null 2>&1 || say "env_status failed"
    exit 0
  fi
  if [ -f "$SHARED/.auto_update_failed" ] && [ "$TARGETS" = "$(cat "$SHARED/.auto_update_failed")"$'\n' ]; then
    say "new:$NEW already failed verification; waiting for a newer commit (FORCE=1 to retry)."
    exit 0
  fi
fi

# pass through any tuning vars that are set
VARS=""
for v in TRACK_MODE ON_FAIL FORCE; do
  [ -n "${!v}" ] && VARS="${VARS:+$VARS,}$v=${!v}"
done
# qsub -v splits on commas, so the extras list travels with ':' instead.
[ -n "$EXTRAS_OVERRIDE" ] && VARS="${VARS:+$VARS,}CG_EXTRAS=${EXTRAS_OVERRIDE//,/:}"
submit(){ # $1=queue
  local args=(-N "$JOBNAME" -A "$PBS_ACCOUNT" -q "$1" -l "select=$PBS_SELECT"
              -l "walltime=$PBS_WALLTIME" -l "filesystems=$PBS_FILESYSTEMS"
              -o "$RES/auto_update.pbs.out")
  [ -n "$VARS" ] && args+=(-v "$VARS")
  qsub "${args[@]}" auto_update.pbs 2>&1
}
cd "$HERE" || exit 1
JID=$(submit "$PBS_QUEUE"); rc=$?
if [ $rc -ne 0 ] && [ -n "$PBS_QUEUE_FALLBACK" ]; then
  say "qsub to $PBS_QUEUE failed ($JID); trying $PBS_QUEUE_FALLBACK"
  JID=$(submit "$PBS_QUEUE_FALLBACK"); rc=$?
fi
say "new:${NEW:- (forced)}; submitted $JOBNAME: $JID (vars: ${VARS:-defaults})" | tee -a "$LOG"
exit $rc
