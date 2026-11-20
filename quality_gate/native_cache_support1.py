"""Native tool cache support."""

from __future__ import annotations

from typing import Literal

from . import native_cache as _native


def _is_link(path: _native.Path) -> bool:
	try:
		metadata = path.lstat()
	except FileNotFoundError:
		return False
	except OSError:
		return True
	return _native.is_link_or_reparse(metadata)


def _absolute_lexical_path(path: _native.Path) -> _native.Path:
	"""Make a path absolute without resolving or collapsing filesystem components."""
	requested = path.expanduser()
	if requested.is_absolute():
		return requested
	if _native.os.name == "nt" and _native.os.path.splitdrive(_native.os.fspath(requested))[0]:
		return requested
	return _native.Path.cwd() / requested


def _directory_identity(path: _native.Path) -> tuple[int, int] | None:
	try:
		metadata = path.lstat()
	except FileNotFoundError:
		return None
	except OSError as error:
		raise _native.NativeToolCacheError("unsafe-source-path") from error
	if _native.is_link_or_reparse(metadata) or not _native.stat.S_ISDIR(metadata.st_mode):
		raise _native.NativeToolCacheError("unsafe-source-path")
	return (metadata.st_dev, metadata.st_ino)


def _require_directory_identity(
	path: _native.Path, expected: tuple[int, int] | None
) -> tuple[int, int] | None:
	actual = _native._directory_identity(path)
	if actual != expected:
		raise _native.NativeToolCacheError("unsafe-source-path")
	return actual


def _cache_directory_identity(path: _native.Path) -> tuple[int, int] | None:
	try:
		metadata = path.lstat()
	except FileNotFoundError:
		return None
	if _native.is_link_or_reparse(metadata) or not _native.stat.S_ISDIR(metadata.st_mode):
		raise _native.NativeToolCacheError("unsafe-cache-path")
	return (metadata.st_dev, metadata.st_ino)


def _namespace_marker_identity(
	path: _native.Path, namespace_id: str, tool: Literal["ruff", "mypy"] | None
) -> tuple[int, int]:
	marker = path / _native._MARKER
	try:
		metadata = marker.lstat()
	except FileNotFoundError as error:
		raise _native.NativeToolCacheError("namespace-marker-missing") from error
	except OSError as error:
		raise _native.NativeToolCacheError("namespace-marker-invalid") from error
	if (
		_native.is_link_or_reparse(metadata)
		or not _native.stat.S_ISREG(metadata.st_mode)
		or metadata.st_nlink != 1
	):
		raise _native.NativeToolCacheError("namespace-marker-foreign")
	marker_data = _native._read_bounded_json(marker)
	if (
		not isinstance(marker_data, dict)
		or marker_data.get("owner") != _native._OWNER
		or marker_data.get("schema") != _native._SCHEMA
		or (marker_data.get("namespace_id") != namespace_id)
		or (marker_data.get("tool") != tool)
		or (not isinstance(marker_data.get("last_used_ns"), int))
		or isinstance(marker_data.get("last_used_ns"), bool)
	):
		raise _native.NativeToolCacheError("namespace-marker-foreign")
	return (metadata.st_dev, metadata.st_ino)


def _read_bounded_json(path: _native.Path) -> object | None:
	try:
		with path.open("rb") as stream:
			payload = stream.read(_native._MAX_MARKER_BYTES + 1)
		if len(payload) > _native._MAX_MARKER_BYTES:
			return None
		return _native.json.loads(payload.decode("utf-8"))
	except (OSError, UnicodeError, _native.json.JSONDecodeError, RecursionError):
		return None


def _json_identity(value: object) -> tuple[int, int] | None:
	if not isinstance(value, list) or len(value) != _native._FILE_IDENTITY_COMPONENTS:
		return None
	first, second = value
	if (
		not isinstance(first, int)
		or isinstance(first, bool)
		or (not isinstance(second, int))
		or isinstance(second, bool)
	):
		return None
	return (first, second)


def _eviction_owner_path(path: _native.Path) -> _native.Path:
	return path.with_name(path.name + _native._EVICTION_OWNER_SUFFIX)


