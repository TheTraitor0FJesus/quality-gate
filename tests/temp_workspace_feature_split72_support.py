"""Fixtures and helpers for the split feature tests."""

from __future__ import annotations

import errno
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO, cast

import pytest

import quality_gate.distribution as distribution
import quality_gate.edit_cli as edit_cli
import quality_gate.temp_workspace as temp_workspace
from quality_gate.edit_protocol import EXIT_UNCHECKED, EditError
from quality_gate.operation import GateOperation
from quality_gate.release import _workspace_parents
from quality_gate.temp_workspace import (
	SYSTEM_TEMP_DIRECTORY,
	TemporaryWorkspaceError,
	cleanup_handed_off_workspace,
	temporary_workspace,
)

_SECOND_NESTED_LSTAT_CALL = 2

_SINGLE_LINK_COUNT = 1

_TRANSITIONAL_HARD_LINK_COUNT = 2

_INTERRUPTED_KEY_EXIT = 73

_INTERRUPTED_CLEANUP_EXIT = 74

_INTERRUPTED_AFTER_UNLINK_EXIT = 75


def _run_workspace_script(
	script: str, environment: dict[str, str], *arguments: str
) -> subprocess.CompletedProcess[bytes]:
	return subprocess.run(
		[sys.executable, "-c", script, *arguments],
		cwd=Path(__file__).resolve().parents[1],
		env=environment,
		capture_output=True,
		timeout=30,
		check=False,
	)


def _start_workspace_script(
	script: str, environment: dict[str, str], *arguments: str
) -> subprocess.Popen[bytes]:
	return subprocess.Popen(
		[sys.executable, "-c", script, *arguments],
		cwd=Path(__file__).resolve().parents[1],
		env=environment,
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
	)


def _wait_for_workspace_marker(
	marker: Path, process: subprocess.Popen[bytes], timeout_seconds: float = 30
) -> None:
	deadline = time.monotonic() + timeout_seconds
	while not marker.exists() and time.monotonic() < deadline:
		if process.poll() is not None:
			break
		time.sleep(0.01)
	assert marker.exists(), f"process did not reach marker {marker.name}"


def _collect_workspace_process(
	process: subprocess.Popen[bytes], timeout_seconds: float = 30
) -> tuple[bytes, bytes]:
	try:
		stdout, stderr = process.communicate(timeout=timeout_seconds)
	except subprocess.TimeoutExpired:
		process.kill()
		stdout, stderr = process.communicate(timeout=15)
	return stdout, stderr


def _assert_workspace_script_cleaned(result: subprocess.CompletedProcess[bytes]) -> Path:
	assert result.returncode == 0, result.stderr
	workspace = Path(result.stdout.decode().strip())
	assert not workspace.exists()
	return workspace


def _apply_publisher_race_final_action(
	final_action: str,
	key_path: Path,
	staging_path: Path,
	key_identity: os.stat_result,
	tmp_path: Path,
) -> tuple[Path | None, Path | None]:
	if final_action == "replaced":
		foreign_key = key_path.with_name("foreign-key")
		foreign_key.write_bytes(b"f" * temp_workspace._OWNERSHIP_KEY_BYTES)
		foreign_key.chmod(temp_workspace._OWNERSHIP_KEY_MODE)
		os.replace(foreign_key, key_path)
		assert not temp_workspace._same_file_identity(key_identity, key_path.lstat())
		return foreign_key, None
	if final_action == "missing":
		key_path.unlink()
		return None, None
	if final_action == "staging-replaced":
		foreign_target = tmp_path / "missing-replacement-target"
		staging_path.symlink_to(foreign_target)
		return None, foreign_target
	return None, None


def _publisher_race_script() -> str:
	return "\n".join(
		(
			"import errno, os, sys, time",
			"from pathlib import Path",
			"import quality_gate.temp_workspace as workspace",
			"root, coordination = map(Path, sys.argv[1:3])",
			"pause_after = int(sys.argv[3])",
			"staging_statted = coordination / 'publisher-staging-statted'",
			"resume_publisher = coordination / 'resume-publisher'",
			"def unsupported(*args, **kwargs):",
			"    raise OSError(errno.ENOSYS, 'no no-replace rename')",
			"workspace._rename_without_replacement = unsupported",
			"original_stat = os.stat",
			"staging_stat_count = [0]",
			"def pause_after_staging_stat(path, *args, **kwargs):",
			"    metadata = original_stat(path, *args, **kwargs)",
			"    is_staging_stat = (",
			"        isinstance(path, str)",
			"        and path.startswith('.quality-gate-pending-')",
			"        and kwargs.get('dir_fd') is not None",
			"        and kwargs.get('follow_symlinks') is False",
			"    )",
			"    if is_staging_stat:",
			"        staging_stat_count[0] += 1",
			"    if is_staging_stat and staging_stat_count[0] == pause_after:",
			"        staging_statted.touch()",
			"        deadline = time.monotonic() + 30",
			"        while not resume_publisher.exists() and time.monotonic() < deadline:",
			"            time.sleep(0.01)",
			"        if not resume_publisher.exists():",
			"            os._exit(88)",
			"    return metadata",
			"workspace.os.stat = pause_after_staging_stat",
			"from quality_gate.temp_workspace import temporary_workspace",
			"with temporary_workspace(root) as work:",
			"    (work / 'publisher').write_bytes(b'published')",
		)
	)


