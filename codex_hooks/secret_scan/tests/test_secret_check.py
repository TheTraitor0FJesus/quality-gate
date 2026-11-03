"""Executable hook ingress, verified engine transport and redacted failures."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _shared.quality_settings import load_settings, settings_operation
from _shared.tests.provider_fixture import native_launcher

from secret_scan.tests.provider_fixture import secret_project_fixture

ENTRY = Path(__file__).resolve().parents[1] / "check.py"


@unittest.skipUnless(
	native_launcher().is_file(), "Machine-owned native release launcher is not installed"
)
class SecretExecutable(unittest.TestCase):
	def invoke(
		self, environment: dict, settings: Path, request: object
	) -> tuple[subprocess.CompletedProcess, dict]:
		process = subprocess.run(
			[sys.executable, "-B", str(ENTRY), "--settings", str(settings)],
			input=json.dumps(request),
			capture_output=True,
			text=True,
			encoding="utf-8",
			env=environment,
			timeout=15,
		)
		self.assertEqual(process.stderr, "")
		self.assertNotIn("synthetic-private", process.stdout)
		return process, json.loads(process.stdout)

	def test_public_cli_accepts_verified_clean_failed_and_waived_reports(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			environment, settings, request, _ = secret_project_fixture(Path(directory))
			for status, code in (("passed", 0), ("failed", 1), ("waived", 0)):
				with self.subTest(status=status):
					process, result = self.invoke(
						{**environment, "SECRET_FIXTURE_MODE": status}, settings, request
					)
					self.assertEqual((result["status"], process.returncode), (status, code))

	def test_malformed_or_noisy_provider_output_is_unchecked_without_leakage(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			environment, settings, request, _ = secret_project_fixture(Path(directory))
			for mode in ("malformed", "noisy"):
				process, result = self.invoke(
					{**environment, "SECRET_FIXTURE_MODE": mode}, settings, request
				)
				self.assertEqual((result["status"], process.returncode), ("unchecked", 2))

	def test_corrupt_engine_does_not_execute_or_fall_back(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			environment, settings, request, wheel = secret_project_fixture(base)
			marker = base / "provider-called"
			wheel.write_bytes(b"corrupt")
			process, result = self.invoke(
				{**environment, "SECRET_FIXTURE_MARKER": str(marker)}, settings, request
			)
			self.assertEqual((result["status"], process.returncode), ("unchecked", 2))
			self.assertFalse(marker.exists())

	def test_stale_input_is_rejected_before_provider_launch(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			environment, settings, request, _ = secret_project_fixture(base)
			marker = base / "provider-called"
			(Path(request["root"]) / "a.py").write_bytes(b"VALUE = 2\n")
			process, result = self.invoke(
				{**environment, "SECRET_FIXTURE_MARKER": str(marker)}, settings, request
			)
			self.assertEqual((result["reason"], process.returncode), ("stale_input", 2))
			self.assertFalse(marker.exists())

	def test_invalid_ingress_never_launches_provider_or_echoes_payload(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			environment, settings, _, _ = secret_project_fixture(base)
			marker = base / "provider-called"
			for payload in (
				"synthetic-private",
				'{"schema": 1, "schema": 1, "root": "synthetic-private", "files": []}',
				"synthetic-private" * 5000,
			):
				process = subprocess.run(
					[sys.executable, "-B", str(ENTRY), "--settings", str(settings)],
					input=payload,
					capture_output=True,
					text=True,
					encoding="utf-8",
					env={**environment, "SECRET_FIXTURE_MARKER": str(marker)},
					timeout=15,
				)
				self.assertEqual((process.returncode, process.stderr), (2, ""))
				self.assertEqual(json.loads(process.stdout)["status"], "unchecked")
				self.assertNotIn("synthetic-private", process.stdout)
				self.assertFalse(marker.exists())

	@unittest.skipUnless(sys.platform == "win32", "Windows owned-process contract")
	def test_deadline_terminates_provider_descendants_before_returning(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			environment, settings, request, _ = secret_project_fixture(base)
			current = load_settings(settings)["current"]
			settings_operation(settings, "update", {**current, "budget_seconds": 2})
			marker = base / "child.pid"
			process, result = self.invoke(
				{
					**environment,
					"SECRET_FIXTURE_MODE": "hang",
					"SECRET_FIXTURE_MARKER": str(marker),
				},
				settings,
				request,
			)
			self.assertEqual((result["reason"], process.returncode), ("deadline", 2))
			self.assertTrue(marker.exists(), "Provider never reached the cancellation fixture")
			pid = marker.read_text(encoding="utf-8")
			self.assertTrue(pid.isdecimal(), "Provider never published its descendant PID")
			remaining = subprocess.run(
				["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
				capture_output=True,
				text=True,
				timeout=5,
			)
			self.assertNotIn(f'"{pid}"', remaining.stdout)


if __name__ == "__main__":
	unittest.main()
