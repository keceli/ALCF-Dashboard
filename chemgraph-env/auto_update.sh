#!/bin/bash
# ===========================================================================
# auto_update.sh -- Safely update ChemGraph / IQC in the shared Aurora env
# when new upstream code appears, verify ON A COMPUTE NODE, and either promote
# the update (keep it) or roll back to the last-good state on any failure.
#
# Designed to run on a COMPUTE node (needs the proxy for pip + a PVC GPU for the
# torch-xpu verify). Normally invoked by auto_update.pbs, which is submitted by
# trigger_auto_update.sh (called over SSH by an off-site cron). Idempotent and
# lock-guarded -- safe to run on any schedule; a no-op when nothing is new.
#
# Config via env vars (all optional):
#   TRACK_MODE=main|release   follow origin/main HEAD (default) or latest tag
#   ON_FAIL=rollback|keep     on verify failure, revert to last-good (default) or keep+alert
#   CHEMGRAPH_EXTRAS=...       ChemGraph optional groups (default parsl,ensemble_launcher,test)
#   FORCE=1                   run the update path even if nothing changed (for testing)
# ===========================================================================
set -o pipefail      # NOT -u: conda Intel-MPI activate hooks reference unbound vars

SHARED=/lus/flare/projects/catalyst/world_shared/chemgraph
ENV_PREFIX=$SHARED/alcf_chemgraph_env
SRC=$SHARED/src
CONSTRAINTS=$SHARED/constraints.txt
RES=$SHARED/test_results
LOG=$RES/auto_update.log
STATUS=$RES/auto_update_status.json
STATE=$SHARED/.auto_update_state           # last-good SHAs (promoted)
MINIFORGE=/opt/aurora/26.26.0/spack/unified/1.1.1/install/linux-x86_64/miniforge3-25.11.0-1-khkcc6i
XPU=https://download.pytorch.org/whl/xpu
PYPI=https://pypi.org/simple

TRACK_MODE="${TRACK_MODE:-main}"
ON_FAIL="${ON_FAIL:-rollback}"
EXTRAS="${CHEMGRAPH_EXTRAS:-parsl,ensemble_launcher,test}"
REPOS="ChemGraph IQC"

mkdir -p "$RES"
log(){ echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }
PIP(){ python -m pip "$@"; }

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
  "track_mode": "$TRACK_MODE",
  "chemgraph": "$(cd "$SRC/ChemGraph" && git rev-parse --short HEAD 2>/dev/null) ($(grep -m1 '^version' "$SRC/ChemGraph/pyproject.toml" 2>/dev/null | tr -d ' "' | cut -d= -f2))",
  "iqc": "$(cd "$SRC/IQC" && git rev-parse --short HEAD 2>/dev/null)",
  "chemgraph_pytest": "${CG_RESULT:-n/a}",
  "iqc_pytest": "${IQC_RESULT:-n/a}"
}
EOF
  chmod a+r "$STATUS" 2>/dev/null || true
}

# ---- environment ----------------------------------------------------------
export http_proxy=http://proxy.alcf.anl.gov:3128 https_proxy=http://proxy.alcf.anl.gov:3128
export no_proxy="127.0.0.1,localhost,::1,.alcf.anl.gov" NO_PROXY="$no_proxy"
export OMP_NUM_THREADS=8 OPENBLAS_NUM_THREADS=8 MKL_NUM_THREADS=8 PYTHONNOUSERSITE=1
export PIP_DISABLE_PIP_VERSION_CHECK=1
ulimit -c 0 2>/dev/null || true
source "$MINIFORGE/etc/profile.d/conda.sh" || { log "FATAL: conda.sh missing"; write_status error "conda base missing"; exit 1; }
conda activate "$ENV_PREFIX" || { log "FATAL: cannot activate $ENV_PREFIX"; write_status error "activate failed"; exit 1; }

log "=== auto_update start on $(hostname)  track=$TRACK_MODE on_fail=$ON_FAIL ==="
# refresh the dashboard env-status snapshot on every exit (any path)
trap 'bash "$SHARED/env_status.sh" >>"$LOG" 2>&1 || true' EXIT

