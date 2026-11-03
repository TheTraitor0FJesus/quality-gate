"""Selected read-only hooks share one deadline and preserve security reserve."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _shared.quality_coordinator import read_only_reports


def test_integrity_reader_uses_bounded_window_without_duplicate_secret_read() -> None:
	root = Path.cwd()
	files = [{"path": "a.py", "sha256": "a" * 64}]
	settings = {"enabled": ["ruff", "secrets", "integrity"]}
	with (
		patch("_shared.quality_coordinator.monotonic", return_value=100.0),
		patch("secret_scan.check.check_files", return_value={"check": "secrets"}) as secrets,
		patch("file_integrity.check.check_files", return_value={"check": "integrity"}) as integrity,
	):
		reports = read_only_reports(root, files, settings, 110.0)

	assert reports == [{"check": "integrity", "root": str(root)}]
	secrets.assert_not_called()
	assert integrity.call_args.args == (root, files, settings, 109.6)


def test_one_selected_reader_receives_the_existing_reader_window() -> None:
	root = Path.cwd()
	files = [{"path": "a.py", "sha256": "a" * 64}]
	settings = {"enabled": ["ruff", "integrity"]}
	with (
		patch("_shared.quality_coordinator.monotonic", return_value=100.0),
		patch("file_integrity.check.check_files", return_value={"check": "integrity"}) as integrity,
	):
		reports = read_only_reports(root, files, settings, 110.0)

	assert reports == [{"check": "integrity", "root": str(root)}]
	assert integrity.call_args.args == (root, files, settings, 109.6)
