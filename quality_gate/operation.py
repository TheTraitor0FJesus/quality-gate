"""One Gate work deadline, retained observations and a shared physical cleanup phase."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, TypeVar

from .contracts import (
	DEFAULT_GATE_TIMEOUT_SECONDS,
	MAX_TELEMETRY_TEXT,
	CheckResult,
	OperationTelemetry,
	StageTiming,
	Status,
	Verdict,
)
from .edit_process import run_process as supervise_process
from .edit_protocol import MAX_TOOL_BYTES, EditError

if TYPE_CHECKING:
	from .native_cache import NativeToolCacheSelection

OPERATION_CLEANUP_SECONDS = 120
MAX_CLEANUP_INVENTORY_BYTES = 64 * 1024
MAX_NATIVE_CACHE_CLEANUPS = 64
MAX_PROBE_OUTPUT_BYTES = 1024 * 1024
_SYSTEM_WORKSPACE_ROOT_PREFIX = "quality-gate-workspaces-"
_SYSTEM_WORKSPACE_ROOT_PART_COUNT = 3
_SYSTEM_WORKSPACE_OWNER_HASH_LENGTH = 16
_SYSTEM_WORKSPACE_REPOSITORY_HASH_LENGTH = 20
_LOWERCASE_HEX = frozenset("0123456789abcdef")
_active: ContextVar[GateOperation | None] = ContextVar("quality_gate_operation", default=None)
_Result = TypeVar("_Result", bound=CheckResult)
_CLEANUP_BODY = """
import json, sys
limit = int(sys.argv[1])
sys.path.insert(0, sys.argv[2])
payload = sys.stdin.buffer.read(limit + 1)
if len(payload) > limit:
    raise ValueError('cleanup inventory exceeded its limit')
from pathlib import Path
from quality_gate.temp_workspace import cleanup_handed_off_workspace
from quality_gate.snapshot import remove_snapshot
from quality_gate.native_cache import cleanup_native_cache_source_workspace
inventory = json.loads(payload)
failed = False
for temp_root, run_directory in inventory['workspaces']:
    try:
        cleanup_handed_off_workspace(Path(temp_root), Path(run_directory))
    except Exception:
        failed = True
for snapshot_root in inventory['snapshots']:
    try:
        remove_snapshot(Path(snapshot_root))
    except Exception:
        failed = True
for source_workspace in inventory['native_cache']:
    try:
        cleanup_native_cache_source_workspace(source_workspace)
    except Exception:
        failed = True
if failed:
    raise OSError('one or more owned cleanup roots could not be removed')
