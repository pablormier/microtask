"""Argument parser construction for the microtask CLI."""
from __future__ import annotations

import argparse
from pathlib import Path
import textwrap

import card_format
import card_query
from cli_admin import (
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
)
from cli_cards import (
    command_add,
    command_bulk_edit,
    command_capture,
    command_card_metadata,
    command_comment,
    command_convert,
    command_delete,
    command_done,
    command_edit,
    command_focus,
    command_followup,
    command_list,
    command_move,
    command_read,
    command_search,
    command_show,
    command_topic_list,
    command_topic_rename,
    command_unfocus,
    parse_edit_tags,
)
from cli_drafts import (
    command_draft_create,
    command_draft_discard,
    command_draft_edit,
    command_draft_list,
    command_draft_read,
    command_draft_recover,
    command_draft_save,
)
from cli_helpers import (
    TIME_QUERY_DEFAULTS,
    MtArgumentParser,
    add_card_metadata_arguments,
    add_match_mode_arguments,
    add_session_window_arguments,
    add_web_arguments,
)
from cli_time import (
    command_time_add,
    command_time_end,
    command_time_list,
    command_time_remove,
    command_time_report,
    command_time_start,
    command_time_status,
)
from transaction import command_sync


def _register_admin_parsers(sub: argparse._SubParsersAction) -> None:
    info = sub.add_parser("info", help="show workspace, Git remotes, and task counts")
    info.set_defaults(func=command_info)

    init = sub.add_parser(
        "init", help="create another workspace, e.g. for a project, without changing which mt you run",
        description="Create a new workspace in DIRECTORY, for example one where a project's agents track their "
                    "work. Your shell config and plain mt stay as they are. By default it runs this workspace's "
                    "install.sh --no-path, which clones microtask from the internet (latest version, ready for "
                    "mt update, and for mt sync when ORIGIN-URL is given). --local works offline: it copies "
                    "this workspace's application files and settings, never its cards, drafts, or local state. "
                    "A --local copy has no Git; to add sync and updates later, run git init there, commit, "
                    "and run its ./install.sh --no-path. --data creates a data-only workspace instead: cards, "
                    "your settings, and a Git repository (with ORIGIN-URL as origin), but no application; this "
                    "mt runs it through -R, so one mt update covers it. An existing workspace is left "
                    "unchanged; any other existing path is refused.",
    )
    init.add_argument("directory", metavar="DIRECTORY", help="folder to create")
    init.add_argument("origin", nargs="?", metavar="ORIGIN-URL",
                      help="private repository for mt sync (not with --local)")
    init.add_argument("--local", action="store_true",
                      help="copy this workspace's application instead of downloading it; works offline")
    init.add_argument("--data", action="store_true",
                      help="create a data-only workspace: cards and your settings, no application. This mt "
                           "runs it with -R; mt update here keeps it current. Works offline")
    init.set_defaults(func=command_init)

    skills = sub.add_parser(
        "skills", help="install microtask's agent skills into a project",
        description="Copy every skill this mt ships into a project's .agents/skills and .claude/skills, with "
                    "their commands pointed at the selected workspace: plain mt for the mt on PATH, mt -R ALIAS "
                    "for a workspace with an alias, otherwise its path. Folders already there that microtask "
                    "did not install are never changed. Examples: mt skills install --project myapp, "
                    "mt -R pkg skills install, mt skills uninstall",
    )
    skills_sub = skills.add_subparsers(dest="skills_command", required=True)
    skills_install = skills_sub.add_parser(
        "install", help="install or refresh the skills; removes ones microtask no longer ships")
    skills_install.add_argument("--project", help="scope every command the skills run to this project (MT_PROJECT)")
    skills_install.add_argument("--topic", metavar="PATH",
                                help="scope every command the skills run to this topic (MT_TOPIC)")
    skills_install.add_argument("--dir", default=".", help="project folder (default: current folder)")
    skills_install.set_defaults(func=command_skills_install)
    skills_uninstall = skills_sub.add_parser("uninstall", help="remove the skills microtask installed")
    skills_uninstall.add_argument("--dir", default=".", help="project folder (default: current folder)")
    skills_uninstall.set_defaults(func=command_skills_uninstall)

    run = sub.add_parser(
        "run",
        help="run a command in the selected workspace",
        usage="mt run [-h] [--] COMMAND [ARGS...]",
        description="Run COMMAND in the selected workspace with arguments unchanged. "
                    "Inherits the environment and terminal; returns the command's exit status.",
    )
    run.add_argument("argv", nargs=argparse.REMAINDER, metavar="COMMAND",
                     help="executable followed by its arguments; use -- before COMMAND if needed")
    run.set_defaults(func=command_run)

    policy_command = sub.add_parser("policy", help="show effective workspace policy",
                                    description="Show effective workspace policy in a readable format, or use --json for automation.")
    policy_command.add_argument("--json", dest="as_json", action="store_true", help="print effective policy as JSON")
    policy_command.set_defaults(func=command_policy)

    scratch = sub.add_parser("scratch", help="create a private scratch directory in the selected workspace")
    scratch.add_argument("--json", dest="as_json", action="store_true", help="print the absolute directory path as JSON")
    scratch.set_defaults(func=command_scratch)

    repo = sub.add_parser("repo", help="manage machine-local repository aliases")
    repo_sub = repo.add_subparsers(dest="repo_command", required=True)
    repo_add = repo_sub.add_parser("add", help="register a repository alias")
    repo_add.add_argument("name", help="alias name")
    repo_add.add_argument("path", help="microtask workspace path: a full workspace or a data-only folder")
    repo_add.set_defaults(func=command_repo_add)
    repo_list = repo_sub.add_parser("list", help="list repository aliases")
    repo_list.set_defaults(func=command_repo_list)
    repo_remove = repo_sub.add_parser("remove", help="remove a repository alias")
    repo_remove.add_argument("name", help="alias name")
    repo_remove.set_defaults(func=command_repo_remove)

    update = sub.add_parser("update", help="update application files from upstream/main")
    update.add_argument("--check", action="store_true", help="preview application changes")
    update.add_argument("--json", dest="as_json", action="store_true", help="print the --check preview as JSON")
    update.add_argument("--force", action="store_true", help="compatibility option; application files always follow upstream")
    update.set_defaults(func=command_update)

    sync = sub.add_parser(
        "sync",
        help="sync task changes with origin",
        description=(
            "Commit card, draft, and policy changes, merge origin, and push. Refuses when origin is the "
            "microtask application repository, or, when the GitHub CLI (gh) is available, a public "
            "GitHub repository unless rules.allow_public_origin is true."
        ),
    )
    sync.add_argument("-m", "--message", help="override the generated commit message")
    sync.set_defaults(func=command_sync)

    validate = sub.add_parser("validate", help="validate all cards")
    validate.set_defaults(func=command_validate)

    web = sub.add_parser(
        "web", help="open the local web dashboard",
        description="Open the local web dashboard. Board filters are kept in the page address, so a "
                    "reload keeps them. The selection filters of list (for example --project, --topic, --tag, "
                    "--updated-since 3m) open the board already filtered; repeat one to show cards matching "
                    "any of its values.",
    )
    add_web_arguments(web)
    web.set_defaults(func=command_web)

    imported = sub.add_parser("import-github", help="capture open labeled GitHub issues for later review")
    imported.add_argument("--repo", metavar="OWNER/REPO")
    imported.add_argument("--json", dest="as_json", action="store_true", help=argparse.SUPPRESS)
    imported.set_defaults(func=command_import_github)


