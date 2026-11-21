"""Explicit workspace identity, roots, policy caching, and demo seeding."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
import os
from pathlib import Path
import re

import card_format
import policy_parser


APP_ROOT = policy_parser.APP_ROOT
DEMO_DATE_ANCHOR = date(2026, 1, 5)

_DETERMINISTIC_POLICY_KEYS = {
    "statuses", "priorities", "types", "due_kinds", "suggested_topics", "suggested_tags",
    "default_status", "default_priority", "default_type", "default_username", "rules", "view_labels",
    "calendar", "lint",
}
_DETERMINISTIC_RULE_KEYS = {
    "auto_sync", "done_window", "max_doing_tasks", "timer_warn_hours",
    "show_year_progress", "show_deadline_progress", "deadline_progress_start",
    "allow_public_origin", "max_attachment_mb", "doing_limit_scope", "local_history",
}


WORKSPACE_DIRECTORY = "workspace"
WORKSPACE_GITIGNORE = ".gitignore"
SETTINGS_FILE = "settings.yaml"
LOCAL_SETTINGS_FILE = "settings.local.yaml"
# Before 0.2.0 the settings file was called mt-policy.yaml. Only detection and 'mt migrate' use these.
OLD_SETTINGS_FILE = "mt-policy.yaml"
OLD_LOCAL_SETTINGS_FILE = "mt-policy.local.yaml"


def is_data_workspace(path: Path) -> bool:
    return (path / "cards").is_dir() and (path / "settings.yaml").is_file() and not (path / "mt").exists()


def is_legacy_workspace(path: Path) -> bool:
    """A folder that keeps cards and settings next to the application code, as older installs did.

    Either settings file name counts: installs from before 0.2.0 call it mt-policy.yaml.
    """
    return ((path / "cards").is_dir()
            and ((path / SETTINGS_FILE).is_file() or (path / OLD_SETTINGS_FILE).is_file())
            and (path / "mt").is_file() and (path / "scripts" / "cli.py").is_file())


def is_unmigrated_workspace(path: Path) -> bool:
    """A workspace folder with no application that still has the old mt-policy.yaml name."""
    return ((path / "cards").is_dir() and (path / OLD_SETTINGS_FILE).is_file()
            and not (path / SETTINGS_FILE).exists() and not (path / "mt").exists())


def enclosing_workspace(path: Path) -> Path | None:
    """Find the nearest workspace containing PATH, including one that needs migration."""
    path = path.resolve()
    for parent in (path, *path.parents):
        if is_data_workspace(parent) or is_legacy_workspace(parent) or is_unmigrated_workspace(parent):
            return parent
    return None


def unmigrated_message(path: Path, command: str) -> str:
    """Tell the user to run 'mt migrate'; command is the form that selects this folder."""
    return (f"the workspace {path} still uses {OLD_SETTINGS_FILE}, which is now {SETTINGS_FILE}. "
            f"Run '{command}' to rename it")


def default_workspace_path(app_root: Path = APP_ROOT) -> Path:
    return app_root / WORKSPACE_DIRECTORY


def resolve_workspace_root(
    environ: Mapping[str, str] | None = None,
    *,
    app_root: Path = APP_ROOT,
) -> Path:
    """Folder holding cards and settings.

    MT_WORKSPACE when set; else app_root/workspace when it is a workspace; else
    app_root itself when it is an old-style workspace; else app_root/workspace,
    which does not exist yet: 'mt init' creates it (see missing_workspace_error).
    """
    env = os.environ if environ is None else environ
    value = env.get("MT_WORKSPACE")
    if value:
        return Path(value)
    default = default_workspace_path(app_root)
    if is_data_workspace(default):
        return default
    if is_legacy_workspace(app_root):
        return app_root
    return default


def workspace_error(
    environ: Mapping[str, str] | None = None,
    *,
    app_root: Path = APP_ROOT,
) -> str | None:
    """Explain why MT_WORKSPACE cannot be used, or None when it is unset or valid."""
    env = os.environ if environ is None else environ
    value = env.get("MT_WORKSPACE")
    if not value:
        return None
    if not Path(value).is_absolute():
        return f"MT_WORKSPACE must be an absolute path: {value}"
    root = resolve_workspace_root(env, app_root=app_root)
    root_resolved = root.resolve()
    app_resolved = app_root.resolve()
    if (root_resolved == app_resolved and is_legacy_workspace(app_resolved)) or is_data_workspace(root):
        return None
    if is_unmigrated_workspace(root):
        return unmigrated_message(root, f"MT_WORKSPACE={root} {app_root / 'mt'} migrate")
    return (
        f"MT_WORKSPACE must name a workspace folder (cards/ and settings.yaml, no mt); "
        f"select a full workspace with -R instead: {value}"
    )


# Instructions for agents started in the workspace, such as by 'mt run' or a cloud service.
AGENT_FILES = ("AGENTS.md", "CLAUDE.md")


def create_workspace(path: Path, app_root: Path = APP_ROOT, *, policy: Path | None = None) -> None:
    """Create or complete a workspace at path from templates/, never overwriting a file.

    policy names the settings.yaml to start from; the default is the one in templates/.
    """
    path = Path(path)
    templates = app_root / "templates"
    path.mkdir(parents=True, exist_ok=True)
    (path / "cards").mkdir(exist_ok=True)
    (path / "cards" / ".gitkeep").touch()
    (path / "drafts").mkdir(exist_ok=True)
    if policy is None or not Path(policy).is_file():
        policy = templates / "settings.yaml"
    for name, source in (("settings.yaml", policy),
                         (WORKSPACE_GITIGNORE, templates / "workspace.gitignore")):
        if not (path / name).exists():
            (path / name).write_bytes(Path(source).read_bytes())
    add_agent_files(path, app_root)


def add_agent_files(path: Path, app_root: Path = APP_ROOT) -> list[str]:
    """Add the agent instruction files a workspace is missing, never overwriting one; return their names."""
    added = []
    for name in AGENT_FILES:
        if not (Path(path) / name).exists():
            (Path(path) / name).write_bytes((app_root / "templates" / f"workspace-{name}").read_bytes())
            added.append(name)
    return added


def missing_workspace_error(
    environ: Mapping[str, str] | None = None,
    *,
    app_root: Path = APP_ROOT,
) -> str | None:
    """Explain how to create the workspace when there is none yet, or None when there is one.

    Nothing creates a workspace as a side effect of another command: only 'mt init' and
    the installer do, so running mt from the wrong folder fails instead of adding data there.
    """
    env = os.environ if environ is None else environ
    if env.get("MT_WORKSPACE"):
        return None
    default = default_workspace_path(app_root)
    if is_unmigrated_workspace(default):
        return unmigrated_message(default, f"{Path(app_root) / 'mt'} migrate")
    root = resolve_workspace_root(env, app_root=app_root)
    if root != default or is_data_workspace(default):
        return None
    if (Path(app_root) / ".mt-hosted").exists():
        return ("no workspace selected. This is a hosted version folder: workspaces live in /srv/mt. "
                "Select one with -R /srv/mt/NAME or MT_WORKSPACE")
    return f"no workspace yet. Run '{Path(app_root) / 'mt'} init' to create one in {default}"


def _shift_demo_date(match: re.Match[str], offset: int) -> str:
    try:
        shifted = date(int(match.group(1)), int(match.group(2)), int(match.group(3))) + timedelta(days=offset)
    except ValueError:
        return match.group(0)
    return shifted.isoformat() + (match.group(4) or "")


def _example_files(examples: Path) -> list[tuple[Path, Path]]:
    """Return (relative, source) for every non-hidden file under examples/, in a stable order."""
    if not examples.is_dir():
        return []
    files = []
    for source in sorted(examples.rglob("*")):
        relative = source.relative_to(examples)
        if source.is_file() and not any(part.startswith(".") for part in relative.parts):
            files.append((relative, source))
    return files


def rebase_demo_dates(text: str) -> str:
    offset = (date.today() - DEMO_DATE_ANCHOR).days
    if offset == 0:
        return text
    return re.sub(
        r"\b(\d{4})-(\d{2})-(\d{2})(T\d{2}:\d{2}:\d{2})?\b",
        lambda match: _shift_demo_date(match, offset),
        text,
    )


_DEMO_UNSET_ENV = ("MT_WORKSPACE", "MT_PROJECT", "MT_TOPIC")


def demo_environ() -> dict[str, str]:
    """The shell environment without the variables that select a workspace or narrow its cards."""
    return {key: value for key, value in os.environ.items() if key not in _DEMO_UNSET_ENV}


def seed_demo_workspace(target: Path | str, *, app_root: Path = APP_ROOT) -> Workspace:
    """Create a temporary demo workspace seeded from examples/ with auto_sync disabled."""
    root = Path(target)
    resolved_app = Path(app_root)
    create_workspace(root, app_root=resolved_app)
    (root / "attachments").mkdir(exist_ok=True)
    # Keep demo sessions local-only regardless of template defaults.
    (root / LOCAL_SETTINGS_FILE).write_text("rules:\n  auto_sync: false\n", encoding="utf-8")
    for relative, source in _example_files(resolved_app / "examples"):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix == ".md":
            destination.write_text(rebase_demo_dates(source.read_text(encoding="utf-8")), encoding="utf-8")
        else:
            destination.write_bytes(source.read_bytes())
    return Workspace.at(root, app_root=resolved_app, environ=demo_environ())


def _stat_signature(path: Path) -> tuple[int, int, int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino


class PolicyCache:
    """Parsed policy shared by the handles of one workspace, reloaded when a policy file changes.

    The file signatures and the parsed policy are stored together as one value, so a
    thread never pairs a new signature with an old policy. Callers must treat the
    returned policy as read-only.
    """

    def __init__(self) -> None:
        self.entry: tuple[tuple[object, object], dict] | None = None


class Workspace:
    """Explicit workspace context replacing module-level REPO_ROOT globals."""

    def __init__(
        self,
        root: Path,
        *,
        app_root: Path = APP_ROOT,
        environ: Mapping[str, str] | None = None,
        policy_cache: PolicyCache | None = None,
    ) -> None:
        self.root = Path(root)
        self.app_root = Path(app_root)
        self.environ = dict(os.environ if environ is None else environ)
        self._policy_cache = policy_cache if policy_cache is not None else PolicyCache()

    @classmethod
    def at(
        cls,
        path: Path | str,
        *,
        app_root: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> Workspace:
        return cls(
            Path(path),
            app_root=APP_ROOT if app_root is None else Path(app_root),
            environ=environ,
        )

    @classmethod
    def resolve(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        app_root: Path | None = None,
    ) -> Workspace:
        resolved_app = APP_ROOT if app_root is None else Path(app_root)
        env = os.environ if environ is None else environ
        return cls(resolve_workspace_root(env, app_root=resolved_app), app_root=resolved_app, environ=env)

    def for_operation(self) -> Workspace:
        """Return a Workspace handle sharing the policy cache."""
        return Workspace(self.root, app_root=self.app_root, environ=self.environ, policy_cache=self._policy_cache)

    @property
    def in_app(self) -> bool:
        """The workspace is the application's own: its workspace/ folder, or an old-style root."""
        return self.root.resolve() in (self.app_root.resolve(), default_workspace_path(self.app_root).resolve())

    def policy(self) -> dict:
        shared, local = policy_parser.settings_paths(self.root)
        stamp = (_stat_signature(shared), _stat_signature(local))
        entry = self._policy_cache.entry
        if entry is not None and entry[0] == stamp:
            return entry[1]
        raw = policy_parser.load_policy(self.root)
        view = {key: value for key, value in raw.items() if key in _DETERMINISTIC_POLICY_KEYS}
        rules = raw.get("rules")
        if isinstance(rules, dict):
            view["rules"] = {key: value for key, value in rules.items() if key in _DETERMINISTIC_RULE_KEYS}
        self._policy_cache.entry = (stamp, view)
        return view

    def statuses(self) -> list[str]:
        return list(self.policy().get("statuses", {}).keys())

    def open_statuses(self) -> list[str]:
        return [name for name in self.statuses() if name != "done"]

    def cards_root(self) -> Path:
        return self.root / "cards"

    def attachments_root(self) -> Path:
        return self.root / "attachments"

    def focus_state_path(self, user: str | None = None) -> Path:
        """The focus file: one per workspace, or one per person in a shared workspace."""
        if user is None:
            return self.root / ".mt" / "focus"
        if not card_format.is_safe_username(user):
            raise ValueError(f"not a safe user name for per-person state: {user!r}")
        return self.root / ".mt" / "focus-users" / user

    def legacy_timer_path(self) -> Path:
        """The old timer file before timers moved onto cards; read once, then renamed."""
        return self.root / ".mt" / "time-running"

    def mutation_lock_path(self) -> Path:
        return self.root / ".mt" / "time-mutation.lock"

    def sync_lock_path(self) -> Path:
        return self.root / ".mt" / "sync.lock"

    def sync_error_path(self) -> Path:
        return self.root / ".mt" / "last-sync-error"

    def scratch_root(self) -> Path:
        return self.root / ".mt" / "tmp"

    def recovery_dir(self) -> Path:
        return self.root / ".mt" / "recovery" / "edit"

    def history_db_path(self) -> Path:
        return self.root / ".mt" / "history.sqlite"

    def env_default(self, name: str) -> str | None:
        return self.environ.get(name)

    def workspace_env(self) -> dict[str, str]:
        return {**self.environ, "MT_WORKSPACE": str(self.root)}

    def foreign_env(self) -> dict[str, str]:
        """Environment for another application copy, which must use its own workspace."""
        return {key: value for key, value in self.environ.items() if key != "MT_WORKSPACE"}

    def run_env(self) -> dict[str, str]:
        """Environment for a command run in the workspace: plain mt runs this app on this workspace."""
        env = self.foreign_env()
        env["PATH"] = os.pathsep.join(filter(None, (str(self.app_root), self.environ.get("PATH", ""))))
        if is_data_workspace(self.root):
            env["MT_WORKSPACE"] = str(self.root)
        return env


if __name__ == "__main__":
    import sys

    if len(sys.argv) == 3 and sys.argv[1] == "create":
        create_workspace(Path(sys.argv[2]).absolute())
    else:
        print("usage: workspace.py create PATH", file=sys.stderr)
        raise SystemExit(2)
