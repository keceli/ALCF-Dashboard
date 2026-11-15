#!/bin/bash
# ===========================================================================
# cron_trigger.sh -- Run from cron on a workstation (ALCF has no user cron):
# asks each machine's login node to check ChemGraph and IQC, which submits the
# update job only when one of them changed. e.g.
#   23 * * * * /path/to/ALCF-Dashboard/chemgraph-env/cron_trigger.sh polaris >> ~/chemgraph_autoupdate.log 2>&1
# Uses ssh -o BatchMode=yes: open a shared connection first (ssh -fN polaris).
# ===========================================================================
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
rc=0
for m in "$@"; do
  # shellcheck disable=SC1090
  SHARED=$(source "$HERE/machines/$m.conf" && echo "$SHARED")
  ssh -o BatchMode=yes -o ConnectTimeout=20 "$m" "bash '$SHARED/trigger_auto_update.sh'" 2>&1 \
    || { echo "[$(date '+%F %T')] $m: ssh failed (no open connection? run: ssh -fN $m)"; rc=1; }
done
exit $rc
