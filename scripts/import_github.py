#!/usr/bin/env python3
"""Capture labeled GitHub issues as validated notes for review."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import card_format
import operations
import store
import transaction
import workspace
import git_ops


ROOT = Path(__file__).resolve().parent.parent
ISSUE_TEMPLATE = ROOT / ".github" / "ISSUE_TEMPLATE" / "capture.yml"


def gh(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["gh", *args], cwd=ROOT, check=False, capture_output=True, text=True)
    except FileNotFoundError as error:
        raise RuntimeError("gh is not installed or not on PATH") from error


def gh_json(*args: str) -> object:
    result = gh(*args)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "gh command failed")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"gh returned invalid JSON: {error}") from error


def resolve_repo(explicit: str | None) -> str:
    if explicit:
        return explicit
    repo_root = workspace.Workspace.resolve().root
    status = git_ops.workspace_status(repo_root)
    if status.state != "ready":
        if status.state == "missing":
            raise RuntimeError(
                "Git is not available to infer the repository; pass --repo OWNER/REPO "
                "or install Git and configure a GitHub origin remote"
            )
        if status.state == "uninitialized":
            raise RuntimeError(
                "this workspace has no GitHub origin; pass --repo OWNER/REPO or "
                "initialize Git and configure an origin remote"
            )
        raise RuntimeError(f"could not inspect Git workspace: {status.detail}; pass --repo OWNER/REPO")
    result = git_ops.run_git(repo_root, "remote", "get-url", "origin")
    repo = git_ops.github_slug(result.stdout) if result.returncode == 0 else None
    if not repo:
        raise RuntimeError("use --repo OWNER/REPO or configure a GitHub origin remote")
    return repo


def capture_label() -> str:
    try:
        raw = ISSUE_TEMPLATE.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise ValueError("capture issue template not found") from error
    # The issue form is nested YAML; only its flat top-level header is relevant.
    header = "\n".join(line for line in raw.splitlines() if line and not line[0].isspace())
    labels = card_format.parse_yaml(header).get("labels")
    if not isinstance(labels, list) or len(labels) != 1 or not str(labels[0]).strip():
        raise ValueError("capture issue template supports exactly one label")
    return str(labels[0]).strip()


def list_issues(repo: str, label: str) -> list[dict[str, object]]:
    data = gh_json("issue", "list", "--repo", repo, "--label", label, "--state", "open",
                   "--limit", "100", "--json", "number,title,body,url,createdAt,labels")
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise RuntimeError("gh issue list response is not an issue list")
    return data


def issue_values(issue: dict[str, object]) -> tuple[int, str, str, str, str]:
    number, title = issue.get("number"), issue.get("title")
    body, url, created = issue.get("body") or "", issue.get("url"), issue.get("createdAt")
    if not isinstance(number, int) or not all(isinstance(value, str) for value in (title, body, url, created)):
        raise ValueError("issue is missing a valid number, title, body, URL, or creation date")
    if not title.strip() or "\n" in title or "\r" in title:
        raise ValueError(f"issue #{number} has an invalid title")
    return number, title, body, url, created


def source_hash(title: str, body: str) -> str:
    source = json.dumps({"title": title, "body": body}, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def source_identity(url: str) -> tuple[str, int] | None:
    match = re.fullmatch(r"https://github\.com/([^/]+)/([^/]+)/issues/(\d+)", url)
    if not match:
        return None
    owner, repo, number = match.groups()
    return f"{owner}/{repo}", int(number)


def issue_timestamp(value: str) -> tuple[str, str]:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"invalid GitHub creation date '{value}'") from error
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    return parsed.strftime("%Y-%m-%d-%H%M%S"), parsed.strftime("%Y-%m-%dT%H:%M:%S")


def real_workspace_items() -> list[store.Card]:
    return store.read_cards(store.real_cards_root())


def existing_issues(repo: str, items: list[store.Card]) -> dict[int, list[store.Card]]:
    result: dict[int, list[store.Card]] = {}
    for item in items:
        source_url = item.frontmatter.get("github_source")
        source_hash_value = item.frontmatter.get("github_source_hash")
        if source_url is None and source_hash_value is None:
            continue
        if not isinstance(source_url, str):
            raise ValueError(f"{item.path}: github_source must be a URL")
        identity = source_identity(source_url)
        if identity is None:
            raise ValueError(f"{item.path}: github_source must be a canonical GitHub issue URL")
        if not isinstance(source_hash_value, str) or not re.fullmatch(r"[0-9a-f]{64}", source_hash_value):
            raise ValueError(f"{item.path}: github_source_hash must be a lowercase SHA-256 hash")
        if identity[0] == repo:
            result.setdefault(identity[1], []).append(item)
    return result


def render_capture(repo: str, issue: tuple[int, str, str, str, str]) -> tuple[str, bytes]:
    number, title, body, url, created_at = issue
    identity = source_identity(url)
    if identity != (repo, number):
        raise ValueError(f"issue #{number} has a non-canonical or mismatched GitHub URL")
    identifier_time, created = issue_timestamp(created_at)
    identifier = f"{identifier_time}-gh-{number}-{store.slugify(title)}"
    rendered = operations.render_item_document(
        item_mode="note", item_id=identifier, title=title,
        priority=operations.unassigned_priority(store.policy()), project=None, topic=None,
        due=None, due_kind=None, created=created, updated=created, tags=[],
        body_text=body, needs_review=True,
        github_source=url, github_source_hash=source_hash(title, body),
    )
    return identifier, rendered.encode("utf-8")


def validate_existing_sources(
    repo: str,
    issues: list[tuple[int, str, str, str, str]],
    index: dict[int, list[store.Card]],
) -> None:
    for number, title, body, url, _created in issues:
        matches = index.get(number, [])
        if not matches:
            continue
        if source_identity(url) != (repo, number):
            raise ValueError(f"issue #{number} has a non-canonical or mismatched GitHub URL")
        expected = source_hash(title, body)
        source_url = matches[0].frontmatter.get("github_source")
        saved = matches[0].frontmatter.get("github_source_hash")
        if source_identity(str(source_url)) != (repo, number):
            raise RuntimeError(f"local provenance for {repo}#{number} has an invalid source URL")
        if not isinstance(saved, str) or not re.fullmatch(r"[0-9a-f]{64}", saved):
            raise RuntimeError(f"local provenance for {repo}#{number} has an invalid source hash")
        if saved != expected:
            raise RuntimeError(
                f"GitHub issue {repo}#{number} changed since capture; review the local note before retrying"
            )


def capture_issues(
    repo: str,
    issues: list[tuple[int, str, str, str, str]],
) -> tuple[list[str], list[int], list[int], dict[int, list[store.Card]]]:
    """Recheck markers and commit the complete import as one locked transaction."""
    with transaction.time_mutation_lock():
        items = real_workspace_items()
        index = existing_issues(repo, items)
        requested_numbers = [issue[0] for issue in issues]
        if len(requested_numbers) != len(set(requested_numbers)):
            raise RuntimeError("GitHub issue list contains duplicate issue numbers")
        duplicate = next((number for number in requested_numbers if len(index.get(number, [])) > 1), None)
        if duplicate is not None:
            raise RuntimeError(f"multiple local items reference {repo}#{duplicate}")
        validate_existing_sources(repo, issues, index)

        new_issues = [issue for issue in issues if issue[0] not in index]
        existing_ids = {str(item.frontmatter.get("id") or item.path.stem) for item in items}
        changes: dict[str, bytes] = {}
        expected: dict[str, bytes | None] = {}
        imported_ids: list[str] = []
        imported_numbers: list[int] = []
        for issue in new_issues:
            identifier, contents = render_capture(repo, issue)
            relative = str(Path("cards") / f"{identifier}.md")
            if identifier in existing_ids or relative in changes or (ROOT / relative).exists():
                raise RuntimeError(f"generated note ID already exists: {identifier}")
            changes[relative] = contents
            expected[relative] = None
            imported_ids.append(identifier)
            imported_numbers.append(issue[0])
        if changes:
            transaction.apply_item_transaction(changes, expected=expected, source_root=ROOT)
            # The first real capture leaves demo mode, like mt add and mt note.
            store.pin_demo_state()
        existing_numbers = [issue[0] for issue in issues if issue[0] in index]
        return imported_ids, imported_numbers, existing_numbers, index


def main(argv: list[str] | None = None, ws: workspace.Workspace | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mt import-github", description="Capture open labeled GitHub issues as notes for review.")
    parser.add_argument("--repo", metavar="OWNER/REPO")
    parser.add_argument("--json", action="store_true", help="print a machine-readable capture receipt")
    args = parser.parse_args(argv)
    resolved_ws = ws or workspace.Workspace.resolve()
    if not transaction.run_validator(resolved_ws):
        return 1
    try:
        label = capture_label()
        repo = resolve_repo(args.repo)
        issues = [issue_values(issue) for issue in list_issues(repo, label)]
        imported_ids, imported_numbers, existing_numbers, index = capture_issues(repo, issues)
        captured_numbers = set(existing_numbers) | set(imported_numbers)
        # Every listed issue now has a durable local capture; sync it before closing any issue.
        sync_status = 0
        if captured_numbers:
            if store.policy().get("rules", {}).get("auto_sync", False):
                # Issue closure is external and requires an explicit successful
                # sync, even when ordinary local mutations can stay Git-free.
                if args.json:
                    with redirect_stdout(sys.stderr):
                        sync_status = transaction.command_sync(argparse.Namespace(message=None))
                else:
                    sync_status = transaction.command_sync(argparse.Namespace(message=None))
            else:
                sync_status = transaction.auto_sync_after_mutation(changed=True)

        receipt: dict[str, object] = {
            "repo": repo,
            "imported_ids": imported_ids,
            "existing_ids": [str(item.frontmatter.get("id") or item.path.stem)
                             for number in sorted(existing_numbers) for item in index[number]],
            "closed": [],
            "close_failed": {},
        }
        if sync_status != 0:
            receipt["sync_failed"] = True
            if args.json:
                print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
            else:
                print("Captures were saved locally, but configured sync failed; GitHub issues remain open.")
            return 1

        closed: list[int] = []
        close_failures: dict[int, str] = {}
        for number in sorted(captured_numbers):
            result = gh("issue", "close", str(number), "--repo", repo)
            if result.returncode == 0:
                closed.append(number)
            else:
                close_failures[number] = result.stderr.strip() or "could not close issue"
        receipt["closed"] = closed
        receipt["close_failed"] = close_failures
        if args.json:
            print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        else:
            for number, title, _body, _url, _created in issues:
                if number in imported_numbers:
                    print(f"Imported issue #{number} -> {title}")
            print(f"Summary: imported={len(imported_ids)}, already-present={len(existing_numbers)}, close-failed={len(close_failures)}")
        return 1 if close_failures else 0
    except (OSError, ValueError, KeyError, RuntimeError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
