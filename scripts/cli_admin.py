"""Administrative, setup, skill, repository, view, and web CLI commands."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap

import card_query
from cli_helpers import (
    REPO_ALIAS_RE,
    load_repo_aliases,
    show_workspace,
    validate_repo_workspace,
    write_repo_aliases,
)
import git_ops
import store
from store import (
    _ws,
    active_root,
    command_query,
    demo_active,
    discover,
    foreign_env,
    item_json_record,
    now_stamp,
    select_cards,
    view_marker,
    workspace_env,
)
from transaction import time_mutation_lock
import validation
from workspace import (
    VIEW_MARKER,
    VIEW_NAME_RE,
    Workspace,
    is_data_workspace,
)

SKILL_TARGETS = (".agents/skills", ".claude/skills")
SKILL_MARKER = "installed-by: microtask"
SKILL_WORKSPACE_BLOCK = re.compile(r"<!-- mt:workspace -->\n.*?<!-- /mt:workspace -->", re.DOTALL)
SKILL_COMMAND = re.compile(r"(?<![\w/.-])(?:\./)?mt(?= [A-Za-z-])")
APPLICATION_FILES = ("mt", "mt-policy.yaml", "install.sh", "README.md", "LICENSE", "AGENTS.md",
                     "CLAUDE.md", "GEMINI.md", "CONTRIBUTING.md", ".gitignore")
APPLICATION_FOLDERS = ("scripts", "tools", "templates", ".agents")


def portable_invocation(ws: Workspace | None = None) -> tuple[str, str | None]:
    """Return a path-free command that reaches this workspace, and the alias it uses.

    Plain mt when this workspace is the one on PATH, otherwise mt -R ALIAS.
    Raises ValueError when neither reaches it.
    """
    resolved = _ws(ws)
    on_path = shutil.which("mt")
    if on_path and resolved.root == resolved.app_root and Path(on_path).resolve() == (resolved.app_root / "mt").resolve():
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
    try:
        text = (folder / "SKILL.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    match = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    return bool(match and re.search(rf"^\s+{re.escape(SKILL_MARKER)}\s*$", match.group(1), re.MULTILINE))


def render_skill_file(text: str, invocation: str, alias: str | None, *, scoped: bool, skill_md: bool) -> str:
    """Point a skill's commands at this workspace and mark the copy as installed by microtask."""
    text = SKILL_COMMAND.sub(lambda _: invocation, text)
    check = ("Each command prints `Workspace:` and `Scope:` lines on stderr; stop and ask if either is "
             "missing or wrong." if scoped else
             "Each command prints a `Workspace:` line on stderr; stop and ask if it is missing or wrong.")
    missing = (f"If `mt` is not found, or the alias `{alias}` is unknown, ask the user to install microtask "
               f"or run `mt repo add {alias} PATH`." if alias else
               "If `mt` is not found, ask the user to install microtask.")
    block = f"Run every command as `{invocation} COMMAND`. {check} {missing}"
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


def command_skills_install(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    project_dir = Path(args.dir).expanduser().resolve()
    sources = skill_sources(resolved)
    if not sources:
        print(f"error: no skills found in {resolved.app_root / '.agents' / 'skills'}", file=sys.stderr)
        return 1
    names = {source.name for source in sources}
    conflicts = [project_dir / target / name for target in SKILL_TARGETS for name in sorted(names)
                 if (project_dir / target / name).exists()
                 and not installed_by_microtask(project_dir / target / name)]
    if conflicts:
        print("error: these skills already exist and were not installed by microtask; rename or remove them:",
              file=sys.stderr)
        for path in conflicts:
            print(f"  {path}", file=sys.stderr)
        return 1
    invocation, alias, local = skills_invocation(args.project, args.topic, ws=resolved)
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
                                                    scoped=bool(args.project or args.topic),
                                                    skill_md=path == source / "SKILL.md").encode("utf-8")
                    except (UnicodeError, ValueError) as error:
                        print(f"error: {path}: {error}", file=sys.stderr)
                        return 1
            files.append((path.relative_to(source), content))
        rendered[source.name] = files
    for target in SKILL_TARGETS:
        base = project_dir / target
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
                staging.chmod(0o755)
                current = base / name
                if current.exists():
                    shutil.rmtree(current)
                os.rename(staging, current)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
    print(f"Installed {len(sources)} skills in {', '.join(str(project_dir / target) for target in SKILL_TARGETS)}:")
    print(f"  {', '.join(sorted(names))}")
    print(f"They run `{invocation} COMMAND`. Run this again after mt update to refresh them.")
    if local:
        print("warning: the skills name a folder on this machine, so do not commit them. To share them, give "
              f"the workspace an alias with mt repo add NAME {shlex.quote(str(resolved.root))} and install again "
              "with mt -R NAME skills install.", file=sys.stderr)
    if resolved.root == resolved.app_root and not (args.project or args.topic):
        print("note: agents will see every card in this workspace; add --project or --topic to limit them.",
              file=sys.stderr)
    return 0


