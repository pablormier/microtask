"""Shared CLI helpers, repository selector utilities, and argument builders."""
from __future__ import annotations

import argparse
from datetime import date
import difflib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import uuid

import card_query
import store
from store import (
    Card,
    TaskError,
    _resolve_unique_card,
    _ws,
    atomic_write,
)
from workspace import (
    OLD_SETTINGS_FILE,
    Workspace,
    is_data_workspace,
    is_unmigrated_workspace,
    unmigrated_message,
)

REPO_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
TIME_QUERY_DEFAULTS = {"archived": "include", "hidden": "include"}


def emit_json(payload: object, *, indent: int | None = None) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=indent))


def command_query(args: argparse.Namespace, ws: Workspace | None = None) -> card_query.Query | None:
    """The validated query a command's selection flags describe, or None after printing the error."""
    resolved = _ws(ws)
    try:
        query = card_query.query_from_args(args, env=resolved.environ)
        card_query.validate(
            query,
            statuses=store.statuses(resolved),
            priorities=list(store.policy(resolved).get("priorities", {})),
        )
    except card_query.QueryError as error:
        print(f"error: {error}", file=sys.stderr)
        return None
    return query


def _cli_print_error(error: TaskError) -> int:
    text = str(error)
    print(f"error: {text}" if error.prefixed else text, file=sys.stderr)
    return error.exit_code


def _cli_emit_write_result(result, *, as_json: bool = False) -> int:
    history_warning = getattr(result, "history_warning", "")
    if as_json:
        payload = dict(result.receipt)
        if result.sync_error:
            payload["sync_error"] = result.sync_error
        if history_warning:
            payload["history_warning"] = history_warning
        emit_json(payload)
        return 0
    if result.message:
        print(result.message)
    if result.sync_error:
        print(f"warning: {result.sync_error}", file=sys.stderr)
    if history_warning:
        print(f"warning: {history_warning}", file=sys.stderr)
    return result.sync_exit_code


def _cli_call_op(op_call, *, as_json: bool = False) -> int:
    try:
        result = op_call()
    except TaskError as error:
        return _cli_print_error(error)
    return _cli_emit_write_result(result, as_json=as_json)


