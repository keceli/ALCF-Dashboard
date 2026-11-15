#!/bin/bash
# ===========================================================================
# auto_update.sh -- Safely update ChemGraph / IQC in a shared ChemGraph env
# when new upstream code appears, verify ON A COMPUTE NODE, and either promote
# the update (keep it) or roll back to the last-good state on any failure.
#
# Machine-independent: reads machine.conf next to this script (a copy of
# machines/<machine>.conf). Runs on a compute node (proxy for pip + the GPU for
# verify), submitted as auto_update.pbs by trigger_auto_update.sh. Idempotent
# and lock-guarded -- a no-op when nothing is new.
#
# Config via env vars (all optional):
#   TRACK_MODE=main|release   follow origin/main HEAD (default) or latest tag
#   ON_FAIL=rollback|keep     on verify failure, revert to last-good (default) or keep+alert
#   CHEMGRAPH_EXTRAS=...       ChemGraph optional groups (default from machine.conf)
#   FORCE=1                   run the update path even if nothing changed (for testing)
# ===========================================================================
set -o pipefail      # NOT -u: conda activate hooks reference unbound vars

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck disable=SC1091
source "$HERE/machine.conf" || { echo "auto_update: no machine.conf in $HERE" >&2; exit 1; }
# shellcheck disable=SC1091
source "$HERE/cg_common.sh"
# an extras override from trigger_auto_update.sh (':'-separated for qsub -v)
[ -n "${CG_EXTRAS:-}" ] && CHEMGRAPH_EXTRAS=${CG_EXTRAS//:/,}

LOG=$RES/auto_update.log
STATUS=$RES/auto_update_status.json
STATE=$SHARED/.auto_update_state           # last-good SHAs (promoted)
FAILED=$SHARED/.auto_update_failed         # targets that failed verification
ON_FAIL="${ON_FAIL:-rollback}"
EXTRAS="${CHEMGRAPH_EXTRAS:-parsl,ensemble_launcher,test}"
EXTRA_PKGS="aiosqlite sqlite-vec argcomplete psutil nglview openpyxl xlrd"

mkdir -p "$RES"
log(){ echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

# one-writer lock; a second run exits immediately
exec 9>"$SHARED/.auto_update.lock"
if ! flock -n 9; then echo "[$(date '+%F %T')] another auto_update is running; exit" >>"$LOG"; exit 0; fi

write_status(){ # $1=result $2=message
  cat > "$STATUS" <<EOF
{
  "result": "$1",
  "message": "$2",
  "time": "$(date '+%F %T %Z')",
  "host": "$(hostname)",
  "machine": "$MACHINE",
  "track_mode": "$TRACK_MODE",
  "chemgraph": "$(git -C "$SRC/ChemGraph" rev-parse --short HEAD 2>/dev/null) ($(grep -m1 '^version' "$SRC/ChemGraph/pyproject.toml" 2>/dev/null | tr -d ' "' | cut -d= -f2))",
  "iqc": "$(git -C "$SRC/IQC" rev-parse --short HEAD 2>/dev/null)",
  "chemgraph_pytest": "${CG_RESULT:-n/a}",
  "iqc_pytest": "${IQC_RESULT:-n/a}"
}
EOF
  chmod a+r "$STATUS" 2>/dev/null || true
}

# ---- environment ----------------------------------------------------------
cg_proxy
cg_threads
ulimit -c 0 2>/dev/null || true
cg_activate || { log "FATAL: cannot activate $ENV_PREFIX"; write_status error "activate failed"; exit 1; }
cg_constraints

log "=== auto_update start on $(hostname) ($MACHINE)  track=$TRACK_MODE on_fail=$ON_FAIL ==="
# refresh the dashboard env-status snapshot on every exit (any path)
trap 'bash "$HERE/env_status.sh" >>"$LOG" 2>&1 || true' EXIT

# ---- resolve target ref per repo + detect change --------------------------
declare -A OLD_SHA TARGET_SHA TARGET_REF
CHANGED=""
for r in $REPOS; do
  d="$SRC/$r"
  git -C "$d" fetch -q origin 2>&1 | tee -a "$LOG"
  OLD_SHA[$r]=$(git -C "$d" rev-parse HEAD)
  read -r TARGET_REF[$r] TARGET_SHA[$r] <<<"$(cg_target "$d")"
  if [ "${OLD_SHA[$r]}" != "${TARGET_SHA[$r]}" ]; then
    CHANGED="$CHANGED $r"
    log "$r: NEW ${OLD_SHA[$r]:0:9} -> ${TARGET_SHA[$r]:0:9} (ref ${TARGET_REF[$r]})"
  else
    log "$r: up to date at ${OLD_SHA[$r]:0:9}"
  fi
done

if [ -z "$CHANGED" ] && [ "${FORCE:-0}" != "1" ]; then
  log "nothing new; exiting (no-op)."
  write_status no-change "all repos up to date"
  exit 0
fi
[ -z "$CHANGED" ] && CHANGED=$REPOS   # FORCE path

# ---- apply the update -----------------------------------------------------
for r in $CHANGED; do
  d="$SRC/$r"
  log "updating $r -> ${TARGET_REF[$r]}"
  git -C "$d" reset -q --hard "${TARGET_SHA[$r]}" 2>&1 | tee -a "$LOG"
  git -C "$d" clean -fdq 2>&1 | tee -a "$LOG"   # drop generated test artifacts
done

# One dry run of the new code serves two purposes: the gate (abort before torch
# could be replaced) and the rollback snapshot (current version of everything
# it would install; editable packages are handled by git).
APPLY_OK=1
ROLLBACK_PINS=""; ROLLBACK_UNINSTALL=""
# shellcheck disable=SC2086
if ! WOULD=$(cg_would_install -c "$CONSTRAINTS" $INDEX_ARGS -e "$SRC/ChemGraph[$EXTRAS]" 2>>"$LOG"); then
  log "dependency resolution FAILED; not installing."
  APPLY_OK=0
elif OFFEND=$(echo "$WOULD" | cg_gate); then
  log "DRY-RUN GATE TRIPPED (torch replacement / CUDA libraries). Not installing."
  echo "$OFFEND" | tee -a "$LOG"
  APPLY_OK=0
else
  NAMES=$(echo "$WOULD" | sed 's/==.*//' | grep -viE '^(chemgraph|iqc)$')
  if [ -n "$NAMES" ]; then
    # shellcheck disable=SC2086
    CUR=$(python - $NAMES <<'PY'
import sys, importlib.metadata as md
for n in sys.argv[1:]:
    try:
        print(n + "==" + md.version(n))
    except md.PackageNotFoundError:
        print(n)
PY
)
    ROLLBACK_PINS=$(echo "$CUR" | grep '==' | tr '\n' ' ')
    ROLLBACK_UNINSTALL=$(echo "$CUR" | grep -v '==' | tr '\n' ' ')
  fi
  log "will be able to restore: ${ROLLBACK_PINS:-(none)} ; uninstall-on-rollback: ${ROLLBACK_UNINSTALL:-(none)}"

  # shellcheck disable=SC2086
  cg_pip install -c "$CONSTRAINTS" $INDEX_ARGS -e "$SRC/ChemGraph[$EXTRAS]" 2>&1 | tail -6 | tee -a "$LOG" || APPLY_OK=0
  cg_editable "$SRC/ChemGraph" "$SRC/IQC" 2>&1 | tail -4 | tee -a "$LOG" || APPLY_OK=0
  # shellcheck disable=SC2086
  cg_pip install -c "$CONSTRAINTS" $INDEX_ARGS $EXTRA_PKGS 2>&1 | tail -2 | tee -a "$LOG" || true
  cg_numpy 2>&1 | tail -2 | tee -a "$LOG" || APPLY_OK=0
fi

# ---- verify on-node -------------------------------------------------------
# run_pytest <dir> <timeout> <output> [args]: summary line on stdout, pytest's status
run_pytest(){
  local dir=$1 limit=$2 out=$3; shift 3
  (cd "$dir" && timeout "$limit" python -m pytest -q -p no:cacheprovider "$@" >"$out" 2>&1)
  local rc=$?
  grep -E 'passed|failed|error|no tests ran' "$out" | tail -1
  [ $rc -eq 124 ] && echo "timed out after ${limit}s"
  return $rc
}

CG_RESULT="not-run"; IQC_RESULT="not-run"; VERIFY_OK=0
if [ "$APPLY_OK" = "1" ]; then
  log "verifying (imports + $DEVICE + calculators)..."
  if DEVICE=$DEVICE NUMPY_PIN=$NUMPY_PIN python "$HERE/auto_update_verify.py" >>"$LOG" 2>&1; then
    log "core verify PASSED; running pytest suites..."
    # shellcheck disable=SC2086
    CG_RESULT=$(run_pytest "$SRC/ChemGraph" 2400 "$RES/pytest_chemgraph.txt" $PYTEST_ARGS); CG_RC=$?
    IQC_RESULT=$(run_pytest "$SRC/IQC" 1200 "$RES/pytest_iqc.txt"); IQC_RC=$?
    CG_RESULT=$(echo $CG_RESULT); IQC_RESULT=$(echo $IQC_RESULT)
    log "ChemGraph pytest: $CG_RESULT (exit $CG_RC)"
    log "IQC pytest:       $IQC_RESULT (exit $IQC_RC)"
    [ "$CG_RC" = 0 ] && [ "$IQC_RC" = 0 ] && VERIFY_OK=1
  else
    log "core verify FAILED (imports/device/calculators)."
  fi
fi

# ---- promote or roll back -------------------------------------------------
if [ "$VERIFY_OK" = "1" ]; then
  for r in $REPOS; do echo "$r $(git -C "$SRC/$r" rev-parse HEAD)"; done > "$STATE"
  rm -f "$FAILED"
  chmod a+r "$STATE" 2>/dev/null || true
  chmod -R a+rX "$ENV_PREFIX" "$SRC/ChemGraph" "$SRC/IQC" 2>/dev/null || true
  log "=== PROMOTED update. ChemGraph=$(grep -m1 '^version' "$SRC/ChemGraph/pyproject.toml" | tr -d ' "' | cut -d= -f2) ==="
  write_status updated_ok "verified clean; promoted"
  exit 0
fi

# Remember the failed targets: the trigger does not resubmit them.
for r in $REPOS; do echo "$r ${TARGET_SHA[$r]}"; done > "$FAILED"
log "UPDATE FAILED verification."
if [ "$ON_FAIL" = "keep" ]; then
  log "ON_FAIL=keep -> leaving updated env in place; ALERT: manual review needed."
  write_status updated_FAILED_kept "verify failed; kept per ON_FAIL=keep -- REVIEW"
  exit 1
fi

log "rolling back to last-good..."
# 1) revert code
for r in $CHANGED; do
  git -C "$SRC/$r" reset -q --hard "${OLD_SHA[$r]}" 2>&1 | tee -a "$LOG"
  git -C "$SRC/$r" clean -fdq 2>&1 | tee -a "$LOG"
done
# 2) revert bumped deps precisely
# shellcheck disable=SC2086
[ -n "$ROLLBACK_PINS" ] && cg_pip install --no-deps -c "$CONSTRAINTS" $INDEX_ARGS $ROLLBACK_PINS 2>&1 | tail -4 | tee -a "$LOG"
# shellcheck disable=SC2086
[ -n "$ROLLBACK_UNINSTALL" ] && cg_uninstall $ROLLBACK_UNINSTALL 2>&1 | tail -4 | tee -a "$LOG"
# 3) re-link editable + re-pin numpy
cg_editable "$SRC/ChemGraph" "$SRC/IQC" 2>&1 | tail -4 | tee -a "$LOG"
cg_numpy 2>&1 | tail -2 | tee -a "$LOG"
# 4) sanity re-check imports
if python -c "import chemgraph, iqc, torch, numpy" 2>>"$LOG"; then
  log "=== ROLLED BACK to last-good; env importable again. ==="
  write_status rolled_back "update failed verification; reverted to last-good"
else
  log "=== ROLLBACK INCOMPLETE -- env may be broken. MANUAL INTERVENTION NEEDED. ==="
  write_status rollback_broken "update failed AND rollback import check failed -- FIX MANUALLY"
fi
exit 1
