#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
if [[ "${1:-}" == "update" ]]; then
  if [[ -n "${MT_WORKSPACE:-}" && "${MT_WORKSPACE%/}" != "$repo_root" ]]; then
    echo "error: mt update changes the application at $repo_root, not the data-only workspace" \
      "$MT_WORKSPACE; run it without -R or MT_WORKSPACE" >&2
    exit 2
  fi
  shift
  exec python3 "$repo_root/scripts/update.py" "$@"
fi
exec python3 "$repo_root/scripts/tasklib.py" "$@"
