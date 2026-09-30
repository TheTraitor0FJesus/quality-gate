from __future__ import annotations

import hashlib
import io
import json
import tarfile
from collections.abc import Sequence

import pytest

from quality_gate import publication_writer
from quality_gate.publication import (
	MAX_IMAGE_REPOSITORY_LENGTH,
	MAX_IMAGE_TAGGED_REFERENCE_LENGTH,
	MAX_VERSION_LENGTH,
	MissingResourceError,
	PublicationError,
	image_repository,
)
from quality_gate.publication_writer import (
	inspect_image_archive,
	preflight_image_archive,
	push_image_archive,
)

SOURCE = "a" * 40
LAYER = b"layer-data"
DIFF_ID = "sha256:" + hashlib.sha256(LAYER).hexdigest()


def _archive(
	*,
	revision: str = SOURCE,
	filename: str = "layer/layer.tar",
	extra_entries: Sequence[tuple[tarfile.TarInfo, bytes | None]] = (),
) -> bytes:
	manifest = [
		{
			"Config": "config.json",
			"RepoTags": ["source/image:v1"],
			"Layers": [filename],
		}
	]
	config = {
		"config": {"Labels": {"org.opencontainers.image.revision": revision}},
		"rootfs": {"type": "layers", "diff_ids": [DIFF_ID]},
	}
	files = {
		"manifest.json": json.dumps(manifest).encode(),
		"config.json": json.dumps(config).encode(),
		filename: LAYER,
	}
	output = io.BytesIO()
	with tarfile.open(fileobj=output, mode="w") as archive:
		for name, content in files.items():
			member = tarfile.TarInfo(name)
			member.size = len(content)
			archive.addfile(member, io.BytesIO(content))
		for member, content in extra_entries:
			archive.addfile(member, io.BytesIO(content) if content is not None else None)
	return output.getvalue()


def _config_digest(content: bytes) -> tuple[str, tuple[str, ...]]:
	with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
		manifest_stream = archive.extractfile("manifest.json")
		assert manifest_stream is not None
		manifest = json.loads(manifest_stream.read())
		config_stream = archive.extractfile(manifest[0]["Config"])
		assert config_stream is not None
		config_content = config_stream.read()
		config = json.loads(config_content)
	return "sha256:" + hashlib.sha256(config_content).hexdigest(), tuple(
		config["rootfs"]["diff_ids"]
	)


def _registry_manifest(config_digest: str) -> bytes:
	return json.dumps(
		{
			"schemaVersion": 2,
			"mediaType": "application/vnd.oci.image.manifest.v1+json",
			"config": {"digest": config_digest},
			"layers": [{"digest": "sha256:" + "c" * 64}],
		}
	).encode()


class _DockerCommand:
	def __init__(
		self,
		*,
		existing_manifest: bytes | None = None,
		loaded_config_digest: str | None = None,
		loaded_layers: tuple[str, ...] | None = None,
	) -> None:
		self.calls: list[list[str]] = []
		self.pushed = False
		self.existing_manifest = existing_manifest
		self.config_digest, self.layers = _config_digest(_archive())
		self.loaded_config_digest = (
			self.config_digest if loaded_config_digest is None else loaded_config_digest
		)
		self.loaded_layers = self.layers if loaded_layers is None else loaded_layers

	def __call__(self, arguments: Sequence[str], *, limit: int) -> bytes:
		call = list(arguments)
		self.calls.append(call)
		if call[:3] == ["docker", "image", "inspect"]:
			if call[4] == "{{json .}}":
				return json.dumps(
					{
						"Id": self.loaded_config_digest,
						"RootFS": {"Layers": self.loaded_layers},
					}
				).encode()
			return (SOURCE + "\n").encode()
		if call[:5] == [
			"docker",
			"buildx",
			"imagetools",
			"inspect",
			"ghcr.io/owner/project:v2.3.0",
		]:
			if not self.pushed and self.existing_manifest is None:
				raise MissingResourceError("registry tag is absent")
			if not self.pushed:
				return self.existing_manifest or b""
			return self.existing_manifest or _registry_manifest(self.config_digest)
		if call[:2] == ["docker", "push"]:
			self.pushed = True
		return b"ok"


