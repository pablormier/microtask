#!/usr/bin/env bash
# Install microtask, or set up an existing workspace on this machine.
#
#   ./install.sh                    inside a cloned workspace: set it up here
#   install.sh [DIR] [ORIGIN-URL]   create DIR (default ~/todo); if ORIGIN-URL
#                                   already holds a workspace, clone it instead
#   --no-path (or MT_NO_PATH=1)     leave the shell config alone, so the mt
#                                   that new terminals run stays the same; use
#                                   this for an extra workspace, e.g. a project's
#
# Safe to run again: it only adds what is missing.
set -euo pipefail

repository="${MT_UPSTREAM_URL:-https://github.com/pablormier/microtask.git}"
archive_url="${MT_ARCHIVE_URL:-https://github.com/pablormier/microtask/archive/refs/heads/main.tar.gz}"

add_to_path=1
[[ "${MT_NO_PATH:-}" != 1 ]] || add_to_path=""
arguments=()
for argument in "$@"; do
  case "$argument" in
    --no-path) add_to_path="" ;;
    -*) echo "error: unknown option $argument" >&2; exit 2 ;;
    *) arguments+=("$argument") ;;
  esac
done
set -- ${arguments[@]+"${arguments[@]}"}

is_workspace() {
  [[ -x "$1/mt" && -f "$1/scripts/tasklib.py" ]]
}

# Print "owner/repo" (lowercase) for a GitHub URL; print nothing otherwise.
github_slug() {
  local url="${1%/}"
  url="${url%.git}"
  if [[ "$url" =~ github\.com(:[0-9]+)?[:/]+([^/]+)/([^/]+)$ ]]; then
    printf '%s/%s\n' "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}" | tr '[:upper:]' '[:lower:]'
  fi
}

# Refuse an origin that is the microtask app repository itself.
check_origin_is_not_app() {
  local origin="$1" origin_slug app_slug
  origin_slug="$(github_slug "$origin")"
  app_slug="$(github_slug "$repository")"
  if [[ "${origin%/}" == "${repository%/}" || ( -n "$origin_slug" && "$origin_slug" == "$app_slug" ) ]]; then
    echo "error: $origin is the microtask application repository; pass a private repository you own" >&2
    exit 1
  fi
}

# Refuse a public GitHub origin before the first push. Runs only when the
# GitHub CLI is installed and can read the repository; otherwise it is skipped.
check_origin_is_not_public() {
  local slug visibility
  slug="$(github_slug "$1")"
  [[ -n "$slug" ]] && command -v gh >/dev/null 2>&1 || return 0
  visibility="$(gh repo view "$slug" --json visibility --jq .visibility 2>/dev/null </dev/null || true)"
  if [[ "$visibility" == "PUBLIC" ]]; then
    echo "error: $slug is a public GitHub repository; pushing the workspace there would publish your cards" >&2
    echo "Make it private, or install without it and connect it later with rules.allow_public_origin: true." >&2
    exit 1
  fi
}

