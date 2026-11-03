"""Bounded asynchronous execution through a controlled immutable CLI transport."""

from __future__ import annotations

import errno
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from time import monotonic
from typing import Protocol, cast
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _shared.quality_settings import load_settings, settings_operation
from _shared.tests.provider_fixture import native_launcher

from python_types.tests.provider_fixture import typing_project_fixture


class _WindowsFunction(Protocol):
	argtypes: list[object]
	restype: object

	def __call__(self, *args: object) -> object: ...


class _WindowsKernel32(Protocol):
	CreateFileW: _WindowsFunction
	CloseHandle: _WindowsFunction


@unittest.skipUnless(native_launcher().is_file(), "Verified release launcher required")
class TypingBackground(unittest.TestCase):
	def test_rapid_replacements_do_not_leave_cancel_signals(self) -> None:
		from _shared.quality_background import BackgroundSession, cancel_session, collect, schedule

		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			environment, settings, request, _ = typing_project_fixture(base)
			with patch.dict(os.environ, environment, clear=True):
				for _ in range(3):
					self.assertEqual(
						schedule(
							Path(request["root"]),
							request["files"],
							session=BackgroundSession(settings, base / "state", "rapid"),
							deadline=monotonic() + 5,
						)["status"],
						"pending",
					)
				self.assertEqual(
					[
						item["status"]
						for item in collect(
							session=BackgroundSession(settings, base / "state", "rapid"),
							deadline=monotonic() + 5,
							drain=True,
							include_clean=True,
						)
					],
					["passed"],
				)
				self.assertEqual(
					cancel_session(
						session=BackgroundSession(settings, base / "state", "rapid"),
						deadline=monotonic() + 5,
					),
					[],
				)
				events = Path(environment["LOCALAPPDATA"]) / "quality-gate/edit-types-events"
				self.assertEqual(list(events.glob("*.cancel")), [])

	def test_cancel_after_completion_removes_its_signal(self) -> None:
		from _shared.quality_background import BackgroundSession, cancel_session, collect, schedule

		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			environment, settings, request, _ = typing_project_fixture(base)
			with patch.dict(os.environ, environment, clear=True):
				schedule(
					Path(request["root"]),
					request["files"],
					session=BackgroundSession(settings, base / "state", "completed"),
					deadline=monotonic() + 5,
				)
				self.assertEqual(
					[
						item["status"]
						for item in collect(
							session=BackgroundSession(settings, base / "state", "completed"),
							deadline=monotonic() + 5,
							drain=True,
							include_clean=True,
						)
					],
					["passed"],
				)
				self.assertEqual(
					cancel_session(
						session=BackgroundSession(settings, base / "state", "completed"),
						deadline=monotonic() + 5,
					),
					[],
				)
				events = Path(environment["LOCALAPPDATA"]) / "quality-gate/edit-types-events"
				self.assertEqual(list(events.glob("*.cancel")), [])

	def test_cached_pass_without_provider_evidence_cannot_certify_completion(self) -> None:
		from _shared.quality_background import BackgroundSession, cancel_session, collect, schedule

		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			environment, settings, request, _ = typing_project_fixture(base)
			marker, release = base / "request.json", base / "release"
			with patch.dict(
				os.environ,
				{
					**environment,
					"TYPING_FIXTURE_MARKER": str(marker),
					"TYPING_FIXTURE_WAIT": str(release),
				},
				clear=True,
			):
				schedule(
					Path(request["root"]),
					request["files"],
					session=BackgroundSession(settings, base / "state", "forged-pass"),
					deadline=monotonic() + 5,
				)
				self.wait_marker(marker)
				path = next((base / "state/background/roots").glob("*/latest.json"))
				document = json.loads(path.read_bytes())
				document.update(
					status="completed", wire=None, report={"status": "passed", "findings": []}
				)
				path.write_text(json.dumps(document), encoding="utf-8")
				try:
					reports = collect(
						session=BackgroundSession(settings, base / "state", "forged-pass"),
						deadline=monotonic() + 3,
						drain=True,
						include_clean=True,
					)
					self.assertEqual([item["status"] for item in reports], ["unchecked"])
				finally:
					# Restore the genuine pending state before exercising owned cleanup.
					document.update(status="pending", wire=None, report=None)
					path.write_text(json.dumps(document), encoding="utf-8")
					release.touch()
					cancel_session(
						session=BackgroundSession(settings, base / "state", "forged-pass"),
						deadline=monotonic() + 5,
					)

	def test_corrupt_event_state_cannot_create_an_unowned_cancellation_file(self) -> None:
		from _shared.quality_background import BackgroundSession, cancel_session, schedule

		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			environment, settings, request, _ = typing_project_fixture(base)
			marker, release = base / "request.json", base / "release"
			foreign = base / "unowned.cancel"
			with patch.dict(
				os.environ,
				{
					**environment,
					"TYPING_FIXTURE_MARKER": str(marker),
					"TYPING_FIXTURE_WAIT": str(release),
				},
				clear=True,
			):
				schedule(
					Path(request["root"]),
					request["files"],
					session=BackgroundSession(settings, base / "state", "corrupt"),
					deadline=monotonic() + 5,
				)
				self.wait_marker(marker)
				path = next((base / "state/background/roots").glob("*/latest.json"))
				original = path.read_bytes()
				document = json.loads(original)
				document["request"]["cancel_path"] = str(foreign)
				path.write_text(json.dumps(document), encoding="utf-8")
				try:
					reports = cancel_session(
						session=BackgroundSession(settings, base / "state", "corrupt"),
						deadline=monotonic() + 0.3,
					)
					self.assertEqual([item["status"] for item in reports], ["unchecked"])
					self.assertFalse(
						foreign.exists(), "Corrupt cache metadata must not grant write ownership"
					)
				finally:
					path.write_bytes(original)
					release.touch()
					cancel_session(
						session=BackgroundSession(settings, base / "state", "corrupt"),
						deadline=monotonic() + 5,
					)

	def wait_for_completed_state(self, latest: Path) -> None:
		limit = monotonic() + 10
		while monotonic() < limit:
			try:
				if json.loads(latest.read_text(encoding="utf-8"))["status"] == "completed":
					return
			except (OSError, ValueError, KeyError):
				pass
			threading.Event().wait(0.01)
		self.fail("The controlled background job did not complete")

	def exclusive_windows_read_handle(self, latest: Path) -> tuple[_WindowsKernel32, int]:
		import ctypes
		from ctypes import wintypes

		kernel32 = cast(
			_WindowsKernel32,
			ctypes.WinDLL("kernel32", use_last_error=True),  # type: ignore[attr-defined]
		)
		kernel32.CreateFileW.argtypes = [
			wintypes.LPCWSTR,
			wintypes.DWORD,
			wintypes.DWORD,
			wintypes.LPVOID,
			wintypes.DWORD,
			wintypes.DWORD,
			wintypes.HANDLE,
		]
		kernel32.CreateFileW.restype = wintypes.HANDLE
		kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
		kernel32.CloseHandle.restype = wintypes.BOOL
		handle = cast(int, kernel32.CreateFileW(str(latest), 0x80000000, 0, None, 3, 0x80, None))
		if ctypes.c_void_p(handle).value == ctypes.c_void_p(-1).value:
			raise ctypes.WinError(  # type: ignore[attr-defined]
				ctypes.get_last_error()  # type: ignore[attr-defined]
			)
		return kernel32, handle

	def collect_after_denied_read(
		self,
		latest: Path,
		handle: int,
		kernel32: _WindowsKernel32,
		settings: Path,
		state_dir: Path,
		session_id: str,
	) -> tuple[list[dict[str, object]], list[OSError], bool]:
		from _shared.quality_background import BackgroundSession, collect

		observed: list[OSError] = []
		released = False
		original_open = Path.open

		def release_after_denied_read(
			path: Path,
			mode: str = "r",
			buffering: int = -1,
			encoding: str | None = None,
			errors: str | None = None,
			newline: str | None = None,
		) -> object:
			nonlocal released
			try:
				return original_open(path, mode, buffering, encoding, errors, newline)
			except OSError as error:
				if path == latest and not released:
					observed.append(error)
					released = True
					kernel32.CloseHandle(handle)
				raise

		with patch.object(Path, "open", release_after_denied_read):
			reports = cast(
				list[dict[str, object]],
				collect(
					session=BackgroundSession(settings, state_dir, session_id),
					deadline=monotonic() + 5,
					drain=True,
					include_clean=True,
				),
			)
		return reports, observed, released

	@unittest.skipUnless(os.name == "nt", "Windows sharing semantics")
	def test_collector_retries_transient_latest_state_sharing_violation(self) -> None:
		from _shared.quality_background import BackgroundSession, cancel_session, schedule

		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			environment, settings, request, _ = typing_project_fixture(base)
			state_dir = base / "state"
			session_id = "read-sharing-retry"
			with patch.dict(os.environ, environment, clear=True):
				schedule(
					Path(request["root"]),
					request["files"],
					session=BackgroundSession(settings, state_dir, session_id),
					deadline=monotonic() + 5,
				)
				latest = next((state_dir / "background/roots").glob("*/latest.json"))
				self.wait_for_completed_state(latest)
				kernel32, handle = self.exclusive_windows_read_handle(latest)
				try:
					reports, observed, released = self.collect_after_denied_read(
						latest, handle, kernel32, settings, state_dir, session_id
					)
				finally:
					if not released:
						kernel32.CloseHandle(handle)
					cancel_session(
						session=BackgroundSession(settings, state_dir, session_id),
						deadline=monotonic() + 5,
					)

				self.assertEqual(len(observed), 1)
				self.assertIsInstance(observed[0], PermissionError)
				self.assertEqual(observed[0].errno, errno.EACCES)
				self.assertEqual(
					[(report["status"], report.get("reason")) for report in reports],
					[("passed", None)],
				)

	@unittest.skipUnless(os.name == "nt", "Windows sharing semantics")
	def test_permanent_state_read_denial_stays_incomplete_after_bounded_retry(self) -> None:
		from _shared import quality_background_state as state

		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "metadata.json"
			path.write_text('{"schema": 1}', encoding="utf-8")
			original_open = Path.open
			attempts = 0

			def deny_state_read(
				candidate: Path,
				mode: str = "r",
				buffering: int = -1,
				encoding: str | None = None,
				errors: str | None = None,
				newline: str | None = None,
			) -> object:
				nonlocal attempts
				if candidate == path:
					attempts += 1
					raise PermissionError(errno.EACCES, "sharing violation", str(path))
				return original_open(candidate, mode, buffering, encoding, errors, newline)

			started = monotonic()
			with patch.object(Path, "open", deny_state_read):
				with self.assertRaises(PermissionError):
					state.read(path)
			elapsed = monotonic() - started

			self.assertGreater(attempts, 1)
			self.assertLessEqual(
				attempts,
				int(state.STATE_READ_RETRY_SECONDS / state.POLL_SECONDS) + 2,
			)
			self.assertLess(elapsed, 2.0)

	def test_completed_reader_survives_a_concurrent_open_state_snapshot(self) -> None:
		from _shared.quality_background import BackgroundSession, collect, schedule

		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			environment, settings, request, _ = typing_project_fixture(base)
			marker, release, returned = base / "request.json", base / "release", base / "returned"
			with patch.dict(
				os.environ,
				{
					**environment,
					"TYPING_FIXTURE_MARKER": str(marker),
					"TYPING_FIXTURE_WAIT": str(release),
					"TYPING_FIXTURE_RETURNED": str(returned),
				},
				clear=True,
			):
				schedule(
					Path(request["root"]),
					request["files"],
					session=BackgroundSession(settings, base / "state", "open-state"),
					deadline=monotonic() + 5,
				)
				self.wait_marker(marker)
				path = next((base / "state/background/roots").glob("*/latest.json"))
				with path.open("rb"):
					release.touch()
					limit = monotonic() + 5
					while not returned.exists() and monotonic() < limit:
						threading.Event().wait(0.01)
					self.assertTrue(returned.exists())
					threading.Event().wait(0.3)
				self.assertEqual(
					collect(
						session=BackgroundSession(settings, base / "state", "open-state"),
						deadline=monotonic() + 3,
						drain=True,
						include_clean=True,
					)[0]["status"],
					"passed",
					self.state_diagnostic(base),
				)

	def test_session_shutdown_preserves_another_sessions_pending_analysis(self) -> None:
		from _shared.quality_background import BackgroundSession, cancel_session, collect, schedule

		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			environment, settings, request, _ = typing_project_fixture(base)
			marker, release = base / "request.json", base / "release"
			with patch.dict(
				os.environ,
				{
					**environment,
					"TYPING_FIXTURE_MARKER": str(marker),
					"TYPING_FIXTURE_WAIT": str(release),
				},
				clear=True,
			):
				first = None
				for session in ("leaving", "remaining"):
					self.assertEqual(
						schedule(
							Path(request["root"]),
							request["files"],
							session=BackgroundSession(settings, base / "state", session),
							deadline=monotonic() + 5,
						)["status"],
						"pending",
					)
					first = self.wait_marker(marker, first["event_id"] if first else None)
				self.assertEqual(
					cancel_session(
						session=BackgroundSession(settings, base / "state", "leaving"),
						deadline=monotonic() + 5,
					),
					[],
				)
				release.touch()
				remaining = collect(
					session=BackgroundSession(settings, base / "state", "remaining"),
					deadline=monotonic() + 10,
					drain=True,
					include_clean=True,
				)
				self.assertEqual([item["status"] for item in remaining], ["passed"])
				self.assertEqual(
					collect(
						session=BackgroundSession(settings, base / "state", "leaving"),
						deadline=monotonic() + 5,
						drain=True,
					),
					[],
				)

	def test_completed_reader_survives_temporary_metadata_lock_contention(self) -> None:
		from _shared.quality_background import BackgroundSession, collect, schedule
		from _shared.quality_storage import directory_transaction

		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			environment, settings, request, _ = typing_project_fixture(base)
			marker, release, returned = base / "request.json", base / "release", base / "returned"
			with patch.dict(
				os.environ,
				{
					**environment,
					"TYPING_FIXTURE_MARKER": str(marker),
					"TYPING_FIXTURE_WAIT": str(release),
					"TYPING_FIXTURE_RETURNED": str(returned),
				},
				clear=True,
			):
				schedule(
					Path(request["root"]),
					request["files"],
					session=BackgroundSession(settings, base / "state", "contention"),
					deadline=monotonic() + 5,
				)
				self.wait_marker(marker)
				job = next((base / "state/background/roots").iterdir())
				with directory_transaction(job):
					release.touch()
					limit = monotonic() + 5
					while not returned.exists() and monotonic() < limit:
						threading.Event().wait(0.01)
					self.assertTrue(
						returned.exists(), "The reader must return while metadata is locked"
					)
					threading.Event().wait(0.5)
				self.assertEqual(
					collect(
						session=BackgroundSession(settings, base / "state", "contention"),
						deadline=monotonic() + 5,
						drain=True,
					),
					[],
					self.state_diagnostic(base),
				)

	def test_engine_changed_during_analysis_cannot_certify_a_clean_result(self) -> None:
		from _shared.quality_background import BackgroundSession, collect, schedule

		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			environment, settings, request, wheel = typing_project_fixture(base)
			marker, release = base / "request.json", base / "release"
			with patch.dict(
				os.environ,
				{
					**environment,
					"TYPING_FIXTURE_MARKER": str(marker),
					"TYPING_FIXTURE_WAIT": str(release),
				},
				clear=True,
			):
				schedule(
					Path(request["root"]),
					request["files"],
					session=BackgroundSession(settings, base / "state", "engine-change"),
					deadline=monotonic() + 5,
				)
				self.wait_marker(marker)
				wheel.write_bytes(b"corrupted after the running engine loaded")
				release.touch()
				results = collect(
					session=BackgroundSession(settings, base / "state", "engine-change"),
					deadline=monotonic() + 10,
					drain=True,
				)
				self.assertTrue(
					results, "Changing the engine must leave verification explicitly incomplete"
				)
				self.assertEqual(results[0]["status"], "unchecked")
				self.assertEqual(results[0]["findings"], [])

	def test_stale_consumer_never_delivers_old_finding_and_retry_is_bounded(self) -> None:
		from _shared.quality_background import BackgroundSession, collect, schedule

		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			environment, settings, request, _ = typing_project_fixture(base)
			root = Path(request["root"])
			marker, release = base / "request.json", base / "release"
			with patch.dict(
				os.environ,
				{
					**environment,
					"TYPING_FIXTURE_MARKER": str(marker),
					"TYPING_FIXTURE_WAIT": str(release),
				},
				clear=True,
			):
				schedule(
					root,
					request["files"],
					session=BackgroundSession(settings, base / "state", "stale"),
					deadline=monotonic() + 5,
				)
				first = self.wait_marker(marker)
				(root / "consumer.py").write_bytes(b"VALUE: str = 'changed'\n")
				release.touch()
				results = collect(
					session=BackgroundSession(settings, base / "state", "stale"),
					deadline=monotonic() + 10,
					drain=True,
				)
				self.assertEqual(
					[(item["status"], item.get("reason")) for item in results],
					[("unchecked", "stale_result")],
					self.state_diagnostic(base),
				)
				self.assertEqual(results[0]["findings"], [])
				self.assertNotEqual(self.wait_marker(marker)["event_id"], first["event_id"])

	def test_shutdown_cancels_and_reaps_without_delivering_an_old_result(self) -> None:
		from _shared.quality_background import BackgroundSession, cancel_session, collect, schedule

		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			environment, settings, request, _ = typing_project_fixture(base)
			marker, release = base / "request.json", base / "release"
			with patch.dict(
				os.environ,
				{
					**environment,
					"TYPING_FIXTURE_MARKER": str(marker),
					"TYPING_FIXTURE_WAIT": str(release),
				},
				clear=True,
			):
				schedule(
					Path(request["root"]),
					request["files"],
					session=BackgroundSession(settings, base / "state", "shutdown"),
					deadline=monotonic() + 5,
				)
				self.wait_marker(marker)
				self.assertEqual(
					cancel_session(
						session=BackgroundSession(settings, base / "state", "shutdown"),
						deadline=monotonic() + 5,
					),
					[],
				)
				release.touch()
				self.assertEqual(
					collect(
						session=BackgroundSession(settings, base / "state", "shutdown"),
						deadline=monotonic() + 5,
						drain=True,
					),
					[],
				)

	def wait_marker(self, marker: Path, event_id: str | None = None) -> dict:
		limit = monotonic() + 5
		while monotonic() < limit:
			try:
				request = json.loads(marker.read_text(encoding="utf-8"))
				if event_id is None or request["event_id"] != event_id:
					return request
			except (OSError, ValueError):
				pass
			threading.Event().wait(0.01)
		self.fail("The controlled provider did not start the expected job")

	def state_diagnostic(self, base: Path) -> list[dict]:
		return [
			{
				key: document.get(key)
				for key in ("status", "failure_errno", "failure_winerror", "report")
			}
			for path in (base / "state/background/roots").glob("*/latest.json")
			for document in [json.loads(path.read_text(encoding="utf-8"))]
		]

	def test_new_edit_cancels_predecessor_and_keeps_both_files_and_deletion(self) -> None:
		from _shared.quality_background import BackgroundSession, collect, schedule

		from python_types.tests.result_fixture import file_hash

		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			environment, settings, request, _ = typing_project_fixture(base)
			root = Path(request["root"])
			marker, release = base / "request.json", base / "release"
			with patch.dict(
				os.environ,
				{
					**environment,
					"TYPING_FIXTURE_MARKER": str(marker),
					"TYPING_FIXTURE_WAIT": str(release),
				},
				clear=True,
			):
				self.assertEqual(
					schedule(
						root,
						request["files"],
						session=BackgroundSession(settings, base / "state", "replacement"),
						deadline=monotonic() + 5,
					)["status"],
					"pending",
				)
				first = self.wait_marker(marker)
				self.assertEqual(
					schedule(
						root,
						[{"path": "consumer.py", "sha256": file_hash(root / "consumer.py")}],
						session=BackgroundSession(settings, base / "state", "replacement"),
						deadline=monotonic() + 5,
					)["status"],
					"pending",
				)
				second = self.wait_marker(marker, first["event_id"])
				self.assertEqual(
					[item["path"] for item in second["files"]],
					["consumer.py", "deleted.py", "example.py"],
				)
				release.touch()
				self.assertEqual(
					collect(
						session=BackgroundSession(settings, base / "state", "replacement"),
						deadline=monotonic() + 10,
						drain=True,
					),
					[],
					self.state_diagnostic(base),
				)

	def test_disabling_pending_work_cancels_and_drains_without_findings(self) -> None:
		from _shared.quality_background import BackgroundSession, collect, schedule

		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			environment, settings, request, _ = typing_project_fixture(base)
			marker, release = base / "request.json", base / "release"
			with patch.dict(
				os.environ,
				{
					**environment,
					"TYPING_FIXTURE_MARKER": str(marker),
					"TYPING_FIXTURE_WAIT": str(release),
				},
				clear=True,
			):
				schedule(
					Path(request["root"]),
					request["files"],
					session=BackgroundSession(settings, base / "state", "disable"),
					deadline=monotonic() + 5,
				)
				self.wait_marker(marker)
				settings_operation(
					settings, "update", {**load_settings(settings)["current"], "enabled": []}
				)
				self.assertEqual(
					collect(
						session=BackgroundSession(settings, base / "state", "disable"),
						deadline=monotonic() + 5,
						drain=True,
					),
					[],
				)

	def test_edit_returns_while_reader_runs_and_drain_delivers_finding(self) -> None:
		from _shared.quality_background import BackgroundSession, collect, schedule

		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			environment, settings, request, _ = typing_project_fixture(base)
			root = Path(request["root"])
			marker, release = base / "request.json", base / "release"
			report_path = Path(environment["TYPING_FIXTURE_RESULT"])
			report = json.loads(report_path.read_text(encoding="utf-8"))
			report["status"] = report["components"][0]["status"] = "failed"
			report["components"][0]["diagnostics"] = [
				{"path": "consumer.py", "line": 1, "column": 1, "code": "assignment"}
			]
			report_path.write_text(json.dumps(report), encoding="utf-8")
			with patch.dict(
				os.environ,
				{
					**environment,
					"TYPING_FIXTURE_MARKER": str(marker),
					"TYPING_FIXTURE_WAIT": str(release),
				},
				clear=True,
			):
				pending = schedule(
					root,
					request["files"],
					session=BackgroundSession(settings, base / "state", "background-test"),
					deadline=monotonic() + 5,
				)
				self.assertEqual(pending["status"], "pending")
				limit = monotonic() + 5
				while not marker.exists() and monotonic() < limit:
					threading.Event().wait(0.01)
				self.assertTrue(marker.exists(), "The provider must start after schedule returns")
				self.assertEqual(
					collect(
						session=BackgroundSession(settings, base / "state", "background-test"),
						deadline=monotonic() + 2,
					),
					[],
				)
				release.touch()
				results = collect(
					session=BackgroundSession(settings, base / "state", "background-test"),
					deadline=monotonic() + 15,
					drain=True,
				)
				self.assertEqual(
					[(item["status"], item.get("reason")) for item in results],
					[("failed", None)],
					json.dumps(results),
				)
				self.assertEqual(
					results[0]["findings"],
					[{"path": "consumer.py", "line": 1, "column": 1, "rule": "assignment"}],
				)
				self.assertEqual(
					collect(
						session=BackgroundSession(settings, base / "state", "background-test"),
						deadline=monotonic() + 2,
						drain=True,
					),
					[],
				)


if __name__ == "__main__":
	unittest.main()
