#!/usr/bin/env bash
# Install a microtask release on a hosted server and switch to it.
#
#   upgrade.sh [options] TAG
#   upgrade.sh [options] --rollback
#
#   upgrade.sh v1.4.0
#
# Options:
#   --source URL      git URL or path to clone from
#                     (default https://github.com/pablormier/microtask.git)
#   --root DIR        prefix for /opt and /srv (or set MT_DEPLOY_ROOT); for tests
#   --no-systemctl    do not run as the mt user, restart services, or chown
#   --dry-run         print what would happen and change nothing
#   --rollback        make the previous version current again
#
# Run it as root. Versions live in /opt/microtask/TAG. 'current' and 'previous'
# are symlinks in that folder. Every workspace in /srv/mt is checked with the new
# version before 'current' moves. Old version folders stay until you remove them.
set -euo pipefail

root="${MT_DEPLOY_ROOT:-}"
source_url="${MT_SOURCE:-https://github.com/pablormier/microtask.git}"
service_user="${MT_SERVICE_USER:-mt}"
no_systemctl=""
dry_run=""
rollback=""

usage() {
  sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//' >&2
}

die() {
  echo "error: $*" >&2
  exit 1
}

positional=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --root) [[ $# -ge 2 ]] || die "--root needs a directory"; root="$2"; shift 2 ;;
    --source) [[ $# -ge 2 ]] || die "--source needs a value"; source_url="$2"; shift 2 ;;
    --no-systemctl) no_systemctl=1; shift ;;
    --dry-run) dry_run=1; shift ;;
    --rollback) rollback=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) usage; die "unknown option $1" ;;
    *) positional+=("$1"); shift ;;
  esac
done
root="${root%/}"
if [[ -n "$rollback" ]]; then
  [[ ${#positional[@]} -eq 0 ]] || { usage; die "--rollback takes no TAG"; }
else
  [[ ${#positional[@]} -eq 1 ]] || { usage; die "expected TAG"; }
  tag="${positional[0]}"
  [[ "$tag" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "tag has characters that are not allowed: $tag"
  [[ "$tag" != "current" && "$tag" != "previous" ]] || die "$tag is not a version name"
fi

apps="$root/opt/microtask"
workspaces="$root/srv/mt"

if [[ -z "$no_systemctl" && -z "$dry_run" ]]; then
  id "$service_user" >/dev/null 2>&1 || die "service account $service_user does not exist"
fi

# Name of the folder a link points at, or nothing when the link is missing.
link_target() {
  if [[ -L "$apps/$1" ]]; then
    basename "$(readlink "$apps/$1")"
  fi
}

# Point $apps/NAME at the folder TARGET with one atomic rename. The link is
# relative, so it does not depend on --root. os.replace works the same on Linux
# and macOS, where 'mv -T' does not exist.
set_link() {
  local name="$1" target="$2"
  rm -f "$apps/.$name.new"
  ln -s "$target" "$apps/.$name.new"
  python3 -c 'import os,sys; os.replace(sys.argv[1], sys.argv[2])' "$apps/.$name.new" "$apps/$name"
}

workspace_names() {
  local dir
  [[ -d "$workspaces" ]] || return 0
  for dir in "$workspaces"/*/; do
    [[ -d "$dir" ]] || continue
    basename "$dir"
  done
}

restart_services() {
  local name
  [[ -z "$no_systemctl" ]] || return 0
  for name in $(workspace_names); do
    if systemctl is-enabled --quiet "mt-web@$name" 2>/dev/null; then
      echo "restarting mt-web@$name"
      systemctl restart "mt-web@$name"
    fi
  done
}

current="$(link_target current)"
previous="$(link_target previous)"

if [[ -n "$rollback" ]]; then
  [[ -n "$previous" ]] || die "there is no previous version to go back to"
  [[ -n "$current" ]] || die "there is no current version"
  [[ -d "$apps/$previous" ]] || die "the previous version folder is missing: $apps/$previous"
  if [[ -n "$dry_run" ]]; then
    echo "would make $previous current and $current previous, then restart the services"
    exit 0
  fi
  set_link current "$previous"
  set_link previous "$current"
  restart_services
  echo "Rolled back: current is $previous, previous is $current"
  exit 0
fi

if [[ -d "$apps/$tag" ]]; then
  if [[ "$tag" == "$current" ]]; then
    echo "$tag is already the current version; nothing to do"
    exit 0
  fi
  die "$apps/$tag already exists. Remove it first, or run --rollback if it is the previous version"
fi

if [[ -n "$dry_run" ]]; then
  echo "would clone $tag from $source_url into $apps/$tag"
  echo "would check these workspaces with $tag: $(workspace_names | tr '\n' ' ')"
  echo "would make $tag current${current:+ and $current previous}, then restart the services"
  exit 0
fi

mkdir -p "$apps"
chmod 755 "$apps"
tmp="$(mktemp -d "$apps/.new-$tag.XXXXXX")"
trap 'rm -rf "$tmp"' EXIT

echo "cloning $tag"
clone_args=(--quiet --branch "$tag" -c advice.detachedHead=false)
[[ -d "$source_url" ]] || clone_args+=(--depth 1)
git clone "${clone_args[@]}" "$source_url" "$tmp/app" 2>&1 </dev/null \
  || die "could not clone $tag from $source_url"
[[ -x "$tmp/app/mt" ]] || die "$tag has no mt launcher"

# Compile now so the first start is fast and nothing writes into the folder later.
python3 -m compileall -q "$tmp/app/scripts" "$tmp/app/tools" >/dev/null
[[ -n "$no_systemctl" ]] || chown -R root:root "$tmp/app"
chmod -R go-w,a+rX "$tmp/app"

mv "$tmp/app" "$apps/$tag"
rmdir "$tmp"
trap - EXIT
echo "installed $apps/$tag"

# Check every workspace with the new version before switching.
failed=()
for name in $(workspace_names); do
  ws="$workspaces/$name"
  if [[ -z "$no_systemctl" ]]; then
    if command -v runuser >/dev/null 2>&1; then
      as_user=(runuser -u "$service_user" --)
    else
      as_user=(sudo -u "$service_user")
    fi
    as_user+=(env HOME="$ws" PYTHONDONTWRITEBYTECODE=1)
  else
    as_user=(env PYTHONDONTWRITEBYTECODE=1)
  fi
  if output="$(cd "$ws" && env -u MT_WORKSPACE "${as_user[@]}" "$apps/$tag/mt" -R "$ws" validate 2>&1 </dev/null)"; then
    echo "ok: $name"
  else
    echo "FAILED: $name" >&2
    printf '%s\n' "$output" | sed 's/^/  /' >&2
    failed+=("$name")
  fi
done
if [[ ${#failed[@]} -gt 0 ]]; then
  echo "error: ${failed[*]} did not validate with $tag. 'current' is unchanged and nothing was restarted." >&2
  echo "The new version stays in $apps/$tag. Remove it by hand when you no longer need it." >&2
  exit 1
fi

if [[ -n "$current" ]]; then
  set_link previous "$current"
fi
set_link current "$tag"
restart_services
echo "Active version: $tag${current:+ (previous: $current)}"
