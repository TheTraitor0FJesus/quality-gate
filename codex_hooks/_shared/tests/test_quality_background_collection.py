"""Collection preserves each project's undelivered result across sibling failures."""

from __future__ import annotations

import errno
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from time import monotonic
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
if __package__ in {None, ""}:
	__package__ = "_shared.tests"

from python_types.results import unchecked

from .. import quality_background_state as background_state
from ..quality_background import (
	BackgroundSession,
	_finding_signature,
	_release_bound,
	_retry_if_stale,
	_validate_builtin,
	_validate_ruff,
	_validate_secrets,
	_visible_reports,
	cancel_all,
	cancel_session,
	collect,
	deliver,
	schedule,
)
from ..quality_background_state import identity, owned_directory, read, write
from ..quality_feedback import feedback
from ..quality_settings import load_settings, settings_operation


def file_signatures(check: str, report: dict[str, Any]) -> dict[str, list[str]]:
	"""Assert the per-file signature contract used by these fixtures."""
	value = _finding_signature(check, report)
	assert isinstance(value, dict)
	return value


class BackgroundCollection(unittest.TestCase):
	def test_delivered_unchecked_result_retains_host_input_paths(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			settings_path, state_dir = base / "settings.json", base / "state"
			self.configure_types(settings_path)
			settings = load_settings(settings_path)["current"]
			root = base / "project"
			root.mkdir()
			(root / "example.py").write_bytes(b"VALUE = 1\n")
			session = BackgroundSession(
				settings_path,
				state_dir,
				"input-scope",
				{
					"session_id": "input-scope",
					"turn_id": "origin-turn",
					"tool_use_id": "origin-call",
				},
			)
			# Control process launch; exercise real scheduling, persistence and delivery.
			with patch("_shared.quality_background.launch"):
				schedule(
					root,
					[{"path": "example.py", "sha256": hashlib.sha256(b"VALUE = 1\n").hexdigest()}],
					session=session,
					deadline=monotonic() + 5,
				)
			owned = owned_directory(state_dir, root)
			document = read(owned / "latest.json")
			document.update(
				status="completed",
				wire=None,
				report=unchecked("dependencies"),
				failure_errno=13,
				failure_winerror=5,
			)
			write(owned / "latest.json", document)
			result = deliver(
				owned, document, identity("input-scope"), session, settings, monotonic() + 5
			)
			assert result is not None
			self.assertEqual(result[0]["input_paths"], ["example.py"])
			self.assertEqual(result[0]["reason"], "dependencies")
			self.assertEqual(result[0]["diagnostic"]["errno"], 13)
			self.assertEqual(result[0]["diagnostic"]["winerror"], 5)
			self.assertEqual(result[0]["diagnostic"]["operation"], "background_worker")
			self.assertEqual(result[0]["event"]["tool_use_id"], "origin-call")
			self.assertEqual(result[0]["background_event_id"], document["request"]["event_id"])

	def configure_types(self, settings_path: Path) -> None:
		local_app_data = settings_path.parent / "local-app-data"
		local_app_data.mkdir(exist_ok=True)
		environment = patch.dict("os.environ", {"LOCALAPPDATA": str(local_app_data)})
		environment.start()
		self.addCleanup(environment.stop)
		settings_operation(settings_path, "init")
		settings_operation(
			settings_path,
			"update",
			{
				**load_settings(settings_path)["current"],
				"provider_release": "v3.2.0",
				"baseline_release": "v2.4.2",
				"enabled": ["types"],
			},
		)

	def test_rollback_cancels_history_beyond_one_session_limit_and_deleted_projects(self) -> None:
		with tempfile.TemporaryDirectory() as temporary_directory:
			base = Path(temporary_directory)
			settings_path = base / "python-quality.json"
			self.configure_types(settings_path)
			foreign_settings = base / "other-settings.json"
			settings_operation(foreign_settings, "init")
			settings_operation(foreign_settings, "update", load_settings(settings_path)["current"])
			state_dir = base / "state"
			latest_paths = []
			content = b"VALUE: int = 1\n"
			with patch("_shared.quality_background.launch"):
				for index in range(background_state.MAX_ROOTS + 2):
					root = base / f"project-{index}"
					root.mkdir()
					(root / "example.py").write_bytes(content)
					owner = foreign_settings if index == 0 else settings_path
					queued = schedule(
						root,
						[{"path": "example.py", "sha256": hashlib.sha256(content).hexdigest()}],
						session=BackgroundSession(owner, state_dir, f"session-{index}"),
						deadline=monotonic() + 5,
					)
					self.assertEqual(queued["status"], "pending")
					latest_paths.append(owned_directory(state_dir, root) / "latest.json")
					if index == 1:
						(root / "example.py").unlink()
						root.rmdir()
			cancel_all(settings_path=settings_path, state_dir=state_dir, deadline=monotonic() + 5)
			self.assertEqual(read(latest_paths[0])["status"], "pending")
			self.assertTrue(all(read(path)["status"] == "retired" for path in latest_paths[1:]))

	def test_rollback_observes_its_deadline_before_reading_history(self) -> None:
		with tempfile.TemporaryDirectory() as temporary_directory:
			base = Path(temporary_directory)
			latest = base / "state/background/roots/project/latest.json"
			latest.parent.mkdir(parents=True)
			latest.write_bytes(b"{}")
			with (
				patch("_shared.quality_background.state.read") as read_state,
				self.assertRaisesRegex(background_state.DeadlineError, "deadline"),
			):
				cancel_all(
					settings_path=base / "python-quality.json",
					state_dir=base / "state",
					deadline=monotonic() - 1,
				)
			read_state.assert_not_called()

	def test_rollback_reports_corrupt_history_instead_of_claiming_cleanup(self) -> None:
		with tempfile.TemporaryDirectory() as temporary_directory:
			base = Path(temporary_directory)
			latest = base / "state/background/roots/project/latest.json"
			latest.parent.mkdir(parents=True)
			latest.write_bytes(b"{}")
			with self.assertRaisesRegex(ValueError, "invalid_background_state"):
				cancel_all(
					settings_path=base / "python-quality.json",
					state_dir=base / "state",
					deadline=monotonic() + 5,
				)

	def test_collection_stops_at_deadline_between_roots(self) -> None:
		with tempfile.TemporaryDirectory() as temporary_directory:
			base = Path(temporary_directory)
			settings_path = base / "python-quality.json"
			self.configure_types(settings_path)
			roots = [base / "project-a", base / "project-b"]
			with (
				patch("_shared.quality_background.state.roots_for", return_value=roots),
				patch(
					"_shared.quality_background.state.owned_directory",
					side_effect=lambda _, root: root,
				),
				patch("_shared.quality_background.collect_root", return_value=None) as collect_root,
				patch("_shared.quality_background.monotonic", side_effect=[0.0, 2.0]),
			):
				reports = collect(
					session=BackgroundSession(settings_path, base / "state", "deadline-profile"),
					deadline=1.0,
				)
		self.assertEqual(collect_root.call_count, 1)
		self.assertEqual(
			reports,
			[
				{
					"check": "background",
					"status": "unchecked",
					"reason": "deadline",
					"diagnostic": {"operation": "background_collect", "phase": "background"},
				}
			],
		)

	def test_state_read_deadline_is_reported_to_the_agent(self) -> None:
		with tempfile.TemporaryDirectory() as temporary_directory:
			base = Path(temporary_directory)
			settings_path = base / "python-quality.json"
			self.configure_types(settings_path)
			with patch(
				"_shared.quality_background.state.roots_for",
				side_effect=background_state.DeadlineError("deadline"),
			):
				reports = collect(
					session=BackgroundSession(settings_path, base / "state", "expired-state"),
					deadline=monotonic() + 1,
				)
		self.assertEqual(reports[0]["check"], "background")
		self.assertEqual(reports[0]["status"], "unchecked")
		self.assertEqual(reports[0]["reason"], "deadline")
		self.assertEqual(reports[0]["diagnostic"]["operation"], "background_collect")

	@unittest.skipUnless(os.name == "nt", "Windows sharing-violation deadline")
	def test_state_read_retry_is_bounded_by_collection_deadline(self) -> None:
		with tempfile.TemporaryDirectory() as temporary_directory:
			path = Path(temporary_directory) / "latest.json"
			path.write_text('{"schema": 1}', encoding="utf-8")
			sharing_violation = PermissionError(errno.EACCES, "sharing violation", str(path))
			with (
				patch(
					"_shared.quality_background_state.monotonic",
					side_effect=[0.0, 0.0, 0.02],
				),
				patch("_shared.quality_background_state.sleep") as wait,
				patch.object(Path, "open", side_effect=sharing_violation),
				self.assertRaisesRegex(TimeoutError, "deadline"),
			):
				background_state.read(path, deadline=0.01)
			wait.assert_called_once_with(0.01)

	def test_later_corrupt_root_does_not_lose_or_block_other_deliveries(self) -> None:
		with tempfile.TemporaryDirectory() as temporary_directory:
			base = Path(temporary_directory)
			state_dir = base / "state"
			settings_path = base / "python-quality.json"
			self.configure_types(settings_path)
			session = "multi-project"
			roots = []
			latest_paths = []
			with patch("_shared.quality_background.launch") as launch:
				for name in ("project-a", "project-b", "project-c"):
					root = base / name
					root.mkdir()
					source = root / "example.py"
					content = b"VALUE: int = 1\n"
					source.write_bytes(content)
					result = schedule(
						root,
						[
							{
								"path": source.name,
								"sha256": hashlib.sha256(content).hexdigest(),
							}
						],
						session=BackgroundSession(settings_path, state_dir, session),
						deadline=monotonic() + 5,
					)
					self.assertEqual(result["status"], "pending")
					directory = owned_directory(state_dir, root)
					latest = directory / "latest.json"
					document = read(latest)
					document.update(
						status="completed",
						wire=None,
						report=unchecked("provider_failure"),
					)
					write(latest, document)
					roots.append(root)
					latest_paths.append(latest)
			self.assertEqual(launch.call_count, 3)

			corrupt_path = latest_paths[1]
			valid_state = corrupt_path.read_bytes()
			corrupt_path.write_text("{", encoding="utf-8")
			try:
				reports = collect(
					session=BackgroundSession(settings_path, state_dir, session),
					deadline=monotonic() + 5,
					drain=True,
				)
				self.assertEqual(
					[(report["status"], report.get("reason")) for report in reports],
					[
						("unchecked", "provider_failure"),
						("unchecked", "invalid_background_state"),
						("unchecked", "provider_failure"),
					],
				)
				audience = identity(session)
				for latest in (latest_paths[0], latest_paths[2]):
					self.assertIn(audience, read(latest)["acknowledged"])
			finally:
				corrupt_path.write_bytes(valid_state)

			recovered = collect(
				session=BackgroundSession(settings_path, state_dir, session),
				deadline=monotonic() + 5,
				drain=True,
			)
			self.assertEqual(
				[(report["status"], report.get("reason")) for report in recovered],
				[("unchecked", "provider_failure")],
			)
			self.assertIn(identity(session), read(latest_paths[1])["acknowledged"])

	def test_repeated_failed_results_remain_visible_during_drain(self) -> None:
		with tempfile.TemporaryDirectory() as temporary_directory:
			base = Path(temporary_directory)
			settings_path = base / "python-quality.json"
			state_dir = base / "state"
			self.configure_types(settings_path)
			root = base / "project"
			root.mkdir()
			source = root / "example.py"
			content = b"VALUE: int = 1\n"
			source.write_bytes(content)
			session = BackgroundSession(settings_path, state_dir, "repeated-failure")
			failed_report = {
				"check": "types",
				"status": "failed",
				"findings": [
					{"path": "example.py", "line": 1, "column": 1, "rule": "attr-defined"}
				],
			}

			def complete_failure() -> None:
				latest = owned_directory(state_dir, root) / "latest.json"
				document = read(latest)
				document.update(
					status="completed",
					wire={"status": "failed"},
					report=None,
					engine_identity="current-engine",
				)
				write(latest, document)

			collected: list[list[dict[str, Any]]] = []
			with patch("_shared.quality_background.launch"):
				for _ in range(2):
					schedule(
						root,
						[{"path": source.name, "sha256": hashlib.sha256(content).hexdigest()}],
						session=session,
						deadline=monotonic() + 5,
					)
					complete_failure()
					with (
						patch("_shared.quality_background.release_inventory", return_value=[]),
						patch("_shared.quality_background.digest", return_value="current-engine"),
						patch(
							"_shared.quality_background.validate_result",
							return_value=failed_report,
						),
					):
						reports = collect(session=session, deadline=monotonic() + 5, drain=True)
					collected.append(reports)
			self.assertEqual(collected[0], [failed_report])
		self.assertEqual(
			collected[1],
			[
				{
					"check": "types",
					"status": "failed",
					"reason": "repeated_findings",
					"findings": [],
				}
			],
		)
		result = feedback("PostToolUse", collected[1])
		assert result is not None
		self.assertIn(
			"types FAILED: unchanged findings remain",
			result["hookSpecificOutput"]["additionalContext"],
		)

	def test_secret_waiver_resets_the_delivered_finding_signature(self) -> None:
		failed = {
			"check": "secrets",
			"status": "failed",
			"files": [
				{
					"path": "example.py",
					"findings": [
						{
							"check_id": "secrets.candidate",
							"line": 1,
							"fingerprint": "a" * 64,
							"status": "failed",
						}
					],
				}
			],
		}
		waived = {
			"check": "secrets",
			"status": "waived",
			"files": [
				{
					"path": "example.py",
					"findings": [
						{
							"check_id": "secrets.candidate",
							"line": 1,
							"fingerprint": "a" * 64,
							"status": "waived",
						}
					],
				}
			],
		}
		document: dict[str, Any] = {
			"checks": ["secrets"],
			"request": {"event_id": "waiver-transition"},
			"last_findings": {"audience": {}},
		}
		persisted: dict[str, Any] = {}

		def acknowledge(
			_directory: Path,
			_event: str,
			audience: str,
			signatures: dict[str, dict[str, list[str]] | list[str] | None],
			*,
			deadline: float | None = None,
		) -> bool:
			for check, value in signatures.items():
				if value is None:
					persisted.pop(check, None)
				elif check in {"ruff", "secrets", "builtin_secrets"}:
					if not isinstance(value, dict):
						persisted.pop(check, None)
						continue
					paths = dict(persisted.get(check, {}))
					for path, current in value.items():
						if current:
							paths[path] = current
						else:
							paths.pop(path, None)
					if paths:
						persisted[check] = paths
					else:
						persisted.pop(check, None)
				else:
					persisted[check] = value
			return True

		with patch("_shared.quality_background.acknowledge", side_effect=acknowledge):
			first = _visible_reports([failed], document, Path("."), "audience", False)
			document["last_findings"]["audience"] = dict(persisted)
			cleared = _visible_reports([waived], document, Path("."), "audience", False)
			self.assertNotIn("secrets", persisted)
			document["last_findings"]["audience"] = dict(persisted)
			restored = _visible_reports([failed], document, Path("."), "audience", False)

		self.assertEqual(first, [failed])
		self.assertEqual(cleared, [waived])
		self.assertEqual(restored, [failed])
		self.assertEqual(
			persisted["secrets"],
			_finding_signature("secrets", failed),
		)

	def test_secret_signature_ignores_new_clean_files(self) -> None:
		finding = {
			"check_id": "secrets.candidate",
			"line": 1,
			"fingerprint": "a" * 64,
			"status": "failed",
		}
		original = {
			"check": "secrets",
			"status": "failed",
			"files": [{"path": "example.py", "findings": [finding]}],
		}
		with_clean_file = {
			**original,
			"files": [
				*original["files"],
				{"path": "clean.py", "findings": []},
			],
		}
		self.assertNotEqual(
			_finding_signature("secrets", original),
			_finding_signature("secrets", with_clean_file),
		)
		document = {
			"checks": ["secrets"],
			"request": {"event_id": "same-secret-finding"},
			"last_findings": {"audience": {"secrets": _finding_signature("secrets", original)}},
		}
		with patch("_shared.quality_background.acknowledge", return_value=True):
			delivery = _visible_reports([with_clean_file], document, Path("."), "audience", False)
		self.assertIsNone(delivery)

	def test_reader_deduplication_retains_findings_for_unobserved_paths(self) -> None:
		with tempfile.TemporaryDirectory() as temporary_directory:
			base = Path(temporary_directory)
			settings_path = base / "python-quality.json"
			state_dir = base / "state"
			self.configure_types(settings_path)
			settings_operation(
				settings_path,
				"update",
				{
					**load_settings(settings_path)["current"],
					"enabled": ["ruff", "secrets"],
				},
			)
			root = base / "project"
			root.mkdir()
			a_path = root / "a.py"
			b_path = root / "b.py"
			a_bytes = b"A: int = 1\n"
			b_bytes = b"B: int = 2\n"
			a_path.write_bytes(a_bytes)
			b_path.write_bytes(b_bytes)
			session = BackgroundSession(settings_path, state_dir, "path-scoped-findings")
			audience = identity(session.session_id)
			ruff_finding = {"check_id": "ruff", "rule": "F821", "line": 1, "column": 1}
			secret_finding = {
				"check_id": "secrets.candidate",
				"line": 1,
				"fingerprint": "a" * 64,
				"status": "failed",
			}
			builtin_finding = {"path": "a.py", "line": 1, "check_id": "secrets.builtin"}
			a_reports: list[dict[str, Any]] = [
				{
					"check": "ruff",
					"status": "failed",
					"files": [
						{
							"path": "a.py",
							"status": "failed",
							"format": "passed",
							"findings": [ruff_finding],
						}
					],
				},
				{
					"check": "secrets",
					"status": "failed",
					"files": [{"path": "a.py", "findings": [secret_finding]}],
				},
				{
					"check": "builtin_secrets",
					"status": "failed",
					"files": [builtin_finding],
				},
			]
			clean_reports: list[dict[str, Any]] = [
				{"check": "ruff", "status": "passed", "files": [{"path": "b.py"}]},
				{"check": "secrets", "status": "passed", "files": [{"path": "b.py"}]},
				{"check": "builtin_secrets", "status": "passed", "files": []},
			]
			with patch("_shared.quality_background.launch"):
				for index, (path, content, reports) in enumerate(
					(
						(a_path, a_bytes, a_reports),
						(b_path, b_bytes, clean_reports),
						(a_path, a_bytes, a_reports),
					)
				):
					schedule(
						root,
						[
							{
								"path": path.name,
								"sha256": hashlib.sha256(content).hexdigest(),
							}
						],
						session=session,
						deadline=monotonic() + 5,
						checks=["ruff", "secrets"],
					)
					directory = owned_directory(state_dir, root)
					document = read(directory / "latest.json")
					visible = _visible_reports(reports, document, directory, audience, False)
					if index > 0:
						self.assertIsNone(visible)
					else:
						self.assertEqual(len(visible or []), 3)
			self.assertEqual(
				read(directory / "latest.json")["last_findings"][audience],
				{
					"ruff": {"a.py": file_signatures("ruff", a_reports[0])["a.py"]},
					"secrets": {"a.py": file_signatures("secrets", a_reports[1])["a.py"]},
					"builtin_secrets": {
						"a.py": file_signatures("builtin_secrets", a_reports[2])["a.py"]
					},
				},
			)

	def test_partial_reader_findings_emit_only_new_actionable_items(self) -> None:
		ruff_first = {"check_id": "ruff", "rule": "F401", "line": 1, "column": 1}
		ruff_remaining = {"check_id": "ruff", "rule": "F821", "line": 2, "column": 1}
		ruff_new = {"check_id": "ruff", "rule": "F841", "line": 3, "column": 1}
		secret_first = {
			"check_id": "secrets.candidate",
			"path": "example.py",
			"target": "example.py:1",
			"line": 1,
			"fingerprint": "a" * 64,
			"status": "failed",
		}
		secret_remaining = {
			**secret_first,
			"line": 2,
			"target": "example.py:2",
			"fingerprint": "b" * 64,
		}
		secret_new = {**secret_first, "line": 3, "target": "example.py:3", "fingerprint": "c" * 64}
		builtin_first = {"path": "example.py", "line": 4, "check_id": "secrets.builtin"}
		builtin_remaining = {"path": "example.py", "line": 5, "check_id": "secrets.builtin"}
		builtin_new = {"path": "example.py", "line": 6, "check_id": "secrets.builtin"}
		previous_reports: list[dict[str, Any]] = [
			{
				"check": "ruff",
				"status": "failed",
				"files": [
					{
						"path": "example.py",
						"status": "failed",
						"format": "passed",
						"findings": [ruff_first, ruff_remaining],
					}
				],
			},
			{
				"check": "secrets",
				"status": "failed",
				"files": [
					{
						"path": "example.py",
						"findings": [secret_first, secret_remaining],
					}
				],
			},
			{
				"check": "builtin_secrets",
				"status": "failed",
				"files": [builtin_first, builtin_remaining],
			},
		]
		remaining_reports = [
			{
				**previous_reports[0],
				"files": [{**previous_reports[0]["files"][0], "findings": [ruff_remaining]}],
			},
			{
				**previous_reports[1],
				"files": [{"path": "example.py", "findings": [secret_remaining]}],
			},
			{**previous_reports[2], "files": [builtin_remaining]},
		]
		current_reports = [
			{
				**remaining_reports[0],
				"files": [
					{**remaining_reports[0]["files"][0], "findings": [ruff_remaining, ruff_new]}
				],
			},
			{
				**remaining_reports[1],
				"files": [{"path": "example.py", "findings": [secret_remaining, secret_new]}],
			},
			{**remaining_reports[2], "files": [builtin_remaining, builtin_new]},
		]
		document = {
			"checks": ["ruff", "secrets"],
			"request": {"event_id": "partial-findings"},
			"last_findings": {
				"audience": {
					report["check"]: _finding_signature(report["check"], report)
					for report in previous_reports
				}
			},
		}

		with patch("_shared.quality_background.acknowledge", return_value=True):
			unchanged = _visible_reports(remaining_reports, document, Path("."), "audience", False)
			delivered = _visible_reports(current_reports, document, Path("."), "audience", False)

		self.assertIsNone(unchanged)
		assert delivered is not None
		self.assertEqual(delivered[0]["files"][0]["findings"], [ruff_new])
		self.assertEqual(delivered[1]["files"][0]["findings"], [secret_new])
		self.assertEqual(delivered[2]["files"], [builtin_new])

	def test_not_applicable_readers_recheck_restored_deleted_inputs(self) -> None:
		with tempfile.TemporaryDirectory() as temporary_directory:
			root = Path(temporary_directory)
			request = {
				"root": str(root.resolve()),
				"files": [{"path": "removed.py", "sha256": None}],
			}
			deadline = monotonic() + 5
			ruff_report = {
				"schema": 1,
				"status": "not_applicable",
				"reason": "no_python_files",
				"files": [],
				"phase": "validate",
				"writes_may_have_occurred": False,
			}
			secret_report = {
				"schema": 1,
				"check": "secrets",
				"check_id": "secrets.candidate",
				"status": "not_applicable",
				"reason": "no_files",
				"files": [],
				"selections": [],
				"inputs": [],
				"writes_may_have_occurred": False,
			}
			settings = {"provider_release": "v3.2.0", "baseline_release": "v2.4.2"}
			self.assertEqual(
				_validate_ruff(ruff_report, request, settings, deadline, {})["status"],
				"not_applicable",
			)
			self.assertEqual(
				_validate_secrets(secret_report, request, settings, deadline, {})["status"],
				"not_applicable",
			)
			(root / "removed.py").write_text("TOKEN = 1\n", encoding="utf-8")
			stale_ruff = _validate_ruff(ruff_report, request, settings, deadline, {})
			stale_secrets = _validate_secrets(secret_report, request, settings, deadline, {})

		self.assertEqual(
			stale_ruff, {"check": "ruff", "status": "unchecked", "reason": "stale_result"}
		)
		self.assertEqual(
			stale_secrets,
			{"check": "secrets", "status": "unchecked", "reason": "stale_result"},
		)

	def test_mixed_reader_requests_reject_restored_deleted_inputs(self) -> None:
		for check in ("ruff", "secrets"):
			with self.subTest(check=check), tempfile.TemporaryDirectory() as temporary_directory:
				root = Path(temporary_directory)
				existing = root / "existing.py"
				existing_bytes = b"VALUE: int = 1\n"
				existing.write_bytes(existing_bytes)
				request = {
					"root": str(root.resolve()),
					"files": [
						{
							"path": "existing.py",
							"sha256": hashlib.sha256(existing_bytes).hexdigest(),
						},
						{"path": "removed.py", "sha256": None},
					],
				}
				removed = root / "removed.py"
				removed.write_bytes(b"RESTORED: int = 2\n")
				provider = (
					{
						"schema": 1,
						"status": "failed",
						"files": [],
					}
					if check == "ruff"
					else {
						"schema": 1,
						"check": "secrets",
						"check_id": "secrets.candidate",
						"status": "failed",
						"files": [],
						"selections": [],
					}
				)
				validated: dict[str, Any] = {
					"check": check,
					"status": "failed",
					"reason": None,
					"files": [],
					"selections": [],
				}
				validator_path = (
					"python_quality.check.validate_result"
					if check == "ruff"
					else "secret_scan.results.validate_result"
				)
				with patch(validator_path, return_value=validated) as validator:
					if check == "ruff":
						result = _validate_ruff(
							provider,
							request,
							{"provider_release": "v3.2.0"},
							monotonic() + 5,
							{},
						)
					else:
						result = _validate_secrets(
							provider,
							request,
							{"provider_release": "v3.2.0", "baseline_release": "v2.4.2"},
							monotonic() + 5,
							{},
						)
				self.assertEqual(
					result,
					{"check": check, "status": "unchecked", "reason": "stale_result"},
				)
				self.assertEqual(
					validator.call_args.args[2]["files"],
					[request["files"][0]],
				)

	def test_reader_configuration_inputs_are_rechecked_after_release_binding(self) -> None:
		for check in ("ruff", "secrets"):
			with self.subTest(check=check), tempfile.TemporaryDirectory() as temporary_directory:
				root = Path(temporary_directory)
				source = root / "example.py"
				source_bytes = b"VALUE: int = 1\n"
				source.write_bytes(source_bytes)
				request = {
					"root": str(root.resolve()),
					"files": [
						{
							"path": source.name,
							"sha256": hashlib.sha256(source_bytes).hexdigest(),
						}
					],
				}
				config = root / (".ruff.toml" if check == "ruff" else "quality-gate.toml")
				original = b"ORIGINAL CONFIG\n"
				config.write_bytes(original)
				if check == "ruff":
					inputs: list[dict[str, Any]] = [
						{
							"path": config.name,
							"sha256": hashlib.sha256(original).hexdigest(),
						}
					]
					provider = {"schema": 1, "status": "failed", "files": []}
					settings = {"provider_release": "v3.2.0"}
					validator_path = "python_quality.check.validate_result"
				else:
					inputs = [
						{
							"path": "quality-gate.toml",
							"sha256": hashlib.sha256(original).hexdigest(),
						},
						{"path": ".gitleaks.toml", "sha256": None},
						{"path": ".gitleaksignore", "sha256": None},
					]
					provider = {
						"schema": 1,
						"check": "secrets",
						"check_id": "secrets.candidate",
						"status": "failed",
						"files": [],
						"selections": [],
					}
					settings = {"provider_release": "v3.2.0", "baseline_release": "v2.4.2"}
					validator_path = "secret_scan.results.validate_result"
				validated: dict[str, Any] = {
					"check": check,
					"status": "failed",
					"reason": None,
					"files": [],
					"selections": [],
					"inputs": inputs,
				}

				def change_config_after_release(
					report: dict[str, Any], *_args: object, config_path: Path = config
				) -> dict[str, Any]:
					config_path.write_bytes(b"CHANGED CONFIG\n")
					return report

				with (
					patch(validator_path, return_value=validated),
					patch(
						"_shared.quality_background._release_bound",
						side_effect=change_config_after_release,
					),
				):
					if check == "ruff":
						result = _validate_ruff(provider, request, settings, monotonic() + 5, {})
					else:
						result = _validate_secrets(provider, request, settings, monotonic() + 5, {})
				self.assertEqual(
					result,
					{"check": check, "status": "unchecked", "reason": "stale_result"},
				)

	def test_invalid_builtin_result_and_io_errors_do_not_trigger_stale_retry(self) -> None:
		with tempfile.TemporaryDirectory() as temporary_directory:
			root = Path(temporary_directory)
			source = root / "example.py"
			content = b"VALUE: int = 1\n"
			source.write_bytes(content)
			request = {
				"root": str(root.resolve()),
				"files": [{"path": source.name, "sha256": hashlib.sha256(content).hexdigest()}],
			}
			deadline = monotonic() + 5
			invalid = _validate_builtin(
				{
					"check": "builtin_secrets",
					"status": "failed",
					"files": [{"path": "other.py", "line": 1, "check_id": "secrets.builtin"}],
				},
				request,
				deadline,
			)
			with patch("_shared.quality_background._fresh_inputs", side_effect=OSError("io")):
				io_error = _validate_builtin(
					{"check": "builtin_secrets", "status": "passed", "files": []},
					request,
					deadline,
				)
			document = {
				"retry": 0,
				"request": request,
				"session_id": "builtin-invalid-result",
				"checks": ["secrets"],
			}
			session = BackgroundSession(
				Path("settings.json"), Path("state"), "builtin-invalid-result"
			)
			with patch("_shared.quality_background.schedule") as schedule:
				for report, reason in ((invalid, "invalid_provider_result"), (io_error, "io")):
					self.assertEqual(report["reason"], reason)
					self.assertIsNone(_retry_if_stale([report], document, session, deadline))
			schedule.assert_not_called()

	def test_stale_adapter_results_keep_reason_for_bounded_retry(self) -> None:
		from python_quality.check import unchecked as ruff_unchecked
		from secret_scan.results import unchecked as secret_unchecked

		ruff_stale = ruff_unchecked("stale_result", phase="validate")
		secret_stale = secret_unchecked("stale_result")
		request = {"root": "C:/project", "files": [{"path": "example.py", "sha256": "a" * 64}]}
		settings = {"provider_release": "v3.2.0", "baseline_release": "v2.4.2"}
		deadline = monotonic() + 5
		with (
			patch("python_quality.check.validate_result", return_value=ruff_stale),
			patch("secret_scan.results.validate_result", return_value=secret_stale),
		):
			validated_ruff = _validate_ruff(ruff_stale, request, settings, deadline, {})
			validated_secrets = _validate_secrets(secret_stale, request, settings, deadline, {})

		self.assertEqual(validated_ruff["reason"], "stale_result")
		self.assertEqual(validated_secrets["reason"], "stale_result")
		document = {
			"retry": 0,
			"request": request,
			"session_id": "bounded-retry",
			"checks": ["ruff", "secrets"],
		}
		session = BackgroundSession(Path("settings.json"), Path("state"), "bounded-retry")
		with (
			patch("_shared.quality_background.current_inputs", return_value=request["files"]),
			patch(
				"_shared.quality_background.schedule", return_value={"status": "pending"}
			) as schedule,
		):
			retry = _retry_if_stale(
				[validated_ruff, validated_secrets], document, session, deadline
			)
		self.assertEqual(retry, {"status": "pending"})
		self.assertEqual(schedule.call_args.kwargs["retry"], 1)
		self.assertEqual(schedule.call_args.kwargs["checks"], ["ruff", "secrets"])

	def test_schedule_retains_the_selected_composite_checks(self) -> None:
		with tempfile.TemporaryDirectory() as temporary_directory:
			base = Path(temporary_directory)
			settings_path = base / "python-quality.json"
			state_dir = base / "state"
			self.configure_types(settings_path)
			settings_operation(
				settings_path,
				"update",
				{
					**load_settings(settings_path)["current"],
					"enabled": ["ruff", "secrets", "types"],
				},
			)
			root = base / "project"
			root.mkdir()
			source = root / "example.py"
			content = b"VALUE: int = 1\n"
			source.write_bytes(content)
			session = BackgroundSession(settings_path, state_dir, "composite-checks")
			deadline = monotonic() + 5
			with (
				patch("_shared.quality_background.launch"),
				patch(
					"_shared.quality_background.state.subscribe",
					wraps=background_state.subscribe,
				) as subscribe,
			):
				result = schedule(
					root,
					[{"path": source.name, "sha256": hashlib.sha256(content).hexdigest()}],
					session=session,
					deadline=deadline,
					checks=["ruff", "secrets", "types"],
				)
				other = root / "consumer.py"
				other_content = b"CONSUMER: int = 2\n"
				other.write_bytes(other_content)
				replacement = schedule(
					root,
					[
						{
							"path": other.name,
							"sha256": hashlib.sha256(other_content).hexdigest(),
						}
					],
					session=session,
					deadline=monotonic() + 5,
				)
			document = read(owned_directory(state_dir, root) / "latest.json")
		self.assertEqual(result, {"check": "background", "status": "pending"})
		self.assertEqual(replacement, {"check": "background", "status": "pending"})
		self.assertEqual(subscribe.call_args_list[0].kwargs["deadline"], deadline)
		self.assertEqual(document["checks"], ["ruff", "secrets", "types"])
		self.assertEqual(
			[item["path"] for item in document["request"]["files"]],
			["consumer.py", "example.py"],
		)

	def test_full_scope_not_applicable_reports_reach_the_release_validators(self) -> None:
		with tempfile.TemporaryDirectory() as temporary_directory:
			root = Path(temporary_directory)
			excluded = root / "excluded.py"
			excluded.write_bytes(b"VALUE: int = 1\n")
			excluded_request = {
				"root": str(root.resolve()),
				"files": [
					{
						"path": excluded.name,
						"sha256": hashlib.sha256(excluded.read_bytes()).hexdigest(),
					}
				],
			}
			ruff_provider = {
				"schema": 1,
				"status": "not_applicable",
				"reason": None,
				"files": [{"path": excluded.name, "status": "not_applicable"}],
			}
			ruff_normalized: dict[str, Any] = {
				"check": "ruff",
				"status": "not_applicable",
				"reason": None,
				"files": [],
				"selections": [],
			}
			with patch(
				"python_quality.check.validate_result", return_value=ruff_normalized
			) as validate_ruff:
				ruff_result = _validate_ruff(
					ruff_provider,
					excluded_request,
					{"provider_release": "v3.2.0"},
					monotonic() + 5,
					{},
				)
		self.assertEqual(ruff_result, ruff_normalized)
		validate_ruff.assert_called_once()

		with tempfile.TemporaryDirectory() as temporary_directory:
			root = Path(temporary_directory)
			empty_file = root / "empty.py"
			empty_file.write_bytes(b"")
			secret_request = {
				"root": str(root.resolve()),
				"files": [
					{
						"path": empty_file.name,
						"sha256": hashlib.sha256(b"").hexdigest(),
					}
				],
			}
			secret_provider = {
				"schema": 1,
				"check": "secrets",
				"check_id": "secrets.candidate",
				"status": "not_applicable",
				"reason": None,
				"files": [{"path": empty_file.name, "status": "not_applicable"}],
			}
			secret_normalized: dict[str, Any] = {
				"check": "secrets",
				"status": "not_applicable",
				"reason": None,
				"files": [],
				"selections": [],
			}
			with patch(
				"secret_scan.results.validate_result", return_value=secret_normalized
			) as validate_secrets:
				secret_result = _validate_secrets(
					secret_provider,
					secret_request,
					{"baseline_release": "v2.4.2", "provider_release": "v3.2.0"},
					monotonic() + 5,
					{},
				)
		self.assertEqual(secret_result, secret_normalized)
		validate_secrets.assert_called_once()

	def test_composite_delivery_revalidates_and_deduplicates_each_reader(self) -> None:
		with tempfile.TemporaryDirectory() as temporary_directory:
			base = Path(temporary_directory)
			settings_path = base / "python-quality.json"
			state_dir = base / "state"
			self.configure_types(settings_path)
			settings_operation(
				settings_path,
				"update",
				{
					**load_settings(settings_path)["current"],
					"enabled": ["ruff", "secrets"],
				},
			)
			settings = load_settings(settings_path)["current"]
			root = base / "project"
			root.mkdir()
			source = root / "example.py"
			content = b"VALUE: int = 1\n"
			source.write_bytes(content)
			session = BackgroundSession(settings_path, state_dir, "composite-delivery")
			ruff_report = {
				"check": "ruff",
				"status": "failed",
				"files": [
					{
						"path": source.name,
						"status": "failed",
						"format": "passed",
						"findings": [
							{
								"check_id": "ruff",
								"rule": "F821",
								"line": 1,
								"column": 1,
							}
						],
					}
				],
			}
			secret_report = {
				"check": "secrets",
				"status": "failed",
				"files": [
					{
						"path": source.name,
						"findings": [
							{
								"check_id": "secrets.candidate",
								"line": 1,
								"fingerprint": "b" * 64,
								"status": "failed",
							}
						],
					}
				],
			}
			builtin_report = {
				"check": "builtin_secrets",
				"status": "failed",
				"files": [{"path": source.name, "line": 1, "check_id": "secrets.builtin"}],
			}
			wire = {
				"schema": 1,
				"results": {
					"ruff": {"report": ruff_report},
					"secrets": {"report": secret_report},
					"builtin_secrets": {"report": builtin_report},
				},
			}

			def completed_document() -> dict[str, Any]:
				document = read(owned_directory(state_dir, root) / "latest.json")
				document.update(
					status="completed",
					wire=wire,
					report=None,
					engine_identity="current-engine",
				)
				write(owned_directory(state_dir, root) / "latest.json", document)
				return document

			failed_ruff = {**ruff_report, "reason": None, "selections": [], "inputs": []}
			failed_secrets = {
				**secret_report,
				"reason": None,
				"selections": [],
				"inputs": [
					{"path": "quality-gate.toml", "sha256": None},
					{"path": ".gitleaks.toml", "sha256": None},
					{"path": ".gitleaksignore", "sha256": None},
				],
			}
			first_reports: list[dict[str, Any]] | None = None
			with (
				patch("_shared.quality_background.launch"),
				patch("_shared.quality_background.release_inventory", return_value={}),
				patch("_shared.quality_background.digest", return_value="current-engine"),
				patch("python_quality.check.validate_result", return_value=failed_ruff),
				patch("secret_scan.results.validate_result", return_value=failed_secrets),
			):
				for index in range(2):
					schedule(
						root,
						[
							{
								"path": source.name,
								"sha256": hashlib.sha256(content).hexdigest(),
							}
						],
						session=session,
						deadline=monotonic() + 5,
						checks=["ruff", "secrets"],
					)
					document = completed_document()
					reports = deliver(
						owned_directory(state_dir, root),
						document,
						identity(session.session_id),
						session,
						settings,
						monotonic() + 5,
					)
					if index == 0:
						first_reports = reports

		assert first_reports is not None
		self.assertEqual(
			[report["check"] for report in first_reports],
			["ruff", "secrets", "builtin_secrets"],
		)
		self.assertTrue(all(report["status"] == "failed" for report in first_reports))
		self.assertIsNone(reports, "Previously delivered findings must not be emitted again")

	def test_policy_release_hash_is_independent_from_interface_engine_hash(self) -> None:
		with tempfile.TemporaryDirectory() as temporary_directory:
			root = Path(temporary_directory)
			engine_identity = "a" * 64
			policy_identity = "b" * 64
			ruff_policy = "c" * 64
			ruff_wheel = "d" * 64
			ruff_native = "1" * 64
			secrets_tool = "e" * 64
			inventory: dict[str, Any] = {
				"sha256": policy_identity,
				"files": {
					"quality_gate/policy/ruff.toml": ruff_policy,
					"tools/ruff-0.16.4-py3-none-win_amd64.whl": ruff_wheel,
					"tools/gitleaks": secrets_tool,
				},
				"tools": [
					{
						"name": "ruff",
						"version": "0.16.4",
						"path": "tools/ruff-0.16.4-py3-none-win_amd64.whl",
						"sha256": ruff_wheel,
					},
					{
						"name": "gitleaks",
						"version": "8.30.1",
						"path": "tools/gitleaks",
						"sha256": secrets_tool,
					},
				],
			}
			selections: dict[str, dict[str, Any]] = {
				"ruff": {
					"policy_release": "v2.4.2",
					"release_sha256": policy_identity,
					"policy_sha256": ruff_policy,
					"ruff_version": "0.16.4",
					"ruff_sha256": ruff_native,
				},
				"secrets": {
					"policy_release": "v2.4.2",
					"release_sha256": policy_identity,
					"policy_source": "gitleaks-built-in",
					"policy_sha256": secrets_tool,
					"scanner_version": "8.30.1",
					"scanner_sha256": secrets_tool,
					"waiver_sha256": "f" * 64,
				},
			}
			for check, selection in selections.items():
				report = {"reason": None, "selections": [selection]}
				release_inventories = {
					"v3.2.0": {"sha256": engine_identity, "files": {}, "tools": []}
				}
				with patch("_shared.quality_background.release_inventory", return_value=inventory):
					bound = _release_bound(
						report,
						{"root": str(root)},
						monotonic() + 5,
						check,
						release_inventories,
					)
				self.assertEqual(bound, report, check)
				self.assertNotEqual(engine_identity, selection["release_sha256"])
				tampered = {**selection, "release_sha256": "f" * 64}
				with patch("_shared.quality_background.release_inventory", return_value=inventory):
					rejected = _release_bound(
						{"reason": None, "selections": [tampered]},
						{"root": str(root)},
						monotonic() + 5,
						check,
						{"v3.2.0": {"sha256": engine_identity, "files": {}, "tools": []}},
					)
				self.assertEqual(rejected["reason"], "release", check)

			self.assertNotEqual(ruff_wheel, selections["ruff"]["ruff_sha256"])
			corrupt_inventory = {
				**inventory,
				"files": {
					**inventory["files"],
					"tools/ruff-0.16.4-py3-none-win_amd64.whl": "f" * 64,
				},
			}
			with patch(
				"_shared.quality_background.release_inventory",
				return_value=corrupt_inventory,
			):
				rejected_wheel = _release_bound(
					{"reason": None, "selections": [selections["ruff"]]},
					{"root": str(root)},
					monotonic() + 5,
					"ruff",
					{"v3.2.0": {"sha256": engine_identity, "files": {}, "tools": []}},
				)
			self.assertEqual(rejected_wheel["reason"], "release")

	def test_session_cancellation_continues_after_one_root_fails(self) -> None:
		with tempfile.TemporaryDirectory() as temporary_directory:
			base = Path(temporary_directory)
			settings_path = base / "python-quality.json"
			state_dir = base / "state"
			self.configure_types(settings_path)
			session = BackgroundSession(settings_path, state_dir, "cancel-roots")
			roots = [base / name for name in ("project-a", "project-b", "project-c")]
			with patch("_shared.quality_background.launch"):
				for root in roots:
					root.mkdir()
					source = root / "example.py"
					content = b"VALUE: int = 1\n"
					source.write_bytes(content)
					result = schedule(
						root,
						[{"path": source.name, "sha256": hashlib.sha256(content).hexdigest()}],
						session=session,
						deadline=monotonic() + 5,
					)
					self.assertEqual(result["status"], "pending", result)

			def fail_one_root(state_directory: Path, root: str | Path) -> Path:
				if Path(root).name == roots[1].name:
					raise ValueError("background_state_links")
				return owned_directory(state_directory, Path(root))

			visited: list[Path] = []

			def record_and_fail_one_root(state_directory: Path, root: str | Path) -> Path:
				visited.append(Path(root))
				return fail_one_root(state_directory, root)

			with patch(
				"_shared.quality_background.state.owned_directory",
				side_effect=record_and_fail_one_root,
			):
				reports = cancel_session(session=session, deadline=monotonic() + 5)

			self.assertEqual(
				{root.name for root in visited},
				{root.name for root in roots},
			)
			self.assertEqual(
				[(report["status"], report.get("reason")) for report in reports],
				[("unchecked", "background_state_links")],
			)
			self.assertEqual(
				[
					read(owned_directory(state_dir, root) / "latest.json")["status"]
					for root in roots
				],
				["retired", "pending", "retired"],
			)
