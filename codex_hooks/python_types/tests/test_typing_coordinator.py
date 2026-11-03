"""Post-writer/deletion routing through the real typing adapter and launcher."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _shared.quality_coordinator import coordinate
from _shared.quality_settings import load_settings, settings_operation
from _shared.tests.provider_fixture import native_launcher

from python_types.tests.provider_fixture import typing_project_fixture
from python_types.tests.result_fixture import seal


@unittest.skipUnless(
	native_launcher().is_file(), "Machine-owned verified release launcher required"
)
class TypingCoordinator(unittest.TestCase):
	def event(self, root: Path, *, deletion: bool = False) -> dict:
		path, operation, letter = (
			("deleted.py", "Delete", "D") if deletion else ("example.py", "Update", "M")
		)
		return {
			"hook_event_name": "PostToolUse",
			"tool_name": "apply_patch",
			"cwd": str(root),
			"session_id": "typing-coordinator",
			"tool_use_id": "edit-1",
			"tool_input": {
				"command": (
					f"*** Begin Patch\n*** {operation} File: {path}\n@@\n-x\n+y\n*** End Patch"
				)
			},
			"tool_response": f"Success. Updated the following files:\n{letter} {path}\n",
		}

	def test_final_writer_bytes_and_unchanged_consumer_diagnostic_reach_model(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			environment, settings, request, _ = typing_project_fixture(base)
			settings_operation(
				settings,
				"update",
				{**load_settings(settings)["current"], "enabled": ["ruff", "types"]},
			)
			root = Path(request["root"])
			(root / "example.py").write_bytes(b"VALUE: int=1\n")
			report_path = Path(environment["TYPING_FIXTURE_RESULT"])
			report = json.loads(report_path.read_text(encoding="utf-8"))
			report["files"] = [item for item in report["files"] if item["path"] == "example.py"]
			report["status"] = report["components"][0]["status"] = "failed"
			report["components"][0]["diagnostics"] = [
				{"path": "consumer.py", "line": 1, "column": 1, "code": "assignment"}
			]
			report_path.write_text(json.dumps(seal(report)), encoding="utf-8")

			def write_final(*args: object, **kwargs: object) -> dict:
				self.assertEqual(kwargs.get("phase"), "write")
				(root / "example.py").write_bytes(b"VALUE: int = 1\n")
				return {"schema": 1, "status": "passed", "files": []}

			with (
				patch.dict(os.environ, environment, clear=True),
				patch("python_quality.check.check_files", side_effect=write_final),
				patch("_shared.quality_background.selected_checks", return_value=["types"]),
			):
				result = coordinate(
					self.event(root),
					settings_path=settings,
					state_dir=base / "state",
					evidence_dir=base / "evidence",
				)
				self.assertIsNone(result, "Typing diagnostics are delivered after the edit returns")
				result = coordinate(
					{
						"hook_event_name": "Stop",
						"session_id": "typing-coordinator",
						"cwd": str(root),
					},
					settings_path=settings,
					state_dir=base / "state",
				)
			assert result is not None
			self.assertEqual(result["decision"], "block")
			context = result["reason"]
			self.assertIn("consumer.py:1:1 mypy assignment", context)
			self.assertNotIn("UNCHECKED", context)
			self.assertEqual((root / "example.py").read_bytes(), b"VALUE: int = 1\n")

	def test_deletion_only_event_runs_typing_and_preserves_confirmed_absence(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			environment, settings, request, _ = typing_project_fixture(base)
			marker = base / "request.json"
			report_path = Path(environment["TYPING_FIXTURE_RESULT"])
			report = json.loads(report_path.read_text(encoding="utf-8"))
			report["files"] = [item for item in report["files"] if item["path"] == "deleted.py"]
			report_path.write_text(json.dumps(seal(report)), encoding="utf-8")
			with patch.dict(
				os.environ, {**environment, "TYPING_FIXTURE_MARKER": str(marker)}, clear=True
			):
				result = coordinate(
					self.event(Path(request["root"]), deletion=True),
					settings_path=settings,
					state_dir=base / "state",
				)
				self.assertIsNone(
					coordinate(
						{
							"hook_event_name": "Stop",
							"session_id": "typing-coordinator",
							"cwd": request["root"],
						},
						settings_path=settings,
						state_dir=base / "state",
					)
				)
			self.assertIsNone(result)
			sent = json.loads(marker.read_text(encoding="utf-8"))
			self.assertEqual(sent["files"], [{"path": "deleted.py", "sha256": None}])
			self.assertFalse((Path(request["root"]) / "deleted.py").exists())


if __name__ == "__main__":
	unittest.main()
