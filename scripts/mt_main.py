#!/usr/bin/env python3
"""Thin CLI entry point so cli.py is imported and bytecode-cached."""
from __future__ import annotations

import sys

import cli

if hasattr(cli, "main"):
    try:
        raise SystemExit(cli.main())
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
