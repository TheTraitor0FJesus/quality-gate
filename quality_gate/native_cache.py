"""Bounded persistent namespaces for native Ruff and mypy caches."""

from __future__ import annotations

import hashlib as hashlib
import json as json
import math
import os as os
import shutil as shutil
import stat as stat
import sys as sys
import time as time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import asdict as asdict
from dataclasses import dataclass, field
from dataclasses import fields as fields
from dataclasses import replace as replace
from pathlib import Path as Path
from typing import Any as Any
from typing import BinaryIO as BinaryIO
from typing import Literal as Literal
from typing import Protocol
from typing import cast as cast

from . import native_cache_methods1 as _methods1
from . import native_cache_methods2 as _methods2
from . import native_cache_methods3 as _methods3
from .edit_protocol import EditError as EditError
from .evidence_identity import EngineIdentity
from .file_lock import FileLockUnavailableError as FileLockUnavailableError
from .file_lock import locked_file as locked_file
from .file_lock import open_locked_file as open_locked_file
from .file_lock import unlock_file as unlock_file
from .native_cache_support1 import (
	_absolute_lexical_path as _absolute_lexical_path,
)
from .native_cache_support1 import (
	_cache_directory_identity as _cache_directory_identity,
)
from .native_cache_support1 import (
	_copy_candidate as _copy_candidate,
)
from .native_cache_support1 import (
	_copy_directory_contents as _copy_directory_contents,
)
from .native_cache_support1 import (
	_copy_source_file as _copy_source_file,
)
from .native_cache_support1 import (
	_create_eviction_owner as _create_eviction_owner,
)
from .native_cache_support1 import (
	_directory_identity as _directory_identity,
)
from .native_cache_support1 import (
	_eviction_owner_path as _eviction_owner_path,
)
from .native_cache_support1 import (
	_is_link as _is_link,
)
from .native_cache_support1 import (
	_json_identity as _json_identity,
)
from .native_cache_support1 import (
	_make_read_only as _make_read_only,
)
from .native_cache_support1 import (
	_make_writable as _make_writable,
)
from .native_cache_support1 import (
	_namespace_marker_identity as _namespace_marker_identity,
)
from .native_cache_support1 import (
	_read_bounded_json as _read_bounded_json,
)
from .native_cache_support1 import (
	_read_eviction_owner as _read_eviction_owner,
)
from .native_cache_support1 import (
	_remove_eviction_owner as _remove_eviction_owner,
)
from .native_cache_support1 import (
	_remove_eviction_payload as _remove_eviction_payload,
)
from .native_cache_support1 import (
	_require_directory_identity as _require_directory_identity,
)
from .native_cache_support1 import (
	_source_owner_marker_is_valid as _source_owner_marker_is_valid,
)
from .native_cache_support1 import (
	_stable_source_mtime_ns as _stable_source_mtime_ns,
)
from .native_cache_support2 import (
	_create_source_workspace as _create_source_workspace,
)
from .native_cache_support2 import (
	_identity_from_payload as _identity_from_payload,
)
from .native_cache_support2 import (
	_identity_payload as _identity_payload,
)
from .native_cache_support2 import (
	_optional_pair_from_payload as _optional_pair_from_payload,
)
from .native_cache_support2 import (
	_pair_from_payload as _pair_from_payload,
)
from .native_cache_support2 import (
	_prepare_source_parent as _prepare_source_parent,
)
from .native_cache_support2 import (
	_prepare_source_workspace as _prepare_source_workspace,
)
from .native_cache_support2 import (
	_remove_new_empty_source_parent as _remove_new_empty_source_parent,
)
from .native_cache_support2 import (
	_remove_owned_source_workspace as _remove_owned_source_workspace,
)
from .native_cache_support2 import (
	_rollback_source_setup as _rollback_source_setup,
)
from .native_cache_support2 import (
	_selected_source_namespace_identity as _selected_source_namespace_identity,
)
from .native_cache_support2 import (
	_selection_payload as _selection_payload,
)
from .native_cache_support2 import (
	_validate_source_workspace_ownership as _validate_source_workspace_ownership,
)
from .native_cache_support3 import (
	_cache_identity_from_request as _cache_identity_from_request,
)
from .native_cache_support3 import (
	_capture_source_cleanup_payload as _capture_source_cleanup_payload,
)
from .native_cache_support3 import (
	_prepare_namespace_request as _prepare_namespace_request,
)
from .native_cache_support3 import (
	_prepare_source_workspace_with_operation as _prepare_source_workspace_with_operation,
)
from .native_cache_support3 import (
	_prune_native_cache_request as _prune_native_cache_request,
)
from .native_cache_support3 import (
	_register_partial_source_cleanup as _register_partial_source_cleanup,
)
from .native_cache_support3 import (
	_run_native_cache_action as _run_native_cache_action,
)
from .native_cache_support3 import (
	_selection_from_payload as _selection_from_payload,
)
from .native_cache_support3 import (
	_source_cleanup_payload as _source_cleanup_payload,
)
from .native_cache_support3 import (
	_source_paths as _source_paths,
)
from .path_safety import is_link_or_reparse as is_link_or_reparse

