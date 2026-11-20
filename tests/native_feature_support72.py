"""Shared fixtures and helpers for feature-preserving test partitions."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO, Literal, cast

import pytest

import quality_gate.native_cache as native_cache_module
from quality_gate import operation as operation_module
from quality_gate import runner
from quality_gate.contracts import CheckResult, Status
from quality_gate.edit_protocol import EditError
from quality_gate.file_lock import open_locked_file
from quality_gate.launcher import PreparedEnvironment, prepare
from quality_gate.native_cache import (
	NativeToolCache,
	NativeToolCacheIdentity,
	NativeToolCacheSelection,
	native_tool_source_workspace,
)
from quality_gate.operation import GateOperation, OperationTimeoutError, observe_check

MEASUREMENT_WARM_RUNS = 5

MEASUREMENT_EXPECTED_TOOL_RUNS = (MEASUREMENT_WARM_RUNS + 2) * 2

MEASUREMENT_SOURCE_MTIME_NS = 1_700_000_000_000_000_000


@pytest.fixture(autouse=True)
def use_long_windows_staging_limit_for_test_sandbox(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	if os.name == "nt":
		monkeypatch.setattr(native_cache_module, "_WINDOWS_MAX_PATH", 32767)


def _identity(
	*,
	tool: Literal["ruff", "mypy"] = "ruff",
	runtime_fingerprint: str = "runtime-a",
	effective_options: tuple[str, ...] = ("check", "--config=ruff.toml"),
) -> NativeToolCacheIdentity:
	return NativeToolCacheIdentity(
		repository="C:/repositories/quality-gate",
		engine_version="4.2.0",
		runtime_fingerprint=runtime_fingerprint,
		tool=tool,
		tool_version="0.16.4" if tool == "ruff" else "2.3.1",
		tool_digest="a" * 64 if tool == "ruff" else "b" * 64,
		policy_release="v4.2.0",
		policy_digest="c" * 64,
		platform="win32-amd64",
		python_version="3.11",
		effective_options=effective_options,
		import_roots=("quality_gate",),
	)


class _NativeToolRunRecorder:
	"""Collect real subprocess duration and the cache selection passed to each tool."""

	def __init__(self, delegate: Callable[..., str]) -> None:
		self.delegate = delegate
		self.current_run = ""
		self.observed: list[dict[str, object]] = []
		self.outputs: list[str] = []
		self.workspace_roots: list[Path] = []

	def __call__(
		self,
		command: list[str],
		root: Path,
		environment: dict[str, str],
		*,
		timeout: float | None = None,
	) -> str:
		tool_name = (
			command[command.index("-m") + 1]
			if "-m" in command
			else Path(command[0]).stem.casefold()
		)
		cache_key = "RUFF_CACHE_DIR" if tool_name == "ruff" else "MYPY_CACHE_DIR"
		started = time.perf_counter()
		status = "passed"
		output = ""
		try:
			output = self.delegate(command, root, environment, timeout=timeout)
			return output
		except runner.QualityGateError:
			status = "failed"
			raise
		finally:
			self.outputs.append(output)
			self.workspace_roots.append(root)
			self.observed.append(
				{
					"run": self.current_run,
					"tool": tool_name,
					"namespace_id": Path(environment[cache_key]).name,
					"cache_dir": environment[cache_key],
					"duration_ms": round((time.perf_counter() - started) * 1000, 2),
					"status": status,
				}
			)


def _assert_persistent_mypy_run(
	recorder: _NativeToolRunRecorder,
	cache_root: Path,
	run_name: str,
) -> str:
	rows = [row for row in recorder.observed if row["run"] == run_name and row["tool"] == "mypy"]
	assert len(rows) == 1
	cache_dir = Path(str(rows[0]["cache_dir"]))
	assert cache_dir.resolve().is_relative_to(cache_root.resolve())
	assert rows[0]["namespace_id"] == cache_dir.name
	return str(rows[0]["namespace_id"])


def _open_file_without_shared_reads(path: Path) -> tuple[object, object]:
	"""Hold a Windows source handle that prevents Ruff from reopening the file."""
	import ctypes
	from ctypes import wintypes

	kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
	create_file = kernel32.CreateFileW
	create_file.argtypes = [
		wintypes.LPCWSTR,
		wintypes.DWORD,
		wintypes.DWORD,
		wintypes.LPVOID,
		wintypes.DWORD,
		wintypes.DWORD,
		wintypes.HANDLE,
	]
	create_file.restype = ctypes.c_void_p
	handle = create_file(
		str(path),
		0x80000000,
		0,
		None,
		3,
		0x80,
		None,
	)
	if handle == ctypes.c_void_p(-1).value or handle is None:
		raise getattr(ctypes, "WinError")(ctypes.get_last_error())
	return kernel32, handle


def _close_file_handle(kernel32: object, handle: object) -> None:
	import ctypes

	close_handle = getattr(kernel32, "CloseHandle")
	close_handle.argtypes = [ctypes.c_void_p]
	close_handle.restype = ctypes.c_int
	if not close_handle(handle):
		raise getattr(ctypes, "WinError")(ctypes.get_last_error())


def _native_tool_command_templates(
	prepared: PreparedEnvironment,
	python: Path,
) -> tuple[tuple[str, list[str]], ...]:
	return (
		(
			"python.component_1.ruff",
			[
				*runner._tool_command(prepared, python, "ruff"),
				"check",
				"--config",
				str(prepared.policy_root / "quality_gate" / "policy" / "ruff.toml"),
				"src",
			],
		),
		(
			"python.component_1.mypy",
			[
				*runner._tool_command(prepared, python, "mypy"),
				"--config-file",
				str(prepared.policy_root / "quality_gate" / "policy" / "mypy.ini"),
				"src",
			],
		),
	)


def _mypy_tool_command_templates(
	prepared: PreparedEnvironment,
	python: Path,
) -> tuple[tuple[str, list[str]], ...]:
	return tuple(
		command
		for command in _native_tool_command_templates(prepared, python)
		if command[0].endswith(".mypy")
	)


def _execute_measured_snapshot(
	tmp_path: Path,
	prepared: PreparedEnvironment,
	recorder: _NativeToolRunRecorder,
	commands: tuple[tuple[str, list[str]], ...],
	name: str,
	source: str,
	additional_sources: dict[str, str] | None = None,
	*,
	package_init: bool = True,
	mtime_overrides_ns: dict[str, int] | None = None,
) -> list[tuple[str, int, str]]:
	recorder.current_run = name
	snapshot_root = tmp_path / name
	source_root = snapshot_root / "src"
	source_root.mkdir(parents=True)
	sources = {"module.py": source, **(additional_sources or {})}
	if package_init:
		sources["__init__.py"] = ""
	for relative_path, content in sources.items():
		path = source_root / relative_path
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(content, encoding="utf-8")
		mtime_ns = (mtime_overrides_ns or {}).get(relative_path, MEASUREMENT_SOURCE_MTIME_NS)
		os.utime(path, ns=(mtime_ns, mtime_ns))
	component = runner.PythonComponent(source_root, None, None, True)
	executed, errors, _outputs = runner._execute_commands(
		snapshot_root,
		runner._safe_environment(str(tmp_path / f"private-{name}")),
		[(check_id, list(command), 30) for check_id, command in commands],
		30,
		time.monotonic() + 90,
		prepared=prepared,
		component=component,
		component_index=1,
	)
	assert executed == [check_id for check_id, _command in commands]
	return [
		(
			error.check_id,
			error.exit_code,
			_tool_finding_text(error),
		)
		for error in errors
	]


def _tool_finding_text(error: runner.QualityGateError) -> str:
	"""Return tool output without the invocation wrapper or temporary config path."""
	message = str(error)
	tool_output = message.partition("\n\n")[2]
	return tool_output or message


def _prepared_with_fixture_dependencies(
	prepared: PreparedEnvironment,
	dependencies: dict[str, str],
) -> PreparedEnvironment:
	"""Add content hashes for isolated fixture dependencies to the prepared runtime."""
	runtime_identity = prepared.runtime_identities[0]
	dependency_inputs = tuple(
		{
			"path": path.replace("\\", "/"),
			"sha256": hashlib.sha256(contents.encode("utf-8")).hexdigest(),
		}
		for path, contents in sorted(dependencies.items())
	)
	updated_identity = replace(
		runtime_identity,
		dependency_inputs=(*runtime_identity.dependency_inputs, *dependency_inputs),
	)
	return replace(
		prepared,
		runtime_identities=(updated_identity, *prepared.runtime_identities[1:]),
	)


def _collect_native_cache_measurements(
	tmp_path: Path,
	prepared: PreparedEnvironment,
	recorder: _NativeToolRunRecorder,
	commands: tuple[tuple[str, list[str]], ...],
) -> None:
	valid_source = "def add(left: int, right: int) -> int:\n\treturn left + right\n"
	assert (
		_execute_measured_snapshot(
			tmp_path, prepared, recorder, commands, "snapshot-cold", valid_source
		)
		== []
	)
	for index in range(MEASUREMENT_WARM_RUNS):
		assert (
			_execute_measured_snapshot(
				tmp_path,
				prepared,
				recorder,
				commands,
				f"snapshot-warm-{index + 1}",
				valid_source,
			)
			== []
		)
	invalid_source = "import os\n\ndef add(left: int, right: int) -> int:\n\treturn 'wrong type'\n"
	changed_source_errors = _execute_measured_snapshot(
		tmp_path, prepared, recorder, commands, "snapshot-source-changed", invalid_source
	)
	assert [error[0] for error in changed_source_errors] == [
		"python.component_1.ruff",
		"python.component_1.mypy",
	]


def _assert_native_cache_measurements(
	observed: list[dict[str, object]],
	cache_root: Path,
) -> dict[str, int]:
	assert len(observed) == MEASUREMENT_EXPECTED_TOOL_RUNS
	payload_counts: dict[str, int] = {}
	for tool_name in ("ruff", "mypy"):
		rows = [row for row in observed if row["tool"] == tool_name]
		assert len({row["namespace_id"] for row in rows}) == 1
		assert all(str(row["cache_dir"]).startswith(str(cache_root)) for row in rows)
		cache_path = Path(str(rows[0]["cache_dir"]))
		assert cache_path.is_dir()
		payload = [
			path
			for path in cache_path.rglob("*")
			if path.is_file() and path.name != ".quality-gate-native-cache.json"
		]
		assert payload, f"{tool_name} did not write native cache payload files"
		payload_counts[tool_name] = len(payload)
	return payload_counts


def _write_native_cache_measurement_report(
	report_path: Path,
	prepared: PreparedEnvironment,
	observed: list[dict[str, object]],
	payload_counts: dict[str, int],
) -> None:
	report_path.write_text(
		json.dumps(
			{
				"python": sys.version.split()[0],
				"platform": platform.platform(),
				"policy_release": prepared.release_manifest.version,
				"pinned_tools": {
					tool.name: tool.version
					for tool in prepared.release_manifest.tools
					if tool.name in {"ruff", "mypy"}
				},
				"snapshot_count": MEASUREMENT_WARM_RUNS + 2,
				"native_cache_payload_files": payload_counts,
				"tool_runs": observed,
			},
			indent=2,
		)
		+ "\n",
		encoding="utf-8",
	)


__all__ = [
	"Any",
	"BinaryIO",
	"Callable",
	"CheckResult",
	"EditError",
	"GateOperation",
	"Literal",
	"MEASUREMENT_EXPECTED_TOOL_RUNS",
	"MEASUREMENT_SOURCE_MTIME_NS",
	"MEASUREMENT_WARM_RUNS",
	"NativeToolCache",
	"NativeToolCacheIdentity",
	"NativeToolCacheSelection",
	"OperationTimeoutError",
	"Path",
	"PreparedEnvironment",
	"SimpleNamespace",
	"Status",
	"_NativeToolRunRecorder",
	"_assert_native_cache_measurements",
	"_assert_persistent_mypy_run",
	"_close_file_handle",
	"_collect_native_cache_measurements",
	"_execute_measured_snapshot",
	"_identity",
	"_mypy_tool_command_templates",
	"_native_tool_command_templates",
	"_open_file_without_shared_reads",
	"_prepared_with_fixture_dependencies",
	"_tool_finding_text",
	"_write_native_cache_measurement_report",
	"asdict",
	"cast",
	"hashlib",
	"json",
	"native_cache_module",
	"native_tool_source_workspace",
	"observe_check",
	"open_locked_file",
	"operation_module",
	"os",
	"platform",
	"prepare",
	"pytest",
	"replace",
	"runner",
	"shutil",
	"stat",
	"subprocess",
	"sys",
	"time",
	"use_long_windows_staging_limit_for_test_sandbox",
]
