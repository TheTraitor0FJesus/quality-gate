"""Executable routing and legacy-security behavior before provider activation."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from time import monotonic
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import _shared.quality_background as quality_background
import _shared.quality_coordinator as quality_coordinator
from _shared.quality_settings import settings_operation
from _shared.tests.provider_fixture import native_launcher

ENTRY = Path(__file__).resolve().parents[1] / "quality_coordinator.py"


class CoordinatedEvents(unittest.TestCase):
	def test_interrupt_cancels_without_stop_drain_or_blocking(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			state = base / "state"
			(state / "background").mkdir(parents=True)
			with (
				mock.patch.object(
					quality_background,
					"cancel_session",
					return_value=[{"check": "types", "status": "unchecked", "reason": "deadline"}],
				) as cancel,
				mock.patch.object(quality_coordinator, "background_reports") as drain,
			):
				before = monotonic()
				result = quality_coordinator.coordinate(
					{"hook_event_name": "Interrupt", "session_id": "session", "turn_id": "turn"},
					settings_path=base / "settings.json",
					state_dir=state,
				)
			self.assertIsNotNone(result)
			assert result is not None
			self.assertIn("systemMessage", result)
			self.assertNotIn("decision", result)
			self.assertLessEqual(cancel.call_args.kwargs["deadline"], before + 3.1)
			drain.assert_not_called()

	def test_blocked_pre_event_discards_capture_and_next_pair_recovers(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			root = base / "project"
			root.mkdir()
			(root / ".git").mkdir()
			(root / "a.py").write_bytes(b"value = 1\n")
			settings = self.configure(base, True)
			state = base / "state"
			event = {
				"hook_event_name": "PreToolUse",
				"tool_name": "Bash",
				"cwd": str(root),
				"session_id": "blocked-pre-test",
				"tool_use_id": "blocked-call",
				"tool_input": {"command": "opaque"},
			}
			finding = {
				"check": "builtin_secrets",
				"status": "failed",
				"files": [{"path": "a.py", "line": 1}],
			}
			with mock.patch.object(
				quality_coordinator, "background_reports", return_value=[finding]
			):
				blocked = quality_coordinator.coordinate(
					event, settings_path=settings, state_dir=state
				)
			assert blocked is not None
			self.assertEqual(blocked.get("decision"), "block")
			self.assertFalse(list(state.glob("*/*.json")))
			self.assertFalse(list(state.glob("*/.conflict")))
			event["tool_use_id"] = "next-call"
			with mock.patch.object(quality_coordinator, "background_reports", return_value=[]):
				self.assertIsNone(
					quality_coordinator.coordinate(event, settings_path=settings, state_dir=state)
				)
				event.update(hook_event_name="PostToolUse", tool_response={"exit_code": 0})
				self.assertIsNone(
					quality_coordinator.coordinate(event, settings_path=settings, state_dir=state)
				)
			self.assertFalse(list(state.glob("*/*.json")))

	def test_informational_pre_feedback_keeps_capture_for_post(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			root = base / "project"
			root.mkdir()
			(root / ".git").mkdir()
			settings = self.configure(base, True)
			state = base / "state"
			event = {
				"hook_event_name": "PreToolUse",
				"tool_name": "Bash",
				"cwd": str(root),
				"session_id": "informational-pre-test",
				"tool_use_id": "call",
				"tool_input": {"command": "opaque"},
			}
			report = {"check": "types", "status": "unchecked", "reason": "runtime"}
			with mock.patch.object(
				quality_coordinator, "background_reports", return_value=[report]
			):
				result = quality_coordinator.coordinate(
					event, settings_path=settings, state_dir=state
				)
			assert result is not None
			self.assertNotIn("decision", result)
			self.assertEqual(len(list(state.glob("*/*.json"))), 1)
			event.update(hook_event_name="PostToolUse", tool_response={"exit_code": 0})
			with mock.patch.object(quality_coordinator, "background_reports", return_value=[]):
				self.assertIsNone(
					quality_coordinator.coordinate(event, settings_path=settings, state_dir=state)
				)
			self.assertFalse(list(state.glob("*/*.json")))

	def test_post_edit_schedules_final_ruff_and_secret_reads_after_writer(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			root = base / "project"
			root.mkdir()
			(root / ".git").mkdir()
			path = root / "a.py"
			path.write_bytes(b"value = 1\n")
			settings = self.configure(base, True)
			settings_operation(
				settings,
				"update",
				{
					"baseline_release": "v2.4.2",
					"provider_release": "v3.2.0",
					"enabled": ["ruff", "secrets", "types"],
				},
			)
			events: list[tuple[Any, ...]] = []

			def write_final(*_args: object, **_kwargs: object) -> dict[str, str]:
				events.append(("writer", _kwargs.get("phase")))
				path.write_bytes(b"value = 2\n")
				return {"status": "passed"}

			def schedule_background(
				_scheduled_root: Path,
				files: list[dict[str, object]],
				*,
				session: quality_background.BackgroundSession,
				deadline: float,
				checks: list[str],
				**_kwargs: object,
			) -> dict[str, str]:
				del _scheduled_root, session, deadline
				events.append(("background", checks, files))
				return {"check": "background", "status": "pending"}

			with (
				mock.patch("python_quality.check.check_files", side_effect=write_final),
				mock.patch(
					"secret_scan.check.check_files",
					side_effect=AssertionError("secret reader ran before background scheduling"),
				),
				mock.patch.object(
					quality_coordinator,
					"legacy_security",
					side_effect=AssertionError("built-in supplement ran synchronously"),
				),
				mock.patch.object(quality_background, "schedule", side_effect=schedule_background),
			):
				result = quality_coordinator.coordinate(
					self.patch_event(root, "a.py"),
					settings_path=settings,
					state_dir=base / "state",
				)

		self.assertIsNone(result, "Clean writes and ordinary background pending must be silent")
		self.assertEqual(
			[item[0] if isinstance(item, tuple) else item for item in events],
			["writer", "background"],
		)
		self.assertEqual(events[0][1], "write")
		_, checks, files = events[1]
		self.assertEqual(checks, ["ruff", "secrets", "types"])
		self.assertEqual(
			files, [{"path": "a.py", "sha256": hashlib.sha256(b"value = 2\n").hexdigest()}]
		)

	@unittest.skipUnless(
		(native_launcher().parent / "codex_hook_adapter.py").is_file(),
		"Existing machine-owned scanner is not installed in this profile",
	)
	def test_invalid_settings_preserve_existing_secret_protection(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			root = base / "project"
			root.mkdir()
			token = "sk-" + "a" * 30
			(root / "a.py").write_text(f'token = "{token}"\n', encoding="utf-8")
			settings = self.configure(base, False)
			settings.write_text("{broken", encoding="utf-8")
			response = quality_coordinator.coordinate(
				self.patch_event(root, "a.py"),
				settings_path=settings,
				state_dir=base / "state",
				evidence_dir=base / "evidence",
				preserve_legacy_security=True,
			)
			assert response is not None
			self.assertEqual(response.get("decision"), "block")
			self.assertIn("UNCHECKED", response["reason"])
			self.assertFalse(token in json.dumps(response), "Synthetic secret was exposed")

	def test_slow_typing_schedule_does_not_exhaust_secrets_deadline(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			root = base / "project"
			root.mkdir()
			(root / "a.py").write_text("value = 1\n", encoding="utf-8")
			settings = self.configure(base, False)
			settings_operation(
				settings,
				"update",
				{
					"baseline_release": "v2.4.2",
					"provider_release": "v3.1.0",
					"enabled": ["ruff", "secrets", "types"],
					"budget_seconds": 2.0,
				},
			)

			class ControlledClock:
				def __init__(self, initial: float) -> None:
					self.current = initial

				def monotonic(self) -> float:
					return self.current

				def advance(self, duration: float) -> None:
					self.current += duration

			clock = ControlledClock(monotonic())
			scheduled = False
			scheduled_checks: list[str] = []

			def check_secrets(
				_root: Path,
				_files: object,
				_settings: dict[str, object],
				deadline: float,
			) -> dict[str, object]:
				del _root, _files, _settings, deadline
				raise AssertionError("secret scanning must run in the shared background job")

			def schedule_typing(
				_root: Path,
				_files: object,
				*,
				session: quality_background.BackgroundSession,
				deadline: float,
				checks: list[str],
				retry: int = 0,
			) -> dict[str, object]:
				nonlocal scheduled
				del session, retry
				scheduled = True
				scheduled_checks.extend(checks)
				clock.advance(0.9)
				if clock.monotonic() >= deadline:
					return {"check": "background", "status": "unchecked", "reason": "deadline"}
				return {"check": "background", "status": "pending"}

			with (
				mock.patch("python_quality.check.check_files", return_value={"status": "passed"}),
				mock.patch("secret_scan.check.check_files", side_effect=check_secrets),
				mock.patch.object(quality_coordinator, "monotonic", side_effect=clock.monotonic),
				mock.patch.object(
					quality_coordinator,
					"legacy_security",
					side_effect=AssertionError("built-in supplement must share the background job"),
				),
				mock.patch.object(quality_background, "schedule", side_effect=schedule_typing),
			):
				result = quality_coordinator.coordinate(
					self.patch_event(root, "a.py"),
					settings_path=settings,
					state_dir=base / "state",
				)

		self.assertTrue(scheduled, "Typing work was not scheduled")
		self.assertEqual(scheduled_checks, ["ruff", "secrets", "types"])
		self.assertIsNone(result, "Slow background typing must not make clean security unchecked")

	def configure(self, base: Path, enabled: bool) -> Path:
		path = base / "configuration" / "settings.json"
		settings_operation(path, "init")
		if enabled:
			settings_operation(
				path,
				"update",
				{"baseline_release": "v2.4.2", "provider_release": "v9.0.0", "enabled": ["ruff"]},
			)
		return path

	def invoke(self, base: Path, settings: Path, event: dict) -> subprocess.CompletedProcess[str]:
		return subprocess.run(
			[
				sys.executable,
				"-B",
				str(ENTRY),
				"--settings",
				str(settings),
				"--state-dir",
				str(base / "state"),
				"--evidence-dir",
				str(base / "evidence"),
			],
			input=json.dumps(event),
			capture_output=True,
			text=True,
			encoding="utf-8",
			timeout=6,
		)

	def patch_event(self, root: Path, name: str, *, success: bool = True) -> dict:
		return {
			"hook_event_name": "PostToolUse",
			"tool_name": "apply_patch",
			"cwd": str(root),
			"session_id": "coordinator-test",
			"tool_use_id": "patch-01",
			"tool_input": {
				"command": f"*** Begin Patch\n*** Add File: {name}\n+fixture\n*** End Patch"
			},
			"tool_response": f"Success. Updated the following files:\nA {name}\n"
			if success
			else "Failed to apply patch",
		}

	def test_disabled_hooks_do_not_snapshot_or_emit_output(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			root = base / "project"
			root.mkdir()
			settings = self.configure(base, False)
			result = self.invoke(base, settings, self.patch_event(root, "missing.py"))
			self.assertEqual(result.returncode, 0, result.stderr)
			self.assertEqual(result.stdout, "")
			self.assertFalse((base / "state").exists())

	def test_read_only_shell_does_not_launch_a_provider_or_emit_findings(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			root = base / "project"
			root.mkdir()
			(root / ".git").mkdir()
			(root / "a.py").write_bytes(b"value = 1\n")
			settings = self.configure(base, True)
			event = {
				"hook_event_name": "PreToolUse",
				"tool_name": "Bash",
				"cwd": str(root),
				"session_id": "coordinator-test",
				"tool_use_id": "shell-01",
				"tool_input": {"command": "opaque"},
			}
			for code in (0, 1):
				with self.subTest(code=code):
					event["hook_event_name"] = "PreToolUse"
					before = self.invoke(base, settings, event)
					self.assertEqual(before.returncode, 0, before.stderr)
					self.assertEqual(before.stdout, "")
					event.update(hook_event_name="PostToolUse", tool_response={"exit_code": code})
					after = self.invoke(base, settings, event)
					self.assertEqual(after.returncode, 0, after.stderr)
					self.assertEqual(after.stdout, "")
			self.assertFalse((base / "state" / "background").exists())

	def test_missing_engine_is_model_visible_and_never_rewrites_the_file(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			root = base / "project"
			root.mkdir()
			original = b"value=  1\n"
			(root / "a.py").write_bytes(original)
			result = self.invoke(base, self.configure(base, True), self.patch_event(root, "a.py"))
			self.assertEqual(result.returncode, 0, result.stderr)
			context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
			self.assertIn("UNCHECKED", context)
			self.assertEqual((root / "a.py").read_bytes(), original)

	@unittest.skipUnless(
		(native_launcher().parent / "codex_hook_adapter.py").is_file(),
		"Existing machine-owned scanner is not installed in this profile",
	)
	def test_existing_secret_protection_runs_even_when_ruff_is_unchecked(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			root = base / "project"
			root.mkdir()
			token = "sk-" + "a" * 30
			(root / "a.py").write_text(f'token = "{token}"\n', encoding="utf-8")
			result = self.invoke(base, self.configure(base, True), self.patch_event(root, "a.py"))
			self.assertEqual(result.returncode, 0, result.stderr)
			response = json.loads(result.stdout)
			self.assertEqual(response["decision"], "block")
			self.assertIn("security", response["reason"].lower())
			self.assertFalse(
				token in result.stdout + result.stderr,
				"Synthetic secret was exposed by coordinator output",
			)

	def test_failed_mutation_reports_incomplete_coverage_without_rewriting(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			root = base / "project"
			root.mkdir()
			(root / "a.py").write_bytes(b"keep= 1\n")
			result = self.invoke(
				base, self.configure(base, True), self.patch_event(root, "a.py", success=False)
			)
			self.assertIn(
				"UNCHECKED", json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
			)
			self.assertEqual((root / "a.py").read_bytes(), b"keep= 1\n")


if __name__ == "__main__":
	unittest.main()
