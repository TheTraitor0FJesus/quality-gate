"""Related behavior tests."""

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
from test_publication_github import (
	Command,
)

from quality_gate.publication import PublicationError
from quality_gate.publication_cli import _main as publisher_main
from quality_gate.publication_command_policy import _operation
from quality_gate.publication_github import EscapeSequenceError, GitHubCLI, registry_configuration

EXPECTED_CONFIG_MODE = 0o600
EXPECTED_RETRY_ATTEMPTS = 2


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


@pytest.mark.parametrize(
	("command", "expected"),
	[
		(
			["docker", "buildx", "imagetools", "inspect", "ghcr.io/owner/image:v1.2.3", "--raw"],
			"docker buildx imagetools",
		),
		(
			["docker", "image", "inspect", "--format", "{{json .}}", "private-reference"],
			"docker image inspect",
		),
		(
			["docker", "image", "tag", "private-source", "private-target"],
			"docker image tag",
		),
		(["docker", "load", "--input", "private-path"], "docker load"),
		(["docker", "push", "private-target"], "docker push"),
		(["docker", "unexpected", "--token=secret"], "docker command"),
	],
)
def test_docker_operation_uses_only_an_allowlisted_subcommand(
	command: list[str], expected: str
) -> None:
	operation = _operation(command)

	assert operation == expected
	assert "private" not in operation
	assert "secret" not in operation


def test_builtin_token_uses_isolated_config_and_restores_existing_docker_config(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	existing_directory = tmp_path / "existing-docker-config"
	existing_directory.mkdir()
	existing_config = {
		"auths": {
			"ghcr.io": {"auth": "prior"},
			"registry.example": {"auth": "external"},
		}
	}
	existing_config_path = existing_directory / "config.json"
	existing_config_path.write_text(json.dumps(existing_config), encoding="utf-8")
	monkeypatch.setenv("DOCKER_CONFIG", str(existing_directory))
	username = "github-actions[bot]"
	token = "short-lived-token"
	with registry_configuration(token, username):
		directory = Path(os.environ["DOCKER_CONFIG"])
		configuration = json.loads((directory / "config.json").read_text())
		assert configuration == {
			"auths": {
				"ghcr.io": {
					"auth": base64.b64encode(f"{username}:{token}".encode()).decode("ascii")
				}
			}
		}
		if os.name != "nt":
			assert (directory / "config.json").stat().st_mode & 0o777 == EXPECTED_CONFIG_MODE
	assert not directory.exists()
	assert os.environ["DOCKER_CONFIG"] == str(existing_directory)
	assert json.loads(existing_config_path.read_text(encoding="utf-8")) == existing_config


@pytest.mark.parametrize(("token", "username"), [("", "actor"), ("token", ""), ("", "")])
def test_ghcr_configuration_requires_both_caller_credentials(token: str, username: str) -> None:
	with pytest.raises(PublicationError, match="GITHUB_TOKEN and actor are required"):
		with registry_configuration(token, username):
			pytest.fail("incomplete caller credentials were accepted")


@pytest.mark.parametrize("username", ["actor:other", "actor\nother"])
def test_ghcr_configuration_rejects_invalid_actor_names(username: str) -> None:
	with pytest.raises(PublicationError, match="actor is invalid"):
		with registry_configuration("token", username):
			pytest.fail("invalid actor was accepted")


def test_ghcr_configuration_rejects_invalid_token_characters() -> None:
	with pytest.raises(PublicationError, match="GHCR token contains invalid characters"):
		with registry_configuration("token\nvalue", "actor"):
			pytest.fail("multiline token was accepted")


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
			timeout=30,
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


def test_publish_does_not_require_a_separate_release_api_token(
	monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
	from quality_gate import publication_cli

	class Publisher:
		@staticmethod
		def publish(_pr: int, _candidate_id: int) -> dict[str, str]:
			return {"status": "already-complete", "version": "2.3.0"}

	monkeypatch.setattr(publication_cli, "_publisher", lambda _root: Publisher())
	monkeypatch.setenv("GH_TOKEN", "built-in-token")
	monkeypatch.setenv("GITHUB_ACTOR", "github-actions[bot]")
	monkeypatch.delenv("PUBLISHER_RELEASE_TOKEN", raising=False)
	assert "PUBLISHER_RELEASE_TOKEN" not in Path(publication_cli.__file__).read_text(
		encoding="utf-8"
	)

	assert publisher_main(["publish", "--pr", "7", "--candidate-id", "9"]) == 0
	assert '"status": "already-complete"' in capsys.readouterr().out


def test_image_writer_cli_passes_the_original_pull_request_number(
	monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
	from quality_gate import publication_cli

	calls: list[tuple[int, int, Path]] = []

	class Publisher:
		@staticmethod
		def write_images(_pr: int, _candidate_id: int, _output: Path) -> dict[str, str]:
			calls.append((_pr, _candidate_id, _output))
			return {"status": "images-written", "version": "2.3.0"}

	monkeypatch.setattr(publication_cli, "_publisher", lambda _root: Publisher())
	assert (
		publisher_main(
			[
				"write-images",
				"--pr",
				"7",
				"--candidate-id",
				"9",
				"--output",
				str(tmp_path),
			]
		)
		== 0
	)
	assert '"status": "images-written"' in capsys.readouterr().out
	assert calls == [(7, 9, tmp_path)]


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
	assert len(command.calls) == EXPECTED_RETRY_ATTEMPTS
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
	assert len(command.calls) == EXPECTED_RETRY_ATTEMPTS
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
	with pytest.raises(PublicationError, match=r"(HTTP 403|timed out after)"):
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
