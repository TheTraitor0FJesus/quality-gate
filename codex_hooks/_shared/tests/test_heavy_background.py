"""Heavy fallback is queued, cancellable and bound to current captured bytes."""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from time import monotonic
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _shared import quality_background as background
from _shared import quality_background_worker as worker
from _shared.quality_coordinator import check_group


class HeavyBackground(unittest.TestCase):
	def test_foreground_runs_builtin_and_queues_heavy_instead_of_waiting(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory).resolve()
			content = b"VALUE: int = 1\n"
			(root / "a.py").write_bytes(content)
			files = [
				{
					"path": "a.py",
					"operation": "modified",
					"sha256": hashlib.sha256(content).hexdigest(),
				}
			]
			settings = {"enabled": ["ruff"]}
			with (
				patch.dict(os.environ, {"CODEX_POST_EDIT_HEAVY_SECURITY": "1"}),
				patch("python_quality.check.check_files", return_value={"status": "passed"}),
				patch(
					"_shared.quality_coordinator.legacy_security",
					return_value={"check": "builtin_secrets", "status": "passed", "files": []},
				) as builtin,
				patch.object(
					background, "schedule", return_value={"status": "pending"}
				) as schedule,
			):
				check_group(
					root,
					files,
					settings,
					monotonic() + 10,
					background=background.BackgroundSession(
						root / "settings.json", root / "state", "actor"
					),
				)
			self.assertTrue(builtin.call_args.kwargs["builtin_only"])
			self.assertEqual(schedule.call_args.kwargs["checks"], ["ruff", "legacy_security"])
			self.assertEqual((root / "a.py").read_bytes(), content)

	def test_worker_owns_heavy_deadline_and_cancellation(self) -> None:
		cancel = threading.Event()
		document = {
			"request": {
				"root": str(Path.cwd()),
				"files": [],
				"baseline": {"policy_release": "v3.1.0"},
			},
			"settings": {"provider_release": "v4.2.1"},
			"checks": ["legacy_security"],
		}
		with (
			patch.object(worker, "release_inventory", return_value={}),
			patch.object(worker, "digest", return_value="engine"),
			patch.object(
				worker,
				"run_scanner",
				return_value={"check": "legacy_security", "status": "passed", "files": []},
			) as scan,
		):
			_summary, wire, _identity = worker.analyze(document, 1000.0, cancel)
		self.assertIsNotNone(wire)
		self.assertTrue(scan.call_args.kwargs["heavy"])
		self.assertIs(scan.call_args.kwargs["cancel_event"], cancel)
		self.assertEqual(scan.call_args.args[2], 1000.0)

	def test_validation_rejects_stale_or_unrequested_findings(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory).resolve()
			content = b"VALUE: int = 1\n"
			(root / "a.py").write_bytes(content)
			request = {
				"root": str(root),
				"files": [{"path": "a.py", "sha256": hashlib.sha256(content).hexdigest()}],
			}
			valid = {"check": "legacy_security", "status": "failed", "files": [{"path": "a.py"}]}
			self.assertEqual(
				background._validate_legacy_security(valid, request, monotonic() + 10), valid
			)
			invalid = {**valid, "files": [{"path": "foreign.py"}]}
			self.assertEqual(
				background._validate_legacy_security(invalid, request, monotonic() + 10)["status"],
				"unchecked",
			)
			(root / "a.py").write_bytes(b"CHANGED = True\n")
			self.assertEqual(
				background._validate_legacy_security(valid, request, monotonic() + 10)["status"],
				"unchecked",
			)

	def test_selection_requires_explicit_heavy_opt_in_and_no_gate_secret_reader(self) -> None:
		with patch.dict(os.environ, {"CODEX_POST_EDIT_HEAVY_SECURITY": "0"}):
			with self.assertRaises(ValueError):
				background.selected_checks({"enabled": ["ruff"]}, ["legacy_security"])
		with patch.dict(os.environ, {"CODEX_POST_EDIT_HEAVY_SECURITY": "1"}):
			self.assertEqual(
				background.selected_checks({"enabled": ["ruff"]}, ["legacy_security"]),
				["legacy_security"],
			)
			with self.assertRaises(ValueError):
				background.selected_checks({"enabled": ["secrets"]}, ["legacy_security"])


class HeavyScannerAvailability(unittest.TestCase):
	def test_optional_scanner_probe_does_not_execute_package_imports(self) -> None:
		import subprocess

		from _shared import codex_hook_adapter as adapter

		with (
			patch.object(adapter, "find_spec", return_value=object()),
			patch.dict(sys.modules, {"detect_secrets": None}),
			patch.object(
				adapter.subprocess,
				"run",
				return_value=subprocess.CompletedProcess([], 0, '{"results": {}}'),
			),
		):
			self.assertEqual(adapter.run_detect_secrets(Path("example.py")), [])

	def test_tool_error_is_unchecked_and_never_a_false_finding_or_clean_pass(self) -> None:
		self._assert_failed_scanner_child('{"results": {}}', 1)
		self._assert_failed_scanner_child("invalid report", 0)

	def _assert_failed_scanner_child(self, output: str, code: int) -> None:
		import subprocess

		from _shared import codex_hook_adapter as adapter

		with (
			patch.object(adapter, "find_spec", return_value=object()),
			patch.object(
				adapter.subprocess,
				"run",
				return_value=subprocess.CompletedProcess([], code, output),
			),
		):
			findings = adapter.run_detect_secrets(Path("example.py"))
		self.assertTrue(any(item.get("tool") == "HOOK" for item in findings))

	def test_real_finding_survives_scanner_child_failure(self) -> None:
		import subprocess

		from _shared import codex_hook_adapter as adapter

		output = '{"results": {"example.py": [{"line_number": 1, "type": "fixture"}]}}'
		with (
			patch.object(adapter, "find_spec", return_value=object()),
			patch.object(
				adapter.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, output)
			),
		):
			findings = adapter.run_detect_secrets(Path("example.py"))
		self.assertTrue(any(item.get("tool") == "SECRET" for item in findings))

	def test_scanner_error_is_unchecked_in_the_legacy_reader(self) -> None:
		from _shared import codex_hook_adapter as adapter
		from _shared.quality_legacy_security import legacy_blocks

		error = {"tool": "HOOK", "check_id": "runtime"}
		with (
			patch.object(adapter, "run_builtin_secret_scan", return_value=[]),
			patch.object(adapter, "run_semgrep", return_value=[error]),
			patch.object(adapter, "run_detect_secrets", return_value=[]),
			self.assertRaisesRegex(ValueError, "security_unavailable"),
		):
			legacy_blocks(Path("example.py"), heavy=True)

	def test_real_finding_survives_another_scanners_error(self) -> None:
		from _shared import codex_hook_adapter as adapter
		from _shared.quality_legacy_security import legacy_blocks

		with (
			patch.object(adapter, "run_builtin_secret_scan", return_value=[{"tool": "SECRET"}]),
			patch.object(adapter, "run_semgrep", return_value=[{"tool": "HOOK"}]),
			patch.object(adapter, "run_detect_secrets", return_value=[]),
		):
			self.assertTrue(legacy_blocks(Path("example.py"), heavy=True))
