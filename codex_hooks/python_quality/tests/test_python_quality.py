"""Host validation of the provider's public edit-check result contract."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _shared.quality_settings import settings_operation
from _shared.tests.provider_fixture import install_engine_fixture, native_launcher

from python_quality.check import check_files, main, validate_result

SOURCE = Path(__file__).resolve().parents[2]


@unittest.skipUnless(
	native_launcher().is_file(), "Machine-owned native release launcher is not installed"
)
class InvocationBudget(unittest.TestCase):
	def invoke(self, entry: str, budget: float | None = None) -> subprocess.CompletedProcess[str]:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			environment, _ = install_engine_fixture(base / "host")
			# A controlled provider refuses insufficient grants without sleeping.
			environment["FIXTURE_MIN_REMAINING_MS"] = "2500"
			root = base / "project"
			root.mkdir()
			content = b"value = 1\n"
			(root / "a.py").write_bytes(content)
			settings = base / "settings.json"
			settings_operation(settings, "init")
			selection: dict[str, object] = {
				"baseline_release": "v2.4.2",
				"provider_release": "v9.0.0",
				"enabled": ["ruff"],
			}
			if budget is not None:
				selection["budget_seconds"] = budget
			settings_operation(settings, "update", selection)
			request = {
				"schema": 1,
				"root": str(root),
				"files": [{"path": "a.py", "sha256": hashlib.sha256(content).hexdigest()}],
			}
			if entry == "_shared/quality_coordinator.py":
				request = {
					"hook_event_name": "PostToolUse",
					"tool_name": "apply_patch",
					"cwd": str(root),
					"tool_input": {
						"command": "*** Begin Patch\n*** Add File: a.py\n+value = 1\n*** End Patch"
					},
					"tool_response": "Success. Updated the following files:\nA a.py\n",
				}
			return subprocess.run(
				[sys.executable, "-B", str(SOURCE / entry), "--settings", str(settings)],
				input=json.dumps(request),
				capture_output=True,
				text=True,
				encoding="utf-8",
				env=environment,
				timeout=15,
			)

	def test_direct_default_allows_work_beyond_the_latency_target(self) -> None:
		result = self.invoke("python_quality/check.py")
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertEqual(json.loads(result.stdout)["status"], "passed")

	def test_direct_entry_honors_the_configured_budget(self) -> None:
		result = self.invoke("python_quality/check.py", 5.0)
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertEqual(json.loads(result.stdout)["status"], "passed")

	@unittest.skipUnless(
		(native_launcher().parent / "codex_hook_adapter.py").is_file(),
		"Existing machine-owned scanner is not installed in this profile",
	)
	def test_coordinator_without_session_reports_background_as_unchecked(self) -> None:
		result = self.invoke("_shared/quality_coordinator.py")
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertIn("background UNCHECKED", result.stdout)


class ProviderResults(unittest.TestCase):
	def test_short_unchecked_reply_preserves_fixed_reason_without_success_fields(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			request, _ = self.fixture(Path(directory))
			response = {
				"schema": 1,
				"status": "unchecked",
				"reason": "io",
				"action": "Provider text must not be forwarded",
				"files": [],
				"writes_may_have_occurred": True,
				"elapsed_ms": 0,
			}
			result = validate_result(json.dumps(response), 2, request, "v2.6.0")
			self.assertEqual(result["status"], "unchecked")
			self.assertEqual(result["reason"], "io")
			self.assertTrue(result["writes_may_have_occurred"])
			self.assertNotIn("Provider text", json.dumps(result))

	def fixture(self, root: Path) -> tuple[dict, dict]:
		(root / "a.py").write_bytes(b"import os\n")
		digest = hashlib.sha256(b"import os\n").hexdigest()
		request = {
			"schema": 1,
			"root": str(root.resolve()),
			"files": [{"path": "a.py", "sha256": digest}],
		}
		result = {
			"schema": 1,
			"engine_version": "9.0.0",
			"root": str(root.resolve()),
			"status": "failed",
			"reason": None,
			"files": [
				{
					"path": "a.py",
					"status": "failed",
					"coverage": "baseline",
					"component": None,
					"input_sha256": digest,
					"sha256": digest,
					"changed": False,
					"selection": 0,
					"findings": [
						{
							"check_id": "ruff",
							"rule": "F401",
							"line": 1,
							"column": 8,
							"action": "This provider action must never be echoed blindly",
						}
					],
					"format": "passed",
				}
			],
			"selections": [
				{
					"policy_release": "v2.4.2",
					"release_sha256": "a" * 64,
					"policy_sha256": "b" * 64,
					"ruff_version": "0.16.4",
					"ruff_sha256": "c" * 64,
					"python": sys.executable,
					"python_version": "3.12",
					"python_sha256": "d" * 64,
					"runtime_fingerprint": "e" * 64,
				}
			],
			"inputs": [],
			"process_count": 1,
			"elapsed_ms": 1,
			"writes_may_have_occurred": False,
		}
		return request, result

	def test_residual_findings_keep_final_identity_without_echoing_provider_text(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			request, response = self.fixture(Path(directory))
			result = validate_result(json.dumps(response), 1, request, "v9.0.0")
			self.assertEqual(result["status"], "failed")
			self.assertEqual(result["files"][0]["findings"][0]["rule"], "F401")
			self.assertNotIn("This provider action", json.dumps(result))
			self.assertEqual(result["files"][0]["sha256"], request["files"][0]["sha256"])

	def test_read_only_validation_requires_explicit_unchanged_response(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			request, response = self.fixture(Path(directory))
			request["phase"] = "validate"
			response.update(phase="validate", writes_may_have_occurred=False)
			result = validate_result(json.dumps(response), 1, request, "v9.0.0", phase="validate")
		self.assertEqual(result["status"], "failed")
		self.assertEqual(result["phase"], "validate")
		self.assertFalse(result["writes_may_have_occurred"])
		self.assertFalse(result["files"][0]["changed"])
		self.assertEqual(result["files"][0]["sha256"], result["files"][0]["input_sha256"])

	def test_read_only_validation_rejects_possible_writes_or_changed_bytes(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			request, response = self.fixture(Path(directory))
			request["phase"] = "validate"
			response.update(phase="validate", writes_may_have_occurred=False)
			response["files"][0].update(changed=True, sha256="f" * 64)
			result = validate_result(json.dumps(response), 1, request, "v9.0.0", phase="validate")
		self.assertEqual(result["status"], "unchecked")
		self.assertEqual(result["reason"], "invalid_provider_result")
		self.assertEqual(result["phase"], "validate")
		self.assertFalse(result["writes_may_have_occurred"])

	def test_read_only_validation_rejects_a_write_claim(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			request, response = self.fixture(Path(directory))
			request["phase"] = "validate"
			response.update(phase="validate", writes_may_have_occurred=True)
			result = validate_result(json.dumps(response), 1, request, "v9.0.0", phase="validate")
		self.assertEqual(result["status"], "unchecked")
		self.assertEqual(result["reason"], "invalid_provider_result")
		self.assertEqual(result["phase"], "validate")
		self.assertFalse(result["writes_may_have_occurred"])

	def test_excluded_file_survives_revalidation_and_still_checks_freshness(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			request, response = self.fixture(root)
			excluded = root / "excluded.py"
			content = b"value = 1\n"
			excluded.write_bytes(content)
			digest = hashlib.sha256(content).hexdigest()
			request["phase"] = "validate"
			request["files"].append({"path": excluded.name, "sha256": digest})
			response.update(phase="validate", writes_may_have_occurred=False)
			response["files"].append(
				{
					"path": excluded.name,
					"status": "not_applicable",
					"coverage": "baseline",
					"component": None,
					"input_sha256": digest,
					"sha256": digest,
					"changed": False,
					"selection": 0,
					"findings": [],
					"format": "not_applicable",
				}
			)
			first = validate_result(json.dumps(response), 1, request, "v9.0.0", phase="validate")
			second = validate_result(json.dumps(first), 1, request, "v9.0.0", phase="validate")
			excluded.write_bytes(b"value = 2\n")
			stale = validate_result(json.dumps(second), 1, request, "v9.0.0", phase="validate")

		self.assertEqual(first["status"], "failed")
		self.assertEqual(second["status"], "failed")
		self.assertEqual(second["files"][1]["input_sha256"], digest)
		self.assertFalse(second["files"][1]["changed"])
		self.assertEqual(stale["status"], "unchecked")
		self.assertEqual(stale["reason"], "stale_result")

	def test_all_excluded_files_remain_not_applicable_during_revalidation(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			request, response = self.fixture(root)
			digest = request["files"][0]["sha256"]
			request["phase"] = "validate"
			response["files"][0].update(
				status="not_applicable",
				input_sha256=digest,
				changed=False,
				findings=[],
				format="not_applicable",
			)
			response.update(
				status="not_applicable",
				phase="validate",
				writes_may_have_occurred=False,
			)
			result = validate_result(json.dumps(response), 0, request, "v9.0.0", phase="validate")
		self.assertEqual(result["status"], "not_applicable")
		self.assertEqual(result["files"][0]["status"], "not_applicable")
		self.assertFalse(result["writes_may_have_occurred"])

	def test_check_files_forwards_read_only_validation_phase(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			request, response = self.fixture(root)
			response.update(phase="validate", writes_may_have_occurred=False)
			settings = {
				"enabled": ["ruff"],
				"provider_release": "v9.0.0",
				"baseline_release": "v2.4.2",
				"baseline_python": sys.executable,
				"baseline_python_version": "3.12",
			}
			with patch(
				"python_quality.check.execute",
				return_value=SimpleNamespace(stdout=json.dumps(response), returncode=1),
			) as invoke_provider:
				result = check_files(
					root,
					request["files"],
					settings,
					monotonic() + 20,
					phase="validate",
				)
			sent = json.loads(invoke_provider.call_args.kwargs["input_text"])
		self.assertEqual(sent["phase"], "validate")
		self.assertEqual(sent["files"], request["files"])
		self.assertEqual(result["status"], "failed")
		self.assertEqual(result["phase"], "validate")

	def test_check_files_forwards_writer_only_phase_and_final_hash(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			request, response = self.fixture(root)
			request["phase"] = "write"
			final = b"import os\n\nvalue = 1\n"
			(root / "a.py").write_bytes(final)
			file = response["files"][0]
			file.update(
				status="passed",
				input_sha256=request["files"][0]["sha256"],
				sha256=hashlib.sha256(final).hexdigest(),
				changed=True,
				findings=[],
				format="passed",
			)
			response.update(
				phase="write",
				status="passed",
				writes_may_have_occurred=True,
			)
			settings = {
				"enabled": ["ruff"],
				"provider_release": "v9.0.0",
				"baseline_release": "v2.4.2",
				"baseline_python": sys.executable,
				"baseline_python_version": "3.12",
			}
			with patch(
				"python_quality.check.execute",
				return_value=SimpleNamespace(stdout=json.dumps(response), returncode=0),
			) as invoke_provider:
				result = check_files(
					root,
					request["files"],
					settings,
					monotonic() + 20,
					phase="write",
				)
			sent = json.loads(invoke_provider.call_args.kwargs["input_text"])
		self.assertEqual(sent["phase"], "write")
		self.assertEqual(result["phase"], "write")
		self.assertEqual(result["status"], "passed")
		self.assertTrue(result["writes_may_have_occurred"])
		self.assertTrue(result["files"][0]["changed"])
		self.assertEqual(result["files"][0]["sha256"], hashlib.sha256(final).hexdigest())
		self.assertEqual(result["files"][0]["findings"], [])

	def test_entry_point_accepts_writer_only_phase(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			content = b"value = 1\n"
			(root / "a.py").write_bytes(content)
			request = {
				"schema": 1,
				"root": str(root.resolve()),
				"files": [{"path": "a.py", "sha256": hashlib.sha256(content).hexdigest()}],
				"phase": "write",
			}
			response = {
				"schema": 1,
				"status": "not_applicable",
				"reason": "disabled",
				"files": [],
				"phase": "write",
				"writes_may_have_occurred": False,
			}
			stdout = io.StringIO()
			with (
				patch.object(sys, "argv", ["check.py"]),
				patch.object(
					sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode()))
				),
				patch.object(sys, "stdout", stdout),
				patch("python_quality.check.load_settings", return_value={"current": {}}),
				patch("python_quality.check.check_files", return_value=response) as invoke,
			):
				status = main()
		self.assertEqual(status, 0)
		self.assertEqual(json.loads(stdout.getvalue()), response)
		self.assertEqual(invoke.call_args.kwargs["phase"], "write")

	def test_inapplicable_validation_still_proves_read_only_phase(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			result = check_files(
				Path(directory), [], {"enabled": ["ruff"]}, monotonic() + 1, phase="validate"
			)
		self.assertEqual(result["status"], "not_applicable")
		self.assertEqual(result["phase"], "validate")
		self.assertFalse(result["writes_may_have_occurred"])

	def test_stale_final_file_is_unchecked(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			request, response = self.fixture(root)
			(root / "a.py").write_bytes(b"value = 2\n")
			result = validate_result(json.dumps(response), 1, request, "v9.0.0")
			self.assertEqual(result["status"], "unchecked")
			self.assertEqual(result["reason"], "stale_result")

	def test_shared_handoff_preserves_component_identity(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			request, response = self.fixture(Path(directory))
			response["files"][0].update(coverage="managed", component="app")
			result = validate_result(json.dumps(response), 1, request, "v9.0.0")
			self.assertEqual(result["files"][0].get("component"), "app")

	def test_provider_configuration_inputs_must_be_project_relative(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			request, response = self.fixture(root)
			response["inputs"] = [
				{"path": str(root / "a.py"), "sha256": request["files"][0]["sha256"]}
			]
			result = validate_result(json.dumps(response), 1, request, "v9.0.0")
			self.assertEqual(result["status"], "unchecked")

	def test_status_and_exit_code_must_agree(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			request, response = self.fixture(Path(directory))
			result = validate_result(json.dumps(response), 0, request, "v9.0.0")
			self.assertEqual(result["status"], "unchecked")
			self.assertEqual(result["reason"], "invalid_provider_result")

	def test_unrequested_path_cannot_be_accepted(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			request, response = self.fixture(Path(directory))
			response["files"][0]["path"] = "../outside.py"
			result = validate_result(json.dumps(response), 1, request, "v9.0.0")
			self.assertEqual(result["status"], "unchecked")
			self.assertEqual(result["reason"], "invalid_provider_result")

	def test_arbitrary_exception_text_is_never_forwarded(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			request, response = self.fixture(Path(directory))
			response.update(status="unchecked", reason="private-fragment-from-subprocess", files=[])
			result = validate_result(json.dumps(response), 2, request, "v9.0.0")
			self.assertEqual(result["status"], "unchecked")
			self.assertNotIn("private-fragment", json.dumps(result))


if __name__ == "__main__":
	unittest.main()
