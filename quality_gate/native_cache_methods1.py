"""Native cache namespace and eviction methods."""

from __future__ import annotations

from typing import Literal

from . import native_cache as _native


def namespace_path(
	self: _native.NativeToolCache, identity: _native.NativeToolCacheIdentity
) -> _native.Path:
	"""Return the stable, non-repository path selected by this identity."""
	return (
		self.root
		/ _native._CACHE_DIRECTORY
		/ identity.tool
		/ identity.repository_key
		/ identity.namespace_id
	)


def _cache_root(self: _native.NativeToolCache) -> _native.Path:
	return self.root / _native._CACHE_DIRECTORY


def _repository_tool_root(
	self: _native.NativeToolCache, identity: _native.NativeToolCacheIdentity
) -> _native.Path:
	return self._cache_root() / identity.tool / identity.repository_key


def _lock_path(
	self: _native.NativeToolCache,
	identity: _native.NativeToolCacheIdentity,
	*,
	namespace_id: str | None = None,
) -> _native.Path:
	selected_namespace = identity.namespace_id if namespace_id is None else namespace_id
	return (
		self._cache_root()
		/ _native._LOCK_DIRECTORY
		/ identity.tool
		/ identity.repository_key[:2]
		/ f"{selected_namespace[:2]}.lock"
	)


def _ensure_directory(self: _native.NativeToolCache, path: _native.Path) -> None:
	try:
		relative = path.relative_to(self.root)
	except ValueError as error:
		raise _native.NativeToolCacheError("unsafe-cache-path") from error
	root_chain = [*reversed(self.root.parents), self.root]
	for current in root_chain:
		if _native._is_link(current):
			raise _native.NativeToolCacheError("unsafe-cache-path")
		try:
			metadata = current.lstat()
		except FileNotFoundError:
			current.mkdir()
			metadata = current.lstat()
		if _native._is_link(current) or not _native.stat.S_ISDIR(metadata.st_mode):
			raise _native.NativeToolCacheError("unsafe-cache-path")
	for part in relative.parts:
		current = current / part
		if _native._is_link(current):
			raise _native.NativeToolCacheError("unsafe-cache-path")
		current.mkdir(exist_ok=True)
		if _native._is_link(current) or not current.is_dir():
			raise _native.NativeToolCacheError("unsafe-cache-path")


def _marker_data(
	self: _native.NativeToolCache, identity: _native.NativeToolCacheIdentity, *, last_used_ns: int
) -> dict[str, object]:
	return {
		"owner": _native._OWNER,
		"schema": _native._SCHEMA,
		"namespace_id": identity.namespace_id,
		"tool": identity.tool,
		"last_used_ns": last_used_ns,
	}


def _read_marker(
	self: _native.NativeToolCache, path: _native.Path, *, tool: str, namespace_id: str
) -> tuple[dict[str, object] | None, str | None]:
	marker = path / _native._MARKER
	if _native._is_link(marker) or not marker.is_file():
		return (None, "namespace-marker-missing")
	marker_data = _native._read_bounded_json(marker)
	if not isinstance(marker_data, dict):
		return (None, "namespace-marker-invalid")
	if (
		marker_data.get("owner") != _native._OWNER
		or marker_data.get("schema") != _native._SCHEMA
		or marker_data.get("tool") != tool
		or (marker_data.get("namespace_id") != namespace_id)
		or (not isinstance(marker_data.get("last_used_ns"), int))
		or isinstance(marker_data.get("last_used_ns"), bool)
	):
		return (None, "namespace-marker-foreign")
	return (marker_data, None)


def _remove_incomplete_marker_temporary(
	path: _native.Path, expected_namespace: tuple[int, int]
) -> bool:
	try:
		entries = tuple(path.iterdir())
		if len(entries) != 1:
			return False
		temporary = entries[0]
		prefix = f".{_native._MARKER}."
		if not temporary.name.startswith(prefix) or not temporary.name.endswith(".tmp"):
			return False
		process_id = temporary.name[len(prefix) : -len(".tmp")]
		if not process_id or not all(character in "0123456789" for character in process_id):
			return False
		metadata = temporary.lstat()
		if (
			_native.is_link_or_reparse(metadata)
			or not _native.stat.S_ISREG(metadata.st_mode)
			or metadata.st_nlink != 1
			or (_native._cache_directory_identity(path) != expected_namespace)
		):
			return False
		temporary.unlink()
		return _native._cache_directory_identity(path) == expected_namespace and (
			not tuple(path.iterdir())
		)
	except OSError:
		return False


def _write_marker(
	self: _native.NativeToolCache, path: _native.Path, identity: _native.NativeToolCacheIdentity
) -> None:
	marker = path / _native._MARKER
	if _native._is_link(marker):
		raise _native.NativeToolCacheError("namespace-marker-foreign")
	temporary = path / f".{_native._MARKER}.{_native.os.getpid()}.tmp"
	created = False
	try:
		with temporary.open("x", encoding="utf-8", newline="\n") as stream:
			created = True
			_native.json.dump(
				self._marker_data(identity, last_used_ns=_native.time.time_ns()),
				stream,
				sort_keys=True,
				separators=(",", ":"),
			)
			stream.write("\n")
		_native.os.replace(temporary, marker)
	except OSError as error:
		if created:
			try:
				temporary.unlink()
			except OSError:
				pass
		raise _native.NativeToolCacheError("storage-unavailable") from error


