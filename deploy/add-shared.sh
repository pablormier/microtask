#!/usr/bin/env bash
# Add one shared microtask workspace for a group of people: workspace, port, service, Caddy site, sign-in.
#
#   add-shared.sh [options] NAME PUBLIC-HOST EMAIL...        create the workspace
#   add-shared.sh --add [options] NAME EMAIL...              let more people in
#   add-shared.sh --remove [options] NAME EMAIL...           take people out
#
#   add-shared.sh --admin alice team team.mt.example.com alice@example.com bob@example.com
#   add-shared.sh --add team carol@example.com
#
# Everyone in the EMAIL list may open https://PUBLIC-HOST and works in the same workspace. Each
# person's user name is the part of their email before the @, unless the MT_USER_MAP file named
# in /etc/mt/NAME.env gives that address another name. The resulting names must all differ.
# The members are kept in /etc/mt/NAME.members. --add and --remove change that list, write the
# Caddy site again from the template (keeping its secret and port), update the oauth2-proxy
# emails file and reload Caddy. An address leaves the emails file only when no other workspace
# or site still uses it. Removing an administrator is refused until MT_ADMINS in
# /etc/mt/NAME.env no longer names them. Cards written by a removed person stay as they are.
# A removed person's user name is retired in /etc/mt/NAME.retired, so that a new person cannot
# take over their drafts, claims and focus by getting the same name. --add refuses a retired
# name unless the same address comes back or --reuse-name confirms the new person may have it.
#
# Options:
#   --add               add the EMAIL addresses to an existing workspace
#   --remove            remove the EMAIL addresses from an existing workspace
#   --reuse-name        (--add only) let an address take a user name that was retired by removing
#                       someone else; use it only after checking that it is the same person
#   --admin USER        (creation only) user name allowed to delete cards, take over claims and
#                       rename topics; repeatable, and must be one of the EMAIL user names
#   --emails-file FILE  more addresses, one per line
#   --root DIR          prefix for /srv, /etc, /opt (or set MT_DEPLOY_ROOT); for tests
#   --no-systemctl      do not create the account, chown, enable the service, or reload Caddy
#   --dry-run           print what would happen and change nothing
#   --app DIR           microtask version that creates the workspace and works out the user
#                       names and administrators for every run (or MT_APP_DIR);
#                       default is /opt/microtask/current
#
# Run it as root. The workspace counts tasks in progress per person (rules.doing_limit_scope
# per_claimant). The service runs the app from /opt/microtask/current (see upgrade.sh) as its
# own system account, mt-NAME, which owns the workspace and /etc/mt/NAME.secret.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
root="${MT_DEPLOY_ROOT:-}"
app_dir="${MT_APP_DIR:-}"
first_port=8101
no_systemctl=""
dry_run=""
emails_file_arg=""
reuse_name=""
mode=create
admins=()

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
    --admin) [[ $# -ge 2 ]] || die "--admin needs a user name"; admins+=("$2"); shift 2 ;;
    --emails-file) [[ $# -ge 2 ]] || die "--emails-file needs a file"; emails_file_arg="$2"; shift 2 ;;
    --add) mode=add; shift ;;
    --remove) mode=remove; shift ;;
    --reuse-name) reuse_name=1; shift ;;
    --no-systemctl) no_systemctl=1; shift ;;
    --dry-run) dry_run=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) usage; die "unknown option $1" ;;
    *) positional+=("$1"); shift ;;
  esac