def _recovery_reader_script() -> str:
	return "\n".join(
		(
			"import sys",
			"from pathlib import Path",
			"from quality_gate.temp_workspace import temporary_workspace",
			"with temporary_workspace(Path(sys.argv[1])) as work:",
			"    (work / 'reader').write_bytes(b'recovered')",
			"    print(work, flush=True)",
		)
	)


def _recover_while_publisher_paused(
	staging_statted: Path,
	publisher: subprocess.Popen[bytes],
	state: Path,
	environment: dict[str, str],
	repository: Path,
	final_action: str,
	tmp_path: Path,
) -> tuple[Path, os.stat_result, bytes, Path, Path | None, Path | None]:
	_wait_for_workspace_marker(staging_statted, publisher, timeout_seconds=25)
	key_path = state / temp_workspace._OWNERSHIP_KEY_NAME
	key_identity = key_path.lstat()
	staging_paths = list(state.glob(".quality-gate-pending-*"))
	assert len(staging_paths) == 1
	staging_path = staging_paths[0]
	assert temp_workspace._same_file_identity(key_identity, staging_path.lstat())
	assert key_identity.st_nlink == _TRANSITIONAL_HARD_LINK_COUNT
	key_bytes = key_path.read_bytes()
	reader = _run_workspace_script(_recovery_reader_script(), environment, str(repository))
	_assert_workspace_script_cleaned(reader)
	assert not staging_path.exists()
	assert temp_workspace._same_file_identity(key_identity, key_path.lstat())
	assert key_path.read_bytes() == key_bytes
	assert key_path.lstat().st_nlink == _SINGLE_LINK_COUNT
	foreign_key, foreign_target = _apply_publisher_race_final_action(
		final_action, key_path, staging_path, key_identity, tmp_path
	)
	return key_path, key_identity, key_bytes, staging_path, foreign_key, foreign_target


def _assert_publisher_race_outcome(
	final_action: str,
	publisher: subprocess.Popen[bytes],
	publisher_stdout: bytes,
	publisher_stderr: bytes,
	state: Path,
	environment: dict[str, str],
	repository: Path,
	key_path: Path,
	key_identity: os.stat_result,
	key_bytes: bytes,
	staging_path: Path,
	foreign_key: Path | None,
	foreign_target: Path | None,
) -> None:
	if final_action in {"intact", "staging-replaced"}:
		assert publisher.returncode == 0, publisher_stderr
		assert temp_workspace._same_file_identity(key_identity, key_path.lstat())
		assert key_path.read_bytes() == key_bytes
		assert key_path.lstat().st_nlink == _SINGLE_LINK_COUNT
		if final_action == "staging-replaced":
			assert foreign_target is not None
			assert staging_path.is_symlink()
			assert os.readlink(staging_path) == str(foreign_target)
			assert not foreign_target.exists()
		return
	assert publisher.returncode != 0, publisher_stdout
	assert not list(state.glob(".quality-gate-pending-*"))
	if final_action == "replaced":
		assert key_path.read_bytes() == b"f" * temp_workspace._OWNERSHIP_KEY_BYTES
		assert foreign_key is not None and not foreign_key.exists()
		return
	assert not key_path.exists()
	fresh = _run_workspace_script(_recovery_reader_script(), environment, str(repository))
	_assert_workspace_script_cleaned(fresh)


__all__ = [
	"BinaryIO",
	"Callable",
	"EXIT_UNCHECKED",
	"EditError",
	"GateOperation",
	"Path",
	"SYSTEM_TEMP_DIRECTORY",
	"SimpleNamespace",
	"TemporaryWorkspaceError",
	"_INTERRUPTED_AFTER_UNLINK_EXIT",
	"_INTERRUPTED_CLEANUP_EXIT",
	"_INTERRUPTED_KEY_EXIT",
	"_SECOND_NESTED_LSTAT_CALL",
	"_SINGLE_LINK_COUNT",
	"_TRANSITIONAL_HARD_LINK_COUNT",
	"_apply_publisher_race_final_action",
	"_assert_publisher_race_outcome",
	"_assert_workspace_script_cleaned",
	"_collect_workspace_process",
	"_publisher_race_script",
	"_recover_while_publisher_paused",
	"_recovery_reader_script",
	"_run_workspace_script",
	"_start_workspace_script",
	"_wait_for_workspace_marker",
	"_workspace_parents",
	"cast",
	"cleanup_handed_off_workspace",
	"distribution",
	"edit_cli",
	"errno",
	"io",
	"json",
	"os",
	"pytest",
	"stat",
	"subprocess",
	"sys",
	"temp_workspace",
	"tempfile",
	"temporary_workspace",
	"threading",
	"time",
]