def command_skills_uninstall(args: argparse.Namespace) -> int:
    project_dir = Path(args.dir).expanduser().resolve()
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
    if resolved.root != resolved.app_root:
        box_line(f"Application: {resolved.app_root}", "2", location=True)
    box_line()
    total = task_count + notes_count
    box_line(f"{total} card{'s' if total != 1 else ''}   ·   "
             f"{task_count} task{'s' if task_count != 1 else ''}   ·   "
             f"{notes_count} note{'s' if notes_count != 1 else ''}", "1")
    if demo_active(resolved):
        box_line("Demo workspace · bundled examples", "33")
    marker = view_marker(resolved)
    if marker is not None:
        box_line(f"View copy: {marker['name']}", "33")
        box_line(f"Source: {marker['source']}", location=True)
        box_line("Changes stay in this view copy.")
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
        for action, remote in (("Sync", "origin"), ("Update", "upstream")):
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
    return 0


def command_policy(args: argparse.Namespace) -> int:
    """Show effective policy, with JSON available for automation."""
    try:
        effective = store.policy()
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if args.as_json:
        print(json.dumps(effective, ensure_ascii=False, sort_keys=True))
        return 0
    print("Effective workspace policy")
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


def command_scratch(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    """Allocate a private working directory in the selected workspace."""
    parent = _ws(ws).root / ".mt" / "scratch"
    parent.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="run-", dir=parent)).resolve()
    if args.as_json:
        print(json.dumps({"path": str(path)}))
    else:
        print(path)
    return 0


def view_name_from_args(args: argparse.Namespace, query: card_query.Query) -> str:
    supplied = getattr(args, "name", None)
    source_text = query.text or next(iter(query.project or query.tag), None)
    if supplied is None:
        if not source_text:
            raise ValueError("provide --name when no project, tag, or search text supplies a name")
        slug = "-".join(re.findall(r"[a-z0-9]+", str(source_text).casefold()))
        if not slug or len(slug) > 64:
            raise ValueError("cannot derive a safe view name; provide --name")
        return slug
    if len(supplied) > 64 or not VIEW_NAME_RE.fullmatch(supplied):
        raise ValueError("--name must be a lowercase slug using letters, digits, and single hyphens")
    return supplied


