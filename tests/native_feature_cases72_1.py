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


def test_compatible_native_cache_is_reused_across_private_snapshot_directories(
	tmp_path: Path,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	first_snapshot_cache = tmp_path / "snapshot-one" / "ruff"
	second_snapshot_cache = tmp_path / "snapshot-two" / "ruff"

	with cache.lease(identity, first_snapshot_cache) as first:
		assert first.storage == "persistent"
		(first.path / "native-state").write_text("warm", encoding="utf-8")

	with cache.lease(identity, second_snapshot_cache) as second:
		assert second.storage == "persistent"
		assert second.path == first.path
		assert (second.path / "native-state").read_text(encoding="utf-8") == "warm"
		assert second.namespace_id == first.namespace_id

	assert not first_snapshot_cache.exists()
	assert not second_snapshot_cache.exists()


def test_persistent_native_tool_workspace_reuses_stable_root_and_content_mtime(
	tmp_path: Path,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	private_cache = tmp_path / "private-cache"
	source = "def add(left: int, right: int) -> int:\n\treturn left + right\n"
	workspace_roots: list[Path] = []
	mtimes: list[int] = []

	for snapshot_name, source_mtime_ns in (("snapshot-one", 1), ("snapshot-two", 2)):
		snapshot_root = tmp_path / snapshot_name
		(snapshot_root / "src").mkdir(parents=True)
		source_path = snapshot_root / "src" / "module.py"
		source_path.write_text(source, encoding="utf-8")
		os.utime(source_path, ns=(source_mtime_ns, source_mtime_ns))
		with cache.lease(identity, private_cache) as selection:
			with native_tool_source_workspace(selection, snapshot_root, private_cache) as (
				effective,
				workspace_root,
			):
				assert effective.storage == "persistent"
				assert workspace_root != snapshot_root
				workspace_roots.append(workspace_root)
				mtimes.append((workspace_root / "src" / "module.py").stat().st_mtime_ns)
				assert (workspace_root / "src" / "module.py").read_text(encoding="utf-8") == source
				source_directory_mode = stat.S_IMODE((workspace_root / "src").stat().st_mode)
				source_file_mode = stat.S_IMODE(
					(workspace_root / "src" / "module.py").stat().st_mode
				)
				assert not source_directory_mode & stat.S_IWRITE
				assert not source_file_mode & stat.S_IWRITE
				if os.name != "nt":
					assert source_directory_mode == 0o500
					assert source_file_mode == 0o400
		staged_root = selection.path / "_s" / "c"
		assert not staged_root.exists()
		assert (selection.path / "_s" / "p").is_dir()

	assert workspace_roots[0] == workspace_roots[1]
	assert mtimes[0] == mtimes[1]


def test_source_cleanup_failure_does_not_override_successful_tool(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	private_cache = tmp_path / "private-cache"
	snapshot_root = tmp_path / "snapshot"
	snapshot_root.mkdir()
	(snapshot_root / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
	tool_completed = False

	def fail_cleanup(*args: Any, **kwargs: Any) -> None:
		raise native_cache_module.NativeToolCacheError("source-cleanup-unavailable")

	with cache.lease(identity, private_cache) as selection:
		with native_tool_source_workspace(selection, snapshot_root, private_cache) as (
			effective,
			workspace_root,
		):
			assert effective.storage == "persistent"
			assert (workspace_root / "module.py").is_file()
			monkeypatch.setattr(native_cache_module, "_remove_owned_source_workspace", fail_cleanup)
			tool_completed = True

	assert tool_completed


def test_operation_owns_native_source_workspace_until_shared_cleanup(
	tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
	# The supervised child uses the real Windows path limit, unlike in-process fixtures.
	cache = NativeToolCache(tmp_path_factory.mktemp("nc"))
	identity = _identity()
	private_cache = tmp_path / "private-cache"
	snapshot_root = tmp_path / "snapshot"
	snapshot_root.mkdir()
	(snapshot_root / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
	repository = tmp_path / "repository"
	repository.mkdir()
	operation = GateOperation(repository, "check", timeout_seconds=10, cleanup_seconds=3)
	staged_root: Path | None = None

	with operation:
		with cache.lease(identity, private_cache, operation=operation) as selection:
			with native_tool_source_workspace(
				selection, snapshot_root, private_cache, operation=operation
			) as (effective, workspace_root):
				assert effective.storage == "persistent", effective.fallback_reason
				staged_root = workspace_root
			assert workspace_root.is_dir()

	assert staged_root is not None
	assert not staged_root.exists()
	assert operation.stages[-1].stage == "cleanup"
	with cache.lease(identity, private_cache) as selection:
		assert selection.storage == "persistent"


def test_operation_deadline_prevents_native_source_preparation_after_expiry(
	tmp_path: Path,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	private_cache = tmp_path / "private-cache"
	snapshot_root = tmp_path / "snapshot"
	snapshot_root.mkdir()
	(snapshot_root / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
	repository = tmp_path / "repository"
	repository.mkdir()
	operation = GateOperation(repository, "check", timeout_seconds=10, cleanup_seconds=3)

	with operation:
		with cache.lease(identity, private_cache, operation=operation) as selection:
			operation.deadline = time.monotonic() - 1
			with pytest.raises(OperationTimeoutError):
				with native_tool_source_workspace(
					selection, snapshot_root, private_cache, operation=operation
				):
					pytest.fail("source preparation started after the operation deadline")

	assert operation.timed_out


def test_operation_cache_selection_does_not_start_after_deadline(tmp_path: Path) -> None:
	cache = NativeToolCache(tmp_path / "cache")
	gate = GateOperation(tmp_path, "check", cleanup_seconds=1)
	entered = False
	with gate:
		gate.deadline = time.monotonic() - 1
		with cache.lease(_identity(), tmp_path / "private", operation=gate):
			entered = True
	assert not entered
	assert gate.timed_out


def test_operation_cache_pruning_is_physically_bounded(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	cache = NativeToolCache(tmp_path / "cache")
	old_identity = _identity(runtime_fingerprint="old")
	with cache.lease(old_identity, tmp_path / "old-private") as selection:
		payload = selection.path / "payload"
		payload.mkdir()
		(payload / "data").write_text("owned", encoding="utf-8")
		marker = selection.path / ".quality-gate-native-cache.json"
		value = json.loads(marker.read_text(encoding="utf-8"))
		value["last_used_ns"] = 1
		marker.write_text(json.dumps(value), encoding="utf-8")
	real_remove = native_cache_module.shutil.rmtree

	def slow_remove(*args: Any, **kwargs: Any) -> None:
		time.sleep(2)
		real_remove(*args, **kwargs)

	monkeypatch.setattr(native_cache_module.shutil, "rmtree", slow_remove)
	body = (
		getattr(native_cache_module, "_NATIVE_CACHE_BODY", None)
		or native_cache_module._SOURCE_PREPARE_BODY
	)
	monkeypatch.setattr(
		native_cache_module,
		"_NATIVE_CACHE_BODY",
		"import time; time.sleep(2)\n" + body,
		raising=False,
	)
	gate = GateOperation(tmp_path, "check", timeout_seconds=0.1, cleanup_seconds=0.5)
	entered = False
	started = time.monotonic()
	with gate:
		with cache.lease(
			_identity(runtime_fingerprint="new"), tmp_path / "private", operation=gate
		):
			entered = True
	assert not entered
	assert gate.timed_out
	assert time.monotonic() - started < 1
	assert gate.stages[-1].stage == "cleanup" and gate.stages[-1].disposition == "executed"


def test_operation_cache_source_worker_does_not_execute_the_tool(
	tmp_path: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
	cache = NativeToolCache(tmp_path_factory.mktemp("nc"))
	snapshot = tmp_path / "snapshot"
	snapshot.mkdir()
	(snapshot / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
	gate = GateOperation(tmp_path, "check", timeout_seconds=10, cleanup_seconds=1)
	real_supervise = operation_module.supervise_process

	def timeout_source(command: list[str], **kwargs: Any) -> tuple[int, bytes, bytes]:
		if b'"snapshot_root"' in (kwargs.get("payload") or b""):
			if kwargs.get("on_started") is not None:
				kwargs["on_started"]()
			gate.deadline = time.monotonic() - 1
			raise EditError("deadline")
		return real_supervise(command, **kwargs)

	monkeypatch.setattr(operation_module, "supervise_process", timeout_source)

	def check() -> CheckResult:
		with cache.lease(_identity(), tmp_path / "private", operation=gate) as selection:
			with native_tool_source_workspace(
				selection, snapshot, tmp_path / "private", operation=gate
			):
				pytest.fail("tool body ran after source preparation timed out")
		return CheckResult("python.component_1.ruff", Status.PASSED, "unexpected")

	with gate:
		observe_check("python.component_1.ruff", "snapshot", check, process_required=True)
	result = next(item for item in gate.verdict().results if item.check_id.endswith(".ruff"))
	assert result.status is Status.UNCHECKED
	assert result.telemetry is not None and result.telemetry.disposition == "not-run"


def test_interrupted_native_tool_source_workspace_is_recovered_under_lease(
	tmp_path: Path,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	private_cache = tmp_path / "private-cache"
	snapshot_root = tmp_path / "snapshot"
	(snapshot_root / "src").mkdir(parents=True)
	(snapshot_root.joinpath("src", "module.py")).write_text("VALUE = 2\n", encoding="utf-8")

	with cache.lease(identity, private_cache) as selection:
		stage_parent = selection.path / "_s"
		stable_root = stage_parent / "c"
		owner_marker = stage_parent / "o"
		parent_owner_marker = stage_parent / "p"
		stage_parent.mkdir()
		parent_owner_marker.mkdir()
		stable_root.mkdir(parents=True)
		(stable_root / "partial-copy").write_text("stale", encoding="utf-8")
		owner_marker.mkdir()
		with native_tool_source_workspace(selection, snapshot_root, private_cache) as (
			effective,
			workspace_root,
		):
			assert effective.storage == "persistent"
			assert workspace_root == stable_root
			assert not (workspace_root / "partial-copy").exists()
			assert (workspace_root / "src" / "module.py").read_text(encoding="utf-8") == (
				"VALUE = 2\n"
			)
		assert not owner_marker.exists()
		assert not stable_root.exists()
		assert parent_owner_marker.is_dir()


def test_source_workspace_cleanup_preserves_replacement_stage_parent(
	tmp_path: Path,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	private_cache = tmp_path / "private-cache"
	snapshot_root = tmp_path / "snapshot"
	(snapshot_root / "src").mkdir(parents=True)
	(snapshot_root / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")

	with cache.lease(identity, private_cache) as selection:
		stage_parent = selection.path / "_s"
		preserved_stage = selection.path / "_s-preserved"
		with pytest.raises(native_cache_module.NativeToolCacheError, match="unsafe-source-path"):
			with native_tool_source_workspace(selection, snapshot_root, private_cache):
				stage_parent.rename(preserved_stage)
				stage_parent.mkdir()
				(stage_parent / "p").mkdir()
				(stage_parent / "o").mkdir()
				(stage_parent / "c").mkdir()
				(stage_parent / "c" / "foreign.txt").write_text("preserve", encoding="utf-8")

		assert (stage_parent / "c" / "foreign.txt").read_text(encoding="utf-8") == "preserve"
		assert (preserved_stage / "c" / "src" / "module.py").is_file()


def test_stale_source_recovery_rechecks_stage_parent_identity_before_deletion(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	private_cache = tmp_path / "private-cache"
	snapshot_root = tmp_path / "snapshot"
	snapshot_root.mkdir()
	with cache.lease(identity, private_cache) as seeded:
		stage_parent = seeded.path / "_s"
		stable_root = stage_parent / "c"
		stage_parent.mkdir()
		(stage_parent / "p").mkdir()
		(stage_parent / "o").mkdir()
		stable_root.mkdir()
		(stable_root / "partial-copy").write_text("stale", encoding="utf-8")
	preserved_stage = stage_parent.with_name("_s-preserved")

	def replace_stage_parent(_path: Path) -> None:
		stage_parent.rename(preserved_stage)
		stage_parent.mkdir()
		(stage_parent / "p").mkdir()
		(stage_parent / "o").mkdir()
		(stage_parent / "c").mkdir()
		(stage_parent / "c" / "foreign.txt").write_text("preserve", encoding="utf-8")

	monkeypatch.setattr(native_cache_module, "_make_writable", replace_stage_parent)
	with cache.lease(identity, private_cache) as selection:
		with native_tool_source_workspace(selection, snapshot_root, private_cache) as (
			effective,
			workspace_root,
		):
			assert effective.storage == "private"
			assert effective.fallback_reason == "unsafe-source-path"
			assert workspace_root == snapshot_root

	assert (stage_parent / "c" / "foreign.txt").read_text(encoding="utf-8") == "preserve"
	assert (preserved_stage / "c" / "partial-copy").read_text(encoding="utf-8") == "stale"
