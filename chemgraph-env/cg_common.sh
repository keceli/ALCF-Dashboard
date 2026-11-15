# ===========================================================================
# cg_common.sh -- helpers shared by the ChemGraph env scripts. Source it after
# machine.conf (a copy of machines/<machine>.conf), which sets MACHINE, SHARED,
# SRC, ENV_KIND (conda|uv), ENV_PREFIX, DEVICE (cuda|xpu|cpu), TORCH_SPEC, ...
# ===========================================================================
RES=$SHARED/test_results
CONSTRAINTS=$SHARED/constraints.txt
REPOS="ChemGraph IQC"
TRACK_MODE="${TRACK_MODE:-main}"
UV="${UV:-$(command -v uv || echo "$HOME/.local/bin/uv")}"
export UV_PYTHON_INSTALL_DIR UV_CACHE_DIR
export UV_LINK_MODE=copy            # the env must not share inodes with the cache

cg_proxy() {
  export http_proxy=http://proxy.alcf.anl.gov:3128 https_proxy=http://proxy.alcf.anl.gov:3128
  # Loopback bypasses Squid: ChemGraph's tests start mock servers on 127.0.0.1.
  export no_proxy="127.0.0.1,localhost,::1,.alcf.anl.gov" NO_PROXY="127.0.0.1,localhost,::1,.alcf.anl.gov"
}

cg_threads() {
  export OMP_NUM_THREADS=${THREADS:-8} OPENBLAS_NUM_THREADS=${THREADS:-8} MKL_NUM_THREADS=${THREADS:-8}
}

# Activate the env (conda prefix or uv venv).
cg_activate() {
  export PYTHONNOUSERSITE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
  if [ "$ENV_KIND" = uv ]; then
    [ -x "$ENV_PREFIX/bin/python" ] || return 1
    # shellcheck disable=SC1091
    source "$ENV_PREFIX/bin/activate"
  else
    [ "${CONDA_PREFIX:-}" = "$ENV_PREFIX" ] && return 0
    # shellcheck disable=SC1090
    source "$CONDA_SH" || return 1
    conda activate "$ENV_PREFIX"
  fi
}

# pip for the env: `uv pip` for a uv venv, the env's own pip for conda.
cg_pip() {
  if [ "$ENV_KIND" = uv ]; then
    local sub=$1; shift
    "$UV" pip "$sub" --python "$ENV_PREFIX/bin/python" "$@"
  else
    python -m pip "$@"
  fi
}

cg_uninstall() {
  if [ "$ENV_KIND" = uv ]; then cg_pip uninstall "$@"; else cg_pip uninstall -y "$@"; fi
}

# Reinstall local checkouts editable without touching their dependencies, so
# the package metadata (version) follows the source.
cg_editable() {
  local d
  for d in "$@"; do
    if [ "$ENV_KIND" = uv ]; then cg_pip install --no-deps --reinstall -e "$d"
    else cg_pip install --no-deps -e "$d"; fi
  done
}

# Pin torch: every install resolves against this, so nothing can swap the
# hardware build of torch. numpy is not constrained here: in the conda envs
# matscipy needs numpy<2 while ChemGraph pins 2.2.6, so cg_numpy re-pins it.
cg_constraints() {
  echo "$TORCH_SPEC" > "$CONSTRAINTS"
}

# Re-pin numpy to NUMPY_PIN (machine.conf) after installs; no-op when unset.
cg_numpy() {
  [ -n "$NUMPY_PIN" ] || return 0
  # shellcheck disable=SC2086
  cg_pip install -c "$CONSTRAINTS" $INDEX_ARGS "numpy==$NUMPY_PIN"
}

# Print name==version for each package a dry run of `install "$@"` would
# install; exit status is the dry run's.
cg_would_install() {
  local out rc
  out=$(cg_pip install --dry-run "$@" 2>&1); rc=$?
  if [ "$ENV_KIND" = uv ]; then
    echo "$out" | sed -n 's/^ + //p'
  else
    echo "$out" | sed -n 's/^Would install //p' | tr ' ' '\n' | sed -E 's/-([0-9][^-]*)$/==\1/'
  fi
  [ $rc -eq 0 ] || echo "$out" | tail -5 >&2
  return $rc
}

# Lines of a would-install list (stdin) that would replace torch with another
# build, or pull CUDA libraries onto a machine without NVIDIA GPUs.
cg_gate() {
  if [ "$DEVICE" = cuda ]; then grep -Ei '^torch=='; else grep -Ei '^(torch|nvidia-[a-z0-9-]+)=='; fi
}

# "<ref> <sha>" the repo at $1 should be on, per TRACK_MODE (main or release).
cg_target() {
  local tag
  if [ "$TRACK_MODE" = release ]; then
    tag=$(git -C "$1" describe --tags --abbrev=0 origin/main 2>/dev/null)
    if [ -n "$tag" ]; then echo "$tag $(git -C "$1" rev-list -n1 "$tag")"; return; fi
  fi
  echo "origin/main $(git -C "$1" rev-parse origin/main)"
}