def _read_eviction_owner(
	path: _native.Path, *, tool: str, namespace_id: str
) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]] | None:
	owner_path = _native._eviction_owner_path(path)
	try:
		metadata = owner_path.lstat()
	except OSError:
		return None
	if (
		_native.is_link_or_reparse(metadata)
		or not _native.stat.S_ISREG(metadata.st_mode)
		or metadata.st_nlink != 1
	):
		return None
	owner = _native._read_bounded_json(owner_path)
	if (
		not isinstance(owner, dict)
		or owner.get("owner") != _native._OWNER
		or owner.get("schema") != _native._SCHEMA
		or (owner.get("kind") != _native._EVICTION_OWNER_KIND)
		or (owner.get("tool") != tool)
		or (owner.get("namespace_id") != namespace_id)
	):
		return None
	directory_identity = _native._json_identity(owner.get("directory_identity"))
	marker_identity = _native._json_identity(owner.get("marker_identity"))
	if directory_identity is None or marker_identity is None:
		return None
	return (directory_identity, marker_identity, (metadata.st_dev, metadata.st_ino))


def _create_eviction_owner(
	path: _native.Path,
	*,
	tool: str,
	namespace_id: str,
	directory_identity: tuple[int, int],
	marker_identity: tuple[int, int],
) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]] | None:
	existing = _native._read_eviction_owner(path, tool=tool, namespace_id=namespace_id)
	if existing is not None:
		return existing if existing[:2] == (directory_identity, marker_identity) else None
	owner_path = _native._eviction_owner_path(path)
	payload = _native.json.dumps(
		{
			"owner": _native._OWNER,
			"schema": _native._SCHEMA,
			"kind": _native._EVICTION_OWNER_KIND,
			"tool": tool,
			"namespace_id": namespace_id,
			"directory_identity": directory_identity,
			"marker_identity": marker_identity,
		},
		sort_keys=True,
		separators=(",", ":"),
	).encode("utf-8")
	flags = (
		_native.os.O_WRONLY
		| _native.os.O_CREAT
		| _native.os.O_EXCL
		| getattr(_native.os, "O_BINARY", 0)
	)
	try:
		handle = _native.os.open(owner_path, flags, _native._SOURCE_FILE_MODE)
	except FileExistsError:
		return _native._read_eviction_owner(path, tool=tool, namespace_id=namespace_id)
	with _native.os.fdopen(handle, "wb") as stream:
		stream.write(payload)
		stream.flush()
		_native.os.fsync(stream.fileno())
	created = _native._read_eviction_owner(path, tool=tool, namespace_id=namespace_id)
	if created is None or created[:2] != (directory_identity, marker_identity):
		return None
	return created


def _remove_eviction_owner(
	path: _native.Path,
	*,
	tool: str,
	namespace_id: str,
	expected: tuple[tuple[int, int], tuple[int, int], tuple[int, int]],
) -> bool:
	owner_path = _native._eviction_owner_path(path)
	current = _native._read_eviction_owner(path, tool=tool, namespace_id=namespace_id)
	if current != expected:
		return False
	try:
		metadata = owner_path.lstat()
		if (
			_native.is_link_or_reparse(metadata)
			or not _native.stat.S_ISREG(metadata.st_mode)
			or metadata.st_nlink != 1
			or ((metadata.st_dev, metadata.st_ino) != expected[2])
		):
			return False
		owner_path.unlink()
	except OSError:
		return False
	return True


def _remove_eviction_payload(
	path: _native.Path,
	*,
	namespace_id: str,
	tool: Literal["ruff", "mypy"],
	namespace_identity: tuple[int, int],
	marker_identity: tuple[int, int],
) -> bool:
	for child in tuple(path.iterdir()):
		if child.name == _native._MARKER:
			continue
		if (
			_native._cache_directory_identity(path) != namespace_identity
			or _native._namespace_marker_identity(path, namespace_id, tool) != marker_identity
		):
			return False
		metadata = child.lstat()
		if _native.is_link_or_reparse(metadata) or not _native.stat.S_ISDIR(metadata.st_mode):
			child.unlink()
		else:
			_native.shutil.rmtree(child)
	if (
		_native._cache_directory_identity(path) != namespace_identity
		or _native._namespace_marker_identity(path, namespace_id, tool) != marker_identity
	):
		return False
	marker = path / _native._MARKER
	if any(child.name != _native._MARKER for child in path.iterdir()):
		return False
	marker.unlink()
	return (
		_native._cache_directory_identity(path) == namespace_identity
		and next(path.iterdir(), None) is None
	)


def _stable_source_mtime_ns(content_digest: bytes) -> int:
	"""Give equal source bytes stable metadata for Ruff's timestamp-based entries."""
	return int.from_bytes(content_digest[:8], "big") & (1 << 63) - 1


