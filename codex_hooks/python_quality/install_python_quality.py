#!/usr/bin/env python3
"""Expose the shared Python Quality activation lifecycle in its hook folder."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _shared.quality_install import main  # noqa: E402

if __name__ == "__main__":
	raise SystemExit(main())
