"""The coordinator hands actual final bytes to the independently enabled reader.

Ruff's documented writer seam is controlled here; the secret entry/launcher and
filesystem collaborators are real. Ruff detector correctness has its own suite.
"""

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
from _shared.tests.provider_fixture import native_launcher

from .provider_fixture import background_secret_fixture, secret_project_fixture


@unittest.skipUnless(
	native_launcher().is_file(), "Machine-owned native release launcher is not installed"
)
class SecretCoordinator(unittest.TestCase):
	def event(self, root: Path) -> dict:
		return {
			"hook_event_name": "PostToolUse",
			"tool_name": "apply_patch",
			"cwd": str(root),
			"session_id": "secret-coordinator",
			"tool_use_id": "edit-1",
			"tool_input": {
				"command": "*** Begin Patch\n*** Update File: a.py\n@@\n-x\n+y\n*** End Patch"
			},
			"tool_response": "Success. Updated the following files:\nM a.py\n",
		}

	def test_secret_reader_observes_actual_writer_bytes_and_retains_exact_finding(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			environment, settings, request, _ = background_secret_fixture(base)
			root = Path(request["root"])
			(root / "a.py").write_bytes(b"VALUE=1\n")

			def write_final(*args: object, **_kwargs: object) -> dict:
				(root / "a.py").write_bytes(b"VALUE = 1\n")
				return {"schema": 1, "status": "passed", "files": []}

			with (
				patch.dict(
					os.environ, {**environment, "SECRET_FIXTURE_MODE": "failed"}, clear=True
				),
				patch(
					"python_quality.check.check_files",
					side_effect=write_final,
				),
			):
				result = coordinate(
					self.event(root),
					settings_path=settings,
					state_dir=base / "state",
					evidence_dir=base / "evidence",
				)
				completion = coordinate(
					{**self.event(root), "hook_event_name": "Stop"},
					settings_path=settings,
					state_dir=base / "state",
					evidence_dir=base / "evidence",
				)
			final = completion or result
			assert final is not None
			context = json.dumps([result, completion])
			self.assertIn("a.py:1 secrets.candidate", context)
			self.assertNotIn("UNCHECKED", context)
			self.assertNotIn("synthetic-private", context)
			self.assertEqual((root / "a.py").read_bytes(), b"VALUE = 1\n")

	def test_disabled_secret_reader_never_launches_provider(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			environment, settings, request, _ = secret_project_fixture(base, ("ruff",))
			marker = base / "secret-called"
			with (
				patch.dict(
					os.environ, {**environment, "SECRET_FIXTURE_MARKER": str(marker)}, clear=True
				),
				patch(
					"python_quality.check.check_files",
					return_value={"schema": 1, "status": "passed", "files": []},
				),
			):
				result = coordinate(
					self.event(Path(request["root"])),
					settings_path=settings,
					state_dir=base / "state",
				)
			self.assertIsNone(result)
			self.assertFalse(marker.exists())

	def test_preserved_builtin_finding_is_separate_from_gate_waiver(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			environment, settings, request, _ = secret_project_fixture(base, ("secrets",))
			root = Path(request["root"])
			synthetic = "sk-" + "a" * 20
			(root / "a.py").write_text(f'TOKEN = "{synthetic}"\n', encoding="utf-8")
			with patch.dict(
				os.environ, {**environment, "SECRET_FIXTURE_MODE": "waived"}, clear=True
			):
				result = coordinate(
					self.event(root), settings_path=settings, state_dir=base / "state"
				)
				completion = coordinate(
					{**self.event(root), "hook_event_name": "Stop"},
					settings_path=settings,
					state_dir=base / "state",
				)
			final = completion or result
			assert final is not None
			self.assertEqual(final["decision"], "block")
			context = json.dumps([result, completion])
			self.assertIn("a.py:1 secrets.builtin", context)
			self.assertNotIn("legacy security finding", context)
			self.assertNotIn(synthetic, context)


if __name__ == "__main__":
	unittest.main()
