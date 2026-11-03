"""Integrity presentation acceptance before the touched-file provider is ready."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from file_integrity.feedback import integrity_feedback


class IntegrityProjection(unittest.TestCase):
	def report(self, status: str = "failed", count: int = 1, reason: str | None = None) -> str:
		data = {
			"schema": 1,
			"check_id": "repository.git.conflict_markers",
			"status": status,
			"findings": [
				{
					"path": "src/пример file.py",
					"line": index + 1,
					"message": "private-source-fragment",
					"action": "private-source-fragment",
				}
				for index in range(count)
			],
			"stderr": "private-source-fragment",
		}
		if reason is not None:
			data["reason"] = reason
		return json.dumps(data)

	def render(self, output: str, *, evidence_dir: Path | None = None) -> dict | None:
		return integrity_feedback(
			output, touched_paths={"src/пример file.py"}, evidence_dir=evidence_dir
		)

	def test_locations_and_fixed_action_are_model_visible_without_console_output(self) -> None:
		stdout, stderr = io.StringIO(), io.StringIO()
		with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
			result = self.render(self.report(count=2))
		assert result is not None
		context = result["hookSpecificOutput"]["additionalContext"]
		self.assertEqual(result["hookSpecificOutput"]["hookEventName"], "PostToolUse")
		self.assertIn("src/пример file.py:1 repository.git.conflict_markers", context)
		self.assertIn("src/пример file.py:2 repository.git.conflict_markers", context)
		self.assertIn("resolve the merge conflict", context)
		self.assertNotIn("private-source-fragment", json.dumps(result))
		self.assertNotIn("decision", result)
		self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")

	def test_clean_waived_and_inapplicable_reports_are_silent(self) -> None:
		for status in ("passed", "waived", "not_applicable"):
			with self.subTest(status=status):
				self.assertIsNone(self.render(self.report(status, count=0)))

	def test_multiple_touched_files_include_non_python_locations(self) -> None:
		data = json.loads(self.report())
		data["findings"].append({"path": "notes/merge.md", "line": 8})
		result = integrity_feedback(
			json.dumps(data), touched_paths={"src/пример file.py", "notes/merge.md"}
		)
		assert result is not None
		context = result["hookSpecificOutput"]["additionalContext"]
		self.assertIn("src/пример file.py:1", context)
		self.assertIn("notes/merge.md:8", context)

	def test_malformed_or_inconsistent_reports_are_incomplete(self) -> None:
		base = json.loads(self.report())
		cases = ["null", "[]", "private-source-fragment", self.report(count=0)]
		changes: list[dict[str, object]] = [
			{"schema": True},
			{"check_id": "other.check"},
			{"status": []},
			{"findings": None},
			{"status": "passed"},
			{"status": "waived"},
		]
		for change in changes:
			cases.append(json.dumps({**base, **change}))
		for output in cases:
			with self.subTest(output=output[:40]):
				result = self.render(output)
				assert result is not None
				self.assertIn("UNCHECKED", json.dumps(result))
				self.assertNotIn("private-source-fragment", json.dumps(result))

	def test_unrequested_paths_and_invalid_tail_discard_all_findings(self) -> None:
		base = json.loads(self.report(count=2))
		for change in (
			{"path": "unrelated.py"},
			{"path": "../escape.py"},
			{"path": "src\\пример file.py"},
			{"line": True},
			{"line": 0},
			{"line": 2**31},
		):
			with self.subTest(change=change):
				data = {
					**base,
					"findings": [base["findings"][0], {**base["findings"][1], **change}],
				}
				result = self.render(json.dumps(data))
				assert result is not None
				context = result["hookSpecificOutput"]["additionalContext"]
				self.assertIn("UNCHECKED", context)
				self.assertNotIn("file.py:1", context)
				self.assertNotIn("unrelated.py", context)

	def test_invalid_attribution_cannot_be_used_as_a_path_allowlist(self) -> None:
		for paths in (
			{"../escape.py"},
			{"src\\пример file.py"},
			{"."},
			{f"file-{index}.py" for index in range(11)},
		):
			with self.subTest(paths=paths):
				result = integrity_feedback(self.report("passed", count=0), touched_paths=paths)
				assert result is not None
				self.assertIn("UNCHECKED", json.dumps(result))

	def test_incomplete_prerequisites_preserve_a_fixed_recovery_message(self) -> None:
		result = self.render(self.report("unchecked", count=0, reason="manifest"))
		assert result is not None
		context = result["hookSpecificOutput"]["additionalContext"]
		self.assertIn("integrity UNCHECKED", context)
		self.assertIn("Correct the working-tree Gate manifest", context)
		self.assertNotIn("private-source-fragment", context)

	def test_unchecked_release_reasons_use_their_specific_recovery(self) -> None:
		for reason, action in (
			("release", "Run explicit Gate sync and edit-setup"),
			("stale_input", "Capture current content again"),
			("deadline", "Restore timely verification"),
		):
			with self.subTest(reason=reason):
				result = self.render(self.report("unchecked", count=0, reason=reason))
				assert result is not None
				self.assertIn(action, result["hookSpecificOutput"]["additionalContext"])

	def test_large_feedback_retains_only_sanitized_evidence(self) -> None:
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
			self.assertNotIn("private-source-fragment", retained)

	def test_limits_encoding_and_nesting_errors_remain_incomplete(self) -> None:
		for output in (
			self.report(count=201),
			self.report() + " " * (256 * 1024),
			"\ud800",
			"[" * 1100 + "]" * 1100,
		):
			with self.subTest(size=len(output)):
				result = self.render(output)
				assert result is not None
				self.assertIn("UNCHECKED", json.dumps(result))

	def test_evidence_write_failure_is_explicitly_incomplete(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			blocked = Path(directory) / "not-a-directory"
			blocked.write_text("", encoding="utf-8")
			result = self.render(self.report(count=200), evidence_dir=blocked)
			assert result is not None
			context = result["hookSpecificOutput"]["additionalContext"]
			self.assertIn("UNCHECKED", context)
			self.assertIn("could not be retained", context)


if __name__ == "__main__":
	unittest.main()
