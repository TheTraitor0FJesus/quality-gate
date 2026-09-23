"""Transport contracts at the shared publisher's GitHub CLI seam."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from quality_gate.publication import PublicationError
from quality_gate.publication_github import (
	BoundedCommand,
	EscapeSequenceError,
	GitHubCLI,
	MissingResourceError,
	_operation,
)


class Command:
	def __init__(self, responses: list[bytes]) -> None:
		self.responses = responses
		self.calls: list[list[str]] = []

	def __call__(
		self, arguments: Sequence[str], *, content: bytes | None = None, limit: int
	) -> bytes:
		self.calls.append(list(arguments))
		return self.responses.pop(0)


def test_source_reads_exact_commit_as_data_through_gh() -> None:
	command = Command(
		[
			json.dumps(
				{"encoding": "base64", "content": base64.b64encode(b'version = "2.1.0"\n').decode()}
			).encode()
		]
	)
	api = GitHubCLI("o/r", timeout_seconds=120, command=command)
	assert api.source("a" * 40, ".release/version.toml") == 'version = "2.1.0"\n'
	assert command.calls[0][:3] == [
		"gh",
		"api",
		"repos/o/r/contents/.release/version.toml?ref=" + "a" * 40,
	]
	assert "--method" in command.calls[0]


@pytest.mark.parametrize(
	"path", ["../secrets", "/absolute/file", "x/../../file", "x\\file", "file?ref=main"]
)
def test_source_paths_cannot_escape_exact_source(path: str) -> None:
	command = Command([])
	with pytest.raises(PublicationError):
		GitHubCLI("o/r", timeout_seconds=120, command=command).source("a" * 40, path)
	assert command.calls == []


def test_api_errors_are_not_interpreted_as_absent_resources() -> None:
	command = Command([b'{"message":"forbidden"}'])
	with pytest.raises(PublicationError):
		GitHubCLI("o/r", timeout_seconds=120, command=command).get("/releases")


def test_image_readback_uses_content_digest_without_running_product() -> None:
	manifest = b"abc"
	command = Command([manifest])
	api = GitHubCLI("o/r", timeout_seconds=120, command=command)
	assert (
		api.image_digest("ghcr.io/o/image@sha256:" + "c" * 64)
		== "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
	)
	assert command.calls == [
		["docker", "buildx", "imagetools", "inspect", "ghcr.io/o/image@sha256:" + "c" * 64, "--raw"]
	]


@pytest.mark.parametrize("program", ["import time; time.sleep(30)", "print('x' * 1024)"])
def test_external_command_deadline_and_size_limits_fail_closed(program: str) -> None:
	with pytest.raises(PublicationError):
		BoundedCommand(0.2)([sys.executable, "-B", "-c", program], limit=64)


def test_external_command_diagnostics_expose_stage_status_without_stderr() -> None:
	program = (
		"import sys; sys.stderr.write('Authorization: bearer secret-token HTTP 403\\n'); "
		"sys.exit(7)"
	)
	with pytest.raises(PublicationError) as error:
		BoundedCommand(5)([sys.executable, "-B", "-c", program], limit=64)
	message = str(error.value)
	assert message == "external command: command failed (exit 7, HTTP 403)"
	assert "secret-token" not in message
	assert "Authorization" not in message


def test_external_command_404_remains_optional_without_stderr() -> None:
	program = "import sys; sys.stderr.write('HTTP 404 secret-token\\n'); sys.exit(1)"
	with pytest.raises(MissingResourceError) as error:
		BoundedCommand(5)([sys.executable, "-B", "-c", program], limit=64)
	message = str(error.value)
	assert message == "external command: resource is absent (HTTP 404)"
	assert "secret-token" not in message


def test_external_command_marks_gh_escape_guard_without_stderr() -> None:
	program = (
		"import sys; sys.stderr.write("
		"'the response contains terminal escape sequences; pass --allow-escape-sequences "
		"secret-token\\n'); sys.exit(1)"
	)
	with pytest.raises(EscapeSequenceError) as error:
		BoundedCommand(5)([sys.executable, "-B", "-c", program], limit=64)
	message = str(error.value)
	assert message == "external command: raw response contains terminal escape sequences"
	assert "secret-token" not in message


@pytest.mark.parametrize(
	"stderr",
	[
		"HTTP 403; pass --allow-escape-sequences secret-token\\n",
		"unknown flag: --allow-escape-sequences\\n",
	],
)
def test_external_command_does_not_trust_an_unrelated_escape_flag_hint(stderr: str) -> None:
	program = f"import sys; sys.stderr.write({stderr!r}); sys.exit(1)"
	with pytest.raises(PublicationError) as error:
		BoundedCommand(5)([sys.executable, "-B", "-c", program], limit=64)
	assert not isinstance(error.value, EscapeSequenceError)


def test_external_command_preserves_http_status_with_escape_text() -> None:
	program = (
		"import sys; sys.stderr.write('HTTP 403: the response contains terminal escape sequences; "
		"pass --allow-escape-sequences secret-token\\n'); sys.exit(1)"
	)
	with pytest.raises(PublicationError) as error:
		BoundedCommand(5)([sys.executable, "-B", "-c", program], limit=64)
	assert not isinstance(error.value, EscapeSequenceError)
	assert str(error.value) == "external command: command failed (exit 1, HTTP 403)"


def test_external_command_timeout_is_distinguished_from_exit_failure() -> None:
	with pytest.raises(PublicationError, match=r"^external command: timed out after"):
		BoundedCommand(0.2)([sys.executable, "-B", "-c", "import time; time.sleep(30)"], limit=64)


def test_missing_executable_is_distinguished_from_timeout(tmp_path: Path) -> None:
	missing = tmp_path / "missing-command"
	with pytest.raises(PublicationError, match=r"^external command: command unavailable$") as error:
		BoundedCommand(5)([str(missing)], limit=64)
	assert str(missing) not in str(error.value)


def test_gh_operation_removes_query_and_untrusted_endpoint_text() -> None:
	assert (
		_operation(
			[
				"gh",
				"api",
				"repos/o/r/actions/runs/7/jobs?filter=all&token=secret",
				"--method",
				"GET",
			]
		)
		== "gh api GET repos/o/r/actions/runs/7/jobs"
	)
	assert (
		_operation(
			(
				"gh",
				"api",
				"https://example.invalid/?token=secret",
				"--method",
				"POST",
			)
		)
		== "gh api POST <endpoint>"
	)


def test_registry_credentials_are_temporary_and_cannot_launch_helpers() -> None:
	from quality_gate.publication_github import registry_configuration

	original = os.environ.get("DOCKER_CONFIG")
	configuration = '{"auths":{"ghcr.io":{"auth":"dXNlcjpwYXNz"}}}'
	with registry_configuration(configuration):
		directory = Path(os.environ["DOCKER_CONFIG"])
		assert json.loads((directory / "config.json").read_text()) == json.loads(configuration)
	assert not directory.exists()
	assert os.environ.get("DOCKER_CONFIG") == original
	with pytest.raises(PublicationError):
		with registry_configuration('{"credHelpers":{"ghcr.io":"product-startup"}}'):
			pytest.fail("executable credential helper was accepted")


@pytest.mark.parametrize("revision", ["a" * 40, "main"])
def test_workflow_entrypoint_rejects_wrong_or_floating_helper_before_github(
	revision: str, tmp_path: Path
) -> None:
	source = Path(__file__).resolve().parents[1]
	root = tmp_path / "provider"
	# Staged Quality Gate snapshots intentionally contain no .git metadata.
	for relative in [
		"scripts/source_release.py",
		".release/release-tools.toml",
		*[str(path.relative_to(source)) for path in (source / "quality_gate").glob("*.py")],
	]:
		target = root / relative
		target.parent.mkdir(parents=True, exist_ok=True)
		shutil.copyfile(source / relative, target)
	environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
	environment.update(
		{
			"PUBLISHER_SHA": revision,
			"GIT_CONFIG_GLOBAL": str(tmp_path / "missing-global-config"),
			"GIT_CONFIG_NOSYSTEM": "1",
		}
	)
	for arguments in [
		["init"],
		["add", "."],
		[
			"-c",
			"user.name=Fixture",
			"-c",
			"user.email=fixture@example.invalid",
			"commit",
			"-m",
			"fixture",
		],
	]:
		subprocess.run(
			["git", *arguments],
			cwd=root,
			env=environment,
			capture_output=True,
			check=True,
			timeout=10,
		)
	environment.pop("GH_TOKEN", None)
	environment.pop("GITHUB_TOKEN", None)
	environment.pop("PUBLISHER_REGISTRY_AUTH", None)
	result = subprocess.run(
		[
			sys.executable,
			"-B",
			"-I",
			str(root / "scripts/source_release.py"),
			"prepare",
			"--pr",
			"7",
			"--output",
			"unused-output",
		],
		cwd=root,
		env=environment,
		capture_output=True,
		text=True,
		timeout=10,
	)
	assert result.returncode == 1
	assert (
		"helper checkout differs" in result.stderr or "full lowercase commit SHAs" in result.stderr
	)
	assert not result.stdout


def test_job_log_download_retries_escape_guard_with_gh_opt_in() -> None:
	class EscapeRetryCommand:
		def __init__(self) -> None:
			self.calls: list[list[str]] = []

		def __call__(
			self, arguments: Sequence[str], *, content: bytes | None = None, limit: int
		) -> bytes:
			self.calls.append(list(arguments))
			if len(self.calls) == 1:
				raise EscapeSequenceError("guarded raw response")
			return b"Artifact ID: 300\n"

	command = EscapeRetryCommand()
	api = GitHubCLI("o/r", timeout_seconds=120, command=command)
	assert api.download("/actions/jobs/42/logs") == b"Artifact ID: 300\n"
	assert len(command.calls) == 2
	assert "--allow-escape-sequences" not in command.calls[0]
	assert "--allow-escape-sequences" in command.calls[1]


def test_job_log_download_preserves_fail_closed_on_retry_failure() -> None:
	class EscapeCommand:
		def __init__(self) -> None:
			self.calls: list[list[str]] = []

		def __call__(
			self, arguments: Sequence[str], *, content: bytes | None = None, limit: int
		) -> bytes:
			self.calls.append(list(arguments))
			raise EscapeSequenceError("guarded raw response")

	command = EscapeCommand()
	api = GitHubCLI("o/r", timeout_seconds=120, command=command)
	with pytest.raises(EscapeSequenceError):
		api.download("/actions/jobs/42/logs")
	assert len(command.calls) == 2
	assert "--allow-escape-sequences" in command.calls[1]


@pytest.mark.parametrize(
	"message",
	[
		"gh api GET repos/o/r/actions/jobs/42/logs: command failed (exit 1, HTTP 403)",
		"gh api GET repos/o/r/actions/jobs/42/logs: timed out after 60 seconds",
	],
)
def test_job_log_download_does_not_retry_other_failures(message: str) -> None:
	class FailingCommand:
		def __init__(self) -> None:
			self.calls: list[list[str]] = []

		def __call__(
			self, arguments: Sequence[str], *, content: bytes | None = None, limit: int
		) -> bytes:
			self.calls.append(list(arguments))
			raise PublicationError(message)

	command = FailingCommand()
	api = GitHubCLI("o/r", timeout_seconds=120, command=command)
	with pytest.raises(PublicationError, match="(HTTP 403|timed out after)"):
		api.download("/actions/jobs/42/logs")
	assert len(command.calls) == 1


def test_job_log_download_keeps_plain_gh_compatible_without_flag() -> None:
	command = Command([b"Artifact ID: 300\n"])
	api = GitHubCLI("o/r", timeout_seconds=120, command=command)
	assert api.download("/actions/jobs/42/logs") == b"Artifact ID: 300\n"
	assert len(command.calls) == 1
	assert "--allow-escape-sequences" not in command.calls[0]


def test_artifact_download_does_not_retry_escape_guard() -> None:
	class EscapeCommand:
		def __init__(self) -> None:
			self.calls: list[list[str]] = []

		def __call__(
			self, arguments: Sequence[str], *, content: bytes | None = None, limit: int
		) -> bytes:
			self.calls.append(list(arguments))
			raise EscapeSequenceError("guarded raw response")

	command = EscapeCommand()
	api = GitHubCLI("o/r", timeout_seconds=120, command=command)
	with pytest.raises(EscapeSequenceError):
		api.download("/actions/artifacts/7/zip")
	assert len(command.calls) == 1
	assert "--allow-escape-sequences" not in command.calls[0]


@pytest.mark.parametrize("oversized", [False, True])
def test_evidence_file_read_has_an_ingestion_bound(tmp_path: Path, oversized: bool) -> None:
	from quality_gate.publication_artifacts import load_json_record

	path = tmp_path / "event.json"
	path.write_bytes(b'{"action":"edited"}' + b" " * (2 * 1024 * 1024 if oversized else 0))
	with path.open("rb") as stream:
		if oversized:
			with pytest.raises(PublicationError, match="exceeds 1 MiB"):
				load_json_record(stream)
			assert stream.tell() <= 1024 * 1024 + 1
		else:
			assert load_json_record(stream) == {"action": "edited"}
