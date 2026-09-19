"""Quality Gate package metadata and bounded build inputs; publication policy is shared."""

from __future__ import annotations

import ast
import math
import re
import subprocess
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

SEMANTIC_VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")


ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


RELEASE_TIMEOUT_KEYS = frozenset(
	{
		"github_api_seconds",
		"external_download_seconds",
		"subprocess_seconds",
		"check_wait_seconds",
		"check_poll_seconds",
	}
)


RELEASE_TIMEOUT_LIMITS = {
	"github_api_seconds": 300.0,
	"external_download_seconds": 1800.0,
	"subprocess_seconds": 3600.0,
	"check_wait_seconds": 3600.0,
	"check_poll_seconds": 60.0,
}


class ReleaseError(RuntimeError):
	"""The reviewed source candidate cannot be published safely."""


@dataclass(frozen=True, slots=True)
class VersionProjection:
	"""The projections that must agree with the repository version authority."""

	version: str
	package: str
	policy_release: str


@dataclass(frozen=True, slots=True)
class ArtifactIdentity:
	"""One verified platform artifact and the source that produced it."""

	version: str
	source_sha: str
	platform: str
	name: str
	sha256: str
	path: str | None = None

	def as_dict(self) -> dict[str, str]:
		value = asdict(self)
		return {key: item for key, item in value.items() if item is not None}

	@classmethod
	def from_mapping(cls, value: Mapping[str, object]) -> ArtifactIdentity:
		fields = {"version", "source_sha", "platform", "name", "sha256", "path"}
		unknown = set(value) - fields
		if unknown:
			raise ReleaseError(f"artifact identity has unknown fields: {sorted(unknown)}")
		required = {
			name: value.get(name)
			for name in ("version", "source_sha", "platform", "name", "sha256")
		}
		if not all(isinstance(item, str) and item.strip() for item in required.values()):
			raise ReleaseError("artifact identity is incomplete")
		if (
			not isinstance(required["name"], str)
			or ARTIFACT_NAME.fullmatch(required["name"]) is None
		):
			raise ReleaseError("artifact identity name is invalid")
		path = value.get("path")
		if path is not None and (not isinstance(path, str) or not path.strip()):
			raise ReleaseError("artifact identity path is invalid")
		return cls(**cast(dict[str, str], required), path=cast(str | None, path))


def load_release_timeout(root: Path | str, name: str) -> float:
	"""Load one positive release-operation timeout from tracked configuration."""
	if name not in RELEASE_TIMEOUT_KEYS:
		raise ReleaseError(f"unknown release timeout: {name}")
	path = Path(root).resolve() / ".release" / "release-tools.toml"
	try:
		config = tomllib.loads(path.read_text(encoding="utf-8"))
	except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
		raise ReleaseError("release tools configuration is unreadable") from error
	timeouts = config.get("timeouts")
	value = timeouts.get(name) if isinstance(timeouts, dict) else None
	if (
		isinstance(value, bool)
		or not isinstance(value, (int, float))
		or not math.isfinite(float(value))
		or value <= 0
		or value > RELEASE_TIMEOUT_LIMITS[name]
	):
		raise ReleaseError(f"release timeout {name} must be finite and within its configured bound")
	return float(value)


def _object(value: object, label: str) -> dict[str, object]:
	if not isinstance(value, dict):
		raise ReleaseError(f"{label} must be an object")
	return cast(dict[str, object], value)


def read_bounded(stream: object, maximum: int, label: str) -> bytes:
	"""Read an external stream while enforcing its declared and actual size."""
	read = getattr(stream, "read", None)
	if not callable(read):
		raise ReleaseError(f"{label} is not readable")
	headers = getattr(stream, "headers", None)
	declared = headers.get("Content-Length") if headers is not None else None
	if declared is not None:
		try:
			if int(declared) > maximum:
				raise ReleaseError(f"{label} exceeds the {maximum}-byte limit")
		except (TypeError, ValueError) as error:
			raise ReleaseError(f"{label} has an invalid Content-Length") from error
	chunks: list[bytes] = []
	total = 0
	while True:
		chunk = cast(bytes, read(min(1024 * 1024, maximum - total + 1)))
		if not chunk:
			break
		total += len(chunk)
		if total > maximum:
			raise ReleaseError(f"{label} exceeds the {maximum}-byte limit")
		chunks.append(chunk)
	return b"".join(chunks)


