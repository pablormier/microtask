# Sourced by demo.tape from the repository root before recording.
source_root="$(pwd -P)"
if [ ! -x "$source_root/mt" ] || [ ! -f "$source_root/scripts/workspace.py" ]; then
  echo "setup.sh: run from the microtask repository root" >&2
  return 1
fi

demo_root="$(mktemp -d)" || return 1
trap 'rm -rf -- "$demo_root"' EXIT
cd "$demo_root" || return 1
export MT_WORKSPACE="$(pwd -P)/workspace"
export MT_USERNAME=alex
unset MT_PROJECT MT_TOPIC
python3 "$source_root/scripts/workspace.py" create "$MT_WORKSPACE" || return 1
printf 'rules:\n  auto_sync: false\n' > "$MT_WORKSPACE/settings.local.yaml" || return 1

# Omit machine-specific paths from the recording.
mt() (
  set -o pipefail
  "$source_root/mt" "$@" 2>&1 | sed -e '/^Workspace: /d' -e '/^Application: /d' \
    -e "s|^${MT_WORKSPACE%/workspace}/|./|"
)
export PS1='\[\e[38;5;141m\]❯\[\e[0m\] '
clear
