# ChemGraph shared environment — ALCF Aurora

Scripts and operating instructions for the **shared [ChemGraph](https://github.com/Autonomous-Scientific-Agents/ChemGraph)
conda environment on ALCF Aurora** (Intel Data Center GPU Max / Ponte Vecchio; PyTorch on the
**Intel XPU** backend). This folder mirrors what is deployed at:

```
/lus/flare/projects/catalyst/world_shared/chemgraph/
```

Its live status (path, package versions, last auto-update result) is published on the
**ALCF Dashboard** in this repo — see [Dashboard integration](#dashboard-integration) below.

---

## Quick start (users)

Real GPU work runs on a **compute node** (login nodes have no PVC). From a compute node:

```bash
source /lus/flare/projects/catalyst/world_shared/chemgraph/activate_chemgraph.sh
python -c "import chemgraph, torch; print(chemgraph.__version__, torch.__version__, torch.xpu.is_available())"
```

Interactive node: `qsub -I -q debug -l select=1 -l walltime=00:60:00 -A ChemGraph -l filesystems=home:flare`

On a login node `import torch` works but `torch.xpu.is_available()` is `False` (no GPU) — expected.

## What's installed (as of the last update)

python 3.11 · **torch 2.12.1+xpu** · numpy 1.26.4 (pinned <2 for matscipy) · chemgraph 0.7.0 ·
iqc 0.1.0 · mace-torch 0.3.16 · tblite 0.7.0 · pyscf 2.14.0 · nwchem · ase/rdkit/pymatgen ·
parsl + ensemble-launcher. 6 GPUs present as **12 tiles** (`ZE_FLAT_DEVICE_HIERARCHY=FLAT`).
The authoritative current versions are in the dashboard / `test_results/env_status.json`.

---

## Scripts in this folder

| File | Purpose |
|---|---|
| `activate_chemgraph.sh` | user entry point: conda bootstrap + activate + proxy + `no_proxy` + XPU env |
| `build_env_aurora.sh` | reproducible build (torch-xpu first, guarded install, numpy re-pin, hardening) |
| `constraints.txt` | pip constraint `torch==2.12.1+xpu` (guards every pip call vs. a CUDA wheel) |
| `probe_xpu.pbs` | finds which pip torch-xpu wheel actually imports + sees the PVC on a compute node |
| `test_env.pbs` | compute-node verification: imports + XPU + tblite/pyscf/nwchem + ChemGraph/IQC pytest |
| `auto_update.sh` | **safe auto-updater**: detect new upstream → install → verify → promote or roll back |
| `auto_update_verify.py` | core verify (imports + XPU + calculators) used by `auto_update.sh` |
| `auto_update.pbs` | PBS wrapper that runs `auto_update.sh` on a compute node |
| `trigger_auto_update.sh` | login-node entry point your off-site cron SSHes to (submits the job, dedupes) |
| `env_status.sh` | writes `env_status.json` (path + versions + git + auto-update status) for the dashboard |
| `PORTABLE_INSTALL.md` | cross-hardware build guide (NVIDIA/Intel/AMD/CPU) this Aurora build follows |

> These scripts contain the absolute Aurora deployment path and the Aurora `frameworks`
> miniforge location. They are the source of truth for the deployed env, kept in version
> control here; edits should be copied back to the shared directory (or vice-versa).

---

## Building from scratch

On a **login node** (proxy works, no walltime cap; no GPU needed to install wheels):
```bash
cd /lus/flare/projects/catalyst/world_shared/chemgraph
TORCH_XPU_VER=2.12.1 bash build_env_aurora.sh 2>&1 | tee test_results/build.log
qsub test_env.pbs        # verify on a compute node (-A ChemGraph)
```
Key invariants the script enforces: install **torch-xpu first** so mace-torch reuses it; a
**dry-run gate** aborts if resolution would pull a CUDA torch / numpy≥2; **re-pin numpy==1.26.4**
after dep installs; install **ChemGraph & IQC editable** so bundled data + pyproject resolve.

### Why torch 2.12.1+xpu
Older pip XPU wheels (2.8.0/2.9.1) **fail to import on Aurora** — their bundled `libsycl.so.8`
needs `LIBUR_LOADER_0.11/0.12` symbols the system oneAPI loader doesn't provide. **2.12.1+xpu**
imports cleanly and reports `xpu.is_available()` with 12 tiles. Re-run `probe_xpu.pbs` before
bumping torch. (The Aurora `frameworks` module ships python 3.12 + numpy 2.2.6, which conflict
with ChemGraph's pins — so we build a fresh python 3.11 prefix instead of using it.)

---

## Automatic updates

`auto_update.sh` safely updates ChemGraph/IQC when new upstream code lands: detect change →
snapshot dep versions → guarded install → **verify on a GPU node** (core checks + both pytest
suites) → **promote on 0 failures, or roll back** (reverts git *and* the exact dep bumps, re-pins
numpy, re-checks imports). Lock-guarded and idempotent; a no-op when nothing is new. It writes
`test_results/auto_update.log`, `auto_update_status.json`, and refreshes `env_status.json`.

Config via env vars (passed through by `trigger_auto_update.sh`):
`TRACK_MODE=main|release` (default `main` = follow `origin/main`), `ON_FAIL=rollback|keep`,
`CHEMGRAPH_EXTRAS=parsl,ensemble_launcher,test`, `FORCE=1`.

### Scheduling from an off-site machine (recommended)
ALCF has **no user cron** on Aurora, so the durable scheduler lives on a machine you control
(laptop/workstation/VM) that can SSH to Aurora:

```sshconfig
# ~/.ssh/config — persistent master so cron skips the ALCF MFA prompt
Host aurora
    HostName aurora.alcf.anl.gov
    User <your_alcf_user>
    ControlMaster auto
    ControlPath ~/.ssh/cm-%r@%h:%p
    ControlPersist 168h
```
```bash
ssh -fN aurora          # authenticate once (MFA); cron reuses this socket
```
```cron
17 7 * * *  ssh aurora 'bash /lus/flare/projects/catalyst/world_shared/chemgraph/trigger_auto_update.sh' >> ~/chemgraph_autoupdate.log 2>&1
```
Re-run `ssh -fN aurora` after the master expires or the machine reboots. (A self-resubmitting
PBS chain is *not* recommended — PBS queue limits silently break it; ALCF **GitLab CI scheduled
pipelines** are the heavier ALCF-native alternative.)

---

## Dashboard integration

The ALCF Dashboard collector reads **`~/.alcf-dashboard/envs/*.json`** over SSH and renders an
**Environments** card showing each env's path, versions, git SHAs, and latest auto-update status.
`env_status.sh` writes this file (and a world-readable copy at `test_results/env_status.json`);
`auto_update.sh` calls it on every run, so the dashboard reflects the live state. To register a
different env, drop its own `env_status.json` into `~/.alcf-dashboard/envs/` on the target machine.