_CACHE_DIRECTORY = "native-tools"
_LOCK_DIRECTORY = ".locks"
# ponytail: this root-level lease serializes namespace create/prune across all tools and
# repositories. If measured wait time becomes material, shard it by tool and repository while
# keeping each shard's per-repository capacity accounting intact.
_MAINTENANCE_LOCK = ".maintenance.lock"
_MARKER = ".quality-gate-native-cache.json"
_EVICTION_OWNER_SUFFIX = ".owner.json"
_EVICTION_OWNER_KIND = "native-cache-eviction"
_SOURCE_DIRECTORY = "_s"
_SOURCE_ROOT = "c"
_SOURCE_OWNER_DIRECTORY = "o"
_SOURCE_PARENT_OWNER_DIRECTORY = "p"
_SOURCE_DIRECTORY_MODE = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
_SOURCE_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR
_SOURCE_READ_ONLY_DIRECTORY_MODE = stat.S_IRUSR | stat.S_IXUSR
_SOURCE_READ_ONLY_FILE_MODE = stat.S_IRUSR
_SOURCE_DIRECTORY_MTIME_NS = 1_700_000_000_000_000_000
_OWNER = "quality-gate"
_SCHEMA = 1
_IDENTITY_SCHEMA = 1
_MAX_MARKER_BYTES = 4096
_WINDOWS_MAX_PATH = 260
_FILE_IDENTITY_COMPONENTS = 2
_SHA256_HEX_LENGTH = 64
_EVICTION_TOKEN_HEX_LENGTH = 32
_LEASE_TIMEOUT_SECONDS = 0.25
_MAINTENANCE_TIMEOUT_SECONDS = 0.1
_RETENTION_SECONDS = 30 * 24 * 60 * 60
_MAX_NAMESPACES_PER_REPOSITORY_TOOL = 8
_MAX_EVICTIONS_PER_LEASE = 16
_MAX_SCANNED_NAMESPACES = 64
_MAX_NATIVE_CACHE_ACTION_BYTES = 64 * 1024
_NATIVE_CACHE_BODY = """
import json, sys
limit = int(sys.argv[1])
sys.path.insert(0, sys.argv[2])
payload = sys.stdin.buffer.read(limit + 1)
if len(payload) > limit:
    raise ValueError('native cache action input exceeded its limit')
from quality_gate.native_cache import execute_native_cache_request
result = execute_native_cache_request(json.loads(payload))
sys.stdout.write(json.dumps(result, separators=(',', ':')))
"""


@dataclass(frozen=True, slots=True)
class _SourceWorkspaceIdentity:
	"""Filesystem identities that authorize source staging cleanup for one lease."""

	namespace: tuple[int, int]
	namespace_marker: tuple[int, int]
	stage_parent: tuple[int, int]
	parent_owner_marker: tuple[int, int] | None
	owner_marker: tuple[int, int] | None
	stable_root: tuple[int, int] | None


@dataclass(slots=True)
class _SourceWorkspaceSetup:
	"""Identity and creation state used to roll back an incomplete staging setup."""

	namespace: tuple[int, int]
	stage_parent: tuple[int, int] | None = None
	parent_owner: tuple[int, int] | None = None
	stage_parent_created: bool = False
	owner_marker_created: bool = False