def _entries(
	self: _native.NativeToolCache, repository_tool_root: _native.Path
) -> tuple[list[_native.Path], bool]:
	if _native._is_link(repository_tool_root):
		raise _native.NativeToolCacheError("unsafe-cache-path")
	if not repository_tool_root.exists():
		return ([], False)
	if not repository_tool_root.is_dir():
		raise _native.NativeToolCacheError("unsafe-cache-path")
	entries: list[_native.Path] = []
	with _native.os.scandir(repository_tool_root) as iterator:
		for item in iterator:
			path = _native.Path(item.path)
			try:
				if _native._is_link(path) or item.is_dir(follow_symlinks=False):
					entries.append(path)
			except OSError:
				entries.append(path)
			if len(entries) > _native._MAX_SCANNED_NAMESPACES:
				return (entries, True)
	return (entries, False)


def _try_evict(
	self: _native.NativeToolCache, path: _native.Path, identity: _native.NativeToolCacheIdentity
) -> bool:
	if _native._is_link(path) or not path.is_dir():
		return False
	namespace_id = path.name
	marker, _reason = self._read_marker(path, tool=identity.tool, namespace_id=namespace_id)
	if marker is None:
		return False
	lock_path = self._lock_path(identity, namespace_id=namespace_id)
	self._ensure_directory(lock_path.parent)
	handle = _native.open_locked_file(lock_path, blocking=False)
	if handle is None:
		return False
	try:
		namespace_identity = _native._cache_directory_identity(path)
		marker_identity = _native._namespace_marker_identity(path, namespace_id, identity.tool)
		marker, _reason = self._read_marker(path, tool=identity.tool, namespace_id=namespace_id)
		if marker is None or namespace_identity is None:
			return False
		if (
			_native._cache_directory_identity(path) != namespace_identity
			or _native._namespace_marker_identity(path, namespace_id, identity.tool)
			!= marker_identity
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
		_native.unlock_file(handle)


def _restore_owned_eviction(
	self: _native.NativeToolCache,
	path: _native.Path,
	eviction_path: _native.Path,
	namespace_id: str,
	tool: Literal["ruff", "mypy"],
	marker_identity: tuple[int, int],
) -> None:
	try:
		if _native._namespace_marker_identity(eviction_path, namespace_id, tool) == marker_identity:
			self._restore_eviction_path(path, eviction_path)
	except _native.NativeToolCacheError:
		return


def _eviction_namespace_id(path: _native.Path) -> str | None:
	prefix = ".evict-"
	if not path.name.startswith(prefix):
		return None
	namespace_id, separator, token = path.name[len(prefix) :].partition("-")
	if (
		separator != "-"
		or len(namespace_id) != _native._SHA256_HEX_LENGTH
		or any(character not in "0123456789abcdef" for character in namespace_id)
		or (len(token) != _native._EVICTION_TOKEN_HEX_LENGTH)
		or any(character not in "0123456789abcdef" for character in token)
	):
		return None
	return namespace_id


def _new_eviction_path(path: _native.Path, namespace_id: str) -> _native.Path | None:
	for _attempt in range(8):
		token = _native.os.urandom(16).hex()
		candidate = path.parent / f".evict-{namespace_id}-{token}"
		try:
			candidate.lstat()
		except FileNotFoundError:
			return candidate
	return None


def _quarantine_namespace(
	self: _native.NativeToolCache,
	path: _native.Path,
	namespace_id: str,
	tool: Literal["ruff", "mypy"],
	namespace_identity: tuple[int, int],
	marker_identity: tuple[int, int],
) -> _native.Path | None:
	eviction_path = self._new_eviction_path(path, namespace_id)
	if eviction_path is None:
		return None
	_native.os.rename(path, eviction_path)
	try:
		if (
			_native._cache_directory_identity(eviction_path) != namespace_identity
			or _native._namespace_marker_identity(eviction_path, namespace_id, tool)
			!= marker_identity
		):
			self._restore_eviction_path(path, eviction_path)
			return None
	except OSError:
		self._restore_eviction_path(path, eviction_path)
		return None
	return eviction_path


def _remove_evicted_namespace(
	self: _native.NativeToolCache,
	path: _native.Path,
	namespace_id: str,
	tool: Literal["ruff", "mypy"],
	namespace_identity: tuple[int, int],
	marker_identity: tuple[int, int],
) -> bool:
	try:
		owner = _native._create_eviction_owner(
			path,
			tool=tool,
			namespace_id=namespace_id,
			directory_identity=namespace_identity,
			marker_identity=marker_identity,
		)
		if owner is None or owner[:2] != (namespace_identity, marker_identity):
			return False
		if not _native._remove_eviction_payload(
			path,
			namespace_id=namespace_id,
			tool=tool,
			namespace_identity=namespace_identity,
			marker_identity=marker_identity,
		):
			return False
		path.rmdir()
		_native._remove_eviction_owner(path, tool=tool, namespace_id=namespace_id, expected=owner)
		return True
	except OSError:
		return False
