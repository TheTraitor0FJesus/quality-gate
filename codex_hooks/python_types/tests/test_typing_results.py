"""Public wire validation; controlled reports prove transport, not mypy parity."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from time import monotonic

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from python_types.tests.result_fixture import certificate, seal


class TypingResults(unittest.TestCase):
	def test_forged_or_incomplete_scope_and_configuration_are_rejected(self) -> None:
		from python_types.results import validate_result

		for mutation in (
			"inventory",
			"duplicate_directory",
			"configuration",
			"diagnostic",
			"environment_owner",
		):
			with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
				request, report = certificate(Path(directory).resolve() / "project")
				component = report["components"][0]
				if mutation == "inventory":
					component["inventory"].pop()
				elif mutation == "duplicate_directory":
					component["directories"].append(dict(component["directories"][0]))
				elif mutation == "configuration":
					report["configuration"].pop()
				elif mutation == "diagnostic":
					report["status"] = component["status"] = "failed"
					component["diagnostics"] = [
						{"path": "outside.py", "line": 1, "column": 1, "code": "assignment"}
					]
				else:
					report["selections"][0]["environment"]["path"] = str(
						Path(directory).resolve().parent
					)
				result = validate_result(
					json.dumps(seal(report)),
					1 if mutation == "diagnostic" else 0,
					request,
					"v9.0.0",
					monotonic() + 10,
				)
				self.assertEqual(result["status"], "unchecked")
				self.assertEqual(result["reason"], "invalid_provider_result")

	def test_expired_result_is_incomplete_and_supported_bytecode_caches_do_not_invalidate(
		self,
	) -> None:
		from python_types.results import validate_result

		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory).resolve() / "project"
			request, report = certificate(root)
			result = validate_result(json.dumps(report), 0, request, "v9.0.0", monotonic() - 1)
			self.assertEqual(result["reason"], "deadline")
			(root / ".venv/__pycache__").mkdir()
			(root / ".venv/__pycache__/ignored.pyc").write_bytes(b"cache")
			result = validate_result(json.dumps(report), 0, request, "v9.0.0", monotonic() + 10)
			self.assertEqual(result["status"], "passed")

	def test_fresh_complete_inventory_accepts_unchanged_dependent_diagnostics(self) -> None:
		from python_types.results import validate_result

		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory).resolve() / "project"
			request, report = certificate(root)
			report["status"] = report["components"][0]["status"] = "failed"
			report["components"][0]["diagnostics"] = [
				{"path": "consumer.py", "line": 1, "column": 1, "code": "assignment"}
			]
			result = validate_result(
				json.dumps(seal(report)), 1, request, "v9.0.0", monotonic() + 10
			)
			self.assertEqual(result["status"], "failed")
			self.assertEqual(
				result["findings"],
				[{"path": "consumer.py", "line": 1, "column": 1, "rule": "assignment"}],
			)

	def test_added_modified_deleted_and_recreated_files_invalidate_the_complete_result(
		self,
	) -> None:
		from python_types.results import validate_result

		for mutation in (
			"added",
			"modified",
			"deleted",
			"recreated",
			"environment",
			"configuration",
			"policy",
			"tool",
			"release",
		):
			with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
				root = Path(directory).resolve() / "project"
				request, report = certificate(root)
				if mutation == "deleted":
					(root / "consumer.py").unlink()
				elif mutation == "environment":
					(root / ".venv/pyvenv.cfg").write_text("changed", encoding="utf-8")
				elif mutation in {"policy", "tool", "release"}:
					release = Path(request["cache_dir"]) / "releases/v2.4.2"
					path = {
						"policy": "quality_gate/policy/mypy.ini",
						"tool": "mypy-2.3.1-fixture.whl",
						"release": "release.toml",
					}[mutation]
					(release / path).write_bytes(b"changed")
				else:
					path = {
						"added": "added.py",
						"modified": "consumer.py",
						"recreated": "deleted.py",
						"configuration": "mypy.ini",
					}[mutation]
					(root / path).write_text("CHANGED = 2\n", encoding="utf-8")
				result = validate_result(json.dumps(report), 0, request, "v9.0.0", monotonic() + 10)
				self.assertEqual(result["status"], "unchecked")
				self.assertEqual(result["reason"], "stale_result")

	def report(self, reason: str = "dependencies") -> dict:
		return {
			"schema": 1,
			"check_id": "mypy",
			"engine_version": "9.0.0",
			"status": "unchecked",
			"reason": reason,
			"action": "private-source-fragment",
			"writes_may_have_occurred": False,
			"root": None,
			"root_identity": None,
			"files": [],
			"configuration": [],
			"selections": [],
			"components": [],
			"freshness_sha256": None,
			"process_count": 0,
			"elapsed_ms": 0,
		}

	def validate(self, output: str, exit_code: int = 2) -> dict:
		from python_types.results import validate_result

		return validate_result(output, exit_code, {}, "v9.0.0", monotonic() + 10)

	def test_provider_prerequisite_failure_is_explicit_without_raw_action_or_context(self) -> None:
		result = self.validate(json.dumps(self.report()))
		self.assertEqual(result["status"], "unchecked")
		self.assertEqual(result["reason"], "dependencies")
		self.assertEqual(result["check"], "types")
		self.assertIs(result["writes_may_have_occurred"], False)
		self.assertNotIn("private-source-fragment", json.dumps(result))

	def test_invalid_envelope_or_exit_agreement_is_never_accepted(self) -> None:
		for change in (
			{"schema": True},
			{"check_id": "other"},
			{"engine_version": "8.0.0"},
			{"status": "pending"},
			{"reason": "private-source-fragment"},
			{"writes_may_have_occurred": True},
			{"process_count": True},
		):
			with self.subTest(change=change):
				result = self.validate(json.dumps({**self.report(), **change}))
				self.assertEqual(result["reason"], "invalid_provider_result")
				self.assertNotIn("private-source-fragment", json.dumps(result))
		self.assertEqual(
			self.validate(json.dumps(self.report()), 0)["reason"], "invalid_provider_result"
		)

	def test_ambiguous_json_and_extra_evidence_are_rejected_without_leaking_it(self) -> None:
		for output in (
			"private-source-fragment",
			json.dumps(self.report()).replace('"schema": 1', '"schema": 1, "schema": 1'),
			json.dumps({**self.report(), "stage": "private-source-fragment"}),
			json.dumps(self.report()) + " " * (256 * 1024),
		):
			with self.subTest(length=len(output)):
				result = self.validate(output)
				self.assertEqual(result["status"], "unchecked")
				self.assertEqual(result["reason"], "invalid_provider_result")
				self.assertNotIn("private-source-fragment", json.dumps(result))


if __name__ == "__main__":
	unittest.main()
