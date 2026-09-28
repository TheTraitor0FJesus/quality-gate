"""Inspect data-only Docker-save-compatible archives and publish them to GHCR."""

from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
import tempfile
from pathlib import Path
from typing import Protocol

from .publication import (
	MAX_IMAGE_TAGGED_REFERENCE_LENGTH,
	MissingResourceError,
	PublicationError,
	image_repository,
)
from .publication_artifacts import MAX_BUNDLE_BYTES

MAX_IMAGE_FILES = 100_000
MAX_IMAGE_METADATA_BYTES = 4 * 1024 * 1024
IMAGE_REVISION_LABEL = "org.opencontainers.image.revision"
_MAX_ARCHIVE_MEMBER_PATH_LENGTH = 1024
_DOCKER_MANIFEST_SCHEMA_VERSION = 2
_IMAGE_TAG = re.compile(
	rf"[A-Za-z0-9_][A-Za-z0-9_.:/-]{{0,{MAX_IMAGE_TAGGED_REFERENCE_LENGTH - 1}}}"
)


class _Command(Protocol):
	def __call__(self, arguments: list[str], *, limit: int) -> bytes: ...


def _validate_image_target(target: str, expected_digest: str | None) -> None:
	if _IMAGE_TAG.fullmatch(target) is None or "@" in target:
		raise PublicationError("image target must be a tagged registry reference")
	repository = target.rpartition(":")[0] or target
	image_repository(repository)
	if expected_digest is not None and re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None:
		raise PublicationError("retained image digest is invalid")


def _safe_member(value: object) -> str:
	if (
		not isinstance(value, str)
		or not value
		or value.startswith(("/", "\\"))
		or "\\" in value
		or any(part in {"", ".", ".."} for part in value.split("/"))
		or len(value) > _MAX_ARCHIVE_MEMBER_PATH_LENGTH
	):
		raise PublicationError("image archive contains an unsafe member path")
	return value


