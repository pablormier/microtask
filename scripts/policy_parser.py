#!/usr/bin/env python3
"""Parse the small YAML subset used by settings.yaml."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from pathlib import Path
import re

import card_format
import card_linter
from vendor import yaml

APP_ROOT = Path(__file__).resolve().parent.parent

# What rules.max_doing_tasks counts: all doing tasks together, or the doing tasks of each claimant.
DOING_LIMIT_SCOPES = ("workspace", "per_claimant")
CORE_STATUSES = ("inbox", "active", "doing", "waiting", "done")
_STATUS_ID = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")


def validate_status_policy(cfg: dict) -> None:
    """Validate the status portion of a parsed microtask settings.

    Status IDs are stable lowercase slugs used in task metadata and CLI
    commands. Display labels are presentation text and may be changed freely.
    """
    errors: list[str] = []
    statuses = cfg.get("statuses") if isinstance(cfg, dict) else None
    if not isinstance(statuses, dict):
        errors.append("settings statuses must be a mapping of status IDs to display labels")
        statuses = {}

    for required in CORE_STATUSES:
        if required not in statuses:
            errors.append(f"settings statuses must include required status '{required}'")

    for status_id, label in statuses.items():
        if not isinstance(status_id, str) or not _STATUS_ID.fullmatch(status_id):
            errors.append(f"settings status ID must be a lowercase slug: {status_id!r}")
        elif status_id == "notes":
            errors.append("settings status ID 'notes' is reserved")
        if not isinstance(label, str) or not label.strip():
            errors.append(f"settings status '{status_id}' must have a non-empty display label")

    default_status = cfg.get("default_status") if isinstance(cfg, dict) else None
    if not isinstance(default_status, str) or default_status not in statuses:
        errors.append("settings default_status must name a configured status ID")
    elif default_status == "done":
        errors.append("settings default_status must not be 'done'")

    if errors:
        raise ValueError("\n".join(errors))


class _PolicyLoader(yaml.SafeLoader):
    pass


# Keep numbers and dates as strings (matching settings.yaml's string rule values).
_PolicyLoader.yaml_implicit_resolvers = {
    initial: [
        (tag, regexp)
        for tag, regexp in resolvers
        if tag not in {"tag:yaml.org,2002:int", "tag:yaml.org,2002:float", "tag:yaml.org,2002:timestamp"}
    ]
    for initial, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _construct_policy_mapping(loader: _PolicyLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    loader.flatten_mapping(node)
    mapping: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"duplicate settings key '{key}'" if isinstance(key, str) else f"duplicate settings key {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


def _construct_policy_null(loader: _PolicyLoader, node: yaml.ScalarNode) -> str | None:
    if node.value == "" and node.style is None:
        return ""
    return None


_PolicyLoader.add_constructor("tag:yaml.org,2002:map", _construct_policy_mapping)
_PolicyLoader.add_constructor("tag:yaml.org,2002:null", _construct_policy_null)


def parse_policy_yaml(content: str) -> dict:
    """Parse settings.yaml using vendored PyYAML with string-preserved numbers/dates."""
    try:
        parsed = yaml.load(content, Loader=_PolicyLoader)
    except yaml.YAMLError as error:
        raise ValueError(f"invalid settings YAML: {error}") from error
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ValueError("settings YAML must be a mapping")
    return parsed

def settings_paths(root: Path) -> tuple[Path, Path]:
    """The shared and local settings files of the workspace at root.

    An old-style install (cards next to the application code) keeps its pre-0.2.0 names,
    mt-policy.yaml and mt-policy.local.yaml, until 'mt migrate' renames them. It must keep
    working until then: the old Git-based 'mt update' validates the new code in place.
    """
    root = Path(root)
    if (not (root / "settings.yaml").is_file() and (root / "mt-policy.yaml").is_file()
            and (root / "mt").is_file() and (root / "scripts" / "cli.py").is_file()):
        return root / "mt-policy.yaml", root / "mt-policy.local.yaml"
    return root / "settings.yaml", root / "settings.local.yaml"


def load_policy(root: Path) -> dict:
    policy_path, local_policy_path = settings_paths(root)
    policy = parse_policy_yaml(policy_path.read_text(encoding="utf-8"))
    validate_status_policy(policy)
    if local_policy_path.exists():
        override = parse_policy_yaml(local_policy_path.read_text(encoding="utf-8"))
        local_statuses = override.get("statuses")
        shared_statuses = policy.get("statuses")
        if isinstance(local_statuses, dict) and isinstance(shared_statuses, dict):
            extra_ids = [status_id for status_id in local_statuses if status_id not in shared_statuses]
            if extra_ids:
                status_id = extra_ids[0]
                raise ValueError(
                    f"local settings cannot add status ID '{status_id}'; "
                    "define it in settings.yaml so status definitions sync with cards"
                )
        policy = merge_policy(policy, override)
        validate_status_policy(policy)
    username = policy.get("default_username")
    if username is not None and (
        not isinstance(username, str) or not username.strip()
        or any(char in username for char in "\r\n\x00")
    ):
        raise ValueError("settings default_username must be a non-empty, single-line name or null")
    rules = policy.get("rules")
    if rules is None:
        rules = {}
        policy["rules"] = rules
    elif not isinstance(rules, dict):
        raise ValueError("settings rules must be a mapping")
    warn_hours = rules.get("timer_warn_hours", 8)
    try:
        valid_warn_hours = not isinstance(warn_hours, bool) and 0 < float(warn_hours) < float("inf")
    except (TypeError, ValueError):
        valid_warn_hours = False
    if not valid_warn_hours:
        raise ValueError("settings rules.timer_warn_hours must be a positive number")
    rules.setdefault("timer_warn_hours", 8)
    max_attachment_mb = rules.get("max_attachment_mb", 10)
    try:
        valid_max_attachment = not isinstance(max_attachment_mb, bool) and 0 < float(max_attachment_mb) < float("inf")
    except (TypeError, ValueError):
        valid_max_attachment = False
    if not valid_max_attachment:
        raise ValueError("settings rules.max_attachment_mb must be a positive number")
    rules.setdefault("max_attachment_mb", 10)
    if rules.get("doing_limit_scope", "workspace") not in DOING_LIMIT_SCOPES:
        raise ValueError("settings rules.doing_limit_scope must be workspace or per_claimant")
    done_window = rules.get("done_window", "2w")
    if not isinstance(done_window, str) or card_format.relative_date(done_window, date.today()) is None:
        raise ValueError("settings rules.done_window must be an age like 2w, 30d, or 1m")
    rules.setdefault("done_window", "2w")
    local_history = rules.get("local_history", True)
    if not isinstance(local_history, bool):
        raise ValueError("settings rules.local_history must be true or false")
    rules.setdefault("local_history", True)
    if "calendar" in policy:
        normalized_calendar = normalize_calendar_policy(policy["calendar"])
        if normalized_calendar is None:
            policy.pop("calendar", None)
        else:
            policy["calendar"] = normalized_calendar
    if "lint" in policy:
        normalized_lint = card_linter.normalize_lint_policy(policy["lint"])
        if normalized_lint:
            policy["lint"] = normalized_lint
        else:
            policy.pop("lint", None)
    return policy


def _single_line_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(char in value for char in "\r\n\x00"):
        raise ValueError(f"{field} must be a non-empty, single-line string")
    return value.strip()


def _normalize_calendar_entries(entries: object, section: str) -> list[dict]:
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise ValueError(f"settings calendar.{section} must be a list")
    normalized = []
    for index, item in enumerate(entries):
        field = f"settings calendar.{section}[{index}]"
        if isinstance(item, str):
            normalized.append({"id": _single_line_text(item, field)})
            continue
        if not isinstance(item, dict):
            raise ValueError(f"{field} must be a calendar ID string or mapping")
        extra_keys = set(item) - {"id", "account", "provider"}
        if extra_keys:
            raise ValueError(f"{field} has unsupported key(s): {', '.join(sorted(map(str, extra_keys)))}")
        entry = {"id": _single_line_text(item.get("id"), f"{field}.id")}
        for opt_key in ("account", "provider"):
            if item.get(opt_key) is not None:
                entry[opt_key] = _single_line_text(item[opt_key], f"{field}.{opt_key}")
        normalized.append(entry)
    return normalized


def normalize_calendar_policy(calendar: object) -> dict | None:
    """Validate and normalize optional calendar allowlist settings from settings."""
    if calendar is None:
        return None
    if not isinstance(calendar, dict):
        raise ValueError("settings calendar must be a mapping")
    extra_keys = set(calendar) - {"include", "guidance"}
    if extra_keys:
        raise ValueError(f"settings calendar has unsupported key(s): {', '.join(sorted(map(str, extra_keys)))}")
    guidance = calendar.get("guidance", "")
    if not isinstance(guidance, str) or "\x00" in guidance:
        raise ValueError("settings calendar.guidance must be text without NUL characters")
    return {
        "include": _normalize_calendar_entries(calendar.get("include"), "include"),
        "guidance": guidance,
    }


def merge_policy(base: dict, override: dict) -> dict:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_policy(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged
