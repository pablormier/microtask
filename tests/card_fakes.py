"""Test helper: cards with the attributes card_query reads, without files or a workspace."""
from __future__ import annotations

from pathlib import Path


class FakeCard:
    def __init__(self, identity: str, kind: str = "task", *, body: str = "", next_action: str = "", **props):
        defaults = ({"status": "active", "priority": "medium", "type": "task"} if kind == "task" else {})
        self.frontmatter = {"id": identity, "kind": kind, "tags": [], **defaults, **props}
        self.kind = kind
        self.title = str(props.get("title", identity))
        self.body = body
        self.next_action = next_action
        self.path = Path(identity)
