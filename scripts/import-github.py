#!/usr/bin/env python3
"""CLI wrapper for scripts/import_github.py."""
from __future__ import annotations

import import_github
from workspace import Workspace

if __name__ == "__main__":
    raise SystemExit(import_github.main(None, Workspace.resolve()))
