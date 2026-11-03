"""Selected typing requires an explicitly prepared real probe before activation."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _shared.quality_install import BUNDLE, registration_groups
from _shared.quality_install_probe import probe
from _shared.quality_settings import load_settings

from python_types.tests.provider_fixture import typing_project_fixture
from python_types.tests.result_fixture import seal


class TypingInstallation(unittest.TestCase):
	def test_managed_completion_registration_drains_background_jobs(self) -> None:
		groups = registration_groups(Path("host").absolute())
		self.assertIn("Stop", groups)
		self.assertGreaterEqual(groups["Stop"]["hooks"][0]["timeout"], 45)
		for name in (
			"quality_background.py",
			"quality_background_state.py",
			"quality_background_worker.py",
			"quality_install_probe.py",
		):
			self.assertIn(f"_shared/{name}", BUNDLE)
		self.assertIn("_shared/native_quality_gate.py", BUNDLE)

	def selection(self) -> dict:
		return {
			"provider_release": "v9.0.0",
			"baseline_release": "v2.4.2",
			"enabled": ["types"],
			"budget_seconds": 10.0,
		}

	def test_selected_typing_without_prepared_probe_is_incomplete(self) -> None:
		self.assertEqual(probe(self.selection()), "typing_probe_required")

	def test_complete_typing_runtime_belongs_to_the_managed_bundle(self) -> None:
		for name in (
			"__init__.py",
			"check.py",
			"coverage.py",
			"feedback.py",
			"freshness.py",
			"inputs.py",
			"provider_bridge.py",
			"results.py",
			"selections.py",
		):
			with self.subTest(name=name):
				self.assertIn(f"python_types/{name}", BUNDLE)

	def test_prepared_root_must_really_pass_typing_without_provisioning(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			environment, settings_path, request, _ = typing_project_fixture(base)
			settings = load_settings(settings_path)["current"]
			root = Path(request["root"])
			(root / "example.py").rename(root / "probe.py")
			report_path = Path(environment["TYPING_FIXTURE_RESULT"])
			report = json.loads(report_path.read_text(encoding="utf-8"))
			report["files"] = [
				{**item, "path": "probe.py"}
				for item in report["files"]
				if item["path"] == "example.py"
			]
			component = report["components"][0]
			component["inventory"][1]["path"] = "probe.py"
			component["directories"][0]["entries"][1]["name"] = "probe.py"
			from python_types.tests.result_fixture import digest

			component["scope_sha256"] = digest(
				{key: component[key] for key in ("inventory", "directories")}
			)
			report_path.write_text(json.dumps(seal(report)), encoding="utf-8")
			marker = base / "request.json"
			with patch.dict(
				os.environ, {**environment, "TYPING_FIXTURE_MARKER": str(marker)}, clear=True
			):
				self.assertIsNone(probe(settings, typing_probe_root=root))
				self.assertEqual(json.loads(marker.read_text(encoding="utf-8"))["schema"], 2)
				report.update(status="unchecked", reason="runtime")
				report_path.write_text(json.dumps(report), encoding="utf-8")
				self.assertEqual(probe(settings, typing_probe_root=root), "typing_probe_unchecked")
			self.assertEqual((root / "probe.py").read_bytes(), b"VALUE: int = 1\n")
			self.assertTrue((root / ".venv").is_dir())


if __name__ == "__main__":
	unittest.main()
