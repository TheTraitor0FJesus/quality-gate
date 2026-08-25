"""Validated schema 2, verdict, finding, and typed-waiver contracts."""

from __future__ import annotations

import hashlib
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import cast

MANIFEST_NAME = "quality-gate.toml"
SCHEMA_VERSION = 2
DEFAULT_MAX_BLOB_SIZE_MIB = 5
DEFAULT_COMMAND_TIMEOUT_SECONDS = 120
DEFAULT_TEST_TIMEOUT_SECONDS = 300
DEFAULT_GATE_TIMEOUT_SECONDS = 600
MAX_FINDINGS_PER_CHECK = 20
MAX_FINDINGS_TOTAL = 100
_CHECK_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_WILDCARD = re.compile(r"[*?\[\]{}]")
_HUMAN_BLOCKLIST = {"bot", "automation", "automated", "dependabot", "github-actions"}


class Status(StrEnum):
	PASSED = "passed"
	FAILED = "failed"
	UNCHECKED = "unchecked"
	NOT_APPLICABLE = "not_applicable"
	WAIVED = "waived"


class ValidationError(ValueError):
	"""A safe, path-aware contract validation error."""

	def __init__(self, path: str, message: str) -> None:
		self.path = path
		self.message = message
		super().__init__(f"{path}: {message}")


def _type(value: object, expected: type | tuple[type, ...], path: str) -> object:
	allows_bool = expected is bool or (isinstance(expected, tuple) and bool in expected)
	if not isinstance(value, expected) or (isinstance(value, bool) and not allows_bool):
		names = (
			", ".join(t.__name__ for t in expected)
			if isinstance(expected, tuple)
			else expected.__name__
		)
		raise ValidationError(path, f"expected {names}")
	return value


def _table(value: object, path: str) -> dict[str, object]:
	return cast(dict[str, object], _type(value, dict, path))


def _keys(table: Mapping[str, object], allowed: set[str], path: str) -> None:
	unknown = sorted(set(table) - allowed)
	if unknown:
		raise ValidationError(path, f"unknown field {unknown[0]!r}")


def _string(value: object, path: str) -> str:
	result = cast(str, _type(value, str, path))
	if not result.strip():
		raise ValidationError(path, "must not be empty")
	return result


def _list(value: object, path: str) -> list[object]:
	return cast(list[object], _type(value, list, path))


def _relative_path(value: object, path: str) -> str:
	result = _string(value, path).replace("\\", "/")
	candidate = Path(result)
	if (
		candidate.is_absolute()
		or result == "."
		or result.startswith("../")
		or "/../" in f"/{result}"
	):
		raise ValidationError(path, "must be a repository-relative path")
	return result


def _positive_int(value: object, path: str) -> int:
	result = cast(int, _type(value, int, path))
	if result <= 0:
		raise ValidationError(path, "must be greater than zero")
	return result


def _date(value: object, path: str) -> date:
	raw = _string(value, path)
	try:
		return date.fromisoformat(raw)
	except ValueError as exc:
		raise ValidationError(path, "must use YYYY-MM-DD") from exc


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

	def __post_init__(self) -> None:
		if not _CHECK_ID.fullmatch(self.check_id):
			raise ValueError(f"invalid check id: {self.check_id!r}")
		if not self.summary.strip():
			raise ValueError("check summaries must not be empty")
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


