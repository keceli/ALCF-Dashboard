#!/usr/bin/env python
# Core verify for auto_update.sh: imports + the machine's torch device + two
# calculators. DEVICE (cuda|xpu|cpu, from machine.conf) selects the device check.
# Exits non-zero on any import failure, missing device, or a calculator result
# that is wildly off. (pytest suites are run separately by auto_update.sh.)
import importlib, os, sys

bad = []
for m in ["numpy","ase","rdkit","pymatgen","mace","tblite.ase","pyscf","chemgraph","iqc","mpi4py"]:
    try:
        importlib.import_module(m)
    except Exception as e:
        bad.append((m, repr(e)[:120]))
if bad:
    for m, e in bad: print("IMPORT FAIL", m, "->", e)
    sys.exit(1)

import numpy, torch
device = os.environ.get("DEVICE", "cpu")
print("numpy", numpy.__version__, "| torch", torch.__version__, "| device", device)
pin = os.environ.get("NUMPY_PIN")
if pin and numpy.__version__ != pin:
    print("WARN: numpy is not", pin)
if device == "xpu":
    if not (getattr(torch, "xpu", None) and torch.xpu.is_available()):
        print("XPU NOT AVAILABLE"); sys.exit(1)
    print("xpu tiles:", torch.xpu.device_count())
elif device == "cuda":
    if not torch.cuda.is_available():
        print("CUDA NOT AVAILABLE"); sys.exit(1)
    print("cuda devices:", torch.cuda.device_count(), torch.cuda.get_device_name(0))
_ = torch.ones(8, device=device).sum().item()

from ase.build import molecule
from tblite.ase import TBLite
a = molecule("H2O"); a.calc = TBLite(method="GFN2-xTB", verbosity=0)
e = a.get_potential_energy()
print("tblite H2O (eV):", round(e, 4))
if not (-139.0 < e < -137.0):
    print("tblite energy out of range"); sys.exit(1)

from pyscf import gto, scf
mol = gto.M(atom="O 0 0 0; H 0 0 0.958; H 0 0.926 -0.24", basis="sto-3g", verbose=0)
ehf = scf.RHF(mol).kernel()
print("pyscf RHF/STO-3G H2O (Ha):", round(ehf, 4))
if not (-75.1 < ehf < -74.8):
    print("pyscf energy out of range"); sys.exit(1)
print("CORE VERIFY OK")
