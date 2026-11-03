"""Verify independent Codex registrations and selectable native Git hooks."""

from __future__ import annotations

import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from python_quality.install_post_edit_hook import group as post_edit_group  # noqa: E402

from _shared.hook_registration import install, uninstall  # noqa: E402


class CodexRegistrationTests(unittest.TestCase):
	def setUp(self) -> None:
		self.temporary = tempfile.TemporaryDirectory(prefix="hook-registration-")
		self.addCleanup(self.temporary.cleanup)
		self.root = Path(self.temporary.name)
		self.configuration: dict[str, Any] = {
			"description": "preserve this",
			"hooks": {
				"PreToolUse": [
					{"matcher": "*", "hooks": [{"type": "command", "command": "other"}]}
				],
				"Stop": [{"matcher": "*", "hooks": [{"type": "command", "command": "backup"}]}],
			},
		}
		(self.root / "hooks.json").write_text(json.dumps(self.configuration), encoding="utf-8")

	def test_post_edit_registration_uses_the_checked_out_source(self) -> None:
		group = post_edit_group(self.root)
		command = group["hooks"][0]["command"]
		self.assertEqual(
			shlex.split(command)[-1],
			str(Path(__file__).resolve().parents[2] / "python_quality" / "post_edit_checks.py"),
		)
		self.assertTrue(install(self.root, "PostToolUse", group))
		configuration = json.loads((self.root / "hooks.json").read_text(encoding="utf-8"))
		self.assertEqual(
			configuration["hooks"]["PreToolUse"], self.configuration["hooks"]["PreToolUse"]
		)
		self.assertEqual(configuration["hooks"]["Stop"], self.configuration["hooks"]["Stop"])
		self.assertTrue(uninstall(self.root, "PostToolUse", group))
		self.assertEqual(
			json.loads((self.root / "hooks.json").read_text(encoding="utf-8")),
			self.configuration,
		)


if __name__ == "__main__":
	unittest.main()
