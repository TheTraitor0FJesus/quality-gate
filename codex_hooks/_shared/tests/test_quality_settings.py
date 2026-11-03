"""Executable tests for portable hook settings and reversible updates."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ENTRY = Path(__file__).resolve().parents[1] / "quality_settings.py"
sys.path.insert(0, str(ENTRY.parent))

from _shared.quality_settings import install_lock_wait_seconds  # noqa: E402


class PortableSettings(unittest.TestCase):
	def invoke(self, path: Path, action: str, settings: object = None) -> tuple[int, dict]:
		result = subprocess.run(
			[sys.executable, "-B", str(ENTRY), "--file", str(path), action],
			input="" if settings is None else json.dumps(settings),
			capture_output=True,
			text=True,
			encoding="utf-8",
			timeout=5,
		)
		self.assertTrue(
			result.stdout.strip(), f"Settings entry point returned no JSON: {result.stderr}"
		)
		return result.returncode, json.loads(result.stdout)

	def test_initialization_is_disabled_and_idempotent(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "settings.json"
			code, first = self.invoke(path, "init")
			self.assertEqual(code, 0)
			self.assertEqual(
				first["current"],
				{
					"baseline_release": None,
					"provider_release": None,
					"enabled": [],
					"install_lock_wait_seconds": 10.0,
				},
			)
			self.assertIsNone(first["previous"])
			before = path.read_bytes()
			code, second = self.invoke(path, "init")
			self.assertEqual(code, 0)
			self.assertEqual(first, second)
			self.assertEqual(path.read_bytes(), before)

	def test_update_and_rollback_preserve_the_previous_selection(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "settings.json"
			self.invoke(path, "init")
			selected = {"baseline_release": "v2.4.2", "provider_release": "v9.0.0", "enabled": []}
			code, updated = self.invoke(path, "update", selected)
			self.assertEqual(code, 0)
			self.assertEqual(updated["current"], selected)
			self.assertEqual(updated["previous"]["enabled"], [])
			code, rolled_back = self.invoke(path, "rollback")
			self.assertEqual(code, 0)
			self.assertEqual(rolled_back["current"]["baseline_release"], None)
			self.assertEqual(rolled_back["previous"], selected)

	def test_unmanaged_destination_is_preserved(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "settings.json"
			original = b'{"unrelated": "keep"}'
			path.write_bytes(original)
			code, result = self.invoke(path, "init")
			self.assertEqual(code, 2)
			self.assertEqual(result["reason"], "unmanaged_settings")
			self.assertEqual(path.read_bytes(), original)

	def test_invalid_update_preserves_current_and_previous_settings(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "settings.json"
			self.invoke(path, "init")
			original = path.read_bytes()
			for settings in (
				{"baseline_release": "latest", "provider_release": "v1.0.0", "enabled": []},
				{"baseline_release": None, "provider_release": None, "enabled": ["ruff"]},
				{
					"baseline_release": "v1.0.0",
					"provider_release": "v1.0.0",
					"enabled": ["unknown"],
				},
			):
				with self.subTest(settings=settings):
					code, result = self.invoke(path, "update", settings)
					self.assertEqual(code, 2)
					self.assertEqual(result["status"], "unchecked")
					self.assertEqual(path.read_bytes(), original)

	def test_rollback_without_previous_selection_is_unchecked(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "settings.json"
			self.invoke(path, "init")
			original = path.read_bytes()
			code, result = self.invoke(path, "rollback")
			self.assertEqual(code, 2)
			self.assertEqual(result["reason"], "previous_settings_missing")
			self.assertEqual(path.read_bytes(), original)

	def test_explicit_baseline_runtime_and_budget_survive_roundtrip(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "settings.json"
			self.invoke(path, "init")
			selection = {
				"baseline_release": "v2.4.2",
				"provider_release": "v9.0.0",
				"enabled": [],
				"baseline_python": sys.executable,
				"baseline_python_version": "3.12",
				"budget_seconds": 2.0,
				"install_lock_wait_seconds": 4.5,
			}
			code, result = self.invoke(path, "update", selection)
			self.assertEqual(code, 0)
			self.assertEqual(result["current"], selection)

	def test_install_lock_wait_uses_configured_value_and_compatible_default(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			self.assertEqual(install_lock_wait_seconds(root), 10.0)
			path = root / "hooks" / "_shared" / "python-quality.json"
			self.invoke(path, "init")
			self.assertEqual(install_lock_wait_seconds(root), 10.0)
			self.invoke(
				path,
				"update",
				{
					"baseline_release": None,
					"provider_release": None,
					"enabled": [],
					"install_lock_wait_seconds": 2.5,
				},
			)
			self.assertEqual(install_lock_wait_seconds(root), 2.5)

	def test_invalid_install_lock_wait_cannot_be_stored(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "settings.json"
			self.invoke(path, "init")
			before = path.read_bytes()
			for value in (0, 120.1, True, "1"):
				with self.subTest(value=value):
					code, result = self.invoke(
						path,
						"update",
						{
							"baseline_release": None,
							"provider_release": None,
							"enabled": [],
							"install_lock_wait_seconds": value,
						},
					)
					self.assertEqual(code, 2)
					self.assertEqual(result["reason"], "invalid_install_lock_wait")
					self.assertEqual(path.read_bytes(), before)

	def test_invalid_deadline_cannot_be_stored(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "settings.json"
			self.invoke(path, "init")
			before = path.read_bytes()
			code, result = self.invoke(
				path,
				"update",
				{
					"baseline_release": None,
					"provider_release": None,
					"enabled": [],
					"budget_seconds": 60,
				},
			)
			self.assertEqual(code, 2)
			self.assertEqual(result["reason"], "invalid_edit_budget")
			self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
	unittest.main()
