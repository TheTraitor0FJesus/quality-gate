"""Bounded event metadata and root ownership for asynchronous edit readers."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from time import monotonic, sleep
from typing import Any

from python_types import BACKGROUND_SCHEMA as BACKGROUND_SCHEMA
from python_types.freshness import plain_path
from python_types.selections import gate_cache_root

from _shared.quality_events import MAX_EVENT_ID_CHARS
from _shared.quality_storage import StorageBusyError, atomic_json, directory_transaction

MAX_STATE_BYTES = 1024 * 1024
MAX_ROOTS = 32
MAX_AUDIENCES = 8
WORK_SECONDS = 30.0
CLEANUP_SECONDS = 3.0
DELIVERY_SECONDS = 10.0
MAX_DRAIN_SECONDS = WORK_SECONDS + CLEANUP_SECONDS + DELIVERY_SECONDS
POLL_SECONDS = 0.025
STATE_READ_RETRY_SECONDS = 0.5
BACKGROUND_CHECK_ORDER = ("ruff", "secrets", "types", "legacy_security")
BACKGROUND_CHECKS = frozenset(BACKGROUND_CHECK_ORDER)


class DeadlineError(TimeoutError):
	"""A state read could not finish before its caller's deadline."""


def key(value: str) -> str:
	return hashlib.sha256(value.encode("utf-8")).hexdigest()


def identity(value: object) -> str:
	if not isinstance(value, str) or not 0 < len(value) <= MAX_EVENT_ID_CHARS:
		raise ValueError("invalid_event")
	return key(value)


def cancellation_path(event_id: str) -> Path:
	if not isinstance(event_id, str) or str(uuid.UUID(event_id)) != event_id:
		raise ValueError("invalid_background_state")
	path = gate_cache_root({}).resolve() / "edit-types-events" / f"{event_id}.cancel"
	plain_path(path)
	return path


def owned_directory(state_dir: Path, root: Path) -> Path:
	"""Locate retained metadata even after its project directory has been removed."""
	root = root.resolve()
	state = state_dir.absolute()
	if state.is_relative_to(root):
		raise ValueError("unsafe_path")
	plain_path(state)
	directory = state / "background" / "roots" / key(str(root).casefold())
	plain_path(directory)
	return directory


def _is_windows_sharing_violation(error: PermissionError, path: Path) -> bool:
	return (
		os.name == "nt"
		and error.errno == errno.EACCES
		and getattr(error, "winerror", None) in (None, 32, 33)
		and error.filename is not None
		and Path(error.filename) == path
	)


def _retry_state_read(
	error: PermissionError, path: Path, retry_deadline: float, deadline: float | None
) -> None:
	if not _is_windows_sharing_violation(error, path):
		raise error
	now = monotonic()
	remaining = retry_deadline - now
	if remaining > 0:
		sleep(min(POLL_SECONDS, remaining))
		return
	if deadline is not None and now >= deadline:
		raise DeadlineError("deadline") from error
	raise error


def read(path: Path, *, deadline: float | None = None) -> dict[str, Any]:
	retry_deadline = monotonic() + STATE_READ_RETRY_SECONDS
	if deadline is not None:
		retry_deadline = min(retry_deadline, deadline)
	while True:
		try:
			plain_path(path)
			metadata = path.stat()
			if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
				raise ValueError("background_state_links")
			with path.open("rb") as stream:
				content = stream.read(MAX_STATE_BYTES + 1)
			if len(content) > MAX_STATE_BYTES:
				raise ValueError("limits")
			document = json.loads(content)
			if not isinstance(document, dict) or document.get("schema") != 1:
				raise ValueError("invalid_background_state")
			if path.name == "latest.json":
				validate_job(path, document)
			return document
		# The Windows CRT can omit winerror when it maps a sharing violation to EACCES.
		except PermissionError as error:
			_retry_state_read(error, path, retry_deadline, deadline)


def validate_job(path: Path, document: dict[str, Any]) -> None:
	"""Private metadata cannot grant ownership of arbitrary cancellation paths."""
	request = document["request"]
	event_id = request["event_id"]
	root = Path(request["root"])
	if not root.is_absolute() or str(root.resolve()) != str(root):
		raise ValueError("invalid_background_state")
	if owned_directory(path.parents[3], root) != path.parent:
		raise ValueError("invalid_background_state")
	expected = cancellation_path(event_id)
	if request["cancel_path"] != str(expected) or expected.is_relative_to(root):
		raise ValueError("invalid_background_state")
	checks = document.get("checks", ["types"])
	if (
		not isinstance(checks, list)
		or not checks
		or len(checks) > len(BACKGROUND_CHECKS)
		or any(not isinstance(name, str) or name not in BACKGROUND_CHECKS for name in checks)
		or len(set(checks)) != len(checks)
	):
		raise ValueError("invalid_background_state")
	validate_outcome(document)


