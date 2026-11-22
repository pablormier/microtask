"""Shared-workspace mode for the dashboard: who is asking, who administers, and local history.

One `mt web` process can serve a workspace to several people when it runs behind a reverse
proxy that signs them in. The proxy sends the signed-in email in X-Auth-Request-Email and
proves it is the proxy with a shared secret in X-Microtask-Proxy-Secret. Without the
secret the email is never trusted, because other programs on the same machine can reach a
loopback port and write any header they like.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
from urllib.parse import urlsplit
import uuid

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(APP_ROOT / "scripts"))

import card_format  # noqa: E402
from workspace import Workspace  # noqa: E402

EMAIL_HEADER = "X-Auth-Request-Email"
SECRET_HEADER = "X-Microtask-Proxy-Secret"
MIN_SECRET_LENGTH = 16
_EMAIL_RE = re.compile(r"[^\s@,;:<>()\[\]\"'\\]{1,64}@[^\s@,;:<>()\[\]\"'\\]{1,190}")


class HostedAuthError(Exception):
    """A request that cannot be tied to a signed-in person. `status` is the HTTP status to answer with."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Identity:
    email: str
    user: str
    admin: bool


def read_secret(path: str | None = None, value: str | None = None,
                option: str = "--trust-proxy-user") -> str:
    """The shared secret from a file (preferred) or a value, checked for being long enough."""
    if path:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as error:
            raise ValueError(f"cannot read the proxy secret file {path}: {error}") from error
    elif value:
        text = value
    else:
        raise ValueError(f"{option} needs the proxy secret: pass --proxy-secret-file FILE "
                         "or set MT_PROXY_SECRET_FILE")
    secret = text.strip()
    if "\n" in secret or "\r" in secret:
        raise ValueError("the proxy secret must be a single line")
    if len(secret) < MIN_SECRET_LENGTH:
        raise ValueError(f"the proxy secret needs at least {MIN_SECRET_LENGTH} characters; "
                         "generate one with: python3 -c 'import secrets; print(secrets.token_urlsafe(32))'")
    return secret


def secret_header_matches(headers, secret: str) -> bool:
    """True when the request carries exactly one X-Microtask-Proxy-Secret header with this value."""
    sent = headers.get_all(SECRET_HEADER) or []
    return len(sent) == 1 and secrets.compare_digest(sent[0].strip().encode("utf-8"), secret.encode("utf-8"))