def _register_draft_parsers(sub: argparse._SubParsersAction) -> None:
    draft = sub.add_parser("draft", help="manage incomplete workspace drafts")
    draft_sub = draft.add_subparsers(dest="draft_command", required=True)
    draft_list = draft_sub.add_parser("list", help="list canonical workspace drafts")
    draft_list.add_argument("--json", dest="as_json", action="store_true", help="print draft metadata and Markdown bodies as JSON")
    draft_list.set_defaults(func=command_draft_list)
    draft_create = draft_sub.add_parser("create", help="create a task or note draft, or an edit draft for an existing card")
    draft_create.add_argument("kind", choices=("note", "task"), help="draft kind")
    draft_create.add_argument("--target", help="item ref or exact title to edit")
    draft_create.add_argument("--id", dest="draft_id", help="stable UUID for an imported or recovered draft")
    draft_create.add_argument("--title", help="single-line title or label for the draft")
    draft_create.add_argument("--capture-id", help="preserve a source capture UUID for safe retries")
    draft_create.add_argument("--created-at", help="local ISO timestamp for a new task or note draft")
    draft_create.add_argument("--file", metavar="PATH", help="initial Markdown body; - reads stdin")
    draft_create.add_argument("--json", dest="as_json", action="store_true", help="print the saved draft as JSON")
    draft_create.set_defaults(func=command_draft_create, section_text="")
    draft_read = draft_sub.add_parser("read", help="print one draft body or its JSON record")
    draft_read.add_argument("draft_id")
    draft_read.add_argument("--json", dest="as_json", action="store_true", help="print metadata and body as JSON")
    draft_read.set_defaults(func=command_draft_read)
    draft_edit = draft_sub.add_parser("edit", help="replace a draft body using its current revision")
    draft_edit.add_argument("draft_id")
    draft_edit.add_argument("--file", metavar="PATH", help="replacement Markdown body; - reads stdin; requires --expected-revision")
    draft_edit.add_argument("--title", help="replace the draft title")
    draft_edit.add_argument("--expected-revision", type=int, help="revision returned by draft read/list; editor sessions use the revision loaded at start")
    draft_edit.add_argument("--json", dest="as_json", action="store_true", help="print the updated draft as JSON")
    draft_edit.set_defaults(func=command_draft_edit)
    draft_discard = draft_sub.add_parser("discard", help="discard one draft")
    draft_discard.add_argument("draft_id")
    draft_discard.add_argument("--expected-revision", type=int, help="refuse if the draft changed since it was read")
    draft_discard.add_argument("--json", dest="as_json", action="store_true", help="print a JSON receipt")
    draft_discard.set_defaults(func=command_draft_discard)
    draft_save = draft_sub.add_parser("save", help="create or update the card represented by a draft")
    draft_save.add_argument("draft_id")
    draft_save.add_argument("--expected-revision", required=True, type=int, help="revision returned by draft read/list")
    draft_save.add_argument("--expected-hash", help="content hash returned by draft read/list")
    draft_save.add_argument("--json", dest="as_json", action="store_true", help="print the card receipt as JSON")
    draft_save.set_defaults(func=command_draft_save)
    draft_recover = draft_sub.add_parser("recover", help="import editor recovery copies from .mt/recovery/edit")
    draft_recover.add_argument("--json", dest="as_json", action="store_true", help="print imported draft records as JSON")
    draft_recover.set_defaults(func=command_draft_recover)


