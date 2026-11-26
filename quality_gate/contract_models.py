"""Manifest and verdict data models."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Literal

from .output_redaction import redact as redact

MAX_TELEMETRY_TEXT = 512
_CHECK_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_WILDCARD = re.compile(r"[*?\[\]{}]")


class Status(StrEnum):
	PASSED = "passed"
	FAILED = "failed"
	UNCHECKED = "unchecked"
	NOT_APPLICABLE = "not_applicable"
	WAIVED = "waived"


@dataclass(frozen=True, slots=True)
class RepositoryContract:
	name: str
	domains: tuple[str, ...]
	required_documents: tuple[str, ...]
	max_blob_size_mib: int
	command_timeout_seconds: int
	test_timeout_seconds: int
	gate_timeout_seconds: int


@dataclass(frozen=True, slots=True)
class PythonComponent:
	name: str
	path: str
	python_version: str
	dependency_inputs: tuple[str, ...]
	test_paths: tuple[str, ...]
	tests_applicable: bool
	tests_reason: str | None
	timeout_seconds: int
	typecheck: bool = True
	stub_roots: tuple[str, ...] = ()
	source_roots: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WebComponent:
	name: str
	root: str
	javascript: tuple[str, ...]
	css: tuple[str, ...]
	exclude: tuple[str, ...]
	javascript_file_kib: int
	css_file_kib: int
	javascript_total_kib: int
	css_total_kib: int


@dataclass(frozen=True, slots=True)
class SupplementalTest:
	name: str
	runner: str
	targets: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Waiver:
	kind: str
	check_id: str
	target: str
	reason: str
	approved_by: str
	reviewed_on: date
	expires_on: date
	fingerprint: str | None

	def is_current(self, today: date | None = None) -> bool:
		current_day = today or date.today()
		return self.reviewed_on <= current_day <= self.expires_on

	def matches(self, check_id: str, target: str, fingerprint: str | None = None) -> bool:
		if self.check_id != check_id or self.target != target or not self.is_current():
			return False
		return self.fingerprint == fingerprint


@dataclass(frozen=True, slots=True)
class Manifest:
	policy_release: str
	repository: RepositoryContract
	python: tuple[PythonComponent, ...]
	web: tuple[WebComponent, ...]
	supplemental_tests: tuple[SupplementalTest, ...]
	waivers: tuple[Waiver, ...]

	def resolve_waiver(
		self, check_id: str, target: str, fingerprint: str | None = None
	) -> Waiver | None:
		for waiver in self.waivers:
			if waiver.matches(check_id, target, fingerprint):
				return waiver
		return None


@dataclass(frozen=True, slots=True)
class Finding:
	path: str = ""
	line: int | None = None
	message: str = ""
	action: str = ""

	def sort_key(self) -> tuple[str, int, str, str]:
		return self.path, self.line or 0, self.message, self.action


def _telemetry_duration(value: int | None, *, required: bool = False) -> None:
	if (required and value is None) or (
		value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0)
	):
		raise ValueError("telemetry duration must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class StageTiming:
	"""Observed cost and disposition; never an eligibility claim for stored results."""

	stage: str
	elapsed_ms: int
	scope: str
	disposition: Literal["executed", "reused", "not-run"]
	reason: str = ""
	lookup_ms: int | None = None
	validation_ms: int | None = None
	original_elapsed_ms: int | None = None
	original_operation_id: str | None = None
	tool_cache: str = "not-measured"
	command_ms: int | None = None

	def __post_init__(self) -> None:
		_telemetry_duration(self.elapsed_ms, required=True)
		for duration in (
			self.lookup_ms,
			self.validation_ms,
			self.original_elapsed_ms,
			self.command_ms,
		):
			_telemetry_duration(duration)
		if self.disposition not in {"executed", "reused", "not-run"}:
			raise ValueError("invalid execution disposition")
		if self.disposition == "reused" and (
			self.original_elapsed_ms is None or not self.original_operation_id
		):
			raise ValueError("reused telemetry requires original execution metadata")
		for value in (
			self.stage,
			self.scope,
			self.reason,
			self.original_operation_id,
			self.tool_cache,
		):
			if value is not None and (
				not isinstance(value, str) or len(value) > MAX_TELEMETRY_TEXT
			):
				raise ValueError("telemetry text must be a bounded string")


@dataclass(frozen=True, slots=True)
class OperationTelemetry:
	"""One operation, reported only after work and owned cleanup have settled."""

	operation_id: str
	operation: str
	profile: str
	candidate: str
	elapsed_ms: int
	stages: tuple[StageTiming, ...] = ()
	complete: bool = True
	current_stage: str | None = None
	last_completed_stage: str | None = None
	timed_out: bool = False
	scope: str = "staged"
	admission_wait_ms: int | None = None

	def __post_init__(self) -> None:
		_telemetry_duration(self.elapsed_ms, required=True)
		_telemetry_duration(self.admission_wait_ms)
		for value in (
			self.operation_id,
			self.operation,
			self.profile,
			self.candidate,
			self.scope,
			self.current_stage,
			self.last_completed_stage,
		):
			if value is not None and (
				not isinstance(value, str) or len(value) > MAX_TELEMETRY_TEXT
			):
				raise ValueError("operation telemetry text must be a bounded string")
		if self.timed_out and self.complete:
			raise ValueError("a timed-out operation cannot be complete")


@dataclass(frozen=True, slots=True)
class CheckResult:
	check_id: str
	status: Status
	summary: str
	findings: tuple[Finding, ...] = ()
	recovery_action: str | None = None
	waiver_target: str | None = None
	_secrets: tuple[str, ...] = field(default=(), repr=False, compare=False)
	omitted_findings: int | None = 0
	telemetry: StageTiming | None = field(default=None, compare=False)
	profile_details: tuple[str, ...] = field(default=(), repr=False, compare=False)
	profile_outcome: Status | None = field(default=None, compare=False)

	def __post_init__(self) -> None:
		if not _CHECK_ID.fullmatch(self.check_id):
			raise ValueError(f"invalid check id: {self.check_id!r}")
		if not self.summary.strip():
			raise ValueError("check summaries must not be empty")
		if self.omitted_findings is not None and (
			not isinstance(self.omitted_findings, int)
			or isinstance(self.omitted_findings, bool)
			or self.omitted_findings < 0
		):
			raise ValueError("omitted finding count must be a non-negative integer or unknown")
		if (
			self.status in {Status.FAILED, Status.UNCHECKED, Status.NOT_APPLICABLE}
			and not self.recovery_action
		):
			raise ValueError(f"{self.status.value} results require a recovery action")
		if self.status is Status.WAIVED and (
			not self.waiver_target
			or not self.waiver_target.strip()
			or _WILDCARD.search(self.waiver_target)
		):
			raise ValueError("waived results require one exact target")
		if self.status is Status.PASSED and self.findings:
			raise ValueError("passed results cannot contain findings")
		if self.status is Status.PASSED and self.omitted_findings != 0:
			raise ValueError("passed results cannot contain omitted findings")

	@property
	def ordered_findings(self) -> tuple[Finding, ...]:
		return tuple(sorted(self.findings, key=Finding.sort_key))


@dataclass(frozen=True, slots=True)
class Verdict:
	results: tuple[CheckResult, ...]
	operation: OperationTelemetry | None = field(default=None, compare=False)
	admission_wait_ms: int | None = field(default=None, compare=False)

	def __post_init__(self) -> None:
		_telemetry_duration(self.admission_wait_ms)
		ids = [result.check_id for result in self.results]
		if len(set(ids)) != len(ids):
			raise ValueError("check ids must be unique within a verdict")

	@property
	def exit_code(self) -> int:
		statuses = {result.status for result in self.results}
		if Status.UNCHECKED in statuses or (
			self.operation is not None and not self.operation.complete
		):
			return 2
		return 1 if Status.FAILED in statuses else 0


def fingerprint_secret(value: str) -> str:
	return hashlib.sha256(value.encode("utf-8")).hexdigest()
