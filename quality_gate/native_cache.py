"""Bounded persistent namespaces for native Ruff and mypy caches."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import BinaryIO, Literal

from .file_lock import FileLockUnavailableError, locked_file, open_locked_file, unlock_file
from .path_safety import is_link_or_reparse

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


@dataclass(frozen=True, slots=True)
class _SourceWorkspaceIdentity:
	"""Filesystem identities that authorize source staging cleanup for one lease."""

	namespace: tuple[int, int]
	namespace_marker: tuple[int, int]
	stage_parent: tuple[int, int]
	parent_owner_marker: tuple[int, int]
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
	engine_version: str
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


def _is_link(path: Path) -> bool:
	try:
		metadata = path.lstat()
	except FileNotFoundError:
		return False
	except OSError:
		return True
	return is_link_or_reparse(metadata)


def _absolute_lexical_path(path: Path) -> Path:
	"""Make a path absolute without resolving or collapsing filesystem components."""
	requested = path.expanduser()
	if requested.is_absolute():
		return requested
	if os.name == "nt" and os.path.splitdrive(os.fspath(requested))[0]:
		return requested
	return Path.cwd() / requested


def _directory_identity(path: Path) -> tuple[int, int] | None:
	try:
		metadata = path.lstat()
	except FileNotFoundError:
		return None
	except OSError as error:
		raise NativeToolCacheError("unsafe-source-path") from error
	if is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
		raise NativeToolCacheError("unsafe-source-path")
	return metadata.st_dev, metadata.st_ino


def _require_directory_identity(
	path: Path, expected: tuple[int, int] | None
) -> tuple[int, int] | None:
	actual = _directory_identity(path)
	if actual != expected:
		raise NativeToolCacheError("unsafe-source-path")
	return actual


def _cache_directory_identity(path: Path) -> tuple[int, int] | None:
	try:
		metadata = path.lstat()
	except FileNotFoundError:
		return None
	if is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
		raise NativeToolCacheError("unsafe-cache-path")
	return metadata.st_dev, metadata.st_ino


def _namespace_marker_identity(
	path: Path,
	namespace_id: str,
	tool: Literal["ruff", "mypy"] | None,
) -> tuple[int, int]:
	marker = path / _MARKER
	try:
		metadata = marker.lstat()
	except FileNotFoundError as error:
		raise NativeToolCacheError("namespace-marker-missing") from error
	except OSError as error:
		raise NativeToolCacheError("namespace-marker-invalid") from error
	if is_link_or_reparse(metadata) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
		raise NativeToolCacheError("namespace-marker-foreign")
	marker_data = _read_bounded_json(marker)
	if (
		not isinstance(marker_data, dict)
		or marker_data.get("owner") != _OWNER
		or marker_data.get("schema") != _SCHEMA
		or marker_data.get("namespace_id") != namespace_id
		or marker_data.get("tool") != tool
		or not isinstance(marker_data.get("last_used_ns"), int)
		or isinstance(marker_data.get("last_used_ns"), bool)
	):
		raise NativeToolCacheError("namespace-marker-foreign")
	return metadata.st_dev, metadata.st_ino


def _read_bounded_json(path: Path) -> object | None:
	try:
		with path.open("rb") as stream:
			payload = stream.read(_MAX_MARKER_BYTES + 1)
		if len(payload) > _MAX_MARKER_BYTES:
			return None
		return json.loads(payload.decode("utf-8"))
	except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
		return None


def _json_identity(value: object) -> tuple[int, int] | None:
	if not isinstance(value, list) or len(value) != _FILE_IDENTITY_COMPONENTS:
		return None
	first, second = value
	if (
		not isinstance(first, int)
		or isinstance(first, bool)
		or not isinstance(second, int)
		or isinstance(second, bool)
	):
		return None
	return first, second


def _eviction_owner_path(path: Path) -> Path:
	return path.with_name(path.name + _EVICTION_OWNER_SUFFIX)


def _read_eviction_owner(
	path: Path,
	*,
	tool: str,
	namespace_id: str,
) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]] | None:
	owner_path = _eviction_owner_path(path)
	try:
		metadata = owner_path.lstat()
	except OSError:
		return None
	if is_link_or_reparse(metadata) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
		return None
	owner = _read_bounded_json(owner_path)
	if (
		not isinstance(owner, dict)
		or owner.get("owner") != _OWNER
		or owner.get("schema") != _SCHEMA
		or owner.get("kind") != _EVICTION_OWNER_KIND
		or owner.get("tool") != tool
		or owner.get("namespace_id") != namespace_id
	):
		return None
	directory_identity = _json_identity(owner.get("directory_identity"))
	marker_identity = _json_identity(owner.get("marker_identity"))
	if directory_identity is None or marker_identity is None:
		return None
	return directory_identity, marker_identity, (metadata.st_dev, metadata.st_ino)


def _create_eviction_owner(
	path: Path,
	*,
	tool: str,
	namespace_id: str,
	directory_identity: tuple[int, int],
	marker_identity: tuple[int, int],
) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]] | None:
	existing = _read_eviction_owner(path, tool=tool, namespace_id=namespace_id)
	if existing is not None:
		return existing if existing[:2] == (directory_identity, marker_identity) else None
	owner_path = _eviction_owner_path(path)
	payload = json.dumps(
		{
			"owner": _OWNER,
			"schema": _SCHEMA,
			"kind": _EVICTION_OWNER_KIND,
			"tool": tool,
			"namespace_id": namespace_id,
			"directory_identity": directory_identity,
			"marker_identity": marker_identity,
		},
		sort_keys=True,
		separators=(",", ":"),
	).encode("utf-8")
	flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
	try:
		handle = os.open(owner_path, flags, _SOURCE_FILE_MODE)
	except FileExistsError:
		return _read_eviction_owner(path, tool=tool, namespace_id=namespace_id)
	with os.fdopen(handle, "wb") as stream:
		stream.write(payload)
		stream.flush()
		os.fsync(stream.fileno())
	created = _read_eviction_owner(path, tool=tool, namespace_id=namespace_id)
	if created is None or created[:2] != (directory_identity, marker_identity):
		return None
	return created


def _remove_eviction_owner(
	path: Path,
	*,
	tool: str,
	namespace_id: str,
	expected: tuple[tuple[int, int], tuple[int, int], tuple[int, int]],
) -> bool:
	owner_path = _eviction_owner_path(path)
	current = _read_eviction_owner(path, tool=tool, namespace_id=namespace_id)
	if current != expected:
		return False
	try:
		metadata = owner_path.lstat()
		if (
			is_link_or_reparse(metadata)
			or not stat.S_ISREG(metadata.st_mode)
			or metadata.st_nlink != 1
			or (metadata.st_dev, metadata.st_ino) != expected[2]
		):
			return False
		owner_path.unlink()
	except OSError:
		return False
	return True


def _remove_eviction_payload(
	path: Path,
	*,
	namespace_id: str,
	tool: Literal["ruff", "mypy"],
	namespace_identity: tuple[int, int],
	marker_identity: tuple[int, int],
) -> bool:
	for child in tuple(path.iterdir()):
		if child.name == _MARKER:
			continue
		if (
			_cache_directory_identity(path) != namespace_identity
			or _namespace_marker_identity(path, namespace_id, tool) != marker_identity
		):
			return False
		metadata = child.lstat()
		if is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
			child.unlink()
		else:
			shutil.rmtree(child)
	if (
		_cache_directory_identity(path) != namespace_identity
		or _namespace_marker_identity(path, namespace_id, tool) != marker_identity
	):
		return False
	marker = path / _MARKER
	if any(child.name != _MARKER for child in path.iterdir()):
		return False
	marker.unlink()
	return (
		_cache_directory_identity(path) == namespace_identity and next(path.iterdir(), None) is None
	)


def _stable_source_mtime_ns(content_digest: bytes) -> int:
	"""Give equal source bytes stable metadata for Ruff's timestamp-based entries."""
	return int.from_bytes(content_digest[:8], "big") & ((1 << 63) - 1)


