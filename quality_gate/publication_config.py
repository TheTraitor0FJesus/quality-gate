"""Declarative product projections and required delivery identities."""

from __future__ import annotations

import re
from collections.abc import Mapping

from .publication import PublicationError
from .publication_evidence import record, records

MAX_ENTRIES = 32
MAX_STRING_LENGTH = 256


def strings(value: object, label: str) -> list[str]:
	"""Require a nonempty, unique and bounded list of nonempty strings."""
	if (
		not isinstance(value, list)
		or not 0 < len(value) <= MAX_ENTRIES
		or not all(
			isinstance(item, str) and item.strip() and len(item) <= MAX_STRING_LENGTH
			for item in value
		)
		or len(value) != len(set(value))
	):
		raise PublicationError(f"{label} requires unique bounded strings")
	return value


def validate_config(config: Mapping[str, object]) -> None:
	"""Validate the source-owned contract before product work or release mutations."""
	if type(config.get("schema")) is not int or config.get("schema") != 1:
		raise PublicationError("unsupported publisher configuration schema")
	if set(config) - {"schema", "workflow", "projections", "producers"}:
		raise PublicationError("unknown publisher configuration field")
	if (
		re.fullmatch(r"\.github/workflows/[A-Za-z0-9_-]+\.ya?ml", str(config.get("workflow")))
		is None
	):
		raise PublicationError("publisher workflow must be a repository workflow path")
	producers = records(config.get("producers"), "required producers")
	if not 0 < len(producers) <= MAX_ENTRIES:
		raise PublicationError("one to 32 required producers must be configured")
	strings([producer.get("name") for producer in producers], "producer names")
	strings([producer.get("job") for producer in producers], "producer jobs")
	for producer in producers:
		_validate_producer(producer)
	for projection in records(config.get("projections", []), "projections"):
		_validate_projection(projection)


def _validate_producer(producer: Mapping[str, object]) -> None:
	if set(producer) - {"name", "job", "checks", "deliverables", "kind"}:
		raise PublicationError("unknown producer configuration field")
	if re.fullmatch(r"[a-z][a-z0-9-]{0,31}", str(producer.get("name"))) is None:
		raise PublicationError("unsafe producer name")
	if producer.get("kind", "archive") not in {"archive", "package", "image"}:
		raise PublicationError("unknown producer deliverable kind")
	strings(producer.get("checks"), "required producer checks")
	for name in strings(producer.get("deliverables"), "required deliverables"):
		if (
			re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name.replace("{version}", "1.0.0"))
			is None
		):
			raise PublicationError("unsafe required deliverable name")


def _validate_projection(projection: Mapping[str, object]) -> None:
	if set(projection) - {"path", "format", "key", "prefix"}:
		raise PublicationError("unknown version projection field")
	path = projection.get("path")
	if (
		not isinstance(path, str)
		or not path
		or len(path) > MAX_STRING_LENGTH
		or any(part in {"", ".", ".."} for part in path.split("/"))
	):
		raise PublicationError("invalid version projection path")
	if re.fullmatch(r"[A-Za-z0-9_./-]+", path) is None:
		raise PublicationError("unsafe version projection path")
	if projection.get("format") not in {"toml", "json", "python"}:
		raise PublicationError("unknown projection format")
	strings([projection.get("key")], "projection key")
	if not isinstance(projection.get("prefix", ""), str):
		raise PublicationError("projection prefix must be a string")


def required_identities(candidate: Mapping[str, object]) -> dict[str, str]:
	"""Resolve the exact names and kinds required by the reviewed source configuration."""
	config = record(candidate.get("config"), "candidate configuration")
	result: dict[str, str] = {}
	for producer in records(config.get("producers"), "required producers"):
		for template in strings(producer.get("deliverables"), "required deliverables"):
			name = template.replace("{version}", str(candidate["version"]))
			if name in result:
				raise PublicationError("duplicate required deliverable identity")
			result[name] = str(producer.get("kind", "archive"))
	return result


def validate_identities(candidate: Mapping[str, object], items: list[dict[str, object]]) -> None:
	"""Reject missing, duplicated, substituted or differently typed delivery identities."""
	expected = required_identities(candidate)
	actual = {str(item.get("name")): str(item.get("kind")) for item in items}
	if not expected or len(items) != len(expected) or actual != expected:
		raise PublicationError("required deliverable names or kinds are incomplete or substituted")
