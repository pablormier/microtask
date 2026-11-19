"""Administrative, setup, skill, repository, and web CLI commands."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time

import card_query
from cli_helpers import (
    REPO_ALIAS_RE,
    command_query,
    load_repo_aliases,
    show_workspace,
    validate_repo_workspace,
    write_repo_aliases,
)
import git_ops
import store
import transaction
from store import (
    _ws,
    discover,
    foreign_env,
    workspace_env,
)
import validation
from workspace import (
    WORKSPACE_DIRECTORY,
    Workspace,
    add_agent_files,
    create_workspace,
    demo_environ,
    default_workspace_path,
    is_data_workspace,
    is_legacy_workspace,
    is_unmigrated_workspace,
    seed_demo_workspace,
    unmigrated_message,
)

SKILL_TARGETS = (".agents/skills", ".claude/skills")
SKILL_MARKER = "installed-by: microtask"
SKILL_WORKSPACE_BLOCK = re.compile(r"<!-- mt:workspace -->\n.*?<!-- /mt:workspace -->", re.DOTALL)
SKILL_COMMAND = re.compile(r"(?<![\w/.-])(?:\./)?mt(?= [A-Za-z-])")
APPLICATION_FILES = ("mt", "install.sh", "README.md", "LICENSE", "AGENTS.md",
                     "CLAUDE.md", "GEMINI.md", "CONTRIBUTING.md", ".gitignore", "VERSION")
APPLICATION_FOLDERS = ("scripts", "tools", "templates", ".agents")


def portable_invocation(ws: Workspace | None = None) -> tuple[str, str | None]:
    """Return a path-free command that reaches this workspace, and the alias it uses.

    Plain mt when this workspace is the one on PATH, otherwise mt -R ALIAS.
    Raises ValueError when neither reaches it.
    """
    resolved = _ws(ws)
    on_path = shutil.which("mt")
    if on_path and resolved.in_app and Path(on_path).resolve() == (resolved.app_root / "mt").resolve():
        return "mt", None
    here = resolved.root.resolve()
    for name, path in sorted(load_repo_aliases().items()):
        if Path(path).resolve() == here:
            return f"mt -R {name}", name
    raise ValueError(f"give this workspace a name first: mt repo add NAME {shlex.quote(str(here))}")


def skill_sources(ws: Workspace | None = None) -> list[Path]:
    """Every skill this application ships: each folder in .agents/skills with a SKILL.md."""
    root = _ws(ws).app_root / ".agents" / "skills"
    return sorted(path for path in root.iterdir() if (path / "SKILL.md").is_file()) if root.is_dir() else []


def installed_by_microtask(folder: Path) -> bool:
    if folder.is_symlink():
        return False
    try:
        text = (folder / "SKILL.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    match = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    return bool(match and re.search(rf"^\s+{re.escape(SKILL_MARKER)}\s*$", match.group(1), re.MULTILINE))


def render_skill_file(text: str, invocation: str, alias: str | None, *, scoped: bool, skill_md: bool) -> str:
    """Point a skill's commands at this workspace and mark the copy as installed by microtask."""
    text = SKILL_COMMAND.sub(lambda _: invocation, text)
    check = ("Check the `Workspace:` and `Scope:` lines when printed; stop and ask if either is wrong."
             if scoped else "Check the `Workspace:` line when printed; stop and ask if it is wrong.")
    missing = (f"If `mt` is not found, or the alias `{alias}` is unknown, ask the user to install microtask "
               f"or run `mt repo add {alias} PATH`." if alias else
               "If `mt` is not found, ask the user to install microtask.")
    block = f"Run microtask commands as `{invocation} COMMAND`. {check} {missing}"
    text = SKILL_WORKSPACE_BLOCK.sub(lambda _: block, text)
    if skill_md:
        match = re.match(r"---\n(.*?\n)?---\n", text, re.DOTALL)
        if match is None:
            raise ValueError("SKILL.md needs YAML frontmatter between --- lines")
        if re.search(r"^metadata:", match.group(1) or "", re.MULTILINE):
            raise ValueError("SKILL.md already has metadata, where microtask marks its installed copies")
        at = match.end() - 4
        text = text[:at] + f"metadata:\n  {SKILL_MARKER}\n" + text[at:]
    return text


