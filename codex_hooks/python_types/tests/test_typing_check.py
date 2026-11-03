"""Executable ingress, immutable transport, disablement and redacted failures."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _shared.quality_settings import load_settings, settings_operation
from _shared.tests.provider_fixture import native_launcher

from python_types.tests.provider_fixture import typing_project_fixture
from python_types.tests.result_fixture import seal

ENTRY = Path(__file__).resolve().parents[1] / "check.py"


@unittest.skipUnless(
	native_launcher().is_file(), "Machine-owned verified release launcher required"
)
class TypingExecutable(unittest.TestCase):
	def test_background_submission_and_explicit_drain_use_the_same_session(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			environment, settings, request, _ = typing_project_fixture(base)
			common = [
				sys.executable,
				"-B",
				str(ENTRY),
				"--settings",
				str(settings),
				"--state-dir",
				str(base / "state"),
				"--session-id",
				"cli-test",
			]
			submission = subprocess.run(
				[*common, "--background"],
				input=json.dumps(self.ingress(request)),
				capture_output=True,
				text=True,
				encoding="utf-8",
				env=environment,
				timeout=5,
				check=False,
			)
			self.assertEqual(submission.stderr, "")
			self.assertEqual(json.loads(submission.stdout)["status"], "pending")
			completion = subprocess.run(
				[*common, "--drain"],
				capture_output=True,
				text=True,
				encoding="utf-8",
				env=environment,
				timeout=10,
				check=False,
			)
			self.assertEqual(completion.stderr, "")
			self.assertEqual(completion.returncode, 0)
			self.assertEqual(
				json.loads(completion.stdout), {"schema": 1, "status": "drained", "reports": []}
			)

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
			check=False,
		)
		self.assertEqual(process.stderr, "")
		self.assertNotIn("private-source-fragment", process.stdout)
		return process, json.loads(process.stdout)

	def ingress(self, request: dict) -> dict:
		return {key: request[key] for key in ("schema", "root", "files")}

	def test_clean_and_failed_reports_use_verified_public_command_and_preserve_deletions(
		self,
	) -> None:
		with tempfile.TemporaryDirectory() as directory:
			environment, settings, request, _ = typing_project_fixture(Path(directory))
			marker = Path(directory) / "request.json"
			process, result = self.invoke(
				{**environment, "TYPING_FIXTURE_MARKER": str(marker)},
				settings,
				self.ingress(request),
			)
			self.assertEqual((result["status"], process.returncode), ("passed", 0))
			sent = json.loads(marker.read_text(encoding="utf-8"))
			self.assertEqual(sent["files"], request["files"])
			self.assertEqual(sent["baseline"], {"policy_release": "v2.4.2"})
			report_path = Path(environment["TYPING_FIXTURE_RESULT"])
			report = json.loads(report_path.read_text(encoding="utf-8"))
			report["status"] = report["components"][0]["status"] = "failed"
			report["components"][0]["diagnostics"] = [
				{"path": "consumer.py", "line": 1, "column": 1, "code": "assignment"}
			]
			report_path.write_text(json.dumps(seal(report)), encoding="utf-8")
			process, result = self.invoke(environment, settings, self.ingress(request))
			self.assertEqual((result["status"], process.returncode), ("failed", 1))
			self.assertEqual(result["findings"][0]["path"], "consumer.py")

	def test_disabled_and_stale_input_never_launch_the_provider(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			environment, settings, request, _ = typing_project_fixture(Path(directory))
			marker = Path(directory) / "called"
			environment["TYPING_FIXTURE_MARKER"] = str(marker)
			(Path(request["root"]) / "example.py").write_text("CHANGED = 2\n", encoding="utf-8")
			process, result = self.invoke(environment, settings, self.ingress(request))
			self.assertEqual((result["reason"], process.returncode), ("stale_input", 2))
			self.assertFalse(marker.exists())
			settings_operation(
				settings, "update", {**load_settings(settings)["current"], "enabled": []}
			)
			process, result = self.invoke(environment, settings, self.ingress(request))
			self.assertEqual((result["status"], process.returncode), ("not_applicable", 0))
			self.assertFalse(marker.exists())

	def test_corrupt_engine_noisy_output_and_invalid_ingress_are_explicitly_incomplete(
		self,
	) -> None:
		with tempfile.TemporaryDirectory() as directory:
			environment, settings, request, wheel = typing_project_fixture(Path(directory))
			for mode in ("malformed", "noisy"):
				with self.subTest(mode=mode):
					process, result = self.invoke(
						{**environment, "TYPING_FIXTURE_MODE": mode},
						settings,
						self.ingress(request),
					)
					self.assertEqual((result["status"], process.returncode), ("unchecked", 2))
			wheel.write_bytes(b"corrupt")
			process, result = self.invoke(environment, settings, self.ingress(request))
			self.assertEqual((result["status"], process.returncode), ("unchecked", 2))
			process, result = self.invoke(
				environment,
				settings,
				{"schema": True, "root": "private-source-fragment", "files": []},
			)
			self.assertEqual((result["status"], process.returncode), ("unchecked", 2))


class TypingBackgroundBudget(unittest.TestCase):
	def test_background_submission_uses_the_selected_foreground_budget(self) -> None:
		from python_types.check import main

		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			settings = base / "python-quality.json"
			settings_operation(settings, "init")
			initial = load_settings(settings)["current"]
			project = base / "project"
			project.mkdir()
			request = json.dumps({"schema": 1, "root": str(project), "files": []}).encode("utf-8")

			for budget, expected in ((0.1, 100.1), (None, 110.0)):
				selection = {
					**initial,
					"provider_release": "v3.2.0",
					"baseline_release": "v2.4.2",
					"enabled": ["types"],
				}
				if budget is not None:
					selection["budget_seconds"] = budget
				settings_operation(settings, "update", selection)
				with (
					patch(
						"sys.argv",
						[
							"check",
							"--settings",
							str(settings),
							"--state-dir",
							str(base / "state"),
							"--session-id",
							"budget-test",
							"--background",
						],
					),
					patch("sys.stdin", SimpleNamespace(buffer=io.BytesIO(request))),
					patch("sys.stdout", io.StringIO()),
					patch("python_types.check.monotonic", return_value=100.0),
					patch(
						"_shared.quality_background.schedule",
						return_value={"check": "types", "status": "pending"},
					) as schedule,
				):
					main()
				self.assertAlmostEqual(schedule.call_args.kwargs["deadline"], expected)

	def test_drain_treats_waived_secret_report_as_successful(self) -> None:
		from python_types.check import main

		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			settings = base / "python-quality.json"
			settings_operation(settings, "init")
			output = io.StringIO()
			with (
				patch(
					"sys.argv",
					[
						str(ENTRY),
						"--settings",
						str(settings),
						"--state-dir",
						str(base / "state"),
						"--session-id",
						"waiver-drain",
						"--drain",
					],
				),
				patch(
					"_shared.quality_background.collect",
					return_value=[{"check": "secrets", "status": "waived"}],
				),
				patch("sys.stdout", output),
			):
				status = main()

		self.assertEqual(status, 0)
		self.assertEqual(
			json.loads(output.getvalue()),
			{
				"schema": 1,
				"status": "drained",
				"reports": [{"check": "secrets", "status": "waived"}],
			},
		)


if __name__ == "__main__":
	unittest.main()
