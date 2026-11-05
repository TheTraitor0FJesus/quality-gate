"""Manifest and verdict data models and safe output redaction."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

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

	def __post_init__(self) -> None:
		ids = [result.check_id for result in self.results]
		if len(set(ids)) != len(ids):
			raise ValueError("check ids must be unique within a verdict")

	@property
	def exit_code(self) -> int:
		statuses = {result.status for result in self.results}
		if Status.UNCHECKED in statuses:
			return 2
		return 1 if Status.FAILED in statuses else 0


def fingerprint_secret(value: str) -> str:
	return hashlib.sha256(value.encode("utf-8")).hexdigest()


def redact(text: str, secrets: Iterable[str] = ()) -> str:
	result = text
	tokens = {secret for secret in secrets if secret}
	for secret in tuple(tokens):
		tokens.update(secret[:length] for length in range(8, len(secret) + 1))
	for token in sorted(tokens, key=len, reverse=True):
		result = result.replace(token, "<REDACTED>")
	for pattern in (
		r"(?i)(?:ghp_|github_pat_|sk-|xox[baprs]-)[A-Za-z0-9_-]{8,}",
		r"(?i)(?:password|passwd|token|secret|api[_-]?key)\s*[=:]\s*[^\s,;]+",
	):
		result = re.sub(pattern, "<REDACTED>", result)
	return result