def skills_invocation(project: str | None, topic: str | None, ws: Workspace | None = None) -> tuple[str, str | None, bool]:
    """The command installed skills run, its alias, and whether it names a folder on this machine."""
    resolved = _ws(ws)
    try:
        mt, alias = portable_invocation(resolved)
        local = False
    except ValueError:
        mt, alias, local = f"mt -R {shlex.quote(str(resolved.root))}", None, True
    scope = [f"MT_PROJECT={shlex.quote(project)}"] if project else []
    scope += [f"MT_TOPIC={shlex.quote(topic)}"] if topic else []
    return " ".join([*scope, mt]), alias, local


def workspace_skill_invocation(project: str | None = None, topic: str | None = None) -> str:
    """Plain mt: inside a workspace it selects that workspace, so the copies name no path."""
    scope = [f"MT_PROJECT={shlex.quote(project)}"] if project else []
    scope += [f"MT_TOPIC={shlex.quote(topic)}"] if topic else []
    return " ".join([*scope, "mt"])


def install_skills(
    target_dir: Path,
    ws: Workspace,
    invocation: str,
    *,
    alias: str | None = None,
    scoped: bool = False,
    local_workspace: bool = False,
) -> set[str]:
    sources = skill_sources(ws)
    if not sources:
        raise ValueError(f"no skills found in {ws.app_root / '.agents' / 'skills'}")
    bases = [target_dir / target for target in SKILL_TARGETS]
    if local_workspace:
        check_workspace_skill_paths(bases)
    names = {source.name for source in sources}
    conflicts = [base / name for base in bases for name in sorted(names)
                 if ((base / name).exists() or (base / name).is_symlink())
                 and not installed_by_microtask(base / name)]
    if conflicts:
        paths = "\n".join(f"  {path}" for path in conflicts)
        raise ValueError(f"these skills already exist and were not installed by microtask; "
                         f"rename or remove them:\n{paths}")
    if local_workspace:
        paths = [str((base / name).relative_to(target_dir)) for base in bases for name in sorted(names)]
        tracked = git_ops.run_git(target_dir, "ls-files", "--", *paths)
        if tracked.returncode == 0 and tracked.stdout:
            raise ValueError("workspace skill copies are generated and ignored by Git, so they cannot replace "
                             "Git-tracked files:\n"
                             + tracked.stdout.rstrip())
    # Render everything first, so a broken skill stops the install before any file changes.
    rendered: dict[str, list[tuple[Path, bytes | None]]] = {}
    for source in sources:
        files: list[tuple[Path, bytes | None]] = []
        for path in sorted(source.rglob("*")):
            if path.name == ".DS_Store" or "__pycache__" in path.parts:
                continue
            content = None
            if path.is_file():
                content = path.read_bytes()
                if path.suffix == ".md":
                    try:
                        content = render_skill_file(content.decode("utf-8"), invocation, alias,
                                                    scoped=scoped,
                                                    skill_md=path == source / "SKILL.md").encode("utf-8")
                    except (UnicodeError, ValueError) as error:
                        raise ValueError(f"{path}: {error}") from error
            files.append((path.relative_to(source), content))
        rendered[source.name] = files
    for base in bases:
        base.mkdir(parents=True, exist_ok=True)
        for stale in sorted(base.iterdir()):
            if stale.name not in names and installed_by_microtask(stale):
                shutil.rmtree(stale)
        for name, files in sorted(rendered.items()):
            staging = Path(tempfile.mkdtemp(prefix=f".{name}.", dir=base))
            try:
                for relative, content in files:
                    destination = staging / relative
                    if content is None:
                        destination.mkdir(exist_ok=True)
                    else:
                        destination.write_bytes(content)
                if local_workspace:
                    (staging / ".gitignore").write_text("*\n", encoding="utf-8")
                staging.chmod(0o755)
                current = base / name
                if current.exists():
                    shutil.rmtree(current)
                os.rename(staging, current)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
    return names


def check_workspace_skill_paths(bases: list[Path]) -> None:
    for base in bases:
        for path in (base.parent, base):
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise ValueError(f"workspace skill directory must be a real directory: {path}")