def _archive_files(archive: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
	files: dict[str, tarfile.TarInfo] = {}
	expanded = 0
	member_count = 0
	for member in archive:
		member_count += 1
		if member_count > MAX_IMAGE_FILES:
			raise PublicationError("image archive has an invalid member count")
		name = _safe_member(member.name.rstrip("/"))
		if member.isdir():
			continue
		if not member.isfile() or name in files:
			raise PublicationError("image archive contains links, special files, or duplicates")
		expanded += member.size
		if expanded > MAX_BUNDLE_BYTES:
			raise PublicationError("image archive expansion exceeds the supported size")
		files[name] = member
	if member_count == 0:
		raise PublicationError("image archive has an invalid member count")
	return files


def _read_archive_json(archive: tarfile.TarFile, member: tarfile.TarInfo, message: str) -> object:
	stream = archive.extractfile(member)
	if stream is None:
		raise PublicationError(message)
	return json.loads(stream.read(MAX_IMAGE_METADATA_BYTES + 1))


def _archive_image_members(manifest: object, files: dict[str, tarfile.TarInfo]) -> tuple[str, str]:
	if not isinstance(manifest, list) or len(manifest) != 1:
		raise PublicationError("image archive must contain exactly one image")
	identity = manifest[0]
	if not isinstance(identity, dict):
		raise PublicationError("image archive manifest identity is invalid")
	config_name = _safe_member(identity.get("Config"))
	tags = identity.get("RepoTags")
	layers = identity.get("Layers")
	if (
		config_name not in files
		or tags is None
		or not isinstance(tags, list)
		or len(tags) != 1
		or not isinstance(tags[0], str)
		or _IMAGE_TAG.fullmatch(tags[0]) is None
		or not isinstance(layers, list)
		or not layers
		or any(_safe_member(layer) not in files for layer in layers)
	):
		raise PublicationError("image archive must identify one complete tagged image")
	return config_name, tags[0]


def _image_archive_details(
	content: bytes, expected_source: str
) -> tuple[str, tuple[str, tuple[str, ...]]]:
	"""Validate one archive and return its tag plus config and layer identity."""
	if not content or len(content) > MAX_BUNDLE_BYTES:
		raise PublicationError("image archive is empty or exceeds the supported size")
	try:
		with tarfile.open(fileobj=io.BytesIO(content), mode="r:*") as archive:
			files = _archive_files(archive)
			manifest_member = files.get("manifest.json")
			if manifest_member is None or manifest_member.size > MAX_IMAGE_METADATA_BYTES:
				raise PublicationError("image archive lacks a bounded Docker manifest")
			manifest = _read_archive_json(
				archive, manifest_member, "image archive manifest cannot be read"
			)
			config_name, image_tag = _archive_image_members(manifest, files)
			config_member = files[config_name]
			if config_member.size > MAX_IMAGE_METADATA_BYTES:
				raise PublicationError("image configuration exceeds the supported metadata size")
			config_stream = archive.extractfile(config_member)
			if config_stream is None:
				raise PublicationError("image configuration cannot be read")
			config_content = config_stream.read(MAX_IMAGE_METADATA_BYTES + 1)
			if len(config_content) > MAX_IMAGE_METADATA_BYTES:
				raise PublicationError("image configuration exceeds the supported metadata size")
			config = json.loads(config_content)
			if not isinstance(config, dict):
				raise PublicationError("image configuration is invalid")
			runtime = config.get("config")
			labels = runtime.get("Labels") if isinstance(runtime, dict) else None
			if not isinstance(labels, dict) or labels.get(IMAGE_REVISION_LABEL) != expected_source:
				raise PublicationError("image revision label does not match the approved source")
			rootfs = config.get("rootfs")
			diff_ids = rootfs.get("diff_ids") if isinstance(rootfs, dict) else None
			if (
				not isinstance(diff_ids, list)
				or not diff_ids
				or any(
					not isinstance(value, str)
					or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None
					for value in diff_ids
				)
			):
				raise PublicationError("image configuration has invalid layer identities")
			identity = "sha256:" + hashlib.sha256(config_content).hexdigest(), tuple(diff_ids)
			return image_tag, identity
	except (tarfile.TarError, OSError, UnicodeError, json.JSONDecodeError) as error:
		raise PublicationError("image archive is not a valid Docker image archive") from error


def inspect_image_archive(content: bytes, expected_source: str) -> str:
	"""Return the sole tagged image after checking its config revision without running it."""
	image_tag, _identity = _image_archive_details(content, expected_source)
	return image_tag


def _docker_image_identity(command: _Command, reference: str) -> tuple[str, tuple[str, ...]]:
	output = command(
		["docker", "image", "inspect", "--format", "{{json .}}", reference],
		limit=MAX_IMAGE_METADATA_BYTES,
	)
	try:
		image = json.loads(output)
		rootfs = image.get("RootFS") if isinstance(image, dict) else None
		layers = rootfs.get("Layers") if isinstance(rootfs, dict) else None
		identity = image.get("Id") if isinstance(image, dict) else None
		if (
			not isinstance(identity, str)
			or re.fullmatch(r"sha256:[0-9a-f]{64}", identity) is None
			or not isinstance(layers, list)
			or not layers
			or any(
				not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None
				for value in layers
			)
		):
			raise ValueError("invalid Docker image identity")
		return identity, tuple(layers)
	except (UnicodeError, json.JSONDecodeError, ValueError) as error:
		raise PublicationError("Docker returned an invalid image identity") from error


def _manifest_config_digest(manifest: bytes) -> str:
	try:
		value = json.loads(manifest)
		config = value.get("config") if isinstance(value, dict) else None
		layers = value.get("layers") if isinstance(value, dict) else None
		config_digest = config.get("digest") if isinstance(config, dict) else None
		if (
			not isinstance(value, dict)
			or type(value.get("schemaVersion")) is not int
			or value.get("schemaVersion") != _DOCKER_MANIFEST_SCHEMA_VERSION
			or not isinstance(config_digest, str)
			or re.fullmatch(r"sha256:[0-9a-f]{64}", config_digest) is None
			or not isinstance(layers, list)
			or not layers
		):
			raise ValueError("invalid registry manifest")
		return config_digest
	except (UnicodeError, json.JSONDecodeError, ValueError) as error:
		raise PublicationError("registry tag is not a supported single-image manifest") from error


def _existing_image_digest(
	target: str,
	command: _Command,
	expected_identity: tuple[str, tuple[str, ...]],
	expected_digest: str | None,
) -> str | None:
	try:
		manifest = command(
			["docker", "buildx", "imagetools", "inspect", target, "--raw"],
			limit=16 * 1024 * 1024,
		)
	except MissingResourceError as error:
		if expected_digest is not None:
			raise PublicationError(
				"retained draft image tag is missing from the registry"
			) from error
		return None
	previous_digest = hashlib.sha256(manifest).hexdigest()
	if expected_digest is None:
		raise PublicationError(
			"existing image tag has no retained writer receipt digest; owner review is required"
		)
	if previous_digest != expected_digest:
		raise PublicationError("existing image tag conflicts with the retained draft digest")
	if _manifest_config_digest(manifest) != expected_identity[0]:
		raise PublicationError("existing image tag points to different image bytes")
	command(["docker", "pull", target], limit=16 * 1024 * 1024)
	if _docker_image_identity(command, target) != expected_identity:
		raise PublicationError("existing image tag points to different image bytes")
	return previous_digest


def preflight_image_archive(
	content: bytes,
	expected_source: str,
	target: str,
	command: _Command,
	*,
	expected_digest: str | None = None,
) -> None:
	"""Check one candidate archive against its remote tag without writing to GHCR."""
	_validate_image_target(target, expected_digest)
	_image_tag, expected_identity = _image_archive_details(content, expected_source)
	_existing_image_digest(target, command, expected_identity, expected_digest)


def push_image_archive(
	content: bytes,
	expected_source: str,
	target: str,
	command: _Command,
	*,
	expected_digest: str | None = None,
) -> str:
	"""Load and verify one image, then create or safely reuse its mutable version tag."""
	_validate_image_target(target, expected_digest)
	loaded_reference, expected_identity = _image_archive_details(content, expected_source)
	with tempfile.TemporaryDirectory(prefix="publisher-image-") as directory:
		archive_path = Path(directory) / "image.tar"
		archive_path.write_bytes(content)
		archive_path.chmod(0o600)
		command(["docker", "load", "--input", str(archive_path)], limit=16 * 1024 * 1024)
		format_value = f'{{{{ index .Config.Labels "{IMAGE_REVISION_LABEL}" }}}}'
		loaded_revision = (
			command(
				["docker", "image", "inspect", "--format", format_value, loaded_reference],
				limit=1024,
			)
			.decode("utf-8")
			.strip()
		)
		if loaded_revision != expected_source:
			raise PublicationError("loaded image revision does not match the approved source")
		loaded_identity = _docker_image_identity(command, loaded_reference)
		if loaded_identity != expected_identity:
			raise PublicationError("loaded image bytes differ from the verified archive")
		previous_digest = _existing_image_digest(target, command, loaded_identity, expected_digest)
		if previous_digest is not None:
			return previous_digest
		command(["docker", "image", "tag", loaded_reference, target], limit=1024)
		target_revision = (
			command(["docker", "image", "inspect", "--format", format_value, target], limit=1024)
			.decode("utf-8")
			.strip()
		)
		if target_revision != expected_source:
			raise PublicationError("tagged image revision does not match the approved source")
		command(["docker", "push", target], limit=16 * 1024 * 1024)
		manifest = command(
			["docker", "buildx", "imagetools", "inspect", target, "--raw"],
			limit=16 * 1024 * 1024,
		)
		if not manifest:
			raise PublicationError("published image manifest is empty")
		if _manifest_config_digest(manifest) != loaded_identity[0]:
			raise PublicationError("published image manifest differs from the verified archive")
		return hashlib.sha256(manifest).hexdigest()
