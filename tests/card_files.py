"""Test helper: list card files in a workspace's cards/ folder, optionally by kind."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import tasklib  # noqa: E402


def card_files(root: Path, kind: str | None = None) -> list[Path]:
    folder = Path(root) / "cards"
    paths = sorted(folder.glob("*.md")) if folder.is_dir() else []
    return [path for path in paths if kind is None or tasklib.card_kind_hint(path) == kind]