# Per-machine setup for an existing workspace checkout. Uses only Bash
# builtins besides git so it also works in minimal environments.
setup_workspace() {
  local dir="$1"
  local has_git=""
  command -v git >/dev/null 2>&1 && [[ -e "$dir/.git" ]] && has_git=1

  if [[ -n "$has_git" ]]; then
    local current
    current="$(git -C "$dir" remote get-url upstream 2>/dev/null || true)"
    if [[ -z "$current" ]]; then
      git -C "$dir" remote add upstream "$repository"
      echo "Added 'upstream' remote for 'mt update': $repository"
    elif [[ "$current" != "$repository" ]]; then
      echo "Keeping existing 'upstream' remote: $current"
    fi
    if ! git -C "$dir" fetch --quiet upstream; then
      echo "warning: could not fetch 'upstream'; 'mt update' needs network access to it" >&2
    fi

    if [[ -z "$(git -C "$dir" remote get-url origin 2>/dev/null || true)" ]]; then
      echo "No 'origin' remote yet; 'mt sync' needs one:"
      echo "  git -C \"$dir\" remote add origin YOUR-PRIVATE-REPOSITORY-URL"
    fi
    if [[ -z "$(git -C "$dir" config user.name || true)" || -z "$(git -C "$dir" config user.email || true)" ]]; then
      echo "Git has no name/email on this machine; 'mt sync' needs them to commit:"
      echo "  git config --global user.name \"Your Name\""
      echo "  git config --global user.email \"you@example.com\""
    fi
  fi

  if [[ -z "$add_to_path" ]]; then
    if ! "$dir/mt" validate >/dev/null 2>&1; then
      echo "warning: workspace validation reported problems; run '$dir/mt validate' for details" >&2
    fi
    printf '\nmicrotask is ready in %s.\n' "$dir"
    printf 'Your shell config and the mt on PATH are unchanged. Run this workspace as:\n\n'
    printf '  %s/mt list\n\n' "$dir"
    printf 'or give it a short name for mt -R:\n\n  mt repo add NAME %s\n\n' "$dir"
    printf 'To let agents in another project use it, see: %s/mt skills install --help\n' "$dir"
    return 0
  fi

  # Persist PATH for new terminals in the file each shell actually reads.
  local shell_rc=""
  case "${SHELL##*/}" in
    zsh) shell_rc="$HOME/.zshrc" ;;
    bash) if [[ "${OSTYPE:-}" == darwin* ]]; then shell_rc="$HOME/.bash_profile"; else shell_rc="$HOME/.bashrc"; fi ;;
  esac
  local path_line="export PATH=\"$dir:\$PATH\""
  if [[ -n "$shell_rc" ]]; then
    # Find this workspace's PATH line and any line that puts another
    # workspace's mt on PATH; the last such line wins in new terminals.
    local found=0 line number=0 folder others="" last_other=0 last_other_folder=""
    local path_pattern='^export PATH="(.+):\$PATH"$'
    if [[ -f "$shell_rc" ]]; then
      while IFS= read -r line || [[ -n "$line" ]]; do
        number=$((number + 1))
        if [[ "$line" == "$path_line" ]]; then
          found=$number
        elif [[ "$line" =~ $path_pattern ]]; then
          folder="${BASH_REMATCH[1]}"
          folder="${folder/#\$HOME/$HOME}"
          folder="${folder/#\~/$HOME}"
          if [[ "$folder" != "$dir" ]] && is_workspace "$folder"; then
            others="$others  $folder"$'\n'
            last_other=$number
            last_other_folder="$folder"
          fi
        fi
      done < "$shell_rc"
    fi
    if [[ "$found" -eq 0 ]]; then
      printf '\n# microtask\n%s\n' "$path_line" >> "$shell_rc"
      echo "Added mt to PATH in $shell_rc"
    fi
    if [[ "$found" -ne 0 && "$last_other" -gt "$found" ]]; then
      echo "warning: $shell_rc puts mt from $last_other_folder on PATH after this workspace," >&2
      echo "so new terminals will still run that one. Remove or move that line to use $dir." >&2
    elif [[ -n "$others" ]]; then
      printf '%s also puts mt from other workspaces on PATH:\n%s' "$shell_rc" "$others"
      echo "New terminals use $dir because its line comes last. Remove the older lines if you no longer use them."
    fi
  else
    echo "Unknown shell '${SHELL:-}'; add this line to your shell config:"
    echo "  $path_line"
  fi

  if ! "$dir/mt" validate >/dev/null 2>&1; then
    echo "warning: workspace validation reported problems; run 'mt validate' for details" >&2
  fi

  local current_mt
  current_mt="$(command -v mt 2>/dev/null || true)"
  if [[ -n "$current_mt" && "$current_mt" != "$dir/mt" ]]; then
    printf '\nThis terminal currently runs mt from %s.\n' "${current_mt%/mt}"
  fi

  printf '\nmicrotask is ready in %s.\n' "$dir"
  printf 'New terminals have mt on PATH. In this terminal, run:\n\n'
  printf '  export PATH="%s:$PATH"\n\n' "$dir"
}

