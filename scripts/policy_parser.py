#!/usr/bin/env python3
"""Parse the small YAML subset used by mt-policy.yaml."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from pathlib import Path
import os
import re

import card_format
from vendor import yaml


APP_ROOT = Path(__file__).resolve().parent.parent
CORE_STATUSES = ("inbox", "active", "doing", "waiting", "done")
_STATUS_ID = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")


def validate_status_policy(cfg: dict) -> None:
    """Validate the status portion of a parsed microtask policy.

    Status IDs are stable lowercase slugs used in task metadata and CLI
    commands. Display labels are presentation text and may be changed freely.
    """
    errors: list[str] = []
    statuses = cfg.get("statuses") if isinstance(cfg, dict) else None
    if not isinstance(statuses, dict):
        errors.append("policy statuses must be a mapping of status IDs to display labels")
        statuses = {}

    for required in CORE_STATUSES:
        if required not in statuses:
            errors.append(f"policy statuses must include required status '{required}'")

    for status_id, label in statuses.items():
        if not isinstance(status_id, str) or not _STATUS_ID.fullmatch(status_id):
            errors.append(f"policy status ID must be a lowercase slug: {status_id!r}")
        elif status_id == "notes":
            errors.append("policy status ID 'notes' is reserved")
        if not isinstance(label, str) or not label.strip():
            errors.append(f"policy status '{status_id}' must have a non-empty display label")

    default_status = cfg.get("default_status") if isinstance(cfg, dict) else None
    if not isinstance(default_status, str) or default_status not in statuses:
        errors.append("policy default_status must name a configured status ID")
    elif default_status == "done":
        errors.append("policy default_status must not be 'done'")

    if errors:
        raise ValueError("\n".join(errors))


class _PolicyLoader(yaml.SafeLoader):
    pass


# Keep numbers and dates as strings (matching mt-policy.yaml's string rule values).
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
            raise ValueError(f"duplicate policy status ID '{key}'" if isinstance(key, str) else f"duplicate policy key {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


def _construct_policy_null(loader: _PolicyLoader, node: yaml.ScalarNode) -> str | None:
    if node.value == "" and node.style is None:
        return ""
    return None


_PolicyLoader.add_constructor("tag:yaml.org,2002:map", _construct_policy_mapping)
_PolicyLoader.add_constructor("tag:yaml.org,2002:null", _construct_policy_null)


def parse_policy_yaml(content: str) -> dict:
    """Parse mt-policy.yaml using vendored PyYAML with string-preserved numbers/dates."""
    try:
        parsed = yaml.load(content, Loader=_PolicyLoader)
    except yaml.YAMLError as error:
        raise ValueError(f"invalid policy YAML: {error}") from error
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ValueError("policy YAML must be a mapping")
    return parsed

def workspace_root() -> Path:
    """Folder holding the cards and policy: MT_WORKSPACE when set, else the application's own."""
    value = os.environ.get("MT_WORKSPACE")
    # A view is a disposable copy and only ever works on its own cards.
    if not value or (APP_ROOT / "mt-view.json").is_file():
        return APP_ROOT
    path = Path(value)
    return path.resolve() if path.is_absolute() else path


def load_policy(root: Path | None = None) -> dict:
    root = root or workspace_root()
    policy_path = root / "mt-policy.yaml"
    local_policy_path = root / "mt-policy.local.yaml"
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
                    f"local policy cannot add status ID '{status_id}'; "
                    "define it in mt-policy.yaml so status definitions sync with cards"
                )
        policy = merge_policy(policy, override)
        validate_status_policy(policy)
    username = policy.get("default_username")
    if username is not None and (
        not isinstance(username, str) or not username.strip()
        or any(char in username for char in "\r\n\x00")
    ):
        raise ValueError("policy default_username must be a non-empty, single-line name or null")
    warn_hours = policy.get("rules", {}).get("timer_warn_hours", 8)
    try:
        valid_warn_hours = not isinstance(warn_hours, bool) and 0 < float(warn_hours) < float("inf")
    except (TypeError, ValueError):
        valid_warn_hours = False
    if not valid_warn_hours:
        raise ValueError("policy rules.timer_warn_hours must be a positive number")
    policy.setdefault("rules", {}).setdefault("timer_warn_hours", 8)
    done_window = policy["rules"].get("done_window", "2w")
    if not isinstance(done_window, str) or card_format.relative_date(done_window, date.today()) is None:
        raise ValueError("policy rules.done_window must be an age like 2w, 30d, or 1m")
    policy["rules"].setdefault("done_window", "2w")
    return policy


def merge_policy(base: dict, override: dict) -> dict:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_policy(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged
