"""Behavior tests for the executable Codex event attribution boundary."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from itertools import chain, repeat
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _shared.quality_coordinator import coordinate
from _shared.quality_events import (
	MAX_SNAPSHOT_BYTES,
	attribute,
	clear_session_snapshots,
	snapshot_tree,
)
from _shared.quality_settings import settings_operation

ENTRY = Path(__file__).resolve().parents[1] / "quality_events.py"


class ShellDeadline(unittest.TestCase):
	def setUp(self) -> None:
		directory = tempfile.TemporaryDirectory()
		self.addCleanup(directory.cleanup)
		self.base = Path(directory.name)
		self.root = self.base / "project"
		self.root.mkdir()
		(self.root / ".git").mkdir()
		(self.root / "a.py").write_bytes(b"value = 1\n")
		self.payload = {
			"hook_event_name": "PreToolUse",
			"session_id": "deadline-test",
			"tool_use_id": "deadline-01",
			"tool_name": "Bash",
			"cwd": str(self.root),
			"tool_input": {"command": "opaque"},
		}

	def test_standalone_capture_can_finish_after_half_a_second(self) -> None:
		clock = chain([100.0], repeat(100.75))
		with patch("_shared.quality_events.monotonic", side_effect=clock):
			result = attribute(self.payload, state_dir=self.base / "state")
		self.assertEqual(result["status"], "pending", result)

	def test_expired_supplied_deadline_remains_unchecked(self) -> None:
		with patch("_shared.quality_events.monotonic", return_value=100.75):
			result = attribute(self.payload, state_dir=self.base / "state", deadline=100.5)
		self.assertEqual(result["status"], "unchecked", result)
		self.assertEqual(result["reason"], "snapshot_limit")

	def test_post_capture_uses_its_supplied_deadline(self) -> None:
		with patch("_shared.quality_events.monotonic", return_value=100.0):
			before = attribute(self.payload, state_dir=self.base / "state", deadline=101.0)
		self.assertEqual(before["status"], "pending", before)
		self.payload.update(hook_event_name="PostToolUse", tool_response={"exit_code": 0})
		with patch("_shared.quality_events.monotonic", return_value=100.75):
			after = attribute(self.payload, state_dir=self.base / "state", deadline=100.5)
		self.assertEqual(after["status"], "unchecked", after)
		self.assertEqual(after["reason"], "snapshot_limit")

	def test_coordinator_shares_its_remaining_budget_with_capture(self) -> None:
		settings = self.base / "settings.json"
		settings_operation(settings, "init")
		settings_operation(
			settings,
			"update",
			{
				"baseline_release": "v2.4.2",
				"provider_release": "v9.0.0",
				"enabled": ["ruff"],
				"budget_seconds": 0.1,
			},
		)
		with (
			patch("_shared.quality_coordinator.monotonic", return_value=100.0),
			patch("_shared.quality_events.monotonic", return_value=100.75),
		):
			result = coordinate(self.payload, settings_path=settings, state_dir=self.base / "state")
		assert result is not None
		self.assertIn("attribution UNCHECKED", result["hookSpecificOutput"]["additionalContext"])


class SnapshotLimits(unittest.TestCase):
	def test_snapshot_byte_limit_is_aggregate_and_includes_its_boundary(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			root = base / "project"
			root.mkdir()
			(root / ".git").mkdir()
			for number in range(2):
				with (root / f"{number}.bin").open("wb") as stream:
					stream.truncate(16 * 1024 * 1024)
			payload = {
				"hook_event_name": "PreToolUse",
				"session_id": "byte-limit-test",
				"tool_use_id": "limit-01",
				"tool_name": "Bash",
				"cwd": str(root),
				"tool_input": {"command": "opaque"},
			}
			before = attribute(payload, state_dir=base / "state")
			self.assertEqual(before["status"], "pending", before)
			payload.update(hook_event_name="PostToolUse", tool_response={"exit_code": 0})
			after = attribute(payload, state_dir=base / "state")
			self.assertEqual(after["status"], "attributed", after)
			self.assertEqual(after["files"], [])
			(root / "overflow.bin").write_bytes(b"x")
			payload.update(hook_event_name="PreToolUse", tool_use_id="limit-02")
			overflow = attribute(payload, state_dir=base / "state")
			self.assertEqual(overflow["status"], "unchecked", overflow)
			self.assertEqual(overflow["reason"], "snapshot_limit")

	def test_generated_python_caches_do_not_consume_snapshot_budget(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			root, state = base / "project", base / "state"
			(root / ".git").mkdir(parents=True)
			generated = (
				".mypy_cache",
				".pytest_cache",
				".ruff_cache",
				"__pycache__",
				".venv",
				".quality-gate-tmp",
			)
			for directory_name in generated:
				generated_dir = root / directory_name
				generated_dir.mkdir()
				(generated_dir / "generated.bin").write_bytes(
					b"x" * (MAX_SNAPSHOT_BYTES // len(generated) + 1)
				)
			(root / "source.py").write_bytes(b"value = 1\n")
			payload = {
				"hook_event_name": "PreToolUse",
				"session_id": "cache-budget-test",
				"tool_use_id": "cache-budget-call",
				"tool_name": "Bash",
				"cwd": str(root),
				"tool_input": {"command": "opaque"},
			}
			before = attribute(payload, state_dir=state)
			self.assertEqual(before["status"], "pending", before)
			(root / "source.py").write_bytes(b"value = 2\n")
			payload.update(hook_event_name="PostToolUse", tool_response={"exit_code": 0})
			after = attribute(payload, state_dir=state)
			self.assertEqual(after["status"], "attributed", after)
			self.assertEqual([item["path"] for item in after["files"]], ["source.py"])

	def test_generated_cache_entries_do_not_consume_entry_budget(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			cache = root / ".mypy_cache"
			cache.mkdir()
			for index in range(4100):
				(cache / f"entry-{index}.bin").touch()
			self.assertEqual(snapshot_tree(root), {})


class PatchEvents(unittest.TestCase):
	def test_codex_execution_envelope_preserves_patch_confirmation(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / ".git").mkdir()
			(root / "a.py").write_bytes(b"VALUE = 1\n")
			command = "*** Begin Patch\n*** Add File: a.py\n+VALUE = 1\n*** End Patch"
			for newline in ("\n", "\r\n"):
				with self.subTest(newline=newline):
					response = newline.join(
						[
							"Exit code: 0",
							"Wall time: 0 seconds",
							"Output:",
							"Success. Updated the following files:",
							"A a.py",
							"",
						]
					)
					result = self.invoke(root, command, response)
					self.assertEqual(result["status"], "attributed", result)
					self.assertEqual(result["files"][0]["path"], "a.py")

	def test_failed_or_unrecognized_envelope_cannot_confirm_a_patch(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "a.py").write_bytes(b"VALUE = 1\n")
			body = "Success. Updated the following files:\nA a.py\n"
			for header in (
				"Exit code: 1\nWall time: 0 seconds\nOutput:\n",
				"noise\nExit code: 0\nWall time: 0 seconds\nOutput:\n",
				"Exit code: unknown\nWall time: 0 seconds\nOutput:\n",
				"Exit code: 0\nOutput:\n",
			):
				with self.subTest(header=header):
					result = self.invoke(
						root,
						"*** Begin Patch\n*** Add File: a.py\n+VALUE = 1\n*** End Patch",
						header + body,
					)
					self.assertEqual(result["status"], "unchecked", result)
					self.assertEqual(result["reason"], "mutation_not_confirmed")
					self.assertEqual(result["files"], [])

	def invoke(self, root: Path, command: str, response: object) -> dict:
		payload = {
			"hook_event_name": "PostToolUse",
			"session_id": "test-session",
			"tool_use_id": "test-call",
			"tool_name": "apply_patch",
			"cwd": str(root),
			"tool_input": {"command": command},
			"tool_response": response,
		}
		result = subprocess.run(
			[sys.executable, "-B", str(ENTRY)],
			input=json.dumps(payload),
			capture_output=True,
			text=True,
			encoding="utf-8",
			timeout=5,
		)
		self.assertEqual(result.returncode, 0, result.stderr)
		return json.loads(result.stdout)

	def test_multifile_patch_reports_final_content_and_rename_destination(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / ".git").mkdir()
			(root / "space ü.py").write_bytes(b"value = 1\n")
			(root / "renamed.py").write_text("value = 2\n", encoding="utf-8")
			patch = "\n".join(
				[
					"*** Begin Patch",
					"*** Add File: space ü.py",
					"+value = 1",
					"*** Update File: old.py",
					"*** Move to: renamed.py",
					"@@",
					"-value = 0",
					"+value = 2",
					"*** Delete File: gone.py",
					"*** End Patch",
				]
			)
			response = (
				"Success. Updated the following files:\nA space ü.py\nM renamed.py\nD gone.py\n"
			)
			result = self.invoke(root, patch, response)
			self.assertEqual(result["status"], "attributed")
			self.assertEqual(result["schema"], 1)
			self.assertEqual(
				[(item["path"], item["operation"]) for item in result["files"]],
				[("space ü.py", "added"), ("renamed.py", "renamed"), ("gone.py", "deleted")],
			)
			self.assertEqual(
				result["files"][0]["sha256"], hashlib.sha256(b"value = 1\n").hexdigest()
			)
			self.assertIsNone(result["files"][2]["sha256"])

	def test_failed_patch_is_not_a_successful_edit(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			result = self.invoke(
				Path(directory),
				"*** Begin Patch\n*** Add File: a.py\n+x=1\n*** End Patch",
				"Failed to apply patch",
			)
			self.assertEqual(result["status"], "unchecked")
			self.assertEqual(result["files"], [])
			self.assertEqual(result["reason"], "mutation_not_confirmed")

	def test_success_header_cannot_attribute_an_unlisted_existing_file(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "unrelated.py").write_text("keep = 1\n", encoding="utf-8")
			result = self.invoke(
				root,
				"*** Begin Patch\n"
				"*** Update File: unrelated.py\n"
				"@@\n-keep = 0\n+keep = 1\n*** End Patch",
				"Success. Updated the following files:\nM other.py\n",
			)
			self.assertEqual(result["status"], "unchecked")
			self.assertEqual(result["files"], [])
			self.assertEqual(result["reason"], "mutation_not_confirmed")

	def test_relative_escape_is_unchecked(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "project").mkdir()
			(root / "project/.git").mkdir()
			(root / "outside.py").write_text("keep = 1\n", encoding="utf-8")
			result = self.invoke(
				root / "project",
				"*** Begin Patch\n"
				"*** Update File: ../outside.py\n"
				"@@\n-keep = 0\n+keep = 1\n*** End Patch",
				"Success. Updated the following files:\nM ../outside.py\n",
			)
			self.assertEqual(result["status"], "unchecked")
			self.assertEqual(result["reason"], "unsafe_path")

	def test_absolute_path_outside_cwd_is_supported(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / ".git").mkdir()
			(root / "nested").mkdir()
			path = root / "absolute.py"
			path.write_text("value = 1\n", encoding="utf-8")
			result = self.invoke(
				root / "nested",
				f"*** Begin Patch\n*** Add File: {path}\n+value = 1\n*** End Patch",
				f"Success. Updated the following files:\nA {path}\n",
			)
			self.assertEqual(result["status"], "attributed")
			self.assertEqual(result["files"][0]["path"], "absolute.py")

	def test_file_count_limit_rejects_the_whole_batch(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			sections = []
			responses = []
			for index in range(11):
				name = f"file{index}.py"
				(root / name).write_text("value = 1\n", encoding="utf-8")
				sections.append(f"*** Add File: {name}\n+value = 1")
				responses.append(f"A {name}")
			result = self.invoke(
				root,
				"*** Begin Patch\n" + "\n".join(sections) + "\n*** End Patch",
				"Success. Updated the following files:\n" + "\n".join(responses),
			)
			self.assertEqual(result["status"], "unchecked")
			self.assertEqual(result["reason"], "file_count_limit")
			self.assertEqual(result["files"], [])

	def test_oversized_file_is_unchecked(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "large.py").write_bytes(b"x" * (128 * 1024 + 1))
			result = self.invoke(
				root,
				"*** Begin Patch\n*** Add File: large.py\n+x\n*** End Patch",
				"Success. Updated the following files:\nA large.py\n",
			)
			self.assertEqual(result["status"], "unchecked")
			self.assertEqual(result["reason"], "file_size_limit")
			self.assertEqual(result["files"], [])

	def test_nested_workdir_uses_project_relative_paths(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / ".git").mkdir()
			(root / "nested").mkdir()
			(root / "nested/a.py").write_text("value = 1\n", encoding="utf-8")
			result = self.invoke(
				root / "nested",
				"*** Begin Patch\n*** Add File: a.py\n+value = 1\n*** End Patch",
				"Success. Updated the following files:\nA a.py\n",
			)
			self.assertEqual(result["status"], "attributed")
			self.assertEqual(result["files"][0]["path"], "nested/a.py")
			self.assertEqual(Path(result["files"][0]["root"]), root.resolve())

	def test_deleted_target_is_never_read(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			result = self.invoke(
				Path(directory),
				"*** Begin Patch\n*** Delete File: gone.py\n*** End Patch",
				"Success. Updated the following files:\nD gone.py\n",
			)
			self.assertEqual(result["status"], "attributed")
			self.assertEqual(result["files"][0]["operation"], "deleted")
			self.assertIsNone(result["files"][0]["sha256"])

	def test_symlink_target_is_unchecked_without_reading_external_content(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "original.py").write_text("private = 1\n", encoding="utf-8")
			try:
				(root / "linked.py").symlink_to(root / "original.py")
			except OSError:
				self.skipTest("OS account cannot create symbolic links")
			result = self.invoke(
				root,
				"*** Begin Patch\n"
				"*** Update File: linked.py\n"
				"@@\n-private = 0\n+private = 1\n*** End Patch",
				"Success. Updated the following files:\nM linked.py\n",
			)
			self.assertEqual(result["status"], "unchecked")
			self.assertEqual(result["files"], [])
			self.assertEqual(result["reason"], "unsafe_path")

	@unittest.skipUnless(os.name == "nt", "Windows junction boundary")
	def test_windows_junction_cannot_redirect_a_patch_to_external_content(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			root, external = base / "project", base / "external"
			root.mkdir()
			external.mkdir()
			(external / "keep.py").write_bytes(b"value = 1\n")
			link = root / "linked"
			created = subprocess.run(
				["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(external)],
				capture_output=True,
				text=True,
				encoding="utf-8",
				errors="replace",
				timeout=5,
			)
			self.assertEqual(created.returncode, 0, created.stderr)
			try:
				result = self.invoke(
					root,
					"*** Begin Patch\n"
					"*** Update File: linked/keep.py\n"
					"@@\n-value = 0\n+value = 1\n*** End Patch",
					"Success. Updated the following files:\nM linked/keep.py\n",
				)
				self.assertEqual(result["status"], "unchecked")
				self.assertEqual(result["reason"], "unsafe_path")
				self.assertEqual(result["files"], [])
				self.assertEqual((external / "keep.py").read_bytes(), b"value = 1\n")
			finally:
				link.rmdir()


class ShellEvents(unittest.TestCase):
	"""Exact changes come from snapshots, not shell command guesses."""

	def test_untrusted_shell_stdout_cannot_confirm_exit_status(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			root, state = base / "project", base / "state"
			for code in (0, 1):
				with self.subTest(code=code):
					before = self.invoke(root, state, "PreToolUse")
					self.assertEqual(before["status"], "pending", before)
					(root / "a.py").write_text(f"VALUE = {code}\n", encoding="utf-8")
					result = self.invoke(
						root,
						state,
						"PostToolUse",
						f"Exit code: {code}\nWall time: 0.125 seconds\nOutput:\n"
						"Exit code: 0\nWall time: 0 seconds\nOutput:\n",
					)
					self.assertEqual(result["status"], "unchecked", result)
					self.assertEqual(result["files"][0]["path"], "a.py")
					self.assertEqual(result["reason"], "mutation_not_confirmed")

	def test_unrecognized_shell_envelope_retains_incomplete_verification(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			root, state = base / "project", base / "state"
			for response in (
				"Exit code: 0\nOutput:\n",
				"output\nExit code: 0\nWall time: 0 seconds\nOutput:\n",
				"Exit code: true\nWall time: 0 seconds\nOutput:\n",
			):
				with self.subTest(response=response):
					self.invoke(root, state, "PreToolUse")
					result = self.invoke(root, state, "PostToolUse", response)
					self.assertEqual(result["status"], "unchecked", result)
					self.assertEqual(result["reason"], "mutation_not_confirmed")

	def invoke(
		self, root: Path, state: Path, event: str, response: object = None, call: str = "call-01"
	) -> dict:
		root.mkdir(parents=True, exist_ok=True)
		(root / ".git").mkdir(exist_ok=True)
		payload = {
			"hook_event_name": event,
			"session_id": "shell-test",
			"tool_use_id": call,
			"tool_name": "Bash",
			"cwd": str(root),
			"tool_input": {"command": "opaque command", "workdir": str(root)},
			"tool_response": response,
		}
		result = subprocess.run(
			[sys.executable, "-B", str(ENTRY), "--state-dir", str(state)],
			input=json.dumps(payload),
			capture_output=True,
			text=True,
			encoding="utf-8",
			timeout=15,
		)
		self.assertEqual(result.returncode, 0, result.stderr)
		return json.loads(result.stdout)

	def test_overlapping_shell_calls_keep_independent_read_only_coverage(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			root, state = base / "project", base / "state"
			root.mkdir()
			self.invoke(root, state, "PreToolUse", call="first")
			second = self.invoke(root, state, "PreToolUse", call="second")
			self.assertEqual(second["status"], "pending")
			self.assertEqual(len(list(state.rglob("*.json"))), 2)
			(root / "ambiguous.py").write_bytes(b"value = 1\n")
			first = self.invoke(root, state, "PostToolUse", {"exit_code": 0}, call="first")
			second = self.invoke(root, state, "PostToolUse", {"exit_code": 0}, call="second")
			for result in (first, second):
				self.assertEqual(result["status"], "attributed", result)
				self.assertFalse(result["write_allowed"])
				self.assertEqual([item["path"] for item in result["files"]], ["ambiguous.py"])
			self.assertEqual(list(state.rglob("*.json")), [])
			self.assertEqual(list(state.rglob(".conflict")), [])

	def test_pre_recovers_an_expired_incomplete_snapshot(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			state = base / "state"
			root = base / "project"
			root.mkdir()
			(root / ".git").mkdir()
			source = root / "source.py"
			source.write_text("value = 1\n", encoding="utf-8")
			group = state / hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()
			group.mkdir(parents=True)
			orphan = group / "orphan.json"
			orphan.write_text(
				json.dumps({"root": str(root.resolve()), "before": None}), encoding="utf-8"
			)
			os.utime(orphan, (0, 0))
			conflict = group / ".conflict"
			conflict.touch()
			payload = {
				"hook_event_name": "PreToolUse",
				"session_id": "recovery-test",
				"tool_use_id": "recovery-call",
				"tool_name": "Bash",
				"cwd": str(root),
				"tool_input": {"command": "opaque"},
			}

			before = attribute(payload, state_dir=state)

			self.assertEqual(before["status"], "pending", before)
			self.assertFalse(orphan.exists())
			self.assertFalse(conflict.exists())
			source.write_text("value = 2\n", encoding="utf-8")
			payload.update(hook_event_name="PostToolUse", tool_response={"exit_code": 0})
			after = attribute(payload, state_dir=state)
			self.assertEqual(after["status"], "attributed", after)
			self.assertEqual([item["path"] for item in after["files"]], ["source.py"])

	def test_pre_preserves_a_recent_incomplete_snapshot(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			state = base / "state"
			root = base / "project"
			root.mkdir()
			(root / ".git").mkdir()
			group = state / hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()
			group.mkdir(parents=True)
			pending = group / "pending.json"
			pending.write_text(
				json.dumps({"root": str(root.resolve()), "before": None}), encoding="utf-8"
			)
			conflict = group / ".conflict"
			conflict.touch()
			payload = {
				"hook_event_name": "PreToolUse",
				"session_id": "recovery-test",
				"tool_use_id": "next-call",
				"tool_name": "Bash",
				"cwd": str(root),
				"tool_input": {"command": "opaque"},
			}

			result = attribute(payload, state_dir=state)

			self.assertEqual(result["status"], "pending", result)
			self.assertTrue(pending.exists())
			self.assertTrue(conflict.exists())

	def test_pre_preserves_a_completed_snapshot_regardless_of_age(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			state = base / "state"
			root = base / "project"
			root.mkdir()
			(root / ".git").mkdir()
			group = state / hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()
			group.mkdir(parents=True)
			completed = group / "completed.json"
			completed.write_text(
				json.dumps({"root": str(root.resolve()), "before": {}}), encoding="utf-8"
			)
			os.utime(completed, (0, 0))
			payload = {
				"hook_event_name": "PreToolUse",
				"session_id": "recovery-test",
				"tool_use_id": "next-call",
				"tool_name": "Bash",
				"cwd": str(root),
				"tool_input": {"command": "opaque"},
			}

			result = attribute(payload, state_dir=state)

			self.assertEqual(result["status"], "pending", result)
			self.assertTrue(completed.exists())

	def test_session_end_clears_only_its_completed_snapshots(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			state = base / "state"
			root = base / "project"
			root.mkdir()
			group = state / hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()
			group.mkdir(parents=True)
			ended_path, live_path, incomplete_path, legacy_path = (
				group / "ended.json",
				group / "live.json",
				group / "incomplete.json",
				group / "legacy.json",
			)
			for path, document in (
				(ended_path, {"root": str(root), "session_id": "ended", "before": {}}),
				(live_path, {"root": str(root), "session_id": "live", "before": {}}),
				(incomplete_path, {"root": str(root), "session_id": "ended", "before": None}),
				(legacy_path, {"root": str(root), "before": {}}),
			):
				path.write_text(json.dumps(document), encoding="utf-8")
			background = state / "background"
			background.mkdir()
			background_state = background / "worker.json"
			background_state.write_text(
				json.dumps({"session_id": "ended", "before": {}}), encoding="utf-8"
			)
			conflict_group = group
			(conflict_group / ".conflict").touch()
			self.assertEqual(clear_session_snapshots(state, "ended"), 2)
			self.assertFalse(ended_path.exists())
			self.assertTrue(live_path.exists())
			self.assertFalse(incomplete_path.exists())
			self.assertTrue(legacy_path.exists())
			self.assertTrue(background_state.exists())
			self.assertTrue((conflict_group / ".conflict").exists())

	def test_session_end_removes_conflict_marker_after_last_owned_snapshot(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			state = base / "state"
			root = base / "project"
			root.mkdir()
			group = state / hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()
			group.mkdir(parents=True)
			(group / "ended.json").write_text(
				json.dumps({"root": str(root), "session_id": "ended", "before": {}}),
				encoding="utf-8",
			)
			(group / ".conflict").touch()
			self.assertEqual(clear_session_snapshots(state, "ended"), 1)
			self.assertFalse(group.exists())

	def test_session_end_event_cleans_owned_snapshot(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			state = base / "state"
			root = base / "project"
			root.mkdir()
			group = state / hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()
			group.mkdir(parents=True)
			owned = group / "owned.json"
			owned.write_text(
				json.dumps({"root": str(root), "session_id": "ended", "before": {}}),
				encoding="utf-8",
			)
			coordinate(
				{"hook_event_name": "SessionEnd", "session_id": "ended"},
				settings_path=base / "settings.json",
				state_dir=state,
			)
			self.assertFalse(owned.exists())

	def test_turn_completion_releases_abandoned_capture_and_allows_retry(self) -> None:
		for event in ("Stop", "Interrupt"):
			with self.subTest(event=event), tempfile.TemporaryDirectory() as directory:
				base = Path(directory)
				root, state = base / "project", base / "state"
				(root / ".git").mkdir(parents=True)
				payload = {
					"cwd": str(root),
					"hook_event_name": "PreToolUse",
					"session_id": "interrupted-session",
					"turn_id": "interrupted-turn",
					"tool_use_id": "abandoned-call",
					"tool_name": "Bash",
					"tool_input": {"command": "owned mutation"},
				}
				self.assertEqual(attribute(payload, state_dir=state)["status"], "pending")
				blocked = attribute({**payload, "tool_use_id": "overlap"}, state_dir=state)
				self.assertEqual(blocked["status"], "pending")
				with patch("_shared.quality_events.shell_snapshot_finished", return_value=True):
					coordinate(
						{**payload, "hook_event_name": event},
						settings_path=base / "settings.json",
						state_dir=state,
					)
				retry = {**payload, "turn_id": "retry-turn", "tool_use_id": "retry-call"}
				self.assertEqual(attribute(retry, state_dir=state)["status"], "pending")
				(root / "source.py").write_bytes(b"VALUE = 1\n")
				result = attribute(
					{**retry, "hook_event_name": "PostToolUse", "tool_response": {"exit_code": 0}},
					state_dir=state,
				)
				self.assertEqual(result["status"], "attributed")
				self.assertEqual([item["path"] for item in result["files"]], ["source.py"])

	def test_turn_cleanup_preserves_other_owners_and_legacy_captures(self) -> None:
		for event in ("Stop", "Interrupt", "SubagentStop"):
			with self.subTest(event=event), tempfile.TemporaryDirectory() as directory:
				base = Path(directory)
				state, root = base / "state", base / "project"
				group = state / hashlib.sha256(str(root).encode()).hexdigest()
				group.mkdir(parents=True)
				for name, session, turn in (
					("owned", "session", "finished"),
					("other-turn", "session", "active"),
					("other-session", "foreign", "finished"),
					("legacy", "session", None),
				):
					(group / f"{name}.json").write_text(
						json.dumps({"session_id": session, "turn_id": turn, "before": {}}),
						encoding="utf-8",
					)
				(group / ".conflict").touch()
				with patch("_shared.quality_events.shell_snapshot_finished", return_value=True):
					coordinate(
						{"hook_event_name": event, "session_id": "session", "turn_id": "finished"},
						settings_path=base / "settings.json",
						state_dir=state,
					)
				self.assertEqual((group / "owned.json").exists(), event == "SubagentStop")
				for name in ("other-turn", "other-session", "legacy"):
					self.assertTrue((group / f"{name}.json").exists())
				self.assertTrue((group / ".conflict").exists())

	def test_snapshot_excludes_git_metadata_but_not_untracked_files(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			root, state = base / "project", base / "state"
			(root / ".git").mkdir(parents=True)
			self.invoke(root, state, "PreToolUse")
			(root / ".git/index").write_bytes(b"metadata")
			(root / "untracked.py").write_bytes(b"value = 1\n")
			result = self.invoke(root, state, "PostToolUse", {"exit_code": 0})
			self.assertEqual(result["status"], "attributed")
			self.assertEqual([item["path"] for item in result["files"]], ["untracked.py"])

	def test_snapshot_ignores_provider_workspaces_but_keeps_neighbor_sources(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			root, state = base / "project", base / "state"
			self.invoke(root, state, "PreToolUse")
			workspace = root / ".quality-gate-edit-owned" / "source"
			workspace.mkdir(parents=True)
			(workspace / "copied.py").write_bytes(b"COPIED = 1\n")
			(root / ".quality-gate-edit.py").write_bytes(b"NEIGHBOR = 2\n")
			result = self.invoke(root, state, "PostToolUse", {"exit_code": 0})
			self.assertEqual(result["status"], "attributed", result)
			self.assertEqual([item["path"] for item in result["files"]], [".quality-gate-edit.py"])

	def test_snapshot_ignores_root_temp_but_tracks_nested_temp_sources(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			root, state = base / "project", base / "state"
			self.invoke(root, state, "PreToolUse")
			workspace = root / "temp" / "qg-owned" / "source"
			workspace.mkdir(parents=True)
			(workspace / "copy.py").write_bytes(b"COPIED = 1\n")
			project_data = root / "package" / "temp"
			project_data.mkdir(parents=True)
			(project_data / "source.py").write_bytes(b"SOURCE = 2\n")
			result = self.invoke(root, state, "PostToolUse", {"exit_code": 0})
			self.assertEqual(result["status"], "attributed", result)
			self.assertEqual([item["path"] for item in result["files"]], ["package/temp/source.py"])

	def test_shell_detects_modified_untracked_and_deleted_content(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			root, state = base / "project", base / "state"
			root.mkdir()
			(root / "before.py").write_bytes(b"value = 1\n")
			(root / "gone.py").write_bytes(b"value = 2\n")
			before = self.invoke(root, state, "PreToolUse")
			self.assertEqual(before["status"], "pending")
			(root / "before.py").write_bytes(b"value = 3\n")
			(root / "new ü.py").write_bytes(b"value = 4\n")
			(root / "gone.py").unlink()
			after = self.invoke(root, state, "PostToolUse", {"exit_code": 0})
			self.assertEqual(after["status"], "attributed")
			self.assertEqual(
				{item["path"]: item["operation"] for item in after["files"]},
				{"before.py": "modified", "new ü.py": "added", "gone.py": "deleted"},
			)
			self.assertEqual(list(state.rglob("*.json")), [])

	def test_read_only_shell_has_no_touched_files(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			root, state = base / "project", base / "state"
			root.mkdir()
			(root / "same.py").write_bytes(b"value = 1\n")
			self.invoke(root, state, "PreToolUse")
			result = self.invoke(root, state, "PostToolUse", {"exit_code": 0})
			self.assertEqual(result["status"], "attributed")
			self.assertEqual(result["files"], [])

	def test_shell_without_before_snapshot_cannot_claim_coverage(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			root = base / "project"
			root.mkdir()
			for code in (0, 1):
				with self.subTest(code=code):
					result = self.invoke(root, base / "state", "PostToolUse", {"exit_code": code})
					self.assertEqual(result["status"], "unchecked")
					self.assertEqual(result["reason"], "snapshot_missing")

	def test_failed_shell_without_changes_needs_no_edit_verification(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			root, state = base / "project", base / "state"
			self.invoke(root, state, "PreToolUse")
			result = self.invoke(root, state, "PostToolUse", {"exit_code": 1})
			self.assertEqual(result["status"], "not_applicable")
			self.assertEqual(result["files"], [])
			self.assertEqual(list(state.rglob("*.json")), [])

	def test_failed_shell_reports_partial_changes_without_claiming_success(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			root, state = base / "project", base / "state"
			root.mkdir()
			self.invoke(root, state, "PreToolUse")
			(root / "partial.py").write_bytes(b"value = 1\n")
			result = self.invoke(root, state, "PostToolUse", {"exit_code": 1})
			self.assertEqual(result["status"], "unchecked")
			self.assertEqual(result["reason"], "mutation_failed")
			self.assertEqual(result["files"][0]["path"], "partial.py")


if __name__ == "__main__":
	unittest.main()