def read_user_map(path: str | None) -> dict[str, str]:
    """Lines of `email user name`; blank lines and # comments are skipped."""
    mapping: dict[str, str] = {}
    if not path:
        return mapping
    try:
        # newline="" keeps every line ending as written; splitlines then drops \n, \r\n and \r alike,
        # and strip() below removes anything else that trails a field.
        with open(path, encoding="utf-8", newline="") as handle:
            lines = handle.read().splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError(f"cannot read the user map {path}: {error}") from error
    for number, line in enumerate(lines, 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2:
            raise ValueError(f"{path}:{number}: expected 'EMAIL USER'")
        email, user = parts[0].lower(), parts[1]
        if not _EMAIL_RE.fullmatch(email):
            raise ValueError(f"{path}:{number}: not an email address: {parts[0]}")
        if not card_format.is_safe_username(user):
            raise ValueError(f"{path}:{number}: user name must be lowercase letters, digits, '.', '_' or '-', "
                             "starting with a letter or digit")
        if email in mapping:
            raise ValueError(f"{path}:{number}: {email} is listed twice")
        mapping[email] = user
    return mapping


def parse_admins(*values: str | None) -> frozenset[str]:
    """User names from comma- or space-separated values, such as `--admin` flags and MT_ADMINS."""
    names: set[str] = set()
    for value in values:
        for name in re.split(r"[,\s]+", value or ""):
            if not name:
                continue
            if not card_format.is_safe_username(name):
                raise ValueError(f"admin name is not a valid user name: {name!r}")
            names.add(name)
    return frozenset(names)


def read_sign_out_url(value: str | None) -> str:
    """An optional sign-out URL for the dashboard: a relative path ('/...') or an https:// URL."""
    url = (value or "").strip()
    if not url:
        return ""
    if any(ch.isspace() or ord(ch) < 32 for ch in url):
        raise ValueError("sign-out URL must not contain whitespace or control characters")
    if url.startswith("/") and not url.startswith("//"):
        return url
    parsed = urlsplit(url)
    if parsed.scheme == "https" and parsed.netloc:
        return url
    raise ValueError("sign-out URL must be a relative path starting with '/' (such as /oauth2/sign_out) or an https:// URL")


def load_members_file(path: str | Path) -> frozenset[str]:
    """Read active member email addresses from a workspace members file (one per line, # comments allowed)."""
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            lines = handle.read().splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError(f"cannot read workspace members file {path}: {error}") from error
    members: set[str] = set()
    for number, line in enumerate(lines, 1):
        email = line.split("#", 1)[0].strip().lower()
        if not email:
            continue
        if not _EMAIL_RE.fullmatch(email):
            raise ValueError(f"{path}:{number}: invalid member email address: {email}")
        members.add(email)
    return frozenset(members)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _token_digest(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _load_token_records(path: str | Path) -> list[dict[str, object]]:
    """Read and validate JSON token records from `path`."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError(f"cannot read API tokens file {path}: {error}") from error
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"API tokens file {path} is invalid JSON: {error}") from error
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("tokens"), list):
        raise ValueError(f"API tokens file {path} has an unsupported structure")
    records: list[dict[str, object]] = []
    for entry in data["tokens"]:
        if not isinstance(entry, dict):
            raise ValueError(f"API tokens file {path} contains a malformed token entry")
        token_id = entry.get("id")
        email = entry.get("email")
        sha256 = entry.get("sha256")
        created_at = entry.get("created_at")
        revoked_at = entry.get("revoked_at")
        if (
            not isinstance(token_id, str) or not token_id
            or not isinstance(email, str) or not _EMAIL_RE.fullmatch(email.lower())
            or not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256)
            or not isinstance(created_at, str) or not created_at
            or (revoked_at is not None and not isinstance(revoked_at, str))
        ):
            raise ValueError(f"API tokens file {path} contains an invalid token record")
        records.append({
            "id": token_id,
            "email": email.lower(),
            "label": str(entry.get("label") or ""),
            "sha256": sha256,
            "created_at": created_at,
            "revoked_at": revoked_at,
        })
    return records


def _public_record(entry: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in entry.items() if key != "sha256"}


@contextmanager
def _token_store_lock(target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(f".{target.name}.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    handle = os.fdopen(fd, "r+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _write_token_records(path: str | Path, records: list[dict[str, object]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    existing_stat = target.stat() if target.exists() else None
    payload = (json.dumps({"version": 1, "tokens": records}, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    replaced = False
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        if existing_stat is not None:
            try:
                os.chown(tmp, existing_stat.st_uid, existing_stat.st_gid)
            except PermissionError:
                pass
            os.chmod(tmp, stat.S_IMODE(existing_stat.st_mode))
        os.replace(tmp, target)
        replaced = True
    finally:
        if not replaced:
            tmp.unlink(missing_ok=True)


def issue_token(
    tokens_path: str | Path,
    email: str,
    *,
    label: str = "",
    members_path: str | Path | None = None,
    user_map_path: str | Path | None = None,
) -> tuple[str, dict[str, object]]:
    """Create a revocable API bearer token for an active workspace member."""
    normalized = email.strip().lower()
    if not _EMAIL_RE.fullmatch(normalized):
        raise ValueError(f"not an email address: {email}")
    user_map = read_user_map(str(user_map_path)) if user_map_path else None
    try:
        HostedConfig("", user_map=user_map).user_for(normalized)
    except HostedAuthError as error:
        raise ValueError(error.message) from error
    if members_path is not None and normalized not in load_members_file(members_path):
        raise ValueError(f"{normalized} is not listed in workspace members file {members_path}")
    clean_label = label.strip()
    if any(ch in clean_label for ch in "\r\n\x00"):
        raise ValueError("token label must be a single line")
    target = Path(tokens_path)
    raw_token = f"mt_{secrets.token_urlsafe(32)}"
    record: dict[str, object] = {
        "id": secrets.token_hex(8),
        "email": normalized,
        "label": clean_label,
        "sha256": _token_digest(raw_token),
        "created_at": _utc_now_iso(),
        "revoked_at": None,
    }
    with _token_store_lock(target):
        records = _load_token_records(target) if target.exists() else []
        records.append(record)
        _write_token_records(target, records)
    return raw_token, _public_record(record)


def revoke_tokens(
    tokens_path: str | Path,
    *,
    token_id: str | None = None,
    email: str | None = None,
) -> list[dict[str, object]]:
    """Revoke matching active tokens by ID or email address and return the updated records."""
    if not token_id and not email:
        raise ValueError("specify token_id or email to revoke")
    target_email = email.strip().lower() if email else None
    target = Path(tokens_path)
    with _token_store_lock(target):
        records = _load_token_records(target)
        revoked: list[dict[str, object]] = []
        now = _utc_now_iso()
        for entry in records:
            if entry["revoked_at"] is not None:
                continue
            if token_id and entry["id"] != token_id:
                continue
            if target_email and entry["email"] != target_email:
                continue
            entry["revoked_at"] = now
            revoked.append(_public_record(entry))
        if revoked:
            _write_token_records(target, records)
    return revoked


def list_tokens(tokens_path: str | Path) -> list[dict[str, object]]:
    """Return non-secret metadata for all stored tokens."""
    target = Path(tokens_path)
    return [_public_record(entry) for entry in _load_token_records(target)] if target.exists() else []


def parse_bearer_token(headers) -> str | None:
    """Return the single Bearer token from Authorization headers, None when absent, or raise HostedAuthError."""
    auth_values = headers.get_all("Authorization") or []
    if not auth_values:
        return None
    if len(auth_values) != 1:
        raise HostedAuthError(401, "Expected a single Authorization header.")
    scheme, _, token = auth_values[0].strip().partition(" ")
    if scheme.lower() != "bearer" or not token.strip() or any(ch.isspace() for ch in token.strip()):
        raise HostedAuthError(401, "Authorization header must use 'Bearer TOKEN'.")
    return token.strip()


class HostedConfig:
    """Who may use a shared workspace and who administers it.

    The admin list lives here and not in settings.yaml: settings are workspace data that
    people can change through the app, and the list of who may change them must not be.
    """

    def __init__(self, secret: str, admins: frozenset[str] = frozenset(),
                 user_map: dict[str, str] | None = None, sign_out_url: str = "",
                 *, tokens_file: str | None = None, members_file: str | None = None,
                 user_map_file: str | None = None):
        self.secret = secret
        self.admins = admins
        self.user_map = dict(user_map or {})
        self.sign_out_url = read_sign_out_url(sign_out_url)
        self.tokens_file = tokens_file
        self.members_file = members_file
        self.user_map_file = user_map_file

    def user_for(self, email: str, user_map: dict[str, str] | None = None) -> str:
        """The user name for an email: its entry in the user map, otherwise the part before the @."""
        mapping = self.user_map if user_map is None else user_map
        mapped = mapping.get(email)
        if mapped:
            return mapped
        local = email.split("@", 1)[0]
        if not card_format.is_safe_username(local):
            raise HostedAuthError(
                403, "This email cannot be turned into a user name. Ask an administrator to add it to the user map.")
        return local

    def identify(self, headers) -> Identity:
        """The person behind a proxy-authenticated request, or HostedAuthError."""
        if not secret_header_matches(headers, self.secret):
            raise HostedAuthError(403, "This request did not come through the sign-in proxy.")
        emails = headers.get_all(EMAIL_HEADER) or []
        if not emails:
            raise HostedAuthError(401, "Sign in to use this workspace.")
        if len(emails) != 1:
            raise HostedAuthError(403, "The request names more than one signed-in user.")
        email = emails[0].strip().lower()
        if not _EMAIL_RE.fullmatch(email):
            raise HostedAuthError(403, "The signed-in identity is not a single email address.")
        user = self.user_for(email)
        return Identity(email=email, user=user, admin=user in self.admins)

    def identify_bearer(self, headers) -> Identity:
        """Authenticate a bearer API credential and verify active workspace membership on every request."""
        token = parse_bearer_token(headers)
        if token is None:
            raise HostedAuthError(401, "API bearer token is required.")
        if (headers.get_all(SECRET_HEADER) or []) and not secret_header_matches(headers, self.secret):
            raise HostedAuthError(403, "This request did not come through the sign-in proxy.")
        if not self.tokens_file or not self.members_file:
            raise HostedAuthError(403, "API bearer authentication is not configured on this server.")
        try:
            members = load_members_file(self.members_file)
            records = _load_token_records(self.tokens_file)
            user_map = read_user_map(self.user_map_file) if self.user_map_file else self.user_map
        except ValueError as error:
            raise HostedAuthError(403, f"API access unavailable: {error}") from error
        candidate_digest = _token_digest(token)
        matched_email: str | None = None
        for entry in records:
            if entry["revoked_at"] is not None:
                continue
            if secrets.compare_digest(str(entry["sha256"]), candidate_digest):
                matched_email = str(entry["email"])
        if matched_email is None:
            raise HostedAuthError(401, "Invalid or revoked API token.")
        if matched_email not in members:
            raise HostedAuthError(403, "This account is not a member of this workspace.")
        user = self.user_for(matched_email, user_map=user_map)
        return Identity(email=matched_email, user=user, admin=user in self.admins)


def shared_process_token(secret: str, app_version: str) -> str:
    """The process token of a shared-workspace dashboard, derived from the proxy secret and the app version.

    Every process that has the same secret and runs the same version derives the same token, so any
    of them accepts a page another one served. An update changes it, so a page loaded from the older
    version is told to reload before it writes.
    """
    return hmac.new(secret.encode("utf-8"), b"microtask-process-token\n" + app_version.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def member_token(process_token: str, user: str) -> str:
    """The write token of one signed-in person: it proves a request comes from a page loaded for them.

    The page and every payload carry this token, and a write is accepted only with the token of
    the person the proxy names. A tab that stayed open after the account behind it changed
    therefore cannot write as the new person.
    """
    return hmac.new(process_token.encode("utf-8"), b"microtask-member-token\n" + user.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def main(argv: list[str] | None = None) -> int:
    """Command-line access to hosted identity rules and API bearer tokens."""
    parser = argparse.ArgumentParser(prog="web_hosted.py")
    commands = parser.add_subparsers(dest="command", required=True)
    names = commands.add_parser("user-names")
    names.add_argument("--map", default=None)
    names.add_argument("emails", nargs="+")
    admins = commands.add_parser("admins")
    admins.add_argument("values", nargs="*")

    t_issue = commands.add_parser("token-issue")
    t_issue.add_argument("--tokens-file", required=True)
    t_issue.add_argument("--members-file", default=None)
    t_issue.add_argument("--map", default=None)
    t_issue.add_argument("--label", default="")
    t_issue.add_argument("--json", dest="as_json", action="store_true")
    t_issue.add_argument("email")

    t_revoke = commands.add_parser("token-revoke")
    t_revoke.add_argument("--tokens-file", required=True)
    t_revoke.add_argument("--id", dest="token_id", default=None)
    t_revoke.add_argument("--email", default=None)
    t_revoke.add_argument("--json", dest="as_json", action="store_true")

    t_list = commands.add_parser("token-list")
    t_list.add_argument("--tokens-file", required=True)
    t_list.add_argument("--json", dest="as_json", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "admins":
            for name in sorted(parse_admins(*args.values)):
                print(name)
            return 0
        if args.command == "token-issue":
            raw_token, meta = issue_token(
                args.tokens_file,
                args.email,
                label=args.label,
                members_path=args.members_file,
                user_map_path=args.map,
            )
            if args.as_json:
                print(json.dumps({"token": raw_token, **meta}, ensure_ascii=False))
            else:
                print(raw_token)
            return 0
        if args.command == "token-revoke":
            revoked = revoke_tokens(args.tokens_file, token_id=args.token_id, email=args.email)
            if args.as_json:
                print(json.dumps({"revoked": revoked}, ensure_ascii=False))
            else:
                print(f"Revoked {len(revoked)} token(s).")
            return 0
        if args.command == "token-list":
            tokens = list_tokens(args.tokens_file)
            if args.as_json:
                print(json.dumps({"tokens": tokens}, ensure_ascii=False))
            else:
                for entry in tokens:
                    state = f"revoked {entry['revoked_at']}" if entry["revoked_at"] else "active"
                    label = f" [{entry['label']}]" if entry["label"] else ""
                    print(f"{entry['id']}  {entry['email']}{label}  {state}")
            return 0
        config = HostedConfig("", user_map=read_user_map(args.map))
        lines = []
        for raw in args.emails:
            email = raw.strip().lower()
            if not _EMAIL_RE.fullmatch(email):
                raise ValueError(f"not an email address: {raw}")
            try:
                lines.append(f"{email} {config.user_for(email)}")
            except HostedAuthError as error:
                raise ValueError(f"{email} cannot be given a user name: use lowercase letters, digits, '.', '_' "
                                 "and '-', or give it another name in the user map") from error
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