# ---- resolve target ref per repo + detect change --------------------------
declare -A OLD_SHA TARGET_SHA TARGET_REF
CHANGED=""
for r in $REPOS; do
  d="$SRC/$r"
  git -C "$d" fetch -q origin 2>&1 | tee -a "$LOG"
  OLD_SHA[$r]=$(git -C "$d" rev-parse HEAD)
  if [ "$TRACK_MODE" = "release" ]; then
    tag=$(git -C "$d" describe --tags --abbrev=0 origin/main 2>/dev/null)
    if [ -n "$tag" ]; then TARGET_REF[$r]="$tag"; TARGET_SHA[$r]=$(git -C "$d" rev-list -n1 "$tag");
    else TARGET_REF[$r]="origin/main"; TARGET_SHA[$r]=$(git -C "$d" rev-parse origin/main); fi
  else
    TARGET_REF[$r]="origin/main"; TARGET_SHA[$r]=$(git -C "$d" rev-parse origin/main)
  fi
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

# ---- snapshot dependency versions for precise rollback --------------------
# Parse the ChemGraph dry-run "Would install" list; record each dep's CURRENT
# version so we can pin it back on failure. Exclude editable pkgs (handled by git).
log "snapshotting dependency state for rollback..."
DRY=$(PIP install --dry-run -c "$CONSTRAINTS" --index-url "$XPU" --extra-index-url "$PYPI" \
        -e "$SRC/ChemGraph[$EXTRAS]" 2>&1)
ROLLBACK_PINS=""; ROLLBACK_UNINSTALL=""
for tok in $(echo "$DRY" | grep -oiE 'Would install .*' | sed 's/Would install //'); do
  name="${tok%-*}"; # strip trailing -version
  case "$name" in chemgraph|iqc|graph[-_]longrange) continue;; esac
  cur=$(PIP show "$name" 2>/dev/null | awk -F': ' '/^Version/{print $2}')
  if [ -n "$cur" ]; then ROLLBACK_PINS="$ROLLBACK_PINS $name==$cur"; else ROLLBACK_UNINSTALL="$ROLLBACK_UNINSTALL $name"; fi
done
log "will be able to restore:${ROLLBACK_PINS:- (none)} ; uninstall-on-rollback:${ROLLBACK_UNINSTALL:- (none)}"

# ---- apply the update -----------------------------------------------------
for r in $CHANGED; do
  d="$SRC/$r"
  log "updating $r -> ${TARGET_REF[$r]}"
  git -C "$d" reset --hard "${TARGET_SHA[$r]}" 2>&1 | tee -a "$LOG"
  git -C "$d" clean -fdq 2>&1 | tee -a "$LOG"   # drop generated test artifacts
done

APPLY_OK=1
# dry-run gate: abort before any CUDA torch / torch replacement can happen
GATE=$(PIP install --dry-run -c "$CONSTRAINTS" --index-url "$XPU" --extra-index-url "$PYPI" \
        -e "$SRC/ChemGraph[$EXTRAS]" 2>&1)
if echo "$GATE" | tr ' ' '\n' | grep -qiE '^nvidia-[a-z]|^torch-[0-9]'; then
  log "DRY-RUN GATE TRIPPED (CUDA torch / torch replacement). Not installing."
  echo "$GATE" | tr ' ' '\n' | grep -iE '^nvidia-[a-z]|^torch-[0-9]' | tee -a "$LOG"
  APPLY_OK=0
else
  PIP install -c "$CONSTRAINTS" --index-url "$XPU" --extra-index-url "$PYPI" \
      -e "$SRC/ChemGraph[$EXTRAS]" 2>&1 | tail -6 | tee -a "$LOG" || APPLY_OK=0
  PIP install --no-deps -e "$SRC/ChemGraph" 2>&1 | tail -2 | tee -a "$LOG" || APPLY_OK=0
  PIP install --no-deps -e "$SRC/IQC"       2>&1 | tail -2 | tee -a "$LOG" || APPLY_OK=0
  PIP install -c "$CONSTRAINTS" aiosqlite sqlite-vec argcomplete psutil nglview openpyxl xlrd \
      2>&1 | tail -2 | tee -a "$LOG" || true
  PIP install -c "$CONSTRAINTS" "numpy==1.26.4" 2>&1 | tail -2 | tee -a "$LOG" || APPLY_OK=0