# Running the script from inside a checkout with no arguments sets up that
# checkout. Under 'curl | bash' there is no script file, so this is skipped.
script_path="${BASH_SOURCE[0]:-}"
if [[ $# -eq 0 && -n "$script_path" && -f "$script_path" ]]; then
  script_dir="$(cd "${script_path%/*}" 2>/dev/null && pwd || true)"
  [[ "$script_path" == */* ]] || script_dir="$PWD"
  if [[ -n "$script_dir" ]] && is_workspace "$script_dir"; then
    setup_workspace "$script_dir"
    exit 0
  fi
fi

destination="${1:-${MT_REPO:-$HOME/todo}}"
origin_url="${2:-${MT_ORIGIN:-}}"

if [[ -e "$destination" ]]; then
  if is_workspace "$destination"; then
    setup_workspace "$(cd "$destination" && pwd)"
    exit 0
  fi
  echo "error: $destination already exists and is not a microtask workspace" >&2
  exit 1
fi

if command -v git >/dev/null 2>&1; then
  # Ask for the private repository only when someone is at a terminal.
  if [[ -z "$origin_url" && -t 1 ]] && (exec </dev/tty) 2>/dev/null; then
    read -r -p "Private repository URL for 'origin' (leave blank to skip): " origin_url < /dev/tty || true
  fi

  [[ -z "$origin_url" ]] || check_origin_is_not_app "$origin_url"

  origin_heads=""
  if [[ -n "$origin_url" ]] && ! origin_heads="$(git ls-remote --heads "$origin_url")"; then
    echo "error: cannot read $origin_url; check the URL and your Git access" >&2
    exit 1
  fi

  if [[ -n "$origin_heads" ]]; then
    # The private repository already holds a workspace: use it.
    git clone --origin origin "$origin_url" "$destination"
  else
    [[ -z "$origin_url" ]] || check_origin_is_not_public "$origin_url"
    git clone --origin upstream "$repository" "$destination"
    if [[ -n "$origin_url" ]]; then
      git -C "$destination" remote add origin "$origin_url"
      if git -C "$destination" push -u origin HEAD; then
        echo "Connected origin: $origin_url"
      else
        echo "warning: added origin but the first push failed; retry with:" >&2
        echo "  git -C \"$destination\" push -u origin HEAD" >&2
      fi
    fi
  fi
else
  python3 - "$archive_url" "$destination" <<'PY'
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath

url, destination = sys.argv[1:]
destination_path = Path(destination).absolute()
parent = destination_path.parent
if destination_path.exists() or destination_path.is_symlink():
    print(f"error: destination already exists: {destination}", file=sys.stderr)
    raise SystemExit(1)
if not parent.is_dir():
    print(f"error: destination parent does not exist: {parent}", file=sys.stderr)
    raise SystemExit(1)

try:
    with tempfile.TemporaryDirectory(prefix=".microtask-install-", dir=str(parent)) as temporary:
        archive_path = Path(temporary) / "source.tar.gz"
        candidate = Path(temporary) / "source"
        candidate.mkdir()
        with urllib.request.urlopen(url, timeout=60) as response, archive_path.open("wb") as output:
            shutil.copyfileobj(response, output)
        root_name = None
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                raw_name = member.name
                path = PurePosixPath(raw_name)
                if path.is_absolute() or ".." in path.parts or not path.parts:
                    raise ValueError(f"unsafe archive path: {raw_name}")
                if root_name is None:
                    root_name = path.parts[0]
                if path.parts[0] != root_name:
                    raise ValueError("archive contains more than one top-level directory")
                relative_parts = path.parts[1:]
                if not relative_parts:
                    continue
                target = candidate.joinpath(*relative_parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise ValueError(f"unsupported archive entry: {raw_name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError(f"could not read archive entry: {raw_name}")
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                os.chmod(target, member.mode & 0o777)
        if root_name is None or not (candidate / "mt").is_file() or not (candidate / "install.sh").is_file():
            raise ValueError("archive does not contain a microtask checkout")
        if destination_path.exists() or destination_path.is_symlink():
            raise FileExistsError(f"destination already exists: {destination}")
        os.rename(candidate, destination_path)
except (OSError, tarfile.TarError, ValueError) as error:
    print(f"error: could not install source archive: {error}", file=sys.stderr)
    raise SystemExit(1)
PY

  echo "Installed source archive without Git. To add sync and updates later:"
  echo "  cd \"$destination\""
  echo "  git init"
  echo "  git config user.name \"Your Name\""
  echo "  git config user.email \"you@example.com\""
  echo "  git add -A && git commit -m \"Initial workspace\""
  if [[ -n "$origin_url" ]]; then
    printf '  git remote add origin %q\n' "$origin_url"
    echo "  git push -u origin HEAD"
  else
    echo "  git remote add origin YOUR-PRIVATE-REPOSITORY-URL"
  fi
  echo "  ./install.sh"
fi

destination="$(cd "$destination" && pwd)"
if [[ -x "$destination/mt" ]]; then
  setup_workspace "$destination"
else
  printf '\nInstalled microtask in %s.\n' "$destination"
fi
if [[ -n "$add_to_path" ]]; then
  printf 'Create your first task:\n\n  mt add "Call dentist about appointment"\n  mt list\n'
fi