def command_skills_install(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    scoped = bool(args.project or args.topic)
    workspace_dir = resolved.root.resolve()
    project_dir = workspace_dir if args.workspace else Path(args.dir).expanduser().resolve()
    local_workspace = args.workspace or (project_dir == workspace_dir and is_data_workspace(project_dir))
    if local_workspace:
        if not (is_data_workspace(project_dir) or is_legacy_workspace(project_dir)):
            raise ValueError(f"no workspace at {project_dir}; run 'mt init' or select one with -R")
        invocation = workspace_skill_invocation(args.project, args.topic)
        alias, local = None, False
    else:
        if not project_dir.is_dir():
            raise ValueError(f"project directory does not exist: {project_dir}")
        invocation, alias, local = skills_invocation(args.project, args.topic, ws=resolved)
    names = install_skills(project_dir, resolved, invocation, alias=alias, scoped=scoped,
                           local_workspace=local_workspace)
    print(f"Installed {len(names)} skills in {', '.join(str(project_dir / target) for target in SKILL_TARGETS)}:")
    print(f"  {', '.join(sorted(names))}")
    print(f"They run `{invocation} COMMAND`. "
          + ("mt update refreshes them; in a Git clone, run this again after git pull." if local_workspace
             else "Run this again after mt update to refresh them."))
    if local_workspace:
        added = add_agent_files(project_dir, resolved.app_root)
        if added:
            print(f"Added {' and '.join(added)}, which tell agents how to use this workspace.")
    if local:
        print("warning: the skills name a folder on this machine, so do not commit them. To share them, give "
              f"the workspace an alias with mt repo add NAME {shlex.quote(str(resolved.root))} and install again "
              "with mt -R NAME skills install.", file=sys.stderr)
    if resolved.in_app and not (scoped or local_workspace):
        print("note: agents will see every card in this workspace; add --project or --topic to limit them.",
              file=sys.stderr)
    return 0


def command_skills_uninstall(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    project_dir = _ws(ws).root.resolve() if args.workspace else Path(args.dir).expanduser().resolve()
    if args.workspace or is_data_workspace(project_dir):
        check_workspace_skill_paths([project_dir / target for target in SKILL_TARGETS])
    removed = 0
    for target in SKILL_TARGETS:
        base = project_dir / target
        if not base.is_dir():
            continue
        for folder in sorted(base.iterdir()):
            if installed_by_microtask(folder):
                shutil.rmtree(folder)
                removed += 1
        for empty in (base, base.parent):
            if empty.is_dir() and not any(empty.iterdir()):
                empty.rmdir()
    print(f"Removed {removed} microtask skill folder(s) from {project_dir}." if removed
          else f"No microtask skills installed in {project_dir}.")
    return 0


def command_info(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    repository = git_ops.repository_info(resolved.root)
    tasks = discover("task", ws=resolved)
    notes_count = len(discover("note", ws=resolved))
    task_count = len(tasks)
    status_counts = {status: sum(task.frontmatter["status"] == status for task in tasks)
                     for status in store.statuses(resolved)}
    width = max(24, min(shutil.get_terminal_size((80, 24)).columns, 96))
    color = sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"

    def paint(value: str, code: str) -> str:
        return f"\033[{code}m{value}\033[0m" if color and code else value

    def wrap_location(value: str, limit: int) -> list[str]:
        lines: list[str] = []
        current = ""
        for segment in re.split(r"(?<=/)", value):
            if current and len(current) + len(segment) > limit:
                lines.append(current)
                current = ""
            while len(segment) > limit:
                lines.append(segment[:limit])
                segment = segment[limit:]
            current += segment
        return lines + ([current] if current else [])

    def box_line(value: str = "", code: str = "", *, location: bool = False) -> None:
        inside = width - 4
        lines = (wrap_location(value, inside) if location else
                 textwrap.wrap(value, inside, break_long_words=True, break_on_hyphens=False))
        for line in lines or [""]:
            print(paint("│", "2") + " " + paint(f"{line:<{inside}}", code) + " " + paint("│", "2"))

    def section(label: str) -> None:
        rule = "─" * max(0, width - len(label) - 3)
        print(paint(label, "1;36") + " " + paint(rule, "2"))

    top = "╭─ microtask " + "─" * (width - len("╭─ microtask ") - 1) + "╮"
    print(paint(top, "36"))
    box_line("WORKSPACE", "1;36")
    box_line(str(resolved.root), "1", location=True)
    if not resolved.in_app:
        box_line(f"Application: {resolved.app_root}", "2", location=True)
    box_line()
    total = task_count + notes_count
    box_line(f"{total} card{'s' if total != 1 else ''}   ·   "
             f"{task_count} task{'s' if task_count != 1 else ''}   ·   "
             f"{notes_count} note{'s' if notes_count != 1 else ''}", "1")
    print(paint("╰" + "─" * (width - 2) + "╯", "36"))

    print()
    section("TASKS")
    labels = store.policy(resolved)["statuses"]
    count_width = max((len(str(count)) for count in status_counts.values()), default=1)
    name_width = min(max((len(str(labels.get(status, status))) for status in status_counts), default=0),
                     max(8, width - count_width - 10))
    bar_width = min(20, max(3, width - name_width - count_width - 7))
    largest = max(status_counts.values(), default=0)
    for status, count in status_counts.items():
        label = str(labels.get(status, status))[:name_width]
        filled = max(1, round(bar_width * count / largest)) if count and largest else 0
        bar_color = {"done": "32", "doing": "36", "waiting": "33", "someday": "2"}.get(status, "34")
        print(f"  {label:<{name_width}} {count:>{count_width}}  {paint('█' * filled, bar_color)}")

    git_state = str(repository["state"])
    print()
    section("GIT")
    if git_state == "ready":
        print(f"  {paint('●', '32')}  Branch   {paint(str(repository['branch']), '1')}")
        for action, remote in (("Sync", "origin"),):
            target = str(repository[remote])
            if target == "-":
                print(f"  {paint('○', '33')}  {action:<8} {paint('add ' + remote, '33')}")
                continue
            print(f"  {paint('●', '32')}  {action:<8} {remote}")
            for line in wrap_location(target, width - 5):
                print(f"     {paint(line, '2')}")
    else:
        print(f"  {paint('○', '33')}  {git_state}")
        if git_state == "missing":
            detail = "Install Git, then run git init"
        elif git_state == "uninitialized":
            detail = "Run git init"
        else:
            detail = str(repository["detail"])
        for line in textwrap.wrap(detail, width - 5, break_long_words=True, break_on_hyphens=False):
            print(f"     {line}")
    failed_at, failure = transaction.read_sync_error(resolved)
    if failure:
        print()
        section("LAST SYNC FAILED")
        if failed_at:
            print(f"  {failed_at}")
        for line in failure.splitlines():
            for wrapped in textwrap.wrap(line, width - 5, break_long_words=True, break_on_hyphens=False) or [""]:
                print(f"     {wrapped}")
    return 0


def command_policy(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    """Show effective settings, with JSON available for automation."""
    try:
        effective = store.policy(_ws(ws))
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if args.as_json:
        print(json.dumps(effective, ensure_ascii=False, sort_keys=True))
        return 0
    print("Effective workspace settings")
    for key, value in effective.items():
        label = key.replace("_", " ").capitalize()
        if isinstance(value, dict):
            print(f"\n{label}:")
            width = max((len(str(name)) for name in value), default=0)
            for name, setting in value.items():
                if isinstance(setting, bool):
                    setting = "true" if setting else "false"
                print(f"  {name:<{width}}  {setting}")
        elif isinstance(value, list):
            print(f"\n{label}: {', '.join(str(item) for item in value)}")
        else:
            print(f"\n{label}: {value}")
    return 0


def copy_application(
    target: Path, *, examples: bool = False, ws: Workspace | None = None,
) -> None:
    """Copy the application into TARGET, with an empty workspace/ that keeps this workspace's settings."""
    resolved = _ws(ws)
    folders = APPLICATION_FOLDERS + (("examples",) if examples else ())
    for name in APPLICATION_FILES:
        if (resolved.app_root / name).is_file():
            shutil.copy2(resolved.app_root / name, target / name)
    for name in folders:
        if (resolved.app_root / name).is_dir():
            shutil.copytree(resolved.app_root / name, target / name,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "*.swp", ".env"))
    create_workspace(target / WORKSPACE_DIRECTORY, resolved.app_root, policy=resolved.root / "settings.yaml")


def command_repo_add(args: argparse.Namespace) -> int:
    if not REPO_ALIAS_RE.fullmatch(args.name):
        print(
            "error: repository alias must begin with a letter or digit and contain only "
            "letters, digits, '.', '_', or '-'",
            file=sys.stderr,
        )
        return 1
    path = Path(args.path).expanduser().resolve()
    error = validate_repo_workspace(path)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    try:
        aliases = load_repo_aliases()
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if args.name in aliases:
        print(f"error: repository alias already exists: {args.name}", file=sys.stderr)
        return 1
    aliases[args.name] = str(path)
    write_repo_aliases(aliases)
    print(f"Added repository alias: {args.name}\t{path}")
    return 0


def command_repo_list(args: argparse.Namespace) -> int:
    try:
        aliases = load_repo_aliases()
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    for name in sorted(aliases):
        print(f"{name}\t{aliases[name]}")
    return 0


def command_repo_remove(args: argparse.Namespace) -> int:
    try:
        aliases = load_repo_aliases()
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if args.name not in aliases:
        print(f"error: unknown repository alias: {args.name}", file=sys.stderr)
        return 1
    path = aliases.pop(args.name)
    write_repo_aliases(aliases)
    print(f"Removed repository alias: {args.name}\t{path}")
    return 0


def command_run(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    os.chdir(resolved.root)
    try:
        # An agent started here runs plain 'mt', which must be this app on this workspace.
        os.execvpe(args.argv[0], args.argv, resolved.run_env())
    except FileNotFoundError:
        print(f"error: command not found: {args.argv[0]}", file=sys.stderr)
        return 127
    except OSError as error:
        print(f"error: cannot run {args.argv[0]}: {error}", file=sys.stderr)
        return 126


def command_update(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    command = [sys.executable, str(resolved.app_root / "scripts" / "update.py")]
    if args.check:
        command.append("--check")
    if args.as_json:
        command.append("--json")
    if args.force:
        command.append("--force")
    return subprocess.run(command, cwd=resolved.app_root, check=False, env=resolved.environ).returncode


def command_import_github(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    show_workspace(ws=resolved)
    import import_github

    argv: list[str] = []
    if args.repo:
        argv.extend(["--repo", args.repo])
    if getattr(args, "as_json", False):
        argv.append("--json")
    return import_github.main(argv, resolved)


def command_validate(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    try:
        effective_policy = store.policy(resolved)
    except (OSError, ValueError) as error:
        print("FAILED: invalid settings:", file=sys.stderr)
        for line in str(error).splitlines():
            print(f"  - {line}", file=sys.stderr)
        return 1
    report = validation.audit_workspace(resolved.root, effective_policy)
    code, stdout_lines, stderr_lines = validation.format_audit_report(report)
    for line in stderr_lines:
        print(line, file=sys.stderr)
    if code == 0 and resolved.root == resolved.app_root and not resolved.environ.get("MT_WORKSPACE"):
        print("note: your cards sit next to the application code. Run 'mt migrate' to move them into "
              "workspace/, so 'mt update' works without Git.", file=sys.stderr)
    for line in stdout_lines:
        print(line)
    return code


def init_data_workspace(destination: Path, origin: str | None, ws: Workspace | None = None) -> int:
    """Create a workspace holding only cards and settings, run by this mt."""
    resolved = _ws(ws)
    if destination.exists():
        if is_data_workspace(destination):
            print(f"{destination} is already a workspace folder; nothing changed.")
            return 0
        print(f"error: {destination} already exists", file=sys.stderr)
        return 1
    if not destination.parent.is_dir():
        print(f"error: parent folder does not exist: {destination.parent}", file=sys.stderr)
        return 1
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        create_workspace(staging, resolved.app_root, policy=resolved.root / "settings.yaml")
        install_skills(staging, resolved, workspace_skill_invocation(), local_workspace=True)
        staging.chmod(0o755)
        os.rename(staging, destination)
    except (OSError, ValueError) as error:
        shutil.rmtree(staging, ignore_errors=True)
        print(f"error: could not create {destination}: {error}", file=sys.stderr)
        return 1
    print(f"Created a workspace folder in {destination} with your settings (settings.yaml) and no cards.")
    print(f"It has no application of its own; this mt ({resolved.app_root}) runs it.")
    if shutil.which("git") is None:
        print("Git is not installed, so it has no history or sync; run 'git init' there later.")
    else:
        def git(*arguments: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(["git", "-C", str(destination), *arguments],
                                  capture_output=True, text=True, check=False)
        steps = [("init", "-q"), ("symbolic-ref", "HEAD", "refs/heads/main")]
        steps += [("remote", "add", "origin", origin)] if origin else []
        steps += [("add", "-A"), ("commit", "-q", "-m", "Create microtask workspace")]
        for step in steps:
            result = git(*step)
            if result.returncode:
                detail = (result.stderr or result.stdout).strip()
                print(f"warning: git {step[0]} failed, so the workspace is not committed yet: {detail}",
                      file=sys.stderr)
                break
        else:
            if not origin:
                print("No 'origin' remote yet; 'mt sync' needs one:")
                print(f"  git -C {shlex.quote(str(destination))} remote add origin YOUR-PRIVATE-REPOSITORY-URL")
    print("\nGive it a short name, then use it from any folder:\n")
    print(f"  mt repo add NAME {shlex.quote(str(destination))}")
    print('  mt -R NAME add "First task"')
    return 0


def init_own_workspace(args: argparse.Namespace, ws: Workspace) -> int:
    """Create this application's own workspace/ folder: the one plain ./mt uses."""
    if args.local or args.data:
        print("error: --local and --data need a DIRECTORY; plain 'mt init' creates this app's workspace/",
              file=sys.stderr)
        return 2
    target = default_workspace_path(ws.app_root)
    if ws.environ.get("MT_WORKSPACE"):
        print(f"error: MT_WORKSPACE is set, so mt uses {ws.environ['MT_WORKSPACE']}, not {target}. "
              "Unset it to create this app's own workspace", file=sys.stderr)
        return 2
    if is_data_workspace(target):
        print(f"Workspace already exists: {target}")
        return 0
    if is_unmigrated_workspace(target):
        print(f"error: {unmigrated_message(target, str(ws.app_root / 'mt') + ' migrate')}", file=sys.stderr)
        return 1
    if is_legacy_workspace(ws.app_root):
        print(f"error: {ws.app_root} keeps its cards next to the application. "
              "Run 'mt migrate' to move them into workspace/", file=sys.stderr)
        return 1
    if target.exists() and not target.is_dir():
        print(f"error: {target} exists and is not a folder", file=sys.stderr)
        return 1
    try:
        create_workspace(target, ws.app_root)
        install_skills(target, ws, workspace_skill_invocation(), local_workspace=True)
    except OSError as error:
        print(f"error: could not create {target}: {error}", file=sys.stderr)
        return 1
    print(f"Created workspace: {target}")
    print("Run 'mt demo' to try it with example cards.")
    return 0


def command_init(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    if args.directory is None:
        return init_own_workspace(args, resolved)
    destination = Path(args.directory).expanduser().resolve()
    if args.data:
        if args.local:
            print("error: --data creates a workspace without an application, so --local does not apply",
                  file=sys.stderr)
            return 2
        return init_data_workspace(destination, args.origin, ws=resolved)
    if not args.local:
        command = ["bash", str(resolved.app_root / "install.sh"), "--no-path", "--yes", str(destination)]
        if args.origin:
            command.append(args.origin)
        return subprocess.run(command, check=False, env=foreign_env(resolved)).returncode
    if args.origin:
        print("error: --local copies files without Git, so it cannot connect ORIGIN-URL; "
              "see 'mt init --help'", file=sys.stderr)
        return 2
    if destination.exists():
        if validate_repo_workspace(destination) is None:
            print(f"{destination} is already a microtask workspace; nothing was copied.")
            return 0
        print(f"error: {destination} already exists and is not a microtask workspace", file=sys.stderr)
        return 1
    if not destination.parent.is_dir():
        print(f"error: parent folder does not exist: {destination.parent}", file=sys.stderr)
        return 1
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        copy_application(staging, examples=True, ws=resolved)
        install_skills(staging / WORKSPACE_DIRECTORY, resolved,
                       workspace_skill_invocation(), local_workspace=True)
        staging.chmod(0o755)
        os.rename(staging, destination)
    except (OSError, ValueError) as error:
        shutil.rmtree(staging, ignore_errors=True)
        print(f"error: could not create {destination}: {error}", file=sys.stderr)
        return 1
    print(f"Copied microtask from {resolved.app_root} without network access, with your settings "
          "(settings.yaml) and no cards. It has no Git yet; see 'mt init --help' to add sync and updates.",
          flush=True)
    # The installer checks the copy and prints the same next steps as the online path.
    return subprocess.run(["bash", str(destination / "install.sh"), "--no-path", "--yes", str(destination)],
                          check=False, env=foreign_env(resolved)).returncode


def command_web(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    command = [sys.executable, str(resolved.app_root / "tools" / "taskweb.py")]
    if args.no_open:
        command.append("--no-open")
    if args.host:
        command.extend(["--host", args.host])
    if args.port is not None:
        command.extend(["--port", str(args.port)])
    for name in getattr(args, "allow_host", None) or []:
        command.extend(["--allow-host", name])
    query = command_query(args, ws=resolved)
    if query is None:
        return 2
    for field, value in card_query.query_to_mapping(query).items():
        flag = "--" + field.replace("_", "-")
        if value is True:
            command.append(flag)
        else:
            command.extend(f"{flag}={item}" for item in (value if isinstance(value, list) else [value]))
    # Become the dashboard instead of waiting on it as a child: a supervisor's SIGTERM (for
    # example `docker stop`) then reaches the server itself, which closes and finishes a running
    # sync. Ctrl-C and the exit code behave as before.
    sys.stdout.flush()
    sys.stderr.flush()
    os.chdir(resolved.root)
    os.execve(sys.executable, command, workspace_env(resolved))
    return 0  # Not reached, except when tests replace os.execve.


_DEMO_ORPHAN_SECONDS = 300


def _sweep_stale_demo_dirs(demo_parent: Path) -> None:
    """Remove leftover demo directories: unlocked ones, and ones with no lock file after a few minutes."""
    if not demo_parent.is_dir():
        return
    for entry in demo_parent.iterdir():
        if not entry.name.startswith("demo-") or entry.is_symlink() or not entry.is_dir():
            continue
        lock_path = entry / ".demo.lock"
        if not lock_path.exists():
            # A demo that died before taking its lock. A young folder may be one another
            # process is creating right now.
            try:
                age = time.time() - entry.stat().st_mtime
            except OSError:
                continue
            if age > _DEMO_ORPHAN_SECONDS:
                shutil.rmtree(entry, ignore_errors=True)
            continue
        try:
            with lock_path.open("a+") as lock_file:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    continue
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            continue


def command_demo(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    """Start a temporary demo workspace seeded from examples/ and serve its dashboard."""
    resolved = _ws(ws)
    if not (resolved.app_root / "examples").is_dir():
        print("error: bundled examples/ folder is missing", file=sys.stderr)
        return 1
    # Check the filter flags before creating anything. The demo's statuses and priorities come
    # from the same template its workspace is created from.
    template_ws = Workspace.at(resolved.app_root / "templates", app_root=resolved.app_root, environ=demo_environ())
    if command_query(args, ws=template_ws) is None:
        return 2
    dir_name = f"microtask-demo-{os.getuid()}" if hasattr(os, "getuid") else "microtask-demo"
    demo_parent = Path(tempfile.gettempdir()) / dir_name
    demo_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = demo_parent.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or (hasattr(os, "getuid") and info.st_uid != os.getuid())
        or info.st_mode & 0o077
    ):
        print(f"error: {demo_parent} is not a private folder owned by you; remove it and try again", file=sys.stderr)
        return 1
    _sweep_stale_demo_dirs(demo_parent)
    demo_dir = Path(tempfile.mkdtemp(prefix="demo-", dir=demo_parent))
    lock_file = (demo_dir / ".demo.lock").open("a+")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        demo_ws = seed_demo_workspace(demo_dir, app_root=resolved.app_root)
        print(f"Demo workspace: {demo_dir}")
        print(f"Try CLI commands in another terminal: {resolved.app_root / 'mt'} -R {demo_dir} list")
        print("Press Ctrl-C to stop; all demo changes will be deleted on exit.")
        sys.stdout.flush()
        app_str = str(resolved.app_root)
        if app_str not in sys.path:
            sys.path.insert(0, app_str)
        from tools import taskweb  # noqa: E402
        return taskweb.serve_dashboard(
            demo_ws,
            args.host,
            args.port,
            no_open=args.no_open,
            filters=taskweb.board_filters(demo_ws, args),
            allow_hosts=getattr(args, "allow_host", None),
            demo=True,
        )
    except KeyboardInterrupt:
        return 0
    finally:
        shutil.rmtree(demo_dir, ignore_errors=True)
        lock_file.close()
