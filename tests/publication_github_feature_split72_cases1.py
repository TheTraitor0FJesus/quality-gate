"""Feature test partition."""

from __future__ import annotations

from publication_github_feature_split72_support import (
	MAX_IMAGE_WRITER_TIMEOUT_SECONDS as MAX_IMAGE_WRITER_TIMEOUT_SECONDS,
)
from publication_github_feature_split72_support import (
	WINDOWS_MODULE_PATH_BUFFER_CHARS as WINDOWS_MODULE_PATH_BUFFER_CHARS,
)
from publication_github_feature_split72_support import (
	BoundedCommand as BoundedCommand,
)
from publication_github_feature_split72_support import (
	Command as Command,
)
from publication_github_feature_split72_support import (
	EscapeSequenceError as EscapeSequenceError,
)
from publication_github_feature_split72_support import (
	GitHubCLI as GitHubCLI,
)
from publication_github_feature_split72_support import (
	MissingResourceError as MissingResourceError,
)
from publication_github_feature_split72_support import (
	Path as Path,
)
from publication_github_feature_split72_support import (
	PublicationError as PublicationError,
)
from publication_github_feature_split72_support import (
	Sequence as Sequence,
)
from publication_github_feature_split72_support import (
	_clocked_bounded_command as _clocked_bounded_command,
)
from publication_github_feature_split72_support import (
	_controlled_exit_command as _controlled_exit_command,
)
from publication_github_feature_split72_support import (
	_operation as _operation,
)
from publication_github_feature_split72_support import (
	base64 as base64,
)
from publication_github_feature_split72_support import (
	ctypes as ctypes,
)
from publication_github_feature_split72_support import (
	io as io,
)
from publication_github_feature_split72_support import (
	json as json,
)
from publication_github_feature_split72_support import (
	os as os,
)
from publication_github_feature_split72_support import (
	publication_github as publication_github,
)
from publication_github_feature_split72_support import (
	publisher_main as publisher_main,
)
from publication_github_feature_split72_support import (
	pytest as pytest,
)
from publication_github_feature_split72_support import (
	registry_configuration as registry_configuration,
)
from publication_github_feature_split72_support import (
	shutil as shutil,
)
from publication_github_feature_split72_support import (
	subprocess as subprocess,
)
from publication_github_feature_split72_support import (
	sys as sys,
)
from publication_github_feature_split72_support import (
	time as time,
)
from publication_github_feature_split72_support import (
	tomllib as tomllib,
)


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
	with pytest.raises(PublicationError, match="shared image-command budget.*exhausted"):
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


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object containment")
def test_windows_job_assignment_failure_does_not_launch_target(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	marker = tmp_path / "uncontained-command-ran"
	program = f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"

	def fail_assignment(_job: object, _process_id: int) -> None:
		raise OSError("simulated Job Object assignment failure")

	monkeypatch.setattr(publication_github._WindowsJob, "assign", fail_assignment)
	with pytest.raises(PublicationError):
		BoundedCommand(5)([sys.executable, "-B", "-c", program], limit=64)

	assert not marker.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object containment")
def test_windows_job_contains_the_helper_that_launches_the_target(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	assigned_process_ids: list[int] = []
	original_assign = publication_github._WindowsJob.assign

	def record_assignment(job: object, process_id: int) -> None:
		original_assign(job, process_id)  # type: ignore[arg-type]
		assigned_process_ids.append(process_id)

	monkeypatch.setattr(publication_github._WindowsJob, "assign", record_assignment)
	from ctypes import wintypes

	kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
	get_module_file_name = kernel32.GetModuleFileNameW
	get_module_file_name.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
	get_module_file_name.restype = wintypes.DWORD
	executable_buffer = ctypes.create_unicode_buffer(WINDOWS_MODULE_PATH_BUFFER_CHARS)
	path_length = get_module_file_name(None, executable_buffer, len(executable_buffer))
	if path_length == 0:
		error_code = ctypes.get_last_error()  # type: ignore[attr-defined]
		raise OSError(error_code, "GetModuleFileNameW failed")
	if path_length >= len(executable_buffer):
		raise OSError("running Python executable path exceeds the Windows path limit")
	current_executable = Path(executable_buffer.value)
	output = BoundedCommand(5)(
		[str(current_executable), "-B", "-c", "import os; print(os.getppid())"], limit=64
	)

	assert len(assigned_process_ids) == 1
	assert output.splitlines() == [str(assigned_process_ids[0]).encode("ascii")]


def test_timeout_stays_primary_when_reader_fails_during_cleanup(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	output = publication_github._CommandOutput()

	class FakeControl:
		@staticmethod
		def terminate() -> None:
			pass

	class FakeProcess:
		pid = 1
		stdin = None

	control = FakeControl()
	session = publication_github._CommandSession(  # type: ignore[arg-type]
		FakeProcess(), control
	)

	def fail_reader_during_cleanup(
		_session: publication_github._CommandSession, _threads: list[object]
	) -> None:
		output.fail("external command stdout stream could not be closed", control)  # type: ignore[arg-type]
		return None

	monkeypatch.setattr(
		BoundedCommand,
		"_cleanup_session",
		staticmethod(fail_reader_during_cleanup),
	)
	error = BoundedCommand(0.5)._cleanup_after_failure(
		subprocess.TimeoutExpired("command", 0.5),
		"external command",
		None,
		session,
		[],
		output,
	)

	assert error is not None
	assert "external command: timed out after 0.5 seconds" in str(error)
	assert "secondary output reader failure" in str(error)


def test_target_exit_125_is_not_reported_as_helper_failure() -> None:
	with pytest.raises(PublicationError, match=r"command failed \(exit 125\)"):
		BoundedCommand(5)([sys.executable, "-B", "-c", "raise SystemExit(125)"], limit=64)


def test_posix_process_group_kill_failure_is_not_ignored_after_leader_exit(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	class ExitedProcess:
		pid = 17
		stdin = None

		@staticmethod
		def poll() -> int:
			return 0

	def fail_killpg(_pid: int, _signal: int) -> None:
		raise PermissionError("simulated process-group kill failure")

	monkeypatch.setattr(publication_github.os, "name", "posix")
	monkeypatch.setattr(publication_github.os, "killpg", fail_killpg, raising=False)
	monkeypatch.setattr(publication_github.signal, "SIGKILL", 9, raising=False)
	control = publication_github._CommandControl(ExitedProcess())  # type: ignore[arg-type]

	with pytest.raises(PermissionError, match="process-group kill failure"):
		control.close()