done
if [[ "$mode" == create ]]; then
  [[ ${#positional[@]} -ge 2 ]] || { usage; die "expected NAME PUBLIC-HOST and at least one EMAIL"; }
  [[ -z "$reuse_name" ]] || die "--reuse-name only works with --add"
  name="${positional[0]}"
  host="${positional[1]}"
  raw_emails=("${positional[@]:2}")
else
  [[ ${#admins[@]} -eq 0 ]] || die "--admin only works when creating a workspace"
  [[ -z "$reuse_name" || "$mode" == add ]] || die "--reuse-name only works with --add"
  [[ ${#positional[@]} -ge 1 ]] || { usage; die "expected NAME and at least one EMAIL"; }
  name="${positional[0]}"
  host=""
  raw_emails=("${positional[@]:1}")
fi
if [[ -n "$emails_file_arg" ]]; then
  [[ -f "$emails_file_arg" ]] || die "no such file: $emails_file_arg"
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    line="${line//[[:space:]]/}"
    [[ -z "$line" ]] || raw_emails+=("$line")
  done < "$emails_file_arg"
fi
[[ ${#raw_emails[@]} -gt 0 ]] || die "give at least one email address"
root="${root%/}"

# The values end up in a shell-free env file and a Caddyfile, so keep them plain.
[[ "$name" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] \
  || die "workspace name must be lowercase letters, digits and dashes, not starting or ending with a dash"
# One Unix account per workspace. Linux account names are at most 32 characters.
unix_user="mt-$name"
[[ ${#unix_user} -le 32 ]] \
  || die "workspace name $name is too long: the account $unix_user would have ${#unix_user} characters and Linux allows 32; use at most 29"
if [[ "$mode" == create ]]; then
  host="$(printf '%s' "$host" | tr '[:upper:]' '[:lower:]')"
  [[ "$host" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]] \
    || die "public host must be a domain name such as team.mt.example.com: $host"
fi

# The user map named by MT_USER_MAP in the workspace's env file; empty when there is none.
user_map_path=""

# User names, admin names and the user map are read by the app's own code (tools/web_hosted.py in
# the app folder), so that this script cannot disagree with the dashboard about them. The two
# commands print one result per line and exit 1 with "error: ..." on anything the dashboard would
# refuse. PYTHONDONTWRITEBYTECODE keeps the app folder untouched.
hosted_tool() {
  local output
  output="$(PYTHONDONTWRITEBYTECODE=1 python3 "$app_dir/tools/web_hosted.py" "$@" 2>&1)" \
    || die "${output#error: }"
  printf '%s' "$output"
}

lowercase() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

# The address looks like one, for the oauth2-proxy allowlist. This is stricter than the dashboard.
check_address() {
  [[ "$1" =~ ^[a-z0-9._+-]+@[a-z0-9]([a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}$ ]] \
    || die "does not look like an email address: $1"
}

# The addresses in the members file, one per line, without comments and blanks.
members_addresses() {
  local line
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    line="${line//[[:space:]]/}"
    [[ -z "$line" ]] || printf '%s\n' "$line"
  done < "$members_file"
}

# Lines "EMAIL USER" for every address this run looks at, from one call to the app. The user map
# is applied there, so an unusable map, a bad mapped name or an address that cannot become a user
# name stops the run here, before any change.
name_cache=""
prime_names() {
  local addresses=() address
  for address in "${raw_emails[@]}"; do
    address="$(lowercase "$address")"
    check_address "$address"
    addresses+=("$address")
  done
  if [[ "$mode" != create ]]; then
    while IFS= read -r address; do
      address="$(lowercase "$address")"
      check_address "$address"
      addresses+=("$address")
    done < <(members_addresses)
  fi
  if [[ -n "$user_map_path" ]]; then
    name_cache="$(hosted_tool user-names --map "$user_map_path" "${addresses[@]}")"
  else
    name_cache="$(hosted_tool user-names "${addresses[@]}")"
  fi
}

# User name of an address that prime_names saw.
user_name_of() {
  local found
  found="$(awk -v a="$1" '$1 == a { print $2; exit }' <<< "$name_cache")"
  [[ -n "$found" ]] || die "no user name was worked out for $1"
  printf '%s' "$found"
}

# Lowercase, check and de-duplicate the addresses given as arguments. Sets emails and user_names
# (the effective user names, see user_name_of), and refuses two addresses that share a user name.
emails=()
user_names=" "
check_emails() {
  local raw email user_name
  emails=()
  user_names=" "
  for raw in "$@"; do
    email="$(lowercase "$raw")"
    check_address "$email"
    case " ${emails[*]-} " in *" $email "*) continue ;; esac
    user_name="$(user_name_of "$email")"
    [[ "$user_names" != *" $user_name "* ]] \
      || die "two addresses give the user name $user_name; give one of them another name in the user map, or use addresses that differ before the @"
    user_names="$user_names$user_name "
    emails+=("$email")
  done
}
# Administrator names from comma- or space-separated text, one per line, as the dashboard reads MT_ADMINS.
split_names() {
  hosted_tool admins "$@"
  echo
}
admin_list=""

workspace="$root/srv/mt/$name"
env_dir="$root/etc/mt"
env_file="$env_dir/$name.env"
secret_file="$env_dir/$name.secret"
members_file="$env_dir/$name.members"
retired_file="$env_dir/$name.retired"
tokens_file="$env_dir/$name.tokens.json"
caddy_dir="$root/etc/caddy/mt-users"
caddy_file="$caddy_dir/$name.caddy"
oauth_emails_file="${OAUTH2_EMAILS_FILE:-$root/etc/oauth2-proxy/emails.txt}"
template="${MT_CADDY_SHARED_TEMPLATE:-$script_dir/caddy/shared.caddy.template}"
app_dir="${app_dir:-$root/opt/microtask/current}"

[[ -f "$template" ]] || die "missing Caddy template: $template"
# The checks on what exists. They run once early for a friendly error and again with the lock
# held, because another run may have created or changed these files in between.
check_existing() {
  local path
  if [[ "$mode" == create ]]; then
    for path in "$workspace" "$env_file" "$secret_file" "$caddy_file" "$members_file" "$retired_file" "$tokens_file"; do
      [[ ! -e "$path" ]] || die "$name already exists ($path is present)"
    done
    if [[ -z "$no_systemctl" ]]; then
      ! id "$unix_user" >/dev/null 2>&1 || die "the account $unix_user already exists"
    fi
  else
    for path in "$env_file" "$secret_file" "$caddy_file"; do
      [[ -f "$path" ]] || die "no shared workspace named $name ($path is missing)"
    done
    [[ -f "$members_file" ]] \
      || die "$members_file is missing; write the current members there, one address per line, and run this again"
  fi
}
[[ -f "$app_dir/tools/web_hosted.py" ]] || die "no microtask at $app_dir; run upgrade.sh first or pass --app"
if [[ "$mode" == create ]]; then
  [[ -x "$app_dir/mt" ]] || die "no microtask at $app_dir; run upgrade.sh first or pass --app"
  if [[ ${#admins[@]} -gt 0 ]]; then
    normalized=()
    while IFS= read -r line; do
      [[ -z "$line" ]] || normalized+=("$line")
    done < <(split_names "${admins[@]}")
    if [[ ${#normalized[@]} -gt 0 ]]; then
      admins=("${normalized[@]}")
      admin_list="$(IFS=,; echo "${normalized[*]}")"
    else
      admins=()
    fi
  fi
fi
check_existing

# Read the settings of an existing workspace from its env and secret files.
load_env() {
  port="$(sed -n 's/^PORT=//p' "$env_file" | head -n 1)"
  host="$(sed -n 's/^HOST_NAME=//p' "$env_file" | head -n 1)"
  admin_names="$(sed -n 's/^MT_ADMINS=//p' "$env_file" | head -n 1)"
  secret="$(head -n 1 "$secret_file")"
  user_map_path="$(sed -n 's/^MT_USER_MAP=//p' "$env_file" | head -n 1)"
  if [[ -n "$user_map_path" && ! -f "$user_map_path" && -f "$root$user_map_path" ]]; then
    user_map_path="$root$user_map_path"
  fi
  [[ -z "$user_map_path" || -f "$user_map_path" ]] \
    || die "MT_USER_MAP in $env_file names $user_map_path, which does not exist; fix it or remove the line, then run this again"
  [[ -n "$port" && -n "$host" && -n "$secret" ]] || die "$env_file or $secret_file is incomplete"
  prime_names
}
if [[ "$mode" == create ]]; then
  prime_names
  check_emails "${raw_emails[@]}"
  for admin in "${admins[@]-}"; do
    [[ -z "$admin" ]] && continue
    [[ "$user_names" == *" $admin "* ]] || die "admin $admin is not the user name of any listed email"
  done
else
  load_env
  check_emails "${raw_emails[@]}"
fi
if [[ "$mode" == create && -z "$no_systemctl" && -z "$dry_run" ]]; then
  command -v useradd >/dev/null 2>&1 || die "useradd is not available, so the account $unix_user cannot be created"
fi

# Create an empty file that nobody else can read, then give it its final mode, so that content
# written afterwards (with > or >>, which keep the mode) is never readable by others.
make_file() {
  local mode_bits="$1" path="$2"
  ( umask 077; : > "$path" )
  chmod "$mode_bits" "$path"
}

# Print the Caddy site for the current values of name, host, port, secret and email_pattern.
render_caddy() {
  local line
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line//@NAME@/$name}"
    line="${line//@HOST@/$host}"
    line="${line//@PORT@/$port}"
    line="${line//@SECRET@/$secret}"
    line="${line//@EMAIL_PATTERN@/$email_pattern}"
    printf '%s\n' "$line"
  done < "$template"
}

# One pattern for every allowed address: dots and plus signs are escaped, and case is ignored.
make_email_pattern() {
  email_pattern="(?i)^($(printf '%s\n' "${emails[@]}" | sed -e 's/[.+]/\\&/g' | paste -s -d '|' -))\$"
}

if [[ -z "$dry_run" ]]; then
  mkdir -p "$env_dir"
  lock_dir="$env_dir/.add-user.lock"
  lock_tries=0
  while ! mkdir "$lock_dir" 2>/dev/null; do
    lock_tries=$((lock_tries + 1))
    [[ $lock_tries -lt 50 ]] || die "another add-user.sh or add-shared.sh run holds $lock_dir"
    sleep 0.1
  done
fi

# Remember a path this run is about to create. It must not exist yet, so the cleanup after a
# failure never removes something that belongs to another run.
claim_path() {
  [[ ! -e "$1" ]] || die "$name already exists ($1 is present)"
  created+=("$1")
}

created=()
created_account=""
added_emails=()
enabled_unit=""
backup_dir=""
changed_members=""
had_retired=""
cleanup() {
  local status=$?
  if [[ $status -ne 0 ]]; then
    if [[ -n "$changed_members" && -n "$backup_dir" ]]; then
      echo "error: update failed; restoring the previous members and site" >&2
      cat "$backup_dir/caddy" > "$caddy_file" 2>/dev/null || true
      cat "$backup_dir/members" > "$members_file" 2>/dev/null || true
      if [[ -n "$had_retired" ]]; then
        cat "$backup_dir/retired" > "$retired_file" 2>/dev/null || true
      else
        rm -f "$retired_file"
      fi
      if [[ -f "$backup_dir/emails" ]]; then
        cat "$backup_dir/emails" > "$oauth_emails_file" 2>/dev/null || true
      else
        rm -f "$oauth_emails_file"
      fi
    fi
    if [[ -n "$enabled_unit" ]]; then
      systemctl disable --now "$enabled_unit" >/dev/null 2>&1 || true
    fi
    if [[ ${#added_emails[@]} -gt 0 && -f "$oauth_emails_file" ]]; then
      python3 -c '
import pathlib, sys
p = pathlib.Path(sys.argv[1])
drop = set(sys.argv[2:])
lines = [line for line in p.read_text(encoding="utf-8").splitlines() if line not in drop]
p.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
' "$oauth_emails_file" "${added_emails[@]}" 2>/dev/null || true
    fi
    if [[ ${#created[@]} -gt 0 ]]; then
      echo "error: setup failed; removing what this run created" >&2
      for path in "${created[@]}"; do
        rm -rf "$path"
      done
    fi
    if [[ -n "$created_account" ]]; then
      userdel "$unix_user" >/dev/null 2>&1 || true
    fi
  fi
  if [[ -n "$backup_dir" ]]; then
    rm -rf "$backup_dir"
  fi
  if [[ -n "${lock_dir:-}" ]]; then
    rmdir "$lock_dir" 2>/dev/null || true
  fi
}
trap cleanup EXIT
if [[ -z "$dry_run" ]]; then
  check_existing
fi

# Another site or members list still names this address, so oauth2-proxy must keep it.
address_in_use_elsewhere() {
  local address="$1" file
  for file in "$env_dir"/*.members; do
    [[ -f "$file" && "$file" != "$members_file" ]] || continue
    if grep -qixF -- "$address" "$file"; then return 0; fi
  done
  for file in "$caddy_dir"/*.caddy; do
    [[ -f "$file" && "$file" != "$caddy_file" ]] || continue
    if grep -qiF -- "$address" "$file"; then return 0; fi
  done
  return 1
}

if [[ "$mode" != create ]]; then
  # Read everything again: another run may have changed the files while this one waited for the lock.
  load_env
  check_emails "${raw_emails[@]}"
  requested=("${emails[@]}")
  current=()
  while IFS= read -r line; do
    current+=("$line")
  done < <(members_addresses)
  check_emails "${current[@]}"
  current=("${emails[@]}")
  if [[ "$mode" == add ]]; then
    check_emails "${current[@]}" "${requested[@]}"
    [[ ${#emails[@]} -gt ${#current[@]} ]] || { echo "Nothing to add: everyone listed is already a member of $name"; exit 0; }
  else
    remaining=()
    for address in "${requested[@]}"; do
      case " ${current[*]} " in *" $address "*) ;; *) die "$address is not a member of $name" ;; esac
    done
    for address in "${current[@]}"; do
      case " ${requested[*]} " in *" $address "*) ;; *) remaining+=("$address") ;; esac
    done
    [[ ${#remaining[@]} -gt 0 ]] || die "that would leave $name with no members; stop its service and remove its files instead (docs/hosting.md)"
    check_emails "${remaining[@]}"
    admin_lines="$(split_names "$admin_names")"
    while IFS= read -r admin; do
      [[ -n "$admin" ]] || continue
      [[ "$user_names" == *" $admin "* ]] \
        || die "$admin is an administrator of $name. Change MT_ADMINS in $env_file first (and restart mt-shared@$name), then run this again"
    done <<< "$admin_lines"
  fi
  make_email_pattern

  retired_owner() {
    [[ -f "$retired_file" ]] || return 0
    awk -v u="$1" '$1 == u { print $2; exit }' "$retired_file"
  }
  if [[ "$mode" == add ]]; then
    for address in "${requested[@]}"; do
      case " ${current[*]} " in *" $address "*) continue ;; esac
      who="$(user_name_of "$address")"
      former="$(retired_owner "$who")"
      if [[ -n "$former" && "$former" != "$address" && -z "$reuse_name" ]]; then
        die "the user name $who belonged to $former, who was removed from $name. $address would inherit that person's drafts, claims and focus. Give $address another name with a line '$address NEWNAME' in the MT_USER_MAP file of $name, or add --reuse-name after checking that it is the same person"
      fi
    done
  fi

  if [[ -n "$dry_run" ]]; then
    echo "would write $members_file with ${#emails[@]} address(es) and $caddy_file from the template"
    if [[ "$mode" == add ]]; then
      echo "would add the new addresses to $oauth_emails_file"
    else
      echo "would remove from $oauth_emails_file the addresses no other site uses: ${requested[*]}"
    fi
    [[ -n "$no_systemctl" ]] || echo "would reload caddy"
    exit 0
  fi

  backup_dir="$(mktemp -d)"
  cat "$caddy_file" > "$backup_dir/caddy"
  cat "$members_file" > "$backup_dir/members"
  if [[ -f "$oauth_emails_file" ]]; then cat "$oauth_emails_file" > "$backup_dir/emails"; fi
  if [[ -f "$retired_file" ]]; then
    cat "$retired_file" > "$backup_dir/retired"
    had_retired=1
  else
    : > "$backup_dir/retired"
  fi
  changed_members=1
  # The backup folder is private (mktemp -d), and the files keep the modes they already have.
  render_caddy > "$backup_dir/caddy.new"
  cat "$backup_dir/caddy.new" > "$caddy_file"
  printf '%s\n' "${emails[@]}" > "$members_file"
  chmod 640 "$caddy_file"
  chmod 640 "$members_file"
  if [[ -z "$no_systemctl" ]] && id "$unix_user" >/dev/null 2>&1; then
    chown "root:$unix_user" "$members_file"
    [[ ! -f "$tokens_file" ]] || chown "root:$unix_user" "$tokens_file"
  fi
  # Retire the names of removed people; give a name back to whoever is now allowed to use it.
  if [[ ! -f "$retired_file" ]]; then make_file 600 "$retired_file"; fi
  : > "$backup_dir/retired.new"
  while read -r line; do
    [[ -n "$line" ]] || continue
    keep=1
    if [[ "$mode" == add ]]; then
      for address in "${requested[@]}"; do
        [[ "${line%% *}" != "$(user_name_of "$address")" ]] || keep=""
      done
    fi
    [[ -z "$keep" ]] || printf '%s\n' "$line" >> "$backup_dir/retired.new"
  done < "$backup_dir/retired"
  if [[ "$mode" == remove ]]; then
    for address in "${requested[@]}"; do
      printf '%s %s\n' "$(user_name_of "$address")" "$address" >> "$backup_dir/retired.new"
    done
  fi
  cat "$backup_dir/retired.new" > "$retired_file"
  mkdir -p "$(dirname "$oauth_emails_file")"
  touch "$oauth_emails_file"
  if [[ "$mode" == add ]]; then
    for address in "${requested[@]}"; do
      if ! grep -qxF "$address" "$oauth_emails_file"; then
        printf '%s\n' "$address" >> "$oauth_emails_file"
      fi
    done
  else
    for address in "${requested[@]}"; do
      if ! address_in_use_elsewhere "$address"; then
        grep -vxF -- "$address" "$oauth_emails_file" > "$backup_dir/emails.new" || true
        cat "$backup_dir/emails.new" > "$oauth_emails_file"
      fi
    done
  fi
  if [[ -z "$no_systemctl" ]]; then
    if command -v caddy >/dev/null 2>&1; then
      caddy validate --config /etc/caddy/Caddyfile >/dev/null
    fi
    systemctl reload caddy
  fi
  changed_members=""
  trap - EXIT
  rm -rf "$backup_dir"
  if [[ "$mode" == remove && -f "$tokens_file" ]]; then
    for address in "${requested[@]}"; do
      if ! python3 "$app_dir/tools/web_hosted.py" token-revoke --tokens-file "$tokens_file" --email "$address" >/dev/null; then
        echo "warning: could not revoke tokens for $address in $tokens_file" >&2
      fi
    done
  fi
  rmdir "$lock_dir" 2>/dev/null || true
  echo "Updated $name: ${#emails[@]} member(s); https://$host"
  exit 0
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
  [[ -n "$no_systemctl" ]] || echo "would create the system account $unix_user"
  echo "would create workspace $workspace with rules.doing_limit_scope per_claimant"
  echo "would write $env_file (PORT=$port, HOST_NAME=$host, MT_ADMINS=$admin_list), $secret_file and $members_file"
  echo "would write $caddy_file for ${#emails[@]} address(es)"
  echo "would add the addresses to $oauth_emails_file"
  [[ -n "$no_systemctl" ]] || echo "would enable mt-shared@$name and reload caddy"
  exit 0
fi

# Create the workspace folder: cards/, settings.yaml and a Git repository, with no app files and
# no 'origin' remote. The Git identity is set for these commits because root on a server often
# has none. Bytecode is not written, so nothing lands in the read-only app folder.
git_identity=(GIT_AUTHOR_NAME=microtask GIT_AUTHOR_EMAIL=microtask@localhost
              GIT_COMMITTER_NAME=microtask GIT_COMMITTER_EMAIL=microtask@localhost)
mkdir -p "$(dirname "$workspace")"
claim_path "$workspace"
init_log="$(env -u MT_WORKSPACE PYTHONDONTWRITEBYTECODE=1 "${git_identity[@]}" \
  "$app_dir/mt" init --data "$workspace" 2>&1 </dev/null)" \
  || { echo "$init_log" >&2; die "could not create the workspace"; }
chmod 700 "$workspace"

# Count tasks in progress per person, and keep that choice in the workspace's history. The line goes
# in with the indentation of the other entries under rules:, and the app then reads the result back.
python3 - "$workspace/settings.yaml" <<'PY' || die "could not set rules.doing_limit_scope in settings.yaml"
import pathlib, re, sys
path = pathlib.Path(sys.argv[1])
lines = path.read_text(encoding="utf-8").split("\n")
rules = [i for i, line in enumerate(lines) if re.match(r"rules:", line)]
if not rules:
    if lines and lines[-1] == "":
        lines.pop()
    lines += ["", "rules:", "  doing_limit_scope: per_claimant", ""]
else:
    start = rules[0]
    if len(rules) > 1 or not re.fullmatch(r"rules:[ \t]*(#.*)?", lines[start]):
        raise SystemExit("settings.yaml has rules: in a form this script does not edit")
    block = []
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.strip() and not line.lstrip().startswith("#") and not line[0].isspace():
            break
        block.append(i)
    children = [i for i in block if lines[i].strip() and not lines[i].lstrip().startswith("#")]
    indent = re.match(r"\s*", lines[children[0]]).group() if children else "  "
    setting = f"{indent}doing_limit_scope: per_claimant"
    active = [i for i in children if re.match(re.escape(indent) + r"doing_limit_scope\s*:", lines[i])]
    if active:
        lines[active[0]] = setting
    else:
        anchor = [i for i in children if re.match(re.escape(indent) + r"max_doing_tasks\s*:", lines[i])]
        lines.insert((anchor[0] if anchor else start) + 1, setting)
path.write_text("\n".join(lines), encoding="utf-8")
PY
# Check with the app itself: the workspace must be valid and read as counting per person.
env -u MT_WORKSPACE PYTHONDONTWRITEBYTECODE=1 "$app_dir/mt" -R "$workspace" validate >/dev/null 2>&1 </dev/null \
  || die "settings.yaml is not valid after setting rules.doing_limit_scope; run '$app_dir/mt -R $workspace validate' on a copy to see why"
scope_read="$(env -u MT_WORKSPACE PYTHONDONTWRITEBYTECODE=1 "$app_dir/mt" -R "$workspace" settings --json </dev/null \
  | python3 -c 'import json, sys; print(json.load(sys.stdin).get("rules", {}).get("doing_limit_scope", ""))')" \
  || die "could not read the settings back from the new workspace"
[[ "$scope_read" == per_claimant ]] || die "the new workspace does not count tasks per person (rules.doing_limit_scope is '$scope_read')"
if command -v git >/dev/null 2>&1 && [[ -d "$workspace/.git" ]]; then
  if [[ -n "$(env "${git_identity[@]}" git -C "$workspace" status --porcelain -- settings.yaml)" ]]; then
    env "${git_identity[@]}" git -C "$workspace" add settings.yaml
    env "${git_identity[@]}" git -C "$workspace" commit -q -m "Count tasks in progress per person"
  fi
fi

mkdir -p "$env_dir"
claim_path "$env_file"
claim_path "$secret_file"
secret="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
make_file 600 "$secret_file"
printf '%s\n' "$secret" > "$secret_file"
printf 'PORT=%s\nHOST_NAME=%s\nMT_ADMINS=%s\nMT_PROXY_SECRET_FILE=%s\nMT_MEMBERS_FILE=%s\nMT_API_TOKENS_FILE=%s\nMT_SIGN_OUT_URL=%s\n' \
  "$port" "$host" "$admin_list" "/etc/mt/$name.secret" "/etc/mt/$name.members" "/etc/mt/$name.tokens.json" "/oauth2/sign_out" > "$env_file"

make_email_pattern

mkdir -p "$caddy_dir"
claim_path "$caddy_file"
make_file 640 "$caddy_file"
render_caddy > "$caddy_file"

claim_path "$members_file"
make_file 640 "$members_file"
printf '%s\n' "${emails[@]}" > "$members_file"

claim_path "$tokens_file"
make_file 640 "$tokens_file"
printf '{"version": 1, "tokens": []}\n' > "$tokens_file"

mkdir -p "$(dirname "$oauth_emails_file")"
touch "$oauth_emails_file"
for email in "${emails[@]}"; do
  if ! grep -qxF "$email" "$oauth_emails_file"; then
    printf '%s\n' "$email" >> "$oauth_emails_file"
    added_emails+=("$email")
  fi
done

if [[ -z "$no_systemctl" ]]; then
  # A system account of its own, with no login shell and no home outside the workspace, so that
  # no other dashboard can read these cards or this secret.
  useradd --system --user-group --no-create-home --home-dir "$workspace" --shell /usr/sbin/nologin "$unix_user"
  created_account=1
  chown -R "$unix_user:$unix_user" "$workspace"
  chown "$unix_user:$unix_user" "$secret_file"
  chown "root:$unix_user" "$members_file"
  chown "root:$unix_user" "$tokens_file"
  if getent group caddy >/dev/null 2>&1; then
    chgrp caddy "$caddy_file"
  fi
  if command -v caddy >/dev/null 2>&1; then
    caddy validate --config /etc/caddy/Caddyfile >/dev/null
  fi
  systemctl enable --now "mt-shared@$name"
  enabled_unit="mt-shared@$name"
  systemctl reload caddy
fi

if [[ -n "${lock_dir:-}" ]]; then
  rmdir "$lock_dir" 2>/dev/null || true
fi
trap - EXIT
echo "Added $name: https://$host (${#emails[@]} user(s), local port $port)"
