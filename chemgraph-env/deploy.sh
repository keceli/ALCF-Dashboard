#!/bin/bash
# ===========================================================================
# deploy.sh -- Copy the env scripts and machines/<machine>.conf (as
# machine.conf) to the machine's shared directory, from a workstation:
#   chemgraph-env/deploy.sh polaris sophia crux aurora
# The SSH host is the machine name (an ~/.ssh/config alias).
# ===========================================================================
set -eo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
FILES="cg_common.sh auto_update.sh auto_update.pbs auto_update_verify.py trigger_auto_update.sh env_status.sh"
for m in "$@"; do
  conf=$HERE/machines/$m.conf
  [ -f "$conf" ] || { echo "no $conf" >&2; exit 1; }
  # shellcheck disable=SC1090
  eval "$(source "$conf"; echo "SHARED=$SHARED ENV_KIND=$ENV_KIND")"
  extra=""; [ "$ENV_KIND" = uv ] && extra="build_env_uv.sh"
  stage=$(mktemp -d)
  # shellcheck disable=SC2086
  (cd "$HERE" && cp $FILES $extra "$stage/") && cp "$conf" "$stage/machine.conf"
  chmod a+r "$stage"/*
  echo "$m: $SHARED"
  tar -C "$stage" -cf - . | ssh -o BatchMode=yes "$m" "mkdir -p '$SHARED' && tar -C '$SHARED' --no-same-owner -xf - && chmod a+rx '$SHARED'"
  rm -rf "$stage"
done