def _register_capture_parsers(sub: argparse._SubParsersAction) -> None:
    capture_form = sub.add_parser(
        "capture", help="create a task or note with a keyboard-driven review",
        description="Human-only interactive terminal capture. Arrow keys select options; Enter accepts defaults. "
                    "When writing text, Enter adds a line and Ctrl+D finishes. "
                    "Review text and optional properties before creating. Ctrl+C or Esc cancels. "
                    "Agents and scripts must use add, note, or edit instead.",
    )
    capture_form.add_argument("text", nargs="*", help="optional initial capture text")
    capture_form.set_defaults(func=command_capture)

    # `mt note` is `mt add --kind note`: one option set, registered once.
    add_description = (
        "Save supplied text as the Markdown body using policy defaults. Optional processing instructions and "
        "task next action are saved as properties. With no main text or --file, redirected stdin supplies the "
        "body. Choose an open status with --status; Next action is optional. Doing tasks are claimed on creation."
    )
    note_description = (
        "Same as add --kind note: task-only options are rejected. With no text or --file, redirected stdin "
        "supplies the body; on a terminal $VISUAL/$EDITOR opens for a human."
    )
    for name, description in (
        ("add", "create a task or note using policy defaults or explicit metadata"),
        ("note", "create a note (alias of add --kind note; no text opens the editor)"),
    ):
        capture = sub.add_parser(
            name, help=description,
            description=(note_description if name == "note" else add_description),
        )
        capture.add_argument("text", nargs="*", help="Markdown body; cannot be combined with --file")
        capture.add_argument("--file", metavar="PATH", help="read UTF-8 Markdown body from a file; - reads stdin")
        capture.add_argument("--instructions", help="set optional Processing instructions in the same creation transaction")
        capture.add_argument("--instructions-file", metavar="PATH", help="read Processing instructions from a UTF-8 file")
        capture.add_argument("--title", help="use a separate single-line title; otherwise derive it from capture text (up to 80 characters); full text stays in the body")
        if name == "add":
            capture.add_argument("--kind", choices=("task", "note"),
                                 help="create a task (default) or note")
        add_card_metadata_arguments(capture, editing=False)
        capture.add_argument("--status", help="task only: initial open status; defaults to workspace policy")
        capture.add_argument("--next-action", help="task only: optional first step or reminder of where to resume")
        capture.add_argument("--next-action-file", metavar="PATH", help="task only: read Next action from a UTF-8 file")
        capture.add_argument("--by", metavar="USERNAME",
                             help="task only: claim identity for doing; defaults to MT_USERNAME, default_username, or the system username")
        capture.add_argument("--capture", action="store_true",
                             help="save supplied text as a quick capture (notes default to needing review)")
        capture.add_argument("--template", choices=("meeting",),
                             help="note only: seed an editor draft with a meeting outline")
        capture.add_argument("--capture-id", help=argparse.SUPPRESS)
        capture.add_argument("--created-at", help=argparse.SUPPRESS)
        capture.add_argument("--json", dest="as_json", action="store_true",
                             help="return a deterministic saved receipt as JSON")
        capture.set_defaults(func=command_add, kind="task" if name == "add" else "note")


