"""Feature-preserving test partition."""

from __future__ import annotations

from native_feature_support72 import (
	MEASUREMENT_EXPECTED_TOOL_RUNS as MEASUREMENT_EXPECTED_TOOL_RUNS,
)
from native_feature_support72 import (
	MEASUREMENT_SOURCE_MTIME_NS as MEASUREMENT_SOURCE_MTIME_NS,
)
from native_feature_support72 import (
	MEASUREMENT_WARM_RUNS as MEASUREMENT_WARM_RUNS,
)
from native_feature_support72 import (
	Any as Any,
)
from native_feature_support72 import (
	BinaryIO as BinaryIO,
)
from native_feature_support72 import (
	Callable as Callable,
)
from native_feature_support72 import (
	CheckResult as CheckResult,
)
from native_feature_support72 import (
	EditError as EditError,
)
from native_feature_support72 import (
	GateOperation as GateOperation,
)
from native_feature_support72 import (
	Literal as Literal,
)
from native_feature_support72 import (
	EngineIdentity as EngineIdentity,
	NativeToolCache as NativeToolCache,
)
from native_feature_support72 import (
	NativeToolCacheIdentity as NativeToolCacheIdentity,
)
from native_feature_support72 import (
	NativeToolCacheSelection as NativeToolCacheSelection,
)
from native_feature_support72 import (
	OperationTimeoutError as OperationTimeoutError,
)
from native_feature_support72 import (
	Path as Path,
)
from native_feature_support72 import (
	PreparedEnvironment as PreparedEnvironment,
)
from native_feature_support72 import (
	SimpleNamespace as SimpleNamespace,
)
from native_feature_support72 import (
	Status as Status,
)
from native_feature_support72 import (
	_assert_native_cache_measurements as _assert_native_cache_measurements,
)
from native_feature_support72 import (
	_assert_persistent_mypy_run as _assert_persistent_mypy_run,
)
from native_feature_support72 import (
	_close_file_handle as _close_file_handle,
)
from native_feature_support72 import (
	_collect_native_cache_measurements as _collect_native_cache_measurements,
)
from native_feature_support72 import (
	_execute_measured_snapshot as _execute_measured_snapshot,
)
from native_feature_support72 import (
	_identity as _identity,
)
from native_feature_support72 import (
	_mypy_tool_command_templates as _mypy_tool_command_templates,
)
from native_feature_support72 import (
	_native_tool_command_templates as _native_tool_command_templates,
)
from native_feature_support72 import (
	_NativeToolRunRecorder as _NativeToolRunRecorder,
)
from native_feature_support72 import (
	_open_file_without_shared_reads as _open_file_without_shared_reads,
)
from native_feature_support72 import (
	_prepared_with_fixture_dependencies as _prepared_with_fixture_dependencies,
)
from native_feature_support72 import (
	_tool_finding_text as _tool_finding_text,
)
from native_feature_support72 import (
	_write_native_cache_measurement_report as _write_native_cache_measurement_report,
)
from native_feature_support72 import (
	asdict as asdict,
)
from native_feature_support72 import (
	cast as cast,
)
from native_feature_support72 import (
	hashlib as hashlib,
)
from native_feature_support72 import (
	json as json,
)
from native_feature_support72 import (
	native_cache_module as native_cache_module,
)
from native_feature_support72 import (
	native_tool_source_workspace as native_tool_source_workspace,
)
from native_feature_support72 import (
	observe_check as observe_check,
)
from native_feature_support72 import (
	open_locked_file as open_locked_file,
)
from native_feature_support72 import (
	operation_module as operation_module,
)
from native_feature_support72 import (
	os as os,
)
from native_feature_support72 import (
	platform as platform,
)
from native_feature_support72 import (
	prepare as prepare,
)
from native_feature_support72 import (
	pytest as pytest,
)
from native_feature_support72 import (
	replace as replace,
)
from native_feature_support72 import (
	runner as runner,
)
from native_feature_support72 import (
	shutil as shutil,
)
from native_feature_support72 import (
	stat as stat,
)
from native_feature_support72 import (
	subprocess as subprocess,
)
from native_feature_support72 import (
	sys as sys,
)
from native_feature_support72 import (
	time as time,
)
from native_feature_support72 import (
	use_long_windows_staging_limit_for_test_sandbox as use_long_windows_staging_limit_for_test_sandbox,
)