def _copy_source_file(source: str, destination: str) -> str:
	source_path = _native.Path(source)
	destination_path = _native.Path(destination)
	digest = _native.hashlib.sha256()
	try:
		with (
			source_path.open("rb") as input_stream,
			_native.os.fdopen(
				_native.os.open(
					destination_path,
					_native.os.O_WRONLY | _native.os.O_CREAT | _native.os.O_EXCL,
					_native._SOURCE_FILE_MODE,
				),
				"wb",
			) as output_stream,
		):
			while block := input_stream.read(1024 * 1024):
				digest.update(block)
				output_stream.write(block)
		_native.os.chmod(destination_path, _native._SOURCE_FILE_MODE)
		mtime_ns = _native._stable_source_mtime_ns(digest.digest())
		_native.os.utime(destination_path, ns=(mtime_ns, mtime_ns))
	except OSError as error:
		raise _native.NativeToolCacheError("source-staging-unavailable") from error
	return destination


def _make_writable(path: _native.Path) -> None:
	try:
		children = sorted(path.rglob("*"), key=lambda child: len(child.parts))
		for child in children:
			if _native._is_link(child):
				continue
			_native.os.chmod(
				child, _native.stat.S_IWRITE | _native.stat.S_IREAD | _native.stat.S_IEXEC
			)
		_native.os.chmod(path, _native.stat.S_IWRITE | _native.stat.S_IREAD | _native.stat.S_IEXEC)
	except OSError as error:
		raise _native.NativeToolCacheError("source-cleanup-unavailable") from error


def _make_read_only(path: _native.Path) -> None:
	try:
		children = sorted(path.rglob("*"), key=lambda child: len(child.parts), reverse=True)
		for child in children:
			if _native._is_link(child):
				continue
			mode = (
				_native._SOURCE_READ_ONLY_DIRECTORY_MODE
				if child.is_dir()
				else _native._SOURCE_READ_ONLY_FILE_MODE
			)
			_native.os.chmod(child, mode)
		_native.os.chmod(path, _native._SOURCE_READ_ONLY_DIRECTORY_MODE)
	except OSError as error:
		raise _native.NativeToolCacheError("source-staging-unavailable") from error


def _copy_directory_contents(source_root: _native.Path, destination_root: _native.Path) -> None:
	try:
		with _native.os.scandir(source_root) as iterator:
			entries = sorted(iterator, key=lambda entry: entry.name)
	except OSError as error:
		raise _native.NativeToolCacheError("source-staging-unavailable") from error
	for entry in entries:
		source = _native.Path(entry.path)
		destination = destination_root / entry.name
		try:
			if source.is_symlink():
				_native.os.symlink(
					_native.os.readlink(source), destination, target_is_directory=source.is_dir()
				)
			elif _native._is_link(source):
				raise _native.NativeToolCacheError("unsafe-source-path")
			elif source.is_dir():
				destination.mkdir(mode=_native._SOURCE_DIRECTORY_MODE)
				_native.os.chmod(destination, _native._SOURCE_DIRECTORY_MODE)
				_native._copy_directory_contents(source, destination)
				_native.os.utime(
					destination,
					ns=(_native._SOURCE_DIRECTORY_MTIME_NS, _native._SOURCE_DIRECTORY_MTIME_NS),
				)
			elif source.is_file():
				_native._copy_source_file(str(source), str(destination))
			else:
				raise _native.NativeToolCacheError("source-staging-unavailable")
		except _native.NativeToolCacheError:
			raise
		except (OSError, RuntimeError, ValueError) as error:
			raise _native.NativeToolCacheError("source-staging-unavailable") from error


def _copy_candidate(source_root: _native.Path, destination_root: _native.Path) -> None:
	try:
		resolved_source = source_root.resolve(strict=True)
		if not resolved_source.is_dir() or _native._is_link(source_root):
			raise _native.NativeToolCacheError("unsafe-source-path")
		if _native._directory_identity(destination_root) is None:
			raise _native.NativeToolCacheError("source-staging-unavailable")
		_native._copy_directory_contents(resolved_source, destination_root)
		_native.os.utime(
			destination_root,
			ns=(_native._SOURCE_DIRECTORY_MTIME_NS, _native._SOURCE_DIRECTORY_MTIME_NS),
		)
	except _native.NativeToolCacheError:
		raise
	except (OSError, RuntimeError, ValueError) as error:
		raise _native.NativeToolCacheError("source-staging-unavailable") from error


def _source_owner_marker_is_valid(marker: _native.Path) -> bool:
	if _native._is_link(marker) or not marker.is_dir():
		return False
	try:
		return next(marker.iterdir(), None) is None
	except OSError:
		return False
