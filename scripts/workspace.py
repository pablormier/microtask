"""Explicit workspace identity, roots, policy caching, and demo resolution."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
import os
from pathlib import Path
import re

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


def is_data_workspace(path: Path) -> bool:
    return (path / "cards").is_dir() and (path / "mt-policy.yaml").is_file() and not (path / "mt").exists()


def resolve_workspace_root(
    environ: Mapping[str, str] | None = None,
    *,
    app_root: Path = APP_ROOT,
) -> Path:
    """Folder holding cards and policy: MT_WORKSPACE when set, else app_root."""
    env = os.environ if environ is None else environ
    value = env.get("MT_WORKSPACE")
    if not value:
        return app_root
    return Path(value)


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
    if root == app_root or is_data_workspace(root):
        return None
    return (
        f"MT_WORKSPACE must name a data-only workspace (cards/ and mt-policy.yaml, no mt); "
        f"select a full workspace with -R instead: {value}"
    )


def rebase_demo_dates(text: str) -> str:
    offset = (date.today() - DEMO_DATE_ANCHOR).days
    if offset == 0:
        return text
    return re.sub(
        r"\b(\d{4})-(\d{2})-(\d{2})(T\d{2}:\d{2}:\d{2})?\b",
        lambda match: (
            date(int(match.group(1)), int(match.group(2)), int(match.group(3))) + timedelta(days=offset)
        ).isoformat()
        + (match.group(4) or ""),
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
    def is_data_only(self) -> bool:
        return self.root != self.app_root and is_data_workspace(self.root)

    def real_cards_root(self) -> Path:
        return self.root / "cards"

    def demo_root(self) -> Path:
        return self.root / ".mt" / "demo"

    def is_empty(self) -> bool:
        return not any(self.real_cards_root().glob("*.md"))

    def policy(self) -> dict:
        stamp = (
            _stat_signature(self.root / "mt-policy.yaml"),
            _stat_signature(self.root / "mt-policy.local.yaml"),
        )
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
            and self.root == self.app_root
            and (self.app_root / "examples").is_dir()
            and self.is_empty()
            and not self.real_legacy_timer_path().exists()
        )
        return self._demo_state

    def reset_demo(self) -> None:
        self._demo_state = None

    def demo_active(self) -> bool:
        if self._demo_state is None:
            return self.pin_demo()
        return self._demo_state

    def maybe_seed_demo(self) -> None:
        if not self.demo_active():
            return
        if (self.demo_root() / "cards").is_dir():
            return
        examples = self.app_root / "examples"
        for source in sorted(examples.rglob("*.md")):
            relative = source.relative_to(examples)
            destination = self.demo_root() / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(rebase_demo_dates(source.read_text(encoding="utf-8")), encoding="utf-8")

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
