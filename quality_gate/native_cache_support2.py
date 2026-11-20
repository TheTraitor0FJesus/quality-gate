"""Native tool cache support."""

from __future__ import annotations

from . import native_cache as _native


def _validate_source_workspace_ownership(
	selection: _native.NativeToolCacheSelection,
	stage_parent: _native.Path,
	stable_root: _native.Path,
	owner_marker: _native.Path,
	parent_owner_marker: _native.Path,
	*,
	expected: _native._SourceWorkspaceIdentity | None = None,
) -> _native._SourceWorkspaceIdentity:
	namespace_identity = _native._directory_identity(selection.path)
	namespace_marker_identity = _native._namespace_marker_identity(
		selection.path, selection.namespace_id, selection._tool
	)
	stage_parent_identity = _native._directory_identity(stage_parent)
	if (
		namespace_identity is None
		or namespace_identity != selection._path_identity
		or namespace_marker_identity != selection._marker_identity
		or (stage_parent_identity is None)
	):
		raise _native.NativeToolCacheError("unsafe-source-path")
	try:
		entries = tuple(stage_parent.iterdir())
	except OSError as error:
		raise _native.NativeToolCacheError("unsafe-source-path") from error
	if any(
		entry.name
		not in {
			_native._SOURCE_ROOT,
			_native._SOURCE_OWNER_DIRECTORY,
			_native._SOURCE_PARENT_OWNER_DIRECTORY,
		}
		for entry in entries
	):
		raise _native.NativeToolCacheError("unsafe-source-path")
	if not _native._source_owner_marker_is_valid(parent_owner_marker):
		raise _native.NativeToolCacheError("unsafe-source-path")
	parent_owner_identity = _native._directory_identity(parent_owner_marker)
	owner_identity = _native._directory_identity(owner_marker)
	stable_identity = _native._directory_identity(stable_root)
	if parent_owner_identity is None:
		raise _native.NativeToolCacheError("unsafe-source-path")
	if owner_identity is not None:
		if not _native._source_owner_marker_is_valid(owner_marker):
			raise _native.NativeToolCacheError("unsafe-source-path")
	elif stable_identity is not None:
		raise _native.NativeToolCacheError("unsafe-source-path")
	if expected is not None and (
		namespace_identity != expected.namespace
		or namespace_marker_identity != expected.namespace_marker
		or stage_parent_identity != expected.stage_parent
		or (parent_owner_identity != expected.parent_owner_marker)
		or (owner_identity != expected.owner_marker)
		or (stable_identity != expected.stable_root)
	):
		raise _native.NativeToolCacheError("unsafe-source-path")
	return _native._SourceWorkspaceIdentity(
		namespace_identity,
		namespace_marker_identity,
		stage_parent_identity,
		parent_owner_identity,
		owner_identity,
		stable_identity,
	)


