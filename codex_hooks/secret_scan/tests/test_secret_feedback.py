"""Presentation acceptance independent of the pending scanner provider contract."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from itertools import product
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from secret_scan.feedback import secret_feedback


class SecretFeedback(unittest.TestCase):
	def test_provider_waived_findings_are_silent_and_mixed_results_show_only_failures(self) -> None:
		from _shared.quality_feedback import feedback

		files = [
			{
				"path": "a.py",
				"findings": [
					{
						"check_id": "secrets.candidate",
						"line": 1,
						"fingerprint": "a" * 64,
						"status": "waived",
					},
					{
						"check_id": "secrets.candidate",
						"line": 2,
						"fingerprint": "b" * 64,
						"status": "failed",
					},
				],
			}
		]
		waived_files = [{"path": "a.py", "findings": files[0]["findings"][:1]}]
		self.assertIsNone(
			feedback(
				"PostToolUse", [{"check": "secrets", "status": "waived", "files": waived_files}]
			)
		)
		result = feedback("PostToolUse", [{"check": "secrets", "status": "failed", "files": files}])
		assert result is not None
		context = result["hookSpecificOutput"]["additionalContext"]
		self.assertNotIn("a.py:1", context)
		self.assertNotIn("a" * 64, context)
		self.assertIn("a.py:2", context)
		self.assertIn("b" * 64, context)

	def report(self, status: str = "failed", count: int = 1) -> str:
		return json.dumps(
			{
				"schema": 1,
				"check_id": "secrets.candidate",
				"status": status,
				"findings": [
					{
						"path": "src/пример file.py",
						"line": index + 1,
						"fingerprint": "a" * 64,
						"Secret": "synthetic-private-fragment",
						"Match": "synthetic-source-fragment",
					}
					for index in range(count)
				],
				"stderr": "synthetic-private-fragment",
				"message": "synthetic-source-fragment",
			}
		)

	def test_duplicate_secret_messages_are_collapsed_without_hiding_status(self) -> None:
		report = json.loads(self.report())
		report["findings"].append(dict(report["findings"][0]))
		result = self.render(json.dumps(report))
		assert result is not None
		context = result["hookSpecificOutput"]["additionalContext"]
		self.assertEqual(context.count("file.py:1 secrets.candidate"), 1)
		self.assertIn("remove", context)

	def render(self, report: str, *, evidence_dir: Path | None = None) -> dict | None:
		return secret_feedback(
			report, touched_paths={"src/пример file.py"}, evidence_dir=evidence_dir
		)

	def test_finding_has_exact_identity_and_no_raw_fields_or_console_output(self) -> None:
		stdout, stderr = io.StringIO(), io.StringIO()
		with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
			result = self.render(self.report(count=2))
		assert result is not None
		text = result["hookSpecificOutput"]["additionalContext"]
		self.assertIn("src/пример file.py:1 secrets.candidate", text)
		self.assertIn("src/пример file.py:2 secrets.candidate", text)
		self.assertEqual(text.count("a" * 64), 2)
		self.assertNotIn("synthetic-", json.dumps(result))
		self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")

	def test_clean_waived_and_inapplicable_input_are_silent(self) -> None:
		for status, count in (("passed", 0), ("waived", 0), ("waived", 2), ("not_applicable", 0)):
			with self.subTest(status=status, count=count):
				self.assertIsNone(self.render(self.report(status, count=count)))

	def test_invalid_and_inconsistent_reports_are_unchecked_and_redacted(self) -> None:
		base = json.loads(self.report())
		cases = ["synthetic-private-fragment", "[]", "null", self.report(count=0)]
		for change in (
			{"schema": True},
			{"check_id": "synthetic-private-fragment"},
			{"status": ["synthetic-private-fragment"]},
			{"findings": None},
			{"status": "passed"},
		):
			cases.append(json.dumps({**base, **change}))
		for report in cases:
			with self.subTest(report=report[:40]):
				result = self.render(report)
				assert result is not None
				text = json.dumps(result)
				self.assertIn("UNCHECKED", text)
				self.assertNotIn("synthetic-", text)

	def test_unrequested_or_invalid_locations_cannot_echo_scanner_text(self) -> None:
		base = json.loads(self.report())
		cases: tuple[dict[str, str | int], ...] = (
			{"path": "synthetic-private-fragment.py"},
			{"path": "../escape.py"},
			{"path": "src\\пример file.py"},
			{"line": True},
			{"line": 0},
			{"fingerprint": "synthetic-private-fragment"},
		)
		for status, change in product(("failed", "waived"), cases):
			with self.subTest(change=change, status=status):
				report = {
					**base,
					"status": status,
					"findings": [{**base["findings"][0], **change}],
				}
				result = self.render(json.dumps(report))
				assert result is not None
				self.assertIn("UNCHECKED", json.dumps(result))
				self.assertNotIn("synthetic-", json.dumps(result))

	def test_truncated_feedback_retains_only_source_free_evidence(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			result = self.render(self.report(count=200), evidence_dir=Path(directory))
			assert result is not None
			context = result["hookSpecificOutput"]["additionalContext"]
			self.assertLessEqual(len(context), 6000)
			self.assertIn("Full source-free evidence", context)
			files = list(Path(directory).glob("*.json"))
			self.assertEqual(len(files), 1)
			retained = files[0].read_text(encoding="utf-8")
			self.assertIn("file.py:200", retained)
			self.assertNotIn("synthetic-", retained)

	def test_over_limit_reports_are_explicitly_unchecked(self) -> None:
		for report in (self.report(count=201), self.report() + " " * (256 * 1024)):
			with self.subTest(size=len(report)):
				result = self.render(report)
				assert result is not None
				self.assertIn("UNCHECKED", json.dumps(result))

	def test_invalid_tail_discards_all_partial_findings(self) -> None:
		report = json.loads(self.report(count=2))
		report["findings"][1]["fingerprint"] = "synthetic-private-fragment"
		result = self.render(json.dumps(report))
		assert result is not None
		text = result["hookSpecificOutput"]["additionalContext"]
		self.assertIn("UNCHECKED", text)
		self.assertNotIn("file.py:1", text)
		self.assertNotIn("synthetic-", text)

	def test_encoding_and_nesting_errors_are_redacted(self) -> None:
		for report in ("\ud800", "[" * 1100 + "synthetic-private-fragment" + "]" * 1100):
			result = self.render(report)
			assert result is not None
			self.assertIn("UNCHECKED", json.dumps(result))
			self.assertNotIn("synthetic-", json.dumps(result))

	def test_evidence_write_failure_reports_incomplete_without_source(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			blocked = Path(directory) / "not-a-directory"
			blocked.write_text("", encoding="utf-8")
			result = self.render(self.report(count=200), evidence_dir=blocked)
			assert result is not None
			context = result["hookSpecificOutput"]["additionalContext"]
			self.assertIn("UNCHECKED", context)
			self.assertIn("could not be retained", context)
			self.assertNotIn("synthetic-", context)


if __name__ == "__main__":
	unittest.main()
