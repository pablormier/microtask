#!/usr/bin/env bash
# Install microtask: download the latest release, create a workspace, and put mt on PATH.
#
#   curl -fsSL https://raw.githubusercontent.com/pablormier/microtask/main/install.sh | bash
#
# Run 'install.sh --help' for the options. Safe to run again: it only adds what is missing.
set -euo pipefail

usage() {
  cat <<'EOF_USAGE'
Install microtask: download the latest release, create a workspace, and put mt on PATH.

Each question has a default; press Enter to accept it. Without a terminal the
defaults are used. For scripts:

  install.sh [options] [DIR [ORIGIN-URL]]

  --dir DIR          install the application in DIR (default ~/microtask)
  --workspace DIR    keep cards in DIR (default DIR/workspace). Any other folder is
                     recorded as MT_WORKSPACE in your shell config
  --origin URL       private Git repository to sync cards with. If it already holds a
                     workspace it is cloned into the workspace folder; if it is empty,
                     the new workspace is pushed to it
  --here             set up the folder this install.sh is in, instead of downloading
  --no-path          leave the shell config alone
  --yes, -y          do not ask; use the defaults and the options given
  -h, --help         show this help

Environment: MT_NO_PATH=1, MT_RELEASE_TAG (install
this release instead of the latest), MT_ARCHIVE_URL (download this tarball instead),
MT_RELEASES_URL (where to list the releases; for tests).

Run from inside a clone or an installed copy, it needs --here (or --dir with that
folder) to set it up, and then downloads nothing. Safe to run again: it only adds what is missing.
EOF_USAGE
}

app_repository="${MT_UPSTREAM_URL:-https://github.com/pablormier/microtask.git}"

app_dir=""
workspace_dir=""
origin_url=""
add_to_path=1
# --no-path leaves the shell config alone. Declining the PATH question only skips PATH:
# a workspace outside the app folder is still saved, or mt would not find it later.
save_workspace=1
[[ "${MT_NO_PATH:-}" != 1 ]] || { add_to_path=""; save_workspace=""; }
assume_yes=""
here=""
positional=()

die() {
  echo "error: $*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) [[ $# -ge 2 ]] || die "--dir needs a folder"; app_dir="$2"; shift 2 ;;
    --workspace) [[ $# -ge 2 ]] || die "--workspace needs a folder"; workspace_dir="$2"; shift 2 ;;
    --origin) [[ $# -ge 2 ]] || die "--origin needs a URL"; origin_url="$2"; shift 2 ;;
    --no-path) add_to_path=""; save_workspace=""; shift ;;
    --yes|-y) assume_yes=1; shift ;;
    --here) here=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "error: unknown option $1" >&2; exit 2 ;;
    *) positional+=("$1"); shift ;;
  esac