class NativeToolCacheError(OSError):
	"""A native tool cache cannot safely use persistent storage for this invocation."""

	def __init__(self, reason: str) -> None:
		super().__init__(reason)
		self.reason = reason


@dataclass(frozen=True, slots=True)
class NativeToolCacheIdentity:
	"""Inputs that make a tool's on-disk cache safe to share between runs."""

	repository: str
	engine: EngineIdentity
	runtime_fingerprint: str
	tool: Literal["ruff", "mypy"]
	tool_version: str
	tool_digest: str
	policy_release: str
	policy_digest: str
	platform: str
	python_version: str
	effective_options: tuple[str, ...]
	import_roots: tuple[str, ...]

	@property
	def namespace_id(self) -> str:
		if self.tool not in {"ruff", "mypy"}:
			raise ValueError("native tool must be ruff or mypy")
		canonical = json.dumps(
			{"identity_schema": _IDENTITY_SCHEMA, "identity": asdict(self)},
			sort_keys=True,
			separators=(",", ":"),
		)
		return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

	@property
	def repository_key(self) -> str:
		repository = self.repository.replace("\\", "/")
		if os.name == "nt":
			repository = os.path.normcase(repository).casefold()
		else:
			repository = os.path.normpath(repository)
		return hashlib.sha256(repository.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class NativeToolCacheSelection:
	"""Cache selection evidence adjacent to the native tool process invocation."""

	namespace_id: str
	path: Path
	storage: Literal["persistent", "private"]
	fallback_reason: str | None
	selection_duration_ms: float
	_tool: Literal["ruff", "mypy"] | None = field(default=None, repr=False, compare=False)
	_path_identity: tuple[int, int] | None = field(default=None, repr=False, compare=False)
	_marker_identity: tuple[int, int] | None = field(default=None, repr=False, compare=False)


class NativeToolCacheOperation(Protocol):
	"""Operation-owned deadlines, process observations and native cache cleanup."""

	root: Path
	command_timeout_seconds: int

	def remaining(self, limit: float | None = None) -> float: ...

	def stage(
		self, name: str, scope: str, *, process_required: bool = False
	) -> AbstractContextManager[None]: ...

	def record_native_cache_selection(self, selection: NativeToolCacheSelection) -> None: ...

	def run_process(
		self,
		command: list[str],
		*,
		root: Path,
		environment: dict[str, str],
		timeout_seconds: float | None = None,
		payload: bytes | None = None,
		limit: int = 0,
		check_execution: bool = True,
	) -> tuple[int, bytes, bytes]: ...

	def register_native_cache_cleanup(self, payload: bytes) -> None: ...

	def defer_release(self, release: Callable[[], None]) -> None: ...


def prepare_native_source_workspace_request(payload: object) -> dict[str, object]:
	"""Create one validated stable source tree in a supervised operation child."""
	if (
		not isinstance(payload, dict)
		or set(payload)
		!= {
			"schema",
			"selection",
			"snapshot_root",
		}
		or payload["schema"] != 1
	):
		raise NativeToolCacheError("unsafe-source-path")
	selection = _selection_from_payload(payload["selection"])
	snapshot_root = payload["snapshot_root"]
	if not isinstance(snapshot_root, str) or not Path(snapshot_root).is_absolute():
		raise NativeToolCacheError("unsafe-source-path")
	stage_parent, stable_root, owner_marker, parent_owner_marker = _source_paths(selection)
	try:
		identity = _prepare_source_workspace(
			selection,
			Path(snapshot_root),
			stage_parent,
			stable_root,
			owner_marker,
			parent_owner_marker,
		)
	except NativeToolCacheError as error:
		return {"status": "fallback", "reason": error.reason}
	return {"status": "ready", "expected": _identity_payload(identity)}


def cleanup_native_cache_source_workspace(payload: object) -> None:
	"""Remove only a native source tree still matching its registered ownership."""
	if (
		not isinstance(payload, dict)
		or set(payload)
		!= {
			"schema",
			"selection",
			"expected",
		}
		or payload["schema"] != 1
	):
		raise NativeToolCacheError("unsafe-source-path")
	selection = _selection_from_payload(payload["selection"])
	stage_parent, stable_root, owner_marker, parent_owner_marker = _source_paths(selection)
	if not stage_parent.exists() and not stage_parent.is_symlink():
		return
	expected_value = payload["expected"]
	if expected_value is None:
		identity = _validate_source_workspace_ownership(
			selection, stage_parent, stable_root, owner_marker, parent_owner_marker
		)
	else:
		identity = _identity_from_payload(expected_value)
	if identity.owner_marker is None and identity.stable_root is None:
		_remove_new_empty_source_parent(
			selection,
			stage_parent,
			identity.stage_parent,
			parent_owner_marker,
			identity.parent_owner_marker,
		)
		if stage_parent.exists() or stage_parent.is_symlink():
			raise NativeToolCacheError("source-cleanup-unavailable")
	else:
		_remove_owned_source_workspace(
			selection,
			stage_parent,
			stable_root,
			owner_marker,
			parent_owner_marker,
			expected=identity,
		)
	if (
		stable_root.exists()
		or stable_root.is_symlink()
		or owner_marker.exists()
		or owner_marker.is_symlink()
	):
		raise NativeToolCacheError("source-cleanup-unavailable")


@contextmanager
def _operation_native_source_workspace(
	selection: NativeToolCacheSelection,
	snapshot_root: Path,
	private_cache_path: Path,
	operation: NativeToolCacheOperation,
) -> Iterator[tuple[NativeToolCacheSelection, Path]]:
	expected, fallback_reason = _prepare_source_workspace_with_operation(
		selection, snapshot_root, operation
	)
	if expected is None:
		yield (
			replace(
				selection,
				path=private_cache_path,
				storage="private",
				fallback_reason=fallback_reason or "source-staging-unavailable",
			),
			snapshot_root,
		)
		return
	try:
		yield selection, selection.path / _SOURCE_DIRECTORY / _SOURCE_ROOT
	except BaseException:
		_register_partial_source_cleanup(selection, operation)
		raise


@contextmanager
def native_tool_source_workspace(
	selection: NativeToolCacheSelection,
	snapshot_root: Path,
	private_cache_path: Path,
	*,
	operation: NativeToolCacheOperation | None = None,
) -> Iterator[tuple[NativeToolCacheSelection, Path]]:
	"""Run a persistent native cache against a stable, disposable source root."""
	if selection.storage == "private":
		yield selection, snapshot_root
		return
	if operation is not None:
		with _operation_native_source_workspace(
			selection, snapshot_root, private_cache_path, operation
		) as workspace:
			yield workspace
		return

	stage_parent = selection.path / _SOURCE_DIRECTORY
	stable_root = stage_parent / _SOURCE_ROOT
	owner_marker = stage_parent / _SOURCE_OWNER_DIRECTORY
	parent_owner_marker = stage_parent / _SOURCE_PARENT_OWNER_DIRECTORY
	try:
		ownership = _prepare_source_workspace(
			selection,
			snapshot_root,
			stage_parent,
			stable_root,
			owner_marker,
			parent_owner_marker,
		)
	except NativeToolCacheError as error:
		fallback = replace(
			selection,
			path=private_cache_path,
			storage="private",
			fallback_reason=error.reason,
		)
		yield fallback, snapshot_root
		return

	body_error: BaseException | None = None
	try:
		yield selection, stable_root
	except BaseException as error:
		body_error = error
		raise
	finally:
		try:
			_remove_owned_source_workspace(
				selection,
				stage_parent,
				stable_root,
				owner_marker,
				parent_owner_marker,
				expected=ownership,
			)
		except NativeToolCacheError as error:
			if body_error is None and error.reason != "source-cleanup-unavailable":
				raise
		except OSError as error:
			if body_error is None:
				raise NativeToolCacheError("source-cleanup-unavailable") from error


class NativeToolCache:
	"""Select, lock, and bound compatible Ruff and mypy native cache directories."""

	_acquire = _methods3._acquire
	_cache_root = _methods1._cache_root
	_ensure_directory = _methods1._ensure_directory
	_entries = _methods1._entries
	_eviction_namespace_id = staticmethod(_methods1._eviction_namespace_id)
	_last_used_ns = _methods2._last_used_ns
	_lock_path = _methods1._lock_path
	_marker_data = _methods1._marker_data
	_new_eviction_path = staticmethod(_methods1._new_eviction_path)
	_open_lease_lock = _methods3._open_lease_lock
	_prepare_namespace = _methods2._prepare_namespace
	_prepare_namespace_for_lease = _methods2._prepare_namespace_for_lease
	_prune = _methods2._prune
	_prune_for_lease = _methods2._prune_for_lease
	_quarantine_namespace = _methods1._quarantine_namespace
	_read_marker = _methods1._read_marker
	_recover_empty_eviction = _methods2._recover_empty_eviction
	_recover_eviction_path = _methods2._recover_eviction_path
	_recover_eviction_under_lock = _methods2._recover_eviction_under_lock
	_recover_evictions = _methods2._recover_evictions
	_recover_orphaned_eviction_owners = _methods2._recover_orphaned_eviction_owners
	_remove_evicted_namespace = _methods1._remove_evicted_namespace
	_remove_incomplete_marker_temporary = staticmethod(
		_methods1._remove_incomplete_marker_temporary
	)
	_repository_tool_root = _methods1._repository_tool_root
	_restore_eviction_path = staticmethod(_methods2._restore_eviction_path)
	_restore_owned_eviction = _methods1._restore_owned_eviction
	_try_evict = _methods1._try_evict
	_write_marker = _methods1._write_marker
	namespace_path = _methods1.namespace_path

	def __init__(
		self,
		cache_root: Path,
		*,
		lease_timeout_seconds: float = _LEASE_TIMEOUT_SECONDS,
		maintenance_timeout_seconds: float = _MAINTENANCE_TIMEOUT_SECONDS,
	) -> None:
		if not math.isfinite(lease_timeout_seconds) or lease_timeout_seconds < 0:
			raise ValueError("lease timeout must be a finite nonnegative number")
		if not math.isfinite(maintenance_timeout_seconds) or maintenance_timeout_seconds < 0:
			raise ValueError("maintenance timeout must be a finite nonnegative number")
		self.root = _absolute_lexical_path(cache_root)
		self.lease_timeout_seconds = lease_timeout_seconds
		self.maintenance_timeout_seconds = maintenance_timeout_seconds

	@contextmanager
	def lease(
		self,
		identity: NativeToolCacheIdentity,
		private_path: Path,
		*,
		operation: NativeToolCacheOperation | None = None,
	) -> Iterator[NativeToolCacheSelection]:
		"""Hold a compatible cache lease across one real native tool invocation."""
		started = time.monotonic()
		handle: BinaryIO | None = None
		lookup = (
			nullcontext()
			if operation is None
			else operation.stage(
				f"native-cache.{identity.tool}.lookup", f"native {identity.tool} cache selection"
			)
		)
		try:
			with lookup:
				try:
					path, handle, path_identity, marker_identity = self._acquire(
						identity, operation=operation
					)
				except OSError as error:
					selection = NativeToolCacheSelection(
						identity.namespace_id,
						private_path,
						"private",
						error.reason
						if isinstance(error, NativeToolCacheError)
						else "storage-unavailable",
						(time.monotonic() - started) * 1000,
					)
				else:
					selection = NativeToolCacheSelection(
						identity.namespace_id,
						path,
						"persistent",
						None,
						(time.monotonic() - started) * 1000,
						_tool=identity.tool,
						_path_identity=path_identity,
						_marker_identity=marker_identity,
					)
				if operation is not None:
					operation.record_native_cache_selection(selection)
			yield selection
		finally:
			if handle is not None:
				if operation is None:
					try:
						unlock_file(handle)
					except OSError:
						pass
				else:
					lease_handle = handle
					operation.defer_release(lambda: unlock_file(lease_handle))


def execute_native_cache_request(payload: object) -> dict[str, object]:
	"""Execute one bounded cache action under the caller-held maintenance/namespace lease."""
	if not isinstance(payload, dict) or set(payload) != {"action", "request"}:
		raise NativeToolCacheError("unsafe-cache-action")
	if payload["action"] == "prepare-source":
		return prepare_native_source_workspace_request(payload["request"])
	if payload["action"] == "prune":
		return _prune_native_cache_request(payload["request"])
	if payload["action"] == "prepare-namespace":
		return _prepare_namespace_request(payload["request"])
	return {"status": "fallback", "reason": "unsupported-cache-action"}
