"""Model-visible typing diagnostics without trusting scanner text or scope."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class TypingFeedback(unittest.TestCase):
	def report(self, status: str = "failed") -> str:
		return json.dumps(
			{
				"schema": 1,
				"check_id": "mypy",
				"status": status,
				"findings": [
					{
						"path": "src/consumer.py",
						"line": 3,
						"column": 7,
						"rule": "arg-type",
						"message": "private-source-fragment",
					}
				]
				if status == "failed"
				else [],
				"stderr": "private-source-fragment",
			}
		)

	def render(self, output: str) -> dict | None:
		from python_types.feedback import typing_feedback

		return typing_feedback(output, analysis_paths={"src/changed.py", "src/consumer.py"})

	def test_dependent_file_location_and_rule_reach_model_without_raw_text(self) -> None:
		stdout, stderr = io.StringIO(), io.StringIO()
		with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
			result = self.render(self.report())
		assert result is not None
		context = result["hookSpecificOutput"]["additionalContext"]
		self.assertEqual(result["hookSpecificOutput"]["hookEventName"], "PostToolUse")
		self.assertIn("src/consumer.py:3:7 mypy arg-type", context)
		self.assertIn("resolve", context)
		self.assertNotIn("private-source-fragment", json.dumps(result))
		self.assertNotIn("decision", result)
		self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")

	def test_success_and_explicit_opt_out_are_silent(self) -> None:
		for status in ("passed", "not_applicable"):
			with self.subTest(status=status):
				self.assertIsNone(self.render(self.report(status)))

	def test_pending_cancelled_undelivered_and_waived_are_never_success(self) -> None:
		for status in ("pending", "cancelled", "undelivered", "waived", "unchecked"):
			with self.subTest(status=status):
				result = self.render(self.report(status))
				assert result is not None
				self.assertIn("types UNCHECKED", result["hookSpecificOutput"]["additionalContext"])

	def test_out_of_scope_or_malformed_diagnostics_reject_the_entire_projection(self) -> None:
		base = json.loads(self.report())
		changes: list[dict[str, object]] = [
			{"path": "../outside.py"},
			{"path": "src/unknown.py"},
			{"path": "src/consumer.py\nforged"},
			{"line": True},
			{"line": 0},
			{"column": -1},
			{"rule": "arg-type\nprivate-source-fragment"},
		]
		for change in changes:
			data = {**base, "findings": [*base["findings"], {**base["findings"][0], **change}]}
			with self.subTest(change=change):
				result = self.render(json.dumps(data))
				assert result is not None
				context = result["hookSpecificOutput"]["additionalContext"]
				self.assertIn("types UNCHECKED", context)
				self.assertNotIn("arg-type", context)

	def test_inconsistent_or_ambiguous_json_remains_incomplete(self) -> None:
		base = json.loads(self.report())
		cases = ["null", "[]", "private-source-fragment", '{"schema":1,"schema":1}']
		cases.append(self.report().replace('"schema": 1', '"schema": 1, "schema": 1'))
		changes: list[dict[str, Any]] = [
			{"schema": True},
			{"check_id": "other"},
			{"status": []},
			{"status": "passed"},
			{"findings": []},
			{"findings": None},
		]
		for change in changes:
			cases.append(json.dumps({**base, **change}))
		for output in cases:
			with self.subTest(output=output[:60]):
				result = self.render(output)
				assert result is not None
				self.assertIn("types UNCHECKED", result["hookSpecificOutput"]["additionalContext"])

	def test_invalid_analysis_inventory_cannot_make_a_clean_result_silent(self) -> None:
		from python_types.feedback import typing_feedback

		inventories: list[Any] = [
			{None},
			{"."},
			{"../escape.py"},
			{"src/a.py\nforged"},
			{"src//a.py"},
		]
		for inventory in inventories:
			with self.subTest(inventory=inventory):
				result = typing_feedback(self.report("passed"), analysis_paths=inventory)
				assert result is not None
				self.assertIn("types UNCHECKED", result["hookSpecificOutput"]["additionalContext"])

	def test_duplicate_diagnostics_are_collapsed_and_large_evidence_is_source_free(self) -> None:
		from python_types.feedback import typing_feedback

		data = json.loads(self.report())
		data["findings"].append(dict(data["findings"][0]))
		result = self.render(json.dumps(data))
		assert result is not None
		self.assertEqual(
			result["hookSpecificOutput"]["additionalContext"].count("src/consumer.py:3:7"), 1
		)
		data["findings"] = [{**data["findings"][0], "line": line} for line in range(1, 201)]
		with tempfile.TemporaryDirectory() as directory:
			evidence = Path(directory)
			result = typing_feedback(
				json.dumps(data), analysis_paths={"src/consumer.py"}, evidence_dir=evidence
			)
			assert result is not None
			context = result["hookSpecificOutput"]["additionalContext"]
			self.assertLessEqual(len(context), 6000)
			self.assertIn("Full source-free evidence", context)
			reports = list(evidence.glob("*.json"))
			self.assertEqual(len(reports), 1)
			retained = reports[0].read_text(encoding="utf-8")
			self.assertIn("src/consumer.py:200:7", retained)
			self.assertNotIn("private-source-fragment", retained)

	def test_oversized_projection_or_diagnostic_count_is_incomplete(self) -> None:
		data = json.loads(self.report())
		data["findings"] *= 201
		for output in (json.dumps(data), self.report() + " " * (256 * 1024)):
			result = self.render(output)
			assert result is not None
			self.assertIn("types UNCHECKED", result["hookSpecificOutput"]["additionalContext"])


if __name__ == "__main__":
	unittest.main()
