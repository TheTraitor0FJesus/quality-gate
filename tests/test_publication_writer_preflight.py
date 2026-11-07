from __future__ import annotations

import hashlib
import io
import json
import sys
import tarfile
from collections.abc import Sequence
from threading import Thread
from typing import BinaryIO

import pytest

from quality_gate import publication_command, publication_github, publication_process
from quality_gate.publication import MissingResourceError, PublicationError
from quality_gate.publication_github import BoundedCommand
from quality_gate.publication_writer import preflight_image_archive

SOURCE = "a" * 40
LAYER = b"layer-data"
DIFF_ID = "sha256:" + hashlib.sha256(LAYER).hexdigest()
TARGET = "ghcr.io/owner/project:v2.4.1"


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


@pytest.mark.parametrize(
	("stderr", "exit_code", "sleep", "expected_absence"),
	[
		(b"ERROR: " + TARGET.encode() + b": manifest unknown\n", 1, False, True),
		(b"ERROR: " + TARGET.encode() + b": manifest_unknown\n", 1, False, True),
		(b"ERROR: " + TARGET.encode() + b": no such manifest\n", 1, False, True),
		(b"ERROR: no such manifest: " + TARGET.encode() + b"\n", 1, False, True),
		(b"ERROR: " + TARGET.encode() + b": not found\n", 1, False, True),
		(TARGET.encode() + b": not found\n", 1, False, True),
		(TARGET.encode() + b": manifest unknown\n", 1, False, True),
		(b"ERROR: ghcr.io/other/project:v2.4.1: manifest unknown\n", 1, False, False),
		(b"ERROR: ghcr.io/other/project:v2.4.1: not found\n", 1, False, False),
		(b"ERROR: ghcr.io/owner/project:v2.4.0: manifest unknown\n", 1, False, False),
		(b"ERROR: blob sha256:" + b"a" * 64 + b": manifest unknown\n", 1, False, False),
		(b"unauthorized: authentication required\n", 1, False, False),
		(b"denied: requested access to the resource is denied\n", 1, False, False),
		(b"lookup ghcr.io: no such host\n", 1, False, False),
		(b"x509: certificate signed by unknown authority\n", 1, False, False),
		(b"unexpected status code: 404\n", 1, False, False),
		(b"429 Too Many Requests\n", 1, False, False),
		(b"500 Internal Server Error\n", 1, False, False),
		(
			b"unexpected status from HEAD request to "
			b"https://ghcr.io/v2/owner/project/manifests/v2.4.1: 404 Not Found\n",
			1,
			False,
			False,
		),
		(b"ERROR: " + TARGET.encode() + b": manifest unknown\n", 143, False, False),
		(b"ERROR: " + TARGET.encode() + b": manifest unknown\n" + b"x" * 5000, 1, False, False),
		(b"ERROR: " + TARGET.encode() + b": manifest unknown\n", 1, True, False),
	],
)
def test_bounded_preflight_classifies_only_exact_missing_manifest(
	monkeypatch: pytest.MonkeyPatch,
	stderr: bytes,
	exit_code: int,
	sleep: bool,
	expected_absence: bool,
) -> None:
	"""Exercise the writer through BoundedCommand and a real controlled child process."""
	original_spawn = publication_command._spawn_bounded_command
	commands: list[list[str]] = []

	def spawn(
		command: list[str], environment: dict[str, str]
	) -> publication_process._CommandSession:
		commands.append(command)
		program = (
			"import sys, time; "
			f"sys.stderr.buffer.write({stderr!r}); "
			"sys.stderr.flush(); "
			f"time.sleep(5) if {sleep!r} else None; "
			f"sys.exit({exit_code})"
		)
		return original_spawn([sys.executable, "-B", "-c", program], environment)

	monkeypatch.setattr(publication_command, "_spawn_bounded_command", spawn)
	command = BoundedCommand(1 if sleep else 5)
	call = ["docker", "buildx", "imagetools", "inspect", TARGET, "--raw"]

	if expected_absence:
		preflight_image_archive(_archive(), SOURCE, TARGET, command)
		with pytest.raises(PublicationError) as error:
			preflight_image_archive(_archive(), SOURCE, TARGET, command, expected_digest="f" * 64)
		assert not isinstance(error.value, MissingResourceError)
		assert TARGET not in str(error.value)
		assert commands == [call, call]
	else:
		with pytest.raises(PublicationError) as error:
			preflight_image_archive(_archive(), SOURCE, TARGET, command)
		assert not isinstance(error.value, MissingResourceError)
		assert TARGET not in str(error.value)
		assert commands == [call]


