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
