"""Validate Ruff output and publish fresh working-tree edits."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .edit_inputs import SourceFile, read_bounded, regular_path
from .edit_policy import sha256
from .edit_protocol import MAX_FILE_BYTES, EditError

if TYPE_CHECKING:
	from .edit_check import Invocation


def format_report(code: int, output: bytes, workspace: Path, included: set[str]) -> set[str]:
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


def publish(
	invocation: Invocation, files: list[SourceFile], workspace: Path, records: list[dict[str, Any]]
) -> None:
	assert_inputs_current(invocation, files)
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


def assert_inputs_current(invocation: Invocation, files: list[SourceFile]) -> None:
	invocation.assert_configuration_current()
	invocation.assert_artifacts_current()
	for file in files:
		if (
			sha256(read_bounded(regular_path(invocation.root, file.path), MAX_FILE_BYTES))
			!= file.sha256
		):
			raise EditError("stale_input")


def environment() -> dict[str, str]:
	"""No project Python paths, Ruff environment overrides, credentials or caches."""
	allowed = {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "LANG", "LC_ALL", "TEMP", "TMP"}
	return {key: value for key, value in os.environ.items() if key.upper() in allowed} | {
		"PYTHONDONTWRITEBYTECODE": "1",
		"PYTHONIOENCODING": "utf-8",
		"PYTHONUTF8": "1",
	}
