"""Related behavior tests."""

from __future__ import annotations

import base64
import json
import sys
import time
import tomllib
from collections.abc import Sequence
from pathlib import Path

import pytest
from test_publication_github import (
	Command,
	_clocked_bounded_command,
	_controlled_exit_command,
)

from quality_gate.publication import PublicationError
from quality_gate.publication_github import (
	MAX_IMAGE_WRITER_TIMEOUT_SECONDS,
	BoundedCommand,
	GitHubCLI,
	MissingResourceError,
)

EXPECTED_GITHUB_TIMEOUT = 60
EXPECTED_COMMAND_COUNT = 2
EXPECTED_API_CALL_COUNT = 7


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


@pytest.mark.parametrize(
	("method", "path"),
	[
		("GET", "/../other/releases"),
		("GET", "/compare/" + "a" * 40 + "..." + "b" * 40 + "/../releases"),
		("GET", "/compare/main...feature"),
		("GET", "/compare/" + "a" * 40 + "...." + "b" * 40),
		("GET", "/compare/" + "a" * 40 + "..." + "b" * 40 + "?path=../other"),
		("POST", "/compare/" + "a" * 40 + "..." + "b" * 40),
	],
)
def test_compare_exception_preserves_api_path_rejections(method: str, path: str) -> None:
	command = Command([])
	api = GitHubCLI("o/r", timeout_seconds=120, command=command)
	with pytest.raises(PublicationError, match="invalid repository API path"):
		if method == "GET":
			api.get(path)
		else:
			api.post(path, {})
	assert command.calls == []


@pytest.mark.parametrize(
	("method", "path"),
	[("GET", "/git/commits/" + "a" * 40), ("POST", "/git/commits")],
)
def test_commit_api_error_objects_remain_fail_closed(method: str, path: str) -> None:
	command = Command([b'{"message":"forbidden"}'])
	api = GitHubCLI("o/r", timeout_seconds=120, command=command)

	with pytest.raises(PublicationError, match="GitHub returned an API error object") as error:
		if method == "GET":
			api.get(path)
		else:
			api.post(path, {"message": "update", "tree": "b" * 40, "parents": ["a" * 40]})

	assert not isinstance(error.value, MissingResourceError)
	assert command.calls[0][2] == f"repos/o/r{path}"


@pytest.mark.parametrize(
	("method", "path"),
	[("GET", "/git/commits/" + "a" * 40), ("POST", "/git/commits")],
)
def test_github_http_errors_remain_fail_closed_for_commit_endpoints(
	monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
	calls = _controlled_exit_command(monkeypatch, detail=b"HTTP 403\n")
	api = GitHubCLI("o/r", timeout_seconds=5, command=BoundedCommand(5))

	with pytest.raises(PublicationError, match=r"command failed \(exit 1, HTTP 403\)") as error:
		if method == "GET":
			api.get(path)
		else:
			api.post(path, {"message": "update", "tree": "b" * 40, "parents": ["a" * 40]})

	assert not isinstance(error.value, MissingResourceError)
	assert calls[0][2] == f"repos/o/r{path}"


def test_tag_creation_403_explains_fail_closed_manual_recovery() -> None:
	class DeniedCommand:
		def __call__(
			self, _arguments: Sequence[str], *, content: bytes | None = None, limit: int
		) -> bytes:
			raise PublicationError(
				"gh api POST repos/o/r/git/refs: command failed (exit 1, HTTP 403)"
			)

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
	assert (
		BoundedCommand(
			MAX_IMAGE_WRITER_TIMEOUT_SECONDS,
			maximum_timeout_seconds=MAX_IMAGE_WRITER_TIMEOUT_SECONDS,
		).timeout_seconds
		== MAX_IMAGE_WRITER_TIMEOUT_SECONDS
	)
	with pytest.raises(PublicationError, match="timeout exceeds its configured bound"):
		BoundedCommand(MAX_IMAGE_WRITER_TIMEOUT_SECONDS)
	settings = tomllib.loads(
		(Path(__file__).resolve().parents[1] / ".release/release-tools.toml").read_text(
			encoding="utf-8"
		)
	)
	assert settings["timeouts"]["github_api_seconds"] == EXPECTED_GITHUB_TIMEOUT
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

	assert len(monitor["calls"]) == EXPECTED_COMMAND_COUNT
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
	with pytest.raises(PublicationError, match="command failed") as error:
		command(["docker", "buildx", "imagetools", "inspect"], limit=64)
	assert not isinstance(error.value, MissingResourceError)
	with pytest.raises(PublicationError, match=r"shared image-command budget.*exhausted"):
		command(["docker", "push"], limit=64)

	assert len(monitor["calls"]) == 1
	assert monitor["waits"] == [1]


def test_github_api_404_keeps_its_missing_resource_semantics(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	calls = _controlled_exit_command(monkeypatch, detail=b"HTTP 404\n")
	api = GitHubCLI("o/r", timeout_seconds=5, command=BoundedCommand(5))

	assert api.get("/releases") is None
	assert calls[0][:3] == ["gh", "api", "repos/o/r/releases"]


def test_docker_image_readback_does_not_treat_manifest_absence_as_missing_target(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	reference = "ghcr.io/o/image@sha256:" + "a" * 64
	detail = f"ERROR: {reference}: manifest unknown\n".encode()
	calls = _controlled_exit_command(monkeypatch, detail=detail)
	api = GitHubCLI("o/r", timeout_seconds=5, image_command=BoundedCommand(5))

	with pytest.raises(PublicationError) as error:
		api.image_digest(reference)
	assert not isinstance(error.value, MissingResourceError)
	assert calls[0] == [
		"docker",
		"buildx",
		"imagetools",
		"inspect",
		reference,
		"--raw",
	]


def test_release_api_reads_and_mutations_use_the_same_caller_command() -> None:
	command = Command(
		[
			b'{"id":1}',
			b"[]",
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

	assert len(command.calls) == EXPECTED_API_CALL_COUNT
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


def test_external_command_stops_when_stderr_exceeds_its_size_limit() -> None:
	program = "import sys, time; sys.stderr.write('x' * 4097); sys.stderr.flush(); time.sleep(30)"
	with pytest.raises(PublicationError, match="error output exceeds its size limit"):
		BoundedCommand(0.5)([sys.executable, "-B", "-c", program], limit=64)


@pytest.mark.parametrize("inherited_stream", ["stdout", "stderr"])
def test_external_command_stops_descendant_holding_output_pipe(
	tmp_path: Path, inherited_stream: str
) -> None:
	marker = tmp_path / "descendant-finished"
	child = (
		f"import pathlib, time; time.sleep(1); pathlib.Path({str(marker)!r}).write_text('finished')"
	)
	stdout = "None" if inherited_stream == "stdout" else "subprocess.DEVNULL"
	stderr = "None" if inherited_stream == "stderr" else "subprocess.DEVNULL"
	program = (
		"import subprocess, sys; "
		f"subprocess.Popen([sys.executable, '-B', '-c', {child!r}], "
		f"stdin=subprocess.DEVNULL, stdout={stdout}, stderr={stderr}); "
		"print('finished')"
	)

	output = BoundedCommand(0.5)([sys.executable, "-B", "-c", program], limit=64)
	time.sleep(1.2)

	assert output.splitlines() == [b"finished"]
	assert not marker.exists()
