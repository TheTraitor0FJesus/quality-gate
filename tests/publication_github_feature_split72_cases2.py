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


def test_incomplete_process_cleanup_is_reported(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	class UnreapedProcess:
		pid = 18
		stdin = None

		def __init__(self) -> None:
			self.stdout = io.BytesIO()
			self.stderr = io.BytesIO()

		@staticmethod
		def wait(*, timeout: float) -> int:
			raise subprocess.TimeoutExpired("command", timeout)

		@staticmethod
		def poll() -> None:
			return None

		@staticmethod
		def kill() -> None:
			pass

	class FailedControl:
		@staticmethod
		def assign() -> None:
			pass

		@staticmethod
		def release() -> None:
			pass

		@staticmethod
		def close() -> None:
			raise OSError("simulated process-tree cleanup failure")

		@staticmethod
		def terminate() -> None:
			raise OSError("simulated process-tree cleanup failure")

	monkeypatch.setattr(
		publication_github,
		"_spawn_bounded_command",
		lambda _arguments, _environment: publication_github._CommandSession(
			UnreapedProcess(),
			FailedControl(),  # type: ignore[arg-type]
		),
	)
	with pytest.raises(PublicationError) as error:
		BoundedCommand(0.5)(["command"], limit=64)
	assert "timed out after 0.5 seconds" in str(error.value)
	assert "command cleanup did not complete" in str(error.value)


def test_output_limit_error_survives_cleanup_failure(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	class CompletedProcess:
		pid = 19
		stdin = None

		def __init__(self) -> None:
			self.stdout = io.BytesIO(b"x" * 65)
			self.stderr = io.BytesIO()

		@staticmethod
		def wait(*, timeout: float) -> int:
			return 0

		@staticmethod
		def poll() -> int:
			return 0

		@staticmethod
		def kill() -> None:
			pass

	class FailingControl:
		@staticmethod
		def assign() -> None:
			pass

		@staticmethod
		def release() -> None:
			pass

		@staticmethod
		def close() -> None:
			raise OSError("simulated process-tree cleanup failure")

		@staticmethod
		def terminate() -> None:
			pass

	monkeypatch.setattr(
		publication_github,
		"_spawn_bounded_command",
		lambda _arguments, _environment: publication_github._CommandSession(
			CompletedProcess(),
			FailingControl(),  # type: ignore[arg-type]
		),
	)
	with pytest.raises(PublicationError) as error:
		BoundedCommand(5)(["command"], limit=64)
	assert str(error.value).startswith("external command response exceeds its size limit")
	assert "command cleanup did not complete" in str(error.value)


def test_command_reader_errors_trigger_teardown() -> None:
	class BrokenStream:
		@staticmethod
		def read(_size: int) -> bytes:
			raise OSError("simulated output read failure")

		@staticmethod
		def close() -> None:
			pass

	class Control:
		terminated = False

		def terminate(self) -> None:
			self.terminated = True

	output = publication_github._CommandOutput()
	control = Control()
	BoundedCommand._capture_stream(
		BrokenStream(),
		bytearray(),
		64,
		"size limit",
		output,
		control,
		"stdout",
		b"",  # type: ignore[arg-type]
	)

	assert control.terminated
	assert output.failure() == "external command stdout could not be read"


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
			assert (directory / "config.json").stat().st_mode & 0o777 == 0o600
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
