"""Shared-workspace mode for the dashboard: who is asking, who administers, and local history.

One `mt web` process can serve a workspace to several people when it runs behind a reverse
proxy that signs them in. The proxy sends the signed-in email in X-Auth-Request-Email and
proves it is the proxy with a shared secret in X-Microtask-Proxy-Secret. Without the
secret the email is never trusted, because other programs on the same machine can reach a
loopback port and write any header they like.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import hmac
from pathlib import Path
import re
import secrets
import sys
from urllib.parse import urlsplit

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


class HostedConfig:
    """Who may use a shared workspace and who administers it. Set once at startup.

    The admin list lives here and not in settings.yaml: settings are workspace data that
    people can change through the app, and the list of who may change them must not be.
    """

    def __init__(self, secret: str, admins: frozenset[str] = frozenset(),
                 user_map: dict[str, str] | None = None, sign_out_url: str = ""):
        self.secret = secret
        self.admins = admins
        self.user_map = dict(user_map or {})
        self.sign_out_url = read_sign_out_url(sign_out_url)

    def user_for(self, email: str) -> str:
        """The user name for an email: its entry in the user map, otherwise the part before the @."""
        mapped = self.user_map.get(email)
        if mapped:
            return mapped
        local = email.split("@", 1)[0]
        if not card_format.is_safe_username(local):
            raise HostedAuthError(
                403, "This email cannot be turned into a user name. Ask an administrator to add it to the user map.")
        return local

    def identify(self, headers) -> Identity:
        """The person behind a request, or HostedAuthError. `headers` is an email.message.Message."""
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
    """Command-line access to the rules above, for deploy/add-shared.sh.

    `user-names [--map FILE] EMAIL...` prints `EMAIL USER` for each address, as the dashboard
    names them. `admins VALUE...` prints the administrator names, one per line, as MT_ADMINS is read.
    """
    parser = argparse.ArgumentParser(prog="web_hosted.py")
    commands = parser.add_subparsers(dest="command", required=True)
    names = commands.add_parser("user-names")
    names.add_argument("--map", default=None)
    names.add_argument("emails", nargs="+")
    admins = commands.add_parser("admins")
    admins.add_argument("values", nargs="*")
    args = parser.parse_args(argv)
    try:
        if args.command == "admins":
            for name in sorted(parse_admins(*args.values)):
                print(name)
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
