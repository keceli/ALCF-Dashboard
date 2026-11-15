#!/bin/bash
# ===========================================================================
# env_status.sh -- Write a small JSON snapshot describing the shared ChemGraph
# env (path, key package versions, git SHAs, latest auto-update result) for the
# ALCF Dashboard to render. Runs anywhere (login or compute); uses
# importlib.metadata only, so it needs no GPU and won't import torch.
#
# Writes two copies:
#   $SHARED/test_results/env_status.json                (world-readable)
#   $HOME/.alcf-dashboard/envs/chemgraph-<machine>.json (read by the collector)
# Polaris, Sophia and Crux share /home, so the file name carries the machine.
#
# auto_update.sh calls this at the end of every run, and trigger_auto_update.sh
# on every check; you can also run it by hand.
# ===========================================================================
set -o pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck disable=SC1091
source "$HERE/machine.conf" || { echo "env_status: no machine.conf in $HERE" >&2; exit 1; }
# shellcheck disable=SC1091
source "$HERE/cg_common.sh"
OUT_SHARED=$RES/env_status.json
OUT_HOME=$HOME/.alcf-dashboard/envs/chemgraph-$MACHINE.json

ulimit -c 0 2>/dev/null || true
cg_activate || { echo "env_status: cannot activate $ENV_PREFIX" >&2; exit 1; }

CG_SHA=$(git -C "$SRC/ChemGraph" rev-parse --short HEAD 2>/dev/null)
CG_DESC=$(git -C "$SRC/ChemGraph" describe --tags 2>/dev/null)
IQC_SHA=$(git -C "$SRC/IQC" rev-parse --short HEAD 2>/dev/null)
LAST_CHECK=$(cat "$RES/last_check" 2>/dev/null)
export CG_SHA CG_DESC IQC_SHA LAST_CHECK RES MACHINE DEVICE ENV_PREFIX ENV_KIND ACTIVATE

mkdir -p "$RES" "$HOME/.alcf-dashboard/envs"

python - > "$OUT_SHARED.tmp" <<'PY'
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

backend = {"xpu": "Intel XPU (PVC)", "cuda": "CUDA (NVIDIA A100)", "cpu": "CPU"}.get(
    os.environ["DEVICE"], os.environ["DEVICE"])

try:
    with open(os.path.join(os.environ["RES"], "auto_update_status.json")) as fh:
        s = json.load(fh)
    au = {k: s.get(k) for k in ("result","time","chemgraph_pytest","iqc_pytest","track_mode")}
except Exception:
    au = None
if os.environ.get("LAST_CHECK"):
    au = dict(au or {}, last_check=os.environ["LAST_CHECK"])

from datetime import datetime, timezone
obj = {
    "name": "ChemGraph",
    "machine": os.environ["MACHINE"],
    "path": os.environ["ENV_PREFIX"],
    "activate": "source %s" % os.environ["ACTIVATE"],
    "backend": backend,
    "manager": "uv" if os.environ["ENV_KIND"] == "uv" else "conda",
    "versions": versions,
    "git": {"ChemGraph": os.environ.get("CG_DESC") or os.environ.get("CG_SHA"),
            "IQC": os.environ.get("IQC_SHA")},
    "auto_update": au,
    "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
}
print(json.dumps(obj, indent=2))
PY

rc=$?
if [ $rc -ne 0 ] || [ ! -s "$OUT_SHARED.tmp" ]; then rm -f "$OUT_SHARED.tmp"; echo "env_status: generation failed" >&2; exit 1; fi
mv -f "$OUT_SHARED.tmp" "$OUT_SHARED"
cp -f "$OUT_SHARED" "$OUT_HOME"
chmod a+r "$OUT_SHARED" 2>/dev/null || true
echo "env_status: wrote $OUT_SHARED and $OUT_HOME"
