"""Schema and waiver contracts."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import cast

from .contract_inputs import (
	MAX_COMPONENT_SOURCE_ROOTS as MAX_COMPONENT_SOURCE_ROOTS,
)
from .contract_inputs import (
	MAX_COMPONENT_STUB_ROOTS as MAX_COMPONENT_STUB_ROOTS,
)
from .contract_inputs import (
	_component_source_roots,
	_component_stub_roots,
	_date,
	_ensure_unique,
	_keys,
	_list,
	_positive_int,
	_relative_path,
	_string,
	_table,
	_type,
)
from .contract_models import (
	_CHECK_ID,
	Manifest,
	PythonComponent,
	RepositoryContract,
	SupplementalTest,
	Waiver,
	WebComponent,
)
from .contract_models import (
	CheckResult as CheckResult,
)
from .contract_models import (
	Finding as Finding,
)
from .contract_models import (
	Status as Status,
)
from .contract_models import (
	Verdict as Verdict,
)
from .contract_models import (
	fingerprint_secret as fingerprint_secret,
)
from .contract_models import (
	redact as redact,
)
from .contract_supplemental import (
	MAX_SUPPLEMENTAL_TARGET_BYTES as MAX_SUPPLEMENTAL_TARGET_BYTES,
)
from .contract_supplemental import (
	MAX_SUPPLEMENTAL_TARGET_MATCHES as MAX_SUPPLEMENTAL_TARGET_MATCHES,
)
from .contract_supplemental import (
	MAX_SUPPLEMENTAL_TOTAL_BYTES as MAX_SUPPLEMENTAL_TOTAL_BYTES,
)
from .contract_supplemental import (
	MAX_SUPPLEMENTAL_TOTAL_MATCHES as MAX_SUPPLEMENTAL_TOTAL_MATCHES,
)
from .contract_supplemental import (
	ValidationError,
	_validate_supplemental_targets,
)

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
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_WILDCARD = re.compile(r"[*?\[\]{}]")
_HUMAN_BLOCKLIST = {"bot", "automation", "automated", "dependabot", "github-actions"}


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
			"typecheck",
			"stub_roots",
			"source_roots",
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
	component_path = _relative_path(table.get("path"), f"{path}.path")
	return PythonComponent(
		name=_string(table.get("name"), f"{path}.name"),
		path=component_path,
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
		typecheck=cast(bool, _type(table.get("typecheck", True), bool, f"{path}.typecheck")),
		stub_roots=_component_stub_roots(table.get("stub_roots", []), f"{path}.stub_roots"),
		source_roots=_component_source_roots(
			table.get("source_roots", []), f"{path}.source_roots", component_path
		),
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
	_validate_typing_policy(root, policy_release)
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


def _validate_typing_policy(root: Mapping[str, object], policy_release: str) -> None:
	"""Reject typing fields unsupported by the selected policy release.

	Raise ValidationError for unsupported fields or malformed component tables,
	even when an unsupported field is explicitly set to its default value."""
	version = tuple(int(part) for part in policy_release[1:].split("."))
	for index, component in enumerate(_list(root.get("python", []), "python")):
		table = _table(component, f"python[{index}]")
		for field, minimum in (
			("typecheck", (3, 1, 0)),
			("stub_roots", (3, 2, 0)),
			("source_roots", (4, 6, 0)),
		):
			if field in table and version < minimum:
				required = ".".join(str(part) for part in minimum)
				raise ValidationError(
					f"python[{index}].{field}", f"requires policy v{required} or newer"
				)


def parse_manifest(content: str) -> Manifest:
	"""Validate the schema without expanding repository-owned supplemental targets."""
	try:
		return _load_table(tomllib.loads(content))
	except tomllib.TOMLDecodeError as exc:
		raise ValidationError("manifest", "cannot read valid UTF-8 TOML") from exc


def load_manifest(root: Path | str = ".", manifest_name: str = MANIFEST_NAME) -> Manifest:
	path = Path(root).resolve() / manifest_name
	try:
		manifest = parse_manifest(path.read_text(encoding="utf-8"))
	except (OSError, UnicodeError) as exc:
		raise ValidationError("manifest", "cannot read valid UTF-8 TOML") from exc
	_validate_supplemental_targets(path.parent, manifest.supplemental_tests)
	return manifest
