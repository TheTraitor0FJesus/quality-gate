"""Bounded helpers shared by the Gate operation supervisor."""

from __future__ import annotations

import json
import math
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from .contracts import CheckResult, Status

if TYPE_CHECKING:
	from .native_cache import NativeToolCacheSelection


@dataclass(slots=True)
class _StageObservation:
	process_required: bool
	process_started: bool
	check: bool = False
	cache_selection: NativeToolCacheSelection | None = None
	command_ms: int | None = None


_Result = TypeVar("_Result", bound=CheckResult)

_CLEANUP_BODY = """
import json, sys
limit = int(sys.argv[1])
sys.path.insert(0, sys.argv[2])
payload = sys.stdin.buffer.read(limit + 1)
if len(payload) > limit:
    raise ValueError('cleanup inventory exceeded its limit')
from pathlib import Path
from quality_gate.temp_workspace import cleanup_handed_off_workspace
from quality_gate.native_cache import cleanup_native_cache_source_workspace
inventory = json.loads(payload)
failed = False
for temp_root, run_directory in inventory['workspaces']:
    try:
        cleanup_handed_off_workspace(Path(temp_root), Path(run_directory))
    except Exception:
        failed = True
for source_workspace in inventory['native_cache']:
    try:
        cleanup_native_cache_source_workspace(source_workspace)
    except Exception:
        failed = True
if failed:
    raise OSError('one or more owned cleanup roots could not be removed')
"""


def cleanup_inventory(
	workspaces: list[tuple[Path, Path]],
	native_cache: dict[Path, bytes],
) -> bytes:
	from .operation import MAX_CLEANUP_INVENTORY_BYTES

	payload = json.dumps(
		{
			"workspaces": [[str(temp), str(root)] for temp, root in workspaces],
			"native_cache": [
				json.loads(native_cache[path]) for path in sorted(native_cache, key=str)
			],
		}
	).encode("utf-8")
	if len(payload) > MAX_CLEANUP_INVENTORY_BYTES:
		raise ValueError("owned cleanup inventory exceeded its bound")
	return payload


def _is_lowercase_hex(value: str, length: int) -> bool:
	from .operation import _LOWERCASE_HEX

	return len(value) == length and all(character in _LOWERCASE_HEX for character in value)


def _is_system_workspace_temp(temp_root: Path, fallback_root: Path) -> bool:
	from . import operation as _operation

	try:
		parts = temp_root.relative_to(fallback_root).parts
	except ValueError:
		return False
	if len(parts) != _operation._SYSTEM_WORKSPACE_ROOT_PART_COUNT or parts[2] != "temp":
		return False
	owner_hash = parts[0].removeprefix(_operation._SYSTEM_WORKSPACE_ROOT_PREFIX)
	return (
		parts[0].startswith(_operation._SYSTEM_WORKSPACE_ROOT_PREFIX)
		and _operation._is_lowercase_hex(owner_hash, _operation._SYSTEM_WORKSPACE_OWNER_HASH_LENGTH)
		and _operation._is_lowercase_hex(
			parts[1], _operation._SYSTEM_WORKSPACE_REPOSITORY_HASH_LENGTH
		)
	)


def remaining_timeout(limit: float | None = None) -> float | None:
	from .operation import current_operation

	operation = current_operation()
	return limit if operation is None else operation.remaining(limit)


def observe_check(
	check_id: str,
	scope: str,
	check: Callable[[], _Result],
	*,
	process_required: bool = False,
) -> _Result:
	"""Time one stable check id and retain its result before later work can fail."""
	from .operation import OperationTimeoutError, current_operation

	operation = current_operation()
	if operation is None:
		return check()
	completed_result: _Result | None = None
	try:
		with operation.stage(
			check_id,
			scope,
			check=True,
			process_required=process_required,
		):
			result = check()
			completed_result = result
	except OperationTimeoutError as error:
		operation.record_result(
			completed_result
			if completed_result is not None
			else CheckResult(
				check_id,
				Status.UNCHECKED,
				f"operation deadline expired during {check_id}",
				recovery_action=(
					"inspect the current and last completed stages; restore the budget and retry"
				),
			)
		)
		raise error
	return operation.record_result(result)


def run_probe(
	command: list[str],
	*,
	cwd: Path | None = None,
	environment: dict[str, str] | None = None,
	timeout: float | None = None,
	payload: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
	"""Run a bounded readiness or Git probe on the active operation deadline."""
	from . import operation as _operation

	operation = _operation.current_operation()
	if operation is None:
		return subprocess.run(
			command,
			cwd=cwd,
			env=environment,
			input=payload,
			capture_output=True,
			check=False,
			timeout=timeout,
		)
	if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
		raise ValueError("readiness probe timeout must be positive and finite")
	try:
		returncode, stdout, stderr = operation.run_process(
			command,
			root=cwd or operation.root,
			environment=dict(os.environ) if environment is None else environment,
			timeout_seconds=timeout,
			payload=payload,
			limit=_operation.MAX_PROBE_OUTPUT_BYTES,
			truncate=False,
			check_execution=False,
		)
	except _operation.EditError as error:
		if str(error) == "deadline":
			limit = timeout if timeout is not None else operation.command_timeout_seconds
			raise subprocess.TimeoutExpired(command, limit) from error
		raise OSError("readiness probe could not complete") from error
	return subprocess.CompletedProcess(command, returncode, stdout, stderr)
