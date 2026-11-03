"""Prepared integrity model-feedback contract, independent of provider activation."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _shared.quality_feedback import feedback


class IntegrityFeedback(unittest.TestCase):
	def test_integrity_conflicts_have_gate_identity_and_location_without_source(self) -> None:
		result = feedback(
			"PostToolUse",
			[
				{
					"check": "integrity",
					"status": "failed",
					"findings": [
						{"path": "nested/merge.py", "line": 7, "message": "private-fragment"},
						{"path": "notes.md", "line": 3, "action": "private-fragment"},
					],
				}
			],
		)
		assert result is not None
		context = result["hookSpecificOutput"]["additionalContext"]
		self.assertIn("nested/merge.py:7", context)
		self.assertIn("notes.md:3", context)
		self.assertIn("repository.git.conflict_markers", context)
		self.assertIn("resolve", context.lower())
		self.assertNotIn("UNCHECKED", context)
		self.assertNotIn("private-fragment", json.dumps(result))
		self.assertNotIn("decision", result)

	def test_integrity_rejects_unsafe_locations_without_hiding_the_failure(self) -> None:
		result = feedback(
			"PostToolUse",
			[
				{
					"check": "integrity",
					"status": "failed",
					"findings": [
						{"path": "../private-fragment", "line": 1},
						{"path": "notes.md\nprivate-fragment", "line": 1},
						{"path": "notes.md", "line": True},
						{"path": "notes.md", "line": 0},
					],
				}
			],
		)
		assert result is not None
		context = result["hookSpecificOutput"]["additionalContext"]
		self.assertIn("Integrity FAILED", context)
		self.assertIn("resolve", context.lower())
		self.assertNotIn("private-fragment", context)

	def test_integrity_waiver_and_clean_results_are_silent(self) -> None:
		for status in ("passed", "not_applicable", "waived"):
			with self.subTest(status=status):
				self.assertIsNone(
					feedback("PostToolUse", [{"check": "integrity", "status": status}])
				)


if __name__ == "__main__":
	unittest.main()