def _copy_source_file(source: str, destination: str) -> str:
	source_path = Path(source)
	destination_path = Path(destination)
	digest = hashlib.sha256()
	try:
		with (
			source_path.open("rb") as input_stream,
			os.fdopen(
				os.open(
					destination_path,
					os.O_WRONLY | os.O_CREAT | os.O_EXCL,
					_SOURCE_FILE_MODE,
				),
				"wb",
			) as output_stream,
		):
			while block := input_stream.read(1024 * 1024):
				digest.update(block)
				output_stream.write(block)
		os.chmod(destination_path, _SOURCE_FILE_MODE)
		mtime_ns = _stable_source_mtime_ns(digest.digest())
		os.utime(destination_path, ns=(mtime_ns, mtime_ns))
	except OSError as error:
		raise NativeToolCacheError("source-staging-unavailable") from error
	return destination


def _make_writable(path: Path) -> None:
	try:
		children = sorted(path.rglob("*"), key=lambda child: len(child.parts))
		for child in children:
			if _is_link(child):
				continue
			os.chmod(child, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
		os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
	except OSError as error:
		raise NativeToolCacheError("source-cleanup-unavailable") from error


def _make_read_only(path: Path) -> None:
	try:
		children = sorted(path.rglob("*"), key=lambda child: len(child.parts), reverse=True)
		for child in children:
			if _is_link(child):
				continue
			mode = (
				_SOURCE_READ_ONLY_DIRECTORY_MODE if child.is_dir() else _SOURCE_READ_ONLY_FILE_MODE
			)
			os.chmod(child, mode)
		os.chmod(path, _SOURCE_READ_ONLY_DIRECTORY_MODE)
	except OSError as error:
		raise NativeToolCacheError("source-staging-unavailable") from error


def _copy_directory_contents(source_root: Path, destination_root: Path) -> None:
	try:
		with os.scandir(source_root) as iterator:
			entries = sorted(iterator, key=lambda entry: entry.name)
	except OSError as error:
		raise NativeToolCacheError("source-staging-unavailable") from error
	for entry in entries:
		source = Path(entry.path)
		destination = destination_root / entry.name
		try:
			if source.is_symlink():
				os.symlink(os.readlink(source), destination, target_is_directory=source.is_dir())
			elif _is_link(source):
				raise NativeToolCacheError("unsafe-source-path")
			elif source.is_dir():
				destination.mkdir(mode=_SOURCE_DIRECTORY_MODE)
				os.chmod(destination, _SOURCE_DIRECTORY_MODE)
				_copy_directory_contents(source, destination)
				os.utime(
					destination,
					ns=(_SOURCE_DIRECTORY_MTIME_NS, _SOURCE_DIRECTORY_MTIME_NS),
				)
			elif source.is_file():
				_copy_source_file(str(source), str(destination))
			else:
				raise NativeToolCacheError("source-staging-unavailable")
		except NativeToolCacheError:
			raise
		except (OSError, RuntimeError, ValueError) as error:
			raise NativeToolCacheError("source-staging-unavailable") from error


def _copy_candidate(source_root: Path, destination_root: Path) -> None:
	try:
		resolved_source = source_root.resolve(strict=True)
		if not resolved_source.is_dir() or _is_link(source_root):
			raise NativeToolCacheError("unsafe-source-path")
		if _directory_identity(destination_root) is None:
			raise NativeToolCacheError("source-staging-unavailable")
		_copy_directory_contents(resolved_source, destination_root)
		os.utime(
			destination_root,
			ns=(_SOURCE_DIRECTORY_MTIME_NS, _SOURCE_DIRECTORY_MTIME_NS),
		)
	except NativeToolCacheError:
		raise
	except (OSError, RuntimeError, ValueError) as error:
		raise NativeToolCacheError("source-staging-unavailable") from error


def _source_owner_marker_is_valid(marker: Path) -> bool:
	if _is_link(marker) or not marker.is_dir():
		return False
	try:
		return next(marker.iterdir(), None) is None
	except OSError:
		return False


def _validate_source_workspace_ownership(
	selection: NativeToolCacheSelection,
	stage_parent: Path,
	stable_root: Path,
	owner_marker: Path,
	parent_owner_marker: Path,
	*,
	expected: _SourceWorkspaceIdentity | None = None,
) -> _SourceWorkspaceIdentity:
	namespace_identity = _directory_identity(selection.path)
	namespace_marker_identity = _namespace_marker_identity(
		selection.path, selection.namespace_id, selection._tool
	)
	stage_parent_identity = _directory_identity(stage_parent)
	if (
		namespace_identity is None
		or namespace_identity != selection._path_identity
		or namespace_marker_identity != selection._marker_identity
		or stage_parent_identity is None
	):
		raise NativeToolCacheError("unsafe-source-path")
	try:
		entries = tuple(stage_parent.iterdir())
	except OSError as error:
		raise NativeToolCacheError("unsafe-source-path") from error
	if any(
		entry.name not in {_SOURCE_ROOT, _SOURCE_OWNER_DIRECTORY, _SOURCE_PARENT_OWNER_DIRECTORY}
		for entry in entries
	):
		raise NativeToolCacheError("unsafe-source-path")
	if not _source_owner_marker_is_valid(parent_owner_marker):
		raise NativeToolCacheError("unsafe-source-path")
	parent_owner_identity = _directory_identity(parent_owner_marker)
	owner_identity = _directory_identity(owner_marker)
	stable_identity = _directory_identity(stable_root)
	if parent_owner_identity is None:
		raise NativeToolCacheError("unsafe-source-path")
	if owner_identity is not None:
		if not _source_owner_marker_is_valid(owner_marker):
			raise NativeToolCacheError("unsafe-source-path")
	elif stable_identity is not None:
		raise NativeToolCacheError("unsafe-source-path")
	if expected is not None and (
		namespace_identity != expected.namespace
		or namespace_marker_identity != expected.namespace_marker
		or stage_parent_identity != expected.stage_parent
		or parent_owner_identity != expected.parent_owner_marker
		or owner_identity != expected.owner_marker
		or stable_identity != expected.stable_root
	):
		raise NativeToolCacheError("unsafe-source-path")
	return _SourceWorkspaceIdentity(
		namespace_identity,
		namespace_marker_identity,
		stage_parent_identity,
		parent_owner_identity,
		owner_identity,
		stable_identity,
	)


def _remove_owned_source_workspace(
	selection: NativeToolCacheSelection,
	stage_parent: Path,
	stable_root: Path,
	owner_marker: Path,
	parent_owner_marker: Path,
	*,
	expected: _SourceWorkspaceIdentity | None = None,
) -> None:
	identity = _validate_source_workspace_ownership(
		selection,
		stage_parent,
		stable_root,
		owner_marker,
		parent_owner_marker,
		expected=expected,
	)
	if identity.owner_marker is not None:
		if identity.stable_root is not None:
			_make_writable(stable_root)
			_validate_source_workspace_ownership(
				selection,
				stage_parent,
				stable_root,
				owner_marker,
				parent_owner_marker,
				expected=identity,
			)
			_require_directory_identity(stable_root, identity.stable_root)
			try:
				shutil.rmtree(stable_root)
			except OSError as error:
				raise NativeToolCacheError("source-cleanup-unavailable") from error
		identity = replace(identity, stable_root=None)
		_validate_source_workspace_ownership(
			selection,
			stage_parent,
			stable_root,
			owner_marker,
			parent_owner_marker,
			expected=identity,
		)
		try:
			owner_marker.rmdir()
		except OSError as error:
			raise NativeToolCacheError("source-cleanup-unavailable") from error


def _remove_new_empty_source_parent(
	selection: NativeToolCacheSelection,
	stage_parent: Path,
	expected_stage_parent: tuple[int, int],
	parent_owner_marker: Path,
	expected_parent_owner: tuple[int, int] | None,
) -> None:
	try:
		if (
			_directory_identity(selection.path) != selection._path_identity
			or _namespace_marker_identity(selection.path, selection.namespace_id, selection._tool)
			!= selection._marker_identity
			or _directory_identity(stage_parent) != expected_stage_parent
		):
			return
		if expected_parent_owner is not None:
			if (
				_directory_identity(parent_owner_marker) != expected_parent_owner
				or next(parent_owner_marker.iterdir(), None) is not None
			):
				return
			parent_owner_marker.rmdir()
		if (
			_directory_identity(selection.path) != selection._path_identity
			or _namespace_marker_identity(selection.path, selection.namespace_id, selection._tool)
			!= selection._marker_identity
			or _directory_identity(stage_parent) != expected_stage_parent
			or next(stage_parent.iterdir(), None) is not None
		):
			return
		stage_parent.rmdir()
	except OSError:
		pass


def _selected_source_namespace_identity(
	selection: NativeToolCacheSelection,
) -> tuple[int, int]:
	namespace_identity = _directory_identity(selection.path)
	try:
		marker_identity = _namespace_marker_identity(
			selection.path, selection.namespace_id, selection._tool
		)
	except NativeToolCacheError as error:
		raise NativeToolCacheError("unsafe-source-path") from error
	if (
		namespace_identity is None
		or namespace_identity != selection._path_identity
		or marker_identity != selection._marker_identity
	):
		raise NativeToolCacheError("unsafe-source-path")
	return namespace_identity


def _prepare_source_parent(
	stage_parent: Path,
	parent_owner_marker: Path,
	setup: _SourceWorkspaceSetup,
) -> None:
	if _is_link(stage_parent):
		raise NativeToolCacheError("unsafe-source-path")
	setup.stage_parent = _directory_identity(stage_parent)
	if setup.stage_parent is None:
		stage_parent.mkdir(mode=_SOURCE_DIRECTORY_MODE)
		setup.stage_parent_created = True
		setup.stage_parent = _directory_identity(stage_parent)
		if setup.stage_parent is None:
			raise NativeToolCacheError("unsafe-source-path")
	else:
		try:
			parent_owner_marker.lstat()
		except FileNotFoundError:
			try:
				stage_entries = tuple(stage_parent.iterdir())
			except OSError as error:
				raise NativeToolCacheError("unsafe-source-path") from error
			if not stage_entries and _directory_identity(stage_parent) == setup.stage_parent:
				setup.stage_parent_created = True
	if not setup.stage_parent_created:
		return
	os.chmod(stage_parent, _SOURCE_DIRECTORY_MODE)
	parent_owner_marker.mkdir(mode=_SOURCE_DIRECTORY_MODE)
	setup.parent_owner = _directory_identity(parent_owner_marker)
	if setup.parent_owner is None:
		raise NativeToolCacheError("unsafe-source-path")
	os.chmod(parent_owner_marker, _SOURCE_DIRECTORY_MODE)


def _create_source_workspace(
	selection: NativeToolCacheSelection,
	snapshot_root: Path,
	stage_parent: Path,
	stable_root: Path,
	owner_marker: Path,
	parent_owner_marker: Path,
	setup: _SourceWorkspaceSetup,
) -> _SourceWorkspaceIdentity:
	ownership = _validate_source_workspace_ownership(
		selection,
		stage_parent,
		stable_root,
		owner_marker,
		parent_owner_marker,
	)
	if ownership.namespace != setup.namespace:
		raise NativeToolCacheError("unsafe-source-path")
	os.chmod(stage_parent, _SOURCE_DIRECTORY_MODE)
	_remove_owned_source_workspace(
		selection,
		stage_parent,
		stable_root,
		owner_marker,
		parent_owner_marker,
		expected=ownership,
	)
	owner_marker.mkdir(mode=_SOURCE_DIRECTORY_MODE)
	setup.owner_marker_created = True
	os.chmod(owner_marker, _SOURCE_DIRECTORY_MODE)
	ownership = _validate_source_workspace_ownership(
		selection,
		stage_parent,
		stable_root,
		owner_marker,
		parent_owner_marker,
	)
	if ownership.namespace != setup.namespace:
		raise NativeToolCacheError("unsafe-source-path")
	stable_root.mkdir(mode=_SOURCE_DIRECTORY_MODE)
	os.chmod(stable_root, _SOURCE_DIRECTORY_MODE)
	ownership = replace(ownership, stable_root=_directory_identity(stable_root))
	_copy_candidate(snapshot_root, stable_root)
	_make_read_only(stable_root)
	completed = _validate_source_workspace_ownership(
		selection,
		stage_parent,
		stable_root,
		owner_marker,
		parent_owner_marker,
	)
	if replace(ownership, stable_root=completed.stable_root) != completed:
		raise NativeToolCacheError("unsafe-source-path")
	return completed


def _rollback_source_setup(
	selection: NativeToolCacheSelection,
	stage_parent: Path,
	stable_root: Path,
	owner_marker: Path,
	parent_owner_marker: Path,
	setup: _SourceWorkspaceSetup,
) -> None:
	if setup.owner_marker_created:
		try:
			ownership = _validate_source_workspace_ownership(
				selection,
				stage_parent,
				stable_root,
				owner_marker,
				parent_owner_marker,
			)
			_remove_owned_source_workspace(
				selection,
				stage_parent,
				stable_root,
				owner_marker,
				parent_owner_marker,
				expected=ownership,
			)
		except OSError:
			pass
	elif setup.stage_parent_created and setup.stage_parent is not None:
		_remove_new_empty_source_parent(
			selection,
			stage_parent,
			setup.stage_parent,
			parent_owner_marker,
			setup.parent_owner,
		)


def _prepare_source_workspace(
	selection: NativeToolCacheSelection,
	snapshot_root: Path,
	stage_parent: Path,
	stable_root: Path,
	owner_marker: Path,
	parent_owner_marker: Path,
) -> _SourceWorkspaceIdentity:
	if os.name == "nt" and len(os.fspath(stable_root)) >= _WINDOWS_MAX_PATH:
		raise NativeToolCacheError("source-staging-unavailable")
	setup = _SourceWorkspaceSetup(_selected_source_namespace_identity(selection))
	try:
		_prepare_source_parent(stage_parent, parent_owner_marker, setup)
		return _create_source_workspace(
			selection,
			snapshot_root,
			stage_parent,
			stable_root,
			owner_marker,
			parent_owner_marker,
			setup,
		)
	except OSError as error:
		_rollback_source_setup(
			selection,
			stage_parent,
			stable_root,
			owner_marker,
			parent_owner_marker,
			setup,
		)
		if isinstance(error, NativeToolCacheError):
			raise
		raise NativeToolCacheError("source-staging-unavailable") from error


@contextmanager
def native_tool_source_workspace(
	selection: NativeToolCacheSelection,
	snapshot_root: Path,
	private_cache_path: Path,
) -> Iterator[tuple[NativeToolCacheSelection, Path]]:
	"""Run a persistent native cache against a stable, disposable source root."""
	if selection.storage == "private":
		yield selection, snapshot_root
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

	def namespace_path(self, identity: NativeToolCacheIdentity) -> Path:
		"""Return the stable, non-repository path selected by this identity."""
		return (
			self.root
			/ _CACHE_DIRECTORY
			/ identity.tool
			/ identity.repository_key
			/ identity.namespace_id
		)

	def _cache_root(self) -> Path:
		return self.root / _CACHE_DIRECTORY

	def _repository_tool_root(self, identity: NativeToolCacheIdentity) -> Path:
		return self._cache_root() / identity.tool / identity.repository_key

	def _lock_path(
		self, identity: NativeToolCacheIdentity, *, namespace_id: str | None = None
	) -> Path:
		selected_namespace = identity.namespace_id if namespace_id is None else namespace_id
		return (
			self._cache_root()
			/ _LOCK_DIRECTORY
			/ identity.tool
			/ identity.repository_key[:2]
			/ f"{selected_namespace[:2]}.lock"
		)

	def _ensure_directory(self, path: Path) -> None:
		try:
			relative = path.relative_to(self.root)
		except ValueError as error:
			raise NativeToolCacheError("unsafe-cache-path") from error
		root_chain = [*reversed(self.root.parents), self.root]
		for current in root_chain:
			if _is_link(current):
				raise NativeToolCacheError("unsafe-cache-path")
			try:
				metadata = current.lstat()
			except FileNotFoundError:
				current.mkdir()
				metadata = current.lstat()
			if _is_link(current) or not stat.S_ISDIR(metadata.st_mode):
				raise NativeToolCacheError("unsafe-cache-path")
		for part in relative.parts:
			current = current / part
			if _is_link(current):
				raise NativeToolCacheError("unsafe-cache-path")
			current.mkdir(exist_ok=True)
			if _is_link(current) or not current.is_dir():
				raise NativeToolCacheError("unsafe-cache-path")

	def _marker_data(
		self, identity: NativeToolCacheIdentity, *, last_used_ns: int
	) -> dict[str, object]:
		return {
			"owner": _OWNER,
			"schema": _SCHEMA,
			"namespace_id": identity.namespace_id,
			"tool": identity.tool,
			"last_used_ns": last_used_ns,
		}

	def _read_marker(
		self, path: Path, *, tool: str, namespace_id: str
	) -> tuple[dict[str, object] | None, str | None]:
		marker = path / _MARKER
		if _is_link(marker) or not marker.is_file():
			return None, "namespace-marker-missing"
		marker_data = _read_bounded_json(marker)
		if not isinstance(marker_data, dict):
			return None, "namespace-marker-invalid"
		if (
			marker_data.get("owner") != _OWNER
			or marker_data.get("schema") != _SCHEMA
			or marker_data.get("tool") != tool
			or marker_data.get("namespace_id") != namespace_id
			or not isinstance(marker_data.get("last_used_ns"), int)
			or isinstance(marker_data.get("last_used_ns"), bool)
		):
			return None, "namespace-marker-foreign"
		return marker_data, None

	@staticmethod
	def _remove_incomplete_marker_temporary(
		path: Path,
		expected_namespace: tuple[int, int],
	) -> bool:
		try:
			entries = tuple(path.iterdir())
			if len(entries) != 1:
				return False
			temporary = entries[0]
			prefix = f".{_MARKER}."
			if not temporary.name.startswith(prefix) or not temporary.name.endswith(".tmp"):
				return False
			process_id = temporary.name[len(prefix) : -len(".tmp")]
			if not process_id or not all(character in "0123456789" for character in process_id):
				return False
			metadata = temporary.lstat()
			if (
				is_link_or_reparse(metadata)
				or not stat.S_ISREG(metadata.st_mode)
				or metadata.st_nlink != 1
				or _cache_directory_identity(path) != expected_namespace
			):
				return False
			temporary.unlink()
			return _cache_directory_identity(path) == expected_namespace and not tuple(
				path.iterdir()
			)
		except OSError:
			return False

	def _write_marker(self, path: Path, identity: NativeToolCacheIdentity) -> None:
		marker = path / _MARKER
		if _is_link(marker):
			raise NativeToolCacheError("namespace-marker-foreign")
		temporary = path / f".{_MARKER}.{os.getpid()}.tmp"
		created = False
		try:
			with temporary.open("x", encoding="utf-8", newline="\n") as stream:
				created = True
				json.dump(
					self._marker_data(identity, last_used_ns=time.time_ns()),
					stream,
					sort_keys=True,
					separators=(",", ":"),
				)
				stream.write("\n")
			os.replace(temporary, marker)
		except OSError as error:
			if created:
				try:
					temporary.unlink()
				except OSError:
					pass
			raise NativeToolCacheError("storage-unavailable") from error

	def _entries(self, repository_tool_root: Path) -> tuple[list[Path], bool]:
		if _is_link(repository_tool_root):
			raise NativeToolCacheError("unsafe-cache-path")
		if not repository_tool_root.exists():
			return [], False
		if not repository_tool_root.is_dir():
			raise NativeToolCacheError("unsafe-cache-path")
		entries: list[Path] = []
		with os.scandir(repository_tool_root) as iterator:
			for item in iterator:
				path = Path(item.path)
				try:
					if _is_link(path) or item.is_dir(follow_symlinks=False):
						entries.append(path)
				except OSError:
					entries.append(path)
				if len(entries) > _MAX_SCANNED_NAMESPACES:
					return entries, True
		return entries, False

	def _try_evict(self, path: Path, identity: NativeToolCacheIdentity) -> bool:
		if _is_link(path) or not path.is_dir():
			return False
		namespace_id = path.name
		marker, _reason = self._read_marker(
			path,
			tool=identity.tool,
			namespace_id=namespace_id,
		)
		if marker is None:
			return False
		lock_path = self._lock_path(identity, namespace_id=namespace_id)
		self._ensure_directory(lock_path.parent)
		handle = open_locked_file(lock_path, blocking=False)
		if handle is None:
			return False
		try:
			namespace_identity = _cache_directory_identity(path)
			marker_identity = _namespace_marker_identity(path, namespace_id, identity.tool)
			marker, _reason = self._read_marker(
				path,
				tool=identity.tool,
				namespace_id=namespace_id,
			)
			if marker is None or namespace_identity is None:
				return False
			if (
				_cache_directory_identity(path) != namespace_identity
				or _namespace_marker_identity(path, namespace_id, identity.tool) != marker_identity
			):
				return False
			eviction_path = self._quarantine_namespace(
				path, namespace_id, identity.tool, namespace_identity, marker_identity
			)
			if eviction_path is None:
				return False
			if not self._remove_evicted_namespace(
				eviction_path, namespace_id, identity.tool, namespace_identity, marker_identity
			):
				self._restore_owned_eviction(
					path, eviction_path, namespace_id, identity.tool, marker_identity
				)
				return False
			return True
		except OSError:
			return False
		finally:
			unlock_file(handle)

	def _restore_owned_eviction(
		self,
		path: Path,
		eviction_path: Path,
		namespace_id: str,
		tool: Literal["ruff", "mypy"],
		marker_identity: tuple[int, int],
	) -> None:
		try:
			if _namespace_marker_identity(eviction_path, namespace_id, tool) == marker_identity:
				self._restore_eviction_path(path, eviction_path)
		except NativeToolCacheError:
			return

	@staticmethod
	def _eviction_namespace_id(path: Path) -> str | None:
		prefix = ".evict-"
		if not path.name.startswith(prefix):
			return None
		namespace_id, separator, token = path.name[len(prefix) :].partition("-")
		if (
			separator != "-"
			or len(namespace_id) != _SHA256_HEX_LENGTH
			or any(character not in "0123456789abcdef" for character in namespace_id)
			or len(token) != _EVICTION_TOKEN_HEX_LENGTH
			or any(character not in "0123456789abcdef" for character in token)
		):
			return None
		return namespace_id

	@staticmethod
	def _new_eviction_path(path: Path, namespace_id: str) -> Path | None:
		for _attempt in range(8):
			token = os.urandom(16).hex()
			candidate = path.parent / f".evict-{namespace_id}-{token}"
			try:
				candidate.lstat()
			except FileNotFoundError:
				return candidate
		return None

	def _quarantine_namespace(
		self,
		path: Path,
		namespace_id: str,
		tool: Literal["ruff", "mypy"],
		namespace_identity: tuple[int, int],
		marker_identity: tuple[int, int],
	) -> Path | None:
		eviction_path = self._new_eviction_path(path, namespace_id)
		if eviction_path is None:
			return None
		os.rename(path, eviction_path)
		try:
			if (
				_cache_directory_identity(eviction_path) != namespace_identity
				or _namespace_marker_identity(eviction_path, namespace_id, tool) != marker_identity
			):
				self._restore_eviction_path(path, eviction_path)
				return None
		except OSError:
			self._restore_eviction_path(path, eviction_path)
			return None
		return eviction_path

	def _remove_evicted_namespace(
		self,
		path: Path,
		namespace_id: str,
		tool: Literal["ruff", "mypy"],
		namespace_identity: tuple[int, int],
		marker_identity: tuple[int, int],
	) -> bool:
		try:
			owner = _create_eviction_owner(
				path,
				tool=tool,
				namespace_id=namespace_id,
				directory_identity=namespace_identity,
				marker_identity=marker_identity,
			)
			if owner is None or owner[:2] != (namespace_identity, marker_identity):
				return False
			if not _remove_eviction_payload(
				path,
				namespace_id=namespace_id,
				tool=tool,
				namespace_identity=namespace_identity,
				marker_identity=marker_identity,
			):
				return False
			path.rmdir()
			_remove_eviction_owner(
				path,
				tool=tool,
				namespace_id=namespace_id,
				expected=owner,
			)
			return True
		except OSError:
			return False

	def _recover_empty_eviction(
		self,
		path: Path,
		namespace_id: str,
		tool: Literal["ruff", "mypy"],
	) -> bool:
		owner = _read_eviction_owner(path, tool=tool, namespace_id=namespace_id)
		if owner is None:
			return False
		try:
			if _cache_directory_identity(path) != owner[0]:
				return False
			try:
				(path / _MARKER).lstat()
			except FileNotFoundError:
				pass
			else:
				return False
			if next(path.iterdir(), None) is not None:
				return False
			if _read_eviction_owner(path, tool=tool, namespace_id=namespace_id) != owner:
				return False
			if _cache_directory_identity(path) != owner[0]:
				return False
			path.rmdir()
		except OSError:
			return False
		_remove_eviction_owner(
			path,
			tool=tool,
			namespace_id=namespace_id,
			expected=owner,
		)
		return True

	def _recover_orphaned_eviction_owners(
		self,
		repository_tool_root: Path,
		tool: Literal["ruff", "mypy"],
	) -> None:
		try:
			with os.scandir(repository_tool_root) as iterator:
				owner_paths = tuple(
					Path(item.path)
					for item in iterator
					if item.name.startswith(".evict-")
					and item.name.endswith(_EVICTION_OWNER_SUFFIX)
				)
		except OSError:
			return
		for owner_path in owner_paths:
			eviction_path = owner_path.with_name(owner_path.name[: -len(_EVICTION_OWNER_SUFFIX)])
			namespace_id = self._eviction_namespace_id(eviction_path)
			if namespace_id is None:
				continue
			try:
				eviction_path.lstat()
			except FileNotFoundError:
				owner = _read_eviction_owner(
					eviction_path,
					tool=tool,
					namespace_id=namespace_id,
				)
				if owner is not None:
					_remove_eviction_owner(
						eviction_path,
						tool=tool,
						namespace_id=namespace_id,
						expected=owner,
					)
			except OSError:
				continue

	def _recover_evictions(self, identity: NativeToolCacheIdentity) -> None:
		repository_tool_root = self._repository_tool_root(identity)
		entries, _overflow = self._entries(repository_tool_root)
		for eviction_path in entries:
			namespace_id = self._eviction_namespace_id(eviction_path)
			if namespace_id is None or _is_link(eviction_path) or not eviction_path.is_dir():
				continue
			self._recover_eviction_path(eviction_path, namespace_id, identity)
		self._recover_orphaned_eviction_owners(repository_tool_root, identity.tool)

	def _recover_eviction_path(
		self,
		eviction_path: Path,
		namespace_id: str,
		identity: NativeToolCacheIdentity,
	) -> None:
		lease_path = self._lock_path(identity, namespace_id=namespace_id)
		try:
			self._ensure_directory(lease_path.parent)
			handle = open_locked_file(lease_path, blocking=False)
		except OSError:
			return
		if handle is None:
			return
		try:
			self._recover_eviction_under_lock(eviction_path, namespace_id, identity)
		finally:
			unlock_file(handle)

	def _recover_eviction_under_lock(
		self,
		eviction_path: Path,
		namespace_id: str,
		identity: NativeToolCacheIdentity,
	) -> None:
		namespace_identity = _cache_directory_identity(eviction_path)
		if namespace_identity is None:
			return
		marker, reason = self._read_marker(
			eviction_path,
			tool=identity.tool,
			namespace_id=namespace_id,
		)
		if marker is None:
			if reason == "namespace-marker-missing":
				self._recover_empty_eviction(eviction_path, namespace_id, identity.tool)
			return
		try:
			marker_identity = _namespace_marker_identity(eviction_path, namespace_id, identity.tool)
		except NativeToolCacheError:
			return
		if (
			_cache_directory_identity(eviction_path) != namespace_identity
			or _namespace_marker_identity(eviction_path, namespace_id, identity.tool)
			!= marker_identity
		):
			return
		quarantined_path = self._quarantine_namespace(
			eviction_path,
			namespace_id,
			identity.tool,
			namespace_identity,
			marker_identity,
		)
		if quarantined_path is None or self._remove_evicted_namespace(
			quarantined_path,
			namespace_id,
			identity.tool,
			namespace_identity,
			marker_identity,
		):
			return
		self._restore_owned_eviction(
			eviction_path,
			quarantined_path,
			namespace_id,
			identity.tool,
			marker_identity,
		)

	@staticmethod
	def _restore_eviction_path(path: Path, eviction_path: Path) -> None:
		try:
			path.lstat()
		except FileNotFoundError:
			pass
		except OSError:
			return
		else:
			return
		try:
			os.rename(eviction_path, path)
		except OSError:
			pass

	def _last_used_ns(
		self,
		entry: Path,
		identity: NativeToolCacheIdentity,
	) -> int | None:
		if _is_link(entry) or not entry.is_dir():
			return None
		marker, _reason = self._read_marker(
			entry,
			tool=identity.tool,
			namespace_id=entry.name,
		)
		if marker is None:
			return None
		last_used = marker["last_used_ns"]
		if not isinstance(last_used, int) or isinstance(last_used, bool):
			return None
		return last_used

	def _prune(
		self,
		identity: NativeToolCacheIdentity,
		*,
		exclude_namespace: str | None,
		needs_slot: bool,
	) -> bool:
		repository_tool_root = self._repository_tool_root(identity)
		self._recover_evictions(identity)
		entries, _truncated = self._entries(repository_tool_root)
		if exclude_namespace is not None:
			entries = [entry for entry in entries if entry.name != exclude_namespace]

		now_ns = time.time_ns()
		stale_before_ns = now_ns - _RETENTION_SECONDS * 1_000_000_000
		candidates: list[tuple[int, Path]] = []
		for entry in entries:
			last_used = self._last_used_ns(entry, identity)
			if last_used is not None:
				candidates.append((last_used, entry))

		candidates.sort(key=lambda item: (item[0], item[1].name))
		evictions = 0
		for last_used, entry in candidates:
			capacity_requires_eviction = needs_slot and (
				len(entries) >= _MAX_NAMESPACES_PER_REPOSITORY_TOOL
			)
			if last_used > stale_before_ns and not capacity_requires_eviction:
				continue
			if evictions >= _MAX_EVICTIONS_PER_LEASE:
				break
			if self._try_evict(entry, identity):
				evictions += 1
				entries.remove(entry)

		if not needs_slot:
			return True
		remaining, overflow = self._entries(repository_tool_root)
		return not overflow and len(remaining) < _MAX_NAMESPACES_PER_REPOSITORY_TOOL

	def _prepare_namespace(
		self,
		path: Path,
		identity: NativeToolCacheIdentity,
	) -> tuple[tuple[int, int], tuple[int, int]]:
		if path.exists():
			if _is_link(path) or not path.is_dir():
				raise NativeToolCacheError("unsafe-cache-path")
			namespace_identity = _cache_directory_identity(path)
			if namespace_identity is None:
				raise NativeToolCacheError("unsafe-cache-path")
			marker, reason = self._read_marker(
				path,
				tool=identity.tool,
				namespace_id=identity.namespace_id,
			)
			if marker is None:
				markerless_empty = reason == "namespace-marker-missing" and (
					not tuple(path.iterdir())
					or self._remove_incomplete_marker_temporary(path, namespace_identity)
				)
				if not markerless_empty:
					raise NativeToolCacheError(reason or "namespace-marker-invalid")
		else:
			path.mkdir()
			namespace_identity = _cache_directory_identity(path)
			if namespace_identity is None:
				raise NativeToolCacheError("unsafe-cache-path")
		self._write_marker(path, identity)
		if _cache_directory_identity(path) != namespace_identity:
			raise NativeToolCacheError("unsafe-cache-path")
		marker_identity = _namespace_marker_identity(path, identity.namespace_id, identity.tool)
		return namespace_identity, marker_identity

	def _acquire(
		self, identity: NativeToolCacheIdentity
	) -> tuple[Path, BinaryIO, tuple[int, int], tuple[int, int]]:
		if not self.root.is_absolute():
			raise NativeToolCacheError("unsafe-cache-path")
		cache_root = self._cache_root()
		self._ensure_directory(cache_root)
		repository_tool_root = self._repository_tool_root(identity)
		self._ensure_directory(repository_tool_root)
		maintenance_lock = cache_root / _MAINTENANCE_LOCK
		try:
			with locked_file(
				maintenance_lock,
				timeout_seconds=self.maintenance_timeout_seconds,
			):
				path = self.namespace_path(identity)
				if _is_link(path):
					raise NativeToolCacheError("unsafe-cache-path")
				exists = path.exists()
				if not self._prune(
					identity,
					exclude_namespace=identity.namespace_id if exists else None,
					needs_slot=not exists,
				):
					raise NativeToolCacheError("capacity-limit")
				lease_path = self._lock_path(identity)
				self._ensure_directory(lease_path.parent)
				handle = self._open_lease_lock(lease_path)
				try:
					namespace_identity, marker_identity = self._prepare_namespace(path, identity)
					return path, handle, namespace_identity, marker_identity
				except BaseException:
					try:
						unlock_file(handle)
					except OSError:
						pass
					raise
		except FileLockUnavailableError as error:
			raise NativeToolCacheError("lock-busy") from error

	def _open_lease_lock(self, path: Path) -> BinaryIO:
		deadline = time.monotonic() + self.lease_timeout_seconds
		while True:
			handle = open_locked_file(path, blocking=False)
			if handle is not None:
				return handle
			remaining = deadline - time.monotonic()
			if remaining <= 0:
				raise NativeToolCacheError("lock-busy")
			time.sleep(min(0.025, remaining))

	@contextmanager
	def lease(
		self,
		identity: NativeToolCacheIdentity,
		private_path: Path,
	) -> Iterator[NativeToolCacheSelection]:
		"""Hold a compatible cache lease across one real native tool invocation."""
		started = time.monotonic()
		try:
			path, handle, path_identity, marker_identity = self._acquire(identity)
		except NativeToolCacheError as error:
			yield NativeToolCacheSelection(
				identity.namespace_id,
				private_path,
				"private",
				error.reason,
				(time.monotonic() - started) * 1000,
			)
			return
		except OSError:
			yield NativeToolCacheSelection(
				identity.namespace_id,
				private_path,
				"private",
				"storage-unavailable",
				(time.monotonic() - started) * 1000,
			)
			return
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
		try:
			yield selection
		finally:
			try:
				unlock_file(handle)
			except OSError:
				pass