@pytest.mark.parametrize("transport_failure", ["reader", "cleanup"])
def test_bounded_preflight_does_not_reclassify_transport_failures(
	monkeypatch: pytest.MonkeyPatch,
	transport_failure: str,
) -> None:
	original_spawn = publication_command._spawn_bounded_command
	commands: list[list[str]] = []
	missing_response = f"ERROR: {TARGET}: manifest unknown\n".encode()
	program = (
		"import sys; "
		f"sys.stderr.buffer.write({missing_response!r}); "
		"sys.stderr.flush(); sys.exit(1)"
	)

	def spawn(
		command: list[str], environment: dict[str, str]
	) -> publication_process._CommandSession:
		commands.append(command)
		return original_spawn([sys.executable, "-B", "-c", program], environment)

	monkeypatch.setattr(publication_command, "_spawn_bounded_command", spawn)
	if transport_failure == "reader":
		capture_response = publication_github.BoundedCommand._capture_response

		def fail_stderr_reader(
			_stream: BinaryIO, _collected: bytearray, limit: int, _overflow_message: str
		) -> None:
			if limit == publication_command.MAX_COMMAND_STDERR_BYTES:
				raise PublicationError("simulated bounded stderr reader failure")
			capture_response(_stream, _collected, limit, _overflow_message)

		monkeypatch.setattr(
			publication_github.BoundedCommand,
			"_capture_response",
			staticmethod(fail_stderr_reader),
		)
	else:
		cleanup = publication_github.BoundedCommand._cleanup_session

		def fail_cleanup(
			session: publication_process._CommandSession,
			threads: list[Thread],
		) -> str:
			cleanup(session, threads)
			return "simulated process-tree cleanup failure"

		monkeypatch.setattr(
			publication_github.BoundedCommand,
			"_cleanup_session",
			staticmethod(fail_cleanup),
		)

	with pytest.raises(PublicationError) as error:
		preflight_image_archive(_archive(), SOURCE, TARGET, BoundedCommand(5))
	assert not isinstance(error.value, MissingResourceError)
	assert commands == [["docker", "buildx", "imagetools", "inspect", TARGET, "--raw"]]


def test_bounded_preflight_does_not_classify_a_signaled_child(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	original_spawn = publication_command._spawn_bounded_command
	commands: list[list[str]] = []
	missing_response = f"ERROR: {TARGET}: manifest unknown\n".encode()
	program = (
		"import os, signal, sys; "
		f"sys.stderr.buffer.write({missing_response!r}); "
		"sys.stderr.flush(); os.kill(os.getpid(), signal.SIGTERM)"
	)

	def spawn(
		command: list[str], environment: dict[str, str]
	) -> publication_process._CommandSession:
		commands.append(command)
		return original_spawn([sys.executable, "-B", "-c", program], environment)

	monkeypatch.setattr(publication_command, "_spawn_bounded_command", spawn)
	with pytest.raises(PublicationError) as error:
		preflight_image_archive(_archive(), SOURCE, TARGET, BoundedCommand(5))
	assert not isinstance(error.value, MissingResourceError)
	assert commands == [["docker", "buildx", "imagetools", "inspect", TARGET, "--raw"]]
