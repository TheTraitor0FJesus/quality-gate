"""Shell attribution uses trusted completion metadata rather than program output."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _shared.quality_completion import (
	shell_exit_code,
	shell_snapshot_finished,
	shell_snapshot_unrecorded,
)
from _shared.quality_coordinator import coordinate
from _shared.quality_events import attribute, shell_event_key


class ShellCompletion(unittest.TestCase):
	def setUp(self) -> None:
		directory = tempfile.TemporaryDirectory()
		self.addCleanup(directory.cleanup)
		self.base = Path(directory.name).resolve()
		self.root = self.base / "project"
		self.root.mkdir()
		(self.root / ".git").mkdir()
		self.host = self.base / "host"
		self.sessions = self.host / "sessions"
		self.sessions.mkdir(parents=True)
		self.session = "11111111-1111-1111-1111-111111111111"
		self.turn = "22222222-2222-2222-2222-222222222222"
		self.transcript = self.sessions / f"rollout-test-{self.session}.jsonl"
		self.payload: dict = {
			"session_id": self.session,
			"turn_id": self.turn,
			"tool_use_id": "exec-test-call",
			"hook_event_name": "PreToolUse",
			"tool_name": "Bash",
			"cwd": str(self.root),
			"transcript_path": str(self.transcript),
			"tool_input": {"command": "opaque"},
			"tool_response": "untrusted output",
		}
		environment = patch.dict(os.environ, {"CODEX_HOME": str(self.host)})
		environment.start()
		self.addCleanup(environment.stop)

	def record(self, code: object = 0) -> dict:
		return {
			"type": "event_msg",
			"payload": {
				"type": "item_completed",
				"thread_id": self.session,
				"turn_id": self.turn,
				"item": {
					"type": "CommandExecution",
					"id": "exec-test-call",
					"cwd": self.root.as_uri(),
					"exit_code": code,
					"status": "completed" if code == 0 else "failed",
					"aggregated_output": "source must not enter the attribution result",
				},
			},
		}

	def history(
		self,
		*,
		code: object = 0,
		status: str = "completed",
		turn_status: str = "completed",
		**overrides: object,
	) -> Path:
		path = self.host / "thread_history_1.sqlite"
		item: dict[str, object] = {
			"type": "commandExecution",
			"id": self.payload["tool_use_id"],
			"status": status,
			"exitCode": code,
			"cwd": str(self.root),
		}
		item.update(overrides)
		with closing(sqlite3.connect(path)) as database, database:
			database.execute(
				"CREATE TABLE IF NOT EXISTS thread_items (thread_id TEXT, turn_id TEXT, "
				"item_id TEXT, item_json TEXT, item_type TEXT, rollout_ordinal INTEGER)"
			)
			database.execute(
				"CREATE TABLE IF NOT EXISTS thread_turns "
				"(thread_id TEXT, turn_id TEXT, status TEXT)"
			)
			database.execute("DELETE FROM thread_items")
			database.execute("DELETE FROM thread_turns")
			database.execute(
				"INSERT INTO thread_items VALUES (?, ?, ?, ?, 'commandExecution', 1)",
				(self.session, self.turn, self.payload["tool_use_id"], json.dumps(item)),
			)
			database.execute(
				"INSERT INTO thread_turns VALUES (?, ?, ?)",
				(self.session, self.turn, turn_status),
			)
		return path

	def child_history(self, *, kind: str = "started", parent: str | None = None) -> Path:
		path = self.history()
		child = "33333333-3333-3333-3333-333333333333"
		spawn = {"type": "subAgentActivity", "kind": kind, "agentThreadId": child}
		with closing(sqlite3.connect(path)) as database, database:
			database.execute("UPDATE thread_items SET thread_id=?", (child,))
			database.execute("UPDATE thread_turns SET thread_id=?", (child,))
			database.execute(
				"INSERT INTO thread_items VALUES (?, ?, ?, ?, 'subAgentActivity', 2)",
				(parent or self.session, "parent-turn", "spawn", json.dumps(spawn)),
			)
		return path

	def test_native_child_shell_uses_parent_session_and_host_proven_child_identity(self) -> None:
		self.child_history()
		state = self.base / "state"
		self.assertEqual(attribute(self.payload, state_dir=state)["status"], "pending")
		(self.root / "a.py").write_bytes(b"VALUE = 1\n")
		result = attribute({**self.payload, "hook_event_name": "PostToolUse"}, state_dir=state)
		self.assertEqual(result["status"], "attributed")
		self.assertEqual([item["path"] for item in result["files"]], ["a.py"])
		self.assertTrue(
			shell_snapshot_finished(
				{**self.payload, "root": str(self.root)},
				shell_event_key(self.payload),
				terminal_turn=True,
			)
		)

	def test_foreign_or_unproven_child_cannot_supply_completion(self) -> None:
		for kind, parent in (("completed", self.session), ("started", "foreign-parent")):
			with self.subTest(kind=kind, parent=parent):
				self.child_history(kind=kind, parent=parent)
				self.assertIsNone(shell_exit_code(self.payload))

	def test_effective_event_cwd_covers_external_project_without_workdir_field(self) -> None:
		external = self.base / "external-project"
		external.mkdir()
		(external / ".git").mkdir()
		untouched = self.root / "untouched.py"
		untouched.write_bytes(b"VALUE = 1\n")
		payload = {**self.payload, "cwd": str(external)}
		self.assertNotIn("workdir", payload["tool_input"])
		state = self.base / "state"
		for value in (1, 2):
			with self.subTest(value=value):
				payload["tool_use_id"] = f"exec-external-{value}"
				self.payload["tool_use_id"] = payload["tool_use_id"]
				self.assertEqual(attribute(payload, state_dir=state)["status"], "pending")
				content = f"VALUE = {value}\n".encode()
				(external / "a.py").write_bytes(content)
				self.history(cwd=str(external))
				result = attribute({**payload, "hook_event_name": "PostToolUse"}, state_dir=state)
				self.assertEqual(result["status"], "attributed", result)
				self.assertEqual(len(result["files"]), 1)
				self.assertEqual(result["files"][0]["root"], str(external))
				self.assertEqual(result["files"][0]["path"], "a.py")
				self.assertEqual(result["files"][0]["sha256"], hashlib.sha256(content).hexdigest())
				self.assertTrue(result["write_allowed"])
		self.assertEqual(untouched.read_bytes(), b"VALUE = 1\n")

	def test_native_child_terminal_missing_call_is_retained_as_unconfirmed(self) -> None:
		path = self.child_history()
		with closing(sqlite3.connect(path)) as database, database:
			database.execute("DELETE FROM thread_items WHERE item_type='commandExecution'")
		self.assertTrue(shell_snapshot_unrecorded(self.payload, shell_event_key(self.payload)))
		with closing(sqlite3.connect(path)) as database, database:
			database.execute("UPDATE thread_turns SET status='in_progress'")
		self.assertFalse(shell_snapshot_unrecorded(self.payload, shell_event_key(self.payload)))

	def test_native_child_cycles_are_bounded_and_do_not_replace_exact_call_matching(self) -> None:
		path = self.child_history()
		with closing(sqlite3.connect(path)) as database, database:
			cycle = {"type": "subAgentActivity", "kind": "started", "agentThreadId": self.session}
			database.execute(
				"INSERT INTO thread_items VALUES (?, ?, ?, ?, 'subAgentActivity', 3)",
				("33333333-3333-3333-3333-333333333333", self.turn, "cycle", json.dumps(cycle)),
			)
		self.assertEqual(shell_exit_code(self.payload), 0)
		self.assertIsNone(shell_exit_code({**self.payload, "tool_use_id": "other"}))

	def test_host_history_accepts_completed_and_failed_commands_without_writes(self) -> None:
		for code in (0, 1):
			with self.subTest(code=code):
				path = self.history(code=code, status="completed" if code == 0 else "failed")
				before = path.read_bytes()
				self.assertEqual(shell_exit_code(self.payload), code)
				self.assertEqual(path.read_bytes(), before)
				self.assertFalse(path.with_name(path.name + "-journal").exists())

	def test_host_history_rejects_other_calls_turns_sessions_and_directories(self) -> None:
		self.history()
		for changed in (
			{"session_id": "other"},
			{"turn_id": "other"},
			{"tool_use_id": "other"},
			{"tool_input": {"workdir": str(self.root / "elsewhere")}},
		):
			with self.subTest(changed=changed):
				self.assertIsNone(shell_exit_code({**self.payload, **changed}))

	def test_host_history_rejects_nonterminal_or_invalid_completion(self) -> None:
		for status, code, changed in (
			("inProgress", None, {}),
			("inProgress", 0, {}),
			("completed", True, {}),
			("completed", "0", {}),
			("completed", None, {}),
			("completed", 0, {"type": "other"}),
			("completed", 0, {"id": "other"}),
			("completed", 0, {"cwd": str(self.base)}),
		):
			with self.subTest(status=status, code=code, changed=changed):
				self.history(status=status, code=code, **changed)
				self.assertIsNone(shell_exit_code(self.payload))

	def test_host_history_missing_corrupt_unknown_or_expired_stays_unchecked(self) -> None:
		path = self.host / "thread_history_1.sqlite"
		self.assertIsNone(shell_exit_code(self.payload))
		self.assertFalse(path.exists())
		path.write_bytes(b"not a database")
		self.assertIsNone(shell_exit_code(self.payload))
		path.unlink()
		with closing(sqlite3.connect(path)) as database, database:
			database.execute("CREATE TABLE unknown (value TEXT)")
		self.assertIsNone(shell_exit_code(self.payload))
		self.history()
		self.assertIsNone(shell_exit_code(self.payload, deadline=0.0))

	def test_project_owned_history_cannot_confirm_completion(self) -> None:
		self.host = self.root / "host"
		self.host.mkdir()
		self.history()
		with patch.dict(os.environ, {"CODEX_HOME": str(self.host)}):
			self.assertIsNone(shell_exit_code(self.payload))

	def test_hardlinked_host_history_cannot_confirm_completion(self) -> None:
		path = self.history()
		alias = self.base / "history-alias.sqlite"
		alias.hardlink_to(path)
		self.assertIsNone(shell_exit_code(self.payload))

	@unittest.skipUnless(os.name == "nt", "Windows short-path ownership regression")
	def test_project_owned_history_short_alias_cannot_confirm_completion(self) -> None:
		import ctypes
		from ctypes import wintypes

		self.host = self.root / "project-owned-host-history"
		self.host.mkdir()
		self.history()
		get_short = ctypes.windll.kernel32.GetShortPathNameW
		get_short.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
		get_short.restype = wintypes.DWORD
		buffer = ctypes.create_unicode_buffer(32768)
		self.assertGreater(get_short(str(self.host), buffer, len(buffer)), 0)
		if buffer.value.casefold() == str(self.host).casefold():
			self.skipTest("Volume does not generate short aliases")
		with patch.dict(os.environ, {"CODEX_HOME": buffer.value}):
			self.assertIsNone(shell_exit_code(self.payload))

	def test_next_pre_recovers_legacy_capture_only_after_command_and_turn_finish(self) -> None:
		self.history(code=1, status="failed")
		state = self.base / "state"
		group = state / hashlib.sha256(str(self.root).encode()).hexdigest()
		group.mkdir(parents=True)
		owned = group / (shell_event_key(self.payload) + ".json")
		owned.write_text(
			json.dumps({"root": str(self.root), "session_id": self.session, "before": {}}),
			encoding="utf-8",
		)
		(group / ".conflict").touch()
		retry = {**self.payload, "turn_id": "next-turn", "tool_use_id": "next-call"}
		self.assertEqual(attribute(retry, state_dir=state)["status"], "pending")
		self.assertFalse(owned.exists())
		self.assertFalse((group / ".conflict").exists())
		(self.root / "a.py").write_bytes(b"VALUE = 1\n")
		result = attribute(
			{**retry, "hook_event_name": "PostToolUse", "tool_response": {"exit_code": 0}},
			state_dir=state,
		)
		self.assertEqual(result["status"], "attributed")

	def test_terminal_events_preserve_running_commands(self) -> None:
		self.history(status="inProgress", code=None)
		state = self.base / "state"
		self.assertEqual(attribute(self.payload, state_dir=state)["status"], "pending")
		for event in ("Stop", "Interrupt"):
			with self.subTest(event=event):
				coordinate(
					{**self.payload, "hook_event_name": event},
					settings_path=self.host / "settings.json",
					state_dir=state,
				)
				retry = {**self.payload, "tool_use_id": f"another-call-{event}"}
				self.assertEqual(attribute(retry, state_dir=state)["status"], "pending")

	def test_failed_launch_in_other_cwd_does_not_pin_the_project(self) -> None:
		self.history(status="inProgress", code=None, turn_status="in_progress")
		state = self.base / "state"
		self.assertEqual(attribute(self.payload, state_dir=state)["status"], "pending")
		retry = {**self.payload, "turn_id": "next-turn", "tool_use_id": "next-call"}
		self.assertFalse(
			shell_snapshot_finished(
				{"root": str(self.root), "session_id": self.session, "turn_id": self.turn},
				shell_event_key(self.payload),
				terminal_turn=True,
			)
		)
		self.history(code=-1, status="failed", cwd=str(self.base / "missing-control-directory"))
		self.assertEqual(attribute(retry, state_dir=state)["status"], "pending")
		(self.root / "a.py").write_bytes(b"VALUE = 1\n")
		result = attribute(
			{**retry, "hook_event_name": "PostToolUse", "tool_response": {"exit_code": 0}},
			state_dir=state,
		)
		self.assertEqual(result["status"], "attributed")

	def test_retiring_another_cwd_never_grants_mutation_coverage(self) -> None:
		self.history(cwd=str(self.base / "other-project"))
		state = self.base / "state"
		self.assertEqual(attribute(self.payload, state_dir=state)["status"], "pending")
		(self.root / "a.py").write_bytes(b"VALUE = 1\n")
		result = attribute({**self.payload, "hook_event_name": "PostToolUse"}, state_dir=state)
		self.assertEqual(
			(result["status"], result["reason"]), ("unchecked", "mutation_not_confirmed")
		)
		retry = {**self.payload, "tool_use_id": "next-call"}
		self.assertEqual(attribute(retry, state_dir=state)["status"], "pending")

	def test_failed_launch_recovers_in_the_same_active_turn(self) -> None:
		self.history(status="inProgress", code=None, turn_status="in_progress")
		state = self.base / "state"
		self.assertEqual(attribute(self.payload, state_dir=state)["status"], "pending")
		retry = {**self.payload, "tool_use_id": "next-call"}
		self.assertFalse(
			shell_snapshot_finished(
				{"root": str(self.root), "session_id": self.session, "turn_id": self.turn},
				shell_event_key(self.payload),
				terminal_turn=True,
			)
		)
		self.history(
			code=-1,
			status="failed",
			turn_status="in_progress",
			processId=None,
			cwd=str(self.base / "missing-directory"),
		)
		self.assertEqual(attribute(retry, state_dir=state)["status"], "pending")
		(self.root / "a.py").write_bytes(b"VALUE = 1\n")
		result = attribute(
			{**retry, "hook_event_name": "PostToolUse", "tool_response": {"exit_code": 0}},
			state_dir=state,
		)
		self.assertEqual(result["status"], "attributed")
		self.assertEqual(list(state.rglob("*.json")), [])
		self.assertEqual(list(state.rglob(".conflict")), [])

	def test_active_turn_recovery_requires_explicit_failed_launch_without_session(self) -> None:
		cases: list[tuple[int, dict[str, object]]] = [
			(-1, {}),
			(-1, {"processId": "29542"}),
			(-1, {"processId": ""}),
			(-1, {"processId": False}),
			(-1, {"processId": 0}),
			(0, {"processId": None}),
		]
		for index, (code, overrides) in enumerate(cases):
			with self.subTest(code=code, overrides=overrides):
				self.history(
					code=code,
					status="completed" if code == 0 else "failed",
					turn_status="in_progress",
					**overrides,
				)
				state = self.base / f"state-{index}"
				self.assertEqual(attribute(self.payload, state_dir=state)["status"], "pending")
				retry = {**self.payload, "tool_use_id": "next-call"}
				self.assertEqual(attribute(retry, state_dir=state)["status"], "pending")

	def test_terminal_events_clear_confirmed_commands_and_allow_retry(self) -> None:
		self.history(turn_status="in_progress")
		for event in ("Stop", "Interrupt"):
			with self.subTest(event=event):
				state = self.base / event
				self.assertEqual(attribute(self.payload, state_dir=state)["status"], "pending")
				coordinate(
					{**self.payload, "hook_event_name": event},
					settings_path=self.host / "settings.json",
					state_dir=state,
				)
				retry = {**self.payload, "tool_use_id": "next-call"}
				self.assertEqual(attribute(retry, state_dir=state)["status"], "pending")

	def test_unrecorded_call_in_terminal_turn_is_retained_without_pinning_fresh_edits(self) -> None:
		path = self.history(turn_status="completed")
		state = self.base / "state"
		self.assertEqual(attribute(self.payload, state_dir=state)["status"], "pending")
		group = state / hashlib.sha256(str(self.root).encode()).hexdigest()
		owned = group / (shell_event_key(self.payload) + ".json")
		snapshot = json.loads(owned.read_bytes())
		with closing(sqlite3.connect(path)) as database, database:
			database.execute("DELETE FROM thread_items")
		(self.root / "earlier.py").write_bytes(b"VALUE = 0\n")
		(group / ".conflict").touch()
		retry = {**self.payload, "turn_id": "next-turn", "tool_use_id": "next-call"}
		with patch(
			"_shared.quality_coordinator.load_settings",
			return_value={
				"current": {
					"enabled": ["integrity"],
					"budget_seconds": 10.0,
				}
			},
		):
			feedback = coordinate(retry, settings_path=self.host / "settings.json", state_dir=state)
		self.assertIn("earlier edits remain unchecked", json.dumps(feedback))
		self.assertNotIn('"decision": "block"', json.dumps(feedback))
		self.assertFalse(owned.exists())
		self.assertFalse((group / ".conflict").exists())
		archive = state / "unconfirmed" / group.name / owned.name
		self.assertEqual(
			json.loads(archive.read_bytes()),
			{"reason": "abandoned_shell_call", "snapshot": snapshot},
		)
		(self.root / "a.py").write_bytes(b"VALUE = 1\n")
		result = attribute(
			{**retry, "hook_event_name": "PostToolUse", "tool_response": {"exit_code": 0}},
			state_dir=state,
		)
		self.assertEqual(result["status"], "attributed")
		self.assertTrue(archive.exists())
		next_call = {**retry, "tool_use_id": "third-call"}
		self.assertEqual(attribute(next_call, state_dir=state)["warnings"], [])

	def test_unrecorded_call_without_observed_changes_does_not_warn(self) -> None:
		path = self.history(turn_status="completed")
		state = self.base / "state"
		self.assertEqual(attribute(self.payload, state_dir=state)["status"], "pending")
		with closing(sqlite3.connect(path)) as database, database:
			database.execute("DELETE FROM thread_items")
		retry = {**self.payload, "turn_id": "next-turn", "tool_use_id": "next-call"}
		result = attribute(retry, state_dir=state)
		self.assertEqual(result["warnings"], [])
		self.assertEqual(len(list((state / "unconfirmed").rglob("*.json"))), 1)
		(self.root / "new.py").write_bytes(b"VALUE = 1\n")
		completed = attribute(
			{**retry, "hook_event_name": "PostToolUse", "tool_response": {"exit_code": 0}},
			state_dir=state,
		)
		self.assertEqual([item["path"] for item in completed["files"]], ["new.py"])

	def test_unrecorded_call_requires_terminal_turn_and_exact_snapshot_identity(self) -> None:
		state = self.base / "state"
		self.assertEqual(attribute(self.payload, state_dir=state)["status"], "pending")
		group = state / hashlib.sha256(str(self.root).encode()).hexdigest()
		key = shell_event_key(self.payload)
		snapshot = json.loads((group / (key + ".json")).read_bytes())
		for turn_status in ("in_progress", "unknown", "completed"):
			with self.subTest(turn_status=turn_status):
				path = self.history(turn_status=turn_status)
				self.assertFalse(shell_snapshot_unrecorded(snapshot, key))
				with closing(sqlite3.connect(path)) as database, database:
					database.execute("DELETE FROM thread_items")
				self.assertEqual(
					shell_snapshot_unrecorded(snapshot, key), turn_status == "completed"
				)
		self.assertFalse(shell_snapshot_unrecorded(snapshot, "other-key"))
		self.assertFalse(shell_snapshot_unrecorded({**snapshot, "turn_id": "missing"}, key))
		self.assertFalse(shell_snapshot_unrecorded(snapshot, key, deadline=0.0))
		path.unlink()
		self.assertFalse(shell_snapshot_unrecorded(snapshot, key))
		self.assertFalse(path.exists())

	def test_finished_command_in_active_turn_is_not_reclaimed_by_new_pre(self) -> None:
		self.history(turn_status="in_progress")
		state = self.base / "state"
		self.assertEqual(attribute(self.payload, state_dir=state)["status"], "pending")
		retry = {**self.payload, "tool_use_id": "another-call"}
		self.assertEqual(attribute(retry, state_dir=state)["status"], "pending")
		self.assertEqual(len(list(state.glob("*/*.json"))), 2)

	def invoke(self, records: list[dict]) -> dict:
		self.transcript.write_text(
			"".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
		)
		self.payload["hook_event_name"] = "PreToolUse"
		before = attribute(self.payload, state_dir=self.base / "state")
		self.assertEqual(before["status"], "pending", before)
		(self.root / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
		self.payload["hook_event_name"] = "PostToolUse"
		return attribute(self.payload, state_dir=self.base / "state")

	def test_trusted_completed_call_confirms_changed_content(self) -> None:
		result = self.invoke([self.record()])
		self.assertEqual(result["status"], "attributed", result)
		self.assertEqual(result["files"][0]["path"], "a.py")
		self.assertNotIn("source must not", json.dumps(result))

	@unittest.skipUnless(os.name == "nt", "Windows extended path notation")
	def test_windows_extended_transcript_path_preserves_host_ownership(self) -> None:
		self.payload["transcript_path"] = "\\\\?\\" + str(self.transcript)
		result = self.invoke([self.record()])
		self.assertEqual(result["status"], "attributed", result)

	def test_actual_failure_cannot_be_overridden_by_success_in_stdout(self) -> None:
		self.payload["tool_response"] = "Exit code: 0\nWall time: 0 seconds\nOutput:\n"
		result = self.invoke([self.record(7)])
		self.assertEqual(result["status"], "unchecked", result)
		self.assertEqual(result["reason"], "mutation_failed")
		self.assertEqual(result["files"][0]["path"], "a.py")

	def nested_cwd_result(self) -> dict:
		nested = self.root / "nested"
		nested.mkdir()
		record = self.record()
		record["payload"]["item"]["cwd"] = nested.as_uri()
		return self.invoke([record])

	def test_nested_cwd_inside_the_observed_project_preserves_coverage(self) -> None:
		result = self.nested_cwd_result()
		self.assertEqual(result["status"], "attributed", result)
		self.assertEqual(result["files"][0]["root"], str(self.root.resolve()))

	def test_nested_cwd_inside_an_unmanaged_tree_preserves_coverage(self) -> None:
		# Gate places its default temp inside Git; this case needs a genuinely unmarked tree.
		storage = Path(os.environ.get("PYTHON_HOOK_TEST_STORAGE", str(Path.home())))
		if any(
			(parent / marker).exists()
			for parent in (storage, *storage.parents)
			for marker in (".git", "quality-gate.toml")
		):
			self.skipTest(
				"Isolated profile has an enclosing project; genuine unmanaged storage unavailable"
			)
		with tempfile.TemporaryDirectory(prefix="codex-hook-unmanaged-", dir=storage) as directory:
			self.base = Path(directory)
			self.root = self.base / "project"
			self.root.mkdir()
			self.payload["cwd"] = str(self.root)
			result = self.nested_cwd_result()
			self.assertEqual(result["status"], "attributed", result)
			self.assertEqual(result["files"][0]["root"], str(self.root.resolve()))

	def test_nested_project_or_changed_explicit_workdir_remains_unchecked(self) -> None:
		nested = self.root / "nested"
		nested.mkdir()
		(nested / ".git").mkdir()
		record = self.record()
		record["payload"]["item"]["cwd"] = nested.as_uri()
		result = self.invoke([record])
		self.assertEqual(result["status"], "unchecked", result)
		self.assertEqual(result["reason"], "mutation_not_confirmed")
		self.payload["tool_input"]["workdir"] = str(nested)
		result = self.invoke([self.record()])
		self.assertEqual(result["status"], "unchecked", result)
		self.assertEqual(result["reason"], "mutation_not_confirmed")

	def test_mismatched_or_incomplete_records_cannot_confirm_a_call(self) -> None:
		for field, value in (
			("thread_id", "other-session"),
			("turn_id", "other-turn"),
			("id", "other-call"),
			("cwd", self.base.as_uri()),
			("exit_code", True),
			("exit_code", None),
			("status", "inProgress"),
			("status", "failed"),
			("type", "OtherTool"),
		):
			with self.subTest(field=field, value=value):
				record = self.record()
				target = (
					record["payload"]
					if field in {"thread_id", "turn_id"}
					else record["payload"]["item"]
				)
				target[field] = value
				result = self.invoke([record])
				self.assertEqual(result["status"], "unchecked", result)
				self.assertEqual(result["reason"], "mutation_not_confirmed")

	def test_missing_or_duplicate_completion_stays_unchecked(self) -> None:
		for records in ([], [self.record(), self.record()]):
			with self.subTest(count=len(records)):
				result = self.invoke(records)
				self.assertEqual(result["status"], "unchecked", result)
				self.assertEqual(result["reason"], "mutation_not_confirmed")

	def test_project_owned_transcript_cannot_claim_success(self) -> None:
		self.transcript = self.root / f"rollout-test-{self.session}.jsonl"
		self.payload["transcript_path"] = str(self.transcript)
		result = self.invoke([self.record()])
		self.assertEqual(result["status"], "unchecked", result)
		self.assertEqual(result["reason"], "mutation_not_confirmed")

	def test_transcript_filename_must_identify_the_current_session(self) -> None:
		self.transcript = self.sessions / "rollout-test-other-session.jsonl"
		self.payload["transcript_path"] = str(self.transcript)
		result = self.invoke([self.record()])
		self.assertEqual(result["status"], "unchecked", result)

	def test_transcript_tail_is_bounded_and_does_not_reuse_an_old_record(self) -> None:
		current = (json.dumps(self.record()) + "\n").encode("utf-8")
		padding = b"x" * (2 * 1024 * 1024) + b"\n"
		self.transcript.write_bytes(current + padding)
		self.assertIsNone(shell_exit_code(self.payload))
		self.transcript.write_bytes(padding + current)
		self.assertEqual(shell_exit_code(self.payload), 0)

	def test_expired_deadline_and_malformed_or_unavailable_storage_stay_unchecked(self) -> None:
		self.transcript.write_text(json.dumps(self.record()) + "\n", encoding="utf-8")
		self.assertIsNone(shell_exit_code(self.payload, deadline=0.0))
		self.transcript.write_bytes(b"not JSON\n{\n")
		self.assertIsNone(shell_exit_code(self.payload))
		self.transcript.unlink()
		self.assertIsNone(shell_exit_code(self.payload))

	def test_linked_transcript_cannot_supply_completion_metadata(self) -> None:
		external = self.base / "external.jsonl"
		external.write_text(json.dumps(self.record()) + "\n", encoding="utf-8")
		try:
			self.transcript.symlink_to(external)
		except OSError:
			self.skipTest("OS account cannot create symbolic links")
		self.assertIsNone(shell_exit_code(self.payload))


if __name__ == "__main__":
	unittest.main()
