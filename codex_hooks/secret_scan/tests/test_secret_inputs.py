"""Exact post-writer input identities before any scanner is launched."""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from time import monotonic

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from secret_scan.inputs import SecretInputError, snapshot_files


class SecretInputs(unittest.TestCase):
	def fixture(
		self, root: Path, name: str = "пример file.py", data: bytes = b"VALUE = 1\n"
	) -> dict:
		(root / ".git").mkdir(exist_ok=True)
		(root / name).write_bytes(data)
		return {"path": name, "sha256": hashlib.sha256(data).hexdigest()}

	def test_complete_current_files_are_deduplicated_without_decoding(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			item = self.fixture(root, data=b"\xff\x00\x80")
			self.assertEqual(snapshot_files(root, [item, item], monotonic() + 10), [item])

	def test_mutation_after_writer_snapshot_is_explicitly_stale(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			item = self.fixture(root)
			(root / item["path"]).write_bytes(b"VALUE = 2\n")
			with self.assertRaisesRegex(SecretInputError, "^stale_input$"):
				snapshot_files(root, [item], monotonic() + 10)

	def test_unrequested_path_spellings_and_invalid_identities_are_rejected(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			item = self.fixture(root)
			for value in (
				{"path": "../escape.py", "sha256": item["sha256"]},
				{"path": str(root / item["path"]), "sha256": item["sha256"]},
				{"path": "./" + item["path"], "sha256": item["sha256"]},
				{"path": item["path"], "sha256": "synthetic-private-fragment"},
				{"path": item["path"], "sha256": None},
			):
				with self.subTest(value=value):
					with self.assertRaises(SecretInputError) as caught:
						snapshot_files(root, [value], monotonic() + 10)
					self.assertNotIn("synthetic-", str(caught.exception))

	def test_conflicting_duplicate_identity_is_invalid(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			item = self.fixture(root)
			with self.assertRaisesRegex(SecretInputError, "^invalid_request$"):
				snapshot_files(root, [item, {**item, "sha256": "a" * 64}], monotonic() + 10)

	def test_missing_or_non_regular_target_is_unchecked(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			item = self.fixture(root)
			(root / item["path"]).unlink()
			with self.assertRaisesRegex(SecretInputError, "^io$"):
				snapshot_files(root, [item], monotonic() + 10)
			(root / item["path"]).mkdir()
			with self.assertRaisesRegex(SecretInputError, "^io$"):
				snapshot_files(root, [item], monotonic() + 10)

	def test_file_batch_and_time_limits_never_return_partial_success(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			large = self.fixture(root, data=b"x" * (128 * 1024 + 1))
			with self.assertRaisesRegex(SecretInputError, "^limits$"):
				snapshot_files(root, [large], monotonic() + 10)
			items = [
				self.fixture(root, f"file{index}.py", b"x" * (128 * 1024)) for index in range(9)
			]
			with self.assertRaisesRegex(SecretInputError, "^limits$"):
				snapshot_files(root, items, monotonic() + 10)
			with self.assertRaisesRegex(SecretInputError, "^deadline$"):
				snapshot_files(root, items[:1], monotonic() - 1)


if __name__ == "__main__":
	unittest.main()
