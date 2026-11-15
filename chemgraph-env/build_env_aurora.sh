#!/bin/bash
# ===========================================================================
# build_env_aurora.sh  --  Reproducible build of the shared ChemGraph conda
# environment on ALCF Aurora (Intel Data Center GPU Max / PVC ; torch = XPU).
#
# Mirrors the Polaris build (PORTABLE_INSTALL.md) with the one hardware change:
# torch comes from the Intel-XPU wheel index instead of CUDA. ChemGraph is the
# priority; optional tools (tblite/pyscf/nwchem/IQC) must not break it.
#
# Run on a LOGIN node (proxy works, no walltime cap, no GPU needed to install).
# Verification runs separately on a COMPUTE node (test_env.pbs).
#
# Idempotent-ish: re-running re-uses existing clones and an existing prefix.
# Usage:   bash build_env_aurora.sh   (logs to test_results/build.log via caller)
# ===========================================================================
# NB: no `set -u` -- conda activate.d hooks (Intel MPI mpivars.*) reference
# unbound vars (SETVARS_CALL/CLASSPATH) and would abort activation under nounset.
set -o pipefail

# ---- Paths ----------------------------------------------------------------
SHARED=/lus/flare/projects/catalyst/world_shared/chemgraph
ENV_PREFIX=$SHARED/alcf_chemgraph_env
SRC_DIR=$SHARED/src
CONSTRAINTS=$SHARED/constraints.txt
MINIFORGE=/opt/aurora/26.26.0/spack/unified/1.1.1/install/linux-x86_64/miniforge3-25.11.0-1-khkcc6i

# torch XPU version — must match constraints.txt. Overridable: TORCH_XPU_VER=X.Y.Z
TORCH_XPU_VER="${TORCH_XPU_VER:-2.8.0}"
# ChemGraph optional-dependency groups to install alongside the base deps.
# (test -> quickjs, needed to exercise the full pytest suite)
CHEMGRAPH_EXTRAS="${CHEMGRAPH_EXTRAS:-parsl,ensemble_launcher,test}"
XPU_INDEX=https://download.pytorch.org/whl/xpu
PYPI=https://pypi.org/simple

# ---- Session env ----------------------------------------------------------
export http_proxy=http://proxy.alcf.anl.gov:3128
export https_proxy=http://proxy.alcf.anl.gov:3128
export no_proxy="127.0.0.1,localhost,::1,.alcf.anl.gov"   # loopback bypasses Squid
export NO_PROXY="$no_proxy"
export OMP_NUM_THREADS=8 OPENBLAS_NUM_THREADS=8 MKL_NUM_THREADS=8
export PYTHONNOUSERSITE=1          # keep ~/.local out of the build
export PIP_DISABLE_PIP_VERSION_CHECK=1
ulimit -c 0 2>/dev/null || true   # torch-XPU shutdown segfaults on login (no PVC) -> no giant cores

say () { echo; echo "=====[ $* ]====="; }
die () { echo "BUILD FAILED: $*" >&2; exit 1; }

say "build starting $(date) on $(hostname)  |  torch xpu ${TORCH_XPU_VER}"
mkdir -p "$SRC_DIR"

# ---- 1. Sources -----------------------------------------------------------
say "1. sources"
if [ ! -d "$SRC_DIR/ChemGraph/.git" ]; then
    git clone https://github.com/Autonomous-Scientific-Agents/ChemGraph.git "$SRC_DIR/ChemGraph" || die "clone ChemGraph"
fi
git -C "$SRC_DIR/ChemGraph" checkout main >/dev/null 2>&1 || true
git -C "$SRC_DIR/ChemGraph" pull --ff-only >/dev/null 2>&1 || true
echo "ChemGraph @ $(git -C "$SRC_DIR/ChemGraph" rev-parse --short HEAD)"

if [ ! -d "$SRC_DIR/IQC/.git" ]; then
    git clone https://github.com/Autonomous-Scientific-Agents/IQC.git "$SRC_DIR/IQC" || die "clone IQC"
fi
echo "IQC @ $(git -C "$SRC_DIR/IQC" rev-parse --short HEAD)"

