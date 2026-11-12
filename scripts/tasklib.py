#!/usr/bin/env python3
"""Dependency-free task operations for microtask's public CLI and validator."""
from __future__ import annotations

import argparse
import base64
import difflib
import fcntl
import getpass
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time as clock_time
import uuid
from contextlib import contextmanager
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path

import policy_parser
import card_format
import card_references
import drafts as draft_store
import git_ops
from card_format import CLAIM_FIELDS, parse_frontmatter, parse_yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
SHORT_REF_RE = re.compile(r"^@[0-9a-f]{3,8}$", re.IGNORECASE)
REPO_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
VIEW_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
VIEW_MARKER = "mt-view.json"
SYNC_PATHS = (":(glob)cards/*.md", ":(glob)drafts/*.md", "mt-policy.yaml")
FOCUS_REASONS = {
    "due_today": "due today",
    "high_overdue": "overdue and high priority",
    "doing": "already started",
    "due_soon": "deadline in the next 7 days",
    "deadline_review": "needs deadline review",
    "active": "ready to start",
    "inbox": "needs a small restart step",
    "manual": "manually focused",
    "cycle": "cycled focus",
    "default": "time-sensitive",
}
RESTART_COMMANDS = {
    "inbox": "mt read {ref}",
    "active": "mt move {ref} doing",
    "waiting": "mt followup",
    "default": "mt read {ref}",
}
VAGUE_RESTART_PATTERN = r"^(plan|research|work on|continue|figure out)\b"
TIME_DURATION_RE = re.compile(r"(\d+)([smhd])")
_TIME_LOCK_STATE = threading.local()


@dataclass
class MutationReceipt:
    payload: dict[str, object]


def today_str() -> str:
    return date.today().isoformat()


def now_stamp() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def is_date(value: str) -> bool:
    if not DATE_RE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def is_timestamp(value: str) -> bool:
    if not TIMESTAMP_RE.fullmatch(value):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def is_due_value(value: str) -> bool:
    return is_date(value) or is_timestamp(value)


def is_metadata_time_value(value: str) -> bool:
    return is_date(value) or is_timestamp(value)


def open_in_browser(url: str) -> None:
    """Open url in the default browser without blocking the caller."""
    try:
        if "BROWSER" in os.environ:
            import webbrowser

            webbrowser.open(url)
        elif sys.platform == "darwin":
            subprocess.Popen(
                ["/usr/bin/open", url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            import webbrowser

            webbrowser.open(url)
    except OSError:
        print("warning: could not open the browser automatically", file=sys.stderr)


def repo_registry_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return root / "microtask" / "repos.json"


def load_repo_aliases() -> dict[str, str]:
    path = repo_registry_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"repository alias registry is malformed: {path}: {error}") from error
    if not isinstance(raw, dict) or any(
        not isinstance(name, str)
        or not REPO_ALIAS_RE.fullmatch(name)
        or not isinstance(repo, str)
        or not Path(repo).is_absolute()
        for name, repo in raw.items()
    ):
        raise ValueError(f"repository alias registry is malformed: {path}")
    return raw


def write_repo_aliases(aliases: dict[str, str]) -> None:
    path = repo_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, (json.dumps(aliases, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def validate_repo_workspace(path: Path) -> str | None:
    required = (path / "mt", path / "scripts" / "tasklib.py", path / "mt-policy.yaml")
    if not path.is_dir():
        return f"repository workspace does not exist: {path}"
    missing = next((candidate for candidate in required if not candidate.is_file()), None)
    if missing is not None:
        return f"not a microtask workspace; missing {missing.relative_to(path)}: {path}"
    if not os.access(path / "mt", os.X_OK):
        return f"microtask entrypoint is not executable: {path / 'mt'}"
    return None


def path_like_repo_selector(value: str) -> bool:
    return value in {".", ".."} or value.startswith("~") or "/" in value


def resolve_repo_selector(value: str) -> Path:
    if path_like_repo_selector(value):
        path = Path(value).expanduser().resolve()
    else:
        aliases = load_repo_aliases()
        if value not in aliases:
            raise ValueError(
                f"unknown repository alias '{value}'; register it with 'mt repo add {value} PATH'"
            )
        path = Path(aliases[value])
    error = validate_repo_workspace(path)
    if error:
        raise ValueError(error)
    return path


def consume_repo_selector(arguments: list[str]) -> tuple[list[str], str | None]:
    if not arguments:
        return arguments, None
    if arguments[0] in {"-R", "--repo"}:
        if len(arguments) == 1:
            raise ValueError(f"{arguments[0]} requires a repository path or alias")
        return arguments[2:], arguments[1]
    if arguments[0].startswith("--repo="):
        value = arguments[0].partition("=")[2]
        if not value:
            raise ValueError("--repo requires a repository path or alias")
        return arguments[1:], value
    return arguments, None


def due_sort_value(value: object, *, date_only_end: bool = True) -> str:
    if value is None:
        return ""
    raw = str(value)
    if is_date(raw):
        return f"{raw}T23:59:59" if date_only_end else f"{raw}T00:00:00"
    return raw if is_timestamp(raw) else raw


def due_day_value(value: object) -> str | None:
    if value is None:
        return None
    raw = str(value)
    if is_date(raw):
        return raw
    if is_timestamp(raw):
        return raw[:10]
    return raw


def due_kind_value(fm: dict[str, object]) -> object:
    return fm.get("due_kind")


def due_kind_error(value: object, due: object, cfg: dict | None = None) -> str | None:
    if due is None:
        return None if value is None else "due_kind must be null when due is null"
    allowed = (cfg or policy()).get("due_kinds", {})
    if not isinstance(value, str) or not isinstance(allowed, dict) or value not in allowed:
        choices = ", ".join(str(name) for name in allowed) if isinstance(allowed, dict) else ""
        return f"due_kind must be one of {choices or 'the effective policy values'} when due is set"
    return None


def completion_sort_value(task: "Card") -> str:
    return str(task.frontmatter.get("completed") or task.frontmatter.get("updated") or "")


def is_archived(item: "Card") -> bool:
    return item.frontmatter.get("archived") is True


def is_hidden(item: "Card") -> bool:
    return item.frontmatter.get("hidden") is True


def item_state_suffix(item: "Card") -> str:
    states = [name for name, flagged in (("archived", is_archived(item)), ("hidden", is_hidden(item))) if flagged]
    return f" ({', '.join(states)})" if states else ""


DEMO_DATE_ANCHOR = date(2026, 1, 5)


def real_cards_root() -> Path:
    return REPO_ROOT / "cards"


def demo_root() -> Path:
    return REPO_ROOT / ".mt" / "demo"


def workspace_is_empty() -> bool:
    return not any(real_cards_root().glob("*.md"))


_DEMO_STATE: bool | None = None


def pin_demo_state() -> None:
    """Pin the demo decision once per process so mid-command mutations stay consistent."""
    global _DEMO_STATE
    enabled = policy().get("rules", {}).get("demo_workspace", True)
    _DEMO_STATE = bool(enabled and (REPO_ROOT / "examples").is_dir() and workspace_is_empty()
                       and not (REPO_ROOT / ".mt" / "time-running").exists())


def demo_active() -> bool:
    if _DEMO_STATE is None:
        pin_demo_state()
    return bool(_DEMO_STATE)


def rebase_demo_dates(text: str) -> str:
    offset = (date.today() - DEMO_DATE_ANCHOR).days
    if offset == 0:
        return text
    return re.sub(
        r"\b(\d{4})-(\d{2})-(\d{2})(T\d{2}:\d{2}:\d{2})?\b",
        lambda match: (date(int(match.group(1)), int(match.group(2)), int(match.group(3))) + timedelta(days=offset)).isoformat()
        + (match.group(4) or ""),
        text,
    )


def maybe_seed_demo() -> None:
    if not demo_active():
        return
    if (demo_root() / "cards").is_dir():
        return
    examples = REPO_ROOT / "examples"
    for source in sorted(examples.rglob("*.md")):
        relative = source.relative_to(examples)
        destination = demo_root() / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rebase_demo_dates(source.read_text(encoding="utf-8")), encoding="utf-8")


def cards_root() -> Path:
    if demo_active():
        return demo_root() / "cards"
    return real_cards_root()


def unique_item_timestamp(*, real: bool = False) -> str:
    current = datetime.now().replace(microsecond=0)
    while True:
        timestamp = current.strftime("%Y-%m-%d-%H%M%S")
        root = real_cards_root() if real else cards_root()
        if not any(path.name.startswith(f"{timestamp}-") for path in root.glob("*.md")):
            return timestamp
        current += timedelta(seconds=1)


def clean_text_values(values: list[str]) -> list[str]:
    return [value.strip() for value in values if value.strip()]


def clean_comment_values(values: list[str]) -> list[str]:
    return [value.strip() for value in values if value.strip()]


def append_comments(raw: str, comments: list[str], timestamp: str) -> str:
    cleaned = clean_comment_values(comments)
    if not cleaned:
        return raw
    properties, body = card_format.parse_document(raw)
    records = properties.setdefault("comments", [])
    if not isinstance(records, list):
        raise ValueError("comments must be a list")
    records.extend({"at": timestamp, "text": comment} for comment in cleaned)
    return card_format.render_document(properties, body)


def time_log_entries(markdown: str | None) -> list[dict[str, object]]:
    if not markdown:
        return []
    properties, _body = card_format.parse_document(markdown)
    entries = properties.get("time_entries", [])
    if not isinstance(entries, list):
        raise ValueError("time_entries must be a list")
    errors = card_format.validate_records({"time_entries": entries})
    if errors:
        raise ValueError(errors[0])
    return [dict(entry) for entry in entries if isinstance(entry, dict)]


def append_time_entry(raw: str, entry: dict[str, object]) -> str:
    properties, body = card_format.parse_document(raw)
    if not isinstance(entry, dict) or set(entry) != {"session_id", "start", "end", "elapsed_seconds"}:
        raise ValueError("time entry must contain session_id, start, end, and elapsed_seconds")
    records = properties.setdefault("time_entries", [])
    if not isinstance(records, list):
        raise ValueError("time_entries must be a list")
    records.append(dict(entry))
    return card_format.render_document(properties, body)


def blank_line_separator(body: str) -> str:
    if not body:
        return ""
    if re.search(r"(?:\r?\n){2}$", body):
        return ""
    newline = "\r\n" if body.endswith("\r\n") else "\n" if body.endswith("\n") else (
        "\r\n" if "\r\n" in body and "\n" not in body.replace("\r\n", "") else "\n"
    )
    return newline if body.endswith(("\n", "\r")) else newline * 2


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def parse_duration(value: str) -> int:
    position = 0
    total = 0
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    for match in TIME_DURATION_RE.finditer(value):
        if match.start() != position:
            break
        total += int(match.group(1)) * units[match.group(2)]
        position = match.end()
    if position != len(value) or total < 1:
        raise ValueError("duration must use positive units such as 45s, 90m, or 1h30m")
    return total


def append_completion_note(raw: str, notes: list[str], today: str) -> str:
    cleaned = clean_text_values(notes)
    if not cleaned:
        return raw
    properties, body = card_format.parse_document(raw)
    comments = properties.setdefault("comments", [])
    if not isinstance(comments, list):
        raise ValueError("comments must be a list")
    comments.extend({"at": today, "text": note} for note in cleaned)
    return card_format.render_document(properties, body)


@dataclass(frozen=True)
class Card:
    """One task or note card; ``kind`` decides whether lifecycle fields apply."""

    path: Path
    raw: str
    frontmatter_text: str
    body: str
    frontmatter: dict[str, object]

    @classmethod
    def read(cls, path: Path) -> "Card":
        with path.open(encoding="utf-8", newline="") as source_file:
            raw = source_file.read()
        frontmatter_text, body = parse_frontmatter(raw)
        if frontmatter_text is None:
            raise ValueError(f"{path}: missing YAML frontmatter")
        properties = parse_yaml(frontmatter_text)
        missing = {"title", "kind"} - set(properties)
        if missing:
            raise ValueError(f"{path}: missing {', '.join(sorted(missing))} properties")
        card_format.require_valid_properties(properties)
        return cls(path, raw, frontmatter_text, body, properties)

    @property
    def title(self) -> str:
        title = self.frontmatter.get("title")
        return title if isinstance(title, str) else self.path.stem

    @property
    def kind(self) -> str:
        return str(self.frontmatter.get("kind"))

    @property
    def is_task(self) -> bool:
        return self.kind == "task"

    @property
    def is_note(self) -> bool:
        return self.kind == "note"

    @property
    def next_action(self) -> str:
        value = self.frontmatter.get("next_action") if self.is_task else None
        return value if isinstance(value, str) else ""

    @property
    def short_ref(self) -> str:
        return hashlib.sha256(self.path.stem.encode()).hexdigest()[:8]




def policy() -> dict:
    raw = policy_parser.load_policy()
    deterministic_keys = {
        "statuses", "priorities", "types", "due_kinds", "suggested_topics", "suggested_tags",
        "default_status", "default_priority", "default_type", "default_username", "rules", "view_labels",
    }
    view = {key: value for key, value in raw.items() if key in deterministic_keys}
    rules = raw.get("rules")
    if isinstance(rules, dict):
        deterministic_rule_keys = {
            "auto_sync", "list_done_limit", "max_doing_tasks", "demo_workspace", "timer_warn_hours",
            "show_year_progress", "show_deadline_progress", "deadline_progress_start",
            "allow_public_origin",
        }
        view["rules"] = {key: value for key, value in rules.items() if key in deterministic_rule_keys}
    if view_marker() is not None:
        view.setdefault("rules", {})["auto_sync"] = False
    return view


def view_marker() -> dict[str, object] | None:
    """Read and validate the provenance marker for a materialized view."""
    marker_path = REPO_ROOT / VIEW_MARKER
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
        or not is_timestamp(str(marker["created"]))
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


def statuses() -> list[str]:
    return list(policy().get("statuses", {}).keys())


def open_statuses() -> list[str]:
    return [name for name in statuses() if name != "done"]


def focus_state_path() -> Path:
    if demo_active():
        return demo_root() / "focus"
    return REPO_ROOT / ".mt" / "focus"


@dataclass(frozen=True)
class FocusState:
    task_id: str
    mode: str


def read_focus_state() -> FocusState | None:
    path = focus_state_path()
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return FocusState(raw, "manual")
    if not isinstance(data, dict):
        return None
    task_id = data.get("task_id")
    mode = data.get("mode")
    if not isinstance(task_id, str) or not task_id.strip():
        return None
    if mode not in {"manual", "cycle"}:
        return None
    return FocusState(task_id.strip(), str(mode))


def read_focus_task_id() -> str | None:
    state = read_focus_state()
    return state.task_id if state else None


def write_focus_task_id(task_id: str) -> None:
    write_focus_state(task_id, "manual")


def write_focus_state(task_id: str, mode: str) -> None:
    path = focus_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"task_id": task_id, "mode": mode}, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )


def clear_focus_task_id() -> None:
    try:
        focus_state_path().unlink()
    except FileNotFoundError:
        pass


def time_timer_path() -> Path:
    if demo_active():
        return demo_root() / "time-running"
    return REPO_ROOT / ".mt" / "time-running"


@contextmanager
def time_mutation_lock():
    """Serialize task and timer mutations across CLI and web processes."""
    lock_path = REPO_ROOT / ".mt" / "time-mutation.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        _TIME_LOCK_STATE.held = True
        try:
            yield
        finally:
            _TIME_LOCK_STATE.held = False
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def sync_serialization_lock():
    """Serialize Git sync operations without blocking task edits during network waits."""
    lock_path = REPO_ROOT / ".mt" / "sync.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def serialized_time_mutation(function):
    @wraps(function)
    def locked(*args, **kwargs):
        _TIME_LOCK_STATE.sync_requested = False
        with time_mutation_lock():
            result = function(*args, **kwargs)
            sync_requested = _TIME_LOCK_STATE.sync_requested
        _TIME_LOCK_STATE.sync_requested = False
        if isinstance(result, MutationReceipt):
            if sync_requested:
                sync_error = run_auto_sync_for_receipt()
                if sync_error:
                    result.payload["sync_error"] = sync_error
            print(json.dumps(result.payload, ensure_ascii=False))
            return 0
        if sync_requested:
            sync_result = _run_auto_sync()
            return result or sync_result
        return result

    return locked


def local_stamp(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds).replace(microsecond=0).isoformat()


def parse_time_argument(value: str) -> int:
    """Accept a naive local ISO timestamp or today's HH:MM[:SS]."""
    try:
        if re.fullmatch(r"\d{2}:\d{2}(:\d{2})?", value):
            value = local_stamp(int(clock_time.time()))[:10] + "T" + value
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None or "T" not in value and " " not in value:
            raise ValueError()
        return int(parsed.timestamp())
    except (ValueError, OverflowError, OSError):
        raise ValueError("TIME must be a local YYYY-MM-DDTHH:MM[:SS] or today's HH:MM[:SS] (without offset)") from None


def time_entry_from_timer(timer: dict[str, object], end_epoch: int) -> dict[str, object]:
    start_epoch = int(timer["start_epoch"])
    elapsed = max(0, end_epoch - start_epoch)
    return {
        "session_id": str(timer["session_id"]),
        "start": str(timer["start"]),
        "end": local_stamp(end_epoch),
        "elapsed_seconds": elapsed,
    }


def read_time_timers() -> dict[str, dict[str, object]]:
    if not getattr(_TIME_LOCK_STATE, "held", False):
        with time_mutation_lock():
            return read_time_timers()
    path = time_timer_path()
    try:
        timers = _read_time_timers()
    except (ValueError, UnicodeError):
        recovery = REPO_ROOT / ".mt" / "recovery" / f"time-running-{uuid.uuid4().hex}.json"
        recovery.parent.mkdir(parents=True, exist_ok=True)
        os.replace(path, recovery)
        print(f"warning: malformed running timer state moved to {recovery}; continuing without timers", file=sys.stderr)
        return {}
    migrated = False
    for task_id, timer in timers.items():
        if "by" not in timer:
            task = task_by_id(task_id)
            timer["by"] = (task.frontmatter.get("claimed_by") if task else None) or current_username()
            migrated = True
    if migrated:
        write_time_timers(timers)
    return timers


def _read_time_timers() -> dict[str, dict[str, object]]:
    path = time_timer_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"running timer state is malformed: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"running timer state is malformed: {path}")
    timers: dict[str, dict[str, object]] = {}
    for task_id, timer in value.items():
        if not isinstance(timer, dict):
            raise ValueError(f"running timer state is malformed: {path}")
        session_id = timer.get("session_id")
        start = timer.get("start")
        start_epoch = timer.get("start_epoch")
        if (
            not isinstance(task_id, str)
            or not task_id.strip()
            or not isinstance(session_id, str)
            or not re.fullmatch(r"[0-9a-f]{32}", session_id)
            or not isinstance(start, str)
            or not is_timestamp(start)
            or not isinstance(start_epoch, int)
            or isinstance(start_epoch, bool)
        ):
            raise ValueError(f"running timer state is malformed: {path}")
        owner = timer.get("by")
        if "by" in timer and (not isinstance(owner, str) or not owner.strip()
                              or any(char in owner for char in "\r\n\x00")):
            raise ValueError(f"running timer state is malformed: {path}")
        timers[task_id] = timer
    return timers


def write_time_timers(timers: dict[str, dict[str, object]]) -> None:
    path = time_timer_path()
    if not timers:
        path.unlink(missing_ok=True)
        return
    atomic_write(path, (json.dumps(timers, indent=2) + "\n").encode("utf-8"))


def clear_time_timer(task_id: str, session_id: str) -> bool:
    timers = read_time_timers()
    timer = timers.get(task_id)
    if timer is None or timer.get("session_id") != session_id:
        return False
    del timers[task_id]
    write_time_timers(timers)
    return True


def finish_lifecycle_timer(task: Card, raw: str, end_epoch: int) -> tuple[str, dict | None]:
    """Stage a session in the same card transaction as its lifecycle change."""
    task_id = str(task.frontmatter.get("id") or task.path.stem)
    timer = read_time_timers().get(task_id)
    if timer:
        entry, already_saved = time_session_for(task_id, timer, end_epoch)
        if not already_saved:
            raw = append_time_entry(raw, entry)
    return raw, timer


def cleanup_lifecycle_timer(task: Card, timer: dict | None) -> None:
    if timer:
        try:
            clear_time_timer(str(task.frontmatter.get("id") or task.path.stem), str(timer["session_id"]))
        except OSError as error:
            print(f"warning: session saved, but timer cleanup failed: {error}; retry time stop safely", file=sys.stderr)


def task_by_id(task_id: str) -> Card | None:
    return next(
        (task for task in discover("task") if str(task.frontmatter.get("id") or task.path.stem) == task_id),
        None,
    )


def timer_session_saved(task_id: str, timer: dict[str, object]) -> bool:
    task = task_by_id(task_id)
    if task is None:
        return False
    return any(
        entry["session_id"] == timer["session_id"]
        for entry in time_log_entries(task.raw)
    )


def time_session_for(task_id: str, timer: dict[str, object], end_epoch: int) -> tuple[dict[str, object], bool]:
    """Return one structured session and whether the task already has its ID."""
    task = task_by_id(task_id)
    if task is None:
        raise ValueError(f"running timer task no longer exists: {task_id}")
    session_id = str(timer["session_id"])
    if any(entry["session_id"] == session_id for entry in time_log_entries(task.raw)):
        return {}, True
    return time_entry_from_timer(timer, end_epoch), False


def save_time_session(task: Card, timer: dict[str, object], end_epoch: int) -> tuple[bool, str]:
    """Append one session; restore the card and retain timer state on validation failure."""
    task_id = str(task.frontmatter.get("id") or task.path.stem)
    entry, already_saved = time_session_for(task_id, timer, end_epoch)
    if already_saved:
        return True, "already saved"
    original = task.path.read_bytes()
    updated = append_time_entry(task.raw, entry)
    updated = replace_frontmatter_field(updated, "updated", now_stamp())
    base = demo_root() if demo_active() else REPO_ROOT
    relative = str(task.path.relative_to(base))
    try:
        apply_item_transaction({relative: updated.encode("utf-8")}, expected={relative: original}, source_root=base)
    except (OSError, ValueError):
        return False, "time log failed validation; running timer was retained"
    return True, "saved"


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def managed_temp_directory(prefix: str) -> tempfile.TemporaryDirectory[str]:
    root = REPO_ROOT / ".mt" / "tmp"
    root.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix=prefix, dir=root)


def editor_recovery_directory() -> Path:
    path = REPO_ROOT / ".mt" / "recovery" / "edit"
    path.mkdir(parents=True, exist_ok=True)
    return path


def create_editor_recovery_copy(
    item_id: str, contents: bytes, *, metadata: dict[str, object] | None = None,
) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_-]+", "-", item_id).strip("-") or "item"
    path = editor_recovery_directory() / f"{safe_id}-edit-{uuid.uuid4().hex}.md"
    atomic_write(path, contents)
    if metadata is not None:
        sidecar = path.with_suffix(".meta.json")
        atomic_write(sidecar, (json.dumps({"version": 1, **metadata}, ensure_ascii=False) + "\n").encode("utf-8"))
    return path


def saved_time_seconds(task: Card) -> int:
    return sum(int(entry["elapsed_seconds"]) for entry in time_log_entries(task.raw))


def focus_sort_key(task: Card, rank: dict[str, int]) -> tuple[int, str, str]:
    priority = str(task.frontmatter.get("priority") or "medium")
    due = due_sort_value(task.frontmatter.get("due")) or "9999-12-31T23:59:59"
    return rank.get(priority, len(rank)), due, task.path.name


def is_high_priority_task(task: Card) -> bool:
    priority = str(task.frontmatter.get("priority") or "medium")
    return priority in {"urgent", "high"}


def has_deadline_due(task: Card) -> bool:
    return due_kind_value(task.frontmatter) == "deadline"


def ranked_focus_candidates(tasks: list["Task"], rank: dict[str, int]) -> list[tuple["Task", str]]:
    """Rank actionable tasks; deadline alerts cover blocked and deferred work."""
    today = date.today().isoformat()
    current = now_stamp()
    soon = (date.today() + timedelta(days=7)).isoformat()

    def due_value(task: Card) -> str | None:
        return due_sort_value(task.frontmatter.get("due")) or None

    def due_day(task: Card) -> str | None:
        return due_day_value(task.frontmatter.get("due"))

    groups = (
        (
            (
                task for task in tasks
                if has_deadline_due(task) and due_day(task) == today and (due_value(task) or "") >= current
            ),
            "due_today",
        ),
        (
            (
                task for task in tasks
                if has_deadline_due(task)
                and (due_value(task) or "9999-12-31T23:59:59") < current
                and is_high_priority_task(task)
            ),
            "high_overdue",
        ),
        ((task for task in tasks if task.frontmatter.get("status") == "doing"), "doing"),
        (
            (task for task in tasks if has_deadline_due(task)
             and today < (due_day(task) or "") <= soon),
            "due_soon",
        ),
        (
            (
                task for task in tasks
                if has_deadline_due(task)
                and (due_value(task) or "9999-12-31T23:59:59") < current
                and not is_high_priority_task(task)
            ),
            "deadline_review",
        ),
        ((task for task in tasks if task.frontmatter.get("status") == "active"), "active"),
        ((task for task in tasks if task.frontmatter.get("status") == "inbox"), "inbox"),
    )
    ordered: list[tuple[Card, str]] = []
    seen: set[str] = set()
    for candidates, reason in groups:
        def key(item: Card) -> tuple:
            order = focus_sort_key(item, rank)
            return (due_value(item) or "", *order) if reason in {"due_today", "due_soon"} else order

        for task in sorted(candidates, key=key):
            task_id = str(task.frontmatter.get("id") or task.path.stem)
            if task_id in seen:
                continue
            seen.add(task_id)
            ordered.append((task, reason))
    return ordered


