"""Model-visible feedback without raw provider diagnostics or source fragments."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _shared import quality_feedback
from _shared.quality_feedback import feedback


class ModelFeedback(unittest.TestCase):
	def setUp(self) -> None:
		directory = tempfile.TemporaryDirectory()
		self.addCleanup(directory.cleanup)
		runtime = patch.object(
			quality_feedback, "runtime_directory", return_value=Path(directory.name)
		)
		runtime.start()
		self.addCleanup(runtime.stop)

	def delivery_fixture(self, base: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
		root = base / "project"
		root.mkdir()
		reports: list[dict[str, Any]] = [
			{
				"check": "ruff",
				"schema": 1,
				"status": "failed",
				"root": str(root),
				"engine_version": "2.6.1",
				"files": [
					{
						"path": "a.py",
						"sha256": "a" * 64,
						"status": "failed",
						"format": "passed",
						"findings": [
							{"check_id": "ruff", "rule": "F401", "line": 7, "column": 8},
							{"check_id": "ruff", "rule": "E501", "line": 80, "column": 89},
						],
					},
				],
				"selections": [{"policy_release": "v2.4.2", "runtime_fingerprint": "b" * 64}],
				"inputs": [{"path": "pyproject.toml", "sha256": "c" * 64}],
				"stderr": "synthetic-private-fragment",
			},
			{"check": "legacy_security", "status": "passed", "files": []},
		]
		arguments: dict[str, Any] = {
			"session_id": "delivery-test",
			"settings": {"provider_release": "v2.6.1", "enabled": ["ruff"]},
			"state_dir": base / "state",
			"evidence_dir": base / "evidence",
		}
		return reports, arguments

	def test_repeated_fresh_findings_keep_status_and_retain_full_source_free_report(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			reports, arguments = self.delivery_fixture(base)
			first = quality_feedback.deliver_feedback("PostToolUse", reports, **arguments)
			repeated = quality_feedback.deliver_feedback("PostToolUse", reports, **arguments)
			assert first is not None and repeated is not None
			self.assertIn("a.py:80:89", first["hookSpecificOutput"]["additionalContext"])
			context = repeated["hookSpecificOutput"]["additionalContext"]
			self.assertIn("Ruff FAILED", context)
			self.assertIn("unchanged", context)
			self.assertIn("verification repeated", context)
			self.assertIn("a.py:7:8", context)
			self.assertNotIn("a.py:80:89", context)
			evidence = list((base / "evidence").glob("*.json"))
			self.assertEqual(len(evidence), 1)
			retained = evidence[0].read_text(encoding="utf-8")
			self.assertIn("a.py:80:89", retained)
			self.assertNotIn("synthetic-private-fragment", json.dumps(repeated) + retained)

	def test_changed_provenance_and_sessions_restore_complete_feedback(self) -> None:
		cases = (
			("content", ("files", 0, "sha256"), "d" * 64),
			("engine", ("engine_version",), "2.6.2"),
			("runtime", ("selections", 0, "runtime_fingerprint"), "d" * 64),
			("policy", ("selections", 0, "policy_release"), "v2.5.0"),
			("tool", ("selections", 0, "ruff_sha256"), "d" * 64),
			("configuration", ("inputs", 0, "sha256"), "d" * 64),
		)
		for name, location, value in cases:
			with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
				reports, arguments = self.delivery_fixture(Path(directory))
				quality_feedback.deliver_feedback("PostToolUse", reports, **arguments)
				changed = deepcopy(reports)
				target: Any = changed[0]
				for part in location[:-1]:
					target = target[part]
				target[location[-1]] = value
				result = quality_feedback.deliver_feedback("PostToolUse", changed, **arguments)
				assert result is not None
				self.assertIn("a.py:80:89", result["hookSpecificOutput"]["additionalContext"])
		with tempfile.TemporaryDirectory() as directory:
			reports, arguments = self.delivery_fixture(Path(directory))
			quality_feedback.deliver_feedback("PostToolUse", reports, **arguments)
			for selection_arguments in (
				{**arguments, "session_id": "another-session"},
				{**arguments, "settings": {**arguments["settings"], "budget_seconds": 5}},
			):
				quality_feedback.deliver_feedback("PostToolUse", reports, **arguments)
				result = quality_feedback.deliver_feedback(
					"PostToolUse", reports, **selection_arguments
				)
				assert result is not None
				self.assertIn("a.py:80:89", result["hookSpecificOutput"]["additionalContext"])

	def test_repeated_integrity_findings_keep_unresolved_status_and_location(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			root = base / "project"
			root.mkdir()
			reports: list[dict[str, Any]] = [
				{
					"check": "integrity",
					"status": "failed",
					"root": str(root),
					"findings": [{"path": "merge.py", "line": 4}],
				}
			]
			arguments: dict[str, Any] = {
				"session_id": "integrity-delivery-test",
				"settings": {"provider_release": "v3.2.0", "enabled": ["integrity"]},
				"state_dir": base / "state",
				"evidence_dir": base / "evidence",
			}
			first = quality_feedback.deliver_feedback("PostToolUse", reports, **arguments)
			repeated = quality_feedback.deliver_feedback("PostToolUse", reports, **arguments)

			assert first is not None and repeated is not None
			context = repeated["hookSpecificOutput"]["additionalContext"]
			self.assertIn("Integrity FAILED", context)
			self.assertIn("unchanged findings", context)
			self.assertIn("verification repeated", context)
			self.assertIn("merge.py:4", context)
			self.assertIsNone(repeated.get("decision"))
			evidence = list((base / "evidence").glob("delivery-*.json"))
			self.assertEqual(len(evidence), 1)
			self.assertIn("merge.py:4", evidence[0].read_text(encoding="utf-8"))

	def test_repeated_integrity_feedback_does_not_hide_new_type_findings(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			root = base / "project"
			root.mkdir()
			reports: list[dict[str, Any]] = [
				{
					"check": "integrity",
					"status": "failed",
					"root": str(root),
					"findings": [{"path": "merge.py", "line": 4}],
				},
				{"check": "types", "status": "passed", "root": str(root), "findings": []},
			]
			arguments: dict[str, Any] = {
				"session_id": "integrity-type-delivery-test",
				"settings": {
					"provider_release": "v3.2.0",
					"enabled": ["integrity", "types"],
				},
				"state_dir": base / "state",
				"evidence_dir": base / "evidence",
			}
			first = quality_feedback.deliver_feedback("PostToolUse", reports, **arguments)
			changed = deepcopy(reports)
			changed[1].update(
				status="failed",
				findings=[{"path": "module.py", "line": 12, "column": 3, "rule": "attr-defined"}],
			)
			repeated = quality_feedback.deliver_feedback("PostToolUse", changed, **arguments)

			assert first is not None and repeated is not None
			context = repeated["hookSpecificOutput"]["additionalContext"]
			self.assertIn("merge.py:4", context)
			self.assertIn("module.py:12:3 mypy attr-defined", context)
			self.assertNotIn("unchanged findings", context)
			self.assertNotIn("verification repeated", context)

	def test_clean_followup_resets_repeated_findings_and_ignores_phase_counters(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			reports, arguments = self.delivery_fixture(Path(directory))
			quality_feedback.deliver_feedback("PostToolUse", reports, **arguments)
			changed = deepcopy(reports)
			changed[0].update(elapsed_ms=87, process_count=0)
			changed[0]["files"][0].update(changed=True, input_sha256="d" * 64)
			repeated = quality_feedback.deliver_feedback("PostToolUse", changed, **arguments)
			assert repeated is not None
			self.assertIn("unchanged", repeated["hookSpecificOutput"]["additionalContext"])
			clean = deepcopy(reports)
			clean[0]["status"] = "passed"
			clean[0]["files"][0].update(status="passed", findings=[])
			self.assertIsNone(quality_feedback.deliver_feedback("PostToolUse", clean, **arguments))
			result = quality_feedback.deliver_feedback("PostToolUse", reports, **arguments)
			assert result is not None
			self.assertIn("a.py:80:89", result["hookSpecificOutput"]["additionalContext"])

	def test_uncertainty_security_and_state_failure_preserve_full_feedback(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			reports, arguments = self.delivery_fixture(base)
			quality_feedback.deliver_feedback("PostToolUse", reports, **arguments)
			for status in ("unchecked", "failed"):
				changed = deepcopy(reports)
				changed[1]["status"] = status
				result = quality_feedback.deliver_feedback("PostToolUse", changed, **arguments)
				assert result is not None
				self.assertIn("a.py:80:89", result["hookSpecificOutput"]["additionalContext"])
				self.assertEqual(result.get("decision"), "block" if status == "failed" else None)
			state = base / "state" / "feedback" / "latest.json"
			state.write_bytes(b"{broken")
			result = quality_feedback.deliver_feedback("PostToolUse", reports, **arguments)
			assert result is not None
			self.assertIn("a.py:80:89", result["hookSpecificOutput"]["additionalContext"])
			self.assertEqual(state.read_bytes(), b"{broken")
			arguments["state_dir"] = base / "project" / "state"
			quality_feedback.deliver_feedback("PostToolUse", reports, **arguments)
			self.assertFalse(arguments["state_dir"].exists())
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			reports, arguments = self.delivery_fixture(base)
			arguments["evidence_dir"].write_bytes(b"unmanaged")
			quality_feedback.deliver_feedback("PostToolUse", reports, **arguments)
			result = quality_feedback.deliver_feedback("PostToolUse", reports, **arguments)
			assert result is not None
			self.assertIn("a.py:80:89", result["hookSpecificOutput"]["additionalContext"])
			self.assertEqual(arguments["evidence_dir"].read_bytes(), b"unmanaged")

	def test_secret_feedback_keeps_location_and_fingerprint_without_scanner_text(self) -> None:
		result = feedback(
			"PostToolUse",
			[
				{
					"check": "secrets",
					"status": "failed",
					"files": [
						{
							"path": "config.py",
							"findings": [
								{
									"check_id": "secrets.candidate",
									"line": 3,
									"fingerprint": "a" * 64,
									"message": "synthetic-private-fragment",
								}
							],
						}
					],
					"stderr": "synthetic-private-fragment",
				}
			],
		)
		assert result is not None
		context = result["hookSpecificOutput"]["additionalContext"]
		self.assertIn("config.py:3", context)
		self.assertIn("secrets.candidate", context)
		self.assertIn("a" * 64, context)
		self.assertIn("remove", context)
		self.assertNotIn("synthetic-private-fragment", json.dumps(result))

	def test_malformed_secret_feedback_is_unchecked_without_partial_findings(self) -> None:
		result = feedback(
			"PostToolUse",
			[
				{
					"check": "secrets",
					"status": "failed",
					"files": [
						{
							"path": "config.py",
							"findings": [
								{
									"check_id": "secrets.candidate",
									"line": 3,
									"fingerprint": "a" * 64,
								},
								{
									"check_id": "secrets.candidate",
									"line": 4,
									"fingerprint": "synthetic-private-fragment",
								},
							],
						}
					],
				}
			],
		)
		assert result is not None
		context = result["hookSpecificOutput"]["additionalContext"]
		self.assertIn("UNCHECKED", context)
		self.assertNotIn("config.py:3", context)
		self.assertNotIn("synthetic-private-fragment", json.dumps(result))

	def test_clean_and_inapplicable_results_are_silent(self) -> None:
		for status in ("passed", "not_applicable"):
			with self.subTest(status=status):
				self.assertIsNone(
					feedback("PostToolUse", [{"check": "ruff", "status": status, "files": []}])
				)

	def test_ruff_feedback_uses_model_context_and_final_rule_location(self) -> None:
		result = feedback(
			"PostToolUse",
			[
				{
					"check": "ruff",
					"status": "failed",
					"root": "/fixture",
					"files": [
						{
							"path": "a.py",
							"status": "failed",
							"findings": [
								{
									"rule": "F401",
									"line": 7,
									"column": 8,
									"action": "private-fragment",
								},
							],
						}
					],
					"stderr": "another-private-fragment",
				}
			],
		)
		assert result is not None
		self.assertEqual(result["hookSpecificOutput"]["hookEventName"], "PostToolUse")
		context = result["hookSpecificOutput"]["additionalContext"]
		self.assertIn("a.py:7:8", context)
		self.assertIn("F401", context)
		self.assertNotIn("private-fragment", json.dumps(result))
		self.assertNotIn("decision", result)

	def test_unknown_error_details_are_replaced_by_a_fixed_recovery_message(self) -> None:
		result = feedback(
			"PostToolUse", [{"check": "ruff", "status": "unchecked", "reason": "private-fragment"}]
		)
		assert result is not None
		context = result["hookSpecificOutput"]["additionalContext"]
		self.assertIn("UNCHECKED", context)
		self.assertNotIn("private-fragment", context)
		self.assertIn("restore", context.lower())

	def test_security_findings_preserve_blocking_without_echoing_source(self) -> None:
		result = feedback(
			"PostToolUse",
			[
				{
					"check": "legacy_security",
					"status": "failed",
					"files": [{"path": "a.py", "line": 2}],
					"reason": "private-fragment",
				}
			],
		)
		assert result is not None
		self.assertEqual(result["decision"], "block")
		self.assertIn("security", result["reason"].lower())
		self.assertNotIn("private-fragment", json.dumps(result))

	def test_large_feedback_retains_full_source_free_evidence_outside_project(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			reports = [
				{
					"check": "ruff",
					"status": "failed",
					"files": [
						{
							"path": "a.py",
							"status": "failed",
							"findings": [
								{"rule": "F401", "line": index + 1, "column": 1}
								for index in range(200)
							],
						}
					],
				}
			]
			result = feedback("PostToolUse", reports, evidence_dir=Path(directory))
			assert result is not None
			context = result["hookSpecificOutput"]["additionalContext"]
			self.assertLessEqual(len(context), 6000)
			self.assertIn("Full", context)
			files = list(Path(directory).glob("*.json"))
			self.assertEqual(len(files), 1)
			retained = files[0].read_text(encoding="utf-8")
			self.assertIn("a.py:200:1", retained)

	def test_feedback_formats_reports_after_the_previous_thirty_two_item_limit(self) -> None:
		reports: list[dict[str, Any]] = [
			{"check": "attribution", "status": "unchecked", "reason": "stale_result"},
			*[
				{
					"check": "types",
					"status": "failed",
					"findings": [
						{
							"path": f"project-{index:02}.py",
							"line": index + 1,
							"column": 1,
							"rule": "attr-defined",
						}
					],
				}
				for index in range(32)
			],
		]
		result = feedback("PostToolUse", reports)
		assert result is not None
		context = result["hookSpecificOutput"]["additionalContext"]
		self.assertIn("project-31.py:32:1 mypy attr-defined", context)


class ActionableFeedback(unittest.TestCase):
	def test_types_dependency_failure_preserves_reason_and_input_scope(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			result = feedback(
				"Stop",
				[
					{
						"check": "types",
						"status": "unchecked",
						"reason": "dependencies",
						"root": str(root),
						"input_paths": ["bot/tests/test_http_utils.py"],
					}
				],
				evidence_dir=root,
			)
			assert result is not None
			context = result["hookSpecificOutput"]["additionalContext"]
			self.assertIn("reason=dependencies", context)
			self.assertIn("dependencies and typing stubs", context)
			self.assertIn(str(root), context)
			self.assertIn("bot/tests/test_http_utils.py", context)
			self.assertIn("Otherwise pause the task", context)

	def test_types_io_does_not_misidentify_integrity_as_the_failed_check(self) -> None:
		result = feedback("Stop", [{"check": "types", "status": "unchecked", "reason": "io"}])
		assert result is not None
		context = result["hookSpecificOutput"]["additionalContext"]
		self.assertIn("reason=io", context)
		self.assertNotIn("retry integrity verification", context)

	def test_unchecked_scope_rejects_unsafe_paths_and_unknown_reason_text(self) -> None:
		result = feedback(
			"Stop",
			[
				{
					"check": "types",
					"status": "unchecked",
					"reason": "private-fragment",
					"root": "invalid\nprivate-fragment",
					"input_paths": ["../private-fragment", "bad\nprivate-fragment"],
				}
			],
		)
		assert result is not None
		self.assertNotIn("private-fragment", json.dumps(result))

	def test_incomplete_verification_has_recovery_and_explicit_action(self) -> None:
		result = feedback(
			"PostToolUse", [{"check": "types", "status": "unchecked", "reason": "runtime"}]
		)
		assert result is not None
		context = result["hookSpecificOutput"]["additionalContext"]
		self.assertIn("types UNCHECKED", context)
		self.assertIn("ACTION REQUIRED", context)
		self.assertIn("Immediately report", context)
		self.assertIn("repository, affected paths", context)
		self.assertIn("source-free evidence path", context)
		self.assertIn("Otherwise pause the task and await the user's instructions", context)
		self.assertIn("Do not claim verification passed", context)
		self.assertIn("user has already authorized diagnosing or repairing", context)
		self.assertIn("continue within that authorized scope", context)
		self.assertIn("do not require renewed approval", context)

	def test_findings_do_not_claim_incomplete_verification(self) -> None:
		result = feedback("Stop", [{"check": "integrity", "status": "failed"}])
		assert result is not None
		context = result["hookSpecificOutput"]["additionalContext"]
		self.assertIn("Fix the reported findings within the task scope", context)
		self.assertIn("The hook will check the next edit automatically", context)
		self.assertNotIn("rerun", context)
		self.assertNotIn("repeat verification", context)
		self.assertNotIn("UNCHECKED", context)
		self.assertNotIn("Pause the task", context)

	def test_missing_diagnostics_require_reporting_even_for_a_failed_status(self) -> None:
		for check in ("types", "secrets"):
			with self.subTest(check=check):
				result = feedback("Stop", [{"check": check, "status": "failed"}])
				assert result is not None
				context = result["hookSpecificOutput"]["additionalContext"]
				self.assertIn("UNCHECKED", context)
				self.assertIn("Otherwise pause the task and await the user's instructions", context)

	def test_truncation_keeps_reporting_instruction_for_late_unchecked_results(self) -> None:
		reports = [
			{"check": "attribution", "status": "unchecked", "reason": "runtime"} for _ in range(100)
		]
		with tempfile.TemporaryDirectory() as directory:
			for unavailable in (False, True):
				with (
					self.subTest(storage_unavailable=unavailable),
					patch.object(
						quality_feedback,
						"atomic_json",
						side_effect=OSError("private-fragment") if unavailable else None,
					),
				):
					result = feedback("Stop", reports, evidence_dir=Path(directory))
					assert result is not None
					context = result["hookSpecificOutput"]["additionalContext"]
					self.assertLessEqual(len(context), 6000)
					self.assertIn(
						"Otherwise pause the task and await the user's instructions", context
					)
					self.assertNotIn("private-fragment", context)

	def test_clean_pending_and_inapplicable_results_remain_silent(self) -> None:
		for status in ("passed", "pending", "not_applicable"):
			with self.subTest(status=status):
				self.assertIsNone(feedback("PostToolUse", [{"check": "types", "status": status}]))


if __name__ == "__main__":
	unittest.main()
