"""Read-only schema 1 to schema 2 migration proposals."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .contracts import ValidationError


@dataclass(frozen=True, slots=True)
class LegacyComponent:
	name: str
	path: str
	python_version: str
	dependency_inputs: list[str]
	test_paths: list[str]


def _quoted(value: str) -> str:
	return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _list(values: list[str] | tuple[str, ...]) -> str:
	return "[" + ", ".join(_quoted(value) for value in values) + "]"


def _component(value: object, index: int) -> LegacyComponent:
	if not isinstance(value, dict):
		raise ValidationError(f"python[{index}]", "schema 1 component must be a table")
	path = value.get("path")
	if not isinstance(path, str) or not path:
		raise ValidationError(f"python[{index}].path", "component path is required")
	dependencies = value.get("dependency_inputs", value.get("dependencies", []))
	tests = value.get("test_paths", value.get("tests", []))
	if isinstance(tests, str):
		tests = [tests]
	if isinstance(dependencies, str):
		dependencies = [dependencies]
	if not isinstance(dependencies, list) or not all(
		isinstance(item, str) for item in dependencies
	):
		raise ValidationError(f"python[{index}].dependencies", "must be a list of paths")
	if not isinstance(tests, list) or not all(isinstance(item, str) for item in tests):
		raise ValidationError(f"python[{index}].tests", "must be a list of paths")
	python_version = value.get("python_version", value.get("python", "3.12"))
	if not isinstance(python_version, str) or not python_version:
		raise ValidationError(f"python[{index}].python_version", "must be a non-empty string")
	name = value.get("name", Path(path).name)
	if not isinstance(name, str) or not name:
		raise ValidationError(f"python[{index}].name", "must be a non-empty string")
	return LegacyComponent(name, path, python_version, dependencies, tests)


def migration_proposal(
	root: Path | str = ".", manifest_name: str = "quality-gate.toml", policy_release: str = "v2.0.1"
) -> str:
	path = Path(root).resolve() / manifest_name
	try:
		raw = tomllib.loads(path.read_text(encoding="utf-8"))
	except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
		raise ValidationError("manifest", "cannot read a valid schema 1 TOML manifest") from exc
	quality = raw.get("quality")
	if not isinstance(quality, dict) or quality.get("schema") != 1:
		raise ValidationError("quality.schema", "migration accepts schema 1 only")
	if not re.fullmatch(r"v\d+\.\d+\.\d+", policy_release):
		raise ValidationError(
			"policy_release", "must be an immutable semantic release such as v2.0.0"
		)
	legacy = raw.get("python", [])
	if not isinstance(legacy, list):
		raise ValidationError("python", "must be a list")
	components = [_component(value, index) for index, value in enumerate(legacy)]
	domains = ["repository"] + (["python"] if components else [])
	lines = [
		"# Read-only proposal generated from schema 1. Review before applying.",
		"",
		"waivers = []",
		"",
		"[quality]",
		"schema = 2",
		f"policy_release = {_quoted(policy_release)}",
		"",
		"[repository]",
		'name = "REVIEW_REQUIRED"',
		f"domains = {_list(domains)}",
		'required_documents = ["AGENTS.md"]',
		"",
		"[repository.limits]",
		"max_blob_size_mib = 5",
		"",
		"[repository.defaults]",
		"command_timeout_seconds = 120",
		"test_timeout_seconds = 300",
		"gate_timeout_seconds = 600",
	]
	for item in components:
		tests_applicable = bool(item.test_paths)
		lines.extend(
			[
				"",
				"[[python]]",
				f"name = {_quoted(item.name)}",
				f"path = {_quoted(item.path)}",
				f"python_version = {_quoted(item.python_version)}",
				f"dependency_inputs = {_list(item.dependency_inputs)}",
				f"test_paths = {_list(item.test_paths)}",
				f"tests_applicable = {'true' if tests_applicable else 'false'}",
				"timeout_seconds = 300",
			]
		)
		if not tests_applicable:
			lines.append(
				'tests_reason = "Schema 1 did not declare test paths; review applicability."'
			)
	lines.append("")
	return "\n".join(lines)
