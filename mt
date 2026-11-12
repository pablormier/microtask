#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
if [[ "${1:-}" == "update" ]]; then
  shift
  exec python3 "$repo_root/scripts/update.py" "$@"
fi
exec python3 "$repo_root/scripts/tasklib.py" "$@"
