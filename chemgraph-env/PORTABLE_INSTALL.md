# Building the ChemGraph environment on other machines

This guide generalizes the Polaris build (`build_env.sh` / `BUILD_NOTES.md`) to
other machines: NVIDIA-GPU, Intel-GPU, AMD-GPU, and CPU-only (laptops, clusters
without GPUs). **ChemGraph is the priority**; tblite / PySCF / nwchem / IQC /
ExaChem are optional and must not break it.

The only part that is genuinely hardware-dependent is **PyTorch** (pulled in by
`mace-torch`). Everything else (ASE, RDKit, pymatgen, tblite, nwchem, PySCF,
langchain/langgraph, …) is portable across Linux/macOS.

Reference versions from the working Polaris build:

| package | version | | package | version |
|---|---|---|---|---|
| python | 3.11 | | numpy | 1.26.4 (pinned below 2 by matscipy) |
| torch | 2.13.0 | | tblite | 0.7.0 |
| mace-torch | 0.3.16 | | pyscf | 2.14.0 |
| ase | 3.25.0 | | rdkit | 2025.3.3 |
| chemgraph | 0.6.0 | | iqc | 0.1.0 |

---

## 0. Prerequisites

- **conda/mamba.** Use whatever the site provides (`module load conda`,
  `module load frameworks`, …) or install [Miniforge](https://github.com/conda-forge/miniforge).
- **git**, and outbound internet. On ALCF systems set the proxy first:
  ```bash
  export http_proxy=http://proxy.alcf.anl.gov:3128
  export https_proxy=http://proxy.alcf.anl.gov:3128
  ```
- A C/Fortran compiler + system MPI **only if** you want `mpi4py` built against
  the site MPI (optional; IQC feature).
- **Do heavy installs on a compute node, not a login node**, where possible.

Pick where the env lives and export it once:
```bash
export ENV_PREFIX=$HOME/envs/alcf_chemgraph_env   # or a shared project path
export SRC_DIR=$HOME/chemgraph_src                 # for the ChemGraph/IQC checkouts
mkdir -p "$SRC_DIR"
```

---

## 1. Choose your PyTorch flavor (the one hardware decision)

Install **torch first**, from the index matching your hardware, so that the
later `mace-torch` install reuses it instead of pulling the default CUDA wheel.
Always confirm the exact command with the selector at
<https://pytorch.org/get-started/locally/>.

| Hardware | Install command (run inside the env, Step 2) | Verify device |
|---|---|---|
| **NVIDIA GPU (CUDA)** | `pip install torch --index-url https://download.pytorch.org/whl/cu124` (match your driver: `cu121`/`cu126`/`cu128`/…) | `torch.cuda.is_available()` |
| **Intel GPU (XPU)** | `pip install torch --index-url https://download.pytorch.org/whl/xpu` — on **ALCF Aurora** prefer `module load frameworks` (ships XPU torch); see notes | `torch.xpu.is_available()` |
| **AMD GPU (ROCm)** | `pip install torch --index-url https://download.pytorch.org/whl/rocm6.2` (match your ROCm) | `torch.cuda.is_available()` (ROCm reports through the cuda API) |
| **No GPU / CPU-only** | `pip install torch --index-url https://download.pytorch.org/whl/cpu` | CPU only |
| **macOS (Apple Silicon)** | `pip install torch` (MPS backend included) | `torch.backends.mps.is_available()` |

> `mace-torch>=0.3.16` only requires "some" recent torch, so any of the above
> (torch ≥ 2.1) satisfies it. Installing torch first is what keeps pip from
> silently replacing your CPU/XPU/ROCm build with the CUDA one.

---

## 2. Create the env and install ChemGraph (priority)

```bash
# 2a. Get the sources
cd "$SRC_DIR"
git clone https://github.com/Autonomous-Scientific-Agents/ChemGraph.git
git clone https://github.com/Autonomous-Scientific-Agents/IQC.git      # optional

# 2b. Base env: python + the compiled chem tools that come from conda-forge.
#     nwchem/tblite are linux/osx conda-forge packages (skip any not available
#     on your platform -- they are optional).
conda create -y -p "$ENV_PREFIX" -c conda-forge \
    python=3.11 pip numpy=1.26.4 pandas=2.2.3 pytest=8.4.1 rich=14.1.0 toml=0.10.2 \
    tblite tblite-python nwchem
conda activate "$ENV_PREFIX"

# 2c. Install the hardware-correct torch FIRST (from the table in Step 1). e.g.:
pip install torch --index-url https://download.pytorch.org/whl/cpu

# 2d. ChemGraph's remaining Python deps. Reuse the tested pin set from its
#     environment.yml, minus the conda-provided packages and minus torch (already
#     installed). The simplest reliable way is to let pip read the pyproject:
pip install ./ChemGraph            # resolves langchain/langgraph/mace-torch/etc.
# then re-pin numpy in case a dep bumped it (matscipy needs <2):
pip install "numpy==1.26.4"

# 2e. Install ChemGraph itself editable so its bundled data files + pyproject
#     resolve (a plain wheel omits them and breaks graspa/version tests):
pip install --no-deps -e ./ChemGraph

# 2f. Fixes needed on top of ChemGraph's deps:
pip install --no-deps "langgraph-checkpoint-sqlite==3.1.1"
pip install aiosqlite sqlite-vec
```

> **Note on `numpy`.** ChemGraph's `pyproject.toml` says `numpy==2.2.6`, but its
> own `mace-torch → matscipy` requires `numpy<2`, so the working, tested state is
> **numpy 1.26.4**. Keep it. `pip check` will report the 2.2.6 mismatch — expected.

At this point ChemGraph works. Verify before adding optional tools:
```bash
python -c "import chemgraph, torch, ase, rdkit, tblite.ase; print('ChemGraph OK', chemgraph.__version__)"
```

---

## 3. Optional chemistry tools

Each is optional — if one fails on your platform, skip it; ChemGraph still works.

```bash
# PySCF (self-contained pip wheel; Linux/macOS)
pip install pyscf

# IQC (--no-deps to protect ChemGraph's pins) + its extra runtime deps
pip install --no-deps -e ./IQC
pip install argcomplete psutil nglview openpyxl xlrd
# mpi4py: laptop/simple -> conda-forge; HPC multi-node -> build against site MPI:
conda install -y --freeze-installed -c conda-forge mpi4py
#   (HPC alternative)  MPICC=$(which mpicc) pip install --no-binary=mpi4py --force-reinstall mpi4py
```

### ExaChem (not portable via conda/pip)
ExaChem is a compiled MPI application built with **TAMM**, with a GPU backend
specific to the machine:
- **NVIDIA:** CUDA backend
- **Intel GPU:** SYCL/DPC++ (oneAPI) backend
- **AMD:** HIP/ROCm backend
- **CPU:** CPU-only build

It cannot be pip/conda-installed. Build it per-machine from
<https://github.com/ExaChem/exachem> (and TAMM
<https://github.com/NWChemEx/TAMM>), then expose the binary via a `setup` script
like Polaris's `exachem/setup_exachem.sh`. On Polaris the prebuilt GPU tree is
shipped in this repo; on other machines, rebuild. (A prebuilt binary is **not**
portable — it links against a specific MPI/GPU-runtime ABI.)

---

## 4. Shared-env hardening (do this for any multi-user install)

Prevent users' personal `~/.local` packages (e.g. a CUDA-mismatched torch) from
shadowing the shared env:
```bash
mkdir -p "$ENV_PREFIX/etc/conda/activate.d" "$ENV_PREFIX/etc/conda/deactivate.d"
cat > "$ENV_PREFIX/etc/conda/activate.d/zzz_no_user_site.sh" <<'EOF'
export CHEMGRAPH_SAVED_PYTHONNOUSERSITE="${PYTHONNOUSERSITE-__unset__}"
export PYTHONNOUSERSITE=1
EOF
cat > "$ENV_PREFIX/etc/conda/deactivate.d/zzz_no_user_site.sh" <<'EOF'
if [ "${CHEMGRAPH_SAVED_PYTHONNOUSERSITE:-__unset__}" = "__unset__" ]; then
    unset PYTHONNOUSERSITE
else export PYTHONNOUSERSITE="$CHEMGRAPH_SAVED_PYTHONNOUSERSITE"; fi
unset CHEMGRAPH_SAVED_PYTHONNOUSERSITE
EOF
```
Also make the tree group/other-readable if sharing:
`chmod -R a+rX "$ENV_PREFIX"` (mind the umask on the parent directories).

---

## 5. Verify (hardware-agnostic)

Run on a **compute node** with the GPU (if any) visible. Cap threads first to
avoid login-node `OpenBLAS pthread_create` errors:
```bash
export OMP_NUM_THREADS=8 OPENBLAS_NUM_THREADS=8 MKL_NUM_THREADS=8 PYTHONNOUSERSITE=1
```
```bash
python - <<'PY'
import importlib, torch
for m in ["numpy","ase","rdkit","pymatgen","mace","tblite.ase","pyscf","chemgraph","iqc"]:
    try: importlib.import_module(m); print("OK  ", m)
    except Exception as e: print("SKIP/FAIL", m, "->", repr(e)[:100])
# device report
dev = ("cuda" if torch.cuda.is_available()
       else "xpu" if getattr(torch,"xpu",None) and torch.xpu.is_available()
       else "mps" if torch.backends.mps.is_available()
       else "cpu")
print("torch", torch.__version__, "device:", dev)
# a real calc (device-independent)
from ase.build import molecule
from tblite.ase import TBLite
a = molecule("H2O"); a.calc = TBLite(method="GFN2-xTB", verbosity=0)
print("H2O GFN2-xTB (eV):", round(a.get_potential_energy(), 4))   # ~ -137.97
PY

# test suites (LLM/Globus tests skip without API keys)
( cd "$SRC_DIR/ChemGraph" && python -m pytest -q ) || true
( cd "$SRC_DIR/IQC"       && python -m pytest -q ) || true
```
Expected: ChemGraph and IQC test suites pass (LLM/Globus tests skipped), and the
tblite H₂O energy ≈ −137.97 eV on any hardware.

---

## 6. Machine-specific notes

- **ALCF Polaris (NVIDIA A100).** The canonical build. Bootstrap conda with
  `source /soft/applications/conda/2025-09-25/mconda3/etc/profile.d/conda.sh`
  (the `conda` module only defines a shell function in interactive shells). Use
  a CUDA torch. See `build_env.sh` in this repo.
- **ALCF Aurora (Intel Data Center GPU Max / PVC).** `module use /soft/modulefiles
  && module load frameworks` provides a conda base with an **XPU-enabled torch**;
  build the env on top of it, or install `torch --index-url .../whl/xpu`. Same
  proxy as Polaris. ExaChem needs the SYCL/DPC++ (oneAPI) build.
- **Generic CPU cluster / laptop.** Use the CPU torch wheel. `nwchem` and
  `tblite` still come from conda-forge; `mpi4py` from conda-forge is fine for
  single-node use. No GPU code paths are required by ChemGraph.
- **No conda modules available.** Install Miniforge, then follow Steps 1–5 as-is.

## 7. Gotchas recap
- Install **torch first** for your hardware, then everything else.
- Keep **numpy 1.26.4** (matscipy pin); re-pin after dep installs if bumped.
- Install ChemGraph/IQC **editable** (`-e`), not as wheels.
- Set **`PYTHONNOUSERSITE=1`** for shared installs.
- **ExaChem** must be rebuilt per machine/backend; binaries are not portable.
- On HPC, build **mpi4py** against the site MPI for multi-node runs.
