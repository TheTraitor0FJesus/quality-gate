"""Native cache namespace and eviction methods."""

from __future__ import annotations

from . import native_cache as _native


def _acquire(
	self: _native.NativeToolCache,
	identity: _native.NativeToolCacheIdentity,
	*,
	operation: _native.NativeToolCacheOperation | None = None,
) -> tuple[_native.Path, _native.BinaryIO, tuple[int, int], tuple[int, int]]:
	if not self.root.is_absolute():
		raise _native.NativeToolCacheError("unsafe-cache-path")
	cache_root = self._cache_root()
	self._ensure_directory(cache_root)
	repository_tool_root = self._repository_tool_root(identity)
	self._ensure_directory(repository_tool_root)
	maintenance_lock = cache_root / _native._MAINTENANCE_LOCK
	try:
		with _native.locked_file(
			maintenance_lock,
			timeout_seconds=self.maintenance_timeout_seconds
			if operation is None
			else operation.remaining(self.maintenance_timeout_seconds),
		):
			path = self.namespace_path(identity)
			if _native._is_link(path):
				raise _native.NativeToolCacheError("unsafe-cache-path")
			exists = path.exists()
			if not self._prune_for_lease(
				identity,
				exclude_namespace=identity.namespace_id if exists else None,
				needs_slot=not exists,
				operation=operation,
			):
				raise _native.NativeToolCacheError("capacity-limit")
			lease_path = self._lock_path(identity)
			self._ensure_directory(lease_path.parent)
			handle = self._open_lease_lock(lease_path, operation=operation)
			try:
				namespace_identity, marker_identity = self._prepare_namespace_for_lease(
					path, identity, operation
				)
				return (path, handle, namespace_identity, marker_identity)
			except BaseException:
				try:
					_native.unlock_file(handle)
				except OSError:
					pass
				raise
	except _native.FileLockUnavailableError as error:
		raise _native.NativeToolCacheError("lock-busy") from error


def _open_lease_lock(
	self: _native.NativeToolCache,
	path: _native.Path,
	*,
	operation: _native.NativeToolCacheOperation | None = None,
) -> _native.BinaryIO:
	deadline = _native.time.monotonic() + self.lease_timeout_seconds
	while True:
		if operation is not None:
			operation.remaining()
		handle = _native.open_locked_file(path, blocking=False)
		if handle is not None:
			return handle
		remaining = deadline - _native.time.monotonic()
		if operation is not None:
			remaining = operation.remaining(remaining)
		if remaining <= 0:
			raise _native.NativeToolCacheError("lock-busy")
		_native.time.sleep(min(0.025, remaining))
