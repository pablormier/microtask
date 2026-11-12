"""Workspace-policy checks applied after canonical card structure is validated."""
from __future__ import annotations

from dataclasses import dataclass

import card_format


@dataclass(frozen=True)
class CheckContext:
    kind: str
    rel: str
    fm: dict[str, object]
    body: str
    policy: dict[str, object]


def status_errors(ctx: CheckContext) -> list[str]:
    if ctx.kind != "task":
        return []
    status = ctx.fm["status"]
    allowed = ctx.policy.get("statuses", {})
    if not isinstance(allowed, dict) or status not in allowed:
        return [f"{ctx.rel}: invalid status '{status}'"]
    return []


def task_type_errors(ctx: CheckContext) -> list[str]:
    if ctx.kind != "task":
        return []
    task_type = ctx.fm["type"]
    allowed = ctx.policy.get("types", {})
    if not isinstance(allowed, dict) or task_type not in allowed:
        return [f"{ctx.rel}: invalid type '{task_type}'"]
    return []


def priority_errors(ctx: CheckContext) -> list[str]:
    priority = ctx.fm["priority"]
    allowed = ctx.policy.get("priorities", {})
    if not isinstance(allowed, dict) or priority not in allowed:
        return [f"{ctx.rel}: invalid priority '{priority}'"]
    return []


def due_kind_errors(ctx: CheckContext) -> list[str]:
    due = ctx.fm.get("due")
    if due is None:
        return []
    due_kind = ctx.fm.get("due_kind")
    allowed = ctx.policy.get("due_kinds", {})
    if not isinstance(allowed, dict) or due_kind not in allowed:
        return [f"{ctx.rel}: due_kind must be one of the configured due kinds when due is set: '{due_kind}'"]
    return []


def lifecycle_errors(ctx: CheckContext) -> list[str]:
    if ctx.kind != "task":
        return []
    errors = []
    if "completed" in ctx.fm and ctx.fm["status"] != "done":
        errors.append(f"{ctx.rel}: completed is only valid for done tasks")
    if ctx.fm.get("claimed_by") is not None and ctx.fm["status"] != "doing":
        errors.append(f"{ctx.rel}: a claimed task must have status doing")
    return errors


def time_log_errors(ctx: CheckContext) -> list[str]:
    if ctx.kind != "task":
        return []
    seen: set[str] = set()
    errors: list[str] = []
    for entry in ctx.fm.get("time_entries", []):
        session_id = entry["session_id"]
        if session_id in seen:
            errors.append(f"{ctx.rel}: duplicate time session id '{session_id}'")
        seen.add(session_id)
    return errors


POLICY_VALIDATORS = (
    status_errors,
    task_type_errors,
    priority_errors,
    due_kind_errors,
    lifecycle_errors,
    time_log_errors,
)


def run_policy_validators(ctx: CheckContext) -> list[str]:
    """Validate workspace enums and lifecycle after canonical schema validation."""
    errors: list[str] = []
    for validator in POLICY_VALIDATORS:
        errors.extend(validator(ctx))
    return errors


def run_item_validators(ctx: CheckContext) -> list[str]:
    """Validate schema first, then workspace enums and lifecycle invariants."""
    structural = card_format.validate_properties(ctx.fm, kind=ctx.kind)
    if structural:
        return [f"{ctx.rel}: {error}" for error in structural]
    return run_policy_validators(ctx)
