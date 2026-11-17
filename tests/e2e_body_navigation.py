#!/usr/bin/env python3
"""Compatibility entrypoint for the production Scarpet navigation matrix."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.e2e_server_navigation_matrix import main  # noqa: E402


if __name__ == "__main__":
    main()
