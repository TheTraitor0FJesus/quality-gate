"""Native cache namespace and eviction methods."""

from __future__ import annotations

from typing import Literal

from . import native_cache as _native


def _recover_empty_eviction(
	self: _native.NativeToolCache,
	path: _native.Path,
	namespace_id: str,
	tool: Literal["ruff", "mypy"],
) -> bool:
	owner = _native._read_eviction_owner(path, tool=tool, namespace_id=namespace_id)
	if owner is None:
		return False
	try:
		if _native._cache_directory_identity(path) != owner[0]:
			return False
		try:
			(path / _native._MARKER).lstat()
		except FileNotFoundError:
			pass
		else:
			return False
		if next(path.iterdir(), None) is not None:
			return False
		if _native._read_eviction_owner(path, tool=tool, namespace_id=namespace_id) != owner:
			return False
		if _native._cache_directory_identity(path) != owner[0]:
			return False
		path.rmdir()
	except OSError:
		return False
	_native._remove_eviction_owner(path, tool=tool, namespace_id=namespace_id, expected=owner)
	return True


def _recover_orphaned_eviction_owners(
	self: _native.NativeToolCache,
	repository_tool_root: _native.Path,
	tool: Literal["ruff", "mypy"],
) -> None:
	try:
		with _native.os.scandir(repository_tool_root) as iterator:
			owner_paths = tuple(
				_native.Path(item.path)
				for item in iterator
				if item.name.startswith(".evict-")
				and item.name.endswith(_native._EVICTION_OWNER_SUFFIX)
			)
	except OSError:
		return
	for owner_path in owner_paths:
		eviction_path = owner_path.with_name(
			owner_path.name[: -len(_native._EVICTION_OWNER_SUFFIX)]
		)
		namespace_id = self._eviction_namespace_id(eviction_path)
		if namespace_id is None:
			continue
		try:
			eviction_path.lstat()
		except FileNotFoundError:
			owner = _native._read_eviction_owner(
				eviction_path, tool=tool, namespace_id=namespace_id
			)
			if owner is not None:
				_native._remove_eviction_owner(
					eviction_path, tool=tool, namespace_id=namespace_id, expected=owner
				)
		except OSError:
			continue


def _recover_evictions(
	self: _native.NativeToolCache, identity: _native.NativeToolCacheIdentity
) -> None:
	repository_tool_root = self._repository_tool_root(identity)
	entries, _overflow = self._entries(repository_tool_root)
	for eviction_path in entries:
		namespace_id = self._eviction_namespace_id(eviction_path)
		if namespace_id is None or _native._is_link(eviction_path) or (not eviction_path.is_dir()):
			continue
		self._recover_eviction_path(eviction_path, namespace_id, identity)
	self._recover_orphaned_eviction_owners(repository_tool_root, identity.tool)


def _recover_eviction_path(
	self: _native.NativeToolCache,
	eviction_path: _native.Path,
	namespace_id: str,
	identity: _native.NativeToolCacheIdentity,
) -> None:
	lease_path = self._lock_path(identity, namespace_id=namespace_id)
	try:
		self._ensure_directory(lease_path.parent)
		handle = _native.open_locked_file(lease_path, blocking=False)
	except OSError:
		return
	if handle is None:
		return
	try:
		self._recover_eviction_under_lock(eviction_path, namespace_id, identity)
	finally:
		_native.unlock_file(handle)


def _recover_eviction_under_lock(
	self: _native.NativeToolCache,
	eviction_path: _native.Path,
	namespace_id: str,
	identity: _native.NativeToolCacheIdentity,
) -> None:
	namespace_identity = _native._cache_directory_identity(eviction_path)
	if namespace_identity is None:
		return
	marker, reason = self._read_marker(eviction_path, tool=identity.tool, namespace_id=namespace_id)
	if marker is None:
		if reason == "namespace-marker-missing":
			self._recover_empty_eviction(eviction_path, namespace_id, identity.tool)
		return
	try:
		marker_identity = _native._namespace_marker_identity(
			eviction_path, namespace_id, identity.tool
		)
	except _native.NativeToolCacheError:
		return
	if (
		_native._cache_directory_identity(eviction_path) != namespace_identity
		or _native._namespace_marker_identity(eviction_path, namespace_id, identity.tool)
		!= marker_identity
	):
		return
	quarantined_path = self._quarantine_namespace(
		eviction_path, namespace_id, identity.tool, namespace_identity, marker_identity
	)
	if quarantined_path is None or self._remove_evicted_namespace(
		quarantined_path, namespace_id, identity.tool, namespace_identity, marker_identity
	):
		return
	self._restore_owned_eviction(
		eviction_path, quarantined_path, namespace_id, identity.tool, marker_identity
	)


def _restore_eviction_path(path: _native.Path, eviction_path: _native.Path) -> None:
	try:
		path.lstat()
	except FileNotFoundError:
		pass
	except OSError:
		return
	else:
		return
	try:
		_native.os.rename(eviction_path, path)
	except OSError:
		pass