def _register_time_parsers(sub: argparse._SubParsersAction) -> None:
    time_command = sub.add_parser(
        "time", help="track and report task time",
        description="Timers belong to a username (--by, MT_USERNAME, default_username, or system username). "
        "Start claims the task in doing and saves/switches your other timers unless --parallel is given. "
        "Leaving doing, completing, hiding, archiving, or taking over saves the running session. "
        "Stop/cancel without a task selects your only timer. Status lists all owners and missing tasks; "
        "cancel TASK_ID clears an orphan. Malformed local state is moved to .mt/recovery/ with a warning. "
        "rules.timer_warn_hours (default 8) warns without capping time. Timestamps are naive local time; "
        "elapsed_seconds is authoritative and reports sum it. Running timers stay local to this checkout.",
    )
    time_sub = time_command.add_subparsers(dest="time_command", required=True)
    time_start = time_sub.add_parser("start", help="claim an open task in doing; save and switch your current timer")
    time_start.add_argument("--parallel", action="store_true", help="keep your other timers running")
    time_start.add_argument("query", nargs="+", help="task title, id, or short reference")
    time_start.add_argument("--by", metavar="USERNAME",
                            help="username; defaults to MT_USERNAME, default_username, or the system username")
    time_start.set_defaults(func=command_time_start)
    time_stop = time_sub.add_parser("stop", help="stop and save a task timer")
    time_stop.add_argument("query", nargs="*", help="task title, id, or short reference")
    time_stop.add_argument("--by", metavar="USERNAME", help="timer owner; defaults to your current username")
    stop_end = time_stop.add_mutually_exclusive_group()
    stop_end.add_argument("--at", metavar="TIME", help="end at local YYYY-MM-DDTHH:MM[:SS] or today's HH:MM[:SS]")
    stop_end.add_argument("--duration", help="record this duration from the timer start, e.g. 2h")
    time_stop.set_defaults(func=command_time_end)
    time_cancel = time_sub.add_parser("cancel", help="discard a running timer without recording time")
    time_cancel.add_argument("query", nargs="*", help="task title, id, or short reference")
    time_cancel.add_argument("--by", metavar="USERNAME", help="timer owner; defaults to your current username")
    time_cancel.set_defaults(func=command_time_end)
    time_status = time_sub.add_parser("status", help="show running timers and saved totals")
    time_status.set_defaults(func=command_time_status)
    time_list = time_sub.add_parser("list", help="list recorded sessions with IDs for removal")
    time_list.add_argument("query", nargs="*", help="optional task title, id, or short reference")
    time_list.description = (
        "List recorded sessions, newest first. Selection filters pick the tasks; unlike list, they include "
        "archived and hidden tasks unless you pass --archived exclude or --hidden exclude. "
        "--session-since and --session-before count only sessions that started inside that window.")
    card_query.add_query_arguments(time_list, defaults=TIME_QUERY_DEFAULTS)
    add_session_window_arguments(time_list)
    time_list.set_defaults(func=command_time_list)
    time_remove = time_sub.add_parser("remove", help="remove one recorded session by its ID or unique prefix")
    time_remove.add_argument("session_id", help="session ID from time list; at least 8 hexadecimal characters")
    time_remove.set_defaults(func=command_time_remove)
    time_add = time_sub.add_parser("add", help="add a missed session with a duration or explicit local start/end")
    time_add.add_argument("query", help="task title, id, or short reference; quote titles with spaces")
    time_add.add_argument("duration", nargs="?", help="duration such as 45s, 90m, or 1h30m; omit with --start")
    time_add.add_argument("--start", metavar="TIME", help="local session start; use instead of DURATION")
    time_add.add_argument("--end", metavar="TIME", help="local session end; defaults to now (same TIME format as stop --at)")
    time_add.set_defaults(func=command_time_add)
    time_report = time_sub.add_parser("report", help="report saved task time")
    time_report.add_argument("--by", choices=("task", "project", "topic"), default="task",
                             help="group the report by task, project, or topic (default: task)")
    time_report.description = (
        "Sum saved task time. Selection filters pick the tasks; unlike list, they include archived and "
        "hidden tasks unless you pass --archived exclude or --hidden exclude. --session-since and "
        "--session-before count only sessions that started inside that window, for example "
        "--session-since 1w for last week's time.")
    card_query.add_query_arguments(time_report, defaults=TIME_QUERY_DEFAULTS)
    add_session_window_arguments(time_report)
    time_report.set_defaults(func=command_time_report)


