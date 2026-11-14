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


def test_unowned_native_cache_directory_is_preserved_and_not_used(tmp_path: Path) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	path = cache.namespace_path(identity)
	path.mkdir(parents=True)
	unknown = path / "user-data"
	unknown.write_text("keep", encoding="utf-8")

	with cache.lease(identity, tmp_path / "snapshot" / "ruff") as selection:
		assert selection.storage == "private"
		assert selection.fallback_reason == "namespace-marker-missing"

	assert unknown.read_text(encoding="utf-8") == "keep"


def test_corrupt_native_cache_marker_uses_private_fallback_without_deleting_state(
	tmp_path: Path,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	with cache.lease(identity, tmp_path / "private-one") as selection:
		assert selection.storage == "persistent"
		state = selection.path / "native-state"
		state.write_text("keep", encoding="utf-8")
		(selection.path / ".quality-gate-native-cache.json").write_text("{", encoding="utf-8")

	with cache.lease(identity, tmp_path / "private-two") as fallback:
		assert fallback.storage == "private"
		assert fallback.fallback_reason == "namespace-marker-invalid"

	assert state.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize(
	"payload",
	(
		"[" * 1500 + "0" + "]" * 1500,
		" " * 5000,
	),
)
def test_pathological_native_cache_marker_uses_private_fallback(
	tmp_path: Path,
	payload: str,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	with cache.lease(identity, tmp_path / "private-one") as selection:
		assert selection.storage == "persistent"
		state = selection.path / "native-state"
		state.write_text("keep", encoding="utf-8")
		(selection.path / ".quality-gate-native-cache.json").write_text(payload, encoding="utf-8")

	with cache.lease(identity, tmp_path / "private-two") as fallback:
		assert fallback.storage == "private"
		assert fallback.fallback_reason == "namespace-marker-invalid"

	assert state.read_text(encoding="utf-8") == "keep"


def test_incomplete_native_cache_marker_temporary_is_recovered(tmp_path: Path) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	with cache.lease(identity, tmp_path / "private-one") as selection:
		assert selection.storage == "persistent"
		namespace_path = selection.path
	(namespace_path / ".quality-gate-native-cache.json").unlink()
	temporary = namespace_path / f"..quality-gate-native-cache.json.{os.getpid()}.tmp"
	temporary.write_text('{"owner":', encoding="utf-8")

	with cache.lease(identity, tmp_path / "private-two") as recovered:
		assert recovered.storage == "persistent"
		assert recovered.path == namespace_path
	assert not temporary.exists()
	assert (namespace_path / ".quality-gate-native-cache.json").is_file()


def test_cache_eviction_removes_only_inactive_provider_owned_namespaces(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	monkeypatch.setattr(native_cache_module, "_MAX_NAMESPACES_PER_REPOSITORY_TOOL", 4)
	cache = NativeToolCache(tmp_path / "provider-cache")
	identities = tuple(
		replace(_identity(), effective_options=("check", f"--config={index}.toml"))
		for index in range(3)
	)
	paths: list[Path] = []
	for index, identity in enumerate(identities):
		with cache.lease(identity, tmp_path / f"private-{index}") as selection:
			paths.append(selection.path)
			assert selection.storage == "persistent"
	assert all(path.is_dir() for path in paths)

	monkeypatch.setattr(native_cache_module, "_MAX_NAMESPACES_PER_REPOSITORY_TOOL", 2)
	new_identity = replace(
		_identity(),
		effective_options=("check", "--config=new.toml"),
	)
	with cache.lease(new_identity, tmp_path / "private-new") as second:
		assert second.storage == "persistent"
	assert sum(path.exists() for path in paths) == 1


def test_cache_eviction_preserves_namespace_replaced_before_quarantine(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	with cache.lease(identity, tmp_path / "private") as selection:
		assert selection.storage == "persistent"
		namespace_path = selection.path
	owned_state = namespace_path / "owned-state"
	owned_state.write_text("owned", encoding="utf-8")
	preserved_path = namespace_path.with_name("preserved-namespace")
	foreign_state: Path | None = None
	replaced = False
	original_rename = os.rename

	def replace_namespace_before_quarantine(source: str | Path, destination: str | Path) -> None:
		nonlocal foreign_state, replaced
		source_path = Path(source)
		destination_path = Path(destination)
		if (
			not replaced
			and source_path == namespace_path
			and destination_path.name.startswith(".evict-")
		):
			replaced = True
			original_rename(source_path, preserved_path)
			namespace_path.mkdir()
			foreign_state = namespace_path / "foreign-state"
			foreign_state.write_text("keep", encoding="utf-8")
		original_rename(source, destination)

	monkeypatch.setattr(native_cache_module.os, "rename", replace_namespace_before_quarantine)

	assert not cache._try_evict(namespace_path, identity)
	assert replaced
	assert foreign_state is not None
	assert foreign_state.read_text(encoding="utf-8") == "keep"
	assert (preserved_path / owned_state.name).read_text(encoding="utf-8") == "owned"
	assert (preserved_path / ".quality-gate-native-cache.json").is_file()


def test_cache_recovers_interrupted_namespace_eviction(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	with cache.lease(identity, tmp_path / "private-before") as selection:
		assert selection.storage == "persistent"
		namespace_path = selection.path
	original_remove = cache._remove_evicted_namespace

	def interrupt_removal(*_args: Any) -> bool:
		raise SystemExit("simulated process interruption after quarantine")

	monkeypatch.setattr(cache, "_remove_evicted_namespace", interrupt_removal)
	with pytest.raises(SystemExit, match="simulated process interruption"):
		cache._try_evict(namespace_path, identity)
	quarantines = tuple(namespace_path.parent.glob(".evict-*"))
	assert len(quarantines) == 1
	assert not namespace_path.exists()

	monkeypatch.setattr(cache, "_remove_evicted_namespace", original_remove)
	with cache.lease(identity, tmp_path / "private-after") as recovered:
		assert recovered.storage == "persistent"
		assert recovered.path == namespace_path
	assert not tuple(namespace_path.parent.glob(".evict-*"))


def test_cache_recovers_eviction_interrupted_after_marker_removal(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	with cache.lease(identity, tmp_path / "private-before") as selection:
		assert selection.storage == "persistent"
		namespace_path = selection.path
	marker_path = namespace_path / ".quality-gate-native-cache.json"
	marker = json.loads(marker_path.read_text(encoding="utf-8"))
	marker["last_used_ns"] = 0
	marker_path.write_text(json.dumps(marker), encoding="utf-8")
	replacement = replace(
		identity,
		effective_options=("check", "--config=replacement.toml"),
	)
	original_rmdir = Path.rmdir
	interrupted_path: Path | None = None

	def interrupt_final_eviction_rmdir(path: Path) -> None:
		nonlocal interrupted_path
		if path.parent == namespace_path.parent and path.name.startswith(".evict-"):
			interrupted_path = path
			raise SystemExit("simulated process interruption after marker removal")
		original_rmdir(path)

	monkeypatch.setattr(Path, "rmdir", interrupt_final_eviction_rmdir)
	with pytest.raises(SystemExit, match="after marker removal"):
		with cache.lease(replacement, tmp_path / "private-replacement"):
			pytest.fail("eviction interruption must happen before the new lease")
	assert interrupted_path is not None
	assert interrupted_path.is_dir()
	assert not (interrupted_path / ".quality-gate-native-cache.json").exists()
	owner_path = interrupted_path.with_name(interrupted_path.name + ".owner.json")
	assert owner_path.is_file()

	monkeypatch.setattr(Path, "rmdir", original_rmdir)
	with cache.lease(identity, tmp_path / "private-after") as recovered:
		assert recovered.storage == "persistent"
	assert not interrupted_path.exists()
	assert not owner_path.exists()


def test_cache_preserves_unowned_empty_eviction_directory(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	monkeypatch.setattr(native_cache_module, "_MAX_NAMESPACES_PER_REPOSITORY_TOOL", 1)
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	repository_tool_root = cache._repository_tool_root(identity)
	repository_tool_root.mkdir(parents=True)
	unowned = repository_tool_root / f".evict-{identity.namespace_id}-{'0' * 32}"
	unowned.mkdir()

	with cache.lease(identity, tmp_path / "private") as selection:
		assert selection.storage == "private"
		assert selection.fallback_reason == "capacity-limit"
	assert unowned.is_dir()
	assert not tuple(unowned.iterdir())


def test_expired_native_cache_namespace_is_evicted(tmp_path: Path) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	with cache.lease(identity, tmp_path / "private-old") as old:
		old_path = old.path
	marker_path = old_path / ".quality-gate-native-cache.json"
	marker = json.loads(marker_path.read_text(encoding="utf-8"))
	marker["last_used_ns"] = 0
	marker_path.write_text(json.dumps(marker), encoding="utf-8")
	new_identity = replace(
		identity,
		effective_options=("check", "--config=new.toml"),
	)
	with cache.lease(new_identity, tmp_path / "private-new") as new:
		assert new.storage == "persistent"
	assert not old_path.exists()


def test_concurrent_worktrees_use_private_cache_when_namespace_is_busy(
	tmp_path: Path,
) -> None:
	cache_root = tmp_path / "provider-cache"
	cache = NativeToolCache(cache_root, lease_timeout_seconds=0)
	identity = _identity()
	private_path = tmp_path / "worktree-one" / ".ruff_cache"
	other_private_path = tmp_path / "worktree-two" / ".ruff_cache"
	child_script = (
		"import json, sys; "
		"from pathlib import Path; "
		"from quality_gate.native_cache import NativeToolCache, NativeToolCacheIdentity; "
		"identity = NativeToolCacheIdentity(**json.loads(sys.argv[2])); "
		"cache = NativeToolCache(Path(sys.argv[1]), lease_timeout_seconds=0); "
		"context = cache.lease(identity, Path(sys.argv[3])); "
		"selection = context.__enter__(); "
		"selection.path.mkdir(parents=True, exist_ok=True); "
		"(selection.path / 'worktree-state').write_text('child', encoding='utf-8'); "
		"print(json.dumps({'storage': selection.storage, 'reason': selection.fallback_reason, "
		"'path': str(selection.path), 'namespace_id': selection.namespace_id})); "
		"context.__exit__(None, None, None)"
	)

	with cache.lease(identity, private_path) as parent:
		assert parent.storage == "persistent"
		parent_state = parent.path / "owner-state"
		parent_state.write_text("parent", encoding="utf-8")
		result = subprocess.run(
			[
				sys.executable,
				"-c",
				child_script,
				str(cache_root),
				json.dumps(asdict(identity)),
				str(other_private_path),
			],
			capture_output=True,
			check=False,
			text=True,
			timeout=10,
			cwd=Path(__file__).parents[1],
		)
	assert result.returncode == 0, result.stderr
	assert json.loads(result.stdout) == {
		"storage": "private",
		"reason": "lock-busy",
		"path": str(other_private_path),
		"namespace_id": parent.namespace_id,
	}
	assert parent_state.read_text(encoding="utf-8") == "parent"
	assert (other_private_path / "worktree-state").read_text(encoding="utf-8") == "child"


def test_cache_eviction_skips_an_active_namespace(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	monkeypatch.setattr(native_cache_module, "_MAX_NAMESPACES_PER_REPOSITORY_TOOL", 1)
	cache = NativeToolCache(tmp_path / "provider-cache", lease_timeout_seconds=0)
	first_identity = _identity()
	second_identity = replace(
		first_identity,
		effective_options=("check", "--config=second.toml"),
	)
	with cache.lease(first_identity, tmp_path / "private-one") as first:
		with cache.lease(second_identity, tmp_path / "private-two") as second:
			assert second.storage == "private"
			assert second.fallback_reason == "capacity-limit"
		assert first.path.is_dir()


def test_pinned_native_tools_reuse_and_invalidate_snapshot_cache(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""Opt-in real-tool measurement for snapshots and changed source content."""
	if os.environ.get("QUALITY_GATE_NATIVE_CACHE_INTEGRATION") != "1":
		pytest.skip("set QUALITY_GATE_NATIVE_CACHE_INTEGRATION=1 for pinned-tool evidence")
	report_raw = os.environ.get("QUALITY_GATE_NATIVE_CACHE_REPORT")
	if not report_raw:
		pytest.fail("QUALITY_GATE_NATIVE_CACHE_REPORT must name an external report path")
	repository_root = Path(__file__).parents[1].resolve()
	prepared = prepare(repository_root, repository_root=repository_root)
	python = prepared.runtimes[0].python
	if python is None:
		pytest.fail("the selected pinned component runtime is not ready")
	cache_root = tmp_path / "provider-cache"
	prepared = replace(prepared, cache_root=cache_root)
	recorder = _NativeToolRunRecorder(runner.run)
	monkeypatch.setattr(runner, "run", recorder)
	commands = _native_tool_command_templates(prepared, python)
	_collect_native_cache_measurements(tmp_path, prepared, recorder, commands)
	payload_counts = _assert_native_cache_measurements(recorder.observed, cache_root)
	_write_native_cache_measurement_report(
		Path(report_raw), prepared, recorder.observed, payload_counts
	)