def test_image_archive_requires_one_image_with_the_approved_source_label() -> None:
	assert inspect_image_archive(_archive(), SOURCE) == "source/image:v1"

	with pytest.raises(PublicationError, match="revision label"):
		inspect_image_archive(_archive(revision="b" * 40), SOURCE)


def test_image_archive_rejects_oci_layout_without_docker_manifest() -> None:
	output = io.BytesIO()
	with tarfile.open(fileobj=output, mode="w") as archive:
		for name, content in {
			"oci-layout": b'{"imageLayoutVersion":"1.0.0"}',
			"index.json": b'{"schemaVersion":2,"manifests":[]}',
		}.items():
			member = tarfile.TarInfo(name)
			member.size = len(content)
			archive.addfile(member, io.BytesIO(content))

	with pytest.raises(PublicationError, match="Docker manifest"):
		inspect_image_archive(output.getvalue(), SOURCE)


def test_image_archive_stops_at_the_first_member_over_the_limit(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	monkeypatch.setattr(publication_writer, "MAX_IMAGE_FILES", 1)
	parsed_members: list[str] = []
	original_next = tarfile.TarFile.next

	def count_parsed_members(archive: tarfile.TarFile) -> tarfile.TarInfo | None:
		member = original_next(archive)
		if member is not None and member.offset not in parsed_offsets:
			parsed_offsets.add(member.offset)
			parsed_members.append(member.name)
		return member

	parsed_offsets: set[int] = set()
	monkeypatch.setattr(tarfile.TarFile, "next", count_parsed_members)
	with pytest.raises(PublicationError, match="invalid member count"):
		inspect_image_archive(_archive(), SOURCE)

	assert parsed_members == ["manifest.json", "config.json"]


def test_image_repository_bound_fits_the_longest_supported_version_tag() -> None:
	prefix = "ghcr.io/a/"
	repository = prefix + "p" * (MAX_IMAGE_REPOSITORY_LENGTH - len(prefix))
	version = "9" * (MAX_VERSION_LENGTH - len(".0.0")) + ".0.0"

	assert image_repository("ghcr.io/owner/project") == "ghcr.io/owner/project"
	assert image_repository(repository) == repository
	assert len(f"{repository}:v{version}") == MAX_IMAGE_TAGGED_REFERENCE_LENGTH

	class MissingTag:
		def __call__(self, _arguments: list[str], *, limit: int) -> bytes:
			raise MissingResourceError("registry tag is absent")

	preflight_image_archive(_archive(), SOURCE, f"{repository}:v{version}", MissingTag())
	with pytest.raises(PublicationError, match="lowercase ghcr.io"):
		image_repository(repository + "p")
	with pytest.raises(PublicationError, match="lowercase ghcr.io"):
		image_repository("ghcr.io/Owner/project")


@pytest.mark.parametrize("filename", ["../layer.tar", "/layer.tar", "layer\\layer.tar"])
def test_image_archive_rejects_unsafe_layer_paths(filename: str) -> None:
	with pytest.raises(PublicationError, match="unsafe member path"):
		inspect_image_archive(_archive(filename=filename), SOURCE)


def test_image_archive_rejects_multiple_images() -> None:
	content = _archive()
	with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
		files = {member.name: archive.extractfile(member).read() for member in archive.getmembers()}
	files["manifest.json"] = json.dumps(
		json.loads(files["manifest.json"]) * 2
	).encode()
	output = io.BytesIO()
	with tarfile.open(fileobj=output, mode="w") as archive:
		for name, value in files.items():
			member = tarfile.TarInfo(name)
			member.size = len(value)
			archive.addfile(member, io.BytesIO(value))
	with pytest.raises(PublicationError, match="exactly one image"):
		inspect_image_archive(output.getvalue(), SOURCE)


def test_writer_rejects_symlink_before_docker_write() -> None:
	link = tarfile.TarInfo("layer-link")
	link.type = tarfile.SYMTYPE
	link.linkname = "layer/layer.tar"
	command = _DockerCommand()

	with pytest.raises(PublicationError, match="links, special files, or duplicates"):
		push_image_archive(
			_archive(extra_entries=((link, None),)),
			SOURCE,
			"ghcr.io/owner/project:v2.3.0",
			command,
		)

	assert command.calls == []


def test_writer_rejects_special_file_before_docker_write() -> None:
	special = tarfile.TarInfo("device")
	special.type = tarfile.FIFOTYPE
	command = _DockerCommand()

	with pytest.raises(PublicationError, match="links, special files, or duplicates"):
		push_image_archive(
			_archive(extra_entries=((special, None),)),
			SOURCE,
			"ghcr.io/owner/project:v2.3.0",
			command,
		)

	assert command.calls == []


def test_writer_rejects_duplicate_path_before_docker_write() -> None:
	duplicate = tarfile.TarInfo("layer/layer.tar")
	duplicate.size = len(LAYER)
	command = _DockerCommand()

	with pytest.raises(PublicationError, match="links, special files, or duplicates"):
		push_image_archive(
			_archive(extra_entries=((duplicate, LAYER),)),
			SOURCE,
			"ghcr.io/owner/project:v2.3.0",
			command,
		)

	assert command.calls == []


def test_writer_loads_inspects_pushes_and_returns_manifest_digest() -> None:
	command = _DockerCommand()
	digest = push_image_archive(
		_archive(), SOURCE, "ghcr.io/owner/project:v2.3.0", command
	)
	manifest = _registry_manifest(command.config_digest)

	assert digest == hashlib.sha256(manifest).hexdigest()
	assert [call[:2] for call in command.calls] == [
		["docker", "load"],
		["docker", "image"],
		["docker", "image"],
		["docker", "buildx"],
		["docker", "image"],
		["docker", "image"],
		["docker", "push"],
		["docker", "buildx"],
	]
	assert not any("run" in call for call in command.calls)


@pytest.mark.parametrize(
	("loaded_config_digest", "loaded_layers"),
	[
		("sha256:" + "f" * 64, None),
		(None, ("sha256:" + "f" * 64,)),
	],
	ids=["config-digest", "layer-identity"],
)
def test_loaded_image_identity_mismatch_stops_before_registry_or_delivery(
	loaded_config_digest: str | None, loaded_layers: tuple[str, ...] | None
) -> None:
	command = _DockerCommand(
		loaded_config_digest=loaded_config_digest,
		loaded_layers=loaded_layers,
	)

	with pytest.raises(
		PublicationError, match="^loaded image bytes differ from the verified archive$"
	):
		push_image_archive(
			_archive(), SOURCE, "ghcr.io/owner/project:v2.3.0", command
		)

	assert len(command.calls) == 3
	assert command.calls[0][:3] == ["docker", "load", "--input"]
	assert command.calls[0][3].endswith("image.tar")
	assert command.calls[1] == [
		"docker",
		"image",
		"inspect",
		"--format",
		'{{ index .Config.Labels "org.opencontainers.image.revision" }}',
		"source/image:v1",
	]
	assert command.calls[2] == [
		"docker",
		"image",
		"inspect",
		"--format",
		"{{json .}}",
		"source/image:v1",
	]


def test_existing_tag_with_different_image_bytes_is_not_mutated() -> None:
	manifest = _registry_manifest("sha256:" + "f" * 64)
	command = _DockerCommand(existing_manifest=manifest)

	with pytest.raises(PublicationError, match="different image bytes"):
		push_image_archive(
			_archive(),
			SOURCE,
			"ghcr.io/owner/project:v2.3.0",
			command,
			expected_digest=hashlib.sha256(manifest).hexdigest(),
		)

	assert not any(call[:2] == ["docker", "image"] and call[2] == "tag" for call in command.calls)
	assert not any(call[:2] == ["docker", "push"] for call in command.calls)


def test_retained_digest_mismatch_does_not_mutate_existing_tag() -> None:
	manifest = _registry_manifest(_config_digest(_archive())[0])
	command = _DockerCommand(existing_manifest=manifest)

	with pytest.raises(PublicationError, match="retained draft digest"):
		push_image_archive(
			_archive(),
			SOURCE,
			"ghcr.io/owner/project:v2.3.0",
			command,
			expected_digest="f" * 64,
		)

	assert not any(
		call[:3] == ["docker", "image", "tag"] for call in command.calls
	)
	assert not any(call[:2] == ["docker", "push"] for call in command.calls)


def test_missing_tag_with_retained_digest_does_not_mutate() -> None:
	command = _DockerCommand()

	with pytest.raises(PublicationError, match="tag is missing"):
		push_image_archive(
			_archive(),
			SOURCE,
			"ghcr.io/owner/project:v2.3.0",
			command,
			expected_digest="f" * 64,
		)

	assert not any(
		call[:3] == ["docker", "image", "tag"] for call in command.calls
	)
	assert not any(call[:2] == ["docker", "push"] for call in command.calls)


def test_preflight_rejects_conflicting_tag_without_loading_or_pushing() -> None:
	command = _DockerCommand(existing_manifest=_registry_manifest("sha256:" + "f" * 64))

	with pytest.raises(PublicationError, match="no retained writer receipt digest"):
		preflight_image_archive(
			_archive(), SOURCE, "ghcr.io/owner/project:v2.3.0", command
		)

	assert not any(call[:2] == ["docker", "load"] for call in command.calls)
	assert not any(call[:2] == ["docker", "push"] for call in command.calls)


def test_existing_tag_without_retained_digest_fails_before_pull_or_mutation() -> None:
	manifest = _registry_manifest(_config_digest(_archive())[0])
	command = _DockerCommand(existing_manifest=manifest)

	with pytest.raises(PublicationError, match="no retained writer receipt digest"):
		push_image_archive(_archive(), SOURCE, "ghcr.io/owner/project:v2.3.0", command)

	assert not any(call[:2] == ["docker", "pull"] for call in command.calls)
	assert not any(call[:3] == ["docker", "image", "tag"] for call in command.calls)
	assert not any(call[:2] == ["docker", "push"] for call in command.calls)


def test_existing_matching_tag_is_reused_without_a_push() -> None:
	manifest = _registry_manifest(_config_digest(_archive())[0])
	command = _DockerCommand(existing_manifest=manifest)

	digest = push_image_archive(
		_archive(),
		SOURCE,
		"ghcr.io/owner/project:v2.3.0",
		command,
		expected_digest=hashlib.sha256(manifest).hexdigest(),
	)

	assert digest == hashlib.sha256(manifest).hexdigest()
	assert any(call[:2] == ["docker", "pull"] for call in command.calls)
	assert not any(call[:2] == ["docker", "image"] and call[2] == "tag" for call in command.calls)
	assert not any(call[:2] == ["docker", "push"] for call in command.calls)


def test_writer_rejects_an_invalid_target_before_running_docker() -> None:
	class Command:
		def __call__(self, _arguments: Sequence[str], *, limit: int) -> bytes:
			raise AssertionError("docker must not run for an invalid target")

	with pytest.raises(PublicationError, match="tagged registry reference"):
		push_image_archive(_archive(), SOURCE, "ghcr.io/owner/project@sha256:" + "f" * 64, Command())
