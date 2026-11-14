"""Explicit workspace identity, roots, policy caching, and demo resolution."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
import json
import os
from pathlib import Path
import re

import card_format
import policy_parser


APP_ROOT = policy_parser.APP_ROOT
VIEW_MARKER = "mt-view.json"
VIEW_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DEMO_DATE_ANCHOR = date(2026, 1, 5)

_DETERMINISTIC_POLICY_KEYS = {
    "statuses", "priorities", "types", "due_kinds", "suggested_topics", "suggested_tags",
    "default_status", "default_priority", "default_type", "default_username", "rules", "view_labels",
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
    if not value or (app_root / VIEW_MARKER).is_file():
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


def _stat_mtime(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


class Workspace:
    """Explicit workspace context replacing module-level REPO_ROOT/_DEMO_STATE globals."""

    def __init__(
        self,
        root: Path,
        *,
        app_root: Path = APP_ROOT,
        environ: Mapping[str, str] | None = None,
        demo: bool | None = None,
    ) -> None:
        self.root = Path(root)
        self.app_root = Path(app_root)
        self.environ = dict(os.environ if environ is None else environ)
        self._demo_state: bool | None = demo
        self._policy_stamp: tuple[int | None, int | None, int | None] | None = None
        self._policy_cache: dict | None = None

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
        """Return a fresh Workspace handle that resolves demo mode per operation."""
        return Workspace(self.root, app_root=self.app_root, environ=self.environ, demo=None)

    @property
    def is_data_only(self) -> bool:
        return self.root != self.app_root and is_data_workspace(self.root)

    @property
    def is_view(self) -> bool:
        return (self.root / VIEW_MARKER).is_file()

    def real_cards_root(self) -> Path:
        return self.root / "cards"

    def demo_root(self) -> Path:
        return self.root / ".mt" / "demo"

    def is_empty(self) -> bool:
        return not any(self.real_cards_root().glob("*.md"))

    def view_marker(self) -> dict[str, object] | None:
        """Read and validate the provenance marker for a materialized view."""
        marker_path = self.root / VIEW_MARKER
        if not marker_path.exists():
            return None
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError(f"view marker is malformed: {marker_path}: {error}") from error
        expected = {"version", "name", "source", "created", "items"}
        if not isinstance(marker, dict) or set(marker) != expected:
            raise ValueError(f"view marker is malformed: {marker_path}")
        if type(marker.get("version")) is not int or marker.get("version") != 1:
            raise ValueError(f"unsupported view marker version in {marker_path}: {marker.get('version')!r}")
        if (
            not isinstance(marker.get("name"), str)
            or not VIEW_NAME_RE.fullmatch(str(marker["name"]))
            or not isinstance(marker.get("source"), str)
            or not Path(str(marker["source"])).is_absolute()
            or not isinstance(marker.get("created"), str)
            or not card_format.is_timestamp(str(marker["created"]))
            or not isinstance(marker.get("items"), list)
        ):
            raise ValueError(f"view marker is malformed: {marker_path}")
        seen_paths: set[str] = set()
        for item in marker["items"]:
            if (
                not isinstance(item, dict)
                or set(item) != {"kind", "path", "content_hash"}
                or not isinstance(item.get("kind"), str)
                or item.get("kind") not in {"task", "note"}
                or not isinstance(item.get("path"), str)
                or not isinstance(item.get("content_hash"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", str(item["content_hash"]))
                or str(item["path"]) in seen_paths
            ):
                raise ValueError(f"view marker is malformed: {marker_path}")
            relative = Path(str(item["path"]))
            if (
                relative.is_absolute()
                or relative.as_posix() != str(item["path"])
                or relative.parent not in {Path("cards"), Path(".mt/demo/cards")}
                or relative.suffix != ".md"
                or relative.name in {"", ".", ".."}
            ):
                raise ValueError(f"view marker is malformed: {marker_path}")
            seen_paths.add(str(item["path"]))
        return marker

    def policy(self) -> dict:
        stamp = (
            _stat_mtime(self.root / "mt-policy.yaml"),
            _stat_mtime(self.root / "mt-policy.local.yaml"),
            _stat_mtime(self.root / VIEW_MARKER),
        )
        if self._policy_cache is not None and self._policy_stamp == stamp:
            return self._policy_cache
        raw = policy_parser.load_policy(self.root)
        view = {key: value for key, value in raw.items() if key in _DETERMINISTIC_POLICY_KEYS}
        rules = raw.get("rules")
        if isinstance(rules, dict):
            view["rules"] = {key: value for key, value in rules.items() if key in _DETERMINISTIC_RULE_KEYS}
        if self.view_marker() is not None:
            view.setdefault("rules", {})["auto_sync"] = False
        self._policy_stamp = stamp
        self._policy_cache = view
        return view

    def statuses(self) -> list[str]:
        return list(self.policy().get("statuses", {}).keys())

    def open_statuses(self) -> list[str]:
        return [name for name in self.statuses() if name != "done"]

    def pin_demo(self) -> bool:
        """Pin the demo decision so mid-command mutations stay consistent."""
        enabled = self.policy().get("rules", {}).get("demo_workspace", True)
        self._demo_state = bool(
            enabled
            and self.root == self.app_root
            and (self.app_root / "examples").is_dir()
            and self.is_empty()
            and not (self.root / ".mt" / "time-running").exists()
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

    def time_timer_path(self) -> Path:
        if self.demo_active():
            return self.demo_root() / "time-running"
        return self.root / ".mt" / "time-running"

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
