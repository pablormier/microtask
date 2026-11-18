#!/usr/bin/env bash
# Add one hosted microtask user: workspace, port, service, Caddy site, sign-in.
#
#   add-user.sh [options] USER OWNER-EMAIL PUBLIC-HOST
#
#   add-user.sh alice alice@example.com alice.mt.example.com
#
# Options:
#   --root DIR        prefix for /srv, /etc, /opt (or set MT_DEPLOY_ROOT); for tests
#   --no-systemctl    do not chown, enable the service, or reload Caddy
#   --dry-run         print what would happen and change nothing
#   --app DIR         microtask version that creates the workspace (or MT_APP_DIR);
#                     default is /opt/microtask/current
#
# Run it as root. The workspace holds cards, settings and Git only. The
# service runs the app from /opt/microtask/current (see upgrade.sh).
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
root="${MT_DEPLOY_ROOT:-}"
app_dir="${MT_APP_DIR:-}"
service_user="${MT_SERVICE_USER:-mt}"
first_port=8101
no_systemctl=""
dry_run=""

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
    --app) [[ $# -ge 2 ]] || die "--app needs a directory"; app_dir="$2"; shift 2 ;;
    --no-systemctl) no_systemctl=1; shift ;;
    --dry-run) dry_run=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) usage; die "unknown option $1" ;;
    *) positional+=("$1"); shift ;;
  esac
done
[[ ${#positional[@]} -eq 3 ]] || { usage; die "expected USER OWNER-EMAIL PUBLIC-HOST"; }
user="${positional[0]}"
email="${positional[1]}"
host="${positional[2]}"
root="${root%/}"

# The values end up in a shell-free env file and a Caddyfile, so keep them plain.
[[ "$user" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] \
  || die "user name must be lowercase letters, digits and dashes, not starting or ending with a dash"
email="$(printf '%s' "$email" | tr '[:upper:]' '[:lower:]')"
[[ "$email" =~ ^[a-z0-9._+-]+@[a-z0-9]([a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}$ ]] \
  || die "owner email does not look like an address: $email"
host="$(printf '%s' "$host" | tr '[:upper:]' '[:lower:]')"
[[ "$host" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]] \
  || die "public host must be a domain name such as alice.mt.example.com: $host"

workspace="$root/srv/mt/$user"
env_dir="$root/etc/mt"
env_file="$env_dir/$user.env"
caddy_dir="$root/etc/caddy/mt-users"
caddy_file="$caddy_dir/$user.caddy"
emails_file="${OAUTH2_EMAILS_FILE:-$root/etc/oauth2-proxy/emails.txt}"
template="${MT_CADDY_TEMPLATE:-$script_dir/caddy/user.caddy.template}"
app_dir="${app_dir:-$root/opt/microtask/current}"

[[ -f "$template" ]] || die "missing Caddy template: $template"
[[ -x "$app_dir/mt" ]] || die "no microtask at $app_dir; run upgrade.sh first or pass --app"
for path in "$workspace" "$env_file" "$caddy_file"; do
  [[ ! -e "$path" ]] || die "user $user already exists ($path is present)"
done
if [[ -z "$no_systemctl" && -z "$dry_run" ]]; then
  id "$service_user" >/dev/null 2>&1 || die "service account $service_user does not exist"
fi

# Lowest unused port from first_port up, judged by the existing env files.
used_ports=" "
if [[ -d "$env_dir" ]]; then
  for file in "$env_dir"/*.env; do
    [[ -f "$file" ]] || continue
    port_line="$(grep -E '^PORT=[0-9]+$' "$file" | head -n 1 || true)"
    [[ -z "$port_line" ]] || used_ports="$used_ports${port_line#PORT=} "
  done
fi
port="$first_port"
while [[ "$used_ports" == *" $port "* ]]; do
  port=$((port + 1))
done

if [[ -n "$dry_run" ]]; then
  echo "would create workspace $workspace"
  echo "would write $env_file (PORT=$port, HOST_NAME=$host)"
  echo "would write $caddy_file for $email"
  echo "would add $email to $emails_file"
  [[ -n "$no_systemctl" ]] || echo "would enable mt-web@$user and reload caddy"
  exit 0
fi

created=()
cleanup() {
  local status=$?
  if [[ $status -ne 0 && ${#created[@]} -gt 0 ]]; then
    echo "error: setup failed; removing what this run created" >&2
    for path in "${created[@]}"; do
      rm -rf "$path"
    done
  fi
}
trap cleanup EXIT

# Create a data-only workspace: cards/, mt-policy.yaml and a Git repository, with
# no app files. The shipped policy has auto_sync off and init adds no 'origin'
# remote, so nothing in it syncs. The Git identity is set for this one commit
# because root on a server often has none. Bytecode is not written, so nothing
# lands in the read-only app folder.
mkdir -p "$(dirname "$workspace")"
created+=("$workspace")
init_log="$(env -u MT_WORKSPACE PYTHONDONTWRITEBYTECODE=1 \
  GIT_AUTHOR_NAME=microtask GIT_AUTHOR_EMAIL=microtask@localhost \
  GIT_COMMITTER_NAME=microtask GIT_COMMITTER_EMAIL=microtask@localhost \
  "$app_dir/mt" init --data "$workspace" 2>&1 </dev/null)" \
  || { echo "$init_log" >&2; die "could not create the workspace"; }

mkdir -p "$env_dir"
created+=("$env_file")
printf 'PORT=%s\nHOST_NAME=%s\n' "$port" "$host" > "$env_file"

mkdir -p "$caddy_dir"
created+=("$caddy_file")
{
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line//@USER@/$user}"
    line="${line//@EMAIL@/$email}"
    line="${line//@HOST@/$host}"
    line="${line//@PORT@/$port}"
    printf '%s\n' "$line"
  done < "$template"
} > "$caddy_file"

mkdir -p "$(dirname "$emails_file")"
touch "$emails_file"
if ! grep -qxF "$email" "$emails_file"; then
  printf '%s\n' "$email" >> "$emails_file"
fi

if [[ -z "$no_systemctl" ]]; then
  chown -R "$service_user:$service_user" "$workspace"
  if command -v caddy >/dev/null 2>&1; then
    caddy validate --config /etc/caddy/Caddyfile >/dev/null
  fi
  systemctl enable --now "mt-web@$user"
  systemctl reload caddy
fi

trap - EXIT
echo "Added $user: https://$host (owner $email, local port $port)"