"""


@dataclass(slots=True)
class _StageObservation:
	process_required: bool
	process_started: bool
	check: bool = False
	cache_selection: NativeToolCacheSelection | None = None


class OperationTimeoutError(RuntimeError):
	"""The original work deadline expired; cleanup retains its separate shared bound."""


def current_operation() -> GateOperation | None:
	return _active.get()


def _is_lowercase_hex(value: str, length: int) -> bool:
	return len(value) == length and all(character in _LOWERCASE_HEX for character in value)


def _is_system_workspace_temp(temp_root: Path, fallback_root: Path) -> bool:
	try:
		parts = temp_root.relative_to(fallback_root).parts
	except ValueError:
		return False
	if len(parts) != _SYSTEM_WORKSPACE_ROOT_PART_COUNT or parts[2] != "temp":
		return False
	owner_hash = parts[0].removeprefix(_SYSTEM_WORKSPACE_ROOT_PREFIX)
	return (
		parts[0].startswith(_SYSTEM_WORKSPACE_ROOT_PREFIX)
		and _is_lowercase_hex(owner_hash, _SYSTEM_WORKSPACE_OWNER_HASH_LENGTH)
		and _is_lowercase_hex(parts[1], _SYSTEM_WORKSPACE_REPOSITORY_HASH_LENGTH)
	)


def remaining_timeout(limit: float | None = None) -> float | None:
	operation = current_operation()
	return limit if operation is None else operation.remaining(limit)


def observe_check(
	check_id: str,
	scope: str,
	check: Callable[[], _Result],
	*,
	process_required: bool = False,
) -> _Result:
	"""Time one stable check id and retain its result before later work can fail."""
	operation = current_operation()
	if operation is None:
		return check()
	completed_result: _Result | None = None
	try:
		with operation.stage(
			check_id,
			scope,
			check=True,
			process_required=process_required,
		):
			result = check()
			completed_result = result
	except OperationTimeoutError as error:
		operation.record_result(
			completed_result
			if completed_result is not None
			else CheckResult(
				check_id,
				Status.UNCHECKED,
				f"operation deadline expired during {check_id}",
				recovery_action=(
					"inspect the current and last completed stages; restore the budget and retry"
				),
			)
		)
		raise error
	return operation.record_result(result)


def run_probe(
	command: list[str],
	*,
	cwd: Path | None = None,
	environment: dict[str, str] | None = None,
	timeout: float | None = None,
	payload: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
	"""Run a bounded readiness or Git probe on the active operation deadline."""
	operation = current_operation()
	if operation is None:
		return subprocess.run(
			command,
			cwd=cwd,
			env=environment,
			input=payload,
			capture_output=True,
			check=False,
			timeout=timeout,
		)
	if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
		raise ValueError("readiness probe timeout must be positive and finite")
	try:
		returncode, stdout, stderr = operation.run_process(
			command,
			root=cwd or operation.root,
			environment=dict(os.environ) if environment is None else environment,
			timeout_seconds=timeout,
			payload=payload,
			limit=MAX_PROBE_OUTPUT_BYTES,
			truncate=False,
			check_execution=False,
		)
	except EditError as error:
		if str(error) == "deadline":
			limit = timeout if timeout is not None else operation.command_timeout_seconds
			raise subprocess.TimeoutExpired(command, limit) from error
		raise OSError("readiness probe could not complete") from error
	return subprocess.CompletedProcess(command, returncode, stdout, stderr)


class GateOperation:
	"""Own one work clock and trusted temporary roots, without selecting checks or reuse."""

	def __init__(
		self,
		root: Path,
		mode: str,
		*,
		timeout_seconds: float = DEFAULT_GATE_TIMEOUT_SECONDS,
		cleanup_seconds: float = OPERATION_CLEANUP_SECONDS,
	) -> None:
		self.started = time.monotonic()
		if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
			raise ValueError("operation work budget must be positive and finite")
		if not math.isfinite(cleanup_seconds) or cleanup_seconds < 0:
			raise ValueError("operation cleanup budget must be non-negative and finite")
		self.root = root.resolve()
		self.mode = mode
		self.operation_id = uuid.uuid4().hex
		self.deadline = self.started + timeout_seconds
		self.cleanup_seconds = cleanup_seconds
		self.command_timeout_seconds = 120
		self.candidate = "unavailable"
		self.scope = "repository root and staged candidate preparation"
		self.results: list[CheckResult] = []
		self.stages: list[StageTiming] = []
		self.check_timings: dict[str, StageTiming] = {}
		self.complete = True
		self.timed_out = False
		self.current_stage: str | None = None
		self.last_completed_stage: str | None = None
		self.interrupted_stage: str | None = None
		self._cleanup_started: float | None = None
		self._cleanup_end: float | None = None
		self._cleanup_process_started = False
		self._work_process_cleanup_started = False
		self._finished: float | None = None
		self._cleaned = False
		self._bases: set[Path] = set()
		self._roots: set[tuple[Path, Path]] = set()
		self._cleanup_roots: set[Path] = set()
		self._native_cache_cleanup: dict[Path, bytes] = {}
		self._deferred_releases: list[Callable[[], None]] = []
		self._planned_checks: dict[str, str] = {}
		self._active_stages: list[_StageObservation] = []
		self._token: Token[GateOperation | None] | None = None

	def __enter__(self) -> GateOperation:
		self._token = _active.set(self)
		return self

	def __exit__(
		self,
		_kind: type[BaseException] | None,
		error: BaseException | None,
		_traceback: TracebackType | None,
	) -> bool:
		try:
			if error is not None:
				self.complete = False
			if isinstance(error, OperationTimeoutError):
				self._record_timeout(error)
			self.cleanup()
		finally:
			self._finished = time.monotonic()
			if self._token is not None:
				_active.reset(self._token)
		return isinstance(error, OperationTimeoutError)

	def configure_budget(self, seconds: float, command_seconds: int) -> None:
		"""Resolve the staged manifest's limits without renewing operation start."""
		if (
			not math.isfinite(seconds)
			or seconds <= 0
			or isinstance(command_seconds, bool)
			or not isinstance(command_seconds, int)
			or command_seconds <= 0
		):
			raise ValueError(
				"operation budget must be positive and command timeout must be a positive integer"
			)
		self.deadline = self.started + seconds
		self.command_timeout_seconds = command_seconds
		self.remaining()

	@property
	def finalized(self) -> bool:
		"""Whether operation cleanup and its elapsed-time endpoint were recorded."""
		return self._finished is not None

	def set_repository_root(self, root: Path) -> None:
		"""Bind a repository discovered under the original work deadline."""
		if self._roots or self._bases:
			raise RuntimeError("repository root cannot change after workspace registration")
		self.root = root.expanduser().resolve()

	def remaining(self, limit: float | None = None) -> float:
		remaining = self.deadline - time.monotonic()
		if remaining <= 0:
			self.timed_out = True
			self.interrupted_stage = self.current_stage
			raise OperationTimeoutError(
				f"operation exceeded its work deadline in {self.current_stage or 'preparation'}; "
				f"last completed stage: {self.last_completed_stage or 'none'}; "
				f"elapsed_ms={self._elapsed(self.started)}"
			)
		return min(remaining, limit) if limit is not None else remaining

	@contextmanager
	def stage(
		self,
		name: str,
		scope: str,
		*,
		check: bool = False,
		process_required: bool = False,
	) -> Iterator[None]:
		previous = self.current_stage
		self.current_stage = name
		started = time.monotonic()
		observation = _StageObservation(process_required, False, check)
		self._active_stages.append(observation)
		try:
			self.remaining()
			if not process_required:
				observation.process_started = True
			yield
			self.remaining()
			self.last_completed_stage = name
		finally:
			end = time.monotonic() if self._cleanup_started is None else self._cleanup_started
			self._active_stages.pop()
			selection = observation.cache_selection
			timing = StageTiming(
				name,
				max(0, int((end - started) * 1000)),
				scope[:MAX_TELEMETRY_TEXT],
				"executed" if observation.process_started else "not-run",
				reason=(
					"process did not start"
					if not observation.process_started
					else (selection.fallback_reason or "")[:MAX_TELEMETRY_TEXT]
					if selection is not None
					else ""
				),
				lookup_ms=max(0, int(selection.selection_duration_ms))
				if selection is not None
				else None,
				tool_cache=f"native-{selection.storage}"
				if selection is not None
				else "not-measured",
			)
			if check:
				self.check_timings[name] = timing
			else:
				self.stages.append(timing)
			self.current_stage = previous

	def attach_check_timing(self, result: _Result) -> _Result:
		"""Bind the observed operation timing to its stable check result."""
		timing = self.check_timings.get(result.check_id)
		return result if timing is None else replace(result, telemetry=timing)

	def record_native_cache_selection(self, selection: NativeToolCacheSelection) -> None:
		"""Attach namespace selection evidence to the active stage, without claiming a hit."""
		if not self._active_stages:
			raise RuntimeError("native cache observation requires an active stage")
		self._active_stages[-1].cache_selection = selection

	def record_result(self, result: _Result) -> _Result:
		"""Retain each completed result once, including its measured check timing."""
		observed = self.attach_check_timing(result)
		for index, existing in enumerate(self.results):
			if existing.check_id == observed.check_id:
				self.results[index] = observed
				return observed
		self.results.append(observed)
		return observed

	def annotate_tool_cache(self, stage: str, value: str) -> None:
		"""Record the observed cache outcome on an already completed stage."""
		for index in range(len(self.stages) - 1, -1, -1):
			if self.stages[index].stage == stage:
				self.stages[index] = replace(
					self.stages[index], tool_cache=value[:MAX_TELEMETRY_TEXT]
				)
				return

	def record_unmeasured_result(
		self, result: CheckResult, *, scope: str = "staged candidate"
	) -> CheckResult:
		"""Retain a result created by an exceptional or explicitly not-applicable path."""
		if result.check_id not in self.check_timings:
			self.check_timings[result.check_id] = StageTiming(
				result.check_id,
				0,
				scope[:MAX_TELEMETRY_TEXT],
				"not-run" if result.status is Status.NOT_APPLICABLE else "executed",
				reason=(
					result.summary
					if result.status is Status.NOT_APPLICABLE
					else "result produced without a timed stage"
				),
			)
		return self.record_result(result)

	def note_not_run(self, stage: str, scope: str, reason: str) -> None:
		"""Report an expected check that a completed prerequisite prevented."""
		if stage in self.check_timings:
			return
		timing = StageTiming(
			stage,
			0,
			scope[:MAX_TELEMETRY_TEXT],
			"not-run",
			reason=reason[:MAX_TELEMETRY_TEXT],
		)
		self.check_timings[stage] = timing
		self.record_result(
			CheckResult(
				stage,
				Status.UNCHECKED,
				reason[:MAX_TELEMETRY_TEXT],
				recovery_action="complete the prerequisite and rerun the affected checks",
			)
		)

	def plan_checks(self, checks: Iterable[tuple[str, str]]) -> None:
		"""Retain expected check scopes so an interrupted operation reports skipped work."""
		for check_id, scope in checks:
			self._planned_checks.setdefault(check_id, scope[:MAX_TELEMETRY_TEXT])

	def mark_incomplete(self, stage: str, reason: str) -> None:
		"""Record a prerequisite failure and the checks it prevented."""
		self.complete = False
		self.interrupted_stage = stage
		for check_id, scope in self._planned_checks.items():
			if check_id not in self.check_timings:
				self.note_not_run(check_id, scope, reason)

	def register_workspace(self, temp_root: Path, root: Path) -> None:
		"""Take cleanup ownership of a workspace handed off by temp_workspace."""
		from .temp_workspace import SYSTEM_TEMP_DIRECTORY

		actual_temp_root = temp_root.resolve()
		actual = root.resolve()
		repository_temp = (self.root / "temp").resolve()
		fallback_root = SYSTEM_TEMP_DIRECTORY.resolve()
		is_repository_temp = actual_temp_root == repository_temp
		is_system_temp = _is_system_workspace_temp(actual_temp_root, fallback_root)
		if (
			temp_root.is_symlink()
			or root.is_symlink()
			or actual_temp_root.is_symlink()
			or not (is_repository_temp or is_system_temp)
			or actual.parent != actual_temp_root
			or not actual.name.startswith("qg-")
			or not actual.is_dir()
			or actual == self.root
			or actual in self.root.parents
		):
			raise ValueError("cleanup root is not an owned temporary workspace")
		self._bases.add(actual)
		self._roots.add((actual_temp_root, actual))

	def handoff_workspace(self, temp_root: Path, root: Path) -> None:
		"""Confirm a workspace registered at allocation is left to operation cleanup."""
		pair = (temp_root.resolve(), root.resolve())
		if pair not in self._roots:
			raise ValueError("temporary workspace was not registered to this operation")

	def register_cleanup(self, root: Path) -> None:
		actual = root.resolve()
		temp_root = Path(tempfile.gettempdir()).resolve()
		inside_workspace = any(actual.is_relative_to(base) for base in self._bases)
		is_operation_staging = (
			actual.parent == temp_root
			and actual.name.startswith("quality-gate-")
			and actual.is_dir()
		)
		if (
			root.is_symlink()
			or actual.is_symlink()
			or not (inside_workspace or is_operation_staging)
			or actual == self.root
			or actual in self.root.parents
		):
			raise ValueError(
				"cleanup root is outside the operation-owned workspace or snapshot staging"
			)
		if actual in self._cleanup_roots:
			raise ValueError("cleanup root was registered more than once")
		self._cleanup_roots.add(actual)

	def register_native_cache_cleanup(self, payload: bytes) -> None:
		"""Register one serialized, ownership-checked native source tree cleanup."""
		if not isinstance(payload, bytes) or len(payload) > MAX_CLEANUP_INVENTORY_BYTES:
			raise ValueError("native cache cleanup registration exceeded its bound")
		try:
			data = json.loads(payload.decode("utf-8"))
			selection = data["selection"]
			cache_path = Path(selection["path"])
			if (
				data["schema"] != 1
				or not isinstance(selection, dict)
				or selection["tool"] not in {"ruff", "mypy"}
				or not cache_path.is_absolute()
				or cache_path.name != selection["namespace_id"]
			):
				raise ValueError("native cache cleanup registration is invalid")
		except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
			raise ValueError("native cache cleanup registration is invalid") from error
		actual = cache_path / "_s" / "c"
		if actual == self.root or actual in self.root.parents or self.root in actual.parents:
			raise ValueError("native cache cleanup root overlaps the operation repository")
		existing = self._native_cache_cleanup.get(actual)
		if existing is not None:
			if existing == payload:
				return
			raise ValueError("native cache cleanup root was registered more than once")
		if len(self._native_cache_cleanup) >= MAX_NATIVE_CACHE_CLEANUPS:
			raise ValueError("native cache cleanup inventory exceeded its bound")
		self._native_cache_cleanup[actual] = payload

	def defer_release(self, release: Callable[[], None]) -> None:
		"""Keep a cache lease held until the shared cleanup process has finished."""
		if not callable(release):
			raise TypeError("deferred release must be callable")
		if self._cleaned:
			release()
			return
		self._deferred_releases.append(release)

	def _begin_cleanup(self) -> float:
		if self._cleanup_end is None:
			self._cleanup_started = time.monotonic()
			self._cleanup_end = self._cleanup_started + self.cleanup_seconds
		return self._cleanup_end

	def _process_cleanup_deadline(self) -> float:
		now = time.monotonic()
		if now >= self.deadline:
			self._work_process_cleanup_started = True
			return self._begin_cleanup()
		return min(self.deadline, now + 1)

	def run_process(
		self,
		command: list[str],
		*,
		root: Path,
		environment: dict[str, str],
		timeout_seconds: float | None = None,
		payload: bytes | None = None,
		limit: int = MAX_TOOL_BYTES,
		truncate: bool = False,
		check_execution: bool = True,
	) -> tuple[int, bytes, bytes]:
		remaining = self.remaining(timeout_seconds or self.command_timeout_seconds)
		try:
			return supervise_process(
				command,
				root=root,
				environment=environment,
				deadline=min(self.deadline, time.monotonic() + remaining),
				payload=payload,
				limit=limit,
				own_tree=True,
				on_started=lambda: self._mark_process_started(check_execution=check_execution),
				cleanup_deadline=self._process_cleanup_deadline,
				truncate=truncate,
			)
		except EditError:
			self.remaining()
			raise

	def _mark_process_started(self, *, check_execution: bool = True) -> None:
		for observation in self._active_stages:
			if observation.process_required and (check_execution or not observation.check):
				observation.process_started = True

	def _record_timeout(self, error: OperationTimeoutError) -> None:
		self.complete = False
		self.timed_out = True
		self.mark_incomplete(
			self.interrupted_stage or "preparation",
			"operation deadline expired before this check started",
		)
		self.results.append(
			CheckResult(
				"runtime.gate_timeout",
				Status.UNCHECKED,
				str(error),
				recovery_action=(
					"inspect the current and last completed stages; restore the budget and retry"
				),
			)
		)

	def cleanup(self) -> None:
		if self._cleaned:
			return
		self._cleaned = True
		end = self._begin_cleanup()
		roots = sorted(self._roots, key=lambda item: (str(item[0]), len(item[1].parts)))
		cleanup_error: Exception | None = None
		try:
			if roots or self._cleanup_roots or self._native_cache_cleanup:
				self._remove_roots(roots, end)
		except Exception as error:
			cleanup_error = error
		finally:
			for release in reversed(self._deferred_releases):
				try:
					release()
				except Exception as error:
					cleanup_error = cleanup_error or error
			if cleanup_error is not None:
				self.complete = False
				self.interrupted_stage = self.interrupted_stage or "cleanup"
				self.results.append(
					CheckResult(
						"runtime.cleanup",
						Status.UNCHECKED,
						f"owned cleanup incomplete: {type(cleanup_error).__name__}",
						recovery_action=(
							"confirm owned workers stopped; remove leftover operation directories "
							"under temp or the reported private workspace, then retry verification"
						),
					)
				)
			self.stages.append(
				StageTiming(
					"cleanup",
					0 if self._cleanup_started is None else self._elapsed(self._cleanup_started),
					"operation-owned process trees, workspaces and native cache staging",
					"executed"
					if self._cleanup_process_started
					or self._work_process_cleanup_started
					or self._deferred_releases
					else "not-run",
					reason=(
						"one shared cleanup allowance"
						if self._cleanup_process_started or self._work_process_cleanup_started
						else "deferred cache leases released"
						if self._deferred_releases
						else "no owned roots"
						if not roots and not self._cleanup_roots and not self._native_cache_cleanup
						else "cleanup worker did not start"
					),
				)
			)

	def _remove_roots(self, roots: list[tuple[Path, Path]], end: float) -> None:
		payload = json.dumps(
			{
				"workspaces": [[str(temp_root), str(root)] for temp_root, root in roots],
				"snapshots": [str(root) for root in sorted(self._cleanup_roots, key=str)],
				"native_cache": [
					json.loads(self._native_cache_cleanup[path].decode("utf-8"))
					for path in sorted(self._native_cache_cleanup, key=str)
				],
			}
		).encode("utf-8")
		if len(payload) > MAX_CLEANUP_INVENTORY_BYTES:
			raise ValueError("owned cleanup inventory exceeded its bound")
		reserve = min(1.0, self.cleanup_seconds / 2)
		code, _output, _diagnostics = supervise_process(
			[
				sys.executable,
				"-I",
				"-S",
				"-B",
				"-c",
				_CLEANUP_BODY,
				str(MAX_CLEANUP_INVENTORY_BYTES),
				str(Path(__file__).resolve().parent.parent),
			],
			root=self.root,
			environment=dict(os.environ),
			deadline=end - reserve,
			payload=payload,
			limit=4096,
			own_tree=True,
			on_started=lambda: setattr(self, "_cleanup_process_started", True),
			cleanup_deadline=end,
		)
		if (
			code != 0
			or any(root.exists() for _temp_root, root in roots)
			or any(root.exists() for root in self._cleanup_roots)
			or any(root.exists() or root.is_symlink() for root in self._native_cache_cleanup)
		):
			raise OSError("owned temporary workspaces were not safely removed")

	@staticmethod
	def _elapsed(started: float) -> int:
		return max(0, int((time.monotonic() - started) * 1000))

	def verdict(self) -> Verdict:
		if self._finished is None:
			raise RuntimeError("operation report requires completed owned cleanup")
		return Verdict(
			tuple(self.results),
			operation=OperationTelemetry(
				operation_id=self.operation_id,
				operation=self.mode,
				profile=self.mode,
				candidate=self.candidate,
				elapsed_ms=max(0, int((self._finished - self.started) * 1000)),
				stages=tuple(self.stages),
				complete=self.complete,
				current_stage=self.interrupted_stage,
				last_completed_stage=self.last_completed_stage,
				timed_out=self.timed_out,
				scope=self.scope,
			),
		)