def open_in_browser(url: str) -> None:
    """Open url in the default browser without blocking the caller."""
    try:
        if "BROWSER" in os.environ:
            import webbrowser

            webbrowser.open(url)
        elif sys.platform == "darwin":
            subprocess.Popen(
                ["/usr/bin/open", url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            import webbrowser

            webbrowser.open(url)
    except OSError:
        print("warning: could not open the browser automatically", file=sys.stderr)


def repo_registry_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return root / "microtask" / "repos.json"


def load_repo_aliases() -> dict[str, str]:
    path = repo_registry_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"repository alias registry is malformed: {path}: {error}") from error
    if not isinstance(raw, dict) or any(
        not isinstance(name, str)
        or not REPO_ALIAS_RE.fullmatch(name)
        or not isinstance(repo, str)
        or not Path(repo).is_absolute()
        for name, repo in raw.items()
    ):
        raise ValueError(f"repository alias registry is malformed: {path}")
    return raw


def write_repo_aliases(aliases: dict[str, str]) -> None:
    path = repo_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, (json.dumps(aliases, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def validate_repo_workspace(path: Path, *, allow_unmigrated: bool = False) -> str | None:
    if not path.is_dir():
        return f"repository workspace does not exist: {path}"
    if not (path / "mt").exists():
        if is_data_workspace(path):
            return None
        if is_unmigrated_workspace(path):
            if allow_unmigrated:
                return None
            return unmigrated_message(path, f"mt -R {shlex.quote(str(path))} migrate")
        return (f"not a microtask workspace; a workspace folder needs cards/ and settings.yaml, "
                f"an application folder the mt application: {path}")
    # An application folder creates its own workspace on first use; an old-style one holds its settings,
    # under either file name.
    required = (path / "scripts" / "cli.py", path / "settings.yaml")
    has_settings = (path / "settings.yaml").is_file() or (path / OLD_SETTINGS_FILE).is_file()
    if has_settings or (path / "templates" / "settings.yaml").is_file():
        required = (path / "scripts" / "cli.py",)
    missing = next((candidate for candidate in required if not candidate.is_file()), None)
    if missing is not None:
        return f"not a microtask workspace; missing {missing.relative_to(path)}: {path}"
    if not os.access(path / "mt", os.X_OK):
        return f"microtask entrypoint is not executable: {path / 'mt'}"
    return None


def path_like_repo_selector(value: str) -> bool:
    return value in {".", ".."} or value.startswith("~") or "/" in value


def resolve_repo_selector(value: str, *, allow_unmigrated: bool = False) -> Path:
    if path_like_repo_selector(value):
        path = Path(value).expanduser().resolve()
    else:
        aliases = load_repo_aliases()
        if value not in aliases:
            raise ValueError(
                f"unknown repository alias '{value}'; register it with 'mt repo add {value} PATH'"
            )
        path = Path(aliases[value])
    error = validate_repo_workspace(path, allow_unmigrated=allow_unmigrated)
    if error:
        raise ValueError(error)
    return path


def consume_repo_selector(arguments: list[str]) -> tuple[list[str], str | None]:
    if not arguments:
        return arguments, None
    if arguments[0] in {"-R", "--repo"}:
        if len(arguments) == 1:
            raise ValueError(f"{arguments[0]} requires a repository path or alias")
        rest, value = arguments[2:], arguments[1]
    elif arguments[0].startswith("--repo="):
        value = arguments[0].partition("=")[2]
        if not value:
            raise ValueError("--repo requires a repository path or alias")
        rest = arguments[1:]
    elif arguments[0].startswith("-R") and len(arguments[0]) > 2:
        rest, value = arguments[1:], arguments[0][2:]
    else:
        return arguments, None
    if rest and (
        rest[0] in {"-R", "--repo"}
        or rest[0].startswith("--repo=")
        or (rest[0].startswith("-R") and len(rest[0]) > 2)
    ):
        raise ValueError("-R / --repo may only be passed once")
    return rest, value


def editor_recovery_directory(ws: Workspace | None = None) -> Path:
    path = _ws(ws).recovery_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def create_editor_recovery_copy(
    item_id: str, contents: bytes, *, ws: Workspace | None = None,
) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_-]+", "-", item_id).strip("-") or "item"
    path = editor_recovery_directory(ws) / f"{safe_id}-edit-{uuid.uuid4().hex}.md"
    atomic_write(path, contents)
    return path


def editor_command(command_name: str = "note") -> list[str]:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        raise ValueError(f"set VISUAL or EDITOR before running {command_name}")
    try:
        command = shlex.split(editor)
    except ValueError as error:
        raise ValueError(f"invalid editor command: {error}") from error
    if not command:
        raise ValueError("VISUAL or EDITOR must not be empty")
    return command


def resolve_unique(
    query: str,
    *,
    kind: str | None = None,
    statuses: list[str] | None = None,
    match_mode: str = "exact",
    ws: Workspace | None = None,
) -> tuple[Card | None, int]:
    resolved = _ws(ws)
    try:
        return _resolve_unique_card(resolved, query, kind=kind, statuses=statuses, match_mode=match_mode), 0
    except TaskError as error:
        return None, _cli_print_error(error)


def read_input_file(path: str) -> str:
    if path == "-":
        return sys.stdin.buffer.read().decode("utf-8")
    with Path(path).open(encoding="utf-8", newline="") as source_file:
        return source_file.read()


def add_session_window_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--session-since", metavar="DATE",
                         help=f"count sessions that started on or after this day: {card_query.DATE_HELP}")
    command.add_argument("--session-before", metavar="DATE",
                         help=f"count sessions that started before this day: {card_query.DATE_HELP}")


def session_window(args: argparse.Namespace) -> tuple[str | None, str | None] | None:
    """The --session-since / --session-before days (start of sessions to count), or None after an error."""
    try:
        since, before = (card_query.parse_day(getattr(args, name), name, date.today())
                         for name in ("session_since", "session_before"))
    except card_query.QueryError as error:
        print(f"error: {error}", file=sys.stderr)
        return None
    if since and before and since >= before:
        print("error: --session-since must be earlier than --session-before", file=sys.stderr)
        return None
    return since, before


def add_match_mode_arguments(command: argparse.ArgumentParser, allow_fuzzy: bool = False) -> None:
    command.description = (
        "Match task title, ID, reference, tags, and body with an exact case-insensitive substring by default. "
        "Use --and to require every word or --or to match any word. "
        "Use @ followed by a unique 3 to 8 character hexadecimal prefix to select a short reference."
    )
    if allow_fuzzy:
        command.description += " Use --fuzzy for close word matches."
    modes = command.add_mutually_exclusive_group()
    modes.add_argument("--and", dest="match_mode", action="store_const", const="and", help="require every word to match")
    modes.add_argument("--or", dest="match_mode", action="store_const", const="or", help="match any word")
    if allow_fuzzy:
        modes.add_argument("--fuzzy", dest="match_mode", action="store_const", const="fuzzy", help="allow close word matches")
    command.set_defaults(match_mode="exact")


def add_card_metadata_arguments(command: argparse.ArgumentParser, *, editing: bool) -> None:
    """Expose the same shared metadata names on create and edit commands."""
    command.add_argument("--project", help="set project metadata")
    command.add_argument("--topic", metavar="PATH",
                         help="set topic: where the card lives, a '/'-separated path such as learning/rust")
    command.add_argument("--priority", help="set priority metadata")
    command.add_argument("--due", help="set due date or local timestamp")
    command.add_argument("--due-kind", help="set the meaning of --due")
    command.add_argument("--tag", dest="tags" if not editing else "tag_add", action="append",
                         default=[] if not editing else None,
                         help="add a tag (repeatable; creation and edit)")
    if not editing:
        for name, description in (("hidden", "create a hidden card"),
                                  ("archived", "create an archived card"),
                                  ("needs-review", "mark the card as needing review")):
            dest = name.replace("-", "_")
            command.add_argument(f"--{name}", dest=dest, action="store_const", const=True,
                                 default=None, help=description)
            command.add_argument(f"--no-{name}", dest=dest, action="store_const", const=False,
                                 help=f"do not {description}")
        command.add_argument("--task-type", dest="task_type", help="set task type (tasks only)")
        return
    command.add_argument("--task-type", dest="task_type", help="set task type (tasks only)")
    command.add_argument("--status", help="set task status (tasks only; non-claim open statuses)")
    command.add_argument("--remove-tag", dest="tag_remove", action="append", default=None,
                         help="remove a tag (repeatable; preserves other tags)")
    for name, dest, description in (
        ("hidden", "hidden", "set hidden visibility"),
        ("archived", "archived", "set archived state"),
        ("needs-review", "needs_review", "set review state"),
    ):
        command.add_argument(f"--{name}", dest=dest, action="store_const", const=True,
                             default=None, help=description)
        command.add_argument(f"--no-{name}", dest=dest, action="store_const", const=False,
                             help=f"clear {description}")


def apply_capture_scope(args: argparse.Namespace, ws: Workspace | None = None) -> list[str]:
    """Default --project/--topic of add and note from MT_PROJECT/MT_TOPIC; explicit options win."""
    resolved = _ws(ws)
    applied = []
    for field, variable in (("project", "MT_PROJECT"), ("topic", "MT_TOPIC")):
        value = (resolved.env_default(variable) or "").strip()
        if value and getattr(args, field, None) is None:
            setattr(args, field, value)
            applied.append(f"{field} {value} ({variable})")
    return applied


def show_workspace(*, command: str | None = None, ws: Workspace | None = None) -> None:
    resolved = _ws(ws)
    print(f"Workspace: {resolved.root}", file=sys.stderr)
    if not resolved.in_app:
        print(f"Application: {resolved.app_root}", file=sys.stderr)


def allow_host_name(value: str) -> str:
    """Validate an --allow-host value (NAME or NAME:PORT) and return it in lowercase."""
    text = value.strip().lower()
    if text.startswith("["):  # An IPv6 literal keeps its brackets.
        name, _, rest = text.partition("]")
        name, port = name + "]", rest[1:] if rest.startswith(":") else rest
    else:
        name, _, port = text.partition(":")
    valid = (len(name.strip("[]")) > 0 and (name.startswith("[") == name.endswith("]")) and not any(char in "[]" for char in name[1:-1])
             and (name.startswith("[") or ":" not in port)
             and not any(char.isspace() or char in "/\\*@?#" for char in text)
             and (text == name or (port.isdigit() and 0 < int(port) < 65536)))
    if not valid:
        raise argparse.ArgumentTypeError(f"invalid host name {value!r}; use NAME or NAME:PORT, without wildcards or paths")
    return text


def add_web_arguments(parser: argparse.ArgumentParser, *, shared: bool = True) -> None:
    """Keep the public CLI and standalone dashboard entry point consistent."""
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    parser.add_argument("--host", default="127.0.0.1", help="server host; defaults to 127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="server port; defaults to an available port")
    parser.add_argument("--allow-host", action="append", default=[], type=allow_host_name, metavar="NAME",
                        help="also accept this Host header name (NAME or NAME:PORT; any port when none is given); "
                             "repeatable. Use it behind a reverse proxy that keeps the public Host; "
                             "otherwise configure the proxy to send the backend address as Host")
    if shared:
        parser.add_argument("--trust-proxy-user", action="store_true",
                            help="serve one shared workspace to several people behind a sign-in proxy: read who is asking "
                                 "from X-Auth-Request-Email, but only on requests that also carry the proxy secret")
        parser.add_argument("--require-proxy-secret", action="store_true",
                            help="keep the single-user dashboard but answer 403 to any request that lacks the proxy "
                                 "secret in X-Microtask-Proxy-Secret, so other programs on this machine cannot use it; "
                                 "needs the secret like --trust-proxy-user and cannot be combined with it")
        parser.add_argument("--proxy-secret-file", metavar="FILE",
                            help="file holding the secret the proxy sends in X-Microtask-Proxy-Secret "
                                 "(or set MT_PROXY_SECRET_FILE); required with --trust-proxy-user "
                                 "or --require-proxy-secret")
        parser.add_argument("--user-map", metavar="FILE",
                            help="lines of 'EMAIL USER' that name the user for an email "
                                 "(or set MT_USER_MAP); other emails use the part before the @")
        parser.add_argument("--admin", action="append", default=[], metavar="USER",
                            help="user name allowed to delete cards, take over claims and rename topics; "
                                 "repeatable, or set MT_ADMINS to a comma-separated list")
        parser.add_argument("--sign-out-url", metavar="URL",
                            help="URL for the dashboard's Sign out link (or set MT_SIGN_OUT_URL), "
                                 "such as /oauth2/sign_out: a path starting with / or an https:// URL. "
                                 "Works with --trust-proxy-user or --require-proxy-secret, and is refused "
                                 "without one of them; no link is shown when unset")
    card_query.add_query_arguments(parser)


class MtArgumentParser(argparse.ArgumentParser):
    """Add a Git-style "did you mean" line to unknown option and command errors."""

    def parse_known_args(self, args=None, namespace=None):
        known, extras = super().parse_known_args(args, namespace)
        options = list(self._option_string_actions)
        hints = []
        for extra in extras:
            if extra.startswith("-"):
                hints += difflib.get_close_matches(extra.split("=", 1)[0], options, n=3)
        if hints and not hasattr(known, "did_you_mean"):
            known.did_you_mean = (self.prog, hints)
        return known, extras

    def parse_args(self, args=None, namespace=None):
        known, extras = self.parse_known_args(args, namespace)
        if extras:
            prog, hints = getattr(known, "did_you_mean", (self.prog, []))
            self.error("unrecognized arguments: " + " ".join(extras), prog, hints)
        return known

    def error(self, message, hint_prog=None, hints=()):
        for action in self._actions:
            if isinstance(action, argparse._SubParsersAction):
                match = re.match(r"argument (?!-).*?: invalid choice: '([^']*)'", message)
                if match and "maybe you meant" not in message:
                    hint_prog, hints = self.prog, difflib.get_close_matches(match.group(1), list(action.choices), n=3)
        self.print_usage(sys.stderr)
        text = f"{self.prog}: error: {message}\n"
        if hints:
            text += f"{hint_prog}: did you mean {' or '.join(hints)}?\n"
        self.exit(2, text)