def test_markerless_native_tool_source_workspace_is_preserved_and_falls_back(
	tmp_path: Path,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	private_cache = tmp_path / "private-cache"
	snapshot_root = tmp_path / "snapshot"
	snapshot_root.mkdir()

	with cache.lease(identity, private_cache) as selection:
		stage_parent = selection.path / "_s"
		stable_root = stage_parent / "c"
		stage_parent.mkdir()
		stable_root.mkdir(parents=True)
		if os.name != "nt":
			stage_parent.chmod(0o755)
		stage_mode = stat.S_IMODE(stage_parent.stat().st_mode)
		with native_tool_source_workspace(selection, snapshot_root, private_cache) as (
			effective,
			workspace_root,
		):
			assert effective.storage == "private"
			assert effective.path == private_cache
			assert effective.fallback_reason == "unsafe-source-path"
			assert workspace_root == snapshot_root
		assert stable_root.is_dir()
		if os.name != "nt":
			assert stat.S_IMODE(stage_parent.stat().st_mode) == stage_mode


def test_foreign_native_tool_workspace_state_uses_private_source_and_cache(
	tmp_path: Path,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	private_cache = tmp_path / "private-cache"
	snapshot_root = tmp_path / "snapshot"
	snapshot_root.mkdir()

	with cache.lease(identity, private_cache) as selection:
		foreign_state = selection.path / "_s"
		foreign_state.write_text("preserve", encoding="utf-8")
		with native_tool_source_workspace(selection, snapshot_root, private_cache) as (
			effective,
			workspace_root,
		):
			assert effective.storage == "private"
			assert effective.path == private_cache
			assert effective.fallback_reason == "unsafe-source-path"
			assert workspace_root == snapshot_root

	assert foreign_state.read_text(encoding="utf-8") == "preserve"


def test_unreadable_source_staging_uses_private_fallback(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	private_cache = tmp_path / "private-cache"
	snapshot_root = tmp_path / "snapshot"
	snapshot_root.mkdir()
	original_iterdir = Path.iterdir
	stage_parent: Path | None = None
	stage_reads = 0

	def guarded_iterdir(path: Path) -> Any:
		nonlocal stage_reads
		if path == stage_parent:
			stage_reads += 1
			if stage_reads in {3, 4}:
				raise PermissionError("staging path cannot be inspected")
		return original_iterdir(path)

	monkeypatch.setattr(Path, "iterdir", guarded_iterdir)
	with cache.lease(identity, private_cache) as selection:
		stage_parent = selection.path / "_s"
		with native_tool_source_workspace(selection, snapshot_root, private_cache) as (
			effective,
			workspace_root,
		):
			assert effective.storage == "private"
			assert effective.path == private_cache
			assert effective.fallback_reason == "unsafe-source-path"
			assert workspace_root == snapshot_root
	assert stage_reads == 4


def test_failed_initial_source_parent_setup_is_recoverable(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	private_cache = tmp_path / "private-cache"
	snapshot_root = tmp_path / "snapshot"
	snapshot_root.mkdir()
	original_chmod = os.chmod

	with cache.lease(identity, private_cache) as selection:
		stage_parent = selection.path / "_s"

		def fail_stage_parent_chmod(
			path: str | Path,
			mode: int,
			*args: Any,
			**kwargs: Any,
		) -> None:
			if Path(path) == stage_parent:
				raise PermissionError("staging parent permissions are unavailable")
			original_chmod(path, mode, *args, **kwargs)

		monkeypatch.setattr(native_cache_module.os, "chmod", fail_stage_parent_chmod)
		with native_tool_source_workspace(selection, snapshot_root, private_cache) as (
			effective,
			workspace_root,
		):
			assert effective.storage == "private"
			assert effective.fallback_reason == "source-staging-unavailable"
			assert workspace_root == snapshot_root
		assert not stage_parent.exists()

		monkeypatch.setattr(native_cache_module.os, "chmod", original_chmod)
		with native_tool_source_workspace(selection, snapshot_root, private_cache) as (
			effective,
			workspace_root,
		):
			assert effective.storage == "persistent"
			assert workspace_root == selection.path / "_s" / "c"


def test_interrupted_empty_source_parent_setup_is_recovered(tmp_path: Path) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	private_cache = tmp_path / "private-cache"
	snapshot_root = tmp_path / "snapshot"
	snapshot_root.mkdir()
	with cache.lease(identity, private_cache) as selection:
		assert selection.storage == "persistent"
		stage_parent = selection.path / "_s"
		stage_parent.mkdir()
	assert not (stage_parent / "p").exists()

	with cache.lease(identity, private_cache) as selection:
		with native_tool_source_workspace(selection, snapshot_root, private_cache) as (
			effective,
			workspace_root,
		):
			assert effective.storage == "persistent"
			assert workspace_root == stage_parent / "c"
	assert (stage_parent / "p").is_dir()


def test_windows_native_tool_workspace_path_limit_uses_private_source_and_cache(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	if os.name != "nt":
		pytest.skip("Windows subprocess working-directory paths have MAX_PATH limit")
	monkeypatch.setattr(native_cache_module, "_WINDOWS_MAX_PATH", 260)
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	private_cache = tmp_path / "private-cache"
	snapshot_root = tmp_path / "snapshot"
	snapshot_root.mkdir()
	long_cache_root = tmp_path / ("p" * 240)

	with cache.lease(identity, private_cache) as selection:
		long_selection = replace(selection, path=long_cache_root)
		with native_tool_source_workspace(long_selection, snapshot_root, private_cache) as (
			effective,
			workspace_root,
		):
			assert effective.storage == "private"
			assert effective.path == private_cache
			assert effective.fallback_reason == "source-staging-unavailable"
			assert workspace_root == snapshot_root


def test_windows_reparse_point_is_detected_without_path_is_junction(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	path = tmp_path / "junction"
	metadata = SimpleNamespace(st_mode=0, st_file_attributes=0x0400)
	monkeypatch.setattr(Path, "lstat", lambda _path: metadata)
	monkeypatch.setattr(native_cache_module.os, "name", "nt")

	assert native_cache_module._is_link(path)


def test_file_lock_rejects_windows_reparse_point(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	path = tmp_path / "reparse-lock"
	metadata = SimpleNamespace(st_mode=0, st_file_attributes=0x0400)
	original_lstat = Path.lstat

	def reparse_lock_lstat(candidate: Path) -> os.stat_result | SimpleNamespace:
		return metadata if candidate == path else original_lstat(candidate)

	monkeypatch.setattr(Path, "lstat", reparse_lock_lstat)
	with pytest.raises(OSError, match="link or reparse point"):
		open_locked_file(path, blocking=False)


def test_file_lock_rejects_open_handle_that_does_not_match_path(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	path = tmp_path / "lock"
	other_path = tmp_path / "other-lock"
	path.write_bytes(b"\0")
	other_path.write_bytes(b"\0")
	original_open = Path.open

	def open_other_lock(candidate: Path, *args: Any, **kwargs: Any) -> BinaryIO:
		if candidate == path:
			return cast(BinaryIO, original_open(other_path, *args, **kwargs))
		return cast(BinaryIO, original_open(candidate, *args, **kwargs))

	monkeypatch.setattr(Path, "open", open_other_lock)
	with pytest.raises(OSError, match="lock path changed while opening"):
		open_locked_file(path, blocking=True)


def test_file_lock_rejects_hard_linked_lock_files(tmp_path: Path) -> None:
	path = tmp_path / "lock"
	linked_path = tmp_path / "linked-lock"
	path.write_bytes(b"\0")
	try:
		os.link(path, linked_path)
	except OSError:
		pytest.skip("hard links are unavailable for this temporary directory")

	with pytest.raises(OSError, match="unsafe file used as a lock"):
		open_locked_file(path, blocking=False)


def test_cache_directory_reparse_point_falls_back_before_creation(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	blocked_path = cache._cache_root() / identity.tool
	original_is_link = native_cache_module._is_link

	def detect_blocked_path(path: Path) -> bool:
		return path == blocked_path or original_is_link(path)

	monkeypatch.setattr(native_cache_module, "_is_link", detect_blocked_path)
	with pytest.raises(native_cache_module.NativeToolCacheError, match="unsafe-cache-path"):
		cache._ensure_directory(cache._repository_tool_root(identity))
	assert not blocked_path.exists()


def test_cache_root_keeps_lexical_path_and_rejects_reparse_ancestor(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	target = tmp_path / "cache-target"
	target.mkdir()
	link = tmp_path / "cache-link"
	try:
		link.symlink_to(target, target_is_directory=True)
	except OSError:
		pytest.skip("directory symlinks are unavailable for this temporary directory")
	cache_root = link / "provider-cache"
	with monkeypatch.context() as patcher:
		patcher.setattr(
			Path,
			"resolve",
			lambda *_args, **_kwargs: pytest.fail("cache root must remain lexical"),
		)
		cache = NativeToolCache(cache_root)
	assert cache.root == cache_root.absolute()

	with cache.lease(_identity(), tmp_path / "private-cache") as selection:
		assert selection.storage == "private"
		assert selection.fallback_reason == "unsafe-cache-path"
	assert not (target / "provider-cache" / "native-tools").exists()


def test_cache_root_does_not_normalize_symlink_before_parent_component(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	link = tmp_path / "cache-link"
	cache_root = link / ".." / "provider-cache"
	cache = NativeToolCache(cache_root)
	original_is_link = native_cache_module._is_link

	def identify_link(path: Path) -> bool:
		return path == link or original_is_link(path)

	monkeypatch.setattr(native_cache_module, "_is_link", identify_link)

	with cache.lease(_identity(), tmp_path / "private-cache") as selection:
		assert selection.storage == "private"
		assert selection.fallback_reason == "unsafe-cache-path"
	assert not (tmp_path / "provider-cache" / "native-tools").exists()


def test_posix_repository_budget_key_preserves_case_sensitivity(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	if os.name == "nt":
		pytest.skip("Windows repository paths are case-insensitive")
	monkeypatch.setattr(native_cache_module.os, "name", "posix")
	upper = replace(_identity(), repository="/repositories/Foo")
	lower = replace(_identity(), repository="/repositories/foo")

	assert upper.repository_key != lower.repository_key


def test_native_cache_namespace_changes_with_tool_and_compatibility_inputs(
	tmp_path: Path,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	base = _identity()
	identities = (
		base,
		replace(base, repository="C:/repositories/other-checkout"),
		replace(base, engine=EngineIdentity("source", "4.2.0", "b" * 64)),
		replace(base, tool="mypy", tool_version="2.3.1", tool_digest="b" * 64),
		replace(base, runtime_fingerprint="runtime-b"),
		replace(base, tool_version="0.17.0"),
		replace(base, tool_digest="d" * 64),
		replace(base, policy_release="v4.3.0"),
		replace(base, policy_digest="e" * 64),
		replace(base, platform="linux-x86_64"),
		replace(base, python_version="3.12"),
		replace(base, effective_options=("check", "--config=other.toml")),
		replace(base, import_roots=("src",)),
	)
	selections: list[NativeToolCacheSelection] = []

	for identity in identities:
		with cache.lease(identity, tmp_path / "private" / identity.tool) as selection:
			selections.append(selection)

	assert all(selection.storage == "persistent" for selection in selections)
	assert len({selection.namespace_id for selection in selections}) == len(identities)
	assert selections[0].path != selections[1].path
	assert selections[0].path != selections[2].path
	assert selections[0].path != selections[3].path


def test_busy_native_cache_uses_private_fallback_and_keeps_tool_executable(
	tmp_path: Path,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache", lease_timeout_seconds=0)
	identity = _identity()
	private_path = tmp_path / "snapshot" / "ruff"

	with cache.lease(identity, private_path) as active:
		assert active.storage == "persistent"
		with cache.lease(identity, private_path) as fallback:
			assert fallback.storage == "private"
			assert fallback.path == private_path
			assert fallback.fallback_reason == "lock-busy"
			assert fallback.namespace_id == active.namespace_id


def test_source_workspace_falls_back_when_namespace_changes_after_lease(
	tmp_path: Path,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	private_cache = tmp_path / "private-cache"
	snapshot_root = tmp_path / "snapshot"
	snapshot_root.mkdir()

	with cache.lease(identity, private_cache) as selection:
		assert selection.storage == "persistent"
		preserved_namespace = selection.path.with_name("preserved-namespace")
		selection.path.rename(preserved_namespace)
		selection.path.mkdir()
		foreign_state = selection.path / "foreign-state"
		foreign_state.write_text("preserve", encoding="utf-8")

		with native_tool_source_workspace(selection, snapshot_root, private_cache) as (
			effective,
			workspace_root,
		):
			assert effective.storage == "private"
			assert effective.fallback_reason == "unsafe-source-path"
			assert effective.path == private_cache
			assert workspace_root == snapshot_root

		assert foreign_state.read_text(encoding="utf-8") == "preserve"
		assert (preserved_namespace / ".quality-gate-native-cache.json").is_file()
