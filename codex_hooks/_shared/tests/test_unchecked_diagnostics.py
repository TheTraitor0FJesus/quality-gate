"""Observable diagnostics for incomplete checkout-hook verification."""

from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import IO, Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _shared.quality_coordinator import coordinate
from _shared.quality_diagnostics import KNOWN_REASONS, failure, read_record, retain
from _shared.quality_events import attribute
from _shared.quality_feedback import feedback
from _shared.quality_storage import StorageBusyError, directory_transaction


class UncheckedDiagnostics(unittest.TestCase):
	def setUp(self) -> None:
		self.directory = tempfile.TemporaryDirectory()
		self.addCleanup(self.directory.cleanup)
		self.base = Path(self.directory.name)
		self.root = self.base / "project"
		self.root.mkdir()
		(self.root / ".git").mkdir()
		(self.root / "a.py").write_text("value = 1\n")
		self.payload = {
			"hook_event_name": "PreToolUse",
			"session_id": "session",
			"turn_id": "turn",
			"tool_use_id": "call",
			"tool_name": "Bash",
			"cwd": str(self.root),
			"tool_input": {"command": "opaque"},
		}

	def test_failed_pre_preserves_snapshot_dimension_for_paired_post(self) -> None:
		with patch("_shared.quality_events.MAX_SNAPSHOT_ENTRIES", 0):
			pre = attribute(self.payload, state_dir=self.base / "state")
		self.payload.update(hook_event_name="PostToolUse", tool_response={"exit_code": 0})
		post = attribute(self.payload, state_dir=self.base / "state")
		self.assertEqual(pre["status"], "unchecked")
		self.assertEqual(post["reason"], "snapshot_limit")
		self.assertEqual(post["diagnostic"]["limit_dimension"], "snapshot_entries")
		self.assertEqual(post["diagnostic"]["allowed"], 0)
		self.assertEqual(post["diagnostic"]["operation"], "snapshot_pre")

	def test_malformed_settings_keep_identity_and_call_scope(self) -> None:
		settings = self.base / "settings.json"
		settings.write_text("private invalid json")
		result = coordinate(
			self.payload,
			settings_path=settings,
			state_dir=self.base / "state",
			evidence_dir=self.base / "evidence",
		)
		text = json.dumps(result)
		self.assertIn("invalid_settings_json", text)
		self.assertIn("settings_read", text)
		self.assertIn("call", text)
		self.assertNotIn("private invalid json", text)

	def test_short_diagnostic_retains_safe_evidence_and_writer_uncertainty(self) -> None:
		report = {
			"check": "ruff",
			"status": "unchecked",
			"reason": "configuration",
			"root": str(self.root),
			"phase": "write",
			"writes_may_have_occurred": True,
			"diagnostic": {
				"operation": "provider_write",
				"errno": 13,
				"exception": "sensitive exception",
			},
		}
		result = feedback("PostToolUse", [report], evidence_dir=self.base / "evidence")
		text = json.dumps(result)
		self.assertIn("configuration", text)
		self.assertIn("errno=13", text)
		self.assertIn("final", text)
		self.assertNotIn("sensitive exception", text)
		files = list((self.base / "evidence").glob("diagnostic-*.json"))
		self.assertEqual(len(files), 1)
		evidence = files[0].read_text()
		self.assertIn('"errno": 13', evidence)
		self.assertNotIn("sensitive exception", evidence)

	def test_known_producer_codes_have_diagnostic_coverage(self) -> None:
		root = Path(__file__).resolve().parents[2]
		codes: set[str] = set()
		for path in root.rglob("*.py"):
			if "tests" in path.relative_to(root).parts:
				continue
			for node in ast.walk(ast.parse(path.read_text(encoding="utf8"))):
				if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
					if node.exc.args and isinstance(node.exc.args[0], ast.Constant):
						value = node.exc.args[0].value
						if (
							isinstance(value, str)
							and value.islower()
							and value.replace("_", "").isalnum()
						):
							codes.add(value)
				if isinstance(node, ast.Dict):
					for key, expression in zip(node.keys, node.values, strict=True):
						if (
							isinstance(key, ast.Constant)
							and key.value == "reason"
							and isinstance(expression, ast.Constant)
							and isinstance(expression.value, str)
						):
							codes.add(expression.value)
		from file_integrity.results import REASONS as INTEGRITY_REASONS
		from python_quality.check import REASONS as RUFF_REASONS
		from python_types.results import REASONS as TYPE_REASONS
		from secret_scan.results import REASONS as SECRET_REASONS

		self.assertEqual(
			(codes | INTEGRITY_REASONS | RUFF_REASONS | TYPE_REASONS | SECRET_REASONS)
			- KNOWN_REASONS,
			set(),
		)

	def test_pre_io_error_survives_post_without_exception_text(self) -> None:
		scandir = os.scandir

		def inspect(path: Path) -> AbstractContextManager[Iterator[os.DirEntry[str]]]:
			if path == self.root:
				raise PermissionError(13, "private-file-content")
			return scandir(path)

		with patch("os.scandir", side_effect=inspect):
			pre = attribute(self.payload, state_dir=self.base / "state")
		self.payload.update(hook_event_name="PostToolUse", tool_response={"exit_code": 0})
		post = attribute(self.payload, state_dir=self.base / "state")
		self.assertEqual(pre["status"], "unchecked")
		self.assertEqual(post["diagnostic"]["errno"], 13)
		self.assertEqual(post["diagnostic"]["operation"], "snapshot_pre")
		self.assertNotIn("private-file-content", json.dumps(post))

	def test_failed_pre_is_not_consumed_by_a_different_turn(self) -> None:
		with patch("_shared.quality_events.MAX_SNAPSHOT_ENTRIES", 0):
			attribute(self.payload, state_dir=self.base / "state")
		self.payload.update(hook_event_name="PostToolUse", turn_id="other")
		result = attribute(self.payload, state_dir=self.base / "state")
		self.assertEqual(result["status"], "unchecked")
		self.assertEqual(result["reason"], "invalid_snapshot")
		self.payload["turn_id"] = "turn"
		paired = attribute(self.payload, state_dir=self.base / "state")
		self.assertEqual(paired["reason"], "snapshot_limit")

	def test_corrupt_failed_pre_is_preserved_as_unchecked(self) -> None:
		with patch("_shared.quality_events.MAX_SNAPSHOT_ENTRIES", 0):
			attribute(self.payload, state_dir=self.base / "state")
		path = next((self.base / "state").glob("*/*.json"))
		path.write_bytes(b"{private-corruption")
		self.payload["hook_event_name"] = "PostToolUse"
		result = attribute(self.payload, state_dir=self.base / "state")
		self.assertEqual(result["status"], "unchecked")
		self.assertEqual(path.read_bytes(), b"{private-corruption")
		self.assertNotIn("private-corruption", json.dumps(result))

	def test_evidence_storage_failure_retains_bounded_correlation(self) -> None:
		report = {"check": "types", "status": "unchecked", "reason": "cancelled"}
		for error in (PermissionError(13, "private"), StorageBusyError("private")):
			with (
				self.subTest(error=type(error).__name__),
				patch("_shared.quality_diagnostics.directory_transaction", side_effect=error),
			):
				result = feedback("PostToolUse", [report], evidence_dir=self.base / "evidence")
				text = json.dumps(result)
				self.assertIn("id=", text)
				self.assertIn("evidence=unavailable", text)
				self.assertIn("cancelled", text)
				self.assertNotIn("private", text)

	def test_retention_preserves_unrelated_and_corrupt_records(self) -> None:
		directory = self.base / "evidence"
		directory.mkdir()
		foreign = directory / "unrelated.json"
		foreign.write_text("private unrelated")
		report = {"check": "types", "status": "unchecked", "reason": "deadline"}
		identity, path = retain(report, directory)
		assert path is not None
		Path(path).write_text("{corrupt")
		second_identity, second_path = retain(report, directory)
		self.assertEqual(second_identity, identity)
		self.assertIsNone(second_path)
		self.assertEqual(Path(path).read_text(), "{corrupt")
		self.assertEqual(foreign.read_text(), "private unrelated")

	def test_numeric_os_errors_reject_booleans_negative_and_arbitrary_values(self) -> None:
		for value in (True, -1, "private", 2**80):
			with self.subTest(value=value):
				error = OSError()
				with patch.object(error, "errno", value):
					result = failure(error, "background_launch", "process_unavailable")
				self.assertNotIn("errno", result["diagnostic"])

	def test_background_deadline_recovery_does_not_change_foreground_budget(self) -> None:
		result = feedback(
			"PostToolUse",
			[
				{
					"check": "types",
					"status": "unchecked",
					"reason": "deadline",
					"writes_may_have_occurred": False,
					"diagnostic": {"operation": "background_worker", "phase": "background"},
				}
			],
			evidence_dir=self.base / "evidence",
		)
		text = json.dumps(result)
		self.assertIn("background work", text)
		self.assertNotIn("adjust", text)
		self.assertIn("Writes from this check: none", text)

	def test_unknown_fields_never_enter_retained_evidence(self) -> None:
		identity, path = retain(
			{
				"check": "types",
				"status": "unchecked",
				"reason": "private unknown",
				"source": "private source",
				"diagnostic": {"operation": "private operation", "errno": "private error"},
			},
			self.base / "evidence",
		)
		assert path is not None
		self.assertEqual(len(identity), 64)
		text = Path(path).read_text()
		self.assertIn('"reason": "unknown"', text)
		self.assertNotIn("private", text)

	def test_storage_lock_contention_is_recoverable_without_overwriting_files(self) -> None:
		directory = self.base / "evidence"
		report = {"check": "types", "status": "unchecked", "reason": "cancelled"}
		with directory_transaction(directory):
			identity, path = retain(report, directory)
			self.assertIsNone(path)
		recovered_identity, recovered_path = retain(report, directory)
		self.assertEqual(recovered_identity, identity)
		self.assertIsNotNone(recovered_path)

	def test_retention_rejects_hardlinked_records_without_changing_their_target(self) -> None:
		directory = self.base / "evidence"
		report = {"check": "types", "status": "unchecked", "reason": "cancelled"}
		_, path = retain(report, directory)
		assert path is not None
		foreign = self.base / "foreign.json"
		os.link(path, foreign)
		before = foreign.read_bytes()
		_, retained = retain(report, directory)
		self.assertIsNone(retained)
		self.assertEqual(foreign.read_bytes(), before)

	def test_retention_rejects_linked_directory_without_changing_foreign_files(self) -> None:
		foreign = self.base / "foreign"
		foreign.mkdir()
		marker = foreign / "unrelated"
		marker.write_text("keep")
		linked = self.base / "linked"
		try:
			linked.symlink_to(foreign, target_is_directory=True)
		except OSError:
			self.skipTest("Directory symlinks are unavailable to this Windows account")
		_, path = retain({"check": "types", "status": "unchecked", "reason": "io"}, linked)
		self.assertIsNone(path)
		self.assertEqual(list(foreign.iterdir()), [marker])

	def test_failed_pre_size_bound_preserves_oversized_foreign_state(self) -> None:
		with patch("_shared.quality_events.MAX_SNAPSHOT_ENTRIES", 0):
			attribute(self.payload, state_dir=self.base / "state")
		path = next((self.base / "state").glob("*/*.json"))
		path.write_bytes(b"x" * 256)
		self.payload["hook_event_name"] = "PostToolUse"
		with patch("_shared.quality_events.MAX_EVENT_BYTES", 100):
			result = attribute(self.payload, state_dir=self.base / "state")
		self.assertEqual(result["status"], "unchecked")
		self.assertEqual(path.stat().st_size, 256)

	def test_snapshot_missing_does_not_assert_registration_failure(self) -> None:
		self.payload["hook_event_name"] = "PostToolUse"
		report = {"check": "attribution", **attribute(self.payload, state_dir=self.base / "state")}
		result = feedback("PostToolUse", [report], evidence_dir=self.base / "evidence")
		text = json.dumps(result)
		self.assertIn("snapshot_missing", text)
		self.assertNotIn("Restore paired", text)
		self.assertIn("not established", text)

	def test_snapshot_limits_distinguish_bytes_from_time(self) -> None:
		for dimension, patches in (
			("snapshot_bytes", [patch("_shared.quality_events.MAX_SNAPSHOT_BYTES", 0)]),
			("snapshot_seconds", [patch("_shared.quality_events.monotonic", return_value=100)]),
		):
			with self.subTest(dimension=dimension), patches[0]:
				result = attribute(
					self.payload,
					state_dir=self.base / dimension,
					deadline=50 if dimension == "snapshot_seconds" else None,
				)
				self.assertEqual(result["status"], "unchecked")
				self.assertEqual(result["diagnostic"]["limit_dimension"], dimension)

	def test_missing_and_unreadable_settings_keep_distinct_diagnostics(self) -> None:
		settings = self.base / "settings.json"
		missing = coordinate(
			self.payload,
			settings_path=settings,
			state_dir=self.base / "state",
			evidence_dir=self.base / "evidence",
		)
		self.assertIn("settings_missing", json.dumps(missing))
		settings.write_text("private bytes")
		with patch(
			"_shared.quality_coordinator.load_settings",
			side_effect=PermissionError(13, "private exception"),
		):
			unreadable = coordinate(
				self.payload,
				settings_path=settings,
				state_dir=self.base / "state",
				evidence_dir=self.base / "evidence",
			)

		text = json.dumps(unreadable)
		self.assertIn("settings_io_unavailable", text)
		self.assertIn("errno=13", text)
		self.assertNotIn("private", text)

	def test_failed_pre_publication_interruption_never_claims_clean(self) -> None:
		from _shared import quality_events

		publish = quality_events.atomic_json

		def interrupted(path: Path, document: dict) -> None:
			if "failure" in document:
				raise PermissionError(13, "private write failure")
			publish(path, document)

		with (
			patch.object(quality_events, "MAX_SNAPSHOT_ENTRIES", 0),
			patch.object(quality_events, "atomic_json", interrupted),
		):
			pre = attribute(self.payload, state_dir=self.base / "state")
		self.payload["hook_event_name"] = "PostToolUse"
		post = attribute(self.payload, state_dir=self.base / "state")
		self.assertEqual(pre["reason"], "snapshot_limit")
		self.assertEqual(post["status"], "unchecked")
		self.assertNotIn("private", json.dumps(post))

	def test_replaced_record_is_rejected_without_deleting_replacement(self) -> None:
		path = self.base / "record.json"
		path.write_text('{"schema": 1}')
		replacement = self.base / "replacement.json"
		replacement.write_text('{"unrelated": true}')
		original = Path.open

		def replace_before_open(target: Path, mode: str = "r") -> IO[Any]:
			if target == path and mode == "rb":
				os.replace(replacement, path)
			return original(target, mode)

		with patch.object(Path, "open", replace_before_open), self.assertRaises(ValueError):
			read_record(path)
		self.assertEqual(path.read_text(), '{"unrelated": true}')

	def test_numeric_windows_error_is_retained_without_os_message(self) -> None:
		error = PermissionError(13, "private OS message")
		with patch.object(error, "winerror", 5, create=True):
			report = {
				"check": "background",
				**failure(error, "background_launch", "process_unavailable"),
			}
		result = feedback("PostToolUse", [report], evidence_dir=self.base / "evidence")
		text = json.dumps(result)
		self.assertIn("winerror=5", text)
		self.assertIn("background_launch", text)
		self.assertNotIn("private OS message", text)

	def test_post_cleanup_failure_preserves_originating_pre_limit(self) -> None:
		with patch("_shared.quality_events.MAX_SNAPSHOT_ENTRIES", 0):
			attribute(self.payload, state_dir=self.base / "state")
		self.payload["hook_event_name"] = "PostToolUse"
		with patch(
			"_shared.quality_events.clear_snapshot",
			side_effect=PermissionError(13, "private cleanup"),
		):
			post = attribute(self.payload, state_dir=self.base / "state")
		self.assertEqual(post["reason"], "snapshot_limit")
		self.assertEqual(post["diagnostic"]["phase"], "pre")
		self.assertNotIn("private cleanup", json.dumps(post))

	def test_pre_state_write_failure_identifies_storage_operation(self) -> None:
		with patch(
			"_shared.quality_events.atomic_json", side_effect=PermissionError(13, "private storage")
		):
			result = attribute(self.payload, state_dir=self.base / "state")
		self.assertEqual(result["status"], "unchecked")
		self.assertEqual(result["diagnostic"]["operation"], "snapshot_store")
		self.assertEqual(result["diagnostic"]["errno"], 13)
		self.assertNotIn("private storage", json.dumps(result))

	def test_pre_capture_preserves_a_replaced_foreign_record(self) -> None:
		state = self.base / "state"
		foreign = self.base / "foreign.json"
		content = b'{"root": "unrelated", "session_id": "foreign"}'
		foreign.write_bytes(content)
		scandir = os.scandir
		paths: list[Path] = []

		def replace_capture(path: Path) -> AbstractContextManager[Iterator[os.DirEntry[str]]]:
			if path == self.root:
				paths.extend(state.glob("*/*.json"))
				os.replace(foreign, paths[0])
			return scandir(path)

		with patch("os.scandir", side_effect=replace_capture):
			result = attribute(self.payload, state_dir=state)
		self.assertEqual(result["status"], "unchecked")
		self.assertEqual(paths[0].read_bytes(), content)

	def test_failed_shell_retains_known_operation_and_changed_input_scope(self) -> None:
		attribute(self.payload, state_dir=self.base / "state")
		(self.root / "a.py").write_text("value = 2\n")
		self.payload.update(hook_event_name="PostToolUse", tool_response={"exit_code": 1})
		report = {"check": "attribution", **attribute(self.payload, state_dir=self.base / "state")}
		result = feedback("PostToolUse", [report], evidence_dir=self.base / "evidence")
		text = json.dumps(result)
		self.assertEqual(report["status"], "unchecked")
		self.assertIn("operation=shell_completion", text)
		self.assertIn("phase=post", text)
		self.assertIn("Input path: a.py", text)


if __name__ == "__main__":
	unittest.main()
