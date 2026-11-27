"""Native tool cache support."""

from __future__ import annotations

from typing import Literal

from . import native_cache as _native


def _selection_from_payload(value: object) -> _native.NativeToolCacheSelection:
	if not isinstance(value, dict) or set(value) != {
		"namespace_id",
		"path",
		"tool",
		"path_identity",
		"marker_identity",
	}:
		raise _native.NativeToolCacheError("unsafe-source-path")
	path = value["path"]
	tool = value["tool"]
	namespace_id = value["namespace_id"]
	if (
		not isinstance(path, str)
		or not isinstance(tool, str)
		or tool not in {"ruff", "mypy"}
		or (not isinstance(namespace_id, str))
		or (len(namespace_id) != _native._SHA256_HEX_LENGTH)
		or any(character not in "0123456789abcdef" for character in namespace_id)
	):
		raise _native.NativeToolCacheError("unsafe-source-path")
	selected_path = _native.Path(path)
	if not selected_path.is_absolute() or selected_path.name != namespace_id:
		raise _native.NativeToolCacheError("unsafe-source-path")
	path_identity = _native._pair_from_payload(value["path_identity"])
	marker_identity = _native._pair_from_payload(value["marker_identity"])
	return _native.NativeToolCacheSelection(
		namespace_id,
		selected_path,
		"persistent",
		None,
		0.0,
		_tool=_native.cast(Literal["ruff", "mypy"], tool),
		_path_identity=path_identity,
		_marker_identity=marker_identity,
	)


def _source_paths(
	selection: _native.NativeToolCacheSelection,
) -> tuple[_native.Path, _native.Path, _native.Path, _native.Path]:
	stage_parent = selection.path / _native._SOURCE_DIRECTORY
	stable_root = stage_parent / _native._SOURCE_ROOT
	owner_marker = stage_parent / _native._SOURCE_OWNER_DIRECTORY
	parent_owner_marker = stage_parent / _native._SOURCE_PARENT_OWNER_DIRECTORY
	return (stage_parent, stable_root, owner_marker, parent_owner_marker)


def _source_cleanup_payload(
	selection: _native.NativeToolCacheSelection,
	*,
	expected: _native._SourceWorkspaceIdentity | None,
) -> bytes:
	payload = {
		"schema": 1,
		"selection": _native._selection_payload(selection),
		"expected": None if expected is None else _native._identity_payload(expected),
	}
	return _native.json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _capture_source_cleanup_payload(selection: _native.NativeToolCacheSelection) -> bytes | None:
	stage_parent, stable_root, owner_marker, parent_owner_marker = _native._source_paths(selection)
	if not stage_parent.exists() and (not stage_parent.is_symlink()):
		return None
	try:
		identity = _native._validate_source_workspace_ownership(
			selection, stage_parent, stable_root, owner_marker, parent_owner_marker
		)
	except _native.NativeToolCacheError:
		if _native._is_link(stage_parent):
			return None
		stage_identity = _native._directory_identity(stage_parent)
		if stage_identity is None:
			return None
		try:
			entries = tuple(stage_parent.iterdir())
			namespace_identity = _native._selected_source_namespace_identity(selection)
			parent_identity = _native._directory_identity(parent_owner_marker)
			if parent_identity is not None and (
				not _native._source_owner_marker_is_valid(parent_owner_marker)
			):
				return None
			marker_identity = selection._marker_identity
			if marker_identity is None:
				return None
			if any(entry.name != _native._SOURCE_PARENT_OWNER_DIRECTORY for entry in entries):
				return None
		except (OSError, _native.NativeToolCacheError):
			return None
		identity = _native._SourceWorkspaceIdentity(
			namespace_identity, marker_identity, stage_identity, parent_identity, None, None
		)
	return _native._source_cleanup_payload(selection, expected=identity)


def _register_partial_source_cleanup(
	selection: _native.NativeToolCacheSelection, operation: _native.NativeToolCacheOperation
) -> None:
	cleanup = _native._capture_source_cleanup_payload(selection)
	if cleanup is not None:
		operation.register_native_cache_cleanup(cleanup)


def _run_native_cache_action(
	operation: _native.NativeToolCacheOperation,
	action: Literal["prepare-source", "prune", "prepare-namespace"],
	request: dict[str, object],
	scope: str,
) -> dict[str, object]:
	payload = _native.json.dumps(
		{"action": action, "request": request}, sort_keys=True, separators=(",", ":")
	).encode("utf-8")
	if len(payload) > _native._MAX_NATIVE_CACHE_ACTION_BYTES:
		raise _native.NativeToolCacheError("cache-action-input-too-large")
	command = [
		_native.sys.executable,
		"-I",
		"-S",
		"-B",
		"-c",
		_native._NATIVE_CACHE_BODY,
		str(_native._MAX_NATIVE_CACHE_ACTION_BYTES),
		str(_native.Path(__file__).resolve().parent.parent),
	]
	with operation.stage(f"native-cache.{action}", scope, process_required=True):
		code, output, _diagnostics = operation.run_process(
			command,
			root=operation.root,
			environment=dict(_native.os.environ),
			timeout_seconds=operation.command_timeout_seconds,
			payload=payload,
			limit=_native._MAX_NATIVE_CACHE_ACTION_BYTES,
			check_execution=False,
		)
	if code != 0:
		raise _native.NativeToolCacheError("cache-action-unavailable")
	try:
		result = _native.json.loads(output.decode("utf-8"))
	except (UnicodeDecodeError, _native.json.JSONDecodeError) as error:
		raise _native.NativeToolCacheError("cache-action-unavailable") from error
	if not isinstance(result, dict):
		raise _native.NativeToolCacheError("cache-action-unavailable")
	return result