fi

# ---- verify on-node -------------------------------------------------------
CG_RESULT="not-run"; IQC_RESULT="not-run"; VERIFY_OK=0
if [ "$APPLY_OK" = "1" ]; then
  log "verifying (imports + xpu + calculators)..."
  if python "$SHARED/auto_update_verify.py" >>"$LOG" 2>&1; then
    log "core verify PASSED; running pytest suites..."
    CG_RESULT=$(cd "$SRC/ChemGraph" && timeout 2400 python -m pytest -q -p no:cacheprovider 2>&1 \
                 | tee "$RES/pytest_chemgraph.txt" | grep -E 'passed|failed|error' | tail -1)
    IQC_RESULT=$(cd "$SRC/IQC" && timeout 1200 python -m pytest -q -p no:cacheprovider 2>&1 \
                 | tee "$RES/pytest_iqc.txt" | grep -E 'passed|failed|error' | tail -1)
    log "ChemGraph pytest: $CG_RESULT"
    log "IQC pytest:       $IQC_RESULT"
    # pass only if neither summary contains 'failed' or 'error'
    if ! echo "$CG_RESULT $IQC_RESULT" | grep -qiE 'failed|error'; then VERIFY_OK=1; fi
  else
    log "core verify FAILED (imports/xpu/calculators)."
  fi
fi

# ---- promote or roll back -------------------------------------------------
if [ "$VERIFY_OK" = "1" ]; then
  for r in $CHANGED; do echo "$r ${TARGET_SHA[$r]}" ; done > "$STATE"
  chmod a+r "$STATE" 2>/dev/null || true
  chmod -R a+rX "$ENV_PREFIX/lib/python3.11/site-packages" "$SRC" 2>/dev/null || true
  log "=== PROMOTED update. ChemGraph=$(grep -m1 '^version' "$SRC/ChemGraph/pyproject.toml" | tr -d ' \"' | cut -d= -f2) ==="
  write_status updated_ok "verified clean; promoted"
  exit 0
fi

log "UPDATE FAILED verification."
if [ "$ON_FAIL" = "keep" ]; then
  log "ON_FAIL=keep -> leaving updated env in place; ALERT: manual review needed."
  write_status updated_FAILED_kept "verify failed; kept per ON_FAIL=keep -- REVIEW"
  exit 1
fi

log "rolling back to last-good..."
# 1) revert code
for r in $CHANGED; do
  git -C "$SRC/$r" reset --hard "${OLD_SHA[$r]}" 2>&1 | tee -a "$LOG"
  git -C "$SRC/$r" clean -fdq 2>&1 | tee -a "$LOG"
done
# 2) revert bumped deps precisely
[ -n "$ROLLBACK_PINS" ] && PIP install --no-deps -c "$CONSTRAINTS" $ROLLBACK_PINS 2>&1 | tail -4 | tee -a "$LOG"
[ -n "$ROLLBACK_UNINSTALL" ] && PIP uninstall -y $ROLLBACK_UNINSTALL 2>&1 | tail -4 | tee -a "$LOG"
# 3) re-link editable + re-pin numpy
PIP install --no-deps -e "$SRC/ChemGraph" 2>&1 | tail -2 | tee -a "$LOG"
PIP install --no-deps -e "$SRC/IQC"       2>&1 | tail -2 | tee -a "$LOG"
PIP install -c "$CONSTRAINTS" "numpy==1.26.4" 2>&1 | tail -2 | tee -a "$LOG"
# 4) sanity re-check imports
if python -c "import chemgraph, iqc, torch, numpy" 2>>"$LOG"; then
  log "=== ROLLED BACK to last-good; env importable again. ==="
  write_status rolled_back "update failed verification; reverted to last-good"
else
  log "=== ROLLBACK INCOMPLETE -- env may be broken. MANUAL INTERVENTION NEEDED. ==="
  write_status rollback_broken "update failed AND rollback import check failed -- FIX MANUALLY"
fi
exit 1
