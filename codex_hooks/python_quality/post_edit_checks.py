#!/usr/bin/env python3
"""Managed routing to the verified Python quality coordinator."""

import sys
from pathlib import Path

sys.dont_write_bytecode = True
source = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(source))
from _shared.hook_registration import codex_home  # noqa: E402
from _shared.quality_install import route_post_edit  # noqa: E402

if __name__ == "__main__":
	route_post_edit(codex_home())
