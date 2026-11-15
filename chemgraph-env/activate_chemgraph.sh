#!/bin/bash
# ===========================================================================
# activate_chemgraph.sh -- Activate the shared ChemGraph env on ALCF Aurora.
#
#   source /lus/flare/projects/catalyst/world_shared/chemgraph/activate_chemgraph.sh
#
# Bootstraps conda from the Aurora frameworks miniforge base and activates the
# shared prefix. We deliberately do NOT `module load frameworks` (that would
# activate frameworks' own python 3.12 env and shadow this prefix). The env's
# etc/conda/activate.d hooks set PYTHONNOUSERSITE=1 and ZE_FLAT_DEVICE_HIERARCHY=FLAT
# automatically. Run real GPU work on a COMPUTE node (login nodes have no PVC).
# ===========================================================================

# torch's XPU runtime segfaults during interpreter *shutdown* on nodes with no PVC
# (e.g. login nodes) -- the work completes first, but it dumps a multi-GB core.
# Suppress cores so a login-node `import torch` can't litter the shared filesystem.
ulimit -c 0 2>/dev/null || true

# ALCF proxy (needed only if you download; harmless otherwise)
export http_proxy=http://proxy.alcf.anl.gov:3128
export https_proxy=http://proxy.alcf.anl.gov:3128
# Bypass the proxy for loopback so local services (MCP servers, parsl/ensemble
# workers, ollama, and ChemGraph's localhost test mocks) aren't routed through
# Squid -> ERR_CONNECT_FAIL. Without this, tests/test_iri_submit_guard.py fails.
export no_proxy="127.0.0.1,localhost,::1,.alcf.anl.gov"
export NO_PROXY="$no_proxy"

# Thread caps: avoid OpenBLAS pthread_create failures on shared nodes.
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-8}"
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-8}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-8}"

_CG_MINIFORGE=/opt/aurora/26.26.0/spack/unified/1.1.1/install/linux-x86_64/miniforge3-25.11.0-1-khkcc6i
_CG_PREFIX=/lus/flare/projects/catalyst/world_shared/chemgraph/alcf_chemgraph_env

if [ ! -f "$_CG_MINIFORGE/etc/profile.d/conda.sh" ]; then
    echo "activate_chemgraph: conda base not found at $_CG_MINIFORGE" >&2
    echo "  (frameworks module release may have changed; update _CG_MINIFORGE)" >&2
    return 1 2>/dev/null || exit 1
fi

# conda's Intel-MPI activate.d hooks reference unbound vars; disable nounset
# during activation (restore the caller's setting afterward).
_cg_had_u=0; case "$-" in *u*) _cg_had_u=1; set +u;; esac
source "$_CG_MINIFORGE/etc/profile.d/conda.sh"
conda activate "$_CG_PREFIX" || { echo "activate_chemgraph: failed to activate $_CG_PREFIX" >&2; [ "$_cg_had_u" = 1 ] && set -u; return 1 2>/dev/null || exit 1; }
[ "$_cg_had_u" = 1 ] && set -u; unset _cg_had_u

echo "ChemGraph env active: $_CG_PREFIX"
python - <<'PY' 2>/dev/null
import torch
dev = "xpu" if getattr(torch,"xpu",None) and torch.xpu.is_available() else "cpu"
print(f"  torch {torch.__version__}  device={dev}"
      + (f"  tiles={torch.xpu.device_count()}" if dev=="xpu" else "  (no PVC visible -- login node?)"))
PY
