#!/bin/bash
# ===========================================================================
# trigger_auto_update.sh -- tiny LOGIN-node entry point for the auto-updater.
# Call this over SSH from an off-site cron, e.g.:
#   ssh aurora 'bash /lus/flare/projects/catalyst/world_shared/chemgraph/trigger_auto_update.sh'
#
# It does NOT do the update itself; it submits auto_update.pbs (which runs on a
# compute node). It dedupes: if an auto_update job is already queued/running it
# just reports and exits, so overlapping cron ticks never pile up jobs.
#
# Env passthrough (optional): TRACK_MODE, ON_FAIL, FORCE, CHEMGRAPH_EXTRAS.
# ===========================================================================
set -o pipefail
SHARED=/lus/flare/projects/catalyst/world_shared/chemgraph
JOBNAME=chemgraph_auto_update
export PATH=/opt/pbs/bin:$PATH

# already queued/running? -> skip (precise match by name+owner via qselect)
EXISTING=$(qselect -u "$USER" -N "$JOBNAME" 2>/dev/null)
if [ -n "$EXISTING" ]; then
  echo "[$(date '+%F %T')] $JOBNAME already in queue ($EXISTING); skipping submit." | tee -a "$SHARED/test_results/auto_update.log"
  exit 0
fi

# pass through any tuning vars that are set
VARS=""
for v in TRACK_MODE ON_FAIL FORCE CHEMGRAPH_EXTRAS; do
  [ -n "${!v}" ] && VARS="${VARS:+$VARS,}$v=${!v}"
done
SUBMIT="qsub"
[ -n "$VARS" ] && SUBMIT="qsub -v $VARS"

cd "$SHARED"
JID=$($SUBMIT auto_update.pbs 2>&1)
echo "[$(date '+%F %T')] submitted $JOBNAME: $JID (vars: ${VARS:-defaults})" | tee -a "$SHARED/test_results/auto_update.log"
