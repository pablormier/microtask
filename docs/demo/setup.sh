# Sourced by demo.tape from the repository root before recording. Builds a
# throwaway workspace from this checkout plus the bundled example cards, so the
# recording never touches your real cards.
source_root="$(pwd -P)"
if [ ! -x "$source_root/mt" ] || [ ! -d "$source_root/examples/cards" ]; then
  echo "setup.sh: run from the microtask repository root" >&2
  return 1
fi
# MT_DEMO_ROOT picks a fixed, disposable folder; it is replaced on each run.
if [ -n "${MT_DEMO_ROOT:-}" ]; then
  rm -rf "$MT_DEMO_ROOT"
  mkdir -p "$MT_DEMO_ROOT"
  demo_root="$(cd "$MT_DEMO_ROOT" && pwd -P)"
else
  demo_root="$(cd "$(mktemp -d)" && pwd -P)/todo"
  mkdir -p "$demo_root"
fi
rsync -a --exclude .git --exclude .mt --exclude __pycache__ --exclude .DS_Store \
  --exclude .playwright-mcp --exclude 'cards/*' "$source_root/" "$demo_root/"
cp "$source_root"/examples/cards/*.md "$demo_root/cards/"

cd "$demo_root"
export PATH="$demo_root:$PATH"
export MT_USERNAME=alex
# Show paths as ~/todo, where the installer puts the workspace, and drop the
# "Workspace:" line mt prints before every command to keep frames short.
# 'mt run' starts interactive programs, so it keeps the terminal.
mt() {
  if [ "${1:-}" = run ]; then command mt "$@"; return; fi
  command mt "$@" 2>&1 | sed -e '/^Workspace: /d' -e "s|$demo_root|~/todo|g"
}
export PS1='\[\e[38;5;141m\]❯\[\e[0m\] '
clear