done
if [[ ${#positional[@]} -gt 2 ]]; then
  usage >&2
  die "too many arguments"
fi
if [[ ${#positional[@]} -ge 1 ]]; then app_dir="${positional[0]}"; fi
if [[ ${#positional[@]} -ge 2 ]]; then origin_url="${positional[1]}"; fi

dir_given="${app_dir:+1}"
workspace_given="${workspace_dir:+1}"
origin_given="${origin_url:+1}"

is_app() {
  [[ -x "$1/mt" && -f "$1/scripts/cli.py" ]]
}

# A workspace folder: cards and settings, no application.
is_workspace() {
  [[ -d "$1/cards" && -f "$1/settings.yaml" && ! -e "$1/mt" ]]
}

# An old-style install keeps cards and settings next to the application code. Before 0.2.0 the
# settings file was called mt-policy.yaml, so either name counts.
is_legacy() {
  [[ -d "$1/cards" && ( -f "$1/settings.yaml" || -f "$1/mt-policy.yaml" ) ]]
}

expand_path() {
  local value="$1"
  case "$value" in
    "~") value="$HOME" ;;
    "~/"*) value="$HOME/${value:2}" ;;
  esac
  [[ "$value" == /* ]] || value="$PWD/$value"
  while [[ "$value" != "/" && "$value" == */ ]]; do value="${value%/}"; done
  printf '%s\n' "$value"
}

shorten() {
  if [[ "$1" == "$HOME" ]]; then
    printf '~\n'
  elif [[ "$1" == "$HOME"/* ]]; then
    printf '~%s\n' "${1#"$HOME"}"
  else
    printf '%s\n' "$1"
  fi
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
  app_slug="$(github_slug "$app_repository")"
  if [[ "${origin%/}" == "${app_repository%/}" || ( -n "$origin_slug" && "$origin_slug" == "$app_slug" ) ]]; then
    die "$origin is the microtask application repository; pass a private repository you own"
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

# Questions come from the terminal, because stdin is the script under 'curl | bash'.
# MT_INSTALL_TTY names another file to read the answers from.
interactive=""
terminal="${MT_INSTALL_TTY:-/dev/tty}"
if [[ -z "$assume_yes" ]] && (exec 3<"$terminal") 2>/dev/null; then
  exec 3<"$terminal"
  interactive=1
fi

# ask VARIABLE PROMPT DEFAULT: store the answer, or DEFAULT on Enter.
ask() {
  local reply=""
  printf '%s [%s]: ' "$2" "$(shorten "$3")" >&2
  IFS= read -r reply <&3 || true
  printf -v "$1" '%s' "${reply:-$3}"
}

# Under 'bash install.sh' from a checkout, that folder is the application, but only
# with --here: running the script from the wrong folder must not set that folder up.
existing_app=""
script_path="${BASH_SOURCE[0]:-}"
script_dir=""
if [[ -n "$script_path" && -f "$script_path" ]]; then
  script_dir="$(cd "$(dirname "$script_path")" 2>/dev/null && pwd -P || true)"
  [[ -n "$script_dir" ]] && is_app "$script_dir" || script_dir=""
fi
if [[ -n "$here" ]]; then
  [[ -n "$script_dir" ]] || die "--here needs install.sh to be run from inside a microtask folder"
  [[ -z "$dir_given" ]] || die "--here and --dir both choose the folder; pass only one"
  app_dir="$script_dir"
  existing_app=1
elif [[ -z "$dir_given" && -n "$script_dir" ]]; then
  die "this install.sh is inside the microtask folder $script_dir. Run it with --here to set up that folder, or with --dir DIR to install a new copy"
fi

if [[ -z "$app_dir" ]]; then
  app_dir="$HOME/microtask"
  if [[ -n "$interactive" ]]; then ask app_dir "Install microtask in" "$app_dir"; fi
fi
app_dir="$(expand_path "$app_dir")"
if [[ -z "$existing_app" ]] && is_app "$app_dir"; then existing_app=1; fi

if [[ -z "$workspace_dir" ]]; then
  workspace_dir="$app_dir/workspace"
  if [[ -n "$interactive" && -z "$workspace_given" ]]; then
    ask workspace_dir "Workspace folder" "$workspace_dir"
  fi
fi
workspace_dir="$(expand_path "$workspace_dir")"

legacy_app=""
if [[ -n "$existing_app" ]] && is_legacy "$app_dir"; then
  legacy_app=1
  workspace_dir="$app_dir"
fi

workspace_ready=""
if is_workspace "$workspace_dir" || [[ -n "$legacy_app" ]]; then workspace_ready=1; fi

if [[ -z "$workspace_ready" && -z "$origin_given" && -n "$interactive" ]]; then
  printf 'Private git repo to sync cards (optional, Enter to skip): ' >&2
  IFS= read -r origin_url <&3 || true
fi

# The workspace may not be inside a folder that is not ours to fill.
if [[ -z "$workspace_ready" && -e "$workspace_dir" ]]; then
  if [[ ! -d "$workspace_dir" || -n "$(ls -A "$workspace_dir")" ]]; then
    die "$workspace_dir already exists and is not a microtask workspace"
  fi
fi
if [[ -z "$existing_app" && -e "$app_dir" ]]; then
  if [[ ! -d "$app_dir" || -n "$(ls -A "$app_dir")" ]]; then
    die "$app_dir already exists and is not microtask"
  fi
fi

origin_heads=""
if [[ -n "$origin_url" && -z "$workspace_ready" ]]; then
  check_origin_is_not_app "$origin_url"
  command -v git >/dev/null 2>&1 || die "Git is needed to connect $origin_url; install Git or skip the repository"
  if ! origin_heads="$(git ls-remote --heads "$origin_url")"; then
    die "cannot read $origin_url; check the URL and your Git access"
  fi
  [[ -n "$origin_heads" ]] || check_origin_is_not_public "$origin_url"
elif [[ -n "$origin_url" ]]; then
  echo "The workspace $(shorten "$workspace_dir") already exists, so $origin_url was not connected."
fi

command -v python3 >/dev/null 2>&1 || die "python3 is required"

download_app() {
  python3 - "$app_dir" <<'PY'
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath

destination = Path(sys.argv[1]).absolute()


def fail(message):
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def latest_tag():
    """MT_RELEASE_TAG, else the newest published release (by version) from the GitHub releases list."""
    override = os.environ.get("MT_RELEASE_TAG")
    if override:
        return override
    url = os.environ.get("MT_RELEASES_URL") or "https://api.github.com/repos/pablormier/microtask/releases?per_page=100"
    found = {}
    for _ in range(20):
        if not url:
            break
        request = urllib.request.Request(
            url, headers={"User-Agent": "microtask-install", "Accept": "application/vnd.github+json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                releases = json.loads(response.read().decode("utf-8"))
                link = response.headers.get("Link") or ""
        except urllib.error.HTTPError as error:
            if error.code in (403, 429):
                fail("GitHub is limiting requests right now. Try again later, or set MT_RELEASE_TAG=vX.Y.Z")
            fail(f"could not list the releases at {url}: HTTP {error.code}")
        except (urllib.error.URLError, OSError, ValueError) as error:
            fail(f"could not read the releases at {url}: {error}")
        if not isinstance(releases, list):
            fail(f"{url} did not return a list of releases")
        for release in releases:
            if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
                continue
            name = release.get("tag_name")
            match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", name) if isinstance(name, str) else None
            if match:
                found[tuple(int(part) for part in match.groups())] = name
        match = re.search(r'<([^>]+)>\s*;\s*rel="next"', link)
        url = match.group(1) if match else None
    if not found:
        fail("no releases found; set MT_RELEASE_TAG=vX.Y.Z to choose one")
    return found[max(found)]


tag = latest_tag()
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", tag):
    fail(f"not a valid release tag: {tag}")
url = os.environ.get("MT_ARCHIVE_URL") or f"https://github.com/pablormier/microtask/archive/refs/tags/{tag}.tar.gz"
parent = destination.parent
try:
    parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading microtask {tag}", file=sys.stderr)
    with tempfile.TemporaryDirectory(prefix=".microtask-install-", dir=str(parent)) as temporary:
        archive_path = Path(temporary) / "source.tar.gz"
        unpacked = Path(temporary) / "unpacked"
        unpacked.mkdir()
        with urllib.request.urlopen(url, timeout=60) as response, archive_path.open("wb") as output:
            shutil.copyfileobj(response, output)
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            top = None
            for member in members:
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or not path.parts:
                    raise ValueError(f"unsafe archive path: {member.name}")
                if not (member.isfile() or member.isdir()):
                    raise ValueError(f"unsupported archive entry: {member.name}")
                top = top or path.parts[0]
                if path.parts[0] != top:
                    raise ValueError("archive contains more than one top-level folder")
            if top is None:
                raise ValueError("archive is empty")
            options = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
            archive.extractall(unpacked, members=members, **options)
        candidate = unpacked / top
        if not (candidate / "mt").is_file() or not (candidate / "scripts" / "cli.py").is_file():
            raise ValueError("archive does not contain microtask")
        # mt update removes only the entries a release brought, never files added later.
        release_entries = sorted(
            item.name for item in candidate.iterdir()
            if item.name not in {"workspace", ".update-tmp", "deploy", "VERSION", ".release-files", ".mt-hosted", "__pycache__", ".DS_Store"}
        )
        (candidate / ".release-files").write_text("".join(name + "\n" for name in release_entries),
                                                  encoding="utf-8")
        (candidate / "VERSION").write_text(tag + "\n", encoding="utf-8")
        if destination.is_dir() and not any(destination.iterdir()):
            destination.rmdir()
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(f"destination already exists: {destination}")
        os.rename(candidate, destination)
except (OSError, tarfile.TarError, ValueError, urllib.error.URLError) as error:
    fail(f"could not install microtask: {error}")
PY
}

installed_now=""
if [[ -z "$existing_app" ]]; then
  download_app
  installed_now=1
fi
is_app "$app_dir" || die "$app_dir is not a microtask application"

# Create the workspace: clone the repository that holds one, or start from the templates.
if [[ -z "$workspace_ready" ]]; then
  if [[ -n "$origin_heads" ]]; then
    if ! git clone --quiet --origin origin "$origin_url" "$workspace_dir"; then
      die "could not clone $origin_url"
    fi
    if [[ -e "$workspace_dir/mt" ]]; then
      rm -rf "$workspace_dir"
      die "$origin_url still uses the old layout, with the application files inside the workspace. On a machine that has it, run 'mt migrate' and 'mt sync', then install again"
    fi
    if [[ ! -f "$workspace_dir/settings.yaml" && -f "$workspace_dir/mt-policy.yaml" ]]; then
      rm -rf "$workspace_dir"
      die "$origin_url still uses mt-policy.yaml, which is now settings.yaml. On a machine that has this workspace, run 'mt migrate' and 'mt sync', then install again"
    fi
    if [[ ! -f "$workspace_dir/settings.yaml" ]]; then
      rm -rf "$workspace_dir"
      die "$origin_url does not hold a microtask workspace (no settings.yaml)"
    fi
    mkdir -p "$workspace_dir/cards" "$workspace_dir/drafts"
    echo "Cloned your workspace from $origin_url"
  else
    python3 "$app_dir/scripts/workspace.py" create "$workspace_dir"
    if [[ -n "$origin_url" ]]; then
      git -C "$workspace_dir" init -q
      git -C "$workspace_dir" symbolic-ref HEAD refs/heads/main
      git -C "$workspace_dir" remote add origin "$origin_url"
      git -C "$workspace_dir" add -A
      if git -C "$workspace_dir" commit -q -m "Create microtask workspace"; then
        if git -C "$workspace_dir" push -q -u origin HEAD; then
          echo "Connected origin: $origin_url"
        else
          echo "warning: added origin but the first push failed; retry with:" >&2
          echo "  git -C \"$workspace_dir\" push -u origin HEAD" >&2
        fi
      else
        echo "warning: Git has no name or email on this machine, so the workspace is not committed yet:" >&2
        echo "  git config --global user.name \"Your Name\"" >&2
        echo "  git config --global user.email \"you@example.com\"" >&2
      fi
    fi
  fi
fi

# Shell config: the file each shell reads.
shell_rc=""
shell_name="${SHELL:-}"
shell_name="${shell_name##*/}"
case "$shell_name" in
  zsh) shell_rc="$HOME/.zshrc" ;;
  bash) if [[ "${OSTYPE:-}" == darwin* ]]; then shell_rc="$HOME/.bash_profile"; else shell_rc="$HOME/.bashrc"; fi ;;
esac
escape_dq() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//\$/\\\$}"
  value="${value//\`/\\\`}"
  printf '%s' "$value"
}
path_line="export PATH=\"$(escape_dq "$app_dir"):\$PATH\""
workspace_line=""
if [[ "$workspace_dir" != "$app_dir/workspace" && -z "$legacy_app" ]]; then
  workspace_line="export MT_WORKSPACE=\"$(escape_dq "$workspace_dir")\""
fi

if [[ -n "$add_to_path" && -n "$interactive" && -n "$shell_rc" ]]; then
  answer=""
  printf 'Add mt to %s? [Y/n]: ' "$(shorten "$shell_rc")" >&2
  IFS= read -r answer <&3 || true
  case "$answer" in
    [Nn]*) add_to_path="" ;;
  esac
fi
if [[ -n "$interactive" ]]; then
  exec 3<&-
fi

rc_changed=""
if [[ -n "$add_to_path" && -n "$shell_rc" ]]; then
  # A line that puts another copy of mt on PATH after ours would win in new terminals.
  found=0 number=0 others="" last_other=0 last_other_folder=""
  path_pattern='^export PATH="(.+):\$PATH"$'
  if [[ -f "$shell_rc" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
      number=$((number + 1))
      if [[ "$line" == "$path_line" ]]; then
        found=$number
      elif [[ "$line" =~ $path_pattern ]]; then
        folder="${BASH_REMATCH[1]}"
        folder="${folder/#\$HOME/$HOME}"
        folder="${folder/#\~/$HOME}"
        if [[ "$folder" != "$app_dir" ]] && is_app "$folder"; then
          others="$others  $folder"$'\n'
          last_other=$number
          last_other_folder="$folder"
        fi
      fi
    done < "$shell_rc"
  fi
  if [[ "$found" -eq 0 ]]; then
    printf '\n# microtask\n%s\n' "$path_line" >> "$shell_rc"
    rc_changed=1
  fi
  if [[ "$found" -ne 0 && "$last_other" -gt "$found" ]]; then
    echo "warning: $shell_rc puts mt from $last_other_folder on PATH after this one," >&2
    echo "so new terminals will still run that one. Remove or move that line to use $app_dir." >&2
  elif [[ -n "$others" ]]; then
    printf '%s also puts mt from other installs on PATH:\n%s' "$shell_rc" "$others"
    echo "New terminals use $app_dir because its line comes last. Remove the older lines if you no longer use them."
  fi
fi

workspace_saved=""
if [[ -n "$workspace_line" && -n "$save_workspace" && -n "$shell_rc" ]]; then
  ws_found=0 ws_number=0 ws_last_other=0 ws_last_other_folder=""
  ws_pattern='^export MT_WORKSPACE="(.+)"$'
  if [[ -f "$shell_rc" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
      ws_number=$((ws_number + 1))
      if [[ "$line" == "$workspace_line" ]]; then
        ws_found=$ws_number
      elif [[ "$line" =~ $ws_pattern ]]; then
        ws_last_other=$ws_number
        ws_last_other_folder="${BASH_REMATCH[1]}"
      fi
    done < "$shell_rc"
  fi
  if [[ "$ws_found" -eq 0 ]]; then
    [[ -n "$add_to_path" ]] || printf '\n# microtask\n' >> "$shell_rc"
    printf '%s\n' "$workspace_line" >> "$shell_rc"
    rc_changed=1
  elif [[ "$ws_last_other" -gt "$ws_found" ]]; then
    echo "warning: $shell_rc sets MT_WORKSPACE=$ws_last_other_folder after this one," >&2
    echo "so new terminals will still use that workspace. Remove or move that line to use $workspace_dir." >&2
  fi
  workspace_saved=1
fi

if ! MT_WORKSPACE="$workspace_dir" "$app_dir/mt" validate >/dev/null 2>&1; then
  echo "warning: validation reported problems; run '$app_dir/mt validate' for details" >&2
fi

version=""
if [[ -f "$app_dir/VERSION" ]]; then version="$(tr -d '\n' < "$app_dir/VERSION")"; fi
if [[ -n "$installed_now" ]]; then headline="installed"; else headline="is set up"; fi
printf '\nmicrotask%s %s\n' "${version:+ $version}" "$headline"
printf '  app:        %s\n' "$(shorten "$app_dir")"
printf '  workspace:  %s\n' "$(shorten "$workspace_dir")"
printf '  update:     mt update\n'
if [[ -n "$legacy_app" ]]; then
  printf 'This folder keeps your cards next to the application. Run mt migrate to move them into workspace/.\n'
fi
if [[ -n "$add_to_path" && -n "$shell_rc" ]]; then
  if [[ -n "$rc_changed" ]]; then
    printf 'Open a new terminal, or run: source %s\n' "$(shorten "$shell_rc")"
  else
    printf 'mt is already set up in %s.\n' "$(shorten "$shell_rc")"
  fi
elif [[ -n "$add_to_path" ]]; then
  printf "Unknown shell '%s'; add these lines to your shell config:\n" "${SHELL:-}"
  printf '  %s\n' "$path_line"
  if [[ -n "$workspace_line" ]]; then printf '  %s\n' "$workspace_line"; fi
elif [[ -n "$workspace_saved" ]]; then
  printf 'mt is not on PATH; run it as %s/mt.\n' "$(shorten "$app_dir")"
  if [[ -n "$rc_changed" ]]; then
    printf 'The workspace location was saved in %s. Open a new terminal, or run: source %s\n' \
      "$(shorten "$shell_rc")" "$(shorten "$shell_rc")"
  fi
else
  printf 'Your shell config is unchanged. Run this copy as %s/mt' "$(shorten "$app_dir")"
  if [[ -n "$workspace_line" ]]; then
    printf ', with this set:\n  %s\n' "$workspace_line"
  else
    printf '\n'
  fi
fi
