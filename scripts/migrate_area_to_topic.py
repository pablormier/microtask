#!/usr/bin/env python3
"""One-time migration: rename the `area` card property to `topic`.

Run from the workspace clone (each workspace has its own copy of the scripts);
the local demo copy under .mt/demo is migrated too when present. Use --plan to preview, then --apply. Cards are rewritten through the normal
transaction path (mutation lock, validation, rollback); drafts under drafts/
get the same property rename, and policy `suggested_areas` becomes `suggested_topics`. Nothing is staged, committed, or synced.
Delete this script once every workspace has been migrated.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import card_format  # noqa: E402
import tasklib  # noqa: E402

AREA_LINE = re.compile(r"(?m)^area:")


def card_changes(root: Path) -> tuple[dict[str, bytes], dict[str, bytes | None]]:
    changes: dict[str, bytes] = {}
    original: dict[str, bytes | None] = {}
    for path in sorted((root / "cards").glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        properties, body = card_format.parse_document(raw)
        if "area" not in properties:
            continue
        area = properties.pop("area")
        if "topic" in properties:
            raise ValueError(f"cards/{path.name}: has both area and topic; resolve by hand")
        if area is not None:
            properties["topic"] = card_format.normalize_topic(str(area))
        relative = f"cards/{path.name}"
        changes[relative] = card_format.render_document(properties, body).encode("utf-8")
        original[relative] = raw.encode("utf-8")
    return changes, original


def draft_changes(root: Path) -> dict[Path, str]:
    drafts: dict[Path, str] = {}
    for path in sorted((root / "drafts").glob("*.md")):
        text = path.read_text(encoding="utf-8")
        frontmatter, _body = card_format.parse_frontmatter(text)
        if frontmatter is not None and AREA_LINE.search(frontmatter):
            head = text.index(frontmatter)
            drafts[path] = text[:head] + AREA_LINE.sub("topic:", frontmatter) + text[head + len(frontmatter):]
    return drafts


def policy_rename() -> tuple[Path, str] | None:
    """The policy file and its text with suggested_areas renamed, when it needs renaming."""
    policy_file = tasklib.REPO_ROOT / "mt-policy.yaml"
    if not policy_file.is_file():
        return None
    text = policy_file.read_text(encoding="utf-8")
    renamed = re.sub(r"(?m)^suggested_areas:", "suggested_topics:", text)
    return (policy_file, renamed) if renamed != text else None


@tasklib.serialized_time_mutation
def apply(roots: list[Path]) -> tuple[int, int]:
    total_cards = total_drafts = 0
    for root in roots:
        changes, original = card_changes(root)
        drafts = draft_changes(root)
        if changes:
            tasklib.apply_item_transaction(changes, expected=original, source_root=root)
        for path, text in drafts.items():
            tasklib.atomic_write(path, text.encode("utf-8"))
        total_cards += len(changes)
        total_drafts += len(drafts)
    # Last, so a failed card transaction leaves the policy unchanged.
    renamed = policy_rename()
    if renamed:
        tasklib.atomic_write(renamed[0], renamed[1].encode("utf-8"))
    return total_cards, total_drafts


def workspace_roots() -> list[Path]:
    demo = tasklib.REPO_ROOT / ".mt" / "demo"
    return [tasklib.REPO_ROOT, *([demo] if (demo / "cards").is_dir() else [])]


def main() -> int:
    parser = argparse.ArgumentParser(description="Rename the area property to topic (one-time).")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true", help="list the cards and drafts that would change as JSON")
    mode.add_argument("--apply", action="store_true", help="perform the migration")
    args = parser.parse_args()
    try:
        if args.plan:
            report = {}
            for root in workspace_roots():
                changes, _ = card_changes(root)
                report[str(root)] = {"cards": sorted(changes),
                                     "drafts": [f"drafts/{p.name}" for p in draft_changes(root)]}
            report["policy"] = {"suggested_areas_renamed": policy_rename() is not None}
            print(json.dumps(report, indent=2))
            return 0
        cards, drafts = apply(workspace_roots())
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Renamed area to topic in {cards} card(s) and {drafts} draft(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