# ---- 2. Ensure constraints.txt matches TORCH_XPU_VER ----------------------
# torch-ONLY: guards against a CUDA torch sneaking in. numpy is NOT constrained
# here -- ChemGraph hard-pins numpy==2.2.6, so a numpy==1.26.4 constraint makes
# resolution impossible. numpy is instead downgraded post-install (steps 5/11),
# which pip allows with only a (documented, expected) dependency-conflict warning.
say "2. constraints (torch-only)"
cat > "$CONSTRAINTS" <<EOF
# auto-written by build_env_aurora.sh -- torch-only (see script step 2 comment)
torch==${TORCH_XPU_VER}+xpu
EOF
cat "$CONSTRAINTS"

# ---- 3. conda base + prefix ----------------------------------------------
say "3. conda create prefix"
source "$MINIFORGE/etc/profile.d/conda.sh" || die "source conda.sh"
if [ ! -d "$ENV_PREFIX/conda-meta" ]; then
    conda create -y -p "$ENV_PREFIX" -c conda-forge \
        python=3.11 pip numpy=1.26.4 pandas=2.2.3 pytest=8.4.1 rich=14.1.0 toml=0.10.2 \
        tblite tblite-python nwchem || die "conda create"
else
    echo "prefix exists, reusing: $ENV_PREFIX"
fi
conda activate "$ENV_PREFIX" || die "conda activate prefix"
echo "python: $(python --version)  @ $(command -v python)"

PIP () { python -m pip "$@"; }

# ---- 4. torch FIRST, from the XPU index, pinned ---------------------------
say "4. torch (XPU) first"
PIP install "torch==${TORCH_XPU_VER}" --index-url "$XPU_INDEX" || die "torch xpu install"
python -c "import torch; print('torch', torch.__version__, '| cuda:', torch.version.cuda)"

# ---- 5. ChemGraph deps (dry-run gate, then real) --------------------------
say "5. ChemGraph deps (guarded)  extras=[${CHEMGRAPH_EXTRAS}]"
CG_TARGET="$SRC_DIR/ChemGraph"
[ -n "$CHEMGRAPH_EXTRAS" ] && CG_TARGET="${SRC_DIR}/ChemGraph[${CHEMGRAPH_EXTRAS}]"
DRY=$(PIP install --dry-run -c "$CONSTRAINTS" --index-url "$XPU_INDEX" --extra-index-url "$PYPI" "$CG_TARGET" 2>&1)
echo "$DRY" | tail -5
# Only trip on a real CUDA leak: an nvidia-* wheel, or the *torch* package itself
# being (re)installed to a non-xpu build. Our torch-2.12.1+xpu is already satisfied,
# so a standalone "torch-<digit>" in "Would install" means a replacement is imminent.
# (Do NOT match torch-ema / torchmetrics / mace-torch — those are fine.)
OFFEND=$(echo "$DRY" | tr ' ' '\n' | grep -Ei '^nvidia-[a-z]|^torch-[0-9]' || true)
if [ -n "$OFFEND" ]; then
    echo "OFFENDING TOKENS:"; echo "$OFFEND"
    die "dry-run shows a CUDA torch / torch replacement — aborting before it happens"
fi
echo "dry-run gate OK: no nvidia-* / no torch replacement"
PIP install -c "$CONSTRAINTS" --index-url "$XPU_INDEX" --extra-index-url "$PYPI" "$CG_TARGET" || die "ChemGraph deps"
PIP install -c "$CONSTRAINTS" "numpy==1.26.4" || die "numpy re-pin (5)"

# ---- 6. ChemGraph editable (bundled data + version tests) -----------------
say "6. ChemGraph editable"
PIP install --no-deps -e "$SRC_DIR/ChemGraph" || die "ChemGraph -e"

# ---- 7. graph-longrange (LEGACY: was a ChemGraph dep <=0.6.x; dropped in 0.7.0) --
# Harmless to install; not required by 0.7.0. Best-effort so old builds still work.
say "7. graph-longrange (legacy, optional)"
PIP install --no-deps -c "$CONSTRAINTS" \
    "graph-longrange @ git+https://github.com/WillBaldwin0/graph_electrostatics.git@v0.4.0" \
    || echo "WARN: graph-longrange install failed (OK -- not needed by ChemGraph >=0.7.0)"

# ---- 8. async sqlite extras ------------------------------------------------
# langgraph-checkpoint-sqlite is a *direct* ChemGraph dep since 0.7.0 (==3.1.2),
# installed by step 5 -- do NOT pin it separately (would downgrade). Just add the
# async-sqlite helpers ChemGraph uses at runtime.
say "8. aiosqlite + sqlite-vec"
PIP install -c "$CONSTRAINTS" aiosqlite sqlite-vec || die "aiosqlite/sqlite-vec"

