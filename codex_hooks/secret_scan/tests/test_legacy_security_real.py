"""Opt-in acceptance of the prepared native heavy-security fallback."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from time import monotonic

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _shared.quality_process import execute


@unittest.skipUnless(
	os.environ.get("LEGACY_SECURITY_ACCEPTANCE_PYTHON"),
	"Explicit prepared heavy-security Python required",
)
class RealLegacySecurity(unittest.TestCase):
	def test_real_clean_and_synthetic_results_and_timing(self) -> None:
		python = Path(os.environ["LEGACY_SECURITY_ACCEPTANCE_PYTHON"]).resolve(strict=True)
		entry = Path(__file__).resolve().parents[2] / "_shared/quality_legacy_security.py"
		budget = float(os.environ.get("LEGACY_SECURITY_ACCEPTANCE_BUDGET_SECONDS", "120"))
		observations = []
		for failing in (False, True):
			with self.subTest(failing=failing), tempfile.TemporaryDirectory() as directory:
				root = Path(directory).resolve()
				content = (
					("TOKEN = '" + "sk-" + "a" * 20 + "'\n").encode()
					if failing
					else b"VALUE: int = 1\n"
				)
				(root / "example.py").write_bytes(content)
				request = {
					"schema": 1,
					"mode": "legacy",
					"root": str(root),
					"files": [
						{"path": "example.py", "sha256": hashlib.sha256(content).hexdigest()}
					],
				}
				# The supervisor owns the whole Windows process tree on timeout.
				result = execute(
					[str(python), "-B", str(entry)],
					deadline=monotonic() + budget,
					input_text=json.dumps(request),
				)
				self.assertEqual(result.returncode, 0)
				self.assertEqual(result.stderr, "")
				report = json.loads(result.stdout)
				observations.append(
					{"failing": failing, "elapsed_ms": result.elapsed_ms, "report": report}
				)
				if output := os.environ.get("LEGACY_SECURITY_ACCEPTANCE_OBSERVATIONS"):
					Path(output).write_text(json.dumps(observations, indent=2), encoding="utf-8")
				self.assertEqual(report["status"], "failed" if failing else "passed", report)
				self.assertEqual(report["files"], [{"path": "example.py"}] if failing else [])
				self.assertNotIn("sk-" + "a" * 20, result.stdout)
				self.assertEqual((root / "example.py").read_bytes(), content)
