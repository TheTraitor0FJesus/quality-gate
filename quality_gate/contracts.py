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
DEFAULT_JAVASCRIPT_FILE_KIB = 100
DEFAULT_CSS_FILE_KIB = 50
DEFAULT_JAVASCRIPT_TOTAL_KIB = 250
DEFAULT_CSS_TOTAL_KIB = 100
MAX_FINDINGS_PER_CHECK = 20
MAX_FINDINGS_TOTAL = 100
MAX_SUPPLEMENTAL_TESTS = 32
MAX_SUPPLEMENTAL_TARGET_PATTERNS = 32
MAX_SUPPLEMENTAL_TARGET_LENGTH = 256
MAX_SUPPLEMENTAL_TARGET_MATCHES = 1024
MAX_SUPPLEMENTAL_TARGET_BYTES = 64 * 1024
MAX_SUPPLEMENTAL_TOTAL_MATCHES = 4096
MAX_SUPPLEMENTAL_TOTAL_BYTES = 256 * 1024
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


def _ensure_unique(values: Iterable[str], path: str, message: str) -> None:
	values = tuple(values)
	if len(set(values)) != len(values):
		raise ValidationError(path, message)


def _relative_path(value: object, path: str) -> str:
	result = _string(value, path).replace("\\", "/")
	candidate = Path(result)
	if (
		"\x00" in result
		or ":" in result
		or candidate.is_absolute()
		or bool(candidate.drive)
		or result == "."
		or any(part in {".", ".."} for part in candidate.parts)
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


def _asset_pattern(value: object, path: str, suffix: str | None = None) -> str:
	pattern = _relative_path(value, path)
	parts = pattern.split("/")
	if "\x00" in pattern or ":" in pattern or "{" in pattern or "}" in pattern:
		raise ValidationError(path, "must be a safe repository-relative glob pattern")
	if any(part in {"", "."} for part in parts):
		raise ValidationError(path, "must not contain empty or current-directory segments")
	if pattern.count("[") != pattern.count("]"):
		raise ValidationError(path, "contains an unbalanced character class")
	if any("**" in part and part != "**" for part in parts):
		raise ValidationError(path, "may use ** only as a complete path segment")
	if suffix is not None and not pattern.casefold().endswith(suffix):
		raise ValidationError(path, f"must select only {suffix} assets")
	return pattern


def _pattern_list(value: object, path: str, suffix: str | None = None) -> tuple[str, ...]:
	patterns = tuple(
		_asset_pattern(item, f"{path}[{index}]", suffix)
		for index, item in enumerate(_list(value, path))
	)
	if len(set(patterns)) != len(patterns):
		raise ValidationError(path, "must contain unique patterns")
	return patterns


def _test_target(value: object, path: str) -> str:
	target = _asset_pattern(value, path)
	if len(target.encode("utf-8")) > MAX_SUPPLEMENTAL_TARGET_LENGTH:
		raise ValidationError(
			path, f"must use at most {MAX_SUPPLEMENTAL_TARGET_LENGTH} UTF-8 bytes"
		)
	if target.startswith("-"):
		raise ValidationError(path, "must not begin with a command-line option")
	return target


def _test_target_list(value: object, path: str) -> tuple[str, ...]:
	targets = tuple(
		_test_target(item, f"{path}[{index}]") for index, item in enumerate(_list(value, path))
	)
	if not targets:
		raise ValidationError(path, "must declare at least one target")
	if len(targets) > MAX_SUPPLEMENTAL_TARGET_PATTERNS:
		raise ValidationError(
			path, f"must contain at most {MAX_SUPPLEMENTAL_TARGET_PATTERNS} targets"
		)
	_ensure_unique(targets, path, "must contain unique targets")
	return targets


def _parse_supplemental_test(value: object, index: int) -> SupplementalTest:
	path = f"supplemental_tests[{index}]"
	table = _table(value, path)
	_keys(table, {"name", "runner", "targets"}, path)
	name = _string(table.get("name"), f"{path}.name")
	if not _CHECK_ID.fullmatch(name):
		raise ValidationError(f"{path}.name", "must be a stable lowercase identifier")
	runner = _string(table.get("runner"), f"{path}.runner")
	if runner != "node-test":
		raise ValidationError(f"{path}.runner", "must be 'node-test'")
	return SupplementalTest(
		name,
		runner,
		_test_target_list(table.get("targets"), f"{path}.targets"),
	)


def _parse_web_component(value: object, index: int) -> WebComponent:
	path = f"web[{index}]"
	table = _table(value, path)
	_keys(table, {"name", "root", "javascript", "css", "exclude", "limits"}, path)
	root = _relative_path(table.get("root"), f"{path}.root")
	if _WILDCARD.search(root) or any(part in {"", "."} for part in root.split("/")):
		raise ValidationError(
			f"{path}.root", "must not contain wildcards, empty, or current-directory segments"
		)
	javascript = _pattern_list(table.get("javascript", []), f"{path}.javascript", ".js")
	css = _pattern_list(table.get("css", []), f"{path}.css", ".css")
	if not javascript and not css:
		raise ValidationError(path, "must declare JavaScript or CSS assets")
	limits = _table(table.get("limits", {}), f"{path}.limits")
	_keys(
		limits,
		{
			"javascript_file_kib",
			"css_file_kib",
			"javascript_total_kib",
			"css_total_kib",
		},
		f"{path}.limits",
	)
	return WebComponent(
		name=_string(table.get("name"), f"{path}.name"),
		root=root,
		javascript=javascript,
		css=css,
		exclude=_pattern_list(table.get("exclude", []), f"{path}.exclude"),
		javascript_file_kib=_positive_int(
			limits.get("javascript_file_kib", DEFAULT_JAVASCRIPT_FILE_KIB),
			f"{path}.limits.javascript_file_kib",
		),
		css_file_kib=_positive_int(
			limits.get("css_file_kib", DEFAULT_CSS_FILE_KIB),
			f"{path}.limits.css_file_kib",
		),
		javascript_total_kib=_positive_int(
			limits.get("javascript_total_kib", DEFAULT_JAVASCRIPT_TOTAL_KIB),
			f"{path}.limits.javascript_total_kib",
		),
		css_total_kib=_positive_int(
			limits.get("css_total_kib", DEFAULT_CSS_TOTAL_KIB),
			f"{path}.limits.css_total_kib",
		),
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


def _parse_supplemental_tests(root: Mapping[str, object]) -> tuple[SupplementalTest, ...]:
	supplemental_tests = tuple(
		_parse_supplemental_test(value, index)
		for index, value in enumerate(
			_list(root.get("supplemental_tests", []), "supplemental_tests")
		)
	)
	if len(supplemental_tests) > MAX_SUPPLEMENTAL_TESTS:
		raise ValidationError(
			"supplemental_tests", f"must contain at most {MAX_SUPPLEMENTAL_TESTS} declarations"
		)
	_ensure_unique(
		(item.name.casefold() for item in supplemental_tests),
		"supplemental_tests",
		"duplicate test name",
	)
	return supplemental_tests


def _load_table(raw: Mapping[str, object]) -> Manifest:
	root = _table(raw, "manifest")
	_keys(
		root,
		{"quality", "repository", "python", "web", "supplemental_tests", "waivers"},
		"manifest",
	)
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
	if "repository" not in domains or set(domains) - {"repository", "python", "web"}:
		raise ValidationError(
			"repository.domains", "must contain only repository and optional python or web"
		)
	if len(set(domains)) != len(domains):
		raise ValidationError("repository.domains", "must contain unique domains")
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
	web_components = tuple(
		_parse_web_component(value, index)
		for index, value in enumerate(_list(root.get("web", []), "web"))
	)
	if ("web" in domains) != bool(web_components):
		raise ValidationError("repository.domains", "web domain must match the component list")
	_ensure_unique(
		(component.name.casefold() for component in web_components),
		"web",
		"duplicate component name",
	)
	supplemental_tests = _parse_supplemental_tests(root)
	waivers = tuple(
		_parse_waiver(value, index)
		for index, value in enumerate(_list(root.get("waivers", []), "waivers"))
	)
	keys = [(item.kind, item.check_id, item.target, item.fingerprint) for item in waivers]
	if len(set(keys)) != len(keys):
		raise ValidationError("waivers", "duplicate waiver")
	return Manifest(
		policy_release, contract, components, web_components, supplemental_tests, waivers
	)


def _validate_supplemental_match(match: Path, root: Path, path: str) -> int:
	try:
		resolved_match = match.resolve()
	except OSError as exc:
		raise ValidationError(path, "must resolve inside the repository") from exc
	if resolved_match != root and root not in resolved_match.parents:
		raise ValidationError(path, "must match only paths inside the repository")
	return len(resolved_match.relative_to(root).as_posix().encode("utf-8"))


def _validate_supplemental_target(root: Path, target: str, path: str) -> tuple[int, int]:
	try:
		matches: list[Path] = []
		for match in root.glob(target):
			if len(matches) >= MAX_SUPPLEMENTAL_TARGET_MATCHES:
				raise ValidationError(
					path,
					f"must match at most {MAX_SUPPLEMENTAL_TARGET_MATCHES} repository paths",
				)
			matches.append(match)
	except ValidationError:
		raise
	except (OSError, ValueError) as exc:
		raise ValidationError(path, "must be a valid repository-relative target") from exc
	if not matches:
		raise ValidationError(path, "must match at least one repository path")
	matched_bytes = sum(_validate_supplemental_match(match, root, path) for match in matches)
	if matched_bytes > MAX_SUPPLEMENTAL_TARGET_BYTES:
		raise ValidationError(
			path, f"matched paths must use at most {MAX_SUPPLEMENTAL_TARGET_BYTES} bytes"
		)
	return len(matches), matched_bytes


def _validate_supplemental_targets(root: Path, tests: tuple[SupplementalTest, ...]) -> None:
	try:
		resolved_root = root.resolve()
	except OSError as exc:
		raise ValidationError("manifest", "repository root cannot be resolved") from exc
	total_matches = 0
	total_bytes = 0
	for test_index, supplemental_test in enumerate(tests):
		for target_index, target in enumerate(supplemental_test.targets):
			path = f"supplemental_tests[{test_index}].targets[{target_index}]"
			match_count, matched_bytes = _validate_supplemental_target(resolved_root, target, path)
			total_matches += match_count
			total_bytes += matched_bytes
			if total_matches > MAX_SUPPLEMENTAL_TOTAL_MATCHES:
				raise ValidationError(
					path,
					f"all targets must match at most {MAX_SUPPLEMENTAL_TOTAL_MATCHES} paths",
				)
			if total_bytes > MAX_SUPPLEMENTAL_TOTAL_BYTES:
				raise ValidationError(
					path,
					f"all matched paths must use at most {MAX_SUPPLEMENTAL_TOTAL_BYTES} bytes",
				)


def load_manifest(root: Path | str = ".", manifest_name: str = MANIFEST_NAME) -> Manifest:
	path = Path(root).resolve() / manifest_name
	try:
		raw = tomllib.loads(path.read_text(encoding="utf-8"))
	except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
		raise ValidationError("manifest", "cannot read valid UTF-8 TOML") from exc
	manifest = _load_table(raw)
	_validate_supplemental_targets(path.parent, manifest.supplemental_tests)
	return manifest


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