# ---- 9. optional: pyscf ---------------------------------------------------
say "9. pyscf (optional)"
PIP install -c "$CONSTRAINTS" pyscf || echo "WARN: pyscf failed (optional)"

# ---- 10. IQC + extras -----------------------------------------------------
say "10. IQC editable + extras"
PIP install --no-deps -e "$SRC_DIR/IQC" || echo "WARN: IQC -e failed (optional)"
PIP install -c "$CONSTRAINTS" argcomplete psutil nglview openpyxl xlrd || echo "WARN: IQC extras partial"
# mpi4py: conda-forge is fine for imports/single-node. Multi-node: rebuild against
# the Aurora site MPICH (see README). --freeze-installed protects the pins.
conda install -y --freeze-installed -c conda-forge mpi4py || echo "WARN: mpi4py (conda) failed (optional)"

# ---- 11. final numpy re-pin + integrity -----------------------------------
say "11. final numpy re-pin + pip check"
PIP install -c "$CONSTRAINTS" "numpy==1.26.4" || die "numpy re-pin (final)"
echo "--- pip check (numpy 2.2.6 vs 1.26.4 + pytest/streamlit mismatch EXPECTED) ---"
PIP check || true
echo "--- key versions ---"
python - <<'PY'
import importlib.metadata as md
for p in ["chemgraph","iqc","torch","numpy","ase","rdkit","pymatgen","mace-torch","tblite","pyscf","langgraph","mpi4py"]:
    try: print(f"  {p:14s} {md.version(p)}")
    except Exception as e: print(f"  {p:14s} MISSING")
PY

# ---- 12. shared-env hardening ---------------------------------------------
say "12. hardening (activate hooks + permissions)"
mkdir -p "$ENV_PREFIX/etc/conda/activate.d" "$ENV_PREFIX/etc/conda/deactivate.d"

cat > "$ENV_PREFIX/etc/conda/activate.d/zzz_no_user_site.sh" <<'EOF'
# Keep users' ~/.local site-packages (e.g. a mismatched torch) from shadowing the shared env.
export CHEMGRAPH_SAVED_PYTHONNOUSERSITE="${PYTHONNOUSERSITE-__unset__}"
export PYTHONNOUSERSITE=1
EOF
cat > "$ENV_PREFIX/etc/conda/deactivate.d/zzz_no_user_site.sh" <<'EOF'
if [ "${CHEMGRAPH_SAVED_PYTHONNOUSERSITE:-__unset__}" = "__unset__" ]; then unset PYTHONNOUSERSITE
else export PYTHONNOUSERSITE="$CHEMGRAPH_SAVED_PYTHONNOUSERSITE"; fi
unset CHEMGRAPH_SAVED_PYTHONNOUSERSITE
EOF

cat > "$ENV_PREFIX/etc/conda/activate.d/zzz_xpu.sh" <<'EOF'
# Aurora Intel-GPU (PVC): present 6 GPUs as 12 tiles consistently for torch XPU.
# Do NOT set ZE_AFFINITY_MASK / ONEAPI_DEVICE_SELECTOR here -- those are per-rank
# concerns; hardcoding them makes all MPI ranks pile onto tile 0.
export CHEMGRAPH_SAVED_ZE_FLAT="${ZE_FLAT_DEVICE_HIERARCHY-__unset__}"
export ZE_FLAT_DEVICE_HIERARCHY=FLAT
EOF
cat > "$ENV_PREFIX/etc/conda/deactivate.d/zzz_xpu.sh" <<'EOF'
if [ "${CHEMGRAPH_SAVED_ZE_FLAT:-__unset__}" = "__unset__" ]; then unset ZE_FLAT_DEVICE_HIERARCHY
else export ZE_FLAT_DEVICE_HIERARCHY="$CHEMGRAPH_SAVED_ZE_FLAT"; fi
unset CHEMGRAPH_SAVED_ZE_FLAT
EOF

chmod -R a+rX "$ENV_PREFIX" 2>/dev/null || echo "WARN: chmod partial (ok if not owner of some files)"
chmod a+rX "$SHARED" "$SRC_DIR" 2>/dev/null || true

say "BUILD COMPLETE $(date)"
echo "prefix: $ENV_PREFIX"