def validated_view_items(records: object, ws: Workspace | None = None) -> list[tuple[str, str, bytes, str]]:
    """Verify selector provenance against the active card roots and current bytes."""
    resolved_ws = _ws(ws)
    if not isinstance(records, list):
        raise ValueError("item selector did not return a JSON array")
    root = store.cards_root(resolved_ws).resolve()
    selected: list[tuple[str, str, bytes, str]] = []
    seen_paths: set[str] = set()
    destinations: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("item selector returned a malformed record")
        kind, raw_path, expected_hash = record.get("kind"), record.get("path"), record.get("content_hash")
        if (
            not isinstance(kind, str)
            or kind not in {"task", "note"}
            or not isinstance(raw_path, str)
            or not isinstance(expected_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
            or raw_path in seen_paths
        ):
            raise ValueError("item selector returned a malformed or duplicate item record")
        relative = Path(raw_path)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError(f"item selector returned an unsafe card path: {raw_path!r}")
        source_path = resolved_ws.root / relative
        try:
            resolved = source_path.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as error:
            raise ValueError(f"item selector returned a path outside the current {kind} cards: {raw_path}") from error
        if source_path.is_symlink() or resolved.parent != root or resolved.suffix != ".md":
            raise ValueError(f"item selector returned a path outside the current {kind} cards: {raw_path}")
        if relative.parent != root.relative_to(resolved_ws.root.resolve()):
            raise ValueError(f"item selector returned a path outside the current {kind} cards: {raw_path}")
        raw = source_path.read_bytes()
        actual_hash = hashlib.sha256(raw).hexdigest()
        if actual_hash != expected_hash:
            raise ValueError(f"selected card changed during view creation: {raw_path}; rerun the command")
        destination_key = source_path.name
        if destination_key in destinations:
            raise ValueError(f"selected cards collide at cards/{source_path.name}")
        seen_paths.add(raw_path)
        destinations.add(destination_key)
        selected.append((str(kind), raw_path, raw, actual_hash))
    return selected


def copy_application(
    target: Path, *, local_policy: bool = False, examples: bool = False, ws: Workspace | None = None,
) -> None:
    """Copy the self-contained application into TARGET with an empty cards/ folder.

    Views keep this machine's mt-policy.local.yaml; a new workspace gets the demo examples.
    """
    resolved = _ws(ws)
    files = APPLICATION_FILES + (("mt-policy.local.yaml",) if local_policy else ())
    folders = APPLICATION_FOLDERS + (("examples",) if examples else ())
    for name in files:
        # Settings come from the workspace in use; everything else is the application.
        root = resolved.root if name.startswith("mt-policy") else resolved.app_root
        if (root / name).is_file():
            shutil.copy2(root / name, target / name)
    for name in folders:
        if (resolved.app_root / name).is_dir():
            shutil.copytree(resolved.app_root / name, target / name,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "*.swp", ".env"))
    (target / "cards").mkdir()