def _parse_component(value: object, index: int) -> PythonComponent:
	path = f"python[{index}]"
	table = _table(value, path)
	_keys(
		table,
		{
			"name",
			"path",
			"python_version",
			"dependency_inputs",
			"test_paths",
			"tests_applicable",
			"tests_reason",
			"timeout_seconds",
		},
		path,
	)
	tests_applicable = cast(
		bool, _type(table.get("tests_applicable"), bool, f"{path}.tests_applicable")
	)
	tests_reason = table.get("tests_reason")
	test_paths = _list(table.get("test_paths"), f"{path}.test_paths")
	if tests_applicable and tests_reason is not None:
		raise ValidationError(
			f"{path}.tests_reason", "is only valid when tests_applicable is false"
		)
	if not tests_applicable and not tests_reason:
		raise ValidationError(f"{path}.tests_reason", "is required when tests are not applicable")
	if tests_applicable and not test_paths:
		raise ValidationError(
			f"{path}.test_paths", "must declare at least one path when tests apply"
		)
	if not tests_applicable and test_paths:
		raise ValidationError(f"{path}.test_paths", "must be empty when tests do not apply")
	return PythonComponent(
		name=_string(table.get("name"), f"{path}.name"),
		path=_relative_path(table.get("path"), f"{path}.path"),
		python_version=_string(table.get("python_version"), f"{path}.python_version"),
		dependency_inputs=tuple(
			_relative_path(item, f"{path}.dependency_inputs[{item_index}]")
			for item_index, item in enumerate(
				_list(table.get("dependency_inputs"), f"{path}.dependency_inputs")
			)
		),
		test_paths=tuple(
			_relative_path(item, f"{path}.test_paths[{item_index}]")
			for item_index, item in enumerate(test_paths)
		),
		tests_applicable=tests_applicable,
		tests_reason=_string(tests_reason, f"{path}.tests_reason")
		if tests_reason is not None
		else None,
		timeout_seconds=_positive_int(table.get("timeout_seconds", 300), f"{path}.timeout_seconds"),
	)


def _parse_waiver(value: object, index: int) -> Waiver:
	path = f"waivers[{index}]"
	table = _table(value, path)
	_keys(
		table,
		{
			"kind",
			"check_id",
			"target",
			"reason",
			"approved_by",
			"reviewed_on",
			"expires_on",
			"fingerprint",
		},
		path,
	)
	kind = _string(table.get("kind"), f"{path}.kind")
	if kind not in {"standard", "secret"}:
		raise ValidationError(f"{path}.kind", "must be 'standard' or 'secret'")
	check_id = _string(table.get("check_id"), f"{path}.check_id")
	if not _CHECK_ID.fullmatch(check_id):
		raise ValidationError(f"{path}.check_id", "must be a stable lowercase check id")
	target = _string(table.get("target"), f"{path}.target")
	if _WILDCARD.search(target):
		raise ValidationError(f"{path}.target", "must identify one exact target")
	approved_by = _string(table.get("approved_by"), f"{path}.approved_by")
	approver = approved_by.casefold().split("@", 1)[0]
	if approved_by.casefold() in _HUMAN_BLOCKLIST or any(
		token in approver for token in _HUMAN_BLOCKLIST
	):
		raise ValidationError(f"{path}.approved_by", "must identify a human reviewer")
	reviewed_on = _date(table.get("reviewed_on"), f"{path}.reviewed_on")
	expires_on = _date(table.get("expires_on"), f"{path}.expires_on")
	today = date.today()
	if reviewed_on > today or expires_on < today or expires_on < reviewed_on:
		raise ValidationError(f"{path}.expires_on", "must be current and on or after review date")
	fingerprint = table.get("fingerprint")
	if kind == "secret":
		fingerprint_value = _string(fingerprint, f"{path}.fingerprint")
		if not _FINGERPRINT.fullmatch(fingerprint_value):
			raise ValidationError(
				f"{path}.fingerprint", "secret waivers require a SHA-256 fingerprint"
			)
		fingerprint = fingerprint_value
	elif fingerprint is not None:
		raise ValidationError(f"{path}.fingerprint", "is only valid for secret waivers")
	return Waiver(
		kind,
		check_id,
		target,
		_string(table.get("reason"), f"{path}.reason"),
		approved_by,
		reviewed_on,
		expires_on,
		cast(str | None, fingerprint),
	)


