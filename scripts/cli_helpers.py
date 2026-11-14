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
    demo_active,
)
from transaction import (
    _run_auto_sync,
    run_auto_sync_for_receipt,
    time_mutation_lock,
)
from workspace import Workspace, is_data_workspace

REPO_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
TIME_QUERY_DEFAULTS = {"archived": "include", "hidden": "include"}


def _cli_print_error(error: TaskError) -> int:
    text = str(error)
    print(f"error: {text}" if error.prefixed else text, file=sys.stderr)
    return error.exit_code


def _cli_run_write(
    ws: Workspace | None,
    operation,
    *,
    as_json: bool = False,
    sync: bool = True,
) -> int:
    resolved = _ws(ws)
    try:
        with time_mutation_lock(resolved):
            result = operation(resolved)
    except TaskError as error:
        if sync and error.sync_needed and store.policy(resolved).get("rules", {}).get("auto_sync", False):
            if as_json:
                run_auto_sync_for_receipt(ws=resolved)
            else:
                _run_auto_sync(ws=resolved)
        return _cli_print_error(error)
    if as_json:
        payload = dict(result.receipt)
        if sync and result.changed and store.policy(resolved).get("rules", {}).get("auto_sync", False):
            sync_error = run_auto_sync_for_receipt(ws=resolved)
            if sync_error:
                payload["sync_error"] = sync_error
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    if result.message:
        print(result.message)
    if sync and result.changed and store.policy(resolved).get("rules", {}).get("auto_sync", False):
        return _run_auto_sync(ws=resolved)
    return 0


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


def validate_repo_workspace(path: Path) -> str | None:
    if not path.is_dir():
        return f"repository workspace does not exist: {path}"
    if not (path / "mt").exists():
        if is_data_workspace(path):
            return None
        return (f"not a microtask workspace; a data-only workspace needs cards/ and mt-policy.yaml, "
                f"a full one the mt application: {path}")
    required = (path / "scripts" / "cli.py", path / "mt-policy.yaml")
    missing = next((candidate for candidate in required if not candidate.is_file()), None)
    if missing is not None:
        return f"not a microtask workspace; missing {missing.relative_to(path)}: {path}"
    if not os.access(path / "mt", os.X_OK):
        return f"microtask entrypoint is not executable: {path / 'mt'}"
    return None


def path_like_repo_selector(value: str) -> bool:
    return value in {".", ".."} or value.startswith("~") or "/" in value


def resolve_repo_selector(value: str) -> Path:
    if path_like_repo_selector(value):
        path = Path(value).expanduser().resolve()
    else:
        aliases = load_repo_aliases()
        if value not in aliases:
            raise ValueError(
                f"unknown repository alias '{value}'; register it with 'mt repo add {value} PATH'"
            )
        path = Path(aliases[value])
    error = validate_repo_workspace(path)
    if error:
        raise ValueError(error)
    return path


def consume_repo_selector(arguments: list[str]) -> tuple[list[str], str | None]:
    if not arguments:
        return arguments, None
    if arguments[0] in {"-R", "--repo"}:
        if len(arguments) == 1:
            raise ValueError(f"{arguments[0]} requires a repository path or alias")
        return arguments[2:], arguments[1]
    if arguments[0].startswith("--repo="):
        value = arguments[0].partition("=")[2]
        if not value:
            raise ValueError("--repo requires a repository path or alias")
        return arguments[1:], value
    return arguments, None


def editor_recovery_directory(ws: Workspace | None = None) -> Path:
    path = _ws(ws).recovery_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def create_editor_recovery_copy(
    item_id: str, contents: bytes, *, metadata: dict[str, object] | None = None, ws: Workspace | None = None,
) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_-]+", "-", item_id).strip("-") or "item"
    path = editor_recovery_directory(ws) / f"{safe_id}-edit-{uuid.uuid4().hex}.md"
    atomic_write(path, contents)
    if metadata is not None:
        sidecar = path.with_suffix(".meta.json")
        atomic_write(sidecar, (json.dumps({"version": 1, **metadata}, ensure_ascii=False) + "\n").encode("utf-8"))
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
        command.add_argument("--type", dest="task_type", help="set task type (tasks only)")
        return
    command.add_argument("--type", dest="task_type", help="set task type (tasks only)")
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
    if resolved.root != resolved.app_root:
        print(f"Application: {resolved.app_root}", file=sys.stderr)
    if demo_active(resolved) and command not in {"add", "note"}:
        print(
            "Demo mode: bundled example tasks and notes — every command works on them. "
            'Create your first item (e.g. ./mt add "my task") to start your own workspace.',
            file=sys.stderr,
        )


def add_web_arguments(parser: argparse.ArgumentParser) -> None:
    """Keep the public CLI and standalone dashboard entry point consistent."""
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    parser.add_argument("--host", default="127.0.0.1", help="server host; defaults to 127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="server port; defaults to an available port")
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
