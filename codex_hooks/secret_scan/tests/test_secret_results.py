"""Host acceptance of the public source-free edit-secrets response."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from time import monotonic

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from secret_scan.results import validate_result


class SecretResults(unittest.TestCase):
	def fixture(self, root: Path) -> tuple[dict, dict]:
		root = root.resolve()
		(root / ".git").mkdir()
		content = b"VALUE = 1\n"
		(root / "a.py").write_bytes(content)
		digest = hashlib.sha256(content).hexdigest()
		request = {
			"schema": 1,
			"root": str(root),
			"files": [{"path": "a.py", "sha256": digest}],
			"baseline": {"policy_release": "v2.4.2"},
		}
		result = {
			"schema": 1,
			"engine_version": "9.0.0",
			"root": str(root),
			"check_id": "secrets.candidate",
			"status": "failed",
			"reason": None,
			"files": [
				{
					"path": "a.py",
					"status": "failed",
					"coverage": "baseline",
					"input_sha256": digest,
					"sha256": digest,
					"changed": False,
					"selection": 0,
					"findings": [
						{
							"check_id": "secrets.candidate",
							"path": "a.py",
							"target": "a.py:3",
							"line": 3,
							"fingerprint": "b" * 64,
							"status": "failed",
							"Secret": "synthetic-private",
							"action": "synthetic-private",
						}
					],
				}
			],
			"selections": [
				{
					"policy_release": "v2.4.2",
					"release_sha256": "c" * 64,
					"policy_source": "gitleaks-built-in",
					"policy_sha256": "d" * 64,
					"scanner_version": "8.30.1",
					"scanner_sha256": "d" * 64,
					"waiver_sha256": "e" * 64,
					"message": "synthetic-private",
				}
			],
			"inputs": [
				{"path": path, "sha256": None}
				for path in (
					".gitleaks.toml",
					".gitleaksignore",
					"quality-gate.toml",
				)
			],
			"process_count": 3,
			"elapsed_ms": 25,
			"writes_may_have_occurred": False,
		}
		return request, result

	def validate(self, request: dict, response: dict, code: int = 1) -> dict:
		return validate_result(json.dumps(response), code, request, "v9.0.0", monotonic() + 10)

	def test_exact_failed_and_waived_findings_keep_only_safe_identity_fields(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			request, response = self.fixture(Path(directory))
			response["files"][0]["findings"].append(
				{
					**response["files"][0]["findings"][0],
					"line": 4,
					"target": "a.py:4",
					"status": "waived",
				}
			)
			result = self.validate(request, response)
			self.assertEqual(result["status"], "failed")
			self.assertEqual(result["files"][0]["findings"][1]["status"], "waived")
			self.assertEqual(result["files"][0]["findings"][0]["fingerprint"], "b" * 64)
			self.assertNotIn("synthetic-private", json.dumps(result))

	def test_clean_and_fully_waived_outcomes_keep_distinct_status(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			request, response = self.fixture(Path(directory))
			response["status"] = response["files"][0]["status"] = "waived"
			response["files"][0]["findings"][0]["status"] = "waived"
			self.assertEqual(self.validate(request, response, 0)["status"], "waived")
			response["status"] = response["files"][0]["status"] = "passed"
			response["files"][0]["findings"] = []
			self.assertEqual(self.validate(request, response, 0)["status"], "passed")

	def test_all_empty_files_remain_not_applicable(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			request, response = self.fixture(root)
			digest = hashlib.sha256(b"").hexdigest()
			(root / "a.py").write_bytes(b"")
			request["files"][0]["sha256"] = digest
			response["files"][0].update(
				status="not_applicable",
				input_sha256=digest,
				sha256=digest,
				changed=False,
				findings=[],
			)
			response["status"] = "not_applicable"
			result = self.validate(request, response, 0)
		self.assertEqual(result["status"], "not_applicable")
		self.assertEqual(result["files"][0]["status"], "not_applicable")

	def test_missing_coverage_inconsistent_status_or_identity_is_unchecked(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			request, response = self.fixture(Path(directory))
			changes: tuple[dict, ...] = (
				{"files": []},
				{"inputs": []},
				{"selections": []},
				{"status": "passed"},
				{"writes_may_have_occurred": True},
				{"engine_version": "8.0.0"},
				{"root": str(Path(directory).parent)},
				{"check_id": "synthetic-private"},
			)
			for change in changes:
				with self.subTest(change=change):
					result = self.validate(request, {**response, **change})
					self.assertEqual(result["status"], "unchecked")
					self.assertNotIn("synthetic-private", json.dumps(result))

	def test_unrequested_findings_or_forged_waiver_targets_are_unchecked(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			request, response = self.fixture(Path(directory))
			original = response["files"][0]["findings"][0]
			for change in (
				{"path": "other.py"},
				{"target": "other.py:3"},
				{"line": True},
				{"fingerprint": "synthetic-private"},
				{"status": "passed"},
			):
				response["files"][0]["findings"] = [{**original, **change}]
				self.assertEqual(self.validate(request, response)["status"], "unchecked")

	def test_changed_content_or_configuration_invalidates_completed_scan(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			request, response = self.fixture(root)
			(root / "a.py").write_bytes(b"VALUE = 2\n")
			self.assertEqual(self.validate(request, response)["reason"], "stale_result")
			(root / "a.py").write_bytes(b"VALUE = 1\n")
			(root / ".gitleaksignore").write_text("new configuration", encoding="utf-8")
			self.assertEqual(self.validate(request, response)["reason"], "stale_result")

	def test_short_unchecked_and_malformed_reports_never_echo_provider_details(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			request, _ = self.fixture(Path(directory))
			result = self.validate(
				request,
				{
					"schema": 1,
					"check_id": "secrets.candidate",
					"status": "unchecked",
					"reason": "tool",
					"action": "synthetic-private",
				},
				2,
			)
			self.assertEqual(result["reason"], "tool")
			self.assertNotIn("synthetic-private", json.dumps(result))
			for output in ("synthetic-private", "[]", '{"schema": 1, "schema": 1}', "[" * 1100):
				result = validate_result(output, 0, request, "v9.0.0", monotonic() + 10)
				self.assertEqual(result["status"], "unchecked")
				self.assertNotIn("synthetic-private", json.dumps(result))


if __name__ == "__main__":
	unittest.main()