def validate_source_sha(value: str) -> str:
	"""Require a full immutable lowercase Git commit SHA."""
	if SOURCE_SHA.fullmatch(value) is None:
		raise ReleaseError("source SHA must be exactly 40 lowercase hexadecimal characters")
	return value


def resolve_local_source_sha(root: Path | str) -> str:
	"""Resolve the checked-out commit that supplies the release source files."""
	actual_root = Path(root).resolve()
	try:
		result = subprocess.run(
			["git", "rev-parse", "HEAD"],
			cwd=actual_root,
			capture_output=True,
			text=True,
			encoding="utf-8",
			check=False,
			timeout=load_release_timeout(actual_root, "subprocess_seconds"),
		)
	except subprocess.TimeoutExpired as error:
		raise ReleaseError("local source revision lookup timed out") from error
	except (OSError, UnicodeError) as error:
		raise ReleaseError("local source revision lookup is unavailable") from error
	if result.returncode != 0:
		raise ReleaseError("local source revision lookup failed")
	try:
		return validate_source_sha(result.stdout.strip())
	except ReleaseError as error:
		raise ReleaseError("local source revision is invalid") from error


def _read_text(path: Path, label: str) -> str:
	try:
		value = path.read_text(encoding="utf-8")
	except (OSError, UnicodeError) as error:
		raise ReleaseError(f"{label} is unreadable") from error
	if not value.strip():
		raise ReleaseError(f"{label} is empty")
	return value


def load_version_authority(root: Path | str) -> str:
	"""Read the only product version authority."""
	actual_root = Path(root).resolve()
	path = actual_root / ".release" / "version.toml"
	try:
		raw = tomllib.loads(path.read_text(encoding="utf-8"))
	except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
		raise ReleaseError(".release/version.toml is unreadable") from error
	if set(raw) != {"version"}:
		raise ReleaseError(".release/version.toml must contain exactly one top-level version")
	version = raw.get("version")
	if not isinstance(version, str) or SEMANTIC_VERSION.fullmatch(version) is None:
		raise ReleaseError(".release/version.toml version must be MAJOR.MINOR.PATCH")
	return version


def _literal_version(path: Path, label: str) -> str:
	try:
		tree = ast.parse(_read_text(path, label), filename=str(path))
	except SyntaxError as error:
		raise ReleaseError(f"{label} is unreadable") from error
	for node in ast.walk(tree):
		if isinstance(node, ast.Assign) and any(
			isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
		):
			if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
				return node.value.value
			break
	raise ReleaseError(f"{label} does not declare a literal __version__")


def _toml(path: Path, label: str) -> dict[str, object]:
	try:
		return _object(tomllib.loads(path.read_text(encoding="utf-8")), label)
	except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
		raise ReleaseError(f"{label} is unreadable") from error


def validate_version_projections(root: Path | str) -> VersionProjection:
	"""Require package, runtime, manifest, template, and workflow projections to agree."""
	actual_root = Path(root).resolve()
	version = load_version_authority(actual_root)
	project = _toml(actual_root / "pyproject.toml", "pyproject.toml").get("project")
	project_table = _object(project, "pyproject.toml project")
	if project_table.get("version") != version:
		raise ReleaseError("native package version does not match the version authority")
	if _literal_version(actual_root / "quality_gate" / "__init__.py", "runtime version") != version:
		raise ReleaseError("runtime version does not match the version authority")
	manifest = _toml(actual_root / "quality-gate.toml", "quality-gate.toml")
	quality = _object(manifest.get("quality"), "quality-gate.toml quality")
	if quality.get("policy_release") != f"v{version}":
		raise ReleaseError("policy manifest version does not match the version authority")
	template = _toml(actual_root / "templates" / "quality-gate.toml", "template manifest")
	template_release = template.get("policy_release")
	if template_release is None:
		template_release = _object(template.get("quality"), "template quality").get(
			"policy_release"
		)
	if template_release != f"v{version}":
		raise ReleaseError("template policy version does not match the version authority")
	workflow = _read_text(
		actual_root / ".github" / "workflows" / "quality.yml", "reusable workflow"
	)
	if "QUALITY_GATE_RELEASE" not in workflow or "quality-gate sync" not in workflow:
		raise ReleaseError("reusable workflow does not project the policy release dynamically")
	from quality_gate.release_contract import supports_unified_inventory

	if not supports_unified_inventory(f"v{version}"):
		raise ReleaseError("distribution inventory does not support the authoritative version")
	return VersionProjection(version, version, f"v{version}")
