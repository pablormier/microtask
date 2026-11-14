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
from cli_admin import (  # noqa: F401
    APPLICATION_FILES,
    APPLICATION_FOLDERS,
    SKILL_COMMAND,
    SKILL_MARKER,
    SKILL_TARGETS,
    SKILL_WORKSPACE_BLOCK,
    command_import_github,
    command_info,
    command_init,
    command_policy,
    command_repo_add,
    command_repo_list,
    command_repo_remove,
    command_run,
    command_scratch,
    command_skills_install,
    command_skills_uninstall,
    command_update,
    command_validate,
    command_view,
    command_web,
    copy_application,
    create_view,
    init_data_workspace,
    installed_by_microtask,
    portable_invocation,
    render_skill_file,
    skill_sources,
    skills_invocation,
    validated_view_items,
    view_name_from_args,
)
from cli_cards import (  # noqa: F401
    _apply_bulk_plan_locked,
    _bulk_preview,
    _command_bulk_edit_apply,
    _command_edit_serialized,
    _edit_body_mode_from_args,
    _edit_metadata_from_args,
    _read_bulk_plan,
    _read_item_data,
    _run_card_query,
    _run_mdviz,
    _validate_capture_args,
    _validate_edit_args,
    command_add,
    command_bulk_edit,
    command_capture,
    command_card_metadata,
    command_comment,
    command_convert,
    command_create,
    command_delete,
    command_done,
    command_edit,
    command_focus,
    command_followup,
    command_list,
    command_move,
    command_note_editor,
    command_read,
    command_search,
    command_show,
    command_topic_list,
    command_topic_rename,
    command_unfocus,
    parse_edit_tags,
    save_terminal_capture,
    sync_error_after_json_save,
)
from cli_drafts import (  # noqa: F401
    command_draft_create,
    command_draft_discard,
    command_draft_edit,
    command_draft_list,
    command_draft_read,
    command_draft_recover,
    command_draft_save,
    draft_conflict_copy,
)
from cli_helpers import (  # noqa: F401
    REPO_ALIAS_RE,
    TIME_QUERY_DEFAULTS,
    MtArgumentParser,
    _cli_print_error,
    _cli_run_write,
    add_card_metadata_arguments,
    add_match_mode_arguments,
    add_session_window_arguments,
    add_web_arguments,
    apply_capture_scope,
    consume_repo_selector,
    create_editor_recovery_copy,
    editor_command,
    editor_recovery_directory,
    load_repo_aliases,
    open_in_browser,
    path_like_repo_selector,
    read_input_file,
    repo_registry_path,
    resolve_repo_selector,
    resolve_unique,
    session_window,
    show_workspace,
    validate_repo_workspace,
    write_repo_aliases,
)
from cli_parser import parser  # noqa: F401
from cli_time import (  # noqa: F401
    command_time_add,
    command_time_end,
    command_time_list,
    command_time_remove,
    command_time_report,
    command_time_start,
    command_time_status,
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
)


def main() -> int:
    arguments = sys.argv[1:]
    if arguments == ["help"]:
        arguments = ["--help"]
    ws = Workspace.resolve()
    try:
        arguments, repo_selector = consume_repo_selector(arguments)
        if repo_selector is not None:
            selected_root = resolve_repo_selector(repo_selector)
            if selected_root != ws.root.resolve():
                if is_data_workspace(selected_root):
                    # Restart this application on the selected data, so every root is set at import.
                    executable = ws.app_root / "mt"
                    env = {**os.environ, "MT_WORKSPACE": str(selected_root)}
                else:
                    executable, env = selected_root / "mt", foreign_env(ws)
                os.execve(str(executable), [str(executable), *arguments], env)
        error = workspace_error(ws)
        if error:
            raise ValueError(error)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    root_parser = parser()
    args = root_parser.parse_args(arguments)
    scope = apply_capture_scope(args, ws=ws) if args.command in {"add", "note"} else card_query.env_scope(args)
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
                   ("file", "replace_file", "next_action_file", "instructions_file")]
    if sum(path == "-" for path in input_files if path is not None) > 1:
        root_parser.error("only one input file may read stdin")
    if getattr(args, "file", None) is not None:
        try:
            args.file_text = read_input_file(args.file)
            args.section_text = args.file_text
        except (OSError, UnicodeError) as error:
            root_parser.error(f"cannot read input text: {error}")
    for option, destination in (("next_action_file", "next_action_text"),
                                ("instructions_file", "instructions_text"),
                                ("replace_file", "replacements_text")):
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
    if args.command not in {"policy", "view", "scratch"}:
        ws.pin_demo()
        ws.maybe_seed_demo()
    if args.command in {
        "add", "note", "capture", "time", "focus", "unfocus", "followup", "list", "search", "view", "show", "edit", "bulk-edit",
        "comment", "move", "convert", "done", "delete", "sync", "validate", "draft",
    }:
        show_workspace(command=args.command, ws=ws)
    if scope:
        print("Scope: " + ", ".join(scope), file=sys.stderr)
    if "ws" in inspect.signature(args.func).parameters:
        return args.func(args, ws=ws)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
