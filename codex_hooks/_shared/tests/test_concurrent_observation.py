"""Concurrent calls retain independent coverage without owning ambiguous writes."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from time import monotonic, sleep
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _shared.quality_background import BackgroundSession
from _shared.quality_coordinator import check_group
from _shared.quality_events import attribute
from _shared.quality_storage import StorageBusyError, directory_transaction


class ConcurrentObservation(unittest.TestCase):
	def test_overlapping_sessions_complete_in_both_orders_without_writing(self) -> None:
		for order in ((0, 1), (1, 0)):
			with self.subTest(order=order), tempfile.TemporaryDirectory() as directory:
				base = Path(directory).resolve()
				root, state = base / "project", base / "state"
				root.mkdir()
				(root / ".git").mkdir()
				payloads = [
					{
						"session_id": f"session-{i}",
						"turn_id": f"turn-{i}",
						"tool_use_id": f"call-{i}",
						"tool_name": "Bash",
						"cwd": str(root),
						"tool_input": {"command": "opaque", "workdir": str(root)},
					}
					for i in range(2)
				]
				with patch.dict(os.environ, {"CODEX_HOME": str(base / "host")}):
					for payload in payloads:
						result = attribute(
							{**payload, "hook_event_name": "PreToolUse"}, state_dir=state
						)
						self.assertEqual(result["status"], "pending", result)
					self.assertEqual(len(list(state.glob("*/*.json"))), 2)
					for index in order:
						(root / f"file{index}.py").write_bytes(b"VALUE: int = 1\n")
						result = attribute(
							{
								**payloads[index],
								"hook_event_name": "PostToolUse",
								"tool_response": {"exit_code": 0},
							},
							state_dir=state,
						)
						self.assertEqual(result["status"], "attributed", result)
						self.assertFalse(result["write_allowed"])
						self.assertIn(f"file{index}.py", {item["path"] for item in result["files"]})
					self.assertFalse(list(state.glob("*/*.json")))
					self.assertFalse(list(state.glob("*/.conflict")))
					self.assertTrue(
						all(
							(root / f"file{i}.py").read_bytes() == b"VALUE: int = 1\n"
							for i in range(2)
						)
					)

	def test_concurrent_scope_skips_writer_but_schedules_every_selected_reader(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory).resolve()
			content = b"VALUE=1\n"
			(root / "a.py").write_bytes(content)
			files = [
				{
					"path": "a.py",
					"sha256": hashlib.sha256(content).hexdigest(),
					"operation": "modified",
				}
			]
			settings = {"enabled": ["ruff", "secrets", "types"]}
			with (
				patch(
					"python_quality.check.check_files",
					side_effect=AssertionError("Ambiguous scope wrote files"),
				),
				patch(
					"_shared.quality_background.schedule", return_value={"status": "pending"}
				) as schedule,
			):
				reports = check_group(
					root,
					files,
					settings,
					monotonic() + 10,
					background=BackgroundSession(root / "settings.json", root / "state", "test"),
					write_allowed=False,
				)
			self.assertEqual(schedule.call_args.kwargs["checks"], ["ruff", "secrets", "types"])
			self.assertEqual((root / "a.py").read_bytes(), content)
			self.assertNotIn("unchecked", json.dumps(reports))


class StateLockBudgets(unittest.TestCase):
	@contextmanager
	def holder(self, state: Path, seconds: float) -> Iterator[subprocess.Popen[str]]:
		ready = state.parent / "ready"
		code = (
			"import sys,time;from pathlib import Path;sys.path.insert(0,"
			+ repr(str(Path(__file__).resolve().parents[2]))
			+ ");from _shared.quality_storage import directory_transaction\n"
			+ "with directory_transaction(Path("
			+ repr(str(state))
			+ ")):\n Path("
			+ repr(str(ready))
			+ ").write_text('ready')\n time.sleep("
			+ repr(seconds)
			+ ")\n"
		)
		process = subprocess.Popen(
			[sys.executable, "-B", "-c", code],
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
			text=True,
		)
		try:
			limit = monotonic() + 5
			while not ready.exists() and monotonic() < limit:
				self.assertIsNone(process.poll(), "Lock holder failed before readiness")
				sleep(0.005)
			self.assertTrue(ready.exists(), "Lock holder readiness deadline")
			yield process
		finally:
			try:
				process.wait(timeout=3)
			except subprocess.TimeoutExpired:
				process.kill()
				process.wait(timeout=3)
				raise
			self.assertEqual(process.returncode, 0)

	def test_shell_waits_for_live_transition_without_owning_overlapping_writes(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory).resolve()
			root, state = base / "project", base / "state"
			root.mkdir()
			(root / ".git").mkdir()
			payload = {
				"cwd": str(root),
				"session_id": "lock-test",
				"turn_id": "turn",
				"tool_use_id": "first",
				"tool_name": "Bash",
				"tool_input": {},
			}
			with patch.dict(os.environ, {"CODEX_HOME": str(base / "host")}):
				first = attribute({**payload, "hook_event_name": "PreToolUse"}, state_dir=state)
				self.assertEqual(first["status"], "pending", first)
				second = {**payload, "tool_use_id": "second"}
				with self.holder(state, 0.4):
					result = attribute(
						{**second, "hook_event_name": "PreToolUse"},
						state_dir=state,
						deadline=monotonic() + 2,
					)
				self.assertEqual(result["status"], "pending", result)
				self.assertEqual(len(list(state.glob("*/*.json"))), 2)
				for owner in (first, second):
					current = payload if owner is first else second
					post = attribute(
						{
							**current,
							"hook_event_name": "PostToolUse",
							"tool_response": {"exit_code": 0},
						},
						state_dir=state,
						deadline=monotonic() + 2,
					)
					self.assertEqual(post["status"], "attributed", post)
					self.assertFalse(post["write_allowed"])
				self.assertFalse(list(state.glob("*/*.json")))

	def test_short_explicit_and_default_budgets_preserve_live_holder(self) -> None:
		for budget in (None, 0.03):
			with self.subTest(budget=budget), tempfile.TemporaryDirectory() as directory:
				state = Path(directory).resolve() / "state"
				with self.holder(state, 0.6) as process:
					deadline = None if budget is None else monotonic() + budget
					with (
						self.assertRaises(StorageBusyError),
						directory_transaction(state, deadline=deadline),
					):
						self.fail("Acquired another live process's lock")
					self.assertIsNone(process.poll())