def _load_table(raw: Mapping[str, object]) -> Manifest:
	root = _table(raw, "manifest")
	_keys(root, {"quality", "repository", "python", "waivers"}, "manifest")
	quality = _table(root.get("quality"), "quality")
	if quality.get("schema") != SCHEMA_VERSION:
		raise ValidationError(
			"quality.schema", f"must be {SCHEMA_VERSION}; run migrate for schema 1"
		)
	_keys(quality, {"schema", "policy_release"}, "quality")
	policy_release = _string(quality.get("policy_release"), "quality.policy_release")
	if not re.fullmatch(r"v\d+\.\d+\.\d+", policy_release):
		raise ValidationError(
			"quality.policy_release", "must be an immutable release such as v2.0.0"
		)
	repository = _table(root.get("repository"), "repository")
	_keys(repository, {"name", "domains", "required_documents", "limits", "defaults"}, "repository")
	domains = tuple(
		_string(value, f"repository.domains[{index}]")
		for index, value in enumerate(_list(repository.get("domains"), "repository.domains"))
	)
	if "repository" not in domains or set(domains) - {"repository", "python"}:
		raise ValidationError(
			"repository.domains", "must contain only repository and optional python"
		)
	documents = tuple(
		_relative_path(value, f"repository.required_documents[{index}]")
		for index, value in enumerate(
			_list(repository.get("required_documents"), "repository.required_documents")
		)
	)
	if not documents or len(set(documents)) != len(documents):
		raise ValidationError(
			"repository.required_documents", "must contain unique non-empty paths"
		)
	limits = _table(repository.get("limits", {}), "repository.limits")
	_keys(limits, {"max_blob_size_mib"}, "repository.limits")
	defaults = _table(repository.get("defaults", {}), "repository.defaults")
	_keys(
		defaults,
		{"command_timeout_seconds", "test_timeout_seconds", "gate_timeout_seconds"},
		"repository.defaults",
	)
	contract = RepositoryContract(
		name=_string(repository.get("name"), "repository.name"),
		domains=domains,
		required_documents=documents,
		max_blob_size_mib=_positive_int(
			limits.get("max_blob_size_mib", DEFAULT_MAX_BLOB_SIZE_MIB),
			"repository.limits.max_blob_size_mib",
		),
		command_timeout_seconds=_positive_int(
			defaults.get("command_timeout_seconds", DEFAULT_COMMAND_TIMEOUT_SECONDS),
			"repository.defaults.command_timeout_seconds",
		),
		test_timeout_seconds=_positive_int(
			defaults.get("test_timeout_seconds", DEFAULT_TEST_TIMEOUT_SECONDS),
			"repository.defaults.test_timeout_seconds",
		),
		gate_timeout_seconds=_positive_int(
			defaults.get("gate_timeout_seconds", DEFAULT_GATE_TIMEOUT_SECONDS),
			"repository.defaults.gate_timeout_seconds",
		),
	)
	components = tuple(
		_parse_component(value, index)
		for index, value in enumerate(_list(root.get("python", []), "python"))
	)
	if ("python" in domains) != bool(components):
		raise ValidationError("repository.domains", "python domain must match the component list")
	waivers = tuple(
		_parse_waiver(value, index)
		for index, value in enumerate(_list(root.get("waivers", []), "waivers"))
	)
	keys = [(item.kind, item.check_id, item.target, item.fingerprint) for item in waivers]
	if len(set(keys)) != len(keys):
		raise ValidationError("waivers", "duplicate waiver")
	return Manifest(policy_release, contract, components, waivers)


def load_manifest(root: Path | str = ".", manifest_name: str = MANIFEST_NAME) -> Manifest:
	path = Path(root).resolve() / manifest_name
	try:
		raw = tomllib.loads(path.read_text(encoding="utf-8"))
	except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
		raise ValidationError("manifest", "cannot read valid UTF-8 TOML") from exc
	return _load_table(raw)


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