def _register_topic_parsers(sub: argparse._SubParsersAction) -> None:
    topic_command = sub.add_parser(
        "topic",
        help="list and reorganize topics",
        description="A topic is where a card lives, as one optional '/'-separated path such as "
                    "learning/rust/async. Topics never end (unlike a project, a finite effort) and "
                    "differ from tags (keywords). The tree is derived from cards; parents are implied.",
    )
    topic_sub = topic_command.add_subparsers(dest="topic_command", required=True)
    topic_list = topic_sub.add_parser(
        "list", help="list topics with card counts (including subtopics)",
        description="Show one level of topics with card counts that include everything below. "
                    "A trailing / marks a topic that has subtopics. Give TOPIC to look inside it. "
                    "Use --tree for every level, --paths for full paths (usable with --topic), "
                    "or --json for the full tree as data.",
    )
    topic_list.add_argument("topic", nargs="?", metavar="TOPIC", help="list the subtopics of this topic instead of the roots")
    topic_format = topic_list.add_mutually_exclusive_group()
    topic_format.add_argument("--tree", action="store_true", help="draw every level as a tree")
    topic_format.add_argument("--paths", action="store_true", help="print every topic as a full path, one per line, without counts")
    topic_format.add_argument("--json", dest="as_json", action="store_true",
                              help="print the full tree as JSON (with TOPIC: that topic and its descendants)")
    topic_list.set_defaults(func=command_topic_list)
    topic_rename = topic_sub.add_parser(
        "rename", help="move a topic and everything under it to a new path",
        description="Rewrite the topic of every card at OLD or under it, like moving a folder. "
                    "Cards are validated and rolled back together on failure.",
    )
    topic_rename.add_argument("old", metavar="OLD", help="existing topic path (its subtree moves too)")
    topic_rename.add_argument("new", metavar="NEW", help="new topic path; may not be inside OLD")
    topic_rename.add_argument("--dry-run", action="store_true", help="list affected cards without writing")
    topic_rename.add_argument("--merge", action="store_true", help="allow NEW to exist already and combine into it")
    topic_rename.add_argument("--by", metavar="USERNAME", help="username when renaming topics of claimed tasks")
    topic_rename.add_argument("--json", dest="as_json", action="store_true", help="print a JSON receipt or preview")
    topic_rename.set_defaults(func=command_topic_rename)


