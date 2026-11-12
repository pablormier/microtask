#!/usr/bin/env python3
"""Parse the small YAML subset used by mt-policy.yaml."""
from copy import deepcopy
from datetime import date
from pathlib import Path
import re

import card_format


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


def _strip_comment(line: str) -> str:
    """Remove a YAML comment outside a quoted scalar."""
    quote: str | None = None
    escaped = False
    for index, char in enumerate(line):
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif quote == "'":
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index]
    return line


def parse_scalar(raw: str):
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        quoted = value[0]
        value = value[1:-1]
        if quoted == "'":
            return value.replace("''", "'")
        return value.replace(r'\"', '"').replace(r'\\', '\\')
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    if value.lower() == "null":
        return None
    return value

def parse_policy_yaml(content: str) -> dict:
    """A simple YAML parser tailored for our flat/nested mt-policy.yaml schema."""
    data = {}
    current_key = None
    current_dict = None
    
    for line in content.splitlines():
        # Remove comments and trailing whitespace
        line_no_comment = _strip_comment(line)
        stripped = line_no_comment.strip()
        if not stripped:
            continue
        
        # Determine indentation level
        indent = len(line_no_comment) - len(line_no_comment.lstrip())
        
        if indent == 0:
            if ':' in stripped:
                key, val = stripped.split(':', 1)
                key = key.strip()
                val = val.strip()
                if val:
                    data[key] = parse_scalar(val)
                    current_key = None
                else:
                    if key not in data or not isinstance(data[key], dict):
                        data[key] = {}
                    current_key = key
                    current_dict = data[key]
        elif indent > 0:
            if current_key is not None:
                if stripped.startswith('-'):
                    item = parse_scalar(stripped[1:])
                    if not isinstance(current_dict, list):
                        data[current_key] = []
                        current_dict = data[current_key]
                    current_dict.append(item)
                elif ':' in stripped:
                    k, v = stripped.split(':', 1)
                    k = k.strip()
                    v = v.strip()
                    if isinstance(current_dict, dict):
                        if current_key == "statuses" and k in current_dict:
                            raise ValueError(f"duplicate policy status ID '{k}'")
                        current_dict[k] = parse_scalar(v)
    return data

def load_policy() -> dict:
    here = Path(__file__).resolve().parent
    policy_path = here.parent / "mt-policy.yaml"
    local_policy_path = here.parent / "mt-policy.local.yaml"
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
