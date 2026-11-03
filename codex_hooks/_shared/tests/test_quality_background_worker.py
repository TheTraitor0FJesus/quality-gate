"""A worker that cannot publish recovery must exit as incomplete."""

from __future__ import annotations

import sys
import tempfile
import unittest
import uuid
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _shared import quality_background_worker as worker


class BackgroundWorkerFailure(unittest.TestCase):
	def test_expired_publication_retains_deadline_and_owned_cancel_cleanup(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			cancel = base / "owned.cancel"
			cancel.touch()
			event = str(uuid.uuid4())
			job = {"request": {"event_id": event}, "status": "pending"}
			with (
				patch.object(worker, "work", side_effect=worker.FreshnessError("deadline")),
				patch.object(worker, "directory_transaction", return_value=nullcontext()),
				patch.object(worker.state, "read", return_value=job),
				patch.object(worker.state, "write") as write,
				patch.object(worker.state, "cancellation_path", return_value=cancel),
				patch("sys.argv", ["worker", str(base), event]),
			):
				self.assertEqual(worker.main(), 0)
			write.assert_called_once()
			self.assertEqual(write.call_args.args[1]["report"]["reason"], "deadline")
			self.assertFalse(cancel.exists())

	def test_types_process_deadline_and_cancellation_are_not_malformed_output(self) -> None:
		for reason in ("deadline", "cancelled", "process_unavailable"):
			with (
				self.subTest(reason=reason),
				patch.object(worker, "execute", side_effect=worker.ProcessError(reason)),
			):
				result = worker._analyze_types({}, "v4.6.0", 100, worker.threading.Event())
				self.assertIsNone(result["wire"])
				report = result["report"]
				assert isinstance(report, dict)
				self.assertEqual(report["status"], "unchecked")
				self.assertEqual(
					report["reason"],
					reason,
				)

	def test_composite_analysis_reuses_one_worker_and_cancellation_signal(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			root = base / "project"
			root.mkdir()
			source = root / "example.py"
			source.write_text("VALUE: int = 1\n", encoding="utf-8")
			document = {
				"request": {
					"root": str(root),
					"files": [{"path": source.name, "sha256": "a" * 64}],
					"baseline": {"policy_release": "v2.4.2"},
				},
				"settings": {
					"provider_release": "v3.2.0",
					"baseline_release": "v2.4.2",
					"enabled": ["ruff", "secrets", "types"],
				},
				"checks": ["ruff", "secrets", "types"],
			}
			calls: list[str] = []
			cancel = worker.threading.Event()

			def ruff(*_args: object, **kwargs: object) -> dict[str, object]:
				calls.append("ruff")
				self.assertIs(kwargs["cancel_event"], cancel)
				return {"check": "ruff", "status": "passed", "files": []}

			def secrets(*_args: object, **kwargs: object) -> dict[str, object]:
				calls.append("secrets")
				self.assertIs(kwargs["cancel_event"], cancel)
				return {"check": "secrets", "status": "passed", "files": []}

			def builtin(*_args: object, **kwargs: object) -> dict[str, object]:
				calls.append("builtin")
				self.assertIs(kwargs["cancel_event"], cancel)
				return {"check": "builtin_secrets", "status": "passed", "files": []}

			def type_process(*_args: object, **kwargs: object) -> SimpleNamespace:
				calls.append("types")
				self.assertIs(kwargs["cancel_event"], cancel)
				return SimpleNamespace(stdout="{}", returncode=0, stderr="")

			with (
				patch("python_quality.check.check_files", side_effect=ruff),
				patch("secret_scan.check.check_files", side_effect=secrets),
				patch.object(worker, "run_scanner", side_effect=builtin),
				patch.object(worker, "execute", side_effect=type_process),
				patch.object(worker, "release_inventory", return_value={"files": {}}),
				patch.object(worker, "digest", return_value="engine"),
				patch.object(
					worker,
					"validate_result",
					return_value={"check": "types", "status": "passed", "findings": []},
				),
				patch.object(worker, "envelope", return_value={"status": "passed"}),
			):
				_summary, wire, _engine = worker.analyze(document, 1000.0, cancel)

		self.assertEqual(calls, ["ruff", "secrets", "builtin", "types"])
		assert wire is not None
		self.assertEqual(list(wire["results"]), ["ruff", "secrets", "builtin_secrets", "types"])

	def test_secret_reader_is_capped_by_its_budget_and_shared_deadline(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			document = {
				"request": {"root": directory},
				"settings": {"enabled": ["secrets"], "budget_seconds": 5.0},
			}
			files = [{"path": "example.py", "sha256": "a" * 64}]
			cancel = worker.threading.Event()
			deadlines: list[float] = []

			def secrets(*args: object, **kwargs: object) -> dict[str, object]:
				deadline = args[3]
				assert isinstance(deadline, float)
				deadlines.append(deadline)
				self.assertIs(kwargs["cancel_event"], cancel)
				return {"check": "secrets", "status": "passed", "files": []}

			with (
				patch("secret_scan.check.check_files", side_effect=secrets),
				patch.object(
					worker,
					"run_scanner",
					return_value={"check": "builtin_secrets", "status": "passed", "files": []},
				),
				patch.object(worker, "monotonic", return_value=100.0),
			):
				worker._analyze_secrets(document, files, 1000.0, cancel)
				worker._analyze_secrets(document, files, 103.0, cancel)

		self.assertEqual(deadlines, [105.0, 103.0])

	def test_unpublished_recovery_returns_failure_and_still_cleans_cancel_signal(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			root = base / "project"
			root.mkdir()
			state_dir = base / "state"
			worker_directory = worker.state.owned_directory(state_dir, root)
			worker_directory.mkdir(parents=True)
			state_path = worker_directory / "latest.json"
			event_id = str(uuid.uuid4())
			cancel = base / "worker.cancel"
			cancel.touch()
			document = {
				"schema": 1,
				"request": {
					"event_id": event_id,
					"root": str(root.resolve()),
					"cancel_path": str(cancel),
				},
				"status": "pending",
				"wire": None,
				"report": None,
			}
			with patch.object(worker.state, "cancellation_path", return_value=cancel):
				worker.state.write(state_path, document)
			with (
				patch.object(worker, "work", side_effect=OSError("analysis failed")),
				patch.object(worker, "directory_transaction", return_value=nullcontext()),
				patch.object(worker.state, "cancellation_path", return_value=cancel),
				patch.object(
					worker.state, "write", side_effect=OSError("state write failed")
				) as write,
				patch("sys.argv", ["worker", str(worker_directory), event_id]),
			):
				result = worker.main()
				persisted = worker.state.read(state_path)
			self.assertEqual(result, 2)
			self.assertFalse(cancel.exists(), "Recovery failure must not skip owned cleanup")
			self.assertEqual(persisted["status"], "pending")
			self.assertEqual(write.call_count, 1)
