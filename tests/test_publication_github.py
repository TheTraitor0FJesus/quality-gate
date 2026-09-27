"""Transport contracts at the shared publisher's GitHub CLI seam."""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

import pytest
from quality_gate import publication_github
from quality_gate.publication import PublicationError
from quality_gate.publication_cli import _main as publisher_main
from quality_gate.publication_github import (
	MAX_IMAGE_WRITER_TIMEOUT_SECONDS,
	BoundedCommand,
	EscapeSequenceError,
	GitHubCLI,
	MissingResourceError,
	_operation,
	registry_configuration,
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


def _clocked_bounded_command(
	monkeypatch: pytest.MonkeyPatch,
	*,
	launch_seconds: list[float],
	wait_seconds: list[float],
	exit_codes: list[int] | None = None,
) -> tuple[BoundedCommand, dict[str, list[object]], list[float]]:
	now = [0.0]
	monitor: dict[str, list[object]] = {
		"calls": [],
		"timers": [],
		"waits": [],
	}
	codes = exit_codes or [0] * len(launch_seconds)

	class FakeTimer:
		def __init__(self, interval: float, _callback: object) -> None:
			monitor["timers"].append(interval)

		def start(self) -> None:
			pass

		def cancel(self) -> None:
			pass

	class FakeProcess:
		def __init__(
			self, arguments: list[str], *, stderr: io.BufferedRandom, **_kwargs: object
		) -> None:
			self.index = len(monitor["calls"])
			monitor["calls"].append(arguments)
			now[0] += launch_seconds[self.index]
			self.stdout = io.BytesIO()
			self.code = codes[self.index]
			if self.code:
				stderr.write(b"HTTP 404\n")

		def __enter__(self) -> FakeProcess:
			return self

		def __exit__(self, *_args: object) -> None:
			return None

		def wait(self, *, timeout: float) -> int:
			monitor["waits"].append(timeout)
			now[0] += wait_seconds[self.index]
			return self.code

		def kill(self) -> None:
			self.code = -9

	monkeypatch.setattr(publication_github, "monotonic", lambda: now[0])
	monkeypatch.setattr(publication_github, "Timer", FakeTimer)
	monkeypatch.setattr(publication_github.subprocess, "Popen", FakeProcess)
	return BoundedCommand(10, aggregate_timeout_seconds=10), monitor, now


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


def test_tag_creation_403_explains_fail_closed_manual_recovery() -> None:
	class DeniedCommand:
		def __call__(
			self, _arguments: Sequence[str], *, content: bytes | None = None, limit: int
		) -> bytes:
			raise PublicationError("gh api POST repos/o/r/git/refs: command failed (exit 1, HTTP 403)")

	api = GitHubCLI("o/r", timeout_seconds=60, command=DeniedCommand())
	with pytest.raises(PublicationError) as error:
		api.post("/git/refs", {"ref": "refs/tags/v2.3.0", "sha": "a" * 40})
	message = str(error.value)
	assert "Workflows: write restriction" in message
	assert "keep the original candidate and its source SHA" in message
	assert "create and push only that exact tag" in message
	assert "rerun only the failed Publish release job in this original workflow run" in message
	assert "do not dispatch a new run, rerun all jobs, or repush the image" in message
	assert "artifact or upload log is missing or expired" in message
	assert "Do not change the source or expand GITHUB_TOKEN permissions" in message


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


def test_image_commands_use_a_separate_bounded_writer_timeout() -> None:
	api_command = Command([b"{}"])
	image_command = Command([b"manifest"])
	api = GitHubCLI(
		"o/r",
		timeout_seconds=60,
		command=api_command,
		image_command=image_command,
	)

	assert api.get("/actions/runs/7") == {}
	assert api.image_digest("ghcr.io/o/image@sha256:" + "c" * 64)
	assert api_command.calls[0][:2] == ["gh", "api"]
	assert image_command.calls[0][:3] == ["docker", "buildx", "imagetools"]
	assert BoundedCommand(
		MAX_IMAGE_WRITER_TIMEOUT_SECONDS,
		maximum_timeout_seconds=MAX_IMAGE_WRITER_TIMEOUT_SECONDS,
	).timeout_seconds == MAX_IMAGE_WRITER_TIMEOUT_SECONDS
	with pytest.raises(PublicationError, match="timeout exceeds its configured bound"):
		BoundedCommand(MAX_IMAGE_WRITER_TIMEOUT_SECONDS)
	settings = tomllib.loads(
		(Path(__file__).resolve().parents[1] / ".release/release-tools.toml").read_text(
			encoding="utf-8"
		)
	)
	assert settings["timeouts"]["github_api_seconds"] == 60
	assert settings["timeouts"]["image_writer_seconds"] == MAX_IMAGE_WRITER_TIMEOUT_SECONDS


def test_sequential_image_commands_share_one_aggregate_deadline(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	command, monitor, _now = _clocked_bounded_command(
		monkeypatch,
		launch_seconds=[1, 1],
		wait_seconds=[1.5, 1.5],
	)
	command(["docker", "load"], limit=64)
	command(["docker", "push"], limit=64)

	assert len(monitor["calls"]) == 2
	assert monitor["timers"] == pytest.approx([9, 6.5])
	assert monitor["waits"] == pytest.approx([9, 6.5])


def test_image_command_failure_does_not_reset_or_bypass_exhausted_budget(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	command, monitor, _now = _clocked_bounded_command(
		monkeypatch,
		launch_seconds=[9, 0],
		wait_seconds=[1, 0],
		exit_codes=[1, 0],
	)
	with pytest.raises(MissingResourceError, match="resource is absent"):
		command(["docker", "buildx", "imagetools", "inspect"], limit=64)
	with pytest.raises(PublicationError, match="shared image-command budget.*exhausted"):
		command(["docker", "push"], limit=64)

	assert len(monitor["calls"]) == 1
	assert monitor["timers"] == [1]
	assert monitor["waits"] == [1]


def test_release_api_reads_and_mutations_use_the_same_caller_command() -> None:
	command = Command(
		[
			b'{"id":1}',
			b'[]',
			b'{"id":3}',
			b'{"ref":"refs/tags/v2.2.0"}',
			b'{"id":2}',
			b'{"id":2}',
			b'{"digest":"sha256:' + b"a" * 64 + b'"}',
		]
	)
	api = GitHubCLI(
		"o/r",
		timeout_seconds=120,
		command=command,
	)
	api.get("/actions/runs/7")
	assert api.get("/releases?per_page=100&page=1") == []
	assert api.get("/releases/3") == {"id": 3}
	api.post("/git/refs", {"ref": "refs/tags/v2.2.0", "sha": "a" * 40})
	api.post("/releases", {"tag_name": "v2.2.0"})
	api.patch("/releases/2", {"draft": False})
	api.upload(2, "quality-gate.zip", b"asset")

	assert len(command.calls) == 7
	assert [call[2] for call in command.calls[1:6]] == [
		"repos/o/r/releases?per_page=100&page=1",
		"repos/o/r/releases/3",
		"repos/o/r/git/refs",
		"repos/o/r/releases",
		"repos/o/r/releases/2",
	]
	assert command.calls[6][2].startswith("https://uploads.github.com/repos/o/r/releases/2")


def test_bounded_command_inherits_the_automatically_issued_token(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	monkeypatch.setenv("GH_TOKEN", "built-in-token")
	output = BoundedCommand(5)(
		[
			sys.executable,
			"-B",
			"-c",
			"import os; print(os.environ['GH_TOKEN'])",
		],
		limit=64,
	)
	assert output.splitlines() == [b"built-in-token"]


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


def test_registry_missing_tag_status_is_distinguished_without_diagnostics() -> None:
	program = (
		"import sys; sys.stderr.write("
		"'response status code 404: Not Found secret-token\\n'); sys.exit(1)"
	)
	with pytest.raises(MissingResourceError) as error:
		BoundedCommand(5)([sys.executable, "-B", "-c", program], limit=64)
	assert "secret-token" not in str(error.value)


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
			assert (directory / "config.json").stat().st_mode & 0o777 == 0o600
	assert not directory.exists()
	assert os.environ["DOCKER_CONFIG"] == str(existing_directory)
	assert json.loads(existing_config_path.read_text(encoding="utf-8")) == existing_config


@pytest.mark.parametrize(
	("token", "username"), [("", "actor"), ("token", ""), ("", "")]
)
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
	assert "PUBLISHER_RELEASE_TOKEN" not in Path(
		publication_cli.__file__
	).read_text(encoding="utf-8")

	assert publisher_main(["publish", "--pr", "7", "--candidate-id", "9"]) == 0
	assert '"status": "already-complete"' in capsys.readouterr().out


def test_image_writer_cli_passes_the_original_pull_request_number(
	monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
	from quality_gate import publication_cli

	class Publisher:
		@staticmethod
		def write_images(_pr: int, _candidate_id: int, _output: Path) -> dict[str, str]:
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
