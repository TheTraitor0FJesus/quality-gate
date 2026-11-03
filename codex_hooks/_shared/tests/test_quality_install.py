"""Installation transactions preserve unrelated registrations and user files."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from time import monotonic, sleep
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from python_quality.install_post_edit_hook import group as post_edit_registration

from _shared.hook_registration import write_configuration
from _shared.quality_install import (
	BUNDLE,
	InstallError,
	_post_edit_group,
	activate,
	bundle_identity,
	prepare,
	rollback,
)
from _shared.quality_install_probe import probe
from _shared.quality_settings import load_settings, settings_operation
from _shared.quality_storage import directory_transaction
from _shared.tests.provider_fixture import install_engine_fixture, native_launcher

SOURCE = Path(__file__).resolve().parents[2]
ENTRY = SOURCE / "_shared" / "quality_install.py"
LEGACY = b'''#!/usr/bin/env python3
"""Codex PostToolUse formatter, sync, and security checks."""

import sys

sys.dont_write_bytecode = True

from _shared.codex_hook_adapter import load_payload, run_post_edit

if __name__ == "__main__":
\trun_post_edit(load_payload())
'''


@contextmanager
def issue_temporary_directory() -> Iterator[Path]:
	root = SOURCE.parent / "temp" / f"issue-167-{uuid4().hex}"
	root.mkdir(parents=True, exist_ok=False)
	try:
		yield root
	finally:
		shutil.rmtree(root)


def locked_worker_code(module: str, arguments: list[str], ready: Path, release: Path) -> str:
	return (
		"import sys\n"
		"from pathlib import Path\n"
		"from time import monotonic, sleep\n"
		f"sys.path.insert(0, {str(SOURCE)!r})\n"
		f"sys.argv = {arguments!r}\n"
		f"from {module} import main\n"
		f"ready = Path({str(ready)!r})\n"
		f"release = Path({str(release)!r})\n"
		"ready.write_text('ready', encoding='ascii')\n"
		"limit = monotonic() + 10\n"
		"while not release.exists() and monotonic() < limit:\n"
		"\tsleep(0.005)\n"
		"if not release.exists():\n"
		"\traise SystemExit(3)\n"
		"raise SystemExit(main())\n"
	)


def start_locked_workers(
	workers: tuple[tuple[str, str, list[str]], ...],
	ready_dir: Path,
	release: Path,
	environment: dict[str, str],
) -> list[tuple[str, subprocess.Popen[str]]]:
	processes: list[tuple[str, subprocess.Popen[str]]] = []
	for name, module, arguments in workers:
		ready = ready_dir / f"{name}.ready"
		process = subprocess.Popen(
			[
				sys.executable,
				"-B",
				"-c",
				locked_worker_code(module, arguments, ready, release),
			],
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			text=True,
			encoding="utf-8",
			env=environment,
		)
		processes.append((name, process))
	return processes


def wait_for_worker_readiness(
	test_case: unittest.TestCase,
	processes: list[tuple[str, subprocess.Popen[str]]],
	ready_paths: list[Path],
) -> None:
	deadline = monotonic() + 5
	while not all(path.exists() for path in ready_paths):
		for name, process in processes:
			if process.poll() is not None:
				stdout, stderr = process.communicate()
				test_case.fail(
					f"{name} failed before lock contention: exit={process.returncode}, "
					f"stdout={stdout!r}, stderr={stderr!r}"
				)
		test_case.assertLess(monotonic(), deadline, "Installer readiness deadline")
		sleep(0.005)


def assert_workers_blocked(
	test_case: unittest.TestCase,
	processes: list[tuple[str, subprocess.Popen[str]]],
) -> None:
	deadline = monotonic() + 0.5
	while monotonic() < deadline:
		for name, process in processes:
			if process.poll() is not None:
				stdout, stderr = process.communicate()
				test_case.fail(
					f"{name} bypassed the shared lock while held: "
					f"exit={process.returncode}, stdout={stdout!r}, stderr={stderr!r}"
				)
		sleep(0.005)


def collect_worker_results(
	test_case: unittest.TestCase,
	processes: list[tuple[str, subprocess.Popen[str]]],
) -> dict[str, str]:
	results: dict[str, str] = {}
	for name, process in processes:
		stdout, stderr = process.communicate(timeout=10)
		test_case.assertEqual(process.returncode, 0, f"{name}: {stdout}\n{stderr}")
		results[name] = stdout
	return results


def stop_locked_workers(release: Path, processes: list[tuple[str, subprocess.Popen[str]]]) -> None:
	release.touch()
	for _, process in processes:
		if process.poll() is None:
			process.kill()
			process.wait(timeout=3)


def run_locked_updates(
	test_case: unittest.TestCase,
	root: Path,
	workers: tuple[tuple[str, str, list[str]], ...],
	ready_dir: Path,
	release: Path,
	environment: dict[str, str],
) -> dict[str, str]:
	processes: list[tuple[str, subprocess.Popen[str]]] = []
	try:
		with directory_transaction(root, deadline=monotonic() + 15):
			processes = start_locked_workers(workers, ready_dir, release, environment)
			wait_for_worker_readiness(
				test_case,
				processes,
				[ready_dir / f"{name}.ready" for name, *_ in workers],
			)
			release.touch()
			assert_workers_blocked(test_case, processes)
		return collect_worker_results(test_case, processes)
	finally:
		stop_locked_workers(release, processes)


class NativeLauncherSelection(unittest.TestCase):
	def test_paused_receipt_skips_registered_events_and_legacy_post_edit(self) -> None:
		from _shared.quality_coordinator import main
		from _shared.quality_install import route_post_edit

		for state in ("active", "prepared"):
			with (
				self.subTest(state=state),
				patch(
					"_shared.quality_install.load_receipt",
					return_value={"state": state, "paused": True},
				),
				patch("_shared.quality_coordinator.read_event") as read,
				patch("_shared.codex_hook_adapter.load_payload") as legacy,
				patch.object(sys, "argv", ["coordinator", "--registered"]),
			):
				self.assertEqual(main(), 0)
				route_post_edit(Path("home"))
				read.assert_not_called()
				legacy.assert_not_called()

	def test_isolated_codex_home_uses_the_checkout_launcher(self) -> None:
		with (
			tempfile.TemporaryDirectory() as directory,
			patch.dict("os.environ", {"CODEX_HOME": directory}),
		):
			expected = Path(__file__).resolve().parents[1] / "native_quality_gate.py"
			self.assertEqual(native_launcher(), expected)


class ManagedRegistration(unittest.TestCase):
	def test_bundle_identity_detects_diagnostic_source_change(self) -> None:
		with issue_temporary_directory() as root:
			for name in BUNDLE:
				path = root / name
				path.parent.mkdir(parents=True, exist_ok=True)
				path.write_bytes(b"original")
			before = bundle_identity(root)
			(root / "_shared/quality_diagnostics.py").write_bytes(b"changed")
			after = bundle_identity(root)
			self.assertNotEqual(
				before["_shared/quality_diagnostics.py"], after["_shared/quality_diagnostics.py"]
			)

	def test_encoded_windows_post_edit_registration_is_recognized(self) -> None:
		group = {
			"matcher": "apply_patch",
			"hooks": [
				{
					"command": (
						"python3 -B /checkout/codex_hooks/python_quality/post_edit_checks.py"
					),
					"commandWindows": (
						"powershell.exe -NoProfile -NonInteractive -EncodedCommand abc123"
					),
					"statusMessage": "Post-edit checks...",
					"timeout": 120,
				}
			],
		}
		self.assertEqual(_post_edit_group({"hooks": {"PostToolUse": [group]}}), (group, 0))

	def test_reinstallation_preserves_user_pause(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			self.fixture(root)
			prepare(root)
			path = root / "hooks/_shared/python-quality-install.json"
			receipt = json.loads(path.read_bytes())
			receipt["paused"] = True
			path.write_text(json.dumps(receipt), encoding="utf-8")
			with (
				patch.dict(
					"os.environ",
					{"LOCALAPPDATA": str(root / "app-data"), "XDG_CACHE_HOME": str(root / "cache")},
				),
				patch("_shared.quality_background.cancel_all", return_value=[]),
			):
				rollback(root)
			prepare(root)
			self.assertTrue(json.loads(path.read_bytes())["paused"])
			with patch("_shared.quality_install_probe.probe", return_value=None):
				activate(root)
			self.assertTrue(json.loads(path.read_bytes())["paused"])

	def test_selected_integrity_reader_must_pass_probe_before_activation(self) -> None:
		settings = {
			"provider_release": "v3.2.0",
			"baseline_release": "v3.1.0",
			"budget_seconds": 10,
			"enabled": ["integrity"],
		}
		with (
			patch("python_quality.check.check_files") as check_ruff,
			patch("file_integrity.check.check_files", return_value={"status": "unchecked"}),
		):
			self.assertEqual(probe(settings), "integrity_probe_unchecked")
		check_ruff.assert_not_called()

	def test_integrity_probe_passes_without_ruff_runtime_or_enablement(self) -> None:
		settings = {
			"provider_release": "v3.2.0",
			"baseline_release": "v3.1.0",
			"budget_seconds": 10,
			"enabled": ["integrity"],
		}
		with (
			patch("python_quality.check.check_files") as check_ruff,
			patch("file_integrity.check.check_files", return_value={"status": "passed"}),
		):
			self.assertIsNone(probe(settings))
		check_ruff.assert_not_called()

	def test_integrity_activation_preserves_only_the_selected_mechanism(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory) / "host"
			self.fixture(root)
			prepare(root)
			settings = root / "hooks" / "_shared" / "python-quality.json"
			selection = {
				"baseline_release": "v3.1.0",
				"provider_release": "v3.2.0",
				"enabled": ["integrity"],
			}
			settings_operation(settings, "update", selection)
			with patch(
				"_shared.quality_install_probe.probe", return_value=None
			) as verify_selected_mechanisms:
				result = activate(root)
			self.assertEqual(load_settings(settings)["current"], selection)
		self.assertEqual(result["state"], "active")
		verify_selected_mechanisms.assert_called_once_with(selection, typing_probe_root=None)

	def test_probe_failure_keeps_registration_prepared(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory) / "host"
			_, wrapper = self.fixture(root)
			prepare(root)
			with patch(
				"_shared.quality_install_probe.probe",
				return_value="integrity_probe_unchecked",
			):
				with self.assertRaisesRegex(InstallError, "integrity_probe_unchecked"):
					activate(root)
			self.assertEqual(wrapper.read_bytes(), LEGACY)
			self.assertEqual(self.invoke(root, "status")[1]["state"], "prepared")

	@unittest.skipUnless(
		native_launcher().is_file(), "Machine-owned native release launcher is not installed"
	)
	def test_selected_secret_reader_must_pass_probe_before_activation(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory) / "host"
			_, wrapper = self.fixture(root)
			self.environment, _ = install_engine_fixture(root)
			self.assertEqual(self.invoke(root, "prepare")[0], 0)
			self.configure_runtime(root, 10)
			settings = root / "hooks" / "_shared" / "python-quality.json"
			settings_operation(
				settings, "update", {**load_settings(settings)["current"], "enabled": ["secrets"]}
			)
			code, result = self.invoke(root, "activate")
			self.assertEqual((code, result.get("reason")), (2, "secret_probe_unchecked"))
			self.assertEqual(wrapper.read_bytes(), LEGACY)
			self.assertEqual(self.invoke(root, "status")[1]["state"], "prepared")

	@unittest.skipUnless(
		native_launcher().is_file(), "Machine-owned native release launcher is not installed"
	)
	def test_secret_probe_activation_and_rollback_preserve_other_registrations(self) -> None:
		from secret_scan.tests.provider_fixture import install_secret_engine_fixture

		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory) / "host"
			original, wrapper = self.fixture(root)
			self.environment, _ = install_secret_engine_fixture(root)
			marker = Path(directory) / "secret-probe-ran"
			self.environment["SECRET_FIXTURE_MARKER"] = str(marker)
			self.assertEqual(self.invoke(root, "prepare")[0], 0)
			self.configure_runtime(root, 10)
			settings = root / "hooks" / "_shared" / "python-quality.json"
			settings_operation(
				settings, "update", {**load_settings(settings)["current"], "enabled": ["secrets"]}
			)
			self.assertEqual(self.invoke(root, "activate")[0], 0)
			self.assertTrue(marker.exists(), "Selected secret provider was never verified")
			self.assertEqual(load_settings(settings)["current"]["enabled"], ["secrets"])
			self.assertEqual(self.invoke(root, "rollback")[0], 0)
			self.assertEqual(load_settings(settings)["current"]["enabled"], [])
			self.assertEqual(wrapper.read_bytes(), LEGACY)
			self.assertEqual(json.loads((root / "hooks.json").read_bytes()), original)

	def fixture(self, root: Path) -> tuple[dict, Path]:
		self.environment = {**os.environ, "LOCALAPPDATA": str(root / "local-app-data")}
		(root / "hooks").mkdir(parents=True)
		for package in (
			"_shared",
			"python_quality",
			"secret_scan",
			"file_integrity",
			"python_types",
		):
			for source in (SOURCE / package).glob("*.py"):
				target = root / "hooks" / package / source.name
				target.parent.mkdir(parents=True, exist_ok=True)
				target.write_bytes(source.read_bytes())
		wrapper = root / "hooks/python_quality/post_edit_checks.py"
		wrapper.parent.mkdir(parents=True, exist_ok=True)
		wrapper.write_bytes(LEGACY)
		configuration: dict[str, object] = {
			"description": "keep",
			"hooks": {
				"PreToolUse": [
					{"matcher": "*", "hooks": [{"type": "command", "command": "existing-safety"}]}
				],
				"PostToolUse": [
					{
						"matcher": "apply_patch",
						"hooks": [{"type": "command", "command": "existing-post-edit"}],
					}
				],
				"Stop": [{"hooks": [{"type": "command", "command": "existing-backup"}]}],
			},
		}
		(root / "hooks.json").write_text(json.dumps(configuration), encoding="utf-8")
		return configuration, wrapper

	def invoke(self, root: Path, action: str) -> tuple[int, dict]:
		result = subprocess.run(
			[sys.executable, "-B", str(ENTRY), "--root", str(root), action],
			capture_output=True,
			text=True,
			encoding="utf-8",
			timeout=10,
			env=getattr(self, "environment", None),
		)
		self.assertTrue(result.stdout.strip(), f"Installer returned no JSON: {result.stderr}")
		return result.returncode, json.loads(result.stdout)

	def configure_runtime(self, root: Path, budget: float | None = 3) -> None:
		configuration: dict[str, object] = {
			"baseline_release": "v3.1.0",
			"provider_release": "v9.0.0",
			"enabled": ["ruff"],
			"baseline_python": sys.executable,
			"baseline_python_version": "3.12",
		}
		if budget is not None:
			configuration["budget_seconds"] = budget
		result = subprocess.run(
			[
				sys.executable,
				"-B",
				str(SOURCE / "_shared" / "quality_settings.py"),
				"--file",
				str(root / "hooks" / "_shared" / "python-quality.json"),
				"update",
			],
			input=json.dumps(configuration),
			capture_output=True,
			text=True,
			encoding="utf-8",
			timeout=5,
		)
		self.assertEqual(result.returncode, 0, result.stdout)

	def test_activation_without_prerequisites_preserves_the_legacy_route(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			_, wrapper = self.fixture(root)
			self.assertEqual(self.invoke(root, "prepare")[0], 0)
			before = (root / "hooks.json").read_bytes()
			code, result = self.invoke(root, "activate")
			self.assertEqual(code, 2)
			self.assertEqual(result["reason"], "activation_prerequisites_missing")
			self.assertEqual(wrapper.read_bytes(), LEGACY)
			self.assertEqual((root / "hooks.json").read_bytes(), before)

	def test_shell_timeout_allows_work_cleanup_and_startup(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			self.fixture(root)
			self.assertEqual(self.invoke(root, "prepare")[0], 0)
			configuration = json.loads((root / "hooks.json").read_bytes())
			for event in ("PreToolUse", "PostToolUse"):
				owned = next(
					item for item in configuration["hooks"][event] if item["matcher"] == "^Bash$"
				)
				# Ten seconds of work, three for cleanup, two for startup/delivery.
				self.assertGreaterEqual(owned["hooks"][0]["timeout"], 15)

	@unittest.skipUnless(
		native_launcher().is_file(), "Machine-owned native release launcher is not installed"
	)
	def test_activation_default_allows_work_beyond_the_latency_target(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory) / "host"
			self.fixture(root)
			self.assertEqual(self.invoke(root, "prepare")[0], 0)
			self.configure_runtime(root, budget=None)
			self.environment, _ = install_engine_fixture(root)
			self.environment["FIXTURE_MIN_REMAINING_MS"] = "2500"
			code, result = self.invoke(root, "activate")
			self.assertEqual(code, 0, result)
			self.assertEqual(result["state"], "active")

	@unittest.skipUnless(
		native_launcher().is_file(), "Machine-owned native release launcher is not installed"
	)
	def test_verified_provider_activation_disable_and_rollback_are_reversible(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory) / "host"
			_, wrapper = self.fixture(root)
			self.assertEqual(self.invoke(root, "prepare")[0], 0)
			self.configure_runtime(root)
			self.environment, _ = install_engine_fixture(root)
			code, result = self.invoke(root, "activate")
			self.assertEqual(code, 0, result)
			self.assertEqual(result["state"], "active")
			self.assertTrue(result["live_acceptance_required"])
			self.assertEqual(wrapper.read_bytes(), LEGACY)
			settings = json.loads((root / "hooks" / "_shared" / "python-quality.json").read_bytes())
			self.assertEqual(settings["current"]["enabled"], ["ruff"])
			disabled = {**settings["current"], "enabled": []}
			changed = subprocess.run(
				[
					sys.executable,
					"-B",
					str(SOURCE / "_shared" / "quality_settings.py"),
					"--file",
					str(root / "hooks" / "_shared" / "python-quality.json"),
					"update",
				],
				input=json.dumps(disabled),
				capture_output=True,
				text=True,
				encoding="utf-8",
				timeout=5,
			)
			self.assertEqual(changed.returncode, 0, changed.stdout)
			if (native_launcher().parent / "codex_hook_adapter.py").is_file():
				project = root.parent / "project"
				project.mkdir()
				token = "sk-" + "a" * 30
				(project / "a.py").write_text(f'token = "{token}"\n', encoding="utf-8")
				calls = Path(self.environment["FIXTURE_PROVIDER_CALLS"])
				before_calls = calls.read_bytes()
				event = {
					"hook_event_name": "PostToolUse",
					"tool_name": "apply_patch",
					"cwd": str(project),
					"tool_input": {
						"command": "*** Begin Patch\n*** Add File: a.py\n+fixture\n*** End Patch"
					},
					"tool_response": "Success. Updated the following files:\nA a.py\n",
				}
				protected = subprocess.run(
					[sys.executable, "-B", str(SOURCE / "python_quality" / "post_edit_checks.py")],
					input=json.dumps(event),
					capture_output=True,
					text=True,
					encoding="utf-8",
					env=self.environment,
					timeout=5,
				)
				self.assertEqual(protected.returncode, 0, protected.stderr)
				self.assertEqual(json.loads(protected.stdout)["decision"], "block")
				self.assertFalse(
					token in protected.stdout + protected.stderr,
					"Synthetic secret was exposed by installed-route output",
				)
				self.assertEqual(calls.read_bytes(), before_calls)
			code, result = self.invoke(root, "rollback")
			self.assertEqual(code, 0, result)
			self.assertEqual(wrapper.read_bytes(), LEGACY)
			settings = json.loads((root / "hooks" / "_shared" / "python-quality.json").read_bytes())
			self.assertEqual(settings["current"]["enabled"], [])

	@unittest.skipUnless(
		native_launcher().is_file(), "Machine-owned native release launcher is not installed"
	)
	def test_corrupt_engine_cannot_activate_or_replace_legacy_protection(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory) / "host"
			_, wrapper = self.fixture(root)
			self.assertEqual(self.invoke(root, "prepare")[0], 0)
			self.configure_runtime(root)
			self.environment, wheel = install_engine_fixture(root)
			wheel.write_bytes(b"corrupt")
			code, result = self.invoke(root, "activate")
			self.assertEqual(code, 2)
			self.assertEqual(result["reason"], "provider_probe_unchecked")
			self.assertEqual(wrapper.read_bytes(), LEGACY)

	def test_prepare_appends_handlers_without_replacing_legacy_or_trust(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			original, wrapper = self.fixture(root)
			trust = root / "config.toml"
			trust.write_bytes(b"# existing trust state\n")
			code, result = self.invoke(root, "prepare")
			self.assertEqual(code, 0)
			self.assertEqual(result["state"], "prepared")
			updated = json.loads((root / "hooks.json").read_bytes())
			self.assertEqual(updated["hooks"]["PreToolUse"][:-1], original["hooks"]["PreToolUse"])
			self.assertEqual(updated["hooks"]["PostToolUse"][:-1], original["hooks"]["PostToolUse"])
			self.assertEqual(updated["hooks"]["Stop"][:-1], original["hooks"]["Stop"])
			self.assertEqual(updated["hooks"]["Interrupt"][0]["hooks"][0]["timeout"], 3)
			self.assertEqual(wrapper.read_bytes(), LEGACY)
			self.assertEqual(trust.read_bytes(), b"# existing trust state\n")

	def test_prepare_is_idempotent(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			self.fixture(root)
			self.assertEqual(self.invoke(root, "prepare")[0], 0)
			before = (root / "hooks.json").read_bytes()
			self.assertEqual(self.invoke(root, "prepare")[0], 0)
			self.assertEqual((root / "hooks.json").read_bytes(), before)

	def test_rollback_preserves_registrations_added_after_prepare(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			original, wrapper = self.fixture(root)
			self.assertEqual(self.invoke(root, "prepare")[0], 0)
			configuration = json.loads((root / "hooks.json").read_bytes())
			later = {"matcher": "*", "hooks": [{"type": "command", "command": "later-user-hook"}]}
			configuration["hooks"]["PostToolUse"].append(later)
			(root / "hooks.json").write_text(json.dumps(configuration), encoding="utf-8")
			code, result = self.invoke(root, "rollback")
			self.assertEqual(code, 0, result)
			self.assertEqual(result["state"], "rolled_back")
			updated = json.loads((root / "hooks.json").read_bytes())
			self.assertEqual(
				updated["hooks"]["PostToolUse"], original["hooks"]["PostToolUse"] + [later]
			)
			self.assertEqual(wrapper.read_bytes(), LEGACY)
			self.assertEqual(self.invoke(root, "prepare")[0], 0)

	def test_custom_legacy_wrapper_is_never_overwritten(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			_, wrapper = self.fixture(root)
			wrapper.write_bytes(b"# custom unmanaged wrapper\n")
			code, result = self.invoke(root, "prepare")
			self.assertEqual(code, 0, result)
			self.assertEqual(result["state"], "prepared")
			self.assertEqual(wrapper.read_bytes(), b"# custom unmanaged wrapper\n")

	def test_rollback_rejects_an_edited_managed_registration(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			self.fixture(root)
			self.assertEqual(self.invoke(root, "prepare")[0], 0)
			configuration = json.loads((root / "hooks.json").read_bytes())
			configuration["hooks"]["PostToolUse"][-1]["hooks"][0]["command"] = "user-edited-command"
			(root / "hooks.json").write_text(json.dumps(configuration), encoding="utf-8")
			before = (root / "hooks.json").read_bytes()
			code, result = self.invoke(root, "rollback")
			self.assertEqual(code, 2)
			self.assertEqual(result["reason"], "registration_conflict")
			self.assertEqual((root / "hooks.json").read_bytes(), before)

	def test_rollback_and_post_edit_install_share_a_bounded_lock(self) -> None:
		with issue_temporary_directory() as temporary:
			root = temporary / "host"
			original, _ = self.fixture(root)
			self.assertEqual(self.invoke(root, "prepare")[0], 0)
			ready_dir = temporary / "ready"
			ready_dir.mkdir()
			release = temporary / "release"
			workers = (
				(
					"rollback",
					"_shared.quality_install",
					["quality-install", "--root", str(root), "rollback"],
				),
				(
					"install",
					"python_quality.install_post_edit_hook",
					[
						"install-post-edit-hook",
						"install",
						"--root",
						str(root),
					],
				),
			)
			results = run_locked_updates(self, root, workers, ready_dir, release, self.environment)

			rollback_result = json.loads(results["rollback"])
			self.assertEqual(rollback_result["state"], "rolled_back")
			configuration = json.loads((root / "hooks.json").read_text(encoding="utf-8"))
			self.assertEqual(configuration["hooks"]["PreToolUse"], original["hooks"]["PreToolUse"])
			self.assertEqual(configuration["hooks"]["Stop"], original["hooks"]["Stop"])
			self.assertIn(
				post_edit_registration(root),
				configuration["hooks"]["PostToolUse"],
			)
			receipt = json.loads(
				(root / "hooks" / "_shared" / "python-quality-install.json").read_text(
					encoding="utf-8"
				)
			)
			self.assertEqual(receipt["state"], "rolled_back")
			self.assertTrue(
				all(
					group not in configuration["hooks"].get(event, [])
					for event, group in receipt["groups"].items()
				)
			)

	def test_failed_hooks_json_replace_keeps_existing_file_valid(self) -> None:
		with issue_temporary_directory() as root:
			original = {
				"description": "preserve this",
				"hooks": {"PreToolUse": [{"matcher": "*", "hooks": []}]},
			}
			path = root / "hooks.json"
			path.write_text(json.dumps(original, indent=2), encoding="utf-8")
			before = path.read_bytes()
			updated = {**original, "hooks": {"PostToolUse": [{"matcher": "apply_patch"}]}}
			with (
				patch(
					"_shared.hook_registration.os.replace",
					side_effect=OSError("replace denied"),
				),
				self.assertRaisesRegex(OSError, "replace denied"),
			):
				write_configuration(root, updated)
			self.assertEqual(path.read_bytes(), before)
			self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)
			self.assertEqual(list(root.glob("tmp*")), [])


if __name__ == "__main__":
	unittest.main()