def create_view(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    query = command_query(args, ws=resolved)
    if query is None:
        return 2
    if query.match != "exact" and not query.text:
        print("error: --and, --or, --fuzzy, and --match require search words", file=sys.stderr)
        return 2
    if query == card_query.DEFAULT and not args.name:
        print("error: view requires search words, a filter, or --name", file=sys.stderr)
        return 2
    try:
        name = view_name_from_args(args, query)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    views_root = resolved.root / ".mt" / "views"
    destination = views_root / name
    if destination.exists():
        print(f"error: view already exists: {destination}", file=sys.stderr)
        return 1

    work_parent = resolved.root / ".mt" / "scratch" / "task-views"
    work_parent.mkdir(parents=True, exist_ok=True)
    work_root = Path(tempfile.mkdtemp(prefix="run-", dir=work_parent))
    try:
        try:
            selected = validated_view_items(
                [item_json_record(card, ws=resolved) for card in select_cards(discover(ws=resolved), query)],
                ws=resolved,
            )
        except (OSError, ValueError) as error:
            print(f"error: could not create view: {error}", file=sys.stderr)
            return 1

        copy_application(work_root, local_policy=True, ws=resolved)
        items: list[dict[str, str]] = []
        for kind, source_path, raw, content_hash in selected:
            filename = Path(source_path).name
            target = work_root / "cards" / filename
            target.write_bytes(raw)
            items.append({"kind": kind, "path": source_path, "content_hash": content_hash})
        marker = {
            "version": 1,
            "name": name,
            "source": str(resolved.root.resolve()),
            "created": now_stamp(),
            "items": items,
        }
        (work_root / VIEW_MARKER).write_text(
            json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        view_ws = Workspace(work_root, app_root=resolved.app_root)
        try:
            view_policy = store.policy(view_ws)
            report = validation.audit_workspace(work_root, view_policy)
        except (OSError, ValueError) as error:
            print(f"error: {error}", file=sys.stderr)
            print("error: selected items did not validate in the new view", file=sys.stderr)
            return 1
        if report.errors:
            for error in report.errors:
                print(f"  - {error}", file=sys.stderr)
            print("error: selected items did not validate in the new view", file=sys.stderr)
            return 1

        views_root.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            print(f"error: view already exists: {destination}", file=sys.stderr)
            return 1
        os.rename(work_root, destination)
        print(f"Created view: {destination}")
        print(f"Selected items: {len(selected)}")
        return 0
    except (OSError, ValueError) as error:
        print(f"error: could not create view: {error}", file=sys.stderr)
        return 1
    finally:
        if work_root.exists():
            shutil.rmtree(work_root)


def command_view(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    # The existing mutation lock protects both the fixed selection snapshot
    # and the exclusive destination check from concurrent card edits/views.
    resolved = _ws(ws)
    with time_mutation_lock(resolved):
        resolved.reset_demo()
        resolved.pin_demo()
        resolved.maybe_seed_demo()
        return create_view(args, ws=resolved)


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
        # The command is not this mt, so any mt it starts picks its own workspace.
        os.execvpe(args.argv[0], args.argv, foreign_env(resolved))
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
    return subprocess.run(command, cwd=resolved.app_root, check=False, env=foreign_env(resolved)).returncode


def command_import_github(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    show_workspace(ws=resolved)
    import import_github

    argv: list[str] = []
    if args.repo:
        argv.extend(["--repo", args.repo])
    if getattr(args, "as_json", False):
        argv.append("--json")
    return import_github.main(argv, ws=resolved)


def command_validate(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    try:
        effective_policy = store.policy(resolved)
    except (OSError, ValueError) as error:
        print("FAILED: invalid policy:", file=sys.stderr)
        for line in str(error).splitlines():
            print(f"  - {line}", file=sys.stderr)
        return 1
    report = validation.audit_workspace(active_root(resolved), effective_policy)
    if report.errors:
        print(f"FAILED: {len(report.errors)} error(s):", file=sys.stderr)
        for error in report.errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    for warning in report.warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    print(
        f"OK: validated {report.task_count} task file(s), {report.note_count} note file(s), "
        f"and {report.draft_count} draft file(s)."
    )
    return 0


def init_data_workspace(destination: Path, origin: str | None, ws: Workspace | None = None) -> int:
    """Create a workspace holding only cards and settings, run by this mt."""
    resolved = _ws(ws)
    if destination.exists():
        if is_data_workspace(destination):
            print(f"{destination} is already a data-only workspace; nothing changed.")
            return 0
        print(f"error: {destination} already exists", file=sys.stderr)
        return 1
    if not destination.parent.is_dir():
        print(f"error: parent folder does not exist: {destination.parent}", file=sys.stderr)
        return 1
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        (staging / "cards").mkdir()
        (staging / "cards" / ".gitkeep").touch()
        shutil.copy2(resolved.root / "mt-policy.yaml", staging / "mt-policy.yaml")
        (staging / ".gitignore").write_text(".mt/\nmt-policy.local.yaml\n.DS_Store\n", encoding="utf-8")
        staging.chmod(0o755)
        os.rename(staging, destination)
    except OSError as error:
        shutil.rmtree(staging, ignore_errors=True)
        print(f"error: could not create {destination}: {error}", file=sys.stderr)
        return 1
    print(f"Created a data-only workspace in {destination} with your settings (mt-policy.yaml) and no cards.")
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


def command_init(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    destination = Path(args.directory).expanduser().resolve()
    if args.data:
        if args.local:
            print("error: --data creates a workspace without an application, so --local does not apply",
                  file=sys.stderr)
            return 2
        return init_data_workspace(destination, args.origin, ws=resolved)
    if not args.local:
        command = ["bash", str(resolved.app_root / "install.sh"), "--no-path", str(destination)]
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
        (staging / "cards" / ".gitkeep").touch()
        staging.chmod(0o755)
        os.rename(staging, destination)
    except OSError as error:
        shutil.rmtree(staging, ignore_errors=True)
        print(f"error: could not create {destination}: {error}", file=sys.stderr)
        return 1
    print(f"Copied microtask from {resolved.app_root} without network access, with your settings "
          "(mt-policy.yaml) and no cards. It has no Git yet; see 'mt init --help' to add sync and updates.",
          flush=True)
    # The installer checks the copy and prints the same next steps as the online path.
    return subprocess.run(["bash", str(destination / "install.sh"), "--no-path", str(destination)],
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
    query = command_query(args, ws=resolved)
    if query is None:
        return 2
    for field, value in card_query.query_to_mapping(query).items():
        flag = "--" + field.replace("_", "-")
        if value is True:
            command.append(flag)
        else:
            command.extend(arg for item in (value if isinstance(value, list) else [value]) for arg in (flag, item))
    try:
        return subprocess.run(command, cwd=resolved.root, check=False, env=workspace_env(resolved)).returncode
    except KeyboardInterrupt:
        return 130
