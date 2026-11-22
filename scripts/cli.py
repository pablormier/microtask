#!/usr/bin/env python3
"""Command-line interface entrypoint for microtask."""
from __future__ import annotations

import inspect
import os
from pathlib import Path
import shlex
import subprocess  # noqa: F401 - exposed for tests patching cli.subprocess
import sys

import card_format  # noqa: F401 - exposed for tests patching cli.card_format
from card_format import is_timestamp  # noqa: F401
import card_query
from cli_admin import command_web  # noqa: F401
from cli_cards import (  # noqa: F401
    _validate_capture_args,
    _validate_edit_args,
    command_create,
    command_move,
    command_topic_rename,
    prepare_command_inputs,
)
from cli_helpers import (  # noqa: F401
    apply_capture_scope,
    consume_repo_selector,
    is_remote_url,
    read_input_file,
    resolve_repo_selector,
    show_workspace,
    validate_remote_url,
)
from cli_parser import parser  # noqa: F401
import remote
from cli_time import (  # noqa: F401
    command_time_add,
    command_time_end,
    command_time_remove,
    command_time_start,
)
from operations import capture_title, render_converted_note  # noqa: F401
from store import (  # noqa: F401
    Card,
    ReferenceIndex,
    foreign_env,
    local_stamp,
    now_stamp,
    time_log_entries,
    workspace_error,
)
from transaction import (  # noqa: F401
    command_sync,
    managed_temp_directory,
    time_mutation_lock,
    validate_item_changes,
)
from workspace import (  # noqa: F401
    Workspace,
    enclosing_workspace,
    is_data_workspace,
    is_unmigrated_workspace,
    missing_workspace_error,
)

# Commands that work on the application or on other folders, so they run without a workspace.
NO_WORKSPACE_COMMANDS = {"demo", "init", "migrate", "repo", "skills", "update"}
# Preserve an explicit selection when the launcher executes mt again.
_EXPLICIT_APP = "_MT_EXPLICIT_APP"


def main() -> int:
    arguments = sys.argv[1:]
    if arguments == ["help"]:
        arguments = ["--help"]
    explicit_app = os.environ.pop(_EXPLICIT_APP, None)
    remote_url: str | None = None
    ws: Workspace | None = None
    try:
        arguments, repo_selector = consume_repo_selector(arguments)
        # 'mt migrate' is the one command that accepts a workspace folder still named mt-policy.yaml.
        migrating = bool(arguments) and arguments[0] == "migrate"
        demo_cmd = bool(arguments) and arguments[0] == "demo" and repo_selector is None
        if repo_selector is not None and is_remote_url(repo_selector):
            remote_url = validate_remote_url(repo_selector)
        elif repo_selector is None and os.environ.get("MT_REMOTE", "").strip():
            remote_url = validate_remote_url(os.environ["MT_REMOTE"])
        else:
            ws = Workspace.resolve()
            if repo_selector is not None:
                selected_root = resolve_repo_selector(repo_selector, allow_unmigrated=migrating)
                if selected_root != ws.root:
                    if is_data_workspace(selected_root) or (migrating and is_unmigrated_workspace(selected_root)):
                        executable = ws.app_root / "mt"
                        env = {**ws.environ, "MT_WORKSPACE": str(selected_root)}
                    else:
                        executable, env = selected_root / "mt", foreign_env(ws)
                    env.pop("MT_REMOTE", None)
                    env[_EXPLICIT_APP] = str(executable.parent.resolve())
                    os.execve(str(executable), [str(executable), *arguments], env)
            error = None if (migrating and is_unmigrated_workspace(ws.root)) or demo_cmd else workspace_error(ws)
            if error:
                raise ValueError(error)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    root_parser = parser()
    args = root_parser.parse_args(arguments)
    if getattr(args, "workspace_repo", None) is not None:
        root_parser.error("-R / --repo must come before the command")
    if remote_url is not None:
        return remote.dispatch_remote(remote_url, root_parser, args)
    assert ws is not None
    try:
        needs_workspace = args.command not in {"demo", "init", "repo", "update"}
        if needs_workspace and repo_selector is None and explicit_app != str(ws.app_root.resolve()):
            ws = select_enclosing_workspace(ws)
        return _dispatch(root_parser, args, ws)
    except OSError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


def select_enclosing_workspace(ws: Workspace) -> Workspace:
    current = enclosing_workspace(Path.cwd())
    if current is None or current == ws.root.resolve():
        return ws
    configured = ws.environ.get("MT_WORKSPACE")
    if configured:
        raise ValueError(f"current workspace is {current}, but MT_WORKSPACE selects {configured}. "
                         f"Select this workspace explicitly with mt -R {shlex.quote(str(current))} COMMAND")
    if not is_data_workspace(current):
        raise ValueError(f"current workspace is {current}. "
                         f"Select it explicitly with mt -R {shlex.quote(str(current))} COMMAND")
    return Workspace.resolve({**ws.environ, "MT_WORKSPACE": str(current)}, app_root=ws.app_root)


def _dispatch(root_parser, args, ws: Workspace) -> int:
    if args.command in NO_WORKSPACE_COMMANDS:
        return args.func(args, ws=ws) if "ws" in inspect.signature(args.func).parameters else args.func(args)
    missing = missing_workspace_error(ws.environ, app_root=ws.app_root)
    if missing:
        print(f"error: {missing}", file=sys.stderr)
        return 2
    scope = apply_capture_scope(args, ws=ws) if args.command in {"add", "note"} else card_query.env_scope(args, ws.environ)
    if args.command == "run":
        if args.argv and args.argv[0] == "--":
            args.argv = args.argv[1:]
        if not args.argv or not args.argv[0]:
            root_parser.error("run requires a command")
        return args.func(args, ws=ws)
    prepare_command_inputs(root_parser, args)
    if args.command in {
        "add", "note", "capture", "time", "focus", "list", "search", "read", "show", "edit", "bulk-edit",
        "comment", "move", "convert", "done", "delete", "topic", "sync", "validate", "draft",
    }:
        show_workspace(command=args.command, ws=ws)
    if scope:
        print("Scope: " + ", ".join(scope), file=sys.stderr)
    if "ws" in inspect.signature(args.func).parameters:
        return args.func(args, ws=ws)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
