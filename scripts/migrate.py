"""Move an old-style workspace into workspace/, and rename mt-policy.yaml to settings.yaml.

Two cases: an old-style folder with application code and cards together (move the data out), and
a workspace folder with no application that only has the old settings file name (rename it).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import git_ops
from workspace import (
    AGENT_FILES,
    LOCAL_SETTINGS_FILE,
    OLD_LOCAL_SETTINGS_FILE,
    OLD_SETTINGS_FILE,
    SETTINGS_FILE,
    WORKSPACE_DIRECTORY,
    WORKSPACE_GITIGNORE,
    Workspace,
    default_workspace_path,
    is_data_workspace,
    is_legacy_workspace,
    is_unmigrated_workspace,
)

# What belongs to the workspace. Everything else Git tracks in the folder is application code.
DATA_FILES = (SETTINGS_FILE, LOCAL_SETTINGS_FILE, OLD_SETTINGS_FILE, OLD_LOCAL_SETTINGS_FILE)
DATA_FOLDERS = ("cards", "drafts", "attachments", ".mt")
IGNORE_LINES = (".mt/", LOCAL_SETTINGS_FILE)
# Machine-local files: ignored from now on, and no longer tracked if they were, so they
# stop syncing between machines. The files themselves stay on disk.
LOCAL_PATHS = (".mt", LOCAL_SETTINGS_FILE, OLD_LOCAL_SETTINGS_FILE)
# The settings files were called mt-policy*.yaml before 0.2.0.
RENAMES = ((OLD_SETTINGS_FILE, SETTINGS_FILE), (OLD_LOCAL_SETTINGS_FILE, LOCAL_SETTINGS_FILE))
COMMIT_MESSAGE = "Move microtask app out of the workspace"
RENAME_COMMIT_MESSAGE = f"Rename {OLD_SETTINGS_FILE} to {SETTINGS_FILE}"
OTHER_MACHINES_WARNING = (
    "Once this commit is pushed (for example by 'mt sync'), other machines still on the old layout "
    "will lose their app files on their next pull. On each other machine, run the installer or "
    "'mt migrate' before syncing."
)


class MigrateError(Exception):
    pass


def git(root: Path, *arguments: str, input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *arguments], cwd=root, input=input_bytes,
                          capture_output=True, check=False)


def checked_git(root: Path, *arguments: str, input_bytes: bytes | None = None) -> bytes:
    result = git(root, *arguments, input_bytes=input_bytes)
    if result.returncode:
        detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
        raise MigrateError(f"git {arguments[0]} failed: {detail}")
    return result.stdout


def is_data_path(path: str) -> bool:
    top = path.split("/", 1)[0]
    return top in DATA_FILES or top in DATA_FOLDERS


def merged_gitignore(existing: str) -> str:
    # A line for the old local file name becomes the new one.
    lines = [LOCAL_SETTINGS_FILE if line == OLD_LOCAL_SETTINGS_FILE else line for line in existing.splitlines()]
    if lines != existing.splitlines():
        existing = "\n".join(lines) + "\n"
    missing = [line for line in IGNORE_LINES if line not in lines]
    if not missing:
        return existing if existing.endswith("\n") or not existing else existing + "\n"
    text = existing if not existing or existing.endswith("\n") else existing + "\n"
    return text + "\n".join(missing) + "\n"


def tracked_local_files(root: Path) -> list[str]:
    output = checked_git(root, "ls-files", "-z", "--", *LOCAL_PATHS).decode("utf-8", errors="surrogateescape")
    return sorted(path for path in output.split("\0") if path)


def tracked_application_files(root: Path) -> list[str]:
    output = checked_git(root, "ls-files", "-z").decode("utf-8", errors="surrogateescape")
    return sorted(path for path in output.split("\0") if path and not is_data_path(path)
                  and path != WORKSPACE_GITIGNORE)


def dirty_paths(root: Path) -> list[str]:
    """Changes that block the move: anything staged, or anything outside drafts/."""
    output = checked_git(root, "status", "--porcelain=v1", "--untracked-files=all", "-z")
    records = output.decode("utf-8", errors="surrogateescape").split("\0")
    blocking: list[str] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        status, path = record[:2], record[3:]
        if status[0] in "RC":
            index += 1
        in_drafts = path.startswith("drafts/")
        staged = status[0] not in " ?"
        if staged or not in_drafts:
            blocking.append(path)
    return blocking


def origin_problem(root: Path) -> str | None:
    result = git(root, "remote", "get-url", "origin")
    if result.returncode:
        return None
    origin = result.stdout.decode().strip()
    if git_ops.remote_identity(origin, root) == git_ops.remote_identity(git_ops.CANONICAL_UPSTREAM, root):
        return (f"origin ({origin}) is the microtask application repository, so this folder is a clone of "
                "the application and has no private cards to move. Update it with 'git pull'")
    return None


def app_upstream(root: Path) -> str | None:
    """The URL of a remote named upstream when it points at the microtask application repository."""
    result = git(root, "remote", "get-url", "upstream")
    if result.returncode:
        return None
    url = result.stdout.decode().strip()
    if git_ops.remote_identity(url, root) == git_ops.remote_identity(git_ops.CANONICAL_UPSTREAM, root):
        return url
    return None


STOP_WEB_NOTICE = ("Before moving anything, stop 'mt web' if it is running in this folder. A dashboard "
                   "started before the move keeps using the old paths.")


def check_preconditions(
    app_root: Path, *, environ: dict[str, str] | None = None
) -> tuple[bool, list[str]]:
    """Refuse when the move is not safe. Returns whether the folder is a Git repository and its app files."""
    env = os.environ if environ is None else environ
    selected = env.get("MT_WORKSPACE")
    if selected and Path(selected).resolve() != app_root.resolve():
        if is_data_workspace(Path(selected)):
            raise MigrateError(f"{selected} already uses {SETTINGS_FILE}; there is nothing to migrate")
        raise MigrateError("MT_WORKSPACE is set to another folder; run mt migrate without it")
    target = app_root / WORKSPACE_DIRECTORY
    if target.exists() or target.is_symlink():
        raise MigrateError(f"{target} already exists, so there is nothing to migrate")
    if not is_legacy_workspace(app_root):
        raise MigrateError(f"{app_root} does not keep cards next to the application code; nothing to migrate")
    if (app_root / OLD_SETTINGS_FILE).exists() and (app_root / SETTINGS_FILE).exists():
        raise MigrateError(f"both {OLD_SETTINGS_FILE} and {SETTINGS_FILE} exist; remove the one you do not use")
    if (app_root / OLD_LOCAL_SETTINGS_FILE).exists() and (app_root / LOCAL_SETTINGS_FILE).exists():
        raise MigrateError(f"both {OLD_LOCAL_SETTINGS_FILE} and {LOCAL_SETTINGS_FILE} exist; remove the one you do not use")
    dot_git = app_root / ".git"
    if not dot_git.exists():
        return False, []
    if not dot_git.is_dir():
        raise MigrateError(f"{dot_git} is not a directory (worktrees and submodules are not supported)")
    top = checked_git(app_root, "rev-parse", "--show-toplevel").decode().strip()
    if Path(top).resolve() != app_root.resolve():
        raise MigrateError(f"{app_root} is inside the Git repository {top}; run mt migrate from its root")
    problem = origin_problem(app_root)
    if problem:
        raise MigrateError(problem)
    blocking = dirty_paths(app_root)
    if blocking:
        shown = ", ".join(blocking[:5]) + (f" and {len(blocking) - 5} more" if len(blocking) > 5 else "")
        raise MigrateError("the Git tree has uncommitted changes outside drafts/ (" + shown + "). "
                           "Run 'mt sync' or commit or discard them first")
    return True, tracked_application_files(app_root)


def moves(app_root: Path) -> list[str]:
    names = [".git", *DATA_FOLDERS, *DATA_FILES]
    return [name for name in names if (app_root / name).exists() or (app_root / name).is_symlink()]


def pending_renames(folder: Path) -> list[tuple[str, str]]:
    return [(old, new) for old, new in RENAMES if (folder / old).exists() and not (folder / new).exists()]


def rename_lines(folder: Path) -> list[str]:
    return [f"Rename {old} to {new}" for old, new in pending_renames(folder)]


def describe(app_root: Path, has_git: bool, application_files: list[str]) -> list[str]:
    lines = [f"Move into {app_root / WORKSPACE_DIRECTORY}: {', '.join(moves(app_root))}"]
    lines.extend(rename_lines(app_root))
    if has_git:
        upstream = app_upstream(app_root)
        if upstream:
            lines.append(f"Remove the 'upstream' remote ({upstream}): it points at the application, "
                         "and pulling from it would merge application files into your cards")
        lines.append(f"Add {WORKSPACE_GITIGNORE} lines: {', '.join(IGNORE_LINES)}")
        lines.append(f"Add {' and '.join(AGENT_FILES)} for agents started in the workspace")
        local = tracked_local_files(app_root)
        if local:
            lines.append(f"Stop tracking machine-local files (they stay on disk): {', '.join(local)}")
        lines.append(f"One commit \"{COMMIT_MESSAGE}\" in the workspace would delete "
                     f"{len(application_files)} application file(s) and update {WORKSPACE_GITIGNORE}, "
                     f"{' and '.join(AGENT_FILES)}:")
        shown = application_files[:10]
        lines.extend(f"  {path}" for path in shown)
        if len(application_files) > len(shown):
            lines.append(f"  and {len(application_files) - len(shown)} more")
    else:
        lines.append("This folder has no Git, so nothing is committed.")
    return lines


def migrate(app_root: Path, has_git: bool, application_files: list[str]) -> None:
    target = app_root / WORKSPACE_DIRECTORY
    gitignore_text = ""
    root_ignore = app_root / WORKSPACE_GITIGNORE
    if root_ignore.is_file():
        gitignore_text = root_ignore.read_text(encoding="utf-8")
    settings_tracked = has_git and bool(checked_git(app_root, "ls-files", "--", SETTINGS_FILE, OLD_SETTINGS_FILE))
    target.mkdir()
    moved: list[str] = []
    renamed: list[tuple[str, str]] = []
    removed_upstream: str | None = None
    try:
        for name in moves(app_root):
            os.rename(app_root / name, target / name)
            moved.append(name)
        for old, new in pending_renames(target):
            os.rename(target / old, target / new)
            renamed.append((old, new))
        (target / WORKSPACE_GITIGNORE).write_text(merged_gitignore(gitignore_text), encoding="utf-8")
        for name in AGENT_FILES:
            # Agents started in the workspace (mt run, cloud services) need instructions there.
            (target / name).write_bytes((app_root / "templates" / f"workspace-{name}").read_bytes())
        if has_git:
            removed_upstream = app_upstream(target)
            if removed_upstream:
                checked_git(target, "remote", "remove", "upstream")
            if application_files:
                checked_git(target, "rm", "--cached", "-q", "--pathspec-from-file=-", "--pathspec-file-nul",
                            input_bytes=b"\0".join(path.encode("utf-8", errors="surrogateescape")
                                                   for path in application_files) + b"\0")
            checked_git(target, "rm", "-r", "--cached", "--ignore-unmatch", "-q", "--", *LOCAL_PATHS)
            if renamed:
                checked_git(target, "rm", "--cached", "--ignore-unmatch", "-q", "--", OLD_SETTINGS_FILE)
            if settings_tracked:
                checked_git(target, "add", "--", SETTINGS_FILE)
            checked_git(target, "add", "--", WORKSPACE_GITIGNORE, *AGENT_FILES)
            checked_git(target, "commit", "-q", "-m", COMMIT_MESSAGE)
    except (OSError, MigrateError):
        # Put everything back so the folder works as before; keep going on partial failures.
        for old, new in reversed(renamed):
            try:
                os.rename(target / new, target / old)
            except OSError:
                pass
        for name in reversed(moved):
            try:
                os.rename(target / name, app_root / name)
            except OSError:
                pass
        for name in (WORKSPACE_GITIGNORE, *AGENT_FILES):
            try:
                (target / name).unlink(missing_ok=True)
            except OSError:
                pass
        if has_git:
            git(app_root, "reset", "-q")
            if removed_upstream:
                git(app_root, "remote", "add", "upstream", removed_upstream)
        try:
            target.rmdir()
        except OSError:
            pass
        raise


def rename_target(ws: Workspace) -> Path | None:
    """The selected workspace folder when it is a data-only folder that still uses mt-policy.yaml."""
    selected = ws.environ.get("MT_WORKSPACE")
    folder = Path(selected) if selected else default_workspace_path(ws.app_root)
    return folder if is_unmigrated_workspace(folder) else None


def check_rename_preconditions(folder: Path) -> bool:
    """Refuse when renaming is not safe. Returns whether the folder is a Git repository."""
    for old, new in RENAMES:
        if (folder / old).exists() and (folder / new).exists():
            raise MigrateError(f"both {old} and {new} exist in {folder}; remove the one you do not use")
    if not (folder / ".git").exists():
        return False
    top = checked_git(folder, "rev-parse", "--show-toplevel").decode().strip()
    if Path(top).resolve() != folder.resolve():
        raise MigrateError(f"{folder} is inside the Git repository {top}; run mt migrate from its root")
    blocking = dirty_paths(folder)
    if blocking:
        shown = ", ".join(blocking[:5]) + (f" and {len(blocking) - 5} more" if len(blocking) > 5 else "")
        raise MigrateError("the Git tree has uncommitted changes outside drafts/ (" + shown + "). "
                           "Run 'mt sync' or commit or discard them first")
    return True


def describe_rename(folder: Path, has_git: bool) -> list[str]:
    lines = [f"In {folder}:"] + [f"  {line}" for line in rename_lines(folder)]
    if has_git:
        local = tracked_local_files(folder)
        if local:
            lines.append(f"  Stop tracking machine-local files (they stay on disk): {', '.join(local)}")
        lines.append(f"  Add {WORKSPACE_GITIGNORE} lines when missing: {', '.join(IGNORE_LINES)}")
        lines.append(f"  One commit \"{RENAME_COMMIT_MESSAGE}\" records the rename. Nothing is pushed.")
    else:
        lines.append("  This folder has no Git, so nothing is committed.")
    return lines


def rename_settings(folder: Path, has_git: bool) -> None:
    """Rename the settings files and commit only that (plus the ignore line for the local file)."""
    ignore_path = folder / WORKSPACE_GITIGNORE
    ignore_before = ignore_path.read_text(encoding="utf-8") if ignore_path.is_file() else None
    tracked_settings = has_git and bool(checked_git(folder, "ls-files", "--", OLD_SETTINGS_FILE))
    renamed: list[tuple[str, str]] = []
    try:
        for old, new in pending_renames(folder):
            os.rename(folder / old, folder / new)
            renamed.append((old, new))
        if has_git:
            merged = merged_gitignore(ignore_before or "")
            if merged != (ignore_before or ""):
                ignore_path.write_text(merged, encoding="utf-8")
            checked_git(folder, "rm", "-r", "--cached", "--ignore-unmatch", "-q", "--", *LOCAL_PATHS)
            checked_git(folder, "rm", "--cached", "--ignore-unmatch", "-q", "--", OLD_SETTINGS_FILE)
            if tracked_settings:
                checked_git(folder, "add", "--", SETTINGS_FILE)
            if ignore_path.is_file() and ignore_path.read_text(encoding="utf-8") != (ignore_before or ""):
                checked_git(folder, "add", "--", WORKSPACE_GITIGNORE)
            if git(folder, "diff", "--cached", "--quiet").returncode:
                checked_git(folder, "commit", "-q", "-m", RENAME_COMMIT_MESSAGE)
    except (OSError, MigrateError):
        for old, new in reversed(renamed):
            os.rename(folder / new, folder / old)
        if has_git:
            git(folder, "reset", "-q")
            if ignore_before is None:
                ignore_path.unlink(missing_ok=True)
            else:
                ignore_path.write_text(ignore_before, encoding="utf-8")
        raise


def command_migrate_rename(args: argparse.Namespace, folder: Path) -> int:
    try:
        has_git = check_rename_preconditions(folder)
        print(STOP_WEB_NOTICE)
        if args.check:
            for line in describe_rename(folder, has_git):
                print(line)
            print("Nothing was changed.")
            return 0
        rename_settings(folder, has_git)
    except (OSError, MigrateError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Renamed {OLD_SETTINGS_FILE} to {SETTINGS_FILE} in {folder}.")
    if has_git:
        print(f"One commit records it: \"{RENAME_COMMIT_MESSAGE}\".")
    print("Nothing was pushed.")
    if has_git:
        print("Warning: once this commit is pushed, other machines that sync this workspace need microtask "
              f"0.2.0 or later, which reads {SETTINGS_FILE}. Update them before they sync.")
    return 0


def command_migrate(args: argparse.Namespace, ws: Workspace) -> int:
    folder = rename_target(ws)
    if folder is not None:
        return command_migrate_rename(args, folder)
    app_root = ws.app_root
    try:
        has_git, application_files = check_preconditions(app_root, environ=dict(ws.environ))
        print(STOP_WEB_NOTICE)
        if args.check:
            for line in describe(app_root, has_git, application_files):
                print(line)
            print("Nothing was changed.")
            return 0
        upstream = app_upstream(app_root) if has_git else None
        migrate(app_root, has_git, application_files)
    except (OSError, MigrateError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    target = app_root / WORKSPACE_DIRECTORY
    print(f"Your workspace is now {target}.")
    if has_git:
        print(f"It kept its Git history, and one commit records the move: \"{COMMIT_MESSAGE}\".")
        if upstream:
            print(f"Removed the 'upstream' remote ({upstream}). It pointed at the application, not at your cards.")
        print("The application folder has no Git now, so 'mt update' works here.")
    print("Nothing was pushed.")
    if has_git:
        print(f"Warning: {OTHER_MACHINES_WARNING}")
    return 0