def _register_query_parsers(sub: argparse._SubParsersAction) -> None:
    focus = sub.add_parser(
        "focus",
        help="show, pin, or cycle one task to focus on next; focus selection is local .mt state",
    )
    focus.add_argument("query", nargs="*", help="task query to pin as manual focus")
    focus.add_argument("--set", dest="set_query", help="task query to pin as manual focus")
    focus.add_argument("-n", "--next", action="store_true", help="cycle to the next ranked local focus task")
    focus.add_argument("--clear", action="store_true", help="clear the local focus selection")
    focus.add_argument("--json", action="store_true", help="print selection, ordered candidates, and nearby deadline alerts as JSON")
    focus.add_argument("--by", help="choose tasks available to this identity; does not claim work")
    focus.description = ("Show the task to focus on next among open tasks. Selection filters narrow the "
                         "candidates; --status further limits the open statuses. Setting or clearing the "
                         "pinned task takes no filters.")
    card_query.add_query_arguments(focus)
    focus.set_defaults(func=command_focus)

    unfocus = sub.add_parser("unfocus", help="clear the local focus selection")
    unfocus.set_defaults(func=command_unfocus)

    followup = sub.add_parser("followup", help="show waiting tasks needing follow-up")
    followup.description = "Show waiting tasks, narrowed by the selection filters."
    card_query.add_query_arguments(followup)
    followup.set_defaults(func=command_followup)

    listing = sub.add_parser("list", help="list tasks and notes")
    listing.description = (
        "List visible, unarchived tasks and notes by default. Filters on different fields combine; "
        "repeated values of one filter are alternatives. --status, --priority, and --type select tasks; "
        "--needs-review selects any card marked for review. --archived and --hidden take exclude "
        "(default), include, or only; --all includes both. "
        "Done tasks older than rules.done_window (default 2w) are left out unless --status, "
        "--completed-since, --completed-before, or --archived include/only (or --all) is given."
    )
    card_query.add_query_arguments(listing)
    listing.add_argument("--sort", choices=("updated", "created", "due", "completed", "priority", "title"),
                         default="updated", help="sort field (default: updated)")
    listing.add_argument("--order", choices=("asc", "desc"), default="desc",
                         help="sort order (default: desc)")
    size = listing.add_mutually_exclusive_group()
    size.add_argument("--limit", type=int, metavar="N", help="keep the first N sorted cards")
    size.add_argument("--tail", type=int, metavar="N", help="keep the last N sorted cards, in order")
    listing.add_argument("--json", action="store_true", help="print matching cards as a JSON array")
    listing_output = listing.add_mutually_exclusive_group()
    listing_output.add_argument("--compact", action="store_true",
                               help="print minimal-field JSON records, one per line")
    listing_output.add_argument("--full", action="store_true",
                               help="print each card as 'mt read --json' does (ref, path, content_hash, "
                                    "properties, body) in one JSON array; implies --json, conflicts with --compact")
    listing.set_defaults(func=command_list)

    search = sub.add_parser("search", help="search task and note titles, IDs, and Markdown bodies")
    search.description = (
        "Search card titles, IDs, and Markdown bodies, excluding YAML frontmatter, among the cards the "
        "selection filters leave (visible, unarchived cards by default). "
        "The default exact mode matches one contiguous case-insensitive phrase. "
        "Use --and or --or to tokenize on whitespace, or --fuzzy for approximate token matches "
        "(--match MODE does the same). "
        "Relevance ranks title matches above ID matches above body matches; and/or reward more matching terms, "
        "and fuzzy ranks by average token similarity."
    )
    card_query.add_query_arguments(search, text=True)
    search.add_argument("--sort", choices=("relevance", "updated", "created", "due", "completed", "priority", "title"),
                        default="relevance", help="sort field (default: relevance)")
    search.add_argument("--order", choices=("asc", "desc"), default="desc",
                        help="sort order (default: desc)")
    search_size = search.add_mutually_exclusive_group()
    search_size.add_argument("--limit", type=int, metavar="N", help="keep the first N sorted matches")
    search_size.add_argument("--tail", type=int, metavar="N", help="keep the last N sorted matches, in order")
    search.add_argument("--json", action="store_true", help="print matching cards as a JSON array")
    search_output = search.add_mutually_exclusive_group()
    search_output.add_argument("--compact", action="store_true",
                               help="print minimal-field JSON records, one per line")
    search_output.add_argument("--full", action="store_true",
                               help="print each card as 'mt read --json' does (ref, path, content_hash, "
                                    "properties, body) in one JSON array; implies --json, conflicts with --compact")
    search.set_defaults(func=command_search)

    view = sub.add_parser("view", help="create an editable workspace copy")
    view.add_argument("--name", help="view name; defaults to a safe slug from the selector")
    card_query.add_query_arguments(view, text=True)
    view.description = (
        "Create an independent workspace copy of the cards selected by the same filters as list and search. "
        "The source selection is copied once; later source changes are not included. "
        "By default, the view directory name is a safe slug from the project, tag, or search text; "
        "use --name when the selector does not provide a clear name."
    )
    view.set_defaults(func=command_view)

    show = sub.add_parser("show", help="human browser preview of an item or saved .mt Markdown file; agents use read")
    show.add_argument(
        "query",
        nargs="+",
        help="task query or .mt Markdown path",
    )
    add_match_mode_arguments(show, allow_fuzzy=True)
    show.description = (
        "Open a human browser preview. Agents inspect items with mt read; use show only "
        "when the user explicitly requests a preview. " + show.description
    )
    show.add_argument("--out", type=Path, help="output HTML path; defaults to a temporary file")
    show.add_argument("--no-open", action="store_true", help="write HTML without opening a browser")
    show.set_defaults(func=command_show)

    read = sub.add_parser("read", help="print one or more matched items as Markdown or JSON")
    read.add_argument("query", nargs="+", help="one or more @ref references, or a case-insensitive search phrase")
    add_match_mode_arguments(read, allow_fuzzy=True)
    read.add_argument("--body", action="store_true", help="print only the logical Markdown body")
    read.add_argument("--json", dest="as_json", action="store_true",
                      help="return ref, path, content_hash, properties, and body as JSON")
    read.description += (
        " Pass multiple @ref references to inspect several items in one call (returning a JSON array with --json)."
        " JSON contains one properties mapping and one logical Markdown body, plus ref, path, "
        "and whole-card SHA-256 content_hash."
    )
    read.set_defaults(func=command_read)