def task_display_sort_key(task: "Card", rank: dict[str, int]) -> tuple[int, int, str, str]:
    due = due_sort_value(task.frontmatter.get("due"))
    due_group = 0 if due and due[:10] <= date.today().isoformat() else 1 if due else 2
    priority, normalized_due, _ = focus_sort_key(task, rank)
    return due_group, priority, normalized_due, str(task.path)


def followup_sort_key(task: Card, rank: dict[str, int]) -> tuple[int, str, int, str]:
    due = due_sort_value(task.frontmatter.get("due"))
    priority = str(task.frontmatter.get("priority") or "medium")
    due_group = 0 if due and due <= now_stamp() else 1 if due else 2
    return due_group, due or "9999-12-31T23:59:59", rank.get(priority, len(rank)), task.path.name


def focus_context(tasks: list["Task"], *, actor: str | None = None) -> dict:
    """One read-only focus snapshot for the CLI, dashboard, and skills.

    Callers select visibility and workspace filters before calling this function.
    Saved selection never suppresses alerts or changes the candidate order.
    """
    actor = current_username(actor)
    rank = {name: index for index, name in enumerate(policy().get("priorities", {}))}
    tasks = [task for task in tasks if task.frontmatter.get("status") != "done"]
    available = [task for task in tasks
                 if task.frontmatter.get("status") in {"active", "doing", "inbox"}
                 and task.frontmatter.get("claimed_by") in {None, "", actor}]

    def record(task: Card, reason_key: str) -> dict:
        return {**item_json_record(task), "reason_key": reason_key,
                "reason": FOCUS_REASONS[reason_key]}

    candidates = [record(task, reason) for task, reason in ranked_focus_candidates(available, rank)]
    state = read_focus_state()
    selected = next((item for item in candidates if state and item["id"] == state.task_id), None)
    mode = state.mode if selected and state else "auto"
    if selected:
        selected = {**selected, "reason_key": mode, "reason": FOCUS_REASONS[mode]}
    elif candidates:
        selected = candidates[0]

    cutoff = (date.today() + timedelta(days=7)).isoformat()
    alerts = []
    deadline_order = lambda item: (due_sort_value(item.frontmatter.get("due")) or "9999",
                                   *focus_sort_key(item, rank))
    for task in sorted(tasks, key=deadline_order):
        due = due_day_value(task.frontmatter.get("due"))
        if not has_deadline_due(task) or not due or due > cutoff:
            continue
        owner = task.frontmatter.get("claimed_by")
        status = task.frontmatter.get("status")
        if owner and owner != actor:
            action = "coordinate"
        elif status == "waiting":
            action = "followup"
        elif status not in {"active", "doing", "inbox"}:
            action = "review"
        else:
            action = "work"
        action_label = {"coordinate": "Check with the owner", "followup": "Follow up",
                        "review": "Review the commitment", "work": "Allow time for this"}[action]
        alerts.append({**item_json_record(task), "action": action, "action_label": action_label})
    return {"selected": selected, "mode": mode, "actor": actor,
            "generated_at": now_stamp(), "candidates": candidates, "alerts": alerts,
            "followup_ids": [str(task.frontmatter.get("id") or task.path.stem)
                             for task in sorted(tasks, key=lambda item: followup_sort_key(item, rank))
                             if task.frontmatter.get("status") == "waiting"]}


def next_action_needs_restart_step(item: Card) -> bool:
    value = str(item.next_action or "").strip().casefold()
    return bool(value) and (value == "tbd" or re.search(VAGUE_RESTART_PATTERN, value) is not None)


KIND_LINE_RE = re.compile(r"""^kind:\s*["']?(task|note)["']?\s*$""")


def card_kind_hint(path: Path) -> str | None:
    """Cheaply guess a card's kind from its frontmatter header; None when unclear."""
    try:
        with path.open(encoding="utf-8", newline="") as source_file:
            if source_file.readline().rstrip("\r\n") != "---":
                return None
            for line in source_file:
                line = line.rstrip("\r\n")
                if line == "---":
                    return None
                match = KIND_LINE_RE.match(line)
                if match:
                    return match.group(1)
    except (OSError, UnicodeDecodeError):
        return None
    return None


def read_cards(root: Path, kind: str | None = None) -> list[Card]:
    """Read and validate the cards in ``root``; the kind hint only skips files that cannot match ``kind``."""
    if not root.is_dir():
        return []
    allowed = set(statuses())
    cards = []
    for path in sorted(root.glob("*.md")):
        hint = card_kind_hint(path)
        if kind is not None and hint is not None and hint != kind:
            continue
        card = Card.read(path)
        if kind is not None and card.kind != kind:
            continue
        status = card.frontmatter.get("status")
        if card.is_task and (not isinstance(status, str) or status not in allowed):
            raise ValueError(
                f"{card.path}: invalid or missing task status {status!r}; "
                "custom status IDs must be defined in mt-policy.yaml"
            )
        cards.append(card)
    return cards


def in_selection(card: Card, kind: str | None, statuses: list[str] | None) -> bool:
    """``statuses`` keeps only tasks with those statuses, so it excludes notes."""
    if kind is not None and card.kind != kind:
        return False
    return statuses is None or (card.is_task and card.frontmatter.get("status") in statuses)


def discover(kind: str | None = None, statuses: list[str] | None = None) -> list[Card]:
    """Cards in the active workspace, optionally limited to a kind and/or task statuses."""
    if statuses is not None and kind is None:
        kind = "task"
    return [card for card in read_cards(cards_root(), kind) if in_selection(card, kind, statuses)]


class ReferenceIndex:
    """Resolve short refs against one unfiltered workspace snapshot."""

    def __init__(self, items: list[Card]):
        self.prefixes: dict[str, list[Card]] = {}
        for item in items:
            short_ref = item.short_ref
            for length in range(3, 9):
                self.prefixes.setdefault(short_ref[:length], []).append(item)

    def resolve(self, ref: str) -> dict[str, object]:
        matches = self.prefixes.get(ref[1:].casefold(), [])
        result: dict[str, object] = {"ref": ref, "state": "missing"}
        if len(matches) > 1:
            result["state"] = "ambiguous"
        elif matches:
            target = matches[0]
            result.update(state="resolved", target={
                "id": str(target.frontmatter.get("id") or target.path.stem),
                "ref": f"@{target.short_ref}", "title": target.title, "kind": target.kind,
            })
        return result

    def occurrences(self, text: str) -> list[dict[str, object]]:
        return [{"start": start, "end": end, **self.resolve(text[start:end])}
                for start, end in card_references.reference_spans(text)]


def item_references(item: Card, index: ReferenceIndex) -> list[dict[str, object]]:
    values = [item.body, str(item.next_action or ""),
              str(item.frontmatter.get("processing_instructions") or "")]
    comments = item.frontmatter.get("comments", [])
    if isinstance(comments, list):
        values.extend(str(comment.get("text") or "") for comment in comments if isinstance(comment, dict))
    text = "\n\n".join(values)
    return [index.resolve(ref) for ref in card_references.extract_references(text)]


def print_item_references(item: Card, index: ReferenceIndex) -> None:
    targets: dict[str, dict] = {}
    for reference in item_references(item, index):
        if reference["state"] == "resolved":
            target = reference["target"]
            targets.setdefault(target["ref"], target)
    if targets:
        print("       referenced:")
        for target in list(targets.values())[:3]:
            print(f"         {target['ref']}  {target['title']}")
        if len(targets) > 3:
            print(f"         … +{len(targets) - 3} more")


def item_json_record(item: Card, reference_index: ReferenceIndex | None = None) -> dict[str, object]:
    """Serialize one discovered item for CLI composition commands."""
    fields = (
        "id", "status", "priority", "type", "project", "topic",
        "due", "due_kind", "created", "updated", "completed", "archived", "hidden", "tags",
        *CLAIM_FIELDS,
    )
    record = {field: item.frontmatter.get(field) for field in fields}
    record["due_kind"] = due_kind_value(item.frontmatter)
    record["hidden"] = is_hidden(item)
    record["needs_review"] = item.frontmatter.get("needs_review") is True
    record.update(
        {
            "kind": item.kind,
            "ref": f"@{item.short_ref}",
            "title": item.title,
            "next_action": item.next_action,
            "path": str(item.path.relative_to(REPO_ROOT)),
            "content_hash": hashlib.sha256(item.raw.encode("utf-8")).hexdigest(),
        }
    )
    if reference_index is not None:
        record["references"] = item_references(item, reference_index)
    return record


def item_compact_json_record(item: Card) -> dict[str, object]:
    """Serialize one item as a compact minimal-field record for fast context searches."""
    record: dict[str, object] = {
        "ref": f"@{item.short_ref}",
        "id": item.frontmatter.get("id") or item.path.stem,
        "kind": item.kind,
        "title": item.title,
        "status": item.frontmatter.get("status") if item.is_task else None,
        "priority": item.frontmatter.get("priority"),
        "project": item.frontmatter.get("project"),
        "topic": item.frontmatter.get("topic"),
        "due": item.frontmatter.get("due"),
        "tags": list(item.frontmatter.get("tags") or []),
    }
    if item.is_task and item.frontmatter.get("type"):
        record["type"] = item.frontmatter.get("type")
    if item.frontmatter.get("due"):
        record["due_kind"] = due_kind_value(item.frontmatter)
    if item.frontmatter.get("needs_review") is True:
        record["needs_review"] = True
    if item.frontmatter.get("archived") is True:
        record["archived"] = True
    if is_hidden(item):
        record["hidden"] = True
    return record


def run_validator(repo_root: Path = REPO_ROOT) -> bool:
    command = [str(repo_root / "scripts" / "validate")]
    if repo_root == REPO_ROOT and demo_active():
        command.extend(["--data-root", str(demo_root())])
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        if result.stdout:
            print(result.stdout, end="", file=sys.stderr)
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
    return result.returncode == 0


def validate_data_root(data_root: Path, *, changes: dict[str, bytes] | None = None,
                       deletions: set[str] | None = None,
                       previous: dict[str, bytes | None] | None = None) -> None:
    """Validate a full tree, or verify and validate the files just committed."""
    if changes is not None:
        for relative, expected in changes.items():
            if (data_root / relative).read_bytes() != expected:
                raise ValueError(f"item changed during transaction: {relative}")
        for relative in set(deletions or ()) - set(changes):
            if (data_root / relative).exists():
                raise ValueError(f"item reappeared during transaction: {relative}")
        validate_item_changes(changes, deletions=deletions, source_root=data_root, previous=previous)
        return
    result = subprocess.run(
        [str(REPO_ROOT / "scripts" / "validate"), "--data-root", str(data_root)],
        cwd=REPO_ROOT, check=False, capture_output=True, text=True,
    )
    if result.returncode:
        detail = (result.stdout + result.stderr).strip()
        raise ValueError(detail or "item transaction failed validation")


def apply_item_transaction(
    changes: dict[str, bytes],
    *,
    deletions: set[str] | None = None,
    source_root: Path | None = None,
    expected: dict[str, bytes | None] | None = None,
) -> None:
    """Validate, atomically apply, and conditionally roll back item-file changes.

    Callers serialize mutations with ``time_mutation_lock``. ``expected`` is a
    compare-and-swap guard: each path must contain its expected bytes, or be
    absent when the value is ``None``.
    """
    deletions = set(deletions or ())
    base = source_root or (demo_root() if demo_active() else REPO_ROOT)
    affected = set(changes) | deletions
    original: dict[str, bytes | None] = {}
    for relative in affected:
        path = base / relative
        original[relative] = path.read_bytes() if path.is_file() else None
    if expected is not None:
        for relative, value in expected.items():
            path = base / relative
            current = original[relative] if relative in original else (path.read_bytes() if path.is_file() else None)
            if current != value:
                raise ValueError(f"item changed before transaction: {relative}")
    validate_item_changes(changes, deletions=deletions, source_root=base, previous=original)
    try:
        for relative, before in original.items():
            path = base / relative
            current = path.read_bytes() if path.is_file() else None
            if current != before:
                raise ValueError(f"item changed before transaction: {relative}")
        for relative in deletions:
            path = base / relative
            if path.exists():
                path.unlink()
        for relative, content in changes.items():
            path = base / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, content)
        validate_data_root(base, changes=changes, deletions=deletions, previous=original)
    except Exception:
        for relative in reversed(sorted(affected)):
            path = base / relative
            before = original[relative]
            try:
                current = path.read_bytes() if path.is_file() else None
                expected_after = changes.get(relative, None)
                if relative in deletions and relative not in changes:
                    expected_after = None
                # Do not overwrite an unexpected concurrent edit while rolling back.
                if current != expected_after and current != before:
                    continue
                if before is None:
                    path.unlink(missing_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write(path, before)
            except OSError:
                pass
        raise


def item_file_snapshot(repo_root: Path = REPO_ROOT) -> dict[str, bytes]:
    root = cards_root()
    base = demo_root() if demo_active() else repo_root
    return {
        str(path.relative_to(base)): path.read_bytes()
        for path in sorted(root.glob("*.md"))
    } if root.is_dir() else {}


def item_files_changed(before: dict[str, bytes]) -> bool:
    pin_demo_state()
    return before != item_file_snapshot()


def auto_sync_after_mutation(*, changed: bool = True) -> int:
    if not changed or not policy().get("rules", {}).get("auto_sync", False):
        return 0
    if getattr(_TIME_LOCK_STATE, "held", False):
        _TIME_LOCK_STATE.sync_requested = True
        return 0
    return _run_auto_sync()


def git_setup_message(status: git_ops.WorkspaceGitStatus, operation: str) -> str:
    if status.state == "missing":
        return (
            f"{operation} requires Git; install Git, then run 'git init' in {REPO_ROOT} "
            "to initialize this workspace"
        )
    if status.state == "uninitialized":
        return f"{operation} requires this workspace to be a Git repository; run 'git init' in {REPO_ROOT}"
    return f"{operation} could not inspect Git: {status.detail}"


def auto_sync_unavailable_message(status: git_ops.WorkspaceGitStatus) -> str:
    if status.state == "missing":
        reason = f"Git is not installed or not on PATH; install Git, then run 'git init' in {REPO_ROOT}"
    else:
        reason = f"this workspace is not initialized for Git ({status.detail})"
    return f"Git sync is unavailable: {reason}; changes remain local."


def _run_auto_sync() -> int:
    status = git_ops.workspace_status(REPO_ROOT)
    if status.state in {"missing", "uninitialized"}:
        print(auto_sync_unavailable_message(status))
        return 0
    print("Auto-syncing task changes...")
    result = command_sync(argparse.Namespace(message=None))
    if result != 0:
        print(
            "error: local change succeeded, but auto-sync failed; local changes were preserved",
            file=sys.stderr,
        )
    return result


def run_auto_sync_for_receipt() -> str | None:
    status = git_ops.workspace_status(REPO_ROOT)
    if status.state in {"missing", "uninitialized"}:
        return auto_sync_unavailable_message(status)
    output = io.StringIO()
    errors = io.StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        result = _run_auto_sync()
    if result == 0:
        return None
    detail = (errors.getvalue() + output.getvalue()).strip()
    return detail or "automatic sync failed"


def render_item_document(
    *,
    item_mode: str,
    item_id: str,
    title: str,
    priority: object,
    project: object,
    topic: object,
    due: object,
    due_kind: object,
    created: object,
    updated: object,
    tags: object,
    body_text: str,
    archived: object = False,
    hidden: object = False,
    needs_review: object = False,
    github_source: object = None,
    github_source_hash: object = None,
    capture_id: str | None = None,
    status: object | None = None,
    task_type: object | None = None,
    next_action: str | None = None,
    processing_instructions: str = "",
) -> str:
    if not isinstance(tags, list):
        raise ValueError("tags must be a list")
    properties: dict[str, object] = {
        "id": item_id, "title": title.strip(), "kind": item_mode,
        "priority": priority, "created": str(created), "updated": str(updated),
    }
    if capture_id:
        properties["capture_id"] = capture_id
    if github_source is not None:
        properties["github_source"] = github_source
    if github_source_hash is not None:
        properties["github_source_hash"] = github_source_hash
    for field, value in (("project", project), ("topic", topic), ("due", due), ("due_kind", due_kind)):
        if value is not None:
            properties[field] = value
    for field, value in (("archived", archived), ("hidden", hidden), ("needs_review", needs_review)):
        if value:
            properties[field] = True
    if tags:
        properties["tags"] = tags
    if item_mode == "task":
        if status is None or task_type is None:
            raise ValueError("task status and type are required")
        properties.update(status=status, type=task_type)
        if next_action:
            properties["next_action"] = next_action
    if processing_instructions:
        properties["processing_instructions"] = processing_instructions
    if item_mode == "note" and (status is not None or task_type is not None or next_action):
        raise ValueError("task-only properties cannot be set on a note")
    if github_source is not None or github_source_hash is not None:
        if github_source is None or github_source_hash is None:
            raise ValueError("github_source and github_source_hash must be set together")
        properties["github_source"] = github_source
        properties["github_source_hash"] = github_source_hash
    if capture_id is not None:
        properties["capture_id"] = capture_id
    return card_format.render_document(properties, body_text, expected_kind=item_mode)


def git_text(*arguments: str, capture_output: bool = True) -> subprocess.CompletedProcess[str]:
    """Compatibility entry point for Git calls in the active workspace."""
    return git_ops.run_git(REPO_ROOT, *arguments, capture_output=capture_output)


def git_task_text(revision: str, path: str) -> str | None:
    result = git_text("show", f"{revision}:{path}")
    return result.stdout if result.returncode == 0 else None


def staged_card(revision: str, path: str) -> tuple[str, str, str]:
    """Title, status and kind of a card as committed (HEAD) or staged (""), tolerating unreadable text."""
    raw = git_task_text(revision, path)
    frontmatter, _body = parse_frontmatter(raw) if raw else (None, "")
    properties = parse_yaml(frontmatter) if frontmatter is not None else {}
    title = properties.get("title")
    return (title if isinstance(title, str) and title.strip() else Path(path).stem,
            str(properties.get("status") or ""), "note" if properties.get("kind") == "note" else "task")


def staged_task_changes() -> list[tuple[str, str, str | None]]:
    result = git_text(
        "diff",
        "--cached",
        "--name-status",
        "--find-renames",
        "--",
        *SYNC_PATHS,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "could not inspect task changes")
    changes: list[tuple[str, str, str | None]] = []
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        code = fields[0][0]
        if code == "R" and len(fields) == 3:
            changes.append((code, fields[1], fields[2]))
        elif code in {"A", "M", "D"} and len(fields) == 2:
            changes.append((code, fields[1], None))
        else:
            raise RuntimeError(f"unsupported task change: {line}")
    return changes


def task_commit_message(changes: list[tuple[str, str, str | None]]) -> str:
    markdown_changes = [change for change in changes if Path(change[1]).suffix == ".md"]
    drafts = [change for change in markdown_changes if change[1].startswith("drafts/")]
    cards = [change for change in markdown_changes if not change[1].startswith("drafts/")]
    if any(old_path == "mt-policy.yaml" for _, old_path, _ in changes):
        message = "Update tasks and policy" if cards else "Update workspace policy"
        if drafts:
            message += f" and {len(drafts)} draft{'s' if len(drafts) != 1 else ''}"
        return message
    if not cards and drafts:
        return f"Update {len(drafts)} workspace draft{'s' if len(drafts) != 1 else ''}"
    if not cards:
        return "Update workspace"
    actions: list[tuple[str, str, str | None, str]] = []  # verb, title, destination status, kind
    for code, old_path, new_path in cards:
        if code == "A":
            title, _status, kind = staged_card("", old_path)
            actions.append(("Add", title, None, kind))
        elif code == "D":
            title, _status, kind = staged_card("HEAD", old_path)
            actions.append(("Delete", title, None, kind))
        else:
            _title, before, _kind = staged_card("HEAD", old_path)
            title, after, kind = staged_card("", new_path or old_path)
            if before and after and before != after:
                actions.append(("Complete" if after == "done" else "Move", title, after, kind))
            else:
                actions.append(("Update", title, None, kind))
    drafts_suffix = f" and {len(drafts)} draft{'s' if len(drafts) != 1 else ''}" if drafts else ""
    if len(actions) == 1:
        verb, title, destination, kind = actions[0]
        if verb == "Move":
            return f"Move task {title} to {destination}{drafts_suffix}"
        return f"{verb} {kind} {title}{drafts_suffix}"
    kinds = {kind for *_rest, kind in actions}
    noun = "notes" if kinds == {"note"} else "tasks" if kinds == {"task"} else "items"
    if len({verb for verb, *_rest in actions}) == 1:
        return f"{actions[0][0]} {len(actions)} {noun}{drafts_suffix}"
    return f"Update {len(actions)} {noun}{drafts_suffix}"


def command_sync(args: argparse.Namespace) -> int:
    with sync_serialization_lock():
        return _command_sync_serialized(args)


def _command_sync_serialized(args: argparse.Namespace) -> int:
    with time_mutation_lock():
        git_status = git_ops.workspace_status(REPO_ROOT)
        if git_status.state != "ready":
            print(f"error: {git_setup_message(git_status, 'sync')}", file=sys.stderr)
            return 1
        if not run_validator():
            return 1
        if git_text("remote", "get-url", "origin").returncode != 0:
            print("error: sync requires an 'origin' remote", file=sys.stderr)
            return 1
        problem = git_ops.origin_publication_problem(
            REPO_ROOT, allow_public=policy().get("rules", {}).get("allow_public_origin") is True
        )
        if problem:
            print(f"error: refusing to sync: {problem}", file=sys.stderr)
            return 1
        branch = git_text("rev-parse", "--abbrev-ref", "HEAD")
        if branch.returncode != 0 or branch.stdout.strip() == "HEAD":
            print("error: sync requires a named current branch", file=sys.stderr)
            return 1
        branch_name = branch.stdout.strip()
        listed = git_text(
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            *SYNC_PATHS,
        )
        if listed.returncode != 0:
            detail = listed.stderr.strip() or listed.stdout.strip()
            print(f"error: could not list task files: {detail}", file=sys.stderr)
            return 1
        task_paths = sorted(set(listed.stdout.splitlines()))
        if task_paths:
            stage = git_text("add", "-A", "--", *task_paths)
            if stage.returncode != 0:
                detail = stage.stderr.strip() or stage.stdout.strip()
                print(f"error: git add failed: {detail}", file=sys.stderr)
                return 1
        try:
            changes = staged_task_changes()
        except RuntimeError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        committed = False
        if not changes:
            print("No local task changes to commit.")
        else:
            message = args.message.strip() if args.message is not None else task_commit_message(changes)
            if not message:
                print("error: sync message must not be empty", file=sys.stderr)
                return 2
            paths = sorted(
                {
                    path
                    for _, old_path, new_path in changes
                    for path in (old_path, new_path)
                    if path is not None
                }
            )
            commit = git_text("commit", "--only", "-m", message, "--", *paths)
            if commit.returncode != 0:
                detail = commit.stderr.strip() or commit.stdout.strip()
                print(f"error: git commit failed: {detail}", file=sys.stderr)
                return 1
            committed = True
            print(f"Committed {len(changes)} workspace change(s): {message}")
        status = git_text("status", "--porcelain", "--untracked-files=all")
        if status.returncode != 0 or status.stdout:
            print(
                "error: sync requires a clean working tree after committing cards and policy; "
                "commit or remove non-task changes first",
                file=sys.stderr,
            )
            return 1

    fetch = git_text("fetch", "origin")
    if fetch.returncode != 0:
        detail = fetch.stderr.strip() or fetch.stdout.strip()
        print(f"error: git fetch failed: {detail}", file=sys.stderr)
        return 1

    with time_mutation_lock():
        status = git_text("status", "--porcelain", "--untracked-files=all")
        if status.returncode != 0:
            detail = status.stderr.strip() or status.stdout.strip()
            print(f"error: could not inspect workspace after fetch: {detail}", file=sys.stderr)
            return 1
        if status.stdout:
            print(
                "error: workspace changed while sync was fetching; no merge was performed. "
                "Run 'mt sync' again after the current task changes finish",
                file=sys.stderr,
            )
            return 1
        remote_ref = f"refs/remotes/origin/{branch_name}"
        has_remote_branch = git_text("rev-parse", "--verify", "--quiet", remote_ref).returncode == 0
        if has_remote_branch:
            preflight = git_text("merge-tree", "--write-tree", "HEAD", f"origin/{branch_name}")
            if preflight.returncode != 0:
                detail = preflight.stderr.strip() or preflight.stdout.strip()
                if detail:
                    print(detail, file=sys.stderr)
                print(
                    f"error: sync would conflict with origin/{branch_name}; "
                    "no merge was performed. Resolve the conflicting paths manually, "
                    "then run 'mt sync' again.",
                    file=sys.stderr,
                )
                return 1
            merge = git_text("merge", "--no-edit", f"origin/{branch_name}")
            if merge.returncode != 0:
                print(
                    "Sync stopped. Resolve the merge conflicts shown by 'git status', "
                    "or run 'git merge --abort'.",
                    file=sys.stderr,
                )
                return merge.returncode
            if not run_validator():
                return 1
            ahead = git_text("rev-list", "--count", f"origin/{branch_name}..HEAD")
            if ahead.returncode != 0:
                detail = ahead.stderr.strip() or ahead.stdout.strip()
                print(f"error: could not inspect sync state: {detail}", file=sys.stderr)
                return 1
            should_push = int(ahead.stdout.strip() or "0") > 0
        else:
            should_push = True

    if not should_push:
        print(f"Synced with origin/{branch_name}. Nothing to push.")
        return 0
    push = git_text("push", "origin", f"HEAD:{branch_name}")
    if push.returncode != 0:
        detail = push.stderr.strip() or push.stdout.strip()
        print(f"error: git push failed: {detail}", file=sys.stderr)
        return 1
    if committed:
        print(f"Synced task changes with origin/{branch_name}.")
    else:
        print(f"Synced with origin/{branch_name}.")
    return 0


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50].rstrip("-")
    return slug or "task"


