#!/bin/bash
# ===========================================================================
# env_status.sh -- Write a small JSON snapshot describing the shared ChemGraph
# env (path, key package versions, git SHAs, latest auto-update result) for the
# ALCF Dashboard to render. Runs anywhere (login or compute); uses
# importlib.metadata only, so it needs no GPU and won't import torch.
#
# Writes two copies:
#   $SHARED/test_results/env_status.json      (world-readable, project record)
#   $HOME/.alcf-dashboard/envs/chemgraph.json (what the dashboard collector reads)
#
# auto_update.sh calls this at the end of every run; you can also run it by hand.
# ===========================================================================
set -o pipefail
SHARED=/lus/flare/projects/catalyst/world_shared/chemgraph
ENV_PREFIX=$SHARED/alcf_chemgraph_env
SRC=$SHARED/src
MINIFORGE=/opt/aurora/26.26.0/spack/unified/1.1.1/install/linux-x86_64/miniforge3-25.11.0-1-khkcc6i
OUT_SHARED=$SHARED/test_results/env_status.json
OUT_HOME=$HOME/.alcf-dashboard/envs/chemgraph.json

export PYTHONNOUSERSITE=1
ulimit -c 0 2>/dev/null || true
if [ "${CONDA_PREFIX:-}" != "$ENV_PREFIX" ]; then
  source "$MINIFORGE/etc/profile.d/conda.sh" || { echo "env_status: conda base missing" >&2; exit 1; }
  conda activate "$ENV_PREFIX" || { echo "env_status: activate failed" >&2; exit 1; }
fi

export CG_SHA=$(git -C "$SRC/ChemGraph" rev-parse --short HEAD 2>/dev/null)
export CG_DESC=$(git -C "$SRC/ChemGraph" describe --tags 2>/dev/null)
export IQC_SHA=$(git -C "$SRC/IQC" rev-parse --short HEAD 2>/dev/null)
export SHARED ENV_PREFIX

mkdir -p "$HOME/.alcf-dashboard/envs"

python - > "$OUT_SHARED" <<'PY'
import json, os, sys
import importlib.metadata as md

def ver(pkg):
    try: return md.version(pkg)
    except Exception: return None

versions = {"python": "%d.%d.%d" % sys.version_info[:3]}
for p in ["torch","numpy","chemgraph","iqc","mace-torch","tblite","pyscf","ase",
          "rdkit","pymatgen","parsl","ensemble-launcher","mpi4py"]:
    v = ver(p)
    if v: versions[p] = v

torch_v = versions.get("torch","")
backend = "Intel XPU (PVC)" if "xpu" in torch_v else ("CUDA" if "cu" in torch_v else "CPU")

au = None
try:
    with open(os.path.join(os.environ["SHARED"], "test_results", "auto_update_status.json")) as fh:
        s = json.load(fh)
    au = {k: s.get(k) for k in ("result","time","chemgraph_pytest","iqc_pytest","track_mode")}
except Exception:
    au = None

from datetime import datetime, timezone
obj = {
    "name": "ChemGraph",
    "machine": "aurora",
    "path": os.environ["ENV_PREFIX"],
    "activate": "source %s/activate_chemgraph.sh" % os.environ["SHARED"],
    "backend": backend,
    "versions": versions,
    "git": {"ChemGraph": os.environ.get("CG_DESC") or os.environ.get("CG_SHA"),
            "IQC": os.environ.get("IQC_SHA")},
    "auto_update": au,
    "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
}
print(json.dumps(obj, indent=2))
PY

rc=$?
if [ $rc -ne 0 ] || [ ! -s "$OUT_SHARED" ]; then echo "env_status: generation failed" >&2; exit 1; fi
cp -f "$OUT_SHARED" "$OUT_HOME"
chmod a+r "$OUT_SHARED" 2>/dev/null || true
echo "env_status: wrote $OUT_SHARED and $OUT_HOME"