def _register_mutation_parsers(sub: argparse._SubParsersAction) -> None:
    card_metadata = sub.add_parser(
        "card-metadata", help=argparse.SUPPRESS,
        description="Apply one hash-guarded card metadata patch for the dashboard.",
    )
    card_metadata.add_argument("ref", help="exact card reference")
    card_metadata.add_argument("--kind", choices=("task", "note"), help="reject the patch unless the card has this kind")
    card_metadata.add_argument("--expected-hash", required=True, help="whole-card SHA-256 precondition")
    card_metadata.add_argument("--patch", required=True, help="JSON object containing metadata fields")
    card_metadata.add_argument("--json", dest="as_json", action="store_true", help="print a saved receipt")
    card_metadata.set_defaults(func=command_card_metadata)
    # The dashboard uses this private command; public editing uses `mt edit`.
    sub._choices_actions[:] = [action for action in sub._choices_actions
                               if action.dest != "card-metadata"]

    edit = sub.add_parser("edit", help="edit one uniquely matched task or note")
    edit.add_argument("query", nargs="+", help="case-insensitive title, id, or body substring")
    add_match_mode_arguments(edit)
    edit.description += (
        " Use --title and --tags for task or note titles and tags, --set for metadata, "
        "and --body with --file for logical Markdown. Full body replacement requires --expected-hash; "
        "--old/--new replaces passages that each match exactly once; body append reads the current card under lock and preserves existing body bytes."
    )
    edit.description = textwrap.fill(edit.description, width=78)
    edit.formatter_class = argparse.RawDescriptionHelpFormatter
    edit.epilog = (
        "Values: boolean fields accept true or false; null clears a nullable field.\n"
        "due accepts YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS; set due_kind in the same\n"
        "edit when adding or clearing due. Use 'mt policy --json' for configured\n"
        "priority, type, and due_kind values.\n\n"
        "Examples:\n"
        "  mt edit REF --set hidden=true --set priority=high\n"
        "  mt edit REF --set due=2026-12-01 --set due_kind=target\n"
        "  mt edit REF --set due=null --set due_kind=null\n"
        "  mt edit REF --body --file body.md --expected-hash HASH\n"
        "  mt edit REF --body --append --file more.md\n"
        "  mt edit REF --old 'draft v1' --new 'draft v2'\n"
        "  mt edit REF --replace-file - <<'EOF'  # many or multiline replacements\n"
        "  [{\"old\": \"first\", \"new\": \"1st\"}]\n"
        "  EOF\n"
        "  mt edit REF --title 'Share the announcement' --tags '[\"work\",\"team\"]'\n"
        "  mt edit REF --tags '[]'  # clear tags\n"
        "Metadata, title, tags, and one body operation can share a single validated transaction.\n"
        "A failed edit leaves the\n"
        "card unchanged; inspect the error and correct the requested edit."
    )
    edit.add_argument(
        "--set",
        dest="assignments",
        action="append",
        metavar="FIELD=VALUE",
        help="repeat to set metadata fields in one validated edit; may combine with content edits. "
             f"Task fields: {', '.join(sorted(card_format.EDITABLE_FIELDS['task']))}. "
             f"Note fields: {', '.join(sorted(card_format.EDITABLE_FIELDS['note']))}.",
    )
    edit.add_argument("--body", action="store_true", help="edit the logical Markdown body")
    edit.add_argument("--file", metavar="PATH", help="UTF-8 Markdown body; - reads stdin")
    edit.add_argument("--append", action="store_true", help="append with a blank-line separator")
    edit.add_argument("--old", action="append", metavar="TEXT",
                      help="exact body passage to replace; must match once. Repeat with --new in the same order")
    edit.add_argument("--new", action="append", metavar="TEXT", help="replacement text for the matching --old")
    edit.add_argument("--replace-file", metavar="PATH",
                      help='JSON array of {"old": ..., "new": ...} body replacements, each matching once; - reads stdin')
    edit.add_argument("--title", help="set a task or note title; can combine with metadata or content edits")
    edit.add_argument("--tags", type=parse_edit_tags, metavar="JSON", help='replace tags with a JSON string array; [] clears tags')
    add_card_metadata_arguments(edit, editing=True)
    edit.add_argument("--by", metavar="USERNAME", help="username of the claimant; needed for status changes, archiving, and body rewrites on a claimed task")
    edit.add_argument("--expected-hash", help="whole-card SHA-256 from read --json; required for full body replacement, optional for --old/--new and --replace-file")
    edit.add_argument("--json", dest="as_json", action="store_true",
                      help="with a deterministic edit, return the saved card (ref, path, content_hash, properties, body) plus updated and changed")
    edit.set_defaults(func=command_edit)

    bulk_edit = sub.add_parser(
        "bulk-edit",
        help="preview or apply guarded metadata changes to a selected card collection",
        description=(
            "Preview metadata changes for the cards the selection filters pick. Save the JSON preview, "
            "review its query, counts, paths, and hashes, then apply that exact "
            "plan; collection membership and every selected card hash must still match."
        ),
    )
    card_query.add_query_arguments(bulk_edit)
    bulk_edit.add_argument(
        "--set", dest="assignments", action="append", default=[], metavar="FIELD=VALUE",
        help="repeat to set editable metadata fields on every selected card",
    )
    bulk_edit.add_argument(
        "--apply", dest="apply_plan", metavar="PLAN.json",
        help="apply a saved JSON preview after verifying selection membership and every card hash; - reads stdin",
    )
    bulk_edit.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit the hash-guarded preview as JSON, or a saved receipt with --apply",
    )
    bulk_edit.set_defaults(func=command_bulk_edit)

    done = sub.add_parser("done", help="mark one uniquely matched task done")
    done.add_argument("query", nargs="+", help="case-insensitive title, id, or body substring")
    done.add_argument(
        "-n", "--note", dest="notes", action="append", default=[],
        help="append completion details under Notes; may be repeated",
    )
    done.add_argument(
        "-c", "--comment", dest="comments", action="append", default=[],
        help="append a timestamped card comment; may be repeated",
    )
    add_match_mode_arguments(done)
    done.add_argument("--json", dest="as_json", action="store_true", help="return the updated task receipt as JSON")
    done.set_defaults(func=command_done)
    done.add_argument("--by", metavar="USERNAME",
                      help="username; defaults to MT_USERNAME, default_username, or the system username")

    move = sub.add_parser("move", help="move one uniquely matched task among open statuses")
    move.add_argument("query", nargs="+", help="case-insensitive title, id, or body substring")
    add_match_mode_arguments(move)
    move.add_argument(
        "-c", "--comment", dest="comments", action="append", default=[],
        help="append a timestamped card comment; may be repeated",
    )
    move.add_argument("destination", help="open destination status")
    move.add_argument("--by", metavar="USERNAME",
                      help="username; defaults to MT_USERNAME, default_username, or the system username")
    move.add_argument("--takeover", action="store_true", help="transfer another user's claim; requires doing")
    move.add_argument("--json", dest="as_json", action="store_true", help="return the updated task receipt as JSON")
    move.set_defaults(func=command_move)

    comment = sub.add_parser("comment", help="append a timestamped comment to one task or note")
    comment.add_argument("query", nargs="+", help="case-insensitive title, id, or body substring")
    comment.add_argument(
        "-m", "--message", dest="comments", action="append", required=True,
        help="comment text; may be repeated",
    )
    add_match_mode_arguments(comment)
    comment.add_argument("--json", dest="as_json", action="store_true", help="return the updated card receipt as JSON")
    comment.set_defaults(func=command_comment)

    convert = sub.add_parser("convert", help="convert one item between task and note")
    convert.add_argument("query", nargs="+", help="case-insensitive title, id, or body substring")
    add_match_mode_arguments(convert)
    convert.add_argument("destination", choices=("task", "note"), help="conversion destination")
    convert.add_argument("--status", help="status when converting a note to task; defaults to policy")
    convert.add_argument("--type", dest="task_type", help="task type when converting a note to task")
    convert.add_argument("--next-action", help="optional next action when converting a note to task")
    convert.add_argument("--json", dest="as_json", action="store_true", help="return the converted card receipt as JSON")
    convert.description = (
        "Change a card's kind in place (same file and ID). Converting a task to a note records its status, type, "
        "and next action in a history comment and drops task-only fields; claimed or time-tracked tasks are "
        "refused. " + convert.description.replace("Match task title", "Match card title")
    )
    convert.set_defaults(func=command_convert)

    delete = sub.add_parser("delete", help="delete one uniquely matched task or note, including unsynced captures")
    delete.add_argument("query", nargs="+", help="case-insensitive title, id, or body substring")
    add_match_mode_arguments(delete)
    delete.add_argument("--json", dest="as_json", action="store_true", help="return a deletion receipt as JSON")
    delete.set_defaults(func=command_delete)
    delete.add_argument("--by", metavar="USERNAME",
                        help="username; defaults to MT_USERNAME, default_username, or the system username")