def replace_frontmatter_field(raw: str, key: str, value: str) -> str:
    return card_format.replace_property(raw, key, value)


def parse_tag_json(value: str) -> list[str]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("tags must be a JSON string array") from error
    if not isinstance(parsed, list) or any(
        not isinstance(tag, str) or not tag.strip() or any(c in tag for c in "\r\n\x00")
        for tag in parsed
    ):
        raise ValueError("tags must be a JSON array of non-empty single-line strings")
    return list(dict.fromkeys(tag.strip() for tag in parsed))


def validate_item_changes(changes: dict[str, bytes], *, deletions: set[str] | None = None,
                          source_root: Path | None = None,
                          previous: dict[str, bytes | None] | None = None) -> None:
    """Validate proposed card bytes without copying the workspace."""
    deletions = deletions or set()
    for relative in set(changes) | deletions:
        if Path(relative).is_absolute() or ".." in Path(relative).parts or not re.fullmatch(r"cards/[^/]+\.md", relative):
            raise ValueError(f"invalid item path in item changes: {relative}")
    base = source_root or (demo_root() if demo_active() else REPO_ROOT)
    request = {"changes": {path: raw.decode("utf-8") for path, raw in changes.items()}, "deletions": sorted(deletions)}
    if previous is not None:
        request["previous"] = {path: raw.decode("utf-8") if raw is not None else None for path, raw in previous.items()}
    result = subprocess.run(
        [str(REPO_ROOT / "scripts" / "validate"), "--data-root", str(base), "--changes"],
        cwd=REPO_ROOT, input=json.dumps(request, ensure_ascii=False), capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise ValueError((result.stdout + result.stderr).strip() or "candidate validation failed")


def current_username(explicit: str | None = None) -> str:
    value = explicit if explicit is not None else os.environ.get("MT_USERNAME")
    if value is None:
        value = policy().get("default_username")
    username = (value if value is not None else getpass.getuser()).strip()
    if not username or any(char in username for char in "\r\n\x00"):
        raise ValueError("username must be a non-empty, single-line name")
    return username


def check_claim(task: Card, username: str, *, takeover: bool = False) -> None:
    owner = task.frontmatter.get("claimed_by")
    if owner and owner != username and not takeover:
        raise ValueError(
            f"task is claimed by '{owner}'. If you are {owner}, add --by {shlex.quote(owner)} "
            f"(or set MT_USERNAME={shlex.quote(owner)}). Taking over someone else's claim needs the user's "
            f"permission: mt move @{task.short_ref} doing --by {shlex.quote(username)} --takeover."
        )


def transition_claim(task: Card, raw: str, destination: str, username: str, timestamp: str) -> str:
    owner = task.frontmatter.get("claimed_by")
    properties, body = card_format.parse_document(raw, expected_kind="task")
    if destination == "doing":
        if owner == username:
            return raw
        properties["claimed_by"] = username
        properties["claimed_at"] = timestamp
        comment = f"{username} took over the claim from {owner}." if owner else f"{username} claimed this task."
    elif owner:
        for field in CLAIM_FIELDS:
            properties.pop(field, None)
        comment = f"{username} moved this task to {destination}; released the claim held by {owner}."
    else:
        return raw
    raw = card_format.render_document(properties, body, expected_kind="task")
    return append_comments(raw, [comment], timestamp)


def task_defaults(cfg: dict) -> tuple[str, str, str]:
    values: list[str] = []
    for field, allowed_field, singular in (
        ("default_status", "statuses", "status"),
        ("default_priority", "priorities", "priority"),
        ("default_type", "types", "type"),
    ):
        value = cfg.get(field)
        allowed = cfg.get(allowed_field)
        if not isinstance(value, str) or not isinstance(allowed, dict) or value not in allowed:
            raise ValueError(f"policy {field} must name a configured {singular}")
        values.append(value)
    return values[0], values[1], values[2]


def unassigned_priority(cfg: dict) -> str:
    """Use the unassigned class when supported, for explicit unassigned flows."""
    return "none" if "none" in cfg.get("priorities", {}) else task_defaults(cfg)[1]


def default_priority(cfg: dict) -> str:
    """Return the shared policy default used when creating either card kind."""
    return task_defaults(cfg)[1]


def validate_shared_metadata(
    cfg: dict,
    *,
    priority: object = None,
    default_priority: bool = True,
    project: object = None,
    topic: object = None,
    due: object = None,
    due_kind: object = None,
    tags: object = None,
) -> dict[str, object]:
    """Normalize and validate shared metadata at every card-write boundary."""
    selected_priority = task_defaults(cfg)[1] if priority is None and default_priority else priority
    if selected_priority is not None and (
        not isinstance(selected_priority, str) or selected_priority not in cfg.get("priorities", {})
    ):
        raise ValueError("priority must be an allowed effective policy value")
    normalized: dict[str, object] = {"priority": selected_priority}
    if project is not None and (not isinstance(project, str) or not project.strip()
                                or any(char in project for char in "\r\n\x00")):
        raise ValueError("project must be non-empty single-line text")
    normalized["project"] = project.strip() if isinstance(project, str) else None
    if topic is not None:
        topic_problem = "must be text" if not isinstance(topic, str) else card_format.topic_path_error(topic)
        if topic_problem:
            raise ValueError(f"topic {topic_problem}")
        normalized["topic"] = card_format.normalize_topic(topic)
    else:
        normalized["topic"] = None
    if due is not None and (not isinstance(due, str) or not is_due_value(due)):
        raise ValueError("due must be a valid date or local timestamp")
    if due is not None and due_kind_error(due_kind, due, cfg):
        raise ValueError(due_kind_error(due_kind, due, cfg) or "invalid due kind")
    if due is None and due_kind is not None:
        raise ValueError("due_kind requires due")
    normalized.update(due=due, due_kind=due_kind)
    if tags is None:
        tags = []
    if not isinstance(tags, list) or any(
        not isinstance(tag, str) or not tag.strip() or any(char in tag for char in "\r\n\x00")
        for tag in tags
    ):
        raise ValueError("tags must be non-empty single-line strings")
    normalized["tags"] = list(dict.fromkeys(tag.strip() for tag in tags))
    return normalized


def render_new_card(
    kind: str,
    card_id: str,
    title: str,
    *,
    body_text: str | None = None,
    status: str | None = None,
    task_type: str | None = None,
    next_action: str = "",
    processing_instructions_text: str = "",
    priority: str | None = None,
    project: str | None = None,
    topic: str | None = None,
    due: str | None = None,
    due_kind: str | None = None,
    tags: list[str] | None = None,
    created_at: str | None = None,
    capture_id: str | None = None,
    archived: bool = False,
    hidden: bool = False,
    needs_review: bool = False,
) -> str:
    """Render a new card; tasks fill status and type from policy defaults."""
    cfg = policy()
    default_status, default_priority, default_type = task_defaults(cfg)
    if kind == "task":
        status, task_type = status or default_status, task_type or default_type
    stamp = now_stamp()
    return render_item_document(
        item_mode=kind, item_id=card_id, title=title, priority=priority or default_priority,
        project=project, topic=topic, due=due, due_kind=due_kind, created=created_at or stamp, updated=stamp,
        tags=tags or [], body_text=title if body_text is None else body_text, status=status,
        task_type=task_type, next_action=next_action,
        processing_instructions=processing_instructions_text,
        archived=archived, hidden=hidden, needs_review=needs_review,
        capture_id=capture_id,
    )


def render_converted_note(task: Card, today: str) -> str:
    properties = {key: value for key, value in task.frontmatter.items()
                  if key != "kind" and key not in card_format.TASK_ONLY_FIELDS}
    properties.update(kind="note", title=task.title, updated=today)
    comments = list(properties.get("comments", []))
    properties["comments"] = comments
    action = task.frontmatter.get("next_action")
    history = (f"Converted from task on {today}.\n\nStatus: {task.frontmatter.get('status')}\n"
               f"Type: {task.frontmatter.get('type')}\nNext action:\n{action if isinstance(action, str) and action else '(none)'}")
    comments.append({"at": today, "text": history})
    return card_format.render_document(properties, task.body, expected_kind="note")


def render_converted_task(note: Card, today: str, *, status_override: str | None = None,
                          type_override: str | None = None,
                          next_action_override: str | None = None) -> str:
    cfg = policy()
    default_status, _priority, default_type = task_defaults(cfg)
    status = status_override or default_status
    task_type = type_override or default_type
    if status not in open_statuses():
        raise ValueError(f"task status must be one of: {', '.join(open_statuses())}")
    if task_type not in cfg.get("types", {}):
        raise ValueError("task type must be an allowed effective policy value")
    properties = {key: value for key, value in note.frontmatter.items()
                  if key not in {"kind", "status", "type", "next_action"}}
    properties.update(kind="task", title=note.title, status=status, type=task_type, updated=today)
    if next_action_override:
        properties["next_action"] = next_action_override
    else:
        properties.pop("next_action", None)
    return card_format.render_document(properties, note.body, expected_kind="task")


class CardInputError(ValueError):
    """New-card input was rejected before anything was written."""


def card_capture_fields(card: Card) -> dict[str, object]:
    """Properties a capture retry must match to be treated as the same card."""
    fm = card.frontmatter
    return {
        "kind": card.kind, "title": card.title, "created": str(fm.get("created") or ""),
        "status": fm.get("status") if card.is_task else None,
        "type": fm.get("type") if card.is_task else None,
        "next_action": card.next_action or None,
        "processing_instructions": fm.get("processing_instructions") or None,
        "priority": fm.get("priority"), "project": fm.get("project"), "topic": fm.get("topic"),
        "due": fm.get("due"), "due_kind": due_kind_value(fm), "tags": fm.get("tags") or [],
        "needs_review": fm.get("needs_review") is True,
        "archived": is_archived(card), "hidden": is_hidden(card),
    }


def card_by_capture_id(capture_id: str) -> Card | None:
    # Search both kinds, including archived/hidden cards.
    for card in read_cards(real_cards_root()):
        if card.frontmatter.get("capture_id") == capture_id:
            return card
    return None


def create_card(
    kind: str,
    text: str,
    *,
    title: str,
    created: str | None = None,
    capture_id: str | None = None,
    status: str | None = None,
    task_type: str | None = None,
    next_action: str = "",
    instructions: str = "",
    priority: str | None = None,
    project: str | None = None,
    topic: str | None = None,
    due: str | None = None,
    due_kind: str | None = None,
    tags: list[str] | None = None,
    needs_review: bool = False,
    archived: bool = False,
    hidden: bool = False,
    by: str | None = None,
) -> tuple[Card, bool]:
    """Validate, deduplicate by capture ID, and write one new task or note.

    Every creation path (add, note, capture, drafts, dashboard) uses this.
    Returns the saved card and whether a new file was written.
    """
    try:
        cfg = policy()
        default_status, _priority, default_type = task_defaults(cfg)
        shared = validate_shared_metadata(
            cfg, priority=priority, project=project, topic=topic, tags=tags, due=due, due_kind=due_kind,
        )
    except ValueError as error:
        raise CardInputError(str(error)) from error
    title = title.strip()
    if not title or any(character in title for character in "\r\n\x00"):
        raise CardInputError("title must be non-empty single-line text")
    if kind == "task":
        status = status or default_status
        if status not in open_statuses():
            raise CardInputError(f"task status must be one of: {', '.join(open_statuses())}")
        task_type = task_type or default_type
        if task_type not in cfg.get("types", {}):
            raise CardInputError("task type must be an allowed effective policy value")
        if by is not None and status != "doing":
            raise CardInputError("--by requires status doing")
    elif kind == "note":
        if status is not None or task_type is not None or next_action or by is not None:
            raise CardInputError("status, type, next action, and claims apply only to tasks")
    else:
        raise CardInputError("kind must be task or note")
    created = created or now_stamp()
    desired = {
        "kind": kind, "title": title, "created": created, "status": status, "type": task_type,
        "next_action": next_action or None, "processing_instructions": instructions or None,
        **shared, "needs_review": bool(needs_review), "archived": bool(archived), "hidden": bool(hidden),
    }
    if capture_id is not None:
        existing = card_by_capture_id(capture_id)
        if existing is not None:
            if card_capture_fields(existing) == desired and existing.body == text:
                return existing, False
            raise CardInputError(
                "conflict: capture ID already exists with different kind, title, body, created-at, or metadata"
            )
    card_id = f"{unique_item_timestamp(real=True)}-{slugify(title)}"
    destination = real_cards_root() / f"{card_id}.md"
    if destination.exists():
        raise CardInputError(f"{kind} file already exists: {destination}")
    try:
        rendered = render_new_card(
            kind, card_id, title, body_text=text, status=status, task_type=task_type,
            next_action=next_action, processing_instructions_text=instructions,
            priority=str(shared["priority"]), project=shared["project"], topic=shared["topic"],
            due=shared["due"], due_kind=shared["due_kind"], tags=shared["tags"],
            created_at=created, capture_id=capture_id,
            archived=bool(archived), hidden=bool(hidden), needs_review=bool(needs_review),
        )
        if kind == "task" and status == "doing":
            frontmatter, body = parse_frontmatter(rendered)
            pending = Card(destination, rendered, frontmatter, body, parse_yaml(frontmatter))
            rendered = transition_claim(pending, rendered, "doing", current_username(by), now_stamp())
    except ValueError as error:
        raise CardInputError(str(error)) from error
    # New user captures always enter the real workspace. Demo cards are a
    # read-only sample and the first real capture switches the view over.
    relative = str(destination.relative_to(REPO_ROOT))
    apply_item_transaction({relative: rendered.encode("utf-8")}, expected={relative: None}, source_root=REPO_ROOT)
    pin_demo_state()
    return Card.read(destination), True


def card_text_option(args: argparse.Namespace, name: str) -> str:
    """Read an optional text property supplied inline or from a --*-file option."""
    value = str(getattr(args, f"{name}_text", None) or getattr(args, name, "") or "")
    return value if value.strip() else ""


def capture_card(args: argparse.Namespace, text: str, kind: str) -> tuple[Card, bool]:
    """Save one quick capture with a retry-safe capture ID when one is supplied.

    Captured notes default to needing review; captured tasks are triaged by status.
    """
    if not run_validator():
        raise RuntimeError(f"workspace validation failed before {kind} capture")
    raw_title = getattr(args, "title", None)
    title = raw_title.strip() if isinstance(raw_title, str) else ""
    if not title and text.strip():
        title = title_from_note_body(text) if kind == "note" else capture_title(text)
    if not title:
        raise CardInputError(f"{kind} requires non-empty text or a title")
    supplied_id = getattr(args, "capture_id", None) or None
    created_at = getattr(args, "created_at", None) or None
    capture_id = None
    if supplied_id is not None:
        try:
            capture_id = str(uuid.UUID(supplied_id))
        except (AttributeError, ValueError) as error:
            raise CardInputError("capture ID must be a canonical UUID") from error
        if capture_id != supplied_id.lower():
            raise CardInputError("capture ID must be a canonical UUID")
        if created_at is None or not is_timestamp(created_at):
            raise CardInputError("a capture ID requires a created-at time in YYYY-MM-DDTHH:MM:SS format")
    elif created_at is not None:
        raise CardInputError("a created-at time requires a capture ID")
    needs_review = getattr(args, "needs_review", None)
    return create_card(
        kind, text, title=title, capture_id=capture_id,
        created=created_at or getattr(args, "capture_timestamp", None),
        status=getattr(args, "status", None) if kind == "task" else None,
        task_type=(getattr(args, "task_type", None) or getattr(args, "type", None)) if kind == "task" else None,
        next_action=card_text_option(args, "next_action") if kind == "task" else "",
        instructions=card_text_option(args, "instructions"),
        priority=getattr(args, "priority", None), project=getattr(args, "project", None),
        topic=getattr(args, "topic", None), due=getattr(args, "due", None),
        due_kind=getattr(args, "due_kind", None), tags=getattr(args, "tags", None),
        needs_review=(kind == "note") if needs_review is None else bool(needs_review),
        archived=bool(getattr(args, "archived", False)), hidden=bool(getattr(args, "hidden", False)),
        by=getattr(args, "by", None) if kind == "task" else None,
    )


def capture_title(text: str) -> str:
    """Keep generated titles short and mark omitted text without summarizing it."""
    title = re.sub(r"\s+", " ", text).strip()
    if len(title) <= 80:
        return title
    prefix = title[:79]
    if title[79] != " " and " " in prefix:
        prefix = prefix.rsplit(" ", 1)[0]
    return prefix.rstrip() + "…"


@serialized_time_mutation
def command_create(args: argparse.Namespace) -> int:
    kind = getattr(args, "kind", "task")
    as_json = getattr(args, "as_json", False)
    if getattr(args, "capture", False):
        try:
            card, changed = capture_card(args, " ".join(args.text), kind)
        except (OSError, ValueError, RuntimeError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
    else:
        if not run_validator():
            return 1
        next_action = card_text_option(args, "next_action")
        text = " ".join(args.text)
        try:
            card, changed = create_card(
                kind, text, title=getattr(args, "title", None) or capture_title(text),
                status=getattr(args, "status", None), task_type=getattr(args, "task_type", None),
                next_action=next_action, instructions=card_text_option(args, "instructions"),
                priority=getattr(args, "priority", None), project=getattr(args, "project", None),
                topic=getattr(args, "topic", None), due=getattr(args, "due", None),
                due_kind=getattr(args, "due_kind", None), tags=getattr(args, "tags", None),
                needs_review=bool(getattr(args, "needs_review", False)),
                archived=bool(getattr(args, "archived", False)), hidden=bool(getattr(args, "hidden", False)),
                by=getattr(args, "by", None),
            )
        except CardInputError as error:
            print(f"error: invalid capture: {error}", file=sys.stderr)
            return 2
        except (OSError, ValueError) as error:
            print(f"error: generated item failed validation; creation rolled back: {error}", file=sys.stderr)
            return 1
    if as_json:
        auto_sync_after_mutation(changed=changed)
        return MutationReceipt(saved_receipt(card, updated=changed))
    print(card.path)
    return auto_sync_after_mutation(changed=changed)


def command_add(args: argparse.Namespace) -> int:
    """Create a card; a note with no text or --file opens the human-only editor."""
    if args.kind == "note" and args.file is None and not args.text:
        return command_note_editor(args)
    return command_create(args)


@serialized_time_mutation
def save_terminal_capture(fields: dict) -> int:
    """Lock only the final write, using the same transaction as dashboard capture."""
    def optional_text(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        value = value.strip()
        return value if value and value != "-" else None

    normalized = dict(fields)
    for key in ("title", "project", "topic", "due", "due_kind"):
        normalized[key] = optional_text(normalized.get(key))
    args = argparse.Namespace(**normalized, capture_id=str(uuid.uuid4()), created_at=now_stamp())
    try:
        item, changed = capture_card(args, normalized["text"], fields["kind"])
    except (OSError, ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Created {item_receipt(item)['ref']}: {item.path}")
    return auto_sync_after_mutation(changed=changed)


def command_capture(args: argparse.Namespace) -> int:
    import capture_cli

    if not sys.stdin.isatty() or not sys.stdout.isatty() or os.environ.get("TERM") == "dumb":
        print("error: capture requires an interactive terminal; use mt add or mt note with text or --file -", file=sys.stderr)
        return 2

    def edit_text(text: str) -> str:
        try:
            command = editor_command("capture")
        except ValueError as error:
            print(f"Cannot open editor: {error}")
            return text
        recovery = create_editor_recovery_copy("terminal-capture", text.encode("utf-8"))
        try:
            result = subprocess.run([*command, str(recovery)], cwd=REPO_ROOT, check=False)
        except (OSError, KeyboardInterrupt) as error:
            print(f"Editor interrupted or unavailable: {error}; copy preserved at {recovery}")
            return text
        if result.returncode:
            print(f"Editor failed; copy preserved at {recovery}")
            return text
        edited = recovery.read_text(encoding="utf-8")
        recovery.unlink()
        return edited

    try:
        cfg = policy()
        fields = capture_cli.collect(cfg, task_defaults(cfg), " ".join(args.text), edit_text, is_due_value)
    except (KeyboardInterrupt, EOFError):
        print("\nCapture cancelled.")
        return 0
    if fields is None:
        print("Capture cancelled.")
        return 0
    return save_terminal_capture(fields)


def sync_error_after_json_save() -> str | None:
    if not policy().get("rules", {}).get("auto_sync", False):
        return None
    return run_auto_sync_for_receipt()


def editor_command(command_name: str = "note") -> list[str]:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        raise ValueError(f"set VISUAL or EDITOR before running {command_name}")
    try:
        command = shlex.split(editor)
    except ValueError as error:
        raise ValueError(f"invalid editor command: {error}") from error
    if not command:
        raise ValueError("VISUAL or EDITOR must not be empty")
    return command


def title_from_note_body(text: str) -> str:
    meeting_headings = {"### Discussion", "### Decisions", "### Actions"}
    is_meeting = meeting_headings <= {line.strip() for line in text.splitlines()}
    for line in text.splitlines():
        value = line.strip()
        if not value or (is_meeting and value in meeting_headings):
            continue
        value = re.sub(r"^#{1,6}\s+", "", value)
        if value:
            return capture_title(value)
    return "Meeting notes" if is_meeting else "Untitled note"


def command_note_editor(args: argparse.Namespace) -> int:
    if not run_validator():
        return 1
    try:
        command = editor_command("note")
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    template = "### Discussion\n\n### Decisions\n\n### Actions\n" if args.template == "meeting" else ""
    timestamp = now_stamp()  # Capture creation time when the editor opens.
    try:
        draft_path = create_editor_recovery_copy("new-note", template.encode("utf-8"))
    except OSError as error:
        print(f"error: could not prepare note draft: {error}", file=sys.stderr)
        return 1
    keep_draft = False
    try:
        keep_draft = True
        try:
            edited = subprocess.run([*command, str(draft_path)], cwd=REPO_ROOT, check=False)
        except FileNotFoundError:
            keep_draft = False
            print(f"error: editor not found: {command[0]}", file=sys.stderr)
            return 1
        if edited.returncode != 0:
            print(f"error: editor exited with status {edited.returncode}; note was not saved", file=sys.stderr)
            return edited.returncode
        candidate = draft_path.read_bytes().decode("utf-8")
        unchanged_template = template and (
            [line.strip() for line in candidate.splitlines() if line.strip()] ==
            [line.strip() for line in template.splitlines() if line.strip()]
        )
        if unchanged_template or not candidate.strip():
            keep_draft = False
            print("No changes: note was not saved.")
            return 0
        args.capture_timestamp = timestamp
        try:
            with time_mutation_lock():
                note_item, changed = capture_card(args, candidate, "note")
        except (OSError, ValueError, RuntimeError) as error:
            print(f"error: note was not saved: {error}", file=sys.stderr)
            return 1
        keep_draft = False
        if getattr(args, "as_json", False):
            receipt = saved_receipt(note_item, updated=changed)
            sync_error = sync_error_after_json_save() if changed else None
            if sync_error:
                receipt["sync_error"] = sync_error
            print(json.dumps(receipt, ensure_ascii=False))
            return 0
        print(note_item.path)
        return auto_sync_after_mutation(changed=changed)
    except (OSError, UnicodeError) as error:
        print(f"error: could not read edited note draft: {error}", file=sys.stderr)
        return 1
    finally:
        if keep_draft:
            print(f"Edited copy preserved for recovery: {draft_path}", file=sys.stderr)
        else:
            draft_path.unlink(missing_ok=True)


def task_matches(task: Card, query: str) -> bool:
    needle = query.casefold()
    if needle.startswith("@"):
        return task.short_ref.startswith(needle[1:])
    task_id = str(task.frontmatter.get("id") or "")
    return needle in searchable_text(task).casefold() or needle in task_id.casefold()


def searchable_text(item: Card) -> str:
    parts = [item.title, item.body, str(item.next_action or "")]
    comments = item.frontmatter.get("comments", [])
    if isinstance(comments, list):
        parts.extend(str(comment.get("text") or "") for comment in comments if isinstance(comment, dict))
    return "\n".join(parts)


def term_match(task: Card, query: str, mode: str) -> bool:
    task_id = str(task.frontmatter.get("id") or "")
    haystack = f"{searchable_text(task)}\n{task_id}".casefold()
    terms = query.casefold().split()
    return all(term in haystack for term in terms) if mode == "and" else any(term in haystack for term in terms)


def fuzzy_match_score(task: Card, query: str) -> float | None:
    """Return a fuzzy token-match score, or None when a query token is too distant."""
    terms = re.findall(r"[\w-]+", query.casefold())
    candidates = set(re.findall(r"[\w-]+", f"{searchable_text(task)}\n{task.path.stem}".casefold()))
    if not terms or not candidates:
        return None
    scores = [max(difflib.SequenceMatcher(None, term, candidate).ratio() for candidate in candidates) for term in terms]
    return sum(scores) / len(scores) if min(scores) >= 0.75 else None


def match_score(task: Card, query: str, mode: str) -> float | None:
    if query.casefold().startswith("@"):
        return 1.0 if task_matches(task, query) else None
    if mode == "exact":
        return 1.0 if task_matches(task, query) else None
    if mode in ("and", "or"):
        return 1.0 if term_match(task, query, mode) else None
    return fuzzy_match_score(task, query)


def find_matches(tasks: list[Card], query: str, match_mode: str) -> list[tuple[float, Card]]:
    if query.startswith("@") and not SHORT_REF_RE.fullmatch(query):
        raise ValueError("short reference must be @ followed by 3 to 8 hexadecimal characters")
    return [
        (score, task)
        for task in tasks
        if (score := match_score(task, query, match_mode)) is not None
    ]


def search_match_score(item: Card, query: str, mode: str) -> float | None:
    """Match only visible card text and return a deterministic relevance score."""
    title = item.title.casefold()
    item_id = str(item.frontmatter.get("id") or "").casefold()
    body = "\n".join([item.body, str(item.next_action or ""), *[
        str(comment.get("text") or "") for comment in (item.frontmatter.get("comments", []) or [])
        if isinstance(comment, dict)
    ]]).casefold()
    regions = ((title, 3), (item_id, 2), (body, 1))

    if mode == "phrase":
        needle = query.casefold()
        weights = [weight for text, weight in regions if needle in text]
        return float(max(weights)) if weights else None

    if mode in ("all", "any"):
        terms = list(dict.fromkeys(query.casefold().split()))
        if not terms:
            return None
        weights = [max((weight for text, weight in regions if term in text), default=0) for term in terms]
        if mode == "all" and any(weight == 0 for weight in weights):
            return None
        if mode == "any" and all(weight == 0 for weight in weights):
            return None
        return float(sum(weights))

    if mode == "fuzzy":
        terms = re.findall(r"[\w-]+", query.casefold())
        candidates = sorted(set(re.findall(r"[\w-]+", f"{item.title}\n{item_id}\n{body}".casefold())))
        if not terms or not candidates:
            return None
        scores = [max(difflib.SequenceMatcher(None, term, candidate).ratio() for candidate in candidates)
                  for term in terms]
        if min(scores) < 0.75:
            return None
        return sum(scores) / len(scores)

    raise ValueError(f"unknown search match mode: {mode}")


def find_search_matches(items: list[Card], query: str, mode: str) -> list[tuple[float, Card]]:
    """Return matching cards with the score used by relevance sorting."""
    return [
        (score, item)
        for item in items
        if (score := search_match_score(item, query, mode)) is not None
    ]


def items_by_ref(ref: str, kind: str | None = None, statuses: list[str] | None = None) -> list[Card]:
    """Resolve filename-based refs before reading card content."""
    if not SHORT_REF_RE.fullmatch(ref):
        raise ValueError("short reference must be @ followed by 3 to 8 hexadecimal characters")
    root = cards_root()
    matches: list[Card] = []
    for path in sorted(root.glob("*.md")) if root.is_dir() else []:
        if not hashlib.sha256(path.stem.encode()).hexdigest()[:8].startswith(ref[1:].lower()):
            continue
        item = Card.read(path)
        if in_selection(item, kind, statuses):
            matches.append(item)
    return matches


def resolve_unique(
    query: str,
    *,
    kind: str | None = None,
    statuses: list[str] | None = None,
    match_mode: str = "exact",
) -> tuple[Card | None, int]:
    includes_notes = kind != "task" and statuses is None
    try:
        if query.startswith("@"):
            matches = items_by_ref(query, kind, statuses)
        else:
            matches = [task for _, task in find_matches(discover(kind, statuses), query, match_mode)]
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return None, 2
    if not matches:
        label = "item" if includes_notes else "task"
        print(f"No {label} matches '{query}'.", file=sys.stderr)
        return None, 1
    if len(matches) > 1:
        label = "items" if includes_notes else "tasks"
        print(f"Multiple {label} match '{query}':", file=sys.stderr)
        for task in matches:
            rel = task.path.relative_to(REPO_ROOT) if task.path.is_relative_to(REPO_ROOT) else task.path
            print(f"  [@{task.short_ref}] {rel} - {task.title}", file=sys.stderr)
            print(f"    next: {task.next_action or '-'}", file=sys.stderr)
        return None, 2
    return matches[0], 0


@serialized_time_mutation
def command_done(args: argparse.Namespace) -> int:
    query = " ".join(args.query).strip()
    if not query:
        print("error: done requires a non-empty query", file=sys.stderr)
        return 2
    task, result = resolve_unique(query, statuses=[name for name in statuses() if name != "done"], match_mode=args.match_mode)
    if task is None:
        return result
    try:
        username = current_username(getattr(args, "by", None))
        check_claim(task, username)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    completed_epoch = int(clock_time.time())
    completed_at = local_stamp(completed_epoch)
    completion_day = completed_at[:10]
    updated = replace_frontmatter_field(task.raw, "status", "done")
    updated = replace_frontmatter_field(updated, "updated", completed_at)
    updated = card_format.replace_property(updated, "completed", completed_at)
    updated = transition_claim(task, updated, "done", username, completed_at)
    try:
        timers = read_time_timers()
        task_id = str(task.frontmatter.get("id") or task.path.stem)
        timer = timers.get(task_id)
        if timer:
            entry, already_saved = time_session_for(task_id, timer, completed_epoch)
            if not already_saved:
                updated = append_time_entry(updated, entry)
        updated = append_completion_note(updated, args.notes, completion_day)
        updated = append_comments(updated, args.comments, completed_at)
    except ValueError as error:
        print(f"error: completion update failed: {error}", file=sys.stderr)
        return 1
    base = demo_root() if demo_active() else REPO_ROOT
    relative = str(task.path.relative_to(base))
    try:
        apply_item_transaction({relative: updated.encode("utf-8")},
                               expected={relative: task.raw.encode("utf-8")}, source_root=base)
    except (OSError, ValueError) as error:
        print(f"error: completion failed validation; change rolled back: {error}", file=sys.stderr)
        return 1
    if timer:
        try:
            clear_time_timer(task_id, str(timer["session_id"]))
        except OSError as error:
            print(f"warning: task completed and time was saved, but timer cleanup failed: {error}", file=sys.stderr)
    completed_item = Card.read(task.path)
    if args.as_json:
        auto_sync_after_mutation()
        return MutationReceipt(saved_receipt(completed_item, updated=True))
    print(f"Completed and validated: {task.path.name}")
    return auto_sync_after_mutation()


@serialized_time_mutation
def command_move(args: argparse.Namespace) -> int:
    query = " ".join(args.query).strip()
    if not query:
        print("error: move requires a non-empty query", file=sys.stderr)
        return 2
    allowed_statuses = open_statuses()
    if args.destination not in allowed_statuses:
        print(
            f"error: move destination must be one of: {', '.join(allowed_statuses)}",
            file=sys.stderr,
        )
        return 2
    takeover = getattr(args, "takeover", False)
    if takeover and args.destination != "doing":
        print("error: --takeover requires destination doing", file=sys.stderr)
        return 2
    task, result = resolve_unique(query, statuses=allowed_statuses, match_mode=args.match_mode)
    if task is None:
        return result
    try:
        username = current_username(getattr(args, "by", None))
        check_claim(task, username, takeover=takeover)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    source_status = task.frontmatter["status"]
    if source_status == args.destination and (
        args.destination != "doing" or task.frontmatter.get("claimed_by") == username
    ):
        if getattr(args, "as_json", False):
            return MutationReceipt(saved_receipt(task))
        print(f"Already {args.destination}: {task.path.name}")
        return 0
    moved_epoch = int(clock_time.time())
    moved_at = local_stamp(moved_epoch)
    updated = replace_frontmatter_field(task.raw, "status", args.destination)
    updated = replace_frontmatter_field(updated, "updated", moved_at)
    updated = transition_claim(task, updated, args.destination, username, moved_at)
    try:
        updated = append_comments(updated, args.comments, moved_at)
        timer = None
        if args.destination != "doing" or task.frontmatter.get("claimed_by") != username:
            updated, timer = finish_lifecycle_timer(task, updated, moved_epoch)
    except ValueError as error:
        print(f"error: move update failed: {error}", file=sys.stderr)
        return 1
    base = demo_root() if demo_active() else REPO_ROOT
    relative = str(task.path.relative_to(base))
    try:
        apply_item_transaction({relative: updated.encode("utf-8")},
                               expected={relative: task.raw.encode("utf-8")}, source_root=base)
    except (OSError, ValueError) as error:
        print(f"error: move failed validation; change rolled back: {error}", file=sys.stderr)
        return 1
    cleanup_lifecycle_timer(task, timer)
    moved_item = Card.read(task.path)
    if getattr(args, "as_json", False):
        auto_sync_after_mutation()
        return MutationReceipt(saved_receipt(moved_item, updated=True))
    print(f"Moved to {args.destination} and validated: {task.path.name}")
    return auto_sync_after_mutation()


@serialized_time_mutation
def command_comment(args: argparse.Namespace) -> int:
    query = " ".join(args.query).strip()
    if not query:
        print("error: comment requires a non-empty query", file=sys.stderr)
        return 2
    item, result = resolve_unique(query, match_mode=args.match_mode)
    if item is None:
        return result
    if not clean_text_values(args.comments):
        print("error: comment requires at least one non-empty message", file=sys.stderr)
        return 2
    commented_at = now_stamp()
    try:
        updated = replace_frontmatter_field(item.raw, "updated", commented_at)
        updated = append_comments(updated, args.comments, commented_at)
    except ValueError as error:
        print(f"error: comment failed: {error}", file=sys.stderr)
        return 1
    base = demo_root() if demo_active() else REPO_ROOT
    relative = str(item.path.relative_to(base))
    original = item.path.read_bytes()
    try:
        apply_item_transaction({relative: updated.encode("utf-8")}, expected={relative: original}, source_root=base)
    except (OSError, ValueError) as error:
        print(f"error: comment failed validation; change rolled back: {error}", file=sys.stderr)
        return 1
    updated_item = Card.read(item.path)
    if args.as_json:
        auto_sync_after_mutation()
        return MutationReceipt(saved_receipt(updated_item, updated=True))
    print(f"Commented and validated: {item.path.relative_to(REPO_ROOT)}")
    return auto_sync_after_mutation()


@serialized_time_mutation
def command_convert(args: argparse.Namespace) -> int:
    query = " ".join(args.query).strip()
    if not query:
        print("error: convert requires a non-empty query", file=sys.stderr)
        return 2
    to_note = args.destination == "note"
    if to_note and any(getattr(args, name, None) is not None for name in ("status", "task_type", "next_action")):
        print("error: --status, --type, and --next-action apply only when converting a note to a task", file=sys.stderr)
        return 2
    item, result = resolve_unique(
        query, kind=None if to_note else "note",
        statuses=[name for name in statuses() if name != "done"] if to_note else None,
        match_mode=args.match_mode,
    )
    if item is None:
        return result
    if item.kind == args.destination:
        print(f"error: convert {args.destination} destination requires a "
              f"{'task' if to_note else 'note'} source", file=sys.stderr)
        return 2
    now = now_stamp()
    try:
        if to_note:
            if item.frontmatter.get("claimed_by"):
                raise ValueError("release the task claim before converting it to a note")
            task_id = str(item.frontmatter.get("id") or item.path.stem)
            if time_log_entries(item.raw) or task_id in read_time_timers():
                raise ValueError("a task with tracked time or a running timer cannot be converted to a note")
            updated = render_converted_note(item, now)
        else:
            updated = render_converted_task(
                item, now, status_override=getattr(args, "status", None),
                type_override=getattr(args, "task_type", None),
                next_action_override=getattr(args, "next_action", None),
            )
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    base = demo_root() if demo_active() else REPO_ROOT
    relative = str(item.path.relative_to(base))
    try:
        apply_item_transaction({relative: updated.encode("utf-8")},
                               expected={relative: item.raw.encode("utf-8")}, source_root=base)
    except (OSError, ValueError) as error:
        print(f"error: conversion failed validation; change rolled back: {error}", file=sys.stderr)
        return 1
    converted = Card.read(item.path)
    if args.as_json:
        auto_sync_after_mutation()
        return MutationReceipt(item_receipt(converted, updated=True))
    print(f"Converted to {args.destination}: {item.path.relative_to(REPO_ROOT)}")
    return auto_sync_after_mutation()


def parse_edit_tags(value: str) -> list[str]:
    try:
        tags = json.loads(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("tags must be a JSON string array") from error
    if not isinstance(tags, list) or any(
        not isinstance(tag, str) or not tag.strip() or any(c in tag for c in "\r\n\x00")
        for tag in tags
    ):
        raise argparse.ArgumentTypeError("tags must be a JSON array of non-empty single-line strings")
    return list(dict.fromkeys(tag.strip() for tag in tags))


def command_edit(args: argparse.Namespace) -> int:
    return _command_edit_serialized(args)


def command_card_metadata(args: argparse.Namespace) -> int:
    """Apply a card metadata patch with a whole-card compare-and-swap."""
    if not re.fullmatch(r"[0-9a-fA-F]{64}", args.expected_hash or ""):
        print("error: expected hash must be a 64-character SHA-256 digest", file=sys.stderr)
        return 2
    try:
        patch = json.loads(args.patch)
    except (TypeError, json.JSONDecodeError) as error:
        print(f"error: metadata patch must be JSON: {error}", file=sys.stderr)
        return 2
    if not isinstance(patch, dict) or not patch:
        print("error: metadata patch contains unsupported or missing fields", file=sys.stderr)
        return 2
    try:
        with time_mutation_lock():
            item, result = resolve_unique(args.ref, match_mode="exact")
            if item is None:
                return result
            kind = item.kind
            if args.kind is not None and kind != args.kind:
                raise ValueError(f"{args.kind} metadata can only be applied to a {args.kind}")
            # Status and visibility flags have their own guarded commands.
            allowed_fields = card_format.EDITABLE_FIELDS[kind] - {"status", *card_format.BOOLEAN_METADATA}
            if set(patch) - allowed_fields:
                print("error: metadata patch contains unsupported or missing fields", file=sys.stderr)
                return 2
            original = item.path.read_bytes()
            actual_hash = hashlib.sha256(original).hexdigest()
            if actual_hash.casefold() != args.expected_hash.casefold():
                raise ValueError("conflict: card changed since it was loaded (content hash mismatch)")
            properties, body = dict(item.frontmatter), item.body
            patched = card_format.apply_property_patch(properties, patch, kind=kind)
            if patched == properties:
                receipt = item_receipt(item)
            else:
                patched["updated"] = now_stamp()
                raw = card_format.render_document(patched, body, expected_kind=kind)
                base = demo_root() if demo_active() else REPO_ROOT
                relative = str(item.path.relative_to(base))
                apply_item_transaction(
                    {relative: raw.encode("utf-8")},
                    expected={relative: original},
                    source_root=base,
                )
                receipt = item_receipt(Card.read(item.path), updated=True)
    except (OSError, ValueError) as error:
        prefix = "error: "
        detail = str(error)
        print(prefix + detail, file=sys.stderr)
        return 1
    if args.as_json:
        print(json.dumps(receipt, ensure_ascii=False))
    else:
        print(f"Updated {kind} properties: {receipt['ref']}")
    return auto_sync_after_mutation(changed=bool(receipt["changed"]))


def abbreviate(text: str, limit: int = 40) -> str:
    flat = " ".join(text.split())
    return repr(flat if len(flat) <= limit else flat[:limit] + "...")


def apply_exact_body_replacements(body: str, raw_json: str) -> str:
    try:
        replacements = json.loads(raw_json)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"--replace-file is not valid JSON ({error.msg}; line {error.lineno}, column {error.colno}). "
            "Pass the JSON through a quoted heredoc (<<'EOF') so the shell does not alter backslashes."
        ) from error
    expected = 'a JSON array of {"old": ..., "new": ...} objects'
    if not isinstance(replacements, list):
        raise ValueError(f"--replace-file must contain {expected}; got {type(replacements).__name__}")
    for index, pair in enumerate(replacements, 1):
        if not isinstance(pair, dict) or set(pair) != {"old", "new"}:
            raise ValueError(f"--replace-file entry {index} must be an object with exactly old and new; expected {expected}")
        if not isinstance(pair["old"], str) or not pair["old"]:
            raise ValueError(f"--replace-file entry {index}: old must be non-empty text")
        if not isinstance(pair["new"], str):
            raise ValueError(f"--replace-file entry {index}: new must be text")
    return apply_body_replacements(body, [(pair["old"], pair["new"]) for pair in replacements])


def apply_body_replacements(body: str, pairs: list[tuple[str, str]]) -> str:
    """Replace each old passage that occurs exactly once in body; all or nothing."""
    matches: list[tuple[int, int, str]] = []
    for index, (old, new) in enumerate(pairs, 1):
        if not old:
            raise ValueError(f"replacement {index} old text must be non-empty")
        positions: list[int] = []
        cursor = 0
        while (position := body.find(old, cursor)) >= 0:
            positions.append(position)
            cursor = position + 1
        if len(positions) != 1:
            raise ValueError(
                f"replacement {index} old text {abbreviate(old)} must match exactly once in the "
                f"original body (found {len(positions)} matches)"
            )
        start = positions[0]
        matches.append((start, start + len(old), new))
    ordered = sorted(matches)
    for previous, current in zip(ordered, ordered[1:]):
        if current[0] < previous[1]:
            raise ValueError("exact body replacements overlap")
    result = body
    for start, end, new in reversed(ordered):
        result = result[:start] + new + result[end:]
    return result


@serialized_time_mutation
def _command_edit_serialized(args: argparse.Namespace) -> int:
    return _command_edit(args)


def _command_edit(args: argparse.Namespace) -> int:
    query = " ".join(args.query).strip()
    item, result = resolve_unique(query, match_mode=args.match_mode)
    if item is None:
        return result
    kind = item.kind
    base = demo_root() if demo_active() else REPO_ROOT
    relative = str(item.path.relative_to(base))
    try:
        original = item.path.read_bytes()
        verify_edit_guards(item, args, original)
        if original != item.raw.encode("utf-8"):
            raise ValueError("conflict: item changed since it was loaded (content changed)")
        properties, body = dict(item.frontmatter), item.body
        initial_properties = dict(properties)
        initial_body = body

        if args.body:
            if args.old is not None:
                body = apply_body_replacements(body, list(zip(args.old, args.new)))
            elif args.replace_file is not None:
                body = apply_exact_body_replacements(body, args.replacements_text)
            elif args.append:
                addition = args.file_text
                if addition:
                    body = body + blank_line_separator(body) + addition
            else:
                body = args.file_text

        metadata: dict[str, object] = {}
        if args.tags is not None:
            metadata["tags"] = args.tags
        for argument, field in (("project", "project"), ("topic", "topic"),
                                ("priority", "priority"), ("task_type", "type"),
                                ("due", "due"), ("due_kind", "due_kind"),
                                ("hidden", "hidden"), ("archived", "archived"),
                                ("needs_review", "needs_review"), ("status", "status")):
            value = getattr(args, argument, None)
            if value is not None:
                metadata[field] = value
        if args.title is not None:
            metadata["title"] = args.title
        for assignment in args.assignments or ():
            field, separator, value = assignment.partition("=")
            if not separator or not field:
                raise ValueError("--set requires FIELD=VALUE")
            allowed = card_format.EDITABLE_FIELDS[kind]
            if field not in allowed:
                raise ValueError(f"field '{field}' cannot be set on a {kind}; choose from {', '.join(sorted(allowed))}")
            if field in metadata:
                raise ValueError(f"field '{field}' was assigned more than once")
            if field == "tags":
                metadata[field] = parse_tag_json(value)
            elif field in card_format.BOOLEAN_METADATA:
                if value not in {"true", "false"}:
                    raise ValueError(f"{field} must be true or false")
                metadata[field] = value == "true"
            elif field in {"next_action", "processing_instructions"}:
                metadata[field] = None if value == "null" or value == "" else value
            elif field == "title":
                if not value.strip() or any(char in value for char in "\r\n\x00"):
                    raise ValueError("title must be non-empty and single-line")
                metadata[field] = value.strip()
            else:
                metadata[field] = None if value == "null" else value
        if getattr(args, "tag_add", None) or getattr(args, "tag_remove", None):
            if "tags" in metadata:
                raise ValueError("use one of --tags/--set tags or --tag/--remove-tag")
            if any(not tag.strip() for tag in (getattr(args, "tag_add", None) or [])
                   + (getattr(args, "tag_remove", None) or [])):
                raise ValueError("tags must be non-empty")
            current_tags = list(properties.get("tags") or [])
            remove = {tag.strip().casefold() for tag in (getattr(args, "tag_remove", None) or [])}
            current_tags = [tag for tag in current_tags if str(tag).strip().casefold() not in remove]
            existing = {str(tag).strip().casefold() for tag in current_tags}
            for tag in getattr(args, "tag_add", None) or []:
                tag = tag.strip()
                if tag.casefold() not in existing:
                    current_tags.append(tag)
                    existing.add(tag.casefold())
            metadata["tags"] = current_tags

        if "status" in metadata:
            if kind != "task":
                raise ValueError("field 'status' cannot be set on a note")
            new_status = metadata["status"]
            if not isinstance(new_status, str) or not new_status.strip():
                raise ValueError("status must be non-empty")
            new_status = new_status.strip()
            if new_status == "doing":
                raise ValueError("status 'doing' requires claiming; use mt move doing or mt claim")
            if new_status == "done":
                raise ValueError("status 'done' requires completing; use mt done")
            if new_status not in open_statuses():
                raise ValueError(f"status must be one of: {', '.join(status for status in open_statuses() if status != 'doing')}")
            metadata["status"] = new_status
        properties = card_format.apply_property_patch(properties, metadata, kind=kind)

        # A claim blocks conflicting work (lifecycle, archiving, body rewrites),
        # not additions: appends, comments, and other property edits are open.
        rewrites_body = bool(args.body and not args.append)
        if item.is_task and item.frontmatter.get("claimed_by") and (
                rewrites_body or "status" in metadata or "archived" in metadata or "hidden" in metadata):
            check_claim(item, current_username(getattr(args, "by", None)))

        if properties == initial_properties and body == initial_body:
            receipt = saved_receipt(item)
            if args.as_json:
                auto_sync_after_mutation(changed=False)
                return MutationReceipt(receipt)
            print(f"No changes: {item.path.relative_to(REPO_ROOT)}")
            return 0

        timestamp = now_stamp()
        properties["updated"] = timestamp
        raw = card_format.render_document(properties, body, expected_kind=kind)
        if item.is_task and properties.get("status") != item.frontmatter.get("status"):
            raw = transition_claim(item, raw, str(properties["status"]), current_username(getattr(args, "by", None)), timestamp)
            properties, body = card_format.parse_document(raw, expected_kind=kind)
            properties["updated"] = timestamp
            raw = card_format.render_document(properties, body, expected_kind=kind)
        timer = None
        if item.is_task and (properties.get("status") != "doing"
                             or properties.get("archived") is True or properties.get("hidden") is True):
            raw, timer = finish_lifecycle_timer(item, raw, int(clock_time.time()))
        edited_item = save_item_edit(item, raw, args)
        cleanup_lifecycle_timer(item, timer)
    except (OSError, ValueError) as error:
        prefix = "conflict" if "conflict:" in str(error) else "invalid edit"
        print(f"error: {error}" if prefix == "conflict" else f"error: invalid edit: {error}", file=sys.stderr)
        return 1 if prefix == "conflict" else 2
    receipt = saved_receipt(edited_item, updated=True)
    if args.as_json:
        auto_sync_after_mutation(changed=True)
        return MutationReceipt(receipt)
    print(f"Edited and validated: {edited_item.path.relative_to(REPO_ROOT)}")
    return auto_sync_after_mutation(changed=True)

def item_receipt(item: Card, *, updated: bool = False) -> dict[str, object]:
    return {
        "ref": f"@{item.short_ref}",
        "path": str(item.path.relative_to(REPO_ROOT)),
        "content_hash": card_format.content_hash(item.raw),
        "updated": item.frontmatter.get("updated"),
        "changed": bool(updated),
    }


def saved_receipt(item: Card, *, updated: bool = False) -> dict[str, object]:
    """Receipt plus the saved card in the same fields as read --json."""
    return {**item_receipt(item, updated=updated), "properties": dict(item.frontmatter), "body": item.body}


def verify_edit_guards(item: Card, args: argparse.Namespace, original: bytes) -> None:
    expected_hash = getattr(args, "expected_hash", None)
    if expected_hash is not None and hashlib.sha256(original).hexdigest().casefold() != expected_hash.casefold():
        raise ValueError("conflict: item changed since it was loaded (content hash mismatch)")


def set_card_title(raw: str, title: str) -> str:
    normalized = title.strip()
    if not normalized:
        return raw
    if any(character in normalized for character in "\r\n"):
        raise ValueError("title must be single-line")
    properties, body = card_format.parse_document(raw)
    properties["title"] = normalized
    return card_format.render_document(properties, body)


def save_item_edit(item: Card, raw: str, args: argparse.Namespace) -> Card:
    base = demo_root() if demo_active() else REPO_ROOT
    relative = str(item.path.relative_to(base))
    original = item.path.read_bytes()
    verify_edit_guards(item, args, original)
    if original != item.raw.encode("utf-8"):
        raise ValueError("conflict: item changed since it was loaded (content changed)")
    apply_item_transaction({relative: raw.encode("utf-8")}, expected={relative: original}, source_root=base)
    return Card.read(item.path)


@serialized_time_mutation
def command_delete(args: argparse.Namespace) -> int:
    if not run_validator():
        return 1
    query = " ".join(args.query).strip()
    if not query:
        print("error: delete requires a non-empty query", file=sys.stderr)
        return 2
    task, result = resolve_unique(query, match_mode=args.match_mode)
    if task is None:
        return result
    if task.is_task:
        try:
            check_claim(task, current_username(getattr(args, "by", None)))
        except ValueError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        try:
            timers = read_time_timers()
        except ValueError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        task_id = str(task.frontmatter.get("id") or task.path.stem)
        if task_id in timers:
            print("error: a task with a running timer cannot be deleted", file=sys.stderr)
            return 1
    base = demo_root() if demo_active() else REPO_ROOT
    relative = task.path.relative_to(base).as_posix()
    original = task.path.read_bytes()
    try:
        apply_item_transaction({}, deletions={relative}, expected={relative: original}, source_root=base)
    except (OSError, ValueError) as error:
        print(f"error: delete failed validation; change rolled back: {error}", file=sys.stderr)
        return 1
    if args.as_json:
        receipt = item_receipt(task)
        receipt["deleted"] = True
        receipt["changed"] = True
        auto_sync_after_mutation()
        return MutationReceipt(receipt)
    print(f"Deleted: {task.path}")
    return auto_sync_after_mutation()


def command_show(args: argparse.Namespace) -> int:
    query = " ".join(args.query).strip()
    candidate = Path(query).expanduser()
    if not candidate.is_absolute():
        candidate = REPO_ROOT / candidate
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(REPO_ROOT / ".mt")
    except (FileNotFoundError, ValueError):
        resolved = None
    if resolved is not None and resolved.is_file() and resolved.suffix == ".md":
        command = [sys.executable, str(REPO_ROOT / "tools" / "mdviz.py"), str(resolved)]
        if args.out:
            command.extend(["--out", str(args.out)])
        if args.no_open:
            command.append("--no-open")
        return subprocess.run(command, check=False).returncode
    task, result = resolve_unique(query, match_mode=args.match_mode)
    if task is None:
        return result
    command = [sys.executable, str(REPO_ROOT / "tools" / "mdviz.py"), str(task.path)]
    if args.out:
        command.extend(["--out", str(args.out)])
    if args.no_open:
        command.append("--no-open")
    return subprocess.run(command, check=False).returncode


def item_read_record(task: Card) -> dict[str, object]:
    """Serialize one card as `mt read --json` does; `--full` on list and search reuses it."""
    return {
        "ref": f"@{task.short_ref}",
        "path": str(task.path.relative_to(REPO_ROOT)),
        "content_hash": card_format.content_hash(task.raw),
        "properties": dict(task.frontmatter),
        "body": task.body,
    }


def _read_item_data(task: Card, args: argparse.Namespace) -> tuple[dict[str, object] | None, str | None]:
    if args.as_json:
        return item_read_record(task), None
    if getattr(args, "body", False):
        return None, task.body
    return None, task.raw


def command_read(args: argparse.Namespace) -> int:
    is_multi_ref = len(args.query) > 1 and all(q.startswith("@") for q in args.query)
    if is_multi_ref:
        tasks = []
        for ref in args.query:
            task, result = resolve_unique(ref, match_mode="exact")
            if task is None:
                return result
            tasks.append(task)
        if args.as_json:
            records = []
            for task in tasks:
                rec, _ = _read_item_data(task, args)
                if rec is None:
                    return 2
                records.append(rec)
            print(json.dumps(records, ensure_ascii=False))
            return 0
        for task in tasks:
            _, text = _read_item_data(task, args)
            if text is None:
                return 2
            sys.stdout.write(text)
            if not text.endswith("\n"):
                sys.stdout.write("\n")
        return 0

    query = " ".join(args.query).strip()
    task, result = resolve_unique(query, match_mode=args.match_mode)
    if task is None:
        return result
    rec, text = _read_item_data(task, args)
    if rec is None and text is None:
        return 2
    if args.as_json:
        print(json.dumps(rec, ensure_ascii=False))
        return 0
    assert text is not None
    sys.stdout.write(text)
    return 0


def _bulk_edit_assignments(values: list[str] | dict[str, object]) -> dict[str, str]:
    """Parse and validate the metadata assignments shared by bulk edit plans."""
    assignments: dict[str, str] = {}
    entries = values.items() if isinstance(values, dict) else (
        (entry.partition("=")[0], entry.partition("=")[2]) for entry in values
    )
    for field, value in entries:
        if not isinstance(field, str) or not field or not isinstance(value, str) or not value:
            raise ValueError("--set requires non-empty FIELD=VALUE assignments")
        if field in assignments:
            raise ValueError(f"field '{field}' was assigned more than once")
        available = card_format.EDITABLE_FIELDS["task"] | card_format.EDITABLE_FIELDS["note"]
        if field not in available:
            raise ValueError(
                f"field '{field}' cannot be set; choose from "
                f"{', '.join(sorted(available))}"
            )
        if field in card_format.BOOLEAN_METADATA and value not in {"true", "false"}:
            raise ValueError(f"{field} must be true or false")
        assignments[field] = value
    if not assignments:
        raise ValueError("bulk-edit requires at least one --set FIELD=VALUE")
    return assignments


def _apply_bulk_metadata(raw: str, kind: str, assignments: dict[str, str], updated: str) -> str:
    frontmatter, body = card_format.parse_document(raw, expected_kind=kind)
    allowed = card_format.EDITABLE_FIELDS[kind]
    targets: dict[str, object] = {}
    for field, value in assignments.items():
        if field not in allowed:
            raise ValueError(f"field '{field}' cannot be set on a {kind}")
        if field == "status":
            if value == "doing":
                raise ValueError("status 'doing' requires claiming; use 'mt move doing' or 'mt claim'")
            if value == "done":
                raise ValueError("status 'done' requires completing; use 'mt done'")
            if value not in open_statuses():
                valid = [s for s in open_statuses() if s != "doing"]
                raise ValueError(f"status must be one of: {', '.join(valid)}")
            target = value
        elif field == "tags":
            target = parse_tag_json(value)
        elif field in card_format.BOOLEAN_METADATA:
            if value not in {"true", "false"}:
                raise ValueError(f"{field} must be true or false")
            target = value == "true"
        else:
            target = None if value == "null" else value
        targets[field] = target
    patched = card_format.apply_property_patch(frontmatter, targets, kind=kind)
    if patched == frontmatter:
        return raw
    if kind == "task" and patched.get("status") != frontmatter.get("status"):
        for field in CLAIM_FIELDS:
            patched.pop(field, None)
    patched["updated"] = updated
    return card_format.render_document(patched, body, expected_kind=kind)


def _bulk_scope(args: argparse.Namespace) -> tuple[str | None, list[str] | None] | None:
    """Return the (kind, task statuses) a bulk selection reads, or None after printing an error."""
    valid_statuses = list(policy().get("statuses", {}).keys())
    statuses = list(getattr(args, "statuses", []) or []) + list(getattr(args, "positional_statuses", []) or [])
    if args.kind == "note" and statuses:
        print("error: --kind note cannot be combined with task statuses", file=sys.stderr)
        return None
    if args.kind == "note" and args.type:
        print("error: --type cannot be combined with --kind note", file=sys.stderr)
        return None
    invalid = [group for group in statuses if group not in valid_statuses]
    if invalid:
        print(f"error: unknown status: {', '.join(invalid)}", file=sys.stderr)
        return None
    if statuses:
        return "task", list(dict.fromkeys(statuses))
    return ("task" if args.type else args.kind), None


def _bulk_has_scope(args: argparse.Namespace) -> bool:
    return bool(
        getattr(args, "statuses", []) or getattr(args, "positional_statuses", [])
        or args.kind or args.needs_review or args.tag or args.type
        or args.project or args.topic or args.since or args.created_since or args.created_before
        or args.updated_before or args.completed_since or args.completed_before
        or args.due_before or args.due_after or args.due or args.archived or args.hidden or args.all
    )


_BULK_SELECTION_FIELDS = (
    "statuses", "kind", "needs_review", "tag", "type", "project", "topic",
    "since", "created_since", "created_before", "updated_before", "completed_since", "completed_before",
    "due_before", "due_after", "due", "all", "include_archived", "archived", "include_hidden", "hidden",
)


def _bulk_selection(args: argparse.Namespace) -> dict[str, object]:
    statuses = list(getattr(args, "statuses", []) or []) + list(getattr(args, "positional_statuses", []) or [])
    normalized = {field: getattr(args, field, None) for field in _BULK_SELECTION_FIELDS}
    normalized["statuses"] = list(dict.fromkeys(statuses))
    for field in ("needs_review", "all", "include_archived", "archived", "include_hidden", "hidden"):
        normalized[field] = bool(normalized[field])
    return normalized


def _bulk_selection_args(selection: object) -> argparse.Namespace:
    if not isinstance(selection, dict) or set(selection) != set(_BULK_SELECTION_FIELDS):
        raise ValueError("bulk edit plan has an invalid selection predicate")
    statuses = selection["statuses"]
    if not isinstance(statuses, list) or any(not isinstance(status, str) for status in statuses):
        raise ValueError("bulk edit plan has invalid task statuses in its selection predicate")
    if selection["kind"] not in {None, "task", "note"}:
        raise ValueError("bulk edit plan has an invalid kind")
    if type(selection["needs_review"]) is not bool:
        raise ValueError("bulk edit plan has an invalid review filter")
    for field in ("all", "include_archived", "archived", "include_hidden", "hidden"):
        if type(selection[field]) is not bool:
            raise ValueError(f"bulk edit plan has an invalid {field.replace('_', '-')} filter")
    if selection["include_archived"] and selection["archived"]:
        raise ValueError("bulk edit plan has conflicting archive filters")
    if selection["include_hidden"] and selection["hidden"]:
        raise ValueError("bulk edit plan has conflicting visibility filters")
    for field in ("tag", "type", "project", "topic", "since", "created_since", "created_before",
                  "updated_before", "completed_since", "completed_before", "due_before", "due_after", "due"):
        value = selection[field]
        if value is not None and not isinstance(value, str):
            raise ValueError(f"bulk edit plan has an invalid {field.replace('_', '-')} filter")
    due = selection["due"]
    if due is not None and (not isinstance(due, str) or due not in {"overdue", "today", "week", "next"}):
        raise ValueError("bulk edit plan has an invalid due filter")
    return argparse.Namespace(**selection)


def _bulk_selected_items(args: argparse.Namespace) -> list[Card]:
    scope = _bulk_scope(args)
    if scope is None:
        raise ValueError("bulk edit plan has an invalid selection predicate")
    keep = metadata_matcher(args)
    if keep is None:
        raise ValueError("bulk edit plan has an invalid metadata filter")
    return [
        item for item in discover(*scope)
        if keep(item) and (not args.needs_review or item.frontmatter.get("needs_review") is True)
    ]


def _bulk_plan_item(item: Card, base: Path, assignments: dict[str, str], updated: str) -> tuple[dict[str, object], bytes | None]:
    kind = item.kind
    if item.path.is_symlink():
        raise ValueError(f"cannot edit a symlinked card: {item.path}")
    original = item.path.read_bytes()
    if original != item.raw.encode("utf-8"):
        raise ValueError(f"conflict: item changed during bulk preview: {item.path}")
    before = {field: item.frontmatter.get(field) for field in assignments}
    candidate = _apply_bulk_metadata(item.raw, kind, assignments, updated)
    after_frontmatter = card_format.parse_yaml(card_format.parse_frontmatter(candidate)[0] or "")
    after = {field: after_frontmatter.get(field) for field in assignments}
    changed = candidate.encode("utf-8") != original
    record: dict[str, object] = {
        "kind": kind,
        "path": item.path.relative_to(base).as_posix(),
        "ref": f"@{item.short_ref}",
        "title": item.title,
        "content_hash": hashlib.sha256(original).hexdigest(),
        "changed": changed,
        "before": before,
        "after": after,
    }
    return record, candidate.encode("utf-8") if changed else None


def _bulk_preview(args: argparse.Namespace) -> int:
    if not _bulk_has_scope(args):
        print("error: bulk-edit requires a scope such as a status, --kind, or metadata filter", file=sys.stderr)
        return 2
    try:
        assignments = _bulk_edit_assignments(args.assignments or [])
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if not run_validator():
        return 1
    base = demo_root() if demo_active() else REPO_ROOT
    try:
        matching = _bulk_selected_items(args)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if not matching:
        print("error: no cards match the bulk-edit selection", file=sys.stderr)
        return 2
    allowed = set.intersection(*(set(card_format.EDITABLE_FIELDS[item.kind]) for item in matching)) if matching else set()
    invalid_for_kind = sorted(set(assignments) - allowed) if matching else []
    if invalid_for_kind:
        kinds = ", ".join(sorted({item.kind for item in matching}))
        print(f"error: field(s) {', '.join(invalid_for_kind)} are not available on every selected {kinds} card", file=sys.stderr)
        return 2
    timestamp = now_stamp()
    try:
        records = []
        changes: dict[str, bytes] = {}
        for item in matching:
            record, contents = _bulk_plan_item(item, base, assignments, timestamp)
            records.append(record)
            if contents is not None:
                changes[str(Path(str(record["path"])).as_posix())] = contents
        if changes:
            validate_item_changes(changes, source_root=base)
    except (OSError, ValueError) as error:
        prefix = "conflict" if "conflict:" in str(error) else "invalid bulk edit"
        print(f"error: {error}" if prefix == "conflict" else f"error: {prefix}: {error}", file=sys.stderr)
        return 1 if prefix == "conflict" else 2
    plan = {
        "version": 1,
        "workspace": str(REPO_ROOT.resolve()),
        "data_root": "demo" if demo_active() else "workspace",
        "assignments": assignments,
        "selection": _bulk_selection(args),
        "selected": len(records),
        "changed": sum(bool(record["changed"]) for record in records),
        "unchanged": sum(not bool(record["changed"]) for record in records),
        "items": records,
    }
    if args.as_json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    changed = sum(bool(record["changed"]) for record in records)
    unchanged = len(records) - changed
    print(f"Bulk edit preview: {len(records)} selected; {changed} would change; {unchanged} already set.")
    for record in records:
        status = "change" if record["changed"] else "no change"
        details = ", ".join(
            f"{field}: {record['before'][field]!r} -> {record['after'][field]!r}"
            for field in assignments
        )
        print(f"  {record['ref']} {record['title']} ({status}; {details})")
    print("Save this preview with --json, review it, then run 'mt bulk-edit --apply PLAN.json'.")
    return 0


def _read_bulk_plan(path_text: str) -> dict[str, object]:
    if path_text == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(path_text).expanduser().read_text(encoding="utf-8")
    try:
        plan = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"bulk edit plan is not valid JSON: {error}") from error
    if not isinstance(plan, dict) or set(plan) != {
        "version", "workspace", "data_root", "assignments", "selection", "selected", "changed", "unchanged", "items"
    }:
        raise ValueError("bulk edit plan has an unsupported structure")
    if type(plan["version"]) is not int or plan["version"] != 1:
        raise ValueError("unsupported bulk edit plan version")
    if not isinstance(plan["workspace"], str) or not isinstance(plan["data_root"], str):
        raise ValueError("bulk edit plan has invalid workspace metadata")
    if plan["workspace"] != str(REPO_ROOT.resolve()):
        raise ValueError("bulk edit plan belongs to a different workspace")
    if plan["data_root"] not in {"demo", "workspace"}:
        raise ValueError("bulk edit plan has an invalid data root")
    expected_root = "demo" if demo_active() else "workspace"
    if plan["data_root"] != expected_root:
        raise ValueError("workspace data changed since preview; create a new bulk edit plan")
    assignments_raw = plan["assignments"]
    if not isinstance(assignments_raw, dict):
        raise ValueError("bulk edit plan assignments must be an object")
    assignments = _bulk_edit_assignments(assignments_raw)
    if not isinstance(plan["items"], list) or not plan["items"]:
        raise ValueError("bulk edit plan must contain at least one selected card")
    _bulk_selection_args(plan["selection"])
    if any(type(plan[field]) is not int or plan[field] < 0 for field in ("selected", "changed", "unchanged")):
        raise ValueError("bulk edit plan has invalid selection counts")
    records = plan["items"]
    if any(not isinstance(entry, dict) or type(entry.get("changed")) is not bool for entry in records):
        raise ValueError("bulk edit plan contains a malformed card record")
    changed_count = sum(entry["changed"] for entry in records)
    if (
        plan["selected"] != len(records)
        or plan["changed"] != changed_count
        or plan["unchanged"] != len(records) - changed_count
    ):
        raise ValueError("bulk edit plan selection counts do not match its card records")
    plan["assignments"] = assignments
    return plan


def _apply_bulk_plan_locked(args: argparse.Namespace) -> int:
    if not run_validator():
        return 1
    try:
        plan = _read_bulk_plan(args.apply_plan)
        assignments = plan["assignments"]
        assert isinstance(assignments, dict)
        selection = _bulk_selection_args(plan["selection"])
        base = demo_root() if demo_active() else REPO_ROOT
        current_items = _bulk_selected_items(selection)
        current_members = {item.path.relative_to(base).as_posix() for item in current_items}
        planned_members = {
            entry.get("path") for entry in plan["items"]
            if isinstance(entry, dict) and isinstance(entry.get("path"), str)
        }
        if current_members != planned_members:
            raise ValueError("conflict: bulk-edit selection membership changed since preview")
        expected: dict[str, bytes] = {}
        changes: dict[str, bytes] = {}
        receipts = []
        finished_timers = []
        seen: set[str] = set()
        timestamp = now_stamp()
        for entry in plan["items"]:
            if not isinstance(entry, dict) or set(entry) != {
                "kind", "path", "ref", "title", "content_hash", "changed", "before", "after"
            }:
                raise ValueError("bulk edit plan contains a malformed card record")
            kind, relative_text = entry["kind"], entry["path"]
            if not isinstance(kind, str) or kind not in {"task", "note"} or not isinstance(relative_text, str):
                raise ValueError("bulk edit plan contains a malformed card path or kind")
            relative = Path(relative_text)
            if (
                relative.is_absolute()
                or relative.as_posix() != relative_text
                or len(relative.parts) != 2
                or relative.parts[0] != "cards"
                or relative.suffix != ".md"
                or relative.name in {"", ".", ".."}
                or relative_text in seen
            ):
                raise ValueError(f"bulk edit plan contains an unsafe or duplicate path: {relative_text}")
            seen.add(relative_text)
            card_path = base / relative
            root = cards_root()
            try:
                resolved = card_path.resolve(strict=True)
                resolved.relative_to(root.resolve())
            except (OSError, ValueError) as error:
                raise ValueError(f"bulk edit plan path is outside the current {kind} cards: {relative_text}") from error
            if card_path.is_symlink() or resolved.parent != root.resolve():
                raise ValueError(f"bulk edit plan path is outside the current {kind} cards: {relative_text}")
            original = card_path.read_bytes()
            digest = hashlib.sha256(original).hexdigest()
            if not isinstance(entry["content_hash"], str) or digest != entry["content_hash"]:
                raise ValueError(f"conflict: {relative_text} changed since the bulk edit preview")
            item = Card.read(card_path)
            if entry["ref"] != f"@{item.short_ref}" or entry["title"] != item.title:
                raise ValueError(f"conflict: {relative_text} no longer matches the preview")
            if set(assignments) - set(card_format.EDITABLE_FIELDS[kind]):
                raise ValueError(f"bulk edit field is not available on {kind} {relative_text}")
            before = {field: item.frontmatter.get(field) for field in assignments}
            raw = _apply_bulk_metadata(item.raw, kind, assignments, timestamp)
            parsed = card_format.parse_yaml(card_format.parse_frontmatter(raw)[0] or "")
            after = {field: parsed.get(field) for field in assignments}
            changed = raw.encode("utf-8") != original
            if (
                type(entry["changed"]) is not bool
                or entry["changed"] is not changed
                or entry["before"] != before
                or entry["after"] != after
            ):
                raise ValueError(f"conflict: {relative_text} no longer matches the previewed metadata change")
            expected[relative_text] = original
            if changed:
                if item.is_task and set(assignments) & {"status", "archived", "hidden"}:
                    username = current_username(getattr(args, "by", None))
                    check_claim(item, username)
                    if parsed.get("status") != item.frontmatter.get("status"):
                        raw = transition_claim(item, raw, str(parsed["status"]), username, timestamp)
                    if parsed.get("status") != "doing" or parsed.get("archived") is True or parsed.get("hidden") is True:
                        raw, timer = finish_lifecycle_timer(item, raw, int(clock_time.time()))
                        finished_timers.append((item, timer))
                changes[relative_text] = raw.encode("utf-8")
            receipts.append({"ref": entry["ref"], "path": relative_text, "changed": changed})
        changed_count = len(changes)
        unchanged_count = len(receipts) - changed_count
        if (
            plan["selected"] != len(receipts)
            or plan["changed"] != changed_count
            or plan["unchanged"] != unchanged_count
        ):
            raise ValueError("bulk edit plan counts do not match the current cards")
        if changes:
            apply_item_transaction(changes, expected=expected, source_root=base)
            for item, timer in finished_timers:
                cleanup_lifecycle_timer(item, timer)
    except (OSError, ValueError) as error:
        prefix = "conflict" if "conflict:" in str(error) else "bulk edit failed validation; changes rolled back"
        print(f"error: {error}" if prefix == "conflict" else f"error: {prefix}: {error}", file=sys.stderr)
        return 1
    if args.as_json:
        auto_sync_after_mutation(changed=bool(changes))
        return MutationReceipt({
            "saved": True,
            "selected": len(receipts),
            "changed": changed_count,
            "unchanged": unchanged_count,
            "items": receipts,
        })
    print(f"Bulk edit applied: {changed_count} changed; {unchanged_count} already set.")
    auto_sync_after_mutation(changed=bool(changes))
    return 0


@serialized_time_mutation
def _command_bulk_edit_apply(args: argparse.Namespace) -> int:
    return _apply_bulk_plan_locked(args)


def command_bulk_edit(args: argparse.Namespace) -> int:
    args.statuses = list(getattr(args, "statuses", []) or [])
    args.positional_statuses = list(getattr(args, "positional_statuses", []) or [])
    if args.apply_plan is not None:
        if args.statuses or args.positional_statuses or args.kind or args.assignments or _bulk_has_scope(args):
            print("error: --apply cannot be combined with a new selection or --set", file=sys.stderr)
            return 2
        return _command_bulk_edit_apply(args)
    return _bulk_preview(args)


def metadata_matcher(args: argparse.Namespace, *, list_selection: bool = False):
    archived_only = getattr(args, "archived", False)
    include_archived = getattr(args, "include_archived", False)
    hidden_only = getattr(args, "hidden", False)
    include_hidden = getattr(args, "include_hidden", False)
    since = None
    list_date_ranges: dict[str, tuple[date | None, date | None]] = {}
    for field in ("created", "updated", "due", "completed"):
        since_text = getattr(args, f"{field}_since", None)
        if field == "updated" and since_text is None:
            since_text = getattr(args, "since", None)
        before_text = getattr(args, f"{field}_before", None)
        values: list[date | None] = []
        for option, value in ((f"--{field}-since", since_text), (f"--{field}-before", before_text)):
            if value is None:
                values.append(None)
                continue
            try:
                if not DATE_RE.fullmatch(value):
                    raise ValueError
                values.append(date.fromisoformat(value))
            except ValueError:
                print(f"error: {option} must be a valid date in YYYY-MM-DD format", file=sys.stderr)
                return None
        if values[0] is not None and values[1] is not None and values[0] >= values[1]:
            print(f"error: --{field}-since must be earlier than --{field}-before", file=sys.stderr)
            return None
        list_date_ranges[field] = (values[0], values[1])
    since = None
    today = date.today()
    now = now_stamp()
    due_before = getattr(args, "due_before", None)
    if due_before:
        try:
            if not is_date(due_before):
                raise ValueError
        except ValueError:
            print("error: --due-before must be a valid date in YYYY-MM-DD format", file=sys.stderr)
            return None
        due_before = f"{due_before}T00:00:00"
    due_after = getattr(args, "due_after", None) or getattr(args, "due_since", None)
    if due_after:
        try:
            if not is_date(due_after):
                raise ValueError
        except ValueError:
            print("error: --due-after must be a valid date in YYYY-MM-DD format", file=sys.stderr)
            return None
        due_after = f"{due_after}T00:00:00"
    due_mode = getattr(args, "due", None)
    if due_mode == "overdue":
        due_before = now
    elif due_mode == "today":
        due_after = f"{today.isoformat()}T00:00:00"
        due_before = f"{(today + timedelta(days=1)).isoformat()}T00:00:00"
    elif due_mode == "week":
        due_after = f"{today.isoformat()}T00:00:00"
        due_before = f"{(today + timedelta(days=7)).isoformat()}T00:00:00"
    elif due_mode == "next":
        due_before = f"{(today + timedelta(days=8)).isoformat()}T00:00:00"
    wanted_tag = args.tag.casefold() if args.tag else None
    supplied_type = getattr(args, "type", getattr(args, "task_type", None))
    wanted_type = supplied_type.casefold() if supplied_type else None
    wanted_project = args.project.casefold() if args.project else None
    wanted_topic = card_format.normalize_topic(args.topic) if args.topic else None

    def date_range_matches(task: Card) -> bool:
        for field, (since_day, before_day) in list_date_ranges.items():
            if since_day is None and before_day is None:
                continue
            value = task.frontmatter.get(field)
            if not isinstance(value, str) or len(value) < 10:
                return False
            try:
                item_day = date.fromisoformat(value[:10])
            except ValueError:
                return False
            if since_day is not None and item_day < since_day:
                return False
            if before_day is not None and item_day >= before_day:
                return False
        return True

    def list_visibility_matches(task: Card) -> bool:
        all_inventory = getattr(args, "all", False)
        archived = is_archived(task)
        hidden = is_hidden(task)
        if archived_only and not archived:
            return False
        if hidden_only and not hidden:
            return False
        if archived_only or hidden_only:
            return True
        return all_inventory or (include_archived or not archived) and (include_hidden or not hidden)

    def matches(task: Card) -> bool:
        tags = task.frontmatter.get("tags") or []
        due = task.frontmatter.get("due")
        due_value = due_sort_value(due) if due is not None else None
        return all(
            (
                wanted_tag is None or wanted_tag in [str(tag).casefold() for tag in tags],
                wanted_type is None or wanted_type == str(task.frontmatter.get("type") or task.kind).casefold(),
                wanted_project is None
                or wanted_project == str(task.frontmatter.get("project") or "").casefold(),
                wanted_topic is None
                or card_format.topic_within(task.frontmatter.get("topic"), wanted_topic),
                list_visibility_matches(task),
                date_range_matches(task),
                due_before is None or (due_value is not None and due_value < due_before),
                due_after is None or (due_value is not None and due_value >= due_after),
            )
        )

    return matches


def select_card_items(args: argparse.Namespace) -> tuple[list[Card], list[Card]] | None:
    """Apply the shared list/search metadata and collection selection."""
    valid_statuses = set(policy().get("statuses", {}))
    statuses = list(dict.fromkeys(args.statuses))
    invalid = [status for status in statuses if status not in valid_statuses]
    if invalid:
        print(f"error: unknown status: {', '.join(invalid)}", file=sys.stderr)
        return None
    if args.kind == "note" and statuses:
        print("error: --kind note cannot be combined with --status", file=sys.stderr)
        return None
    if args.type and args.kind == "note":
        print("error: --type cannot be combined with --kind note", file=sys.stderr)
        return None

    keep = metadata_matcher(args, list_selection=True)
    if keep is None:
        return None
    all_items = discover()
    selected: list[Card] = []
    for item in all_items:
        if args.kind == "task" and not item.is_task:
            continue
        if args.kind == "note" and not item.is_note:
            continue
        if statuses and (not item.is_task or item.frontmatter.get("status") not in statuses):
            continue
        if args.type and (
            not item.is_task
            or str(item.frontmatter.get("type") or "").casefold() != args.type.casefold()
        ):
            continue
        if args.needs_review and item.frontmatter.get("needs_review") is not True:
            continue
        if getattr(args, "triage", False):
            is_triage = (
                (item.is_task and (item.frontmatter.get("status") == "inbox" or item.frontmatter.get("needs_review") is True))
                or (item.is_note and item.frontmatter.get("needs_review") is True)
            )
            if not is_triage:
                continue
        if keep(item):
            selected.append(item)
    return all_items, selected


def sort_card_items(
    items: list[Card],
    *,
    field: str,
    order: str,
    priority_rank: dict[str, int],
    relevance_scores: dict[Path, float] | None = None,
) -> list[Card]:
    """Sort cards with nulls last and an ascending stable ID tie-break."""
    def sort_value(item: Card) -> object | None:
        if field == "relevance":
            return (relevance_scores or {}).get(item.path)
        value = item.title if field == "title" else item.frontmatter.get(field)
        if value is None or value == "":
            return None
        if field == "priority":
            return (-priority_rank.get(str(value), len(priority_rank)), str(value).casefold())
        if field == "title":
            return str(value).casefold()
        return str(value)

    sorted_by_id = sorted(items, key=lambda item: str(item.frontmatter.get("id") or item.path.stem))
    present = [item for item in sorted_by_id if sort_value(item) is not None]
    missing = [item for item in sorted_by_id if sort_value(item) is None]
    present.sort(key=sort_value, reverse=order == "desc")
    return present + missing


def command_list(args: argparse.Namespace) -> int:
    if args.limit is not None and args.limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    if args.tail is not None and args.tail < 1:
        print("error: --tail must be a positive integer", file=sys.stderr)
        return 2

    selection = select_card_items(args)
    if selection is None:
        return 2
    all_items, selected = selection
    reference_index = ReferenceIndex(all_items)
    priorities = list(policy().get("priorities", {}).keys())
    rank = {name: index for index, name in enumerate(priorities)}
    selected = sort_card_items(selected, field=args.sort, order=args.order, priority_rank=rank)
    if args.limit is not None:
        selected = selected[:args.limit]
    elif args.tail is not None:
        selected = selected[-args.tail:]

    if getattr(args, "compact", False):
        for item in selected:
            print(json.dumps(item_compact_json_record(item), ensure_ascii=False))
        return 0

    if args.full:
        print(json.dumps([item_read_record(item) for item in selected], ensure_ascii=False))
        return 0

    if args.json:
        print(json.dumps([item_json_record(item, reference_index) for item in selected],
                         ensure_ascii=False, indent=2))
        return 0

    for item in selected:
        kind = item.kind
        status = f" [{item.frontmatter['status']}]" if item.is_task else ""
        priority = str(item.frontmatter.get("priority") or "-")
        claim = f" [claimed: {item.frontmatter['claimed_by']}]" if item.frontmatter.get("claimed_by") else ""
        print(f"[@{item.short_ref}] [{kind}]{status} [{priority}] {item.title}{item_state_suffix(item)}{claim}")
    return 0


@serialized_time_mutation
def command_time_start(args: argparse.Namespace) -> int:
    if not run_validator():
        return 1
    query = " ".join(args.query).strip()
    if not query:
        print("error: time start requires a task", file=sys.stderr)
        return 2
    task, result = resolve_unique(query, statuses=open_statuses())
    if task is None:
        return result
    try:
        username = current_username(getattr(args, "by", None))
        check_claim(task, username)
        if is_archived(task) or is_hidden(task):
            raise ValueError("unarchive and unhide the task before starting a timer")
        task_id = str(task.frontmatter.get("id") or task.path.stem)
        timers = read_time_timers()
        if task_id in timers:
            raise ValueError(f"timer already running for [@{task.short_ref}] {task.title}")
        started_epoch = int(clock_time.time())
        timestamp = local_stamp(started_epoch)
        updated = task.raw
        if task.frontmatter.get("status") != "doing" or task.frontmatter.get("claimed_by") != username:
            updated = replace_frontmatter_field(updated, "status", "doing")
            updated = replace_frontmatter_field(updated, "updated", timestamp)
            updated = transition_claim(task, updated, "doing", username, timestamp)
        base = demo_root() if demo_active() else REPO_ROOT
        relative = str(task.path.relative_to(base))
        changes = {relative: updated.encode("utf-8")}
        expected = {relative: task.raw.encode("utf-8")}
        if not getattr(args, "parallel", False):
            for other_id, other_timer in list(timers.items()):
                if other_timer["by"] != username:
                    continue
                other = task_by_id(other_id)
                if other is None:
                    raise ValueError(f"running timer task no longer exists: {other_id}; use mt time cancel {other_id}")
                entry, already_saved = time_session_for(other_id, other_timer, started_epoch)
                if not already_saved:
                    other_raw = append_time_entry(other.raw, entry)
                    other_raw = replace_frontmatter_field(other_raw, "updated", timestamp)
                    other_path = str(other.path.relative_to(base))
                    changes[other_path] = other_raw.encode("utf-8")
                    expected[other_path] = other.raw.encode("utf-8")
                del timers[other_id]
        timer = {"session_id": uuid.uuid4().hex, "start": timestamp,
                 "start_epoch": started_epoch, "by": username}
        apply_item_transaction(changes, expected=expected, source_root=base)
        try:
            timers[task_id] = timer
            write_time_timers(timers)
        except OSError:
            apply_item_transaction(expected, expected=changes, source_root=base)
            raise
    except (OSError, ValueError) as error:
        print(f"error: could not start timer: {error}", file=sys.stderr)
        return 1
    print(f"Started timer for [@{task.short_ref}] {task.title} at {timer['start']} ({username}).")
    return auto_sync_after_mutation(changed=any(content != expected[path] for path, content in changes.items()))


def command_time_status(_args: argparse.Namespace) -> int:
    if not run_validator():
        return 1
    try:
        timers = read_time_timers()
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if not timers:
        print("No timer running.")
        return 0
    for task_id, timer in sorted(timers.items()):
        task = task_by_id(task_id)
        label = f"[@{task.short_ref}] {task.title}" if task else f"missing task {task_id}"
        already_saved = timer_session_saved(task_id, timer)
        elapsed = 0 if already_saved else max(0, int(clock_time.time()) - int(timer["start_epoch"]))
        print(f"Timer: {label}")
        print(f"  owner:   {timer['by']}")
        print(f"  started: {timer['start']}")
        if not already_saved and elapsed > float(policy().get("rules", {}).get("timer_warn_hours", 8)) * 3600:
            print("  warning: long-running timer; use time stop --at TIME or --duration DURATION to correct it")
        print(f"  elapsed: {format_duration(elapsed)}" + (" (session already saved)" if already_saved else ""))
        if task:
            print(f"  saved:   {format_duration(saved_time_seconds(task))}")
    return 0


@serialized_time_mutation
def command_time_end(args: argparse.Namespace) -> int:
    if not run_validator():
        return 1
    try:
        timers = read_time_timers()
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if not timers:
        print("error: no timer is running", file=sys.stderr)
        return 1
    query = " ".join(getattr(args, "query", [])).strip()
    if query in timers:
        task_id, timer = query, timers[query]
    elif query:
        task, result = resolve_unique(query, kind="task")
        if task is None:
            return result
        task_id = str(task.frontmatter.get("id") or task.path.stem)
        timer = timers.get(task_id)
        if timer is None:
            print(f"error: no timer is running for [@{task.short_ref}] {task.title}", file=sys.stderr)
            return 1
    elif len(owned := {key: value for key, value in timers.items()
                       if value["by"] == current_username(getattr(args, "by", None))}) == 1:
        task_id, timer = next(iter(owned.items()))
    elif not owned:
        print("error: no timer is running for this user; specify a task", file=sys.stderr)
        return 1
    else:
        refs = []
        for running_task_id in sorted(owned):
            running_task = task_by_id(running_task_id)
            refs.append(f"@{running_task.short_ref}" if running_task else running_task_id)
        print(
            "error: multiple timers are running; specify a task: " + ", ".join(refs),
            file=sys.stderr,
        )
        return 1
    task = task_by_id(task_id)
    if timer["by"] != current_username(getattr(args, "by", None)):
        print(f"error: timer belongs to {timer['by']}; use --by {shlex.quote(str(timer['by']))}", file=sys.stderr)
        return 1
    if task is None and args.time_command != "cancel":
        print(f"error: running timer task no longer exists: {task_id}; use mt time cancel {task_id}", file=sys.stderr)
        return 1
    if args.time_command == "cancel":
        try:
            clear_time_timer(task_id, str(timer["session_id"]))
        except OSError as error:
            print(f"error: could not cancel timer: {error}", file=sys.stderr)
            return 1
        label = f"[@{task.short_ref}] {task.title}" if task else task_id
        print(f"Cancelled timer for {label}; no time recorded.")
        return 0
    try:
        end_epoch = int(clock_time.time())
        if getattr(args, "at", None):
            end_epoch = parse_time_argument(args.at)
        elif getattr(args, "duration", None):
            end_epoch = int(timer["start_epoch"]) + parse_duration(args.duration)
        if end_epoch < int(timer["start_epoch"]) or end_epoch > int(clock_time.time()):
            raise ValueError("timer end must be between its start and now")
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    elapsed = max(0, end_epoch - int(timer["start_epoch"]))
    saved, message = save_time_session(task, timer, end_epoch)
    if not saved:
        print(f"error: {message}", file=sys.stderr)
        return 1
    # The card is durable even if clearing local timer state fails. Queue sync
    # now so the wrapper runs it after releasing the mutation lock.
    auto_sync_after_mutation()
    try:
        if not clear_time_timer(task_id, str(timer["session_id"])):
            print("error: timer state changed after saving; retry stop safely", file=sys.stderr)
            return 1
    except OSError as error:
        print(f"error: session saved, but timer cleanup failed ({error}); retry stop safely", file=sys.stderr)
        return 1
    if message == "already saved":
        print("Timer session was already saved; cleared the running state.")
    else:
        print(f"Stopped timer for [@{task.short_ref}] {task.title}: {format_duration(elapsed)}.")
    return 0


def command_time_list(args: argparse.Namespace) -> int:
    if not run_validator():
        return 1
    query = " ".join(args.query).strip()
    if query:
        task, result = resolve_unique(query, kind="task")
        if task is None:
            return result
        tasks = [task]
    else:
        tasks = discover("task")
    sessions = [(task, entry) for task in tasks for entry in time_log_entries(task.raw)]
    if not sessions:
        print("No recorded sessions.")
        return 0
    for task, entry in sorted(sessions, key=lambda pair: (pair[1]["start"], pair[1]["session_id"]), reverse=True):
        print(f"{entry['session_id'][:8]}  {entry['start']} → {entry['end']}  "
              f"{format_duration(int(entry['elapsed_seconds']))}  [@{task.short_ref}] {task.title}")
    return 0


@serialized_time_mutation
def command_time_remove(args: argparse.Namespace) -> int:
    if not run_validator():
        return 1
    session_ref = args.session_id.lower()
    if not re.fullmatch(r"[0-9a-f]{8,32}", session_ref):
        print("error: use a session ID from 'mt time list' (at least 8 hexadecimal characters)", file=sys.stderr)
        return 2
    matches = [
        (task, entry)
        for task in discover("task")
        for entry in time_log_entries(task.raw)
        if str(entry["session_id"]).startswith(session_ref)
    ]
    if not matches:
        print(f"error: no recorded session matches '{session_ref}'", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print("error: multiple sessions match; use a longer session ID:", file=sys.stderr)
        for task, entry in matches:
            print(f"  {entry['session_id']}  [@{task.short_ref}] {task.title}", file=sys.stderr)
        return 2
    task, entry = matches[0]
    session_id = str(entry["session_id"])
    properties, body = card_format.parse_document(task.raw, expected_kind="task")
    entries = properties.get("time_entries", [])
    if not isinstance(entries, list):
        print("error: task time_entries property is malformed", file=sys.stderr)
        return 1
    properties["time_entries"] = [entry for entry in entries
                                  if not isinstance(entry, dict) or entry.get("session_id") != session_id]
    if not properties["time_entries"]:
        properties.pop("time_entries")
    updated = card_format.render_document(properties, body, expected_kind="task")
    updated = replace_frontmatter_field(updated, "updated", now_stamp())
    original = task.path.read_bytes()
    base = demo_root() if demo_active() else REPO_ROOT
    relative = str(task.path.relative_to(base))
    try:
        apply_item_transaction({relative: updated.encode("utf-8")}, expected={relative: original}, source_root=base)
    except (OSError, ValueError) as error:
        print(f"error: session removal failed validation; task was restored: {error}", file=sys.stderr)
        return 1
    print(f"Removed session {session_id} ({format_duration(int(entry['elapsed_seconds']))}) "
          f"from [@{task.short_ref}] {task.title}.")
    return auto_sync_after_mutation()


@serialized_time_mutation
def command_time_add(args: argparse.Namespace) -> int:
    if not run_validator():
        return 1
    try:
        end_epoch = parse_time_argument(args.end) if getattr(args, "end", None) else int(clock_time.time())
        if getattr(args, "start", None):
            if args.duration:
                raise ValueError("use either DURATION or --start, not both")
            start_epoch = parse_time_argument(args.start)
            seconds = end_epoch - start_epoch
        else:
            if not args.duration:
                raise ValueError("provide DURATION or --start TIME")
            seconds = parse_duration(args.duration)
            start_epoch = end_epoch - seconds
        if seconds <= 0 or end_epoch > int(clock_time.time()):
            raise ValueError("session start must precede end, and end cannot be in the future")
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    task, result = resolve_unique(args.query, kind="task")
    if task is None:
        return result
    timer = {
        "session_id": uuid.uuid4().hex,
        "task_id": str(task.frontmatter.get("id") or task.path.stem),
        "start": local_stamp(start_epoch),
        "start_epoch": start_epoch,
    }
    saved, message = save_time_session(task, timer, end_epoch)
    if not saved:
        print(f"error: {message}", file=sys.stderr)
        return 1
    print(f"Added {format_duration(seconds)} to [@{task.short_ref}] {task.title}.")
    return auto_sync_after_mutation()


def command_time_report(args: argparse.Namespace) -> int:
    if not run_validator():
        return 1
    tasks = discover("task")
    grouped: dict[str, int] = {}
    labels: dict[str, list[str]] = {}
    for task in tasks:
        seconds = saved_time_seconds(task)
        if not seconds:
            continue
        if args.by == "task":
            key = f"[@{task.short_ref}] {task.title}"
        else:
            value = task.frontmatter.get(args.by)
            key = str(value) if value else f"(no {args.by})"
            labels.setdefault(key, []).append(task.title)
        grouped[key] = grouped.get(key, 0) + seconds
    if not grouped:
        print("No tracked time yet.")
        return 0
    for key in sorted(grouped, key=str.casefold):
        suffix = f" ({len(labels.get(key, []))} task(s))" if args.by != "task" else ""
        print(f"{key}: {format_duration(grouped[key])}{suffix}")
    print(f"Total: {format_duration(sum(grouped.values()))}")
    return 0


def workspace_topic_counts(cards: list[Card]) -> dict[str, int]:
    """Cards per exact topic path, counting every card including hidden and archived."""
    counts: dict[str, int] = {}
    for card in cards:
        topic = card.frontmatter.get("topic")
        if isinstance(topic, str) and topic:
            counts[topic] = counts.get(topic, 0) + 1
    return counts


def suggested_topics(cfg: dict) -> list[str]:
    values = cfg.get("suggested_topics")
    return [card_format.normalize_topic(value) for value in values
            if isinstance(value, str) and card_format.topic_path_error(value) is None] if isinstance(values, list) else []


def workspace_topic_tree(cards: list[Card], cfg: dict) -> list[dict[str, object]]:
    return card_format.topic_tree(workspace_topic_counts(cards), suggested_topics(cfg))


def command_topic_list(args: argparse.Namespace) -> int:
    if not run_validator():
        return 1
    tree = workspace_topic_tree(discover(), policy())
    start, end, depth = 0, len(tree), 0  # `tree` is depth-first, so a topic's subtree is the run after it
    if args.topic:
        # Accept "learning/" as printed by the one-level listing.
        wanted = card_format.topic_key(args.topic.strip().rstrip(card_format.TOPIC_SEPARATOR))
        found = [index for index, node in enumerate(tree) if card_format.topic_key(str(node["path"])) == wanted]
        if not found:
            print(f"error: unknown topic: {args.topic}", file=sys.stderr)
            return 1
        start, depth = found[0], int(tree[found[0]]["depth"])
        end = start + 1
        while end < len(tree) and int(tree[end]["depth"]) > depth:
            end += 1
    subtree = tree[start:end]  # the topic itself (when given) and everything below it
    if args.as_json:
        print(json.dumps({"topics": subtree}, ensure_ascii=False))
        return 0
    if args.paths:
        for node in subtree:
            print(node["path"])
        return 0
    if not tree:
        print("No topics yet. Set one with 'mt edit REF --topic PATH'.")
        return 0
    color = sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"

    def line(prefix: str, label: str, index: int) -> str:
        node = subtree[index]
        has_children = index + 1 < len(subtree) and int(subtree[index + 1]["depth"]) > int(node["depth"])
        text = f"{prefix}{label}{'/' if has_children and not args.tree else ''} ({node['count']})"
        return f"\033[2m{text}\033[0m" if color and not node["count"] else text

    if not args.tree:
        level = depth + 1 if args.topic else 0  # one level: the roots, or the children of TOPIC
        for index, node in enumerate(subtree):
            if int(node["depth"]) == level:
                print(line("", str(node["name"]), index))
        return 0
    # --tree: a node is last when no later node sits at its depth before the subtree climbs higher.
    base = depth if args.topic else 0
    for index, node in enumerate(subtree):
        level = int(node["depth"]) - base
        if level == 0:
            print(line("", str(node["path"]) if args.topic else str(node["name"]), index))
            continue
        siblings_after = [later for later in subtree[index + 1:]
                          if int(later["depth"]) <= int(node["depth"])]
        is_last = not siblings_after or int(siblings_after[0]["depth"]) < int(node["depth"])
        prefix = ""
        for ancestor_level in range(1, level):
            ancestor_depth = base + ancestor_level
            later = [item for item in subtree[index + 1:] if int(item["depth"]) <= ancestor_depth]
            prefix += "│   " if later and int(later[0]["depth"]) == ancestor_depth else "    "
        print(line(prefix + ("└── " if is_last else "├── "), str(node["name"]), index))
    return 0


def topic_rename_plan(old: str, new: str, merge: bool) -> list[tuple[Card, str]]:
    """Cards to move and their new topics, or ValueError when the rename is not allowed."""
    for label, value in (("OLD", old), ("NEW", new)):
        problem = card_format.topic_path_error(value)
        if problem:
            raise CardInputError(f"{label} topic {problem}")
    old, new = card_format.normalize_topic(old), card_format.normalize_topic(new)
    old_key, new_key = card_format.topic_key(old), card_format.topic_key(new)
    if new == old:
        raise CardInputError("OLD and NEW are the same topic")
    if len(new_key) > len(old_key) and new_key[:len(old_key)] == old_key:
        raise CardInputError(f"cannot move topic '{old}' into its own subtree '{new}'")
    cards = discover()
    affected = [card for card in cards if card_format.topic_within(card.frontmatter.get("topic"), old)]
    if not affected:
        raise CardInputError(f"no cards use topic '{old}' or anything under it")
    if new_key != old_key and not merge:
        moving = {card.path for card in affected}
        existing = [card for card in cards if card.path not in moving
                    and card_format.topic_within(card.frontmatter.get("topic"), new)]
        if existing or any(card_format.topic_key(item) == new_key for item in suggested_topics(policy())):
            raise CardInputError(f"topic '{new}' already exists; use --merge to combine into it")
    plan = []
    for card in affected:
        parts = str(card.frontmatter["topic"]).split(card_format.TOPIC_SEPARATOR)
        rest = parts[len(old_key):]
        plan.append((card, card_format.TOPIC_SEPARATOR.join([new, *rest])))
    return plan


@serialized_time_mutation
def command_topic_rename(args: argparse.Namespace) -> int:
    if not run_validator():
        return 1
    try:
        plan = topic_rename_plan(args.old, args.new, args.merge)
        username = current_username(getattr(args, "by", None))
        claimed = []
        for card, _new_topic in plan:
            try:
                check_claim(card, username)
            except ValueError:
                claimed.append(f"@{card.short_ref} (claimed by {card.frontmatter.get('claimed_by')})")
        if claimed:
            raise CardInputError(
                f"cannot rename: claimed by someone else: {', '.join(claimed)}. "
                f"Release the claim or pass --by with the claiming username; nothing was changed")
        base = demo_root() if demo_active() else REPO_ROOT
        timestamp = now_stamp()
        changes: dict[str, bytes] = {}
        expected: dict[str, bytes | None] = {}
        records = []
        for card, new_topic in plan:
            relative = card.path.relative_to(base).as_posix()
            if card.path.is_symlink():
                raise CardInputError(f"cannot edit a symlinked card: {card.path}")
            original = card.path.read_bytes()
            if original != card.raw.encode("utf-8"):
                raise CardInputError(f"conflict: card changed while planning: {relative}")
            records.append({"ref": f"@{card.short_ref}", "path": relative, "title": card.title,
                            "from": card.frontmatter["topic"], "to": new_topic})
            if card.frontmatter["topic"] == new_topic:
                continue
            properties, body = card_format.parse_document(card.raw, expected_kind=card.kind)
            patched = card_format.apply_property_patch(properties, {"topic": new_topic}, kind=card.kind)
            patched["updated"] = timestamp
            changes[relative] = card_format.render_document(patched, body, expected_kind=card.kind).encode("utf-8")
            expected[relative] = original
        if not args.dry_run and changes:
            apply_item_transaction(changes, expected=expected, source_root=base)
    except CardInputError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print(f"error: topic rename failed; changes rolled back: {error}", file=sys.stderr)
        return 1
    saved = not args.dry_run
    if args.as_json:
        payload = {"saved": saved, "dry_run": args.dry_run, "selected": len(records),
                   "changed": len(changes), "items": records}
        if saved:
            auto_sync_after_mutation(changed=bool(changes))
            return MutationReceipt(payload)
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    verb = "Would move" if args.dry_run else "Moved"
    for record in records:
        print(f"{record['ref']}  {record['from']} -> {record['to']}  {record['title']}")
    print(f"{verb} {len(changes)} card(s) from '{card_format.normalize_topic(args.old)}' to '{card_format.normalize_topic(args.new)}'.")
    if any(card_format.topic_key(item)[:len(card_format.topic_key(args.old))] == card_format.topic_key(args.old)
           for item in suggested_topics(policy())):
        print("Note: suggested_topics in the workspace policy still lists the old name; update it by hand.")
    return auto_sync_after_mutation(changed=bool(changes)) if saved else 0


def command_focus(args: argparse.Namespace) -> int:
    query = " ".join(getattr(args, "query", []) or []).strip()
    set_query = (getattr(args, "set_query", None) or "").strip()
    has_filters = any(
        getattr(args, name, None)
        for name in ("tag", "type", "project", "topic", "since", "due_before", "due_after", "due")
    )
    if query and set_query:
        print("error: use either a focus query or --set, not both", file=sys.stderr)
        return 2
    if args.next and (query or set_query):
        print("error: --next cannot be combined with a focus query or --set", file=sys.stderr)
        return 2
    if args.clear and (query or set_query):
        print("error: --clear cannot be combined with a focus query or --set", file=sys.stderr)
        return 2
    if args.clear and args.next:
        print("error: --clear cannot be combined with --next", file=sys.stderr)
        return 2
    if (query or set_query or args.clear) and has_filters:
        print("error: set and clear focus operations cannot be combined with filters", file=sys.stderr)
        return 2
    keep = metadata_matcher(args)
    if keep is None:
        return 2
    actor = current_username(getattr(args, "by", None))
    if args.clear:
        clear_focus_task_id()
    elif query or set_query:
        task, result = resolve_unique(query or set_query, statuses=open_statuses())
        if task is None:
            return result
        if task.frontmatter.get("status") not in {"inbox", "active", "doing"}:
            print("error: focus requires an inbox, active, or doing task; read blocked or deferred work first", file=sys.stderr)
            return 2
        check_claim(task, actor)
        write_focus_task_id(str(task.frontmatter.get("id") or task.path.stem))
    open_tasks = [task for task in discover(statuses=open_statuses()) if keep(task)]
    context = focus_context(open_tasks, actor=actor)
    candidates = context["candidates"]
    if args.next:
        if candidates:
            current_id = context["selected"]["id"]
            current_index = next(index for index, item in enumerate(candidates) if item["id"] == current_id)
            selected_task = candidates[(current_index + 1) % len(candidates)]
            write_focus_state(str(selected_task["id"]), "cycle")
            context = focus_context(open_tasks, actor=actor)
    if getattr(args, "json", False):
        print(json.dumps(context, ensure_ascii=False, indent=2))
        return 0
    selected = context["selected"]
    if selected:
        status = str(selected["status"] or "")
        command = RESTART_COMMANDS.get(status, RESTART_COMMANDS["default"]).format(ref=selected["ref"])
        print("Focus:")
        print(f"  [{selected['ref']}] [{selected['priority']:<7}] {selected['title']}")
        print(f"       due:     {selected['due'] or '-'}")
        print(f"       next:    {selected['next_action'] or '-'}")
        print(f"       why:     {selected['reason']}")
        print(f"       restart: {command}")
    else:
        print("No open tasks to focus on.")
    alerts = [item for item in context["alerts"] if not selected or item["id"] != selected["id"]]
    if alerts:
        print(f"\nDeadlines to check ({len(alerts)}):")
        for item in alerts:
            print(f"  [{item['ref']}] {item['due']} | {item['title']} | {item['action_label']}")
    return 0


def command_unfocus(args: argparse.Namespace) -> int:
    clear_focus_task_id()
    return command_focus(
        argparse.Namespace(
            query=[],
            set_query=None,
            clear=False,
            next=False,
            tag=None,
            type=None,
            project=None,
            topic=None,
            since=None,
            due_before=None,
            due_after=None,
            due=None,
            include_archived=False,
            archived=False,
        )
    )


def command_followup(args: argparse.Namespace) -> int:
    cfg = policy()
    priorities = list(cfg.get("priorities", {}).keys())
    rank = {name: index for index, name in enumerate(priorities)}
    tasks = [task for task in discover(statuses=["waiting"]) if not is_archived(task) and not is_hidden(task)]
    tasks = sorted(tasks, key=lambda task: followup_sort_key(task, rank))
    if not tasks:
        print("No waiting follow-ups.")
        return 0
    print(f"Follow-ups ({len(tasks)} waiting):\n")
    for task in tasks:
        priority = str(task.frontmatter.get("priority") or "medium")
        due = str(task.frontmatter.get("due") or "-")
        ref = f"@{task.short_ref}"
        print(f"  [{ref}] [{priority:<7}] {task.title}")
        print(f"       due:  {due}")
        print(f"       next: {task.next_action or '-'}")
        print(f"       cmds: mt read {ref}")
    return 0


def command_search(args: argparse.Namespace) -> int:
    query = " ".join(args.query).strip()
    if not query:
        print("error: search requires a non-empty query", file=sys.stderr)
        return 2
    if args.limit is not None and args.limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    if args.tail is not None and args.tail < 1:
        print("error: --tail must be a positive integer", file=sys.stderr)
        return 2
    try:
        selection = select_card_items(args)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if selection is None:
        return 2
    all_items, selected = selection
    try:
        scored_matches = find_search_matches(selected, query, args.match_mode)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    relevance_scores = {item.path: score for score, item in scored_matches}
    priorities = list(policy().get("priorities", {}).keys())
    priority_rank = {name: index for index, name in enumerate(priorities)}
    results = sort_card_items(
        [item for _score, item in scored_matches], field=args.sort, order=args.order,
        priority_rank=priority_rank, relevance_scores=relevance_scores,
    )
    if args.limit is not None:
        results = results[:args.limit]
    elif args.tail is not None:
        results = results[-args.tail:]

    if getattr(args, "compact", False):
        for item in results:
            print(json.dumps(item_compact_json_record(item), ensure_ascii=False))
        return 0

    if args.full:
        print(json.dumps([item_read_record(item) for item in results], ensure_ascii=False))
        return 0

    if args.json:
        index = ReferenceIndex(all_items)
        print(json.dumps([item_json_record(item, index) for item in results], ensure_ascii=False, indent=2))
        return 0

    for item in results:
        kind = item.kind
        status = f" [{item.frontmatter['status']}]" if item.is_task else ""
        priority = str(item.frontmatter.get("priority") or "-")
        claim = f" [claimed: {item.frontmatter['claimed_by']}]" if item.frontmatter.get("claimed_by") else ""
        fuzzy_suffix = f" (fuzzy {relevance_scores[item.path]:.0%})" if args.match_mode == "fuzzy" else ""
        print(f"[@{item.short_ref}] [{kind}]{status} [{priority}] {item.title}"
              f"{item_state_suffix(item)}{claim}{fuzzy_suffix}")
    return 0


def add_match_mode_arguments(command: argparse.ArgumentParser, allow_fuzzy: bool = False) -> None:
    command.description = (
        "Match task title, ID, and body with an exact case-insensitive substring by default. "
        "Use --and to require every word or --or to match any word. "
        "Use @ followed by a unique 3 to 8 character hexadecimal prefix to select a short reference."
    )
    if allow_fuzzy:
        command.description += " Use --fuzzy for close word matches."
    modes = command.add_mutually_exclusive_group()
    modes.add_argument("--and", dest="match_mode", action="store_const", const="and", help="require every word to match")
    modes.add_argument("--or", dest="match_mode", action="store_const", const="or", help="match any word")
    if allow_fuzzy:
        modes.add_argument("--fuzzy", dest="match_mode", action="store_const", const="fuzzy", help="allow close word matches")
    command.set_defaults(match_mode="exact")


def add_card_selection_arguments(command: argparse.ArgumentParser) -> None:
    """Register the shared collection and metadata filters for list and search."""
    command.add_argument("--all", action="store_true",
                         help="include every task and note, including hidden and archived cards")
    command.add_argument("--kind", choices=("task", "note"), help="select tasks or notes")
    command.add_argument("--status", dest="statuses", action="append", default=[], metavar="STATUS",
                         help="select a task status; repeat to select several statuses")
    command.add_argument("--needs-review", action="store_true", help="select cards marked for review")
    command.add_argument("--triage", action="store_true",
                         help="select cards needing triage (inbox tasks and notes needing review)")
    command.add_argument("--project", help="exact case-insensitive project match")
    command.add_argument("--topic", metavar="PATH",
                         help="select this topic and everything under it (case-insensitive, whole segments)")
    command.add_argument("--tag", help="exact case-insensitive tag match")
    command.add_argument("--type", "--task-type", dest="type",
                         help="select tasks of this type (alias: --task-type)")
    for field in ("created", "updated", "due", "completed"):
        since_flags = ("--updated-since", "--since") if field == "updated" else (f"--{field}-since",)
        command.add_argument(*since_flags, dest=f"{field}_since", metavar="YYYY-MM-DD",
                             help=f"include cards with {field} on or after this calendar day")
        command.add_argument(f"--{field}-before", metavar="YYYY-MM-DD",
                             help=f"include cards with {field} before this calendar day")
    command.add_argument("--due-after", dest="due_since", metavar="YYYY-MM-DD",
                         help="alias for --due-since; include cards due on or after this date")
    command.add_argument("--archived", action="store_true", help="select archived cards, including hidden ones")
    command.add_argument("--hidden", action="store_true", help="select hidden cards, including archived ones")


def add_metadata_filter_arguments(
    command: argparse.ArgumentParser,
    *,
    item_label: str = "tasks",
    include_project: bool = True,
    collection: bool = False,
) -> None:
    command.add_argument("--tag")
    command.add_argument("--type", "--task-type", dest="type", help="task type (alias: --task-type)")
    if include_project:
        command.add_argument("--project")
    command.add_argument("--topic", metavar="PATH", help="this topic and everything under it")
    if collection:
        command.add_argument("--kind", choices=("task", "note"), help="select tasks or notes")
        command.add_argument("--status", dest="statuses", action="append", default=[], metavar="STATUS",
                             help="select a task status; repeat to select several")
        command.add_argument("--needs-review", action="store_true", help=f"select {item_label} marked for review")
    command.add_argument(
        "--since", "--updated-since", dest="since",
        metavar="YYYY-MM-DD",
        help=f"include {item_label} updated on or after this date",
    )
    for field in ("created", "updated", "completed"):
        if field == "updated":
            command.add_argument("--updated-before", metavar="YYYY-MM-DD",
                                 help=f"include {item_label} updated before this date (exclusive)")
            continue
        command.add_argument(f"--{field}-since", metavar="YYYY-MM-DD",
                             help=f"include {item_label} {field} on or after this date")
        command.add_argument(f"--{field}-before", metavar="YYYY-MM-DD",
                             help=f"include {item_label} {field} before this date (exclusive)")
    command.add_argument(
        "--due-before",
        metavar="YYYY-MM-DD",
        help=f"include {item_label} due before this calendar day (exclusive)",
    )
    command.add_argument(
        "--due-after", "--due-since", dest="due_after",
        metavar="YYYY-MM-DD",
        help=f"include {item_label} due on or after this date",
    )
    command.add_argument(
        "--due",
        choices=("overdue", "today", "week", "next"),
        help="shortcut due filter: overdue, today, week, or next 7 days including overdue",
    )
    archive = command.add_mutually_exclusive_group()
    archive.add_argument("--include-archived", action="store_true", help=f"include archived {item_label}")
    archive.add_argument("--archived", action="store_true", help=f"show only archived {item_label}")
    visibility = command.add_mutually_exclusive_group()
    visibility.add_argument("--include-hidden", action="store_true", help=f"include hidden {item_label}")
    visibility.add_argument("--hidden", action="store_true", help=f"show only hidden {item_label}")
    command.add_argument("--all", action="store_true",
                         help="include hidden and archived cards in the selected collection")


def add_card_metadata_arguments(command: argparse.ArgumentParser, *, editing: bool) -> None:
    """Expose the same shared metadata names on create and edit commands."""
    command.add_argument("--project", help="set project metadata")
    command.add_argument("--topic", metavar="PATH",
                         help="set topic: where the card lives, a '/'-separated path such as learning/rust")
    command.add_argument("--priority", help="set priority metadata")
    command.add_argument("--due", help="set due date or local timestamp")
    command.add_argument("--due-kind", help="set the meaning of --due")
    command.add_argument("--tag", dest="tags" if not editing else "tag_add", action="append",
                         default=[] if not editing else None,
                         help="add a tag (repeatable; creation and edit)")
    if not editing:
        for name, description in (("hidden", "create a hidden card"),
                                  ("archived", "create an archived card"),
                                  ("needs-review", "mark the card as needing review")):
            dest = name.replace("-", "_")
            command.add_argument(f"--{name}", dest=dest, action="store_const", const=True,
                                 default=None, help=description)
            command.add_argument(f"--no-{name}", dest=dest, action="store_const", const=False,
                                 help=f"do not {description}")
        command.add_argument("--type", dest="task_type", help="set task type (tasks only)")
        return
    command.add_argument("--type", dest="task_type", help="set task type (tasks only)")
    command.add_argument("--status", help="set task status (tasks only; non-claim open statuses)")
    command.add_argument("--remove-tag", dest="tag_remove", action="append", default=None,
                         help="remove a tag (repeatable; preserves other tags)")
    for name, dest, description in (
        ("hidden", "hidden", "set hidden visibility"),
        ("archived", "archived", "set archived state"),
        ("needs-review", "needs_review", "set review state"),
    ):
        command.add_argument(f"--{name}", dest=dest, action="store_const", const=True,
                             default=None, help=description)
        command.add_argument(f"--no-{name}", dest=dest, action="store_const", const=False,
                             help=f"clear {description}")


def command_info(args: argparse.Namespace) -> int:
    repository = git_ops.repository_info(REPO_ROOT)
    tasks = discover("task")
    notes_count = len(discover("note"))
    task_count = len(tasks)
    status_counts = {status: sum(task.frontmatter["status"] == status for task in tasks)
                     for status in statuses()}
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
    box_line(str(REPO_ROOT), "1", location=True)
    box_line()
    total = task_count + notes_count
    box_line(f"{total} card{'s' if total != 1 else ''}   ·   "
             f"{task_count} task{'s' if task_count != 1 else ''}   ·   "
             f"{notes_count} note{'s' if notes_count != 1 else ''}", "1")
    if demo_active():
        box_line("Demo workspace · bundled examples", "33")
    marker = view_marker()
    if marker is not None:
        box_line(f"View copy: {marker['name']}", "33")
        box_line(f"Source: {marker['source']}", location=True)
        box_line("Changes stay in this view copy.")
    print(paint("╰" + "─" * (width - 2) + "╯", "36"))

    print()
    section("TASKS")
    labels = policy()["statuses"]
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
        effective = policy()
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


def command_scratch(args: argparse.Namespace) -> int:
    """Allocate a private working directory in the selected workspace."""
    parent = REPO_ROOT / ".mt" / "scratch"
    parent.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="run-", dir=parent)).resolve()
    if args.as_json:
        print(json.dumps({"path": str(path)}))
    else:
        print(path)
    return 0


def view_name_from_args(args: argparse.Namespace) -> str:
    supplied = getattr(args, "name", None)
    source_text = " ".join(args.search) if args.search else (args.project or args.tag)
    if supplied is None:
        if source_text is None:
            raise ValueError("provide --name when no project, tag, or search text supplies a name")
        slug = "-".join(re.findall(r"[a-z0-9]+", str(source_text).casefold()))
        if not slug or len(slug) > 64:
            raise ValueError("cannot derive a safe view name; provide --name")
        return slug
    if len(supplied) > 64 or not VIEW_NAME_RE.fullmatch(supplied):
        raise ValueError("--name must be a lowercase slug using letters, digits, and single hyphens")
    return supplied


def view_selector_arguments(args: argparse.Namespace) -> list[str]:
    """Select a validated inventory snapshot or delegate a text search."""
    metadata = (("--tag", args.tag), ("--type", args.type), ("--project", args.project),
                ("--topic", args.topic), ("--since", args.since),
                ("--due-before", args.due_before), ("--due-after", args.due_after), ("--due", args.due))
    if any(value is not None and not value.strip() for _flag, value in metadata):
        raise ValueError("view filters must have non-empty values")
    if args.search:
        mode = {"exact": "phrase", "and": "all", "or": "any", "fuzzy": "fuzzy"}[args.match_mode]
        # Search the full inventory, then apply view's legacy visibility and
        # date filters to the validated snapshot in filter_view_items.
        return ["search", "--all", "--json", "--match", mode, "--", *args.search]
    if args.match_mode != "exact":
        raise ValueError("--and, --or, and --fuzzy require --search")
    has_list_filter = any((args.project, args.tag, args.type, args.topic, args.since,
                           args.created_since, args.created_before, args.updated_before,
                           args.completed_since, args.completed_before, args.due_before,
                           args.due_after, args.due, args.statuses, args.kind,
                           args.needs_review, args.include_archived, args.archived,
                           args.include_hidden, args.hidden, args.all))
    if not has_list_filter and not args.name:
        raise ValueError("view requires --project, a list filter, --search, or --name")
    # Keep view's existing include and date semantics independent of list's
    # simpler selectors. Filter the validated inventory bytes in create_view.
    return ["list", "--all", "--json"]


def filter_view_items(args: argparse.Namespace, selected: list[tuple[str, str, bytes, str]]
                      ) -> list[tuple[str, str, bytes, str]] | None:
    """Apply view's legacy structured filters to a validated card snapshot."""
    valid_statuses = set(policy().get("statuses", {}))
    if any(status not in valid_statuses for status in args.statuses):
        print("error: unknown status in --status", file=sys.stderr)
        return None
    if args.statuses and args.kind == "note":
        print("error: --status cannot be combined with --kind note", file=sys.stderr)
        return None
    if args.type and args.kind == "note":
        print("error: --type cannot be combined with --kind note", file=sys.stderr)
        return None
    keep = metadata_matcher(args)
    if keep is None:
        return None
    filtered = []
    for entry in selected:
        kind, source_path, raw, _content_hash = entry
        text = raw.decode("utf-8")
        frontmatter_text, body = parse_frontmatter(text)
        if frontmatter_text is None:
            raise ValueError(f"selected card has no frontmatter: {source_path}")
        item = Card(REPO_ROOT / source_path, text, frontmatter_text, body, parse_yaml(frontmatter_text))
        if args.kind is not None and kind != args.kind:
            continue
        if args.statuses and (kind != "task" or item.frontmatter.get("status") not in args.statuses):
            continue
        if args.type and kind == "note":
            continue
        if args.needs_review and item.frontmatter.get("needs_review") is not True:
            continue
        if getattr(args, "triage", False):
            is_triage = (
                (item.is_task and (item.frontmatter.get("status") == "inbox" or item.frontmatter.get("needs_review") is True))
                or (item.is_note and item.frontmatter.get("needs_review") is True)
            )
            if not is_triage:
                continue
        if keep(item):
            filtered.append(entry)
    return filtered


def validated_view_items(records: object) -> list[tuple[str, str, bytes, str]]:
    """Verify selector provenance against the active card roots and current bytes."""
    if not isinstance(records, list):
        raise ValueError("item selector did not return a JSON array")
    root = cards_root().resolve()
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
        source_path = REPO_ROOT / relative
        try:
            resolved = source_path.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as error:
            raise ValueError(f"item selector returned a path outside the current {kind} cards: {raw_path}") from error
        if source_path.is_symlink() or resolved.parent != root or resolved.suffix != ".md":
            raise ValueError(f"item selector returned a path outside the current {kind} cards: {raw_path}")
        if relative.parent != root.relative_to(REPO_ROOT.resolve()):
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


def copy_view_application(candidate: Path) -> None:
    """Copy the self-contained application without source data or local state."""
    for name in ("mt", "mt-policy.yaml"):
        shutil.copy2(REPO_ROOT / name, candidate / name)
    for name in ("install.sh", "README.md", "AGENTS.md", ".gitignore"):
        source = REPO_ROOT / name
        if source.is_file():
            shutil.copy2(source, candidate / name)
    local_policy = REPO_ROOT / "mt-policy.local.yaml"
    if local_policy.is_file():
        shutil.copy2(local_policy, candidate / local_policy.name)
    for name in ("scripts", "tools", "templates", ".agents", ".github"):
        source = REPO_ROOT / name
        if source.is_dir():
            shutil.copytree(
                source,
                candidate / name,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "*.swp"),
            )
    (candidate / "cards").mkdir()


def create_view(args: argparse.Namespace) -> int:
    try:
        name = view_name_from_args(args)
        selector_args = view_selector_arguments(args)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    views_root = REPO_ROOT / ".mt" / "views"
    destination = views_root / name
    if destination.exists():
        print(f"error: view already exists: {destination}", file=sys.stderr)
        return 1

    work_parent = REPO_ROOT / ".mt" / "scratch" / "task-views"
    work_parent.mkdir(parents=True, exist_ok=True)
    work_root = Path(tempfile.mkdtemp(prefix="run-", dir=work_parent))
    try:
        selection = subprocess.run(
            [str(REPO_ROOT / "mt"), *selector_args],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        if selection.returncode != 0:
            if selection.stderr:
                print(selection.stderr, end="", file=sys.stderr)
            return selection.returncode
        try:
            records = json.loads(selection.stdout)
            selected = validated_view_items(records)
        except (json.JSONDecodeError, OSError, ValueError) as error:
            print(f"error: could not create view: {error}", file=sys.stderr)
            return 1

        try:
            filtered = filter_view_items(args, selected)
        except (UnicodeError, ValueError) as error:
            print(f"error: could not create view: {error}", file=sys.stderr)
            return 1
        if filtered is None:
            return 2
        selected = filtered

        copy_view_application(work_root)
        items: list[dict[str, str]] = []
        for kind, source_path, raw, content_hash in selected:
            filename = Path(source_path).name
            target = work_root / "cards" / filename
            target.write_bytes(raw)
            items.append({"kind": kind, "path": source_path, "content_hash": content_hash})
        marker = {
            "version": 1,
            "name": name,
            "source": str(REPO_ROOT.resolve()),
            "created": now_stamp(),
            "items": items,
        }
        (work_root / VIEW_MARKER).write_text(
            json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        validation_env = os.environ.copy()
        validation_env["PYTHONDONTWRITEBYTECODE"] = "1"
        validation = subprocess.run(
            [str(work_root / "scripts" / "validate")],
            cwd=work_root,
            env=validation_env,
            check=False,
            capture_output=True,
            text=True,
        )
        if validation.returncode != 0:
            if validation.stdout:
                print(validation.stdout, end="", file=sys.stderr)
            if validation.stderr:
                print(validation.stderr, end="", file=sys.stderr)
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


def command_view(args: argparse.Namespace) -> int:
    # The existing mutation lock protects both the fixed selection snapshot
    # and the exclusive destination check from concurrent card edits/views.
    global _DEMO_STATE
    with time_mutation_lock():
        _DEMO_STATE = None
        pin_demo_state()
        maybe_seed_demo()
        return create_view(args)


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


def show_workspace(*, command: str | None = None) -> None:
    print(f"Workspace: {REPO_ROOT}", file=sys.stderr)
    if demo_active() and command not in {"add", "note"}:
        print(
            "Demo mode: bundled example tasks and notes — every command works on them. "
            'Create your first item (e.g. ./mt add "my task") to start your own workspace.',
            file=sys.stderr,
        )


def command_run(args: argparse.Namespace) -> int:
    os.chdir(REPO_ROOT)
    try:
        os.execvp(args.argv[0], args.argv)
    except FileNotFoundError:
        print(f"error: command not found: {args.argv[0]}", file=sys.stderr)
        return 127
    except OSError as error:
        print(f"error: cannot run {args.argv[0]}: {error}", file=sys.stderr)
        return 126


def command_update(args: argparse.Namespace) -> int:
    command = [sys.executable, str(REPO_ROOT / "scripts" / "update.py")]
    if args.check:
        command.append("--check")
    if args.as_json:
        command.append("--json")
    if args.force:
        command.append("--force")
    return subprocess.run(command, cwd=REPO_ROOT, check=False).returncode


def command_import_github(args: argparse.Namespace) -> int:
    show_workspace()
    command = [sys.executable, str(REPO_ROOT / "scripts" / "import-github.py")]
    if args.repo:
        command.extend(["--repo", args.repo])
    if getattr(args, "as_json", False):
        command.append("--json")
    return subprocess.run(command, cwd=REPO_ROOT, check=False).returncode


def command_validate(args: argparse.Namespace) -> int:
    return subprocess.run([str(REPO_ROOT / "scripts" / "validate")], check=False).returncode


def command_draft_list(args: argparse.Namespace) -> int:
    values = draft_store.list_drafts(REPO_ROOT)
    if args.as_json:
        print(json.dumps({"drafts": values}, ensure_ascii=False))
        return 0
    if not values:
        print("No drafts.")
        return 0
    for draft in values:
        excerpt = " ".join(str(draft.get("body") or "").split())
        if len(excerpt) > 110:
            excerpt = excerpt[:107].rstrip() + "..."
        label = str(draft.get("title") or "").strip() or excerpt[:70] or "Untitled draft"
        target = f" → {draft.get('target')}" if draft.get("target") else ""
        print(f"{draft['id']}  {draft['kind']} {draft['mode']}{target}  {label}")
    return 0


def command_draft_recover(args: argparse.Namespace) -> int:
    recovery = REPO_ROOT / ".mt" / "recovery" / "edit"
    unresolved = [source.name for source in sorted(recovery.glob("*.md"))
                  if source.is_file() and not source.is_symlink()] if recovery.is_dir() else []
    if args.as_json:
        print(json.dumps({"recovered": [], "unresolved": unresolved}, ensure_ascii=False))
    elif unresolved:
        print("Legacy editor recovery copies need manual review:")
        for name in unresolved:
            print(f"  {name}")
    else:
        print("No editor recovery copies.")
    return 0


def command_draft_read(args: argparse.Namespace) -> int:
    draft = draft_store.read(REPO_ROOT, args.draft_id)
    if args.as_json:
        print(json.dumps(draft, ensure_ascii=False))
    else:
        sys.stdout.write(str(draft.get("body") or ""))
    return 0


def command_draft_create(args: argparse.Namespace) -> int:
    if args.title is not None and (not args.title.strip() or any(character in args.title for character in "\r\n")):
        print("error: draft title must be non-empty and single-line", file=sys.stderr)
        return 2
    fields: dict[str, object] = {"kind": args.kind, "mode": "new", "title": args.title or ""}
    body = args.section_text
    if args.target:
        item, result = resolve_unique(args.target, match_mode="exact")
        if item is None:
            return result
        if (args.kind == "task") != item.is_task:
            print(f"error: {args.kind} drafts require a matching {args.kind} target", file=sys.stderr)
            return 2
        fields.update({
            "mode": "edit", "target": str(item.path.relative_to(REPO_ROOT)),
            "base_hash": hashlib.sha256(item.raw.encode("utf-8")).hexdigest(),
            "base_updated": str(item.frontmatter.get("updated") or ""), "title": args.title or "",
            "origin": "card-body",
        })
        # Seed from the card unless --file supplies the proposed body.
        body = args.section_text if args.file is not None else item.body
    else:
        if args.created_at and not is_timestamp(args.created_at):
            print("error: --created-at must be a local ISO timestamp with seconds", file=sys.stderr)
            return 2
        capture_id = args.capture_id or str(uuid.uuid4())
        try:
            parsed_capture_id = uuid.UUID(capture_id)
        except (AttributeError, ValueError):
            print("error: --capture-id must be a valid UUID", file=sys.stderr)
            return 2
        if str(parsed_capture_id) != capture_id.lower():
            print("error: --capture-id must be a canonical UUID", file=sys.stderr)
            return 2
        fields.update({"capture_id": str(parsed_capture_id), "created": args.created_at or now_stamp()})
        if args.kind == "task":
            default_status, default_priority, default_type = task_defaults(policy())
            fields.update({
                "status": default_status, "priority": default_priority, "type": default_type,
                "project": None, "topic": None, "tags": [], "due": None, "due_kind": None,
            })
    try:
        draft = draft_store.save(
            REPO_ROOT, draft_id=args.draft_id, fields=fields, body=body,
            expected_revision=0, now=now_stamp(),
        )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if args.as_json:
        print(json.dumps(draft, ensure_ascii=False))
    else:
        print(f"Draft saved: {draft['id']}")
    return 0


def draft_conflict_copy(existing: dict[str, object], body: str, title: str) -> dict[str, object]:
    fields = {field: existing.get(field) for field in draft_store.FIELDS
              if field in existing and field not in {"id", "revision", "updated"}}
    fields["title"] = title
    return draft_store.save(
        REPO_ROOT, draft_id=None, fields=fields, body=body,
        expected_revision=0, now=now_stamp(),
    )


def command_draft_edit(args: argparse.Namespace) -> int:
    try:
        existing = draft_store.read(REPO_ROOT, args.draft_id)
        expected_revision = (args.expected_revision if args.expected_revision is not None
                             else int(existing["revision"]))
        if expected_revision != existing["revision"]:
            raise draft_store.DraftConflict("draft changed since it was loaded; refresh and retry")
        title = args.title if args.title is not None else str(existing.get("title") or "")
        if any(character in title for character in "\r\n"):
            raise ValueError("draft title must be single-line")
        if args.file is not None:
            if args.expected_revision is None:
                raise ValueError("--expected-revision is required when using --file")
            body = args.section_text
        else:
            command = editor_command("draft edit")
            with managed_temp_directory("draft-edit-") as directory:
                editor_path = Path(directory) / "draft.md"
                with editor_path.open("w", encoding="utf-8", newline="") as editor_file:
                    editor_file.write(str(existing.get("body") or ""))
                try:
                    edited = subprocess.run([*command, str(editor_path)], cwd=REPO_ROOT, check=False)
                except FileNotFoundError as error:
                    raise ValueError(f"editor not found: {command[0]}") from error
                with editor_path.open(encoding="utf-8", newline="") as editor_file:
                    body = editor_file.read()
                if edited.returncode != 0:
                    if body != existing.get("body"):
                        copy = draft_conflict_copy(existing, body, title)
                        print(f"Editor exited with status {edited.returncode}; changes kept as draft {copy['id']}", file=sys.stderr)
                    else:
                        print(f"Editor exited with status {edited.returncode}; original draft was kept", file=sys.stderr)
                    return edited.returncode or 1
                if body == existing.get("body") and title == str(existing.get("title") or ""):
                    print("No changes: draft was not updated.")
                    return 0
        fields = {field: existing.get(field) for field in draft_store.FIELDS
                  if field in existing and field not in {"id", "revision", "updated"}}
        fields["title"] = title
        try:
            updated = draft_store.save(
                REPO_ROOT, draft_id=args.draft_id, fields=fields, body=body,
                expected_revision=expected_revision, expected_content_hash=str(existing["content_hash"]),
                now=now_stamp(),
            )
        except draft_store.DraftConflict:
            copy = draft_conflict_copy(existing, body, title)
            raise draft_store.DraftConflict(
                f"draft changed since it was loaded; edits were kept as draft {copy['id']}"
            )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if args.as_json:
        print(json.dumps(updated, ensure_ascii=False))
    else:
        print(f"Draft saved: {updated['id']} revision {updated['revision']}")
    return 0


def command_draft_discard(args: argparse.Namespace) -> int:
    try:
        existing = draft_store.read(REPO_ROOT, args.draft_id)
        removed = draft_store.discard(
            REPO_ROOT, args.draft_id, args.expected_revision,
            expected_content_hash=str(existing["content_hash"]),
        )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if not removed:
        print("error: draft not found", file=sys.stderr)
        return 1
    if args.as_json:
        print(json.dumps({"discarded": True, "id": args.draft_id}))
    else:
        print("Draft discarded.")
    return 0


def command_draft_save(args: argparse.Namespace) -> int:
    path = draft_store.draft_path(REPO_ROOT, args.draft_id)
    item: Card | None = None
    changed = False
    draft_removed = False
    try:
        # Keep the global mutation lock order aligned with migration and card
        # writes: time/mutation first, drafts second. Auto-sync runs below after
        # both locks have been released.
        with time_mutation_lock():
            lock_handle = draft_store.lock(REPO_ROOT)
            try:
                if not path.is_file():
                    raise draft_store.DraftConflict("draft was removed outside microtask; local copy was kept")
                draft = draft_store.read(REPO_ROOT, args.draft_id)
                if draft["revision"] != args.expected_revision:
                    raise draft_store.DraftConflict("draft changed since it was loaded; refresh and retry")
                if args.expected_hash is not None and draft["content_hash"] != args.expected_hash:
                    raise draft_store.DraftConflict("draft content changed outside microtask; refresh and reconcile")
                if draft["mode"] == "new":
                    body = str(draft.get("body") or "")
                    capture_args = argparse.Namespace(
                        text=[body], title=str(draft.get("title") or "").strip() or None,
                        capture_id=str(draft.get("capture_id") or ""), created_at=str(draft.get("created") or ""),
                        instructions_text=str(draft.get("instructions") or ""),
                    )
                    for field in ("priority", "project", "topic", "due", "due_kind"):
                        setattr(capture_args, field, draft.get(field))
                    capture_args.tags = draft.get("tags") or []
                    if draft["kind"] == "task":
                        capture_args.status = draft.get("status")
                        capture_args.task_type = draft.get("type")
                    item, changed = capture_card(capture_args, body, draft["kind"])
                    if draft_store.read(REPO_ROOT, args.draft_id)["content_hash"] != draft["content_hash"]:
                        raise draft_store.DraftConflict("draft content changed outside microtask; card saved and draft retained")
                    path.unlink()
                    draft_removed = True
                else:
                    target = str(draft.get("target") or "")
                    if not re.fullmatch(r"(?:\.mt/demo/)?cards/[^/]+\.md", target):
                        raise ValueError("draft target path is invalid")
                    target_path = REPO_ROOT / target
                    if target_path.is_symlink() or not target_path.is_file():
                        raise draft_store.DraftConflict("draft target is missing; draft was kept")
                    original = target_path.read_bytes()
                    if draft.get("base_hash") == draft_store.UNVERIFIED_BASE_HASH:
                        raise draft_store.DraftConflict(
                            "draft has no verified target baseline; compare the saved text and create a fresh edit draft before applying"
                        )
                    if hashlib.sha256(original).hexdigest() != draft.get("base_hash"):
                        raise draft_store.DraftConflict("target changed since the draft was created; draft was kept")
                    current = Card.read(target_path)
                    if draft.get("origin") != "card-body":
                        raise draft_store.DraftConflict(
                            "edit draft uses a retired card format; create a fresh edit draft before saving"
                        )
                    current_bytes = target_path.read_bytes()
                    if current_bytes != original:
                        raise draft_store.DraftConflict("target changed while saving; draft was kept")
                    if (draft["kind"] == "note") != current.is_note:
                        raise ValueError("draft kind does not match target kind")
                    if current.is_task and current.frontmatter.get("claimed_by"):
                        check_claim(current, current_username(None))
                    properties, current_body = dict(current.frontmatter), current.body
                    body = str(draft.get("body") or "")
                    title = str(draft.get("title") or "").strip()
                    if title:
                        properties = card_format.apply_property_patch(
                            properties, {"title": title}, kind=str(draft["kind"])
                        )
                    changed = body != current_body or properties != current.frontmatter
                    if changed:
                        properties["updated"] = now_stamp()
                        raw = card_format.render_document(properties, body, expected_kind=str(draft["kind"]))
                        if target.startswith(".mt/demo/"):
                            if not demo_active():
                                raise draft_store.DraftConflict("demo target is no longer active; draft was kept")
                            base = demo_root()
                            relative = target[len(".mt/demo/"):]
                        else:
                            base = REPO_ROOT
                            relative = target
                        apply_item_transaction(
                            {relative: raw.encode("utf-8")}, expected={relative: original}, source_root=base
                        )
                        item = Card.read(target_path)
                    else:
                        item = current
                    if draft_store.read(REPO_ROOT, args.draft_id)["content_hash"] != draft["content_hash"]:
                        raise draft_store.DraftConflict("draft content changed outside microtask; changes applied and draft retained")
                    path.unlink()
                    draft_removed = True
                if item is None:
                    raise ValueError("draft save did not create or update a card")
            finally:
                draft_store.unlock(lock_handle)
    except (OSError, UnicodeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    receipt = item_receipt(item, updated=changed)
    if changed or draft_removed:
        sync_error = run_auto_sync_for_receipt()
        if sync_error:
            receipt["sync_error"] = sync_error
    if args.as_json:
        print(json.dumps(receipt, ensure_ascii=False))
    else:
        print(f"Applied draft to {receipt['ref']}: {receipt['path']}")
    return 0


def command_web(args: argparse.Namespace) -> int:
    command = [sys.executable, str(REPO_ROOT / "tools" / "taskweb.py")]
    if args.no_open:
        command.append("--no-open")
    if args.host:
        command.extend(["--host", args.host])
    if args.port is not None:
        command.extend(["--port", str(args.port)])
    try:
        return subprocess.run(command, cwd=REPO_ROOT, check=False).returncode
    except KeyboardInterrupt:
        return 130


def add_web_arguments(parser: argparse.ArgumentParser) -> None:
    """Keep the public CLI and standalone dashboard entry point consistent."""
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    parser.add_argument("--host", default="127.0.0.1", help="server host; defaults to 127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="server port; defaults to an available port")


class MtArgumentParser(argparse.ArgumentParser):
    """Add a Git-style "did you mean" line to unknown option and command errors."""

    def parse_known_args(self, args=None, namespace=None):
        known, extras = super().parse_known_args(args, namespace)
        options = list(self._option_string_actions)
        hints = []
        for extra in extras:
            if extra.startswith("-"):
                hints += difflib.get_close_matches(extra.split("=", 1)[0], options, n=3)
        if hints and not hasattr(known, "did_you_mean"):
            known.did_you_mean = (self.prog, hints)
        return known, extras

    def parse_args(self, args=None, namespace=None):
        known, extras = self.parse_known_args(args, namespace)
        if extras:
            prog, hints = getattr(known, "did_you_mean", (self.prog, []))
            self.error("unrecognized arguments: " + " ".join(extras), prog, hints)
        return known

    def error(self, message, hint_prog=None, hints=()):
        for action in self._actions:
            if isinstance(action, argparse._SubParsersAction):
                match = re.match(r"argument (?!-).*?: invalid choice: '([^']*)'", message)
                if match and "maybe you meant" not in message:
                    hint_prog, hints = self.prog, difflib.get_close_matches(match.group(1), list(action.choices), n=3)
        self.print_usage(sys.stderr)
        text = f"{self.prog}: error: {message}\n"
        if hints:
            text += f"{hint_prog}: did you mean {' or '.join(hints)}?\n"
        self.exit(2, text)


def parser() -> argparse.ArgumentParser:
    root = MtArgumentParser(
        prog="mt",
        description="Deterministic microtask operations.",
        epilog="Capture with 'mt add <text>', 'mt note <text>', or guided 'mt capture'. "
               "Use 'mt <command> --help' for command-specific options and matching behavior. "
               "For metadata and body changes, see 'mt edit --help'; "
               "for configured values and defaults, use 'mt policy --json'.",
    )
    root.add_argument(
        "-R",
        "--repo",
        dest="workspace_repo",
        metavar="PATH_OR_NAME",
        help="use another microtask workspace; must precede the command",
    )
    sub = root.add_subparsers(dest="command", required=True)

    info = sub.add_parser("info", help="show workspace, Git remotes, and task counts")
    info.set_defaults(func=command_info)

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
    repo_add.add_argument("path", help="microtask workspace path")
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
    time_report.add_argument("--by", choices=("task", "project", "topic"), default="task")
    time_report.set_defaults(func=command_time_report)

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
    add_metadata_filter_arguments(focus)
    focus.set_defaults(func=command_focus)

    unfocus = sub.add_parser("unfocus", help="clear the local focus selection")
    unfocus.set_defaults(func=command_unfocus)

    followup = sub.add_parser("followup", help="show waiting tasks needing follow-up")
    followup.set_defaults(func=command_followup)

    listing = sub.add_parser("list", help="list tasks and notes")
    listing.description = (
        "List visible, unarchived tasks and notes by default. Selection filters combine; "
        "repeated --status values are alternatives. --status and --type select tasks; "
        "--needs-review selects any card marked for review. --all includes hidden and archived cards. "
        "--archived and --hidden select that state, including cards with both flags."
    )
    add_card_selection_arguments(listing)
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
        "Search card titles, IDs, and Markdown bodies, excluding YAML frontmatter. "
        "The default phrase mode matches one contiguous case-insensitive query. "
        "Use --match all or any to tokenize on whitespace, or fuzzy for approximate token matches. "
        "Relevance ranks title matches above ID matches above body matches; all/any reward more matching terms, "
        "and fuzzy ranks by average token similarity."
    )
    search.add_argument("query", nargs="+", metavar="WORDS", help="one or more words to search for")
    add_card_selection_arguments(search)
    search.add_argument("--match", dest="match_mode", choices=("phrase", "all", "any", "fuzzy"),
                        default="phrase", help="query matching mode (default: phrase)")
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
    view.add_argument("--search", nargs="+", help="select by the existing search command")
    view.add_argument("--name", help="view name; defaults to a safe slug from the selector")
    add_match_mode_arguments(view, allow_fuzzy=True)
    add_metadata_filter_arguments(view, item_label="items", collection=True)
    view.description = (
        "Create an independent workspace copy from the current list or search selection. "
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
            "Preview metadata changes for an explicit collection. Save the JSON preview, "
            "review its selection predicate, counts, paths, and hashes, then apply that exact "
            "plan; collection membership and every selected card hash must still match."
        ),
    )
    bulk_edit.add_argument("positional_statuses", nargs="*", help="task status IDs to include (compatibility form)")
    add_metadata_filter_arguments(bulk_edit, item_label="items", collection=True)
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


    web = sub.add_parser("web", help="open the local web dashboard")
    add_web_arguments(web)
    web.set_defaults(func=command_web)

    imported = sub.add_parser("import-github", help="capture open labeled GitHub issues for later review")
    imported.add_argument("--repo", metavar="OWNER/REPO")
    imported.add_argument("--json", dest="as_json", action="store_true", help=argparse.SUPPRESS)
    imported.set_defaults(func=command_import_github)

    public_commands = [name for name in sub.choices if name != "card-metadata"]
    sub.metavar = "{" + ",".join(public_commands) + "}"
    return root


def read_input_file(path: str) -> str:
    if path == "-":
        return sys.stdin.buffer.read().decode("utf-8")
    with Path(path).open(encoding="utf-8", newline="") as source_file:
        return source_file.read()


def main() -> int:
    arguments = sys.argv[1:]
    if arguments == ["help"]:
        arguments = ["--help"]
    try:
        arguments, repo_selector = consume_repo_selector(arguments)
        if repo_selector is not None:
            selected_root = resolve_repo_selector(repo_selector)
            if selected_root != REPO_ROOT.resolve():
                executable = selected_root / "mt"
                os.execv(str(executable), [str(executable), *arguments])
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    root_parser = parser()
    args = root_parser.parse_args(arguments)
    if args.command == "run":
        if args.argv and args.argv[0] == "--":
            args.argv = args.argv[1:]
        if not args.argv or not args.argv[0]:
            root_parser.error("run requires a command")
        return args.func(args)
    if (args.command in {"add", "note"} and args.file is None and not args.text
            and not getattr(args, "template", None) and not sys.stdin.isatty()):
        secondary_files = [getattr(args, field, None) for field in ("next_action_file", "instructions_file")]
        if any(path == "-" for path in secondary_files):
            root_parser.error("implicit body input conflicts with another file reading stdin; pass --file PATH")
        args.file = "-"
    if args.command == "edit":
        if args.file is not None and not args.body:
            root_parser.error("--file requires --body")
        if (args.old is None) != (args.new is None) or len(args.old or ()) != len(args.new or ()):
            root_parser.error(f"--old and --new must be given the same number of times "
                              f"(got {len(args.old or ())} --old, {len(args.new or ())} --new)")
        if sum(value is not None for value in (args.old, args.replace_file, args.file)) > 1:
            root_parser.error("--old/--new, --replace-file, and --file are mutually exclusive body operations")
        if args.append and (not args.body or args.file is None):
            root_parser.error("--append requires --body --file")
        if args.replace_file is not None or args.old is not None:
            args.body = True
        if args.body and args.file is None and args.replace_file is None and args.old is None:
            root_parser.error("--body requires --file, --old/--new, or --replace-file")
        if args.tags is not None and (args.tag_add or args.tag_remove):
            root_parser.error("--tags cannot be combined with --tag or --remove-tag")
        deterministic = bool(args.assignments or args.body or args.replace_file is not None or args.old is not None
                             or args.title is not None or args.tags is not None
                             or any(getattr(args, field, None) is not None for field in (
                                 "project", "topic", "priority", "task_type", "due", "due_kind",
                                 "hidden", "archived", "needs_review", "tag_add", "tag_remove", "status",
                             )))
        if not deterministic:
            root_parser.error("edit requires --title, --tags, --set, or a body operation")
        if args.title is not None:
            if not args.title.strip() or any(c in args.title for c in "\r\n\x00"):
                root_parser.error("--title must be non-empty and single-line")
            args.title = args.title.strip()
        if args.expected_hash is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", args.expected_hash):
            root_parser.error("--expected-hash must be a 64-character SHA-256 hex digest")
        if args.file is not None and not args.append and not args.expected_hash:
            root_parser.error("body replacement requires --expected-hash from read --json")
        if args.expected_hash and not deterministic:
            root_parser.error("--expected-hash requires an edit operation")
        if args.as_json and not deterministic:
            root_parser.error("--json requires a deterministic edit")
    if args.command in {"add", "note"}:
        if args.file is not None and args.text:
            root_parser.error("capture text and --file cannot be combined")
        has_input = args.file is not None or bool(args.text)
        if args.kind == "note":
            if any(getattr(args, name, None) is not None
                   for name in ("status", "task_type", "next_action", "next_action_file", "by")):
                root_parser.error("--status, --type, --next-action, --next-action-file, and --by are task-only options")
        else:
            if args.template:
                root_parser.error("--template applies only to notes")
            if not has_input:
                root_parser.error("add requires text or --file")
        if args.capture and not has_input:
            root_parser.error("--capture requires text or --file")
        if args.template and has_input:
            root_parser.error("--template cannot be combined with capture text or --file")
        if not has_input and (args.instructions is not None or args.instructions_file is not None):
            root_parser.error("--instructions requires text or --file")
        if args.capture_id is not None and not args.capture:
            root_parser.error("--capture-id requires --capture")
        if args.created_at is not None and not args.capture:
            root_parser.error("--created-at requires --capture")
        if args.capture_id is not None and args.created_at is None:
            root_parser.error("--capture-id requires --created-at")
        if args.created_at is not None and args.capture_id is None:
            root_parser.error("--created-at requires --capture-id")
        if args.capture_id is not None:
            try:
                uuid.UUID(args.capture_id)
            except ValueError:
                root_parser.error("--capture-id must be a valid UUID")
            if not is_timestamp(args.created_at or ""):
                root_parser.error("--created-at must be a valid timestamp in YYYY-MM-DDTHH:MM:SS format")
        if args.instructions is not None and args.instructions_file is not None:
            root_parser.error("--instructions and --instructions-file cannot be combined")
        if args.next_action is not None and args.next_action_file is not None:
            root_parser.error("--next-action and --next-action-file cannot be combined")
        if args.title is not None:
            if not args.title.strip() or "\n" in args.title or "\r" in args.title:
                root_parser.error("--title must be non-empty and single-line")
            args.title = args.title.strip()
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
        pin_demo_state()
        maybe_seed_demo()
    if args.command in {
        "add", "note", "capture", "time", "focus", "unfocus", "followup", "list", "search", "view", "show", "edit", "bulk-edit",
        "comment", "move", "convert", "done", "delete", "sync", "validate", "draft",
    }:
        show_workspace(command=args.command)
    return args.func(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
