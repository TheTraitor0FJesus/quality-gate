"""Read-only raw-byte and deletion attribution at the typing request seam."""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from time import monotonic

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class TypingInputs(unittest.TestCase):
	def fixture(self, directory: str) -> tuple[Path, list[dict]]:
		root = Path(directory).resolve()
		(root / ".git").mkdir()
		content = b"VALUE: int = 1\r\n"
		(root / "example.py").write_bytes(content)
		return root, [
			{"path": "example.py", "sha256": hashlib.sha256(content).hexdigest()},
			{"path": "deleted.py", "sha256": None},
		]

	def test_current_bytes_and_confirmed_deletion_are_read_only_and_deduplicated(self) -> None:
		from python_types.inputs import snapshot_inputs

		with tempfile.TemporaryDirectory() as directory:
			root, files = self.fixture(directory)
			before = (root / "example.py").read_bytes()
			self.assertEqual(snapshot_inputs(root, [*files, files[0]], monotonic() + 10), files)
			self.assertEqual((root / "example.py").read_bytes(), before)
			self.assertFalse((root / "deleted.py").exists())

	def test_changed_bytes_or_recreated_deletion_are_stale(self) -> None:
		from python_types.inputs import TypingInputError, snapshot_inputs

		with tempfile.TemporaryDirectory() as directory:
			root, files = self.fixture(directory)
			(root / "example.py").write_bytes(b"VALUE: int = 2\n")
			with self.assertRaisesRegex(TypingInputError, "^stale_input$"):
				snapshot_inputs(root, files, monotonic() + 10)
			(root / "deleted.py").write_bytes(b"recreated")
			with self.assertRaisesRegex(TypingInputError, "^stale_input$"):
				snapshot_inputs(root, files[1:], monotonic() + 10)

	def test_configuration_bytes_are_eligible_but_directories_and_escapes_are_incomplete(
		self,
	) -> None:
		from python_types.inputs import TypingInputError, snapshot_inputs

		with tempfile.TemporaryDirectory() as directory:
			root, _ = self.fixture(directory)
			config = b"[quality]\npolicy_release = 'v2.4.2'\n"
			(root / "quality-gate.toml").write_bytes(config)
			request = [{"path": "quality-gate.toml", "sha256": hashlib.sha256(config).hexdigest()}]
			self.assertEqual(snapshot_inputs(root, request, monotonic() + 10), request)
			for path in ("../escape.py", ".git", "example.py\nforged", "src//a.py"):
				with self.subTest(path=path), self.assertRaises(TypingInputError):
					snapshot_inputs(root, [{"path": path, "sha256": "a" * 64}], monotonic() + 10)

	def test_conflicting_identity_extra_fields_and_expired_deadline_are_rejected(self) -> None:
		from python_types.inputs import TypingInputError, snapshot_inputs

		with tempfile.TemporaryDirectory() as directory:
			root, files = self.fixture(directory)
			for invalid in (
				[files[0], {"path": "example.py", "sha256": None}],
				[{**files[0], "unexpected": "private-source-fragment"}],
				[{"path": "example.py", "sha256": True}],
			):
				with self.subTest(invalid=invalid), self.assertRaises(TypingInputError):
					snapshot_inputs(root, invalid, monotonic() + 10)
			with self.assertRaisesRegex(TypingInputError, "^deadline$"):
				snapshot_inputs(root, files, monotonic() - 1)


if __name__ == "__main__":
	unittest.main()