def parser() -> argparse.ArgumentParser:
    root = MtArgumentParser(
        prog="mt",
        description="Deterministic microtask operations.",
        epilog="Capture with 'mt add <text>', 'mt note <text>', or guided 'mt capture'. "
               "Use 'mt <command> --help' for command-specific options and matching behavior. "
               "For metadata and body changes, see 'mt edit --help'; "
               "for configured values and defaults, use 'mt policy --json'. "
               "Set MT_PROJECT or MT_TOPIC to scope add, note, list, search, focus, followup, view, bulk-edit, "
               "time list, time report, and web to one project or topic; an explicit --project or --topic wins.",
    )
    root.add_argument(
        "-R",
        "--repo",
        dest="workspace_repo",
        metavar="PATH_OR_NAME",
        help="use another microtask workspace; must precede the command. A full workspace runs its "
             "own mt; a data-only folder (cards/ and mt-policy.yaml, no mt) is run by this one",
    )
    sub = root.add_subparsers(dest="command", required=True)
    _register_admin_parsers(sub)
    _register_draft_parsers(sub)
    _register_capture_parsers(sub)
    _register_time_parsers(sub)
    _register_topic_parsers(sub)
    _register_query_parsers(sub)
    _register_mutation_parsers(sub)
    public_commands = [name for name in sub.choices if name != "card-metadata"]
    sub.metavar = "{" + ",".join(public_commands) + "}"
    return root
