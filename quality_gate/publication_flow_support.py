"""Shared publication contracts and source metadata parsing."""

from __future__ import annotations

import ast
import json
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from .publication import PublicationError
from .publication_evidence import record

RECEIPT_MARKER = "\n<!-- shared-publisher-receipt-v1\n"


PAGE_SIZE = 100


MAX_NOTES_BYTES = 64 * 1024


MAX_PULL_FILES = 3000


DEPENDABOT_LOGIN = "dependabot[bot]"


WORKFLOW_YAML_PATH = re.compile(r"\.github/workflows/[^/]+\.ya?ml")


class GitHub(Protocol):
	"""External GitHub and registry operations, controlled by behavioral fixtures."""

	def get(self, path: str) -> object: ...
	def source(self, sha: str, path: str) -> str: ...
	def post(self, path: str, payload: Mapping[str, object]) -> dict[str, object]: ...
	def patch(self, path: str, payload: Mapping[str, object]) -> dict[str, object]: ...
	def upload(self, release_id: int, name: str, content: bytes) -> dict[str, object]: ...
	def download(self, path: str) -> bytes: ...
	def image_digest(self, reference: str) -> str: ...
	def preflight_image_archive(
		self,
		content: bytes,
		source: str,
		repository: str,
		version: str,
		producer: str,
		*,
		expected_digest: str | None = None,
	) -> None: ...
	def publish_image_archive(
		self,
		content: bytes,
		source: str,
		repository: str,
		version: str,
		producer: str,
		*,
		expected_digest: str | None = None,
	) -> dict[str, str]: ...


def sha(value: object) -> str:
	"""Require a full immutable Git source identity."""
	if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
		raise PublicationError("source and provider revisions must be full lowercase commit SHAs")
	return value


def canonical(value: object) -> bytes:
	"""Stable UTF-8 encoding for retained envelopes and receipts."""
	return (
		json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
	).encode("utf-8")


def _toml(content: str) -> dict[str, object]:
	if len(content.encode("utf-8")) > 1024 * 1024:
		raise PublicationError("source metadata exceeds 1 MiB")
	try:
		return record(tomllib.loads(content), "source TOML")
	except tomllib.TOMLDecodeError as error:
		raise PublicationError("source metadata is invalid TOML") from error


def _authority(content: str) -> str:
	fields = _toml(content)
	if set(fields) != {"version"} or not isinstance(fields["version"], str):
		raise PublicationError(
			"version authority must contain exactly one top-level version string"
		)
	return fields["version"]


def _projection(content: str, projection: Mapping[str, object]) -> str:
	kind, key = projection.get("format"), projection.get("key")
	value: object
	if not isinstance(key, str):
		raise PublicationError("projection key is required")
	if kind == "python":
		values = [
			node.value.value
			for node in ast.parse(content).body
			if isinstance(node, ast.Assign)
			and isinstance(node.value, ast.Constant)
			and any(isinstance(target, ast.Name) and target.id == key for target in node.targets)
		]
		if len(values) != 1:
			raise PublicationError("Python projection requires one literal assignment")
		value = values[0]
	else:
		value = json.loads(content) if kind == "json" else _toml(content)
		if kind not in {"json", "toml"}:
			raise PublicationError("unknown projection format")
		for part in key.split("."):
			value = record(value, "projection").get(part)
	prefix = projection.get("prefix", "")
	if not isinstance(value, str) or not isinstance(prefix, str) or not value.startswith(prefix):
		raise PublicationError("version projection is missing or invalid")
	return value.removeprefix(prefix)


@dataclass(frozen=True)
class Context:
	"""Actual Actions context supplied by the reusable wrapper, never by a product file."""

	repository: str
	provider_repository: str
	provider_sha: str
	helper_sha: str
	run_id: int
	attempt: int
	run_source_sha: str
	run_head_sha: str


@dataclass(frozen=True, slots=True)
class _VerifiedImage:
	producer: Mapping[str, object]
	item: Mapping[str, object]
	archive_name: str
	archive_sha256: str
	archive_bytes: bytes
	producer_proof: Mapping[str, object]


def _unique_receipt_image(
	images: list[dict[str, object]], name: object, failure_message: str
) -> dict[str, object]:
	matches = [image for image in images if image.get("name") == name]
	if len(matches) != 1:
		raise PublicationError(failure_message)
	return matches[0]