def _prepare_source_workspace_with_operation(
	selection: _native.NativeToolCacheSelection,
	snapshot_root: _native.Path,
	operation: _native.NativeToolCacheOperation,
) -> tuple[_native._SourceWorkspaceIdentity | None, str | None]:
	try:
		result = _native._run_native_cache_action(
			operation,
			"prepare-source",
			{
				"schema": 1,
				"selection": _native._selection_payload(selection),
				"snapshot_root": str(snapshot_root),
			},
			f"namespace={selection.namespace_id}",
		)
	except BaseException as error:
		_native._register_partial_source_cleanup(selection, operation)
		if isinstance(error, (_native.EditError, OSError)):
			raise _native.NativeToolCacheError("source-staging-unavailable") from error
		raise
	if result.get("status") == "fallback":
		_native._register_partial_source_cleanup(selection, operation)
		reason = result.get("reason")
		return (
			None,
			reason if isinstance(reason, str) and reason else "source-staging-unavailable",
		)
	if result.get("status") != "ready":
		_native._register_partial_source_cleanup(selection, operation)
		return (None, "source-staging-unavailable")
	try:
		expected = _native._identity_from_payload(result.get("expected"))
		operation.register_native_cache_cleanup(
			_native._source_cleanup_payload(selection, expected=expected)
		)
	except (_native.NativeToolCacheError, ValueError):
		_native._register_partial_source_cleanup(selection, operation)
		return (None, "source-staging-unavailable")
	return (expected, None)


def _cache_identity_from_request(value: object) -> _native.NativeToolCacheIdentity:
	if not isinstance(value, dict) or set(value) != {
		item.name for item in _native.fields(_native.NativeToolCacheIdentity)
	}:
		raise _native.NativeToolCacheError("unsafe-cache-action")
	decoded = dict(value)
	sequence_fields = {"effective_options", "import_roots"}
	engine = decoded["engine"]
	if (
		not isinstance(engine, dict)
		or set(engine) != {"form", "version", "digest"}
		or not all(isinstance(item, str) for item in engine.values())
	):
		raise _native.NativeToolCacheError("unsafe-cache-action")
	try:
		decoded["engine"] = _native.EngineIdentity(**engine)
	except (TypeError, ValueError) as error:
		raise _native.NativeToolCacheError("unsafe-cache-action") from error
	for name in sequence_fields:
		items = decoded[name]
		if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
			raise _native.NativeToolCacheError("unsafe-cache-action")
		decoded[name] = tuple(items)
	if any(
		(
			not isinstance(item, str)
			for name, item in decoded.items()
			if name not in sequence_fields and name != "engine"
		)
	):
		raise _native.NativeToolCacheError("unsafe-cache-action")
	if decoded["tool"] not in {"ruff", "mypy"}:
		raise _native.NativeToolCacheError("unsafe-cache-action")
	return _native.NativeToolCacheIdentity(**_native.cast(dict[str, _native.Any], decoded))


def _prune_native_cache_request(value: object) -> dict[str, object]:
	if (
		not isinstance(value, dict)
		or set(value) != {"schema", "cache_root", "identity", "exclude_namespace", "needs_slot"}
		or value["schema"] != 1
		or (not isinstance(value["cache_root"], str))
		or (not _native.Path(value["cache_root"]).is_absolute())
		or (not isinstance(value["needs_slot"], bool))
	):
		raise _native.NativeToolCacheError("unsafe-cache-action")
	identity = _native._cache_identity_from_request(value["identity"])
	exclude = value["exclude_namespace"]
	if exclude is not None and exclude != identity.namespace_id:
		raise _native.NativeToolCacheError("unsafe-cache-action")
	cache = _native.NativeToolCache(_native.Path(value["cache_root"]))
	cache._ensure_directory(cache._cache_root())
	cache._ensure_directory(cache._repository_tool_root(identity))
	available = cache._prune(identity, exclude_namespace=exclude, needs_slot=value["needs_slot"])
	return {"status": "ready", "space_available": available}


def _prepare_namespace_request(value: object) -> dict[str, object]:
	if (
		not isinstance(value, dict)
		or set(value) != {"schema", "cache_root", "identity"}
		or value["schema"] != 1
		or (not isinstance(value["cache_root"], str))
		or (not _native.Path(value["cache_root"]).is_absolute())
	):
		raise _native.NativeToolCacheError("unsafe-cache-action")
	identity = _native._cache_identity_from_request(value["identity"])
	cache = _native.NativeToolCache(_native.Path(value["cache_root"]))
	cache._ensure_directory(cache._cache_root())
	cache._ensure_directory(cache._repository_tool_root(identity))
	namespace, marker = cache._prepare_namespace(cache.namespace_path(identity), identity)
	return {"status": "ready", "namespace": namespace, "marker": marker}
