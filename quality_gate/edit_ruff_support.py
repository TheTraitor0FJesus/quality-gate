"""Ruff evidence identities, reports and guarded source publication."""

from __future__ import annotations

import json
import os
import platform
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .contracts import Manifest, PythonComponent
from .edit_inputs import (
	SourceFile,
	read_bounded,
	regular_path,
)
from .edit_policy import sha256
from .edit_protocol import (
	MAX_FILE_BYTES,
	EditError,
)
from .verification_evidence import (
	repository_digest,
	ruff_file_identity,
)

if TYPE_CHECKING:
	from .edit_invocation import Invocation


@dataclass(slots=True)
class Selection:
	tool: Path
	policy: Path
	identity: dict[str, Any]


def _format_report(code: int, output: bytes, workspace: Path, included: set[str]) -> set[str]:
	try:
		report = json.loads(output)
		if not isinstance(report, list) or (code == 0 and report):
			raise EditError("tool_output")
		unformatted = {
			Path(item["filename"]).resolve().relative_to(workspace).as_posix() for item in report
		}
		if not unformatted <= included or (code == 1 and not unformatted):
			raise EditError("tool_output")
		return unformatted
	except (ValueError, TypeError, KeyError) as error:
		raise EditError("tool_output") from error


def _file_local_identity(
	invocation: Invocation, file: SourceFile, selection: Selection
) -> dict[str, Any] | None:
	engine = invocation.engine_identity
	if engine is None or file.mode is None:
		return None
	repository = repository_digest(invocation.root)
	if repository is None:
		return None
	file_parent = Path(file.path).parent
	configuration = [
		item
		for item in invocation.configuration.as_list()
		if isinstance(path := item["path"], str)
		and path != ".git/info/exclude"
		and Path(path).name
		in {
			"quality-gate.toml",
			"pyproject.toml",
			"ruff.toml",
			".ruff.toml",
			".gitignore",
			".ignore",
		}
		and (
			Path(path).parent == Path(".")
			or Path(path).parent == file_parent
			or Path(path).parent in file_parent.parents
		)
	]
	identity = selection.identity
	return ruff_file_identity(
		engine=engine,
		repository=repository,
		path=file.path,
		mode=file.mode,
		content_sha256=file.sha256,
		tool_version=identity["ruff_version"],
		tool_sha256=identity["ruff_sha256"],
		policy_release=identity["policy_release"],
		release_sha256=identity["release_sha256"],
		policy_sha256=identity["policy_sha256"],
		python_version=identity["python_version"],
		python_sha256=identity["python_sha256"],
		runtime_fingerprint=identity["runtime_fingerprint"],
		platform=f"{sys.platform}-{platform.machine().casefold()}",
		configuration=configuration,
	)


def _component(manifest: Manifest | None, path: str) -> PythonComponent | None:
	if manifest is None:
		return None
	return next(
		(component for component in manifest.python if Path(path).is_relative_to(component.path)),
		None,
	)


def _publish(
	invocation: Invocation, files: list[SourceFile], workspace: Path, records: list[dict[str, Any]]
) -> None:
	_assert_inputs_current(invocation, files)
	for file, record in zip(files, records, strict=True):
		if not record["changed"]:
			continue
		if time.monotonic() >= invocation.deadline:
			raise EditError("deadline")
		target = regular_path(invocation.root, file.path)
		if sha256(read_bounded(target, MAX_FILE_BYTES)) != file.sha256:
			raise EditError("stale_input")
		staged = workspace / file.path
		staged.chmod(target.stat().st_mode)
		os.replace(staged, target)
	invocation.assert_configuration_current()
	for record in records:
		if (
			sha256(read_bounded(regular_path(invocation.root, record["path"]), MAX_FILE_BYTES))
			!= record["sha256"]
		):
			raise EditError("stale_input")


def _assert_inputs_current(invocation: Invocation, files: list[SourceFile]) -> None:
	invocation.assert_configuration_current()
	invocation.assert_artifacts_current()
	for file in files:
		path = regular_path(invocation.root, file.path)
		if (
			sha256(read_bounded(path, MAX_FILE_BYTES)) != file.sha256
			or file.mode is not None
			and stat.S_IMODE(path.stat().st_mode) != file.mode
		):
			raise EditError("stale_input")
