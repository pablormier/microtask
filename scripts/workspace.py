"""Explicit workspace identity, roots, policy caching, and demo resolution."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
import os
from pathlib import Path
import re
import shutil
import uuid

import policy_parser


APP_ROOT = policy_parser.APP_ROOT
DEMO_DATE_ANCHOR = date(2026, 1, 5)

_DETERMINISTIC_POLICY_KEYS = {
    "statuses", "priorities", "types", "due_kinds", "suggested_topics", "suggested_tags",
    "default_status", "default_priority", "default_type", "default_username", "rules", "view_labels",
    "calendar", "lint",
}
_DETERMINISTIC_RULE_KEYS = {
    "auto_sync", "done_window", "max_doing_tasks", "demo_workspace", "timer_warn_hours",
    "show_year_progress", "show_deadline_progress", "deadline_progress_start",
    "allow_public_origin",
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


def rebase_demo_dates(text: str) -> str:
    offset = (date.today() - DEMO_DATE_ANCHOR).days
    if offset == 0:
        return text
    return re.sub(
        r"\b(\d{4})-(\d{2})-(\d{2})(T\d{2}:\d{2}:\d{2})?\b",
        lambda match: _shift_demo_date(match, offset),
        text,
    )


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
    """Explicit workspace context replacing module-level REPO_ROOT/_DEMO_STATE globals."""

    def __init__(
        self,
        root: Path,
        *,
        app_root: Path = APP_ROOT,
        environ: Mapping[str, str] | None = None,
        demo: bool | None = None,
        policy_cache: PolicyCache | None = None,
    ) -> None:
        self.root = Path(root)
        self.app_root = Path(app_root)
        self.environ = dict(os.environ if environ is None else environ)
        self._demo_state: bool | None = demo
        self._policy_cache = policy_cache if policy_cache is not None else PolicyCache()

    @classmethod
    def at(
        cls,
        path: Path | str,
        *,
        app_root: Path | None = None,
        environ: Mapping[str, str] | None = None,
        demo: bool | None = None,
    ) -> Workspace:
        return cls(
            Path(path),
            app_root=APP_ROOT if app_root is None else Path(app_root),
            environ=environ,
            demo=demo,
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
        """Return a fresh Workspace handle that resolves demo mode per operation and shares the policy cache."""
        return Workspace(self.root, app_root=self.app_root, environ=self.environ, demo=None,
                         policy_cache=self._policy_cache)

    @property
    def in_app(self) -> bool:
        """The workspace is the application's own: its workspace/ folder, or an old-style root."""
        return self.root.resolve() in (self.app_root.resolve(), default_workspace_path(self.app_root).resolve())

    def real_cards_root(self) -> Path:
        return self.root / "cards"

    def demo_root(self) -> Path:
        return self.root / ".mt" / "demo"

    def is_empty(self) -> bool:
        return not any(self.real_cards_root().glob("*.md"))

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

    def pin_demo(self) -> bool:
        """Pin the demo decision so mid-command mutations stay consistent.

        A running timer lives on a real card, and a workspace with cards is not
        empty, so only a legacy timer file not yet imported keeps it real.
        """
        enabled = self.policy().get("rules", {}).get("demo_workspace", True)
        self._demo_state = bool(
            enabled
            and self.in_app
            and (self.app_root / "examples").is_dir()
            and self.is_empty()
            and not self.real_legacy_timer_path().exists()
        )
        return self._demo_state

    def demo_active(self) -> bool:
        if self._demo_state is None:
            return self.pin_demo()
        return self._demo_state

    def maybe_seed_demo(self) -> None:
        if not self.demo_active():
            return
        demo = self.demo_root()
        if (demo / "cards").is_dir():
            return
        examples = self.app_root / "examples"
        demo.parent.mkdir(parents=True, exist_ok=True)
        staging = demo.parent / f".demo.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        try:
            staging.mkdir(parents=True, exist_ok=True)
            for source in sorted(examples.rglob("*.md")):
                relative = source.relative_to(examples)
                destination = staging / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(rebase_demo_dates(source.read_text(encoding="utf-8")), encoding="utf-8")
            if (demo / "cards").is_dir():
                return
            if demo.exists():
                shutil.rmtree(demo)
            try:
                os.replace(staging, demo)
            except OSError:
                if not (demo / "cards").is_dir():
                    raise
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    def active_root(self) -> Path:
        return self.demo_root() if self.demo_active() else self.root

    def cards_root(self) -> Path:
        if self.demo_active():
            return self.demo_root() / "cards"
        return self.real_cards_root()

    def focus_state_path(self) -> Path:
        if self.demo_active():
            return self.demo_root() / "focus"
        return self.root / ".mt" / "focus"

    def real_legacy_timer_path(self) -> Path:
        """Where releases before timers moved onto cards kept real-workspace timers."""
        return self.root / ".mt" / "time-running"

    def legacy_timer_path(self) -> Path:
        """The old timer file for the active cards (demo mode kept its own); read once, then renamed."""
        if self.demo_active():
            return self.demo_root() / "time-running"
        return self.real_legacy_timer_path()

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

    def env_default(self, name: str) -> str | None:
        return self.environ.get(name)

    def workspace_env(self) -> dict[str, str]:
        return {**self.environ, "MT_WORKSPACE": str(self.root)}

    def foreign_env(self) -> dict[str, str]:
        """Environment for another application copy, which must use its own workspace."""
        return {key: value for key, value in self.environ.items() if key != "MT_WORKSPACE"}


if __name__ == "__main__":
    import sys

    if len(sys.argv) == 3 and sys.argv[1] == "create":
        create_workspace(Path(sys.argv[2]).absolute())
    else:
        print("usage: workspace.py create PATH", file=sys.stderr)
        raise SystemExit(2)