def _last_used_ns(
	self: _native.NativeToolCache, entry: _native.Path, identity: _native.NativeToolCacheIdentity
) -> int | None:
	if _native._is_link(entry) or not entry.is_dir():
		return None
	marker, _reason = self._read_marker(entry, tool=identity.tool, namespace_id=entry.name)
	if marker is None:
		return None
	last_used = marker["last_used_ns"]
	if not isinstance(last_used, int) or isinstance(last_used, bool):
		return None
	return last_used


def _prune(
	self: _native.NativeToolCache,
	identity: _native.NativeToolCacheIdentity,
	*,
	exclude_namespace: str | None,
	needs_slot: bool,
) -> bool:
	repository_tool_root = self._repository_tool_root(identity)
	self._recover_evictions(identity)
	entries, _truncated = self._entries(repository_tool_root)
	if exclude_namespace is not None:
		entries = [entry for entry in entries if entry.name != exclude_namespace]
	now_ns = _native.time.time_ns()
	stale_before_ns = now_ns - _native._RETENTION_SECONDS * 1000000000
	candidates: list[tuple[int, _native.Path]] = []
	for entry in entries:
		last_used = self._last_used_ns(entry, identity)
		if last_used is not None:
			candidates.append((last_used, entry))
	candidates.sort(key=lambda item: (item[0], item[1].name))
	evictions = 0
	for last_used, entry in candidates:
		capacity_requires_eviction = (
			needs_slot and len(entries) >= _native._MAX_NAMESPACES_PER_REPOSITORY_TOOL
		)
		if last_used > stale_before_ns and (not capacity_requires_eviction):
			continue
		if evictions >= _native._MAX_EVICTIONS_PER_LEASE:
			break
		if self._try_evict(entry, identity):
			evictions += 1
			entries.remove(entry)
	if not needs_slot:
		return True
	remaining, overflow = self._entries(repository_tool_root)
	return not overflow and len(remaining) < _native._MAX_NAMESPACES_PER_REPOSITORY_TOOL


def _prepare_namespace(
	self: _native.NativeToolCache, path: _native.Path, identity: _native.NativeToolCacheIdentity
) -> tuple[tuple[int, int], tuple[int, int]]:
	if path.exists():
		if _native._is_link(path) or not path.is_dir():
			raise _native.NativeToolCacheError("unsafe-cache-path")
		namespace_identity = _native._cache_directory_identity(path)
		if namespace_identity is None:
			raise _native.NativeToolCacheError("unsafe-cache-path")
		marker, reason = self._read_marker(
			path, tool=identity.tool, namespace_id=identity.namespace_id
		)
		if marker is None:
			markerless_empty = reason == "namespace-marker-missing" and (
				not tuple(path.iterdir())
				or self._remove_incomplete_marker_temporary(path, namespace_identity)
			)
			if not markerless_empty:
				raise _native.NativeToolCacheError(reason or "namespace-marker-invalid")
	else:
		path.mkdir()
		namespace_identity = _native._cache_directory_identity(path)
		if namespace_identity is None:
			raise _native.NativeToolCacheError("unsafe-cache-path")
	self._write_marker(path, identity)
	if _native._cache_directory_identity(path) != namespace_identity:
		raise _native.NativeToolCacheError("unsafe-cache-path")
	marker_identity = _native._namespace_marker_identity(path, identity.namespace_id, identity.tool)
	return (namespace_identity, marker_identity)


def _prune_for_lease(
	self: _native.NativeToolCache,
	identity: _native.NativeToolCacheIdentity,
	*,
	exclude_namespace: str | None,
	needs_slot: bool,
	operation: _native.NativeToolCacheOperation | None,
) -> bool:
	if operation is None:
		return self._prune(identity, exclude_namespace=exclude_namespace, needs_slot=needs_slot)
	try:
		result = _native._run_native_cache_action(
			operation,
			"prune",
			{
				"schema": 1,
				"cache_root": str(self.root),
				"identity": _native.asdict(identity),
				"exclude_namespace": exclude_namespace,
				"needs_slot": needs_slot,
			},
			f"tool={identity.tool}; namespace={identity.namespace_id}",
		)
	except (_native.EditError, OSError) as error:
		raise _native.NativeToolCacheError("maintenance-unavailable") from error
	if result.get("status") != "ready" or not isinstance(result.get("space_available"), bool):
		raise _native.NativeToolCacheError("maintenance-unavailable")
	return result["space_available"] is True


def _prepare_namespace_for_lease(
	self: _native.NativeToolCache,
	path: _native.Path,
	identity: _native.NativeToolCacheIdentity,
	operation: _native.NativeToolCacheOperation | None,
) -> tuple[tuple[int, int], tuple[int, int]]:
	if operation is None:
		return self._prepare_namespace(path, identity)
	try:
		result = _native._run_native_cache_action(
			operation,
			"prepare-namespace",
			{"schema": 1, "cache_root": str(self.root), "identity": _native.asdict(identity)},
			f"tool={identity.tool}; namespace={identity.namespace_id}",
		)
		if result.get("status") != "ready":
			raise _native.NativeToolCacheError("namespace-unavailable")
		return (
			_native._pair_from_payload(result.get("namespace")),
			_native._pair_from_payload(result.get("marker")),
		)
	except (_native.EditError, OSError, ValueError) as error:
		raise _native.NativeToolCacheError("namespace-unavailable") from error
