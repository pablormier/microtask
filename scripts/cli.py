#!/usr/bin/env python3
"""Command-line interface entrypoint for microtask."""
from __future__ import annotations

import inspect
import os
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
)
from cli_helpers import (  # noqa: F401
    apply_capture_scope,
    consume_repo_selector,
    read_input_file,
    resolve_repo_selector,
    show_workspace,
)
from cli_parser import parser  # noqa: F401
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
    demo_active,
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
    is_data_workspace,
    is_unmigrated_workspace,
    missing_workspace_error,
)

# Commands that work on the application or on other folders, so they run without a workspace.
NO_WORKSPACE_COMMANDS = {"init", "migrate", "repo", "skills", "update"}


def main() -> int:
    arguments = sys.argv[1:]
    if arguments == ["help"]:
        arguments = ["--help"]
    ws = Workspace.resolve()
    try:
        arguments, repo_selector = consume_repo_selector(arguments)
        # 'mt migrate' is the one command that accepts a workspace folder still named mt-policy.yaml.
        migrating = bool(arguments) and arguments[0] == "migrate"
        if repo_selector is not None:
            selected_root = resolve_repo_selector(repo_selector, allow_unmigrated=migrating)
            if selected_root != ws.root.resolve():
                if is_data_workspace(selected_root) or (migrating and is_unmigrated_workspace(selected_root)):
                    # Restart this application on the selected data, so every root is set at import.
                    executable = ws.app_root / "mt"
                    env = {**os.environ, "MT_WORKSPACE": str(selected_root)}
                else:
                    executable, env = selected_root / "mt", foreign_env(ws)
                os.execve(str(executable), [str(executable), *arguments], env)
        error = None if migrating and is_unmigrated_workspace(ws.root) else workspace_error(ws)
        if error:
            raise ValueError(error)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    root_parser = parser()
    args = root_parser.parse_args(arguments)
    if getattr(args, "workspace_repo", None) is not None:
        root_parser.error("-R / --repo must come before the command")
    try:
        return _dispatch(root_parser, args, ws)
    except OSError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


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
    if (args.command in {"add", "note"} and args.file is None and not args.text
            and not getattr(args, "template", None) and not sys.stdin.isatty()):
        secondary_files = [getattr(args, field, None) for field in ("next_action_file", "instructions_file")]
        if any(path == "-" for path in secondary_files):
            root_parser.error("implicit body input conflicts with another file reading stdin; pass --file PATH")
        args.file = "-"
    if args.command == "edit":
        _validate_edit_args(root_parser, args)
    if args.command in {"add", "note"}:
        _validate_capture_args(root_parser, args)
    # Input is read before acquiring locks. Only one input may consume stdin.
    input_files = [getattr(args, field, None) for field in
                   ("file", "replace_file", "next_action_file", "instructions_file", "apply_plan")]
    if sum(path == "-" for path in input_files if path is not None) > 1:
        root_parser.error("only one input file may read stdin")
    if getattr(args, "file", None) is not None:
        try:
            args.file_text = read_input_file(args.file)
        except (OSError, UnicodeError) as error:
            root_parser.error(f"cannot read input text: {error}")
    for option, destination in (("next_action_file", "next_action_text"),
                                ("instructions_file", "instructions_text"),
                                ("replace_file", "replacements_text"),
                                ("apply_plan", "apply_text")):
        path = getattr(args, option, None)
        if path is not None:
            try:
                setattr(args, destination, read_input_file(path))
            except (OSError, UnicodeError) as error:
                root_parser.error(f"cannot read input text: {error}")
    if args.command in {"add", "note"} and getattr(args, "instructions", None) is not None:
        args.instructions_text = args.instructions
    if args.command in {"add", "note"} and args.file is not None:
        args.text = [args.file_text]
    if args.command in {"add", "note"} and args.text and not " ".join(args.text).strip() and not args.title:
        root_parser.error("an empty body requires --title")
    if args.command != "settings":
        ws.pin_demo()
        ws.maybe_seed_demo()
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
