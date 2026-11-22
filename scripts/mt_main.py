#!/usr/bin/env python3
"""Thin CLI entry point so cli.py is imported and bytecode-cached."""
from __future__ import annotations

import cli

if hasattr(cli, "main"):
    raise SystemExit(cli.main())
