#!/usr/bin/env bash
set -euo pipefail
_mt_dir() {
  case "$1" in
    */*) printf '%s\n' "${1%/*}" ;;
    *) printf '.\n' ;;
  esac
}
source="${BASH_SOURCE[0]}"
while [[ -L "$source" ]]; do
  dir="$(cd -P "$(_mt_dir "$source")" && pwd)"
  target="$(readlink "$source")"
  if [[ "$target" == /* ]]; then
    source="$target"
  else
    source="$dir/$target"
  fi
done
repo_root="$(cd -P "$(_mt_dir "$source")" && pwd)"
if [[ "${1:-}" == "update" ]]; then
  shift
  exec python3 "$repo_root/scripts/update.py" "$@"
fi
exec python3 "$repo_root/scripts/mt_main.py" "$@"
