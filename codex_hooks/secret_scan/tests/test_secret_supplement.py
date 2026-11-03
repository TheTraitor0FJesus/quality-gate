"""The migrated supplement preserves existing rules without a second scanner."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _shared.tests.provider_fixture import native_launcher

ENTRY = Path(__file__).resolve().parents[2] / "_shared" / "quality_legacy_security.py"


@unittest.skipUnless(
	(native_launcher().parent / "codex_hook_adapter.py").is_file(),
	"Existing machine-owned detector definitions are not installed",
)
class PreservedSecretRules(unittest.TestCase):
	def invoke(self, content: bytes, *, stale: bool = False) -> tuple[dict, str]:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory).resolve()
			(root / "example.py").write_bytes(content)
			request = {
				"schema": 1,
				"mode": "builtin",
				"root": str(root),
				"files": [{"path": "example.py", "sha256": hashlib.sha256(content).hexdigest()}],
			}
			if stale:
				(root / "example.py").write_bytes(b"CHANGED = True\n")
			completed = subprocess.run(
				[sys.executable, "-B", str(ENTRY)],
				input=json.dumps(request),
				capture_output=True,
				text=True,
				encoding="utf-8",
				timeout=10,
			)
			self.assertEqual(completed.stderr, "")
			return json.loads(completed.stdout), completed.stdout

	def test_all_five_existing_families_keep_source_free_locations(self) -> None:
		synthetic = (
			"AKIA" + "A" * 16,
			"AWS_SECRET_ACCESS_KEY = '" + "aB3dE5fG" + "'",
			"ghp_" + "A" * 20,
			"sk-" + "a" * 20,
			"-----BEGIN " + "RSA PRIVATE KEY-----",
		)
		for value in synthetic:
			with self.subTest(family=synthetic.index(value)):
				result, output = self.invoke(("# fixture\nVALUE = " + repr(value) + "\n").encode())
				self.assertEqual(result["check"], "builtin_secrets")
				self.assertEqual(result["status"], "failed")
				self.assertEqual(
					result["files"],
					[{"path": "example.py", "line": 2, "check_id": "secrets.builtin"}],
				)
				self.assertNotIn(value, output)

	def test_clean_and_stale_are_distinct(self) -> None:
		self.assertEqual(self.invoke(b"VALUE = 1\n")[0]["status"], "passed")
		self.assertEqual(self.invoke(b"VALUE = 1\n", stale=True)[0]["status"], "unchecked")


if __name__ == "__main__":
	unittest.main()