def _remove_owned_source_workspace(
	selection: _native.NativeToolCacheSelection,
	stage_parent: _native.Path,
	stable_root: _native.Path,
	owner_marker: _native.Path,
	parent_owner_marker: _native.Path,
	*,
	expected: _native._SourceWorkspaceIdentity | None = None,
) -> None:
	identity = _native._validate_source_workspace_ownership(
		selection, stage_parent, stable_root, owner_marker, parent_owner_marker, expected=expected
	)
	if identity.owner_marker is not None:
		if identity.stable_root is not None:
			_native._make_writable(stable_root)
			_native._validate_source_workspace_ownership(
				selection,
				stage_parent,
				stable_root,
				owner_marker,
				parent_owner_marker,
				expected=identity,
			)
			_native._require_directory_identity(stable_root, identity.stable_root)
			try:
				_native.shutil.rmtree(stable_root)
			except OSError as error:
				raise _native.NativeToolCacheError("source-cleanup-unavailable") from error
		identity = _native.replace(identity, stable_root=None)
		_native._validate_source_workspace_ownership(
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
			raise _native.NativeToolCacheError("source-cleanup-unavailable") from error


def _remove_new_empty_source_parent(
	selection: _native.NativeToolCacheSelection,
	stage_parent: _native.Path,
	expected_stage_parent: tuple[int, int],
	parent_owner_marker: _native.Path,
	expected_parent_owner: tuple[int, int] | None,
) -> None:
	try:
		if (
			_native._directory_identity(selection.path) != selection._path_identity
			or _native._namespace_marker_identity(
				selection.path, selection.namespace_id, selection._tool
			)
			!= selection._marker_identity
			or _native._directory_identity(stage_parent) != expected_stage_parent
		):
			return
		if expected_parent_owner is not None:
			if (
				_native._directory_identity(parent_owner_marker) != expected_parent_owner
				or next(parent_owner_marker.iterdir(), None) is not None
			):
				return
			parent_owner_marker.rmdir()
		if (
			_native._directory_identity(selection.path) != selection._path_identity
			or _native._namespace_marker_identity(
				selection.path, selection.namespace_id, selection._tool
			)
			!= selection._marker_identity
			or _native._directory_identity(stage_parent) != expected_stage_parent
			or (next(stage_parent.iterdir(), None) is not None)
		):
			return
		stage_parent.rmdir()
	except OSError:
		pass


def _selected_source_namespace_identity(
	selection: _native.NativeToolCacheSelection,
) -> tuple[int, int]:
	namespace_identity = _native._directory_identity(selection.path)
	try:
		marker_identity = _native._namespace_marker_identity(
			selection.path, selection.namespace_id, selection._tool
		)
	except _native.NativeToolCacheError as error:
		raise _native.NativeToolCacheError("unsafe-source-path") from error
	if (
		namespace_identity is None
		or namespace_identity != selection._path_identity
		or marker_identity != selection._marker_identity
	):
		raise _native.NativeToolCacheError("unsafe-source-path")
	return namespace_identity


def _prepare_source_parent(
	stage_parent: _native.Path,
	parent_owner_marker: _native.Path,
	setup: _native._SourceWorkspaceSetup,
) -> None:
	if _native._is_link(stage_parent):
		raise _native.NativeToolCacheError("unsafe-source-path")
	setup.stage_parent = _native._directory_identity(stage_parent)
	if setup.stage_parent is None:
		stage_parent.mkdir(mode=_native._SOURCE_DIRECTORY_MODE)
		setup.stage_parent_created = True
		setup.stage_parent = _native._directory_identity(stage_parent)
		if setup.stage_parent is None:
			raise _native.NativeToolCacheError("unsafe-source-path")
	else:
		try:
			parent_owner_marker.lstat()
		except FileNotFoundError:
			try:
				stage_entries = tuple(stage_parent.iterdir())
			except OSError as error:
				raise _native.NativeToolCacheError("unsafe-source-path") from error
			if (
				not stage_entries
				and _native._directory_identity(stage_parent) == setup.stage_parent
			):
				setup.stage_parent_created = True
	if not setup.stage_parent_created:
		return
	_native.os.chmod(stage_parent, _native._SOURCE_DIRECTORY_MODE)
	parent_owner_marker.mkdir(mode=_native._SOURCE_DIRECTORY_MODE)
	setup.parent_owner = _native._directory_identity(parent_owner_marker)
	if setup.parent_owner is None:
		raise _native.NativeToolCacheError("unsafe-source-path")
	_native.os.chmod(parent_owner_marker, _native._SOURCE_DIRECTORY_MODE)


def _create_source_workspace(
	selection: _native.NativeToolCacheSelection,
	snapshot_root: _native.Path,
	stage_parent: _native.Path,
	stable_root: _native.Path,
	owner_marker: _native.Path,
	parent_owner_marker: _native.Path,
	setup: _native._SourceWorkspaceSetup,
) -> _native._SourceWorkspaceIdentity:
	ownership = _native._validate_source_workspace_ownership(
		selection, stage_parent, stable_root, owner_marker, parent_owner_marker
	)
	if ownership.namespace != setup.namespace:
		raise _native.NativeToolCacheError("unsafe-source-path")
	_native.os.chmod(stage_parent, _native._SOURCE_DIRECTORY_MODE)
	_native._remove_owned_source_workspace(
		selection, stage_parent, stable_root, owner_marker, parent_owner_marker, expected=ownership
	)
	owner_marker.mkdir(mode=_native._SOURCE_DIRECTORY_MODE)
	setup.owner_marker_created = True
	_native.os.chmod(owner_marker, _native._SOURCE_DIRECTORY_MODE)
	ownership = _native._validate_source_workspace_ownership(
		selection, stage_parent, stable_root, owner_marker, parent_owner_marker
	)
	if ownership.namespace != setup.namespace:
		raise _native.NativeToolCacheError("unsafe-source-path")
	stable_root.mkdir(mode=_native._SOURCE_DIRECTORY_MODE)
	_native.os.chmod(stable_root, _native._SOURCE_DIRECTORY_MODE)
	ownership = _native.replace(ownership, stable_root=_native._directory_identity(stable_root))
	_native._copy_candidate(snapshot_root, stable_root)
	_native._make_read_only(stable_root)
	completed = _native._validate_source_workspace_ownership(
		selection, stage_parent, stable_root, owner_marker, parent_owner_marker
	)
	if _native.replace(ownership, stable_root=completed.stable_root) != completed:
		raise _native.NativeToolCacheError("unsafe-source-path")
	return completed


def _rollback_source_setup(
	selection: _native.NativeToolCacheSelection,
	stage_parent: _native.Path,
	stable_root: _native.Path,
	owner_marker: _native.Path,
	parent_owner_marker: _native.Path,
	setup: _native._SourceWorkspaceSetup,
) -> None:
	if setup.owner_marker_created:
		try:
			ownership = _native._validate_source_workspace_ownership(
				selection, stage_parent, stable_root, owner_marker, parent_owner_marker
			)
			_native._remove_owned_source_workspace(
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
		_native._remove_new_empty_source_parent(
			selection, stage_parent, setup.stage_parent, parent_owner_marker, setup.parent_owner
		)


def _prepare_source_workspace(
	selection: _native.NativeToolCacheSelection,
	snapshot_root: _native.Path,
	stage_parent: _native.Path,
	stable_root: _native.Path,
	owner_marker: _native.Path,
	parent_owner_marker: _native.Path,
) -> _native._SourceWorkspaceIdentity:
	if _native.os.name == "nt" and len(_native.os.fspath(stable_root)) >= _native._WINDOWS_MAX_PATH:
		raise _native.NativeToolCacheError("source-staging-unavailable")
	setup = _native._SourceWorkspaceSetup(_native._selected_source_namespace_identity(selection))
	try:
		_native._prepare_source_parent(stage_parent, parent_owner_marker, setup)
		return _native._create_source_workspace(
			selection,
			snapshot_root,
			stage_parent,
			stable_root,
			owner_marker,
			parent_owner_marker,
			setup,
		)
	except OSError as error:
		_native._rollback_source_setup(
			selection, stage_parent, stable_root, owner_marker, parent_owner_marker, setup
		)
		if isinstance(error, _native.NativeToolCacheError):
			raise
		raise _native.NativeToolCacheError("source-staging-unavailable") from error


def _pair_from_payload(value: object) -> tuple[int, int]:
	if (
		isinstance(value, (list, tuple))
		and len(value) == _native._FILE_IDENTITY_COMPONENTS
		and all(isinstance(part, int) and (not isinstance(part, bool)) for part in value)
	):
		return (int(value[0]), int(value[1]))
	raise _native.NativeToolCacheError("unsafe-source-path")


def _optional_pair_from_payload(value: object) -> tuple[int, int] | None:
	return None if value is None else _native._pair_from_payload(value)


def _identity_payload(identity: _native._SourceWorkspaceIdentity) -> dict[str, object]:
	return _native.asdict(identity)


def _identity_from_payload(value: object) -> _native._SourceWorkspaceIdentity:
	if not isinstance(value, dict) or set(value) != {
		"namespace",
		"namespace_marker",
		"stage_parent",
		"parent_owner_marker",
		"owner_marker",
		"stable_root",
	}:
		raise _native.NativeToolCacheError("unsafe-source-path")
	return _native._SourceWorkspaceIdentity(
		_native._pair_from_payload(value["namespace"]),
		_native._pair_from_payload(value["namespace_marker"]),
		_native._pair_from_payload(value["stage_parent"]),
		_native._optional_pair_from_payload(value["parent_owner_marker"]),
		_native._optional_pair_from_payload(value["owner_marker"]),
		_native._optional_pair_from_payload(value["stable_root"]),
	)


def _selection_payload(selection: _native.NativeToolCacheSelection) -> dict[str, object]:
	if (
		selection.storage != "persistent"
		or selection._tool not in {"ruff", "mypy"}
		or selection._path_identity is None
		or (selection._marker_identity is None)
		or (not selection.path.is_absolute())
		or (len(selection.namespace_id) != _native._SHA256_HEX_LENGTH)
		or any(character not in "0123456789abcdef" for character in selection.namespace_id)
	):
		raise _native.NativeToolCacheError("unsafe-source-path")
	return {
		"namespace_id": selection.namespace_id,
		"path": str(selection.path),
		"tool": selection._tool,
		"path_identity": list(selection._path_identity),
		"marker_identity": list(selection._marker_identity),
	}