def validate_outcome(document: dict[str, Any]) -> None:
	status, wire, report = document["status"], document["wire"], document["report"]
	if status not in {"pending", "completed", "retired"}:
		raise ValueError("invalid_background_state")
	if status == "pending":
		if wire is not None or report is not None:
			raise ValueError("invalid_background_state")
	elif wire is None:
		if not isinstance(report, dict) or report.get("status") != "unchecked":
			raise ValueError("invalid_background_state")
	elif status != "completed" or report is not None or not isinstance(wire, dict):
		raise ValueError("invalid_background_state")
	else:
		_validate_composite_wire(wire)


def _validate_composite_wire(wire: dict[str, Any]) -> None:
	results = wire.get("results") if wire.get("schema") == 1 else None
	if results is None:
		return
	if not isinstance(results, dict) or set(results) - {
		"ruff",
		"secrets",
		"builtin_secrets",
		"types",
		"legacy_security",
	}:
		raise ValueError("invalid_background_state")
	for check, result in results.items():
		if not isinstance(result, dict):
			raise ValueError("invalid_background_state")
		if check == "types":
			if (
				set(result) != {"wire", "report"}
				or (result["wire"] is not None and not isinstance(result["wire"], dict))
				or (result["report"] is not None and not isinstance(result["report"], dict))
			):
				raise ValueError("invalid_background_state")
		elif set(result) != {"report"} or not isinstance(result["report"], dict):
			raise ValueError("invalid_background_state")


def write(path: Path, document: dict[str, Any]) -> None:
	plain_path(path)
	if path.name == "latest.json":
		validate_job(path, document)
	if len(json.dumps(document, ensure_ascii=True).encode()) > MAX_STATE_BYTES:
		raise ValueError("limits")
	try:
		atomic_json(path, document)
	except PermissionError as error:
		# Windows readers can temporarily deny replacement of an otherwise owned file.
		raise StorageBusyError("state_busy") from error


def subscribe(state_dir: Path, audience: str, root: Path, *, deadline: float | None = None) -> None:
	directory = state_dir / "background" / "sessions"
	plain_path(directory)
	with directory_transaction(directory):
		path = directory / f"{audience}.json"
		document = read(path, deadline=deadline) if path.exists() else {"schema": 1, "roots": []}
		roots = document["roots"]
		if not isinstance(roots, list) or any(not isinstance(item, str) for item in roots):
			raise ValueError("invalid_background_state")
		if str(root) not in roots:
			roots.append(str(root))
		if len(roots) > MAX_ROOTS:
			raise ValueError("limits")
		write(path, document)


def roots_for(state_dir: Path, audience: str, *, deadline: float | None = None) -> list[Path]:
	path = state_dir / "background" / "sessions" / f"{audience}.json"
	if not path.exists():
		return []
	roots = read(path, deadline=deadline)["roots"]
	if not isinstance(roots, list) or len(roots) > MAX_ROOTS:
		raise ValueError("invalid_background_state")
	result = []
	for item in roots:
		if not isinstance(item, str) or not Path(item).is_absolute():
			raise ValueError("invalid_background_state")
		result.append(Path(item))
	return result


def signal_cancel(path: Path) -> None:
	plain_path(path)
	path.parent.mkdir(parents=True, exist_ok=True)
	if path.exists():
		if not path.is_file() or path.stat().st_nlink != 1:
			raise ValueError("unsafe_path")
	else:
		with path.open("xb"):
			pass


@contextmanager
def reader_lease(directory: Path, deadline: float) -> Iterator[bool]:
	"""Serialize analysis separately from short metadata transactions."""
	plain_path(directory)
	directory.mkdir(parents=True, exist_ok=True)
	lock = directory / ".reader.lock"
	plain_path(lock)
	with lock.open("a+b") as handle:
		metadata = os.fstat(handle.fileno())
		if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
			raise ValueError("background_state_links")
		if handle.tell() == 0:
			handle.write(b"0")
			handle.flush()
		acquired = False
		while monotonic() < deadline:
			try:
				handle.seek(0)
				if os.name == "nt":
					import msvcrt

					msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
				else:
					import fcntl

					fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
				acquired = True
				break
			except OSError:
				sleep(POLL_SECONDS)
		try:
			yield acquired
		finally:
			if acquired:
				handle.seek(0)
				if os.name == "nt":
					msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
				else:
					fcntl.flock(handle, fcntl.LOCK_UN)  # type: ignore[attr-defined]
