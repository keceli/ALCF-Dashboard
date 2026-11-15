#!/bin/bash
# ===========================================================================
# build_env_uv.sh -- Build the shared ChemGraph env as a uv venv (Sophia, Crux).
#
# Reads machine.conf next to this script. Follows the Polaris/Aurora recipe
# (PORTABLE_INSTALL.md) with uv in place of conda: torch for the hardware
# first, then ChemGraph with torch constrained, ChemGraph and IQC editable.
# Unlike the conda envs, numpy stays at ChemGraph's 2.2.6: uv resolves a
# matscipy that supports it. nwchem is conda-only and is not included.
#
# Run on a LOGIN node (proxy, no walltime cap; installing needs no GPU), then
# verify on a compute node:  FORCE=1 bash trigger_auto_update.sh
# Re-running reuses the clones and the venv.
# ===========================================================================
set -o pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck disable=SC1091
source "$HERE/machine.conf" || { echo "build: no machine.conf in $HERE" >&2; exit 1; }
# shellcheck disable=SC1091
source "$HERE/cg_common.sh"
[ "$ENV_KIND" = uv ] || { echo "build: $MACHINE is not a uv machine" >&2; exit 1; }
EXTRAS="${CHEMGRAPH_EXTRAS:-parsl,ensemble_launcher,test}"

say () { echo; echo "=====[ $* ]====="; }
die () { echo "BUILD FAILED: $*" >&2; exit 1; }

cg_proxy
cg_threads
export PYTHONNOUSERSITE=1
ulimit -c 0 2>/dev/null || true
umask 022
[ -x "$UV" ] || die "uv not found (install: curl -LsSf https://astral.sh/uv/install.sh | sh)"
say "build starting $(date) on $(hostname) | $MACHINE | $TORCH_SPEC | $("$UV" --version)"
mkdir -p "$SRC" "$RES" "$UV_PYTHON_INSTALL_DIR" "$UV_CACHE_DIR"

say "1. sources"
clone () { # $1=name $2=url
  if [ ! -d "$SRC/$1/.git" ]; then git clone -q "$2" "$SRC/$1" || die "clone $1"; fi
  git -C "$SRC/$1" checkout -q main && git -C "$SRC/$1" pull -q --ff-only || echo "WARN: $1 not updated"
  echo "$1 @ $(git -C "$SRC/$1" rev-parse --short HEAD)"
}
clone ChemGraph https://github.com/argonne-lcf/ChemGraph.git
clone IQC https://github.com/Autonomous-Scientific-Agents/IQC.git

say "2. python 3.11 + venv"
"$UV" python install 3.11 || die "uv python install"
if [ ! -x "$ENV_PREFIX/bin/python" ]; then
  "$UV" venv --python 3.11 --seed "$ENV_PREFIX" || die "uv venv"
else
  echo "venv exists, reusing: $ENV_PREFIX"
fi
"$ENV_PREFIX/bin/python" --version

say "3. torch first ($TORCH_SPEC from $TORCH_INDEX)"
cg_constraints
cg_pip install "$TORCH_SPEC" --index-url "$TORCH_INDEX" || die "torch"

say "4. ChemGraph deps (guarded) extras=[$EXTRAS]"
WOULD=$(cg_would_install -c "$CONSTRAINTS" -e "$SRC/ChemGraph[$EXTRAS]") || die "ChemGraph resolution"
if OFFEND=$(echo "$WOULD" | cg_gate); then
  echo "$OFFEND"; die "dry run would replace torch or add CUDA libraries"
fi
echo "dry-run gate OK ($(echo "$WOULD" | grep -c .) packages)"
cg_pip install -c "$CONSTRAINTS" -e "$SRC/ChemGraph[$EXTRAS]" || die "ChemGraph deps"

say "5. IQC + optional tools"
cg_pip install -c "$CONSTRAINTS" -e "$SRC/IQC" || echo "WARN: IQC failed (optional)"
cg_pip install -c "$CONSTRAINTS" aiosqlite sqlite-vec pyscf argcomplete psutil nglview openpyxl xlrd \
  || echo "WARN: extras partial"
# mpi4py's wheel loads an MPI at run time; the mpich wheel provides one for
# single-node use. For multi-node runs rebuild it against the site MPI.
cg_pip install -c "$CONSTRAINTS" mpi4py mpich || echo "WARN: mpi4py failed (optional)"

say "6. numpy + editable refresh"
# IQC and the optional tools above do not pin numpy; put back ChemGraph's.
cg_pip install -c "$CONSTRAINTS" -e "$SRC/ChemGraph[$EXTRAS]" || die "ChemGraph deps (2)"
cg_numpy || die "numpy re-pin"
cg_editable "$SRC/ChemGraph" "$SRC/IQC" || die "editable installs"
echo "--- uv pip check ---"
cg_pip check || echo "WARN: uv pip check reports conflicts (above)"

say "7. activate script + permissions"
cat > "$ACTIVATE" <<EOS
# Activate the shared ChemGraph env on ALCF ${MACHINE^}:
#
#   source $ACTIVATE
#
# A uv venv (python 3.11, $TORCH_SPEC). Run real work on a compute node.
ulimit -c 0 2>/dev/null || true
export http_proxy=http://proxy.alcf.anl.gov:3128
export https_proxy=http://proxy.alcf.anl.gov:3128
# Loopback bypasses the proxy (MCP servers, parsl workers, test mocks).
export no_proxy="127.0.0.1,localhost,::1,.alcf.anl.gov"
export NO_PROXY="\$no_proxy"
export OMP_NUM_THREADS="\${OMP_NUM_THREADS:-1}"
export OPENBLAS_NUM_THREADS="\${OPENBLAS_NUM_THREADS:-1}"
export MKL_NUM_THREADS="\${MKL_NUM_THREADS:-1}"
# Keep users' ~/.local packages (e.g. a mismatched torch) out of the shared env.
export PYTHONNOUSERSITE=1
source $ENV_PREFIX/bin/activate
echo "ChemGraph env active: $ENV_PREFIX (\$(python --version 2>&1))"
EOS
chmod -R a+rX "$ENV_PREFIX" "$SRC" "$UV_PYTHON_INSTALL_DIR" "$ACTIVATE" "$HERE" 2>/dev/null || echo "WARN: chmod partial"

say "8. versions"
bash "$HERE/env_status.sh" && cat "$RES/env_status.json"
say "BUILD COMPLETE $(date). Verify on a compute node: FORCE=1 bash $HERE/trigger_auto_update.sh"
