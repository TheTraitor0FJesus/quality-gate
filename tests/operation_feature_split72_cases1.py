"""Feature test partition."""

from __future__ import annotations

from operation_feature_split72_support import (
	EXIT_UNCHECKED as EXIT_UNCHECKED,
)
from operation_feature_split72_support import (
	PREPARATION_ELAPSED_MS as PREPARATION_ELAPSED_MS,
)
from operation_feature_split72_support import (
	PROBE_TERMINATION_BOUND_SECONDS as PROBE_TERMINATION_BOUND_SECONDS,
)
from operation_feature_split72_support import (
	TREE_TERMINATION_BOUND_SECONDS as TREE_TERMINATION_BOUND_SECONDS,
)
from operation_feature_split72_support import (
	CheckResult as CheckResult,
)
from operation_feature_split72_support import (
	Finding as Finding,
)
from operation_feature_split72_support import (
	GateOperation as GateOperation,
)
from operation_feature_split72_support import (
	Path as Path,
)
from operation_feature_split72_support import (
	RuntimeInspection as RuntimeInspection,
)
from operation_feature_split72_support import (
	SimpleNamespace as SimpleNamespace,
)
from operation_feature_split72_support import (
	Status as Status,
)
from operation_feature_split72_support import (
	TemporaryWorkspaceError as TemporaryWorkspaceError,
)
from operation_feature_split72_support import (
	errno as errno,
)
from operation_feature_split72_support import (
	launcher as launcher,
)
from operation_feature_split72_support import (
	load_manifest as load_manifest,
)
from operation_feature_split72_support import (
	operation as operation,
)
from operation_feature_split72_support import (
	os as os,
)
from operation_feature_split72_support import (
	process_alive as process_alive,
)
from operation_feature_split72_support import (
	pytest as pytest,
)
from operation_feature_split72_support import (
	runner as runner,
)
from operation_feature_split72_support import (
	secrets_module as secrets_module,
)
from operation_feature_split72_support import (
	shutil as shutil,
)
from operation_feature_split72_support import (
	signal as signal,
)
from operation_feature_split72_support import (
	sys as sys,
)
from operation_feature_split72_support import (
	temp_workspace as temp_workspace,
)
from operation_feature_split72_support import (
	tempfile as tempfile,
)
from operation_feature_split72_support import (
	temporary_workspace as temporary_workspace,
)
from operation_feature_split72_support import (
	time as time,
)


def test_preparation_consumes_the_original_operation_deadline(
	monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
	now = [100.0]
	monkeypatch.setattr(operation.time, "monotonic", lambda: now[0])
	gate = GateOperation(tmp_path, "check", timeout_seconds=1)
	with gate:
		with gate.stage("manifest.readiness", "staged manifest"):
			pass
		with gate.stage("candidate.prepare", "staged candidate"):
			now[0] = 102.0
		gate.results.append(CheckResult("unexpected.check", Status.PASSED, "must not run"))
	verdict = gate.verdict()
	assert verdict.exit_code == EXIT_UNCHECKED
	assert not any(result.check_id == "unexpected.check" for result in verdict.results)
	assert verdict.operation is not None
	assert verdict.operation.timed_out
	assert verdict.operation.current_stage == "candidate.prepare"
	assert verdict.operation.last_completed_stage == "manifest.readiness"
	assert verdict.operation.elapsed_ms == PREPARATION_ELAPSED_MS


def test_command_timeout_keeps_the_manifest_integer_for_snapshot_contract(
	tmp_path: Path,
) -> None:
	gate = GateOperation(tmp_path, "check")
	gate.configure_budget(12.0, 120)

	assert gate.command_timeout_seconds == 120
	assert isinstance(gate.command_timeout_seconds, int)


def test_unstarted_planned_check_has_an_unchecked_status_and_not_run_disposition(
	tmp_path: Path,
) -> None:
	gate = GateOperation(tmp_path, "check")
	with gate:
		gate.plan_checks((("python.component_1.pytest", "quality_gate; tests=tests"),))
		gate.mark_incomplete("policy.cache-validation", "policy cache validation failed")
	result = next(
		item for item in gate.verdict().results if item.check_id == "python.component_1.pytest"
	)
	assert result.status is Status.UNCHECKED
	assert result.telemetry is not None
	assert result.telemetry.disposition == "not-run"
	assert result.telemetry.scope == "quality_gate; tests=tests"


def test_deadline_keeps_completed_failed_finding_and_marks_later_check_not_run(
	tmp_path: Path,
) -> None:
	gate = GateOperation(tmp_path, "check")
	with gate:
		gate.plan_checks(
			(
				("python.component_1.mypy", "quality_gate"),
				("python.component_1.pytest", "quality_gate; tests=tests"),
			)
		)
		with gate.stage("python.component_1.mypy", "quality_gate", check=True):
			pass
		gate.record_result(
			CheckResult(
				"python.component_1.mypy",
				Status.FAILED,
				"type error",
				findings=(Finding(path="example.py", line=1, message="expected int"),),
				recovery_action="repair the type and rerun mypy",
			)
		)
		gate.mark_incomplete("python.component_1.pytest", "operation deadline expired")
	verdict = gate.verdict()
	mypy = next(item for item in verdict.results if item.check_id.endswith(".mypy"))
	pytest_result = next(item for item in verdict.results if item.check_id.endswith(".pytest"))
	assert mypy.status is Status.FAILED
	assert mypy.findings[0].message == "expected int"
	assert mypy.telemetry is not None and mypy.telemetry.disposition == "executed"
	assert pytest_result.status is Status.UNCHECKED
	assert pytest_result.telemetry is not None and pytest_result.telemetry.disposition == "not-run"


def test_stage_exit_timeout_retains_the_failed_callback_result(
	monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
	now = [100.0]
	monkeypatch.setattr(operation.time, "monotonic", lambda: now[0])
	gate = GateOperation(tmp_path, "check", timeout_seconds=1)

	def fail_check() -> CheckResult:
		now[0] = 102.0
		return CheckResult(
			"python.component_1.mypy",
			Status.FAILED,
			"type error",
			findings=(Finding(path="example.py", message="expected int"),),
			recovery_action="repair the type and rerun mypy",
		)

	with gate:
		operation.observe_check("python.component_1.mypy", "quality_gate", fail_check)

	verdict = gate.verdict()
	mypy = next(item for item in verdict.results if item.check_id.endswith(".mypy"))
	assert mypy.status is Status.FAILED
	assert mypy.findings[0].message == "expected int"
	assert mypy.telemetry is not None and mypy.telemetry.disposition == "executed"
	assert verdict.operation is not None and verdict.operation.timed_out
	assert verdict.exit_code != 0


def test_command_stage_exit_timeout_retains_failed_tool_finding(
	monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
	now = [100.0]
	monkeypatch.setattr(operation.time, "monotonic", lambda: now[0])
	gate = GateOperation(tmp_path, "check", timeout_seconds=1)

	def fail_command(*_args: object, **_kwargs: object) -> None:
		gate._mark_process_started()
		now[0] = 102.0
		raise runner.QualityGateError("observed type error", exit_code=1)

	monkeypatch.setattr(runner, "run", fail_command)
	with gate:
		runner._execute_commands(
			tmp_path,
			{"MYPY_CACHE_DIR": str(tmp_path / "cache")},
			[("python.component_1.mypy", [sys.executable, "-m", "mypy"], 120)],
			120,
			gate.deadline,
		)

	verdict = gate.verdict()
	mypy = next(item for item in verdict.results if item.check_id.endswith(".mypy"))
	assert mypy.status is Status.FAILED
	assert any("observed type error" in item.message for item in mypy.findings)
	assert mypy.telemetry is not None and mypy.telemetry.disposition == "executed"
	assert verdict.operation is not None and verdict.operation.timed_out
	assert verdict.exit_code != 0


def test_expired_logical_check_is_not_executed(tmp_path: Path) -> None:
	gate = GateOperation(tmp_path, "check", cleanup_seconds=1)
	with gate:
		gate.deadline = time.monotonic() - 1
		operation.observe_check(
			"repository.fixture", "staged files", lambda: pytest.fail("expired check ran")
		)
	result = next(item for item in gate.verdict().results if item.check_id == "repository.fixture")
	assert result.status is Status.UNCHECKED
	assert result.telemetry is not None and result.telemetry.disposition == "not-run"


def test_preparation_reports_policy_and_runtime_cache_validation(
	monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
	fixture = Path(__file__).parent / "fixtures" / "valid"
	policy_root = tmp_path / "policy"
	policy_root.mkdir()

	class FakePolicyCache:
		def __init__(self, cache_dir: Path | None) -> None:
			self.requested_root = cache_dir

		def select(self, _version: str) -> Path:
			return policy_root

	class FakeRuntimeManager:
		root = tmp_path / "runtimes"

		def __init__(self, _cache: FakePolicyCache) -> None:
			pass

		def inspect(self, _root: Path, _identity: object) -> RuntimeInspection:
			return RuntimeInspection(self.root / "current", self.root / "python.exe", True)

	monkeypatch.setattr(launcher, "PolicyCache", FakePolicyCache)
	monkeypatch.setattr(launcher, "load_release_manifest", lambda _root: object())
	monkeypatch.setattr(launcher, "RuntimeManager", FakeRuntimeManager)
	monkeypatch.setattr(launcher, "runtime_identity", lambda *_args, **_kwargs: object())
	gate = GateOperation(fixture, "check")
	with gate:
		prepared = launcher.prepare(fixture, cache_dir=tmp_path / "cache")
	assert prepared.policy_root == policy_root
	verdict = gate.verdict()
	assert verdict.operation is not None
	stages = {timing.stage: timing for timing in verdict.operation.stages}
	assert stages["policy.cache-lookup-validation"].tool_cache == "verified cache hit"
	assert stages["policy.release-manifest-validation"].tool_cache == "release inventory validated"
	runtime_stage = stages["runtime.cache-validation.component_1"]
	assert runtime_stage.disposition == "executed"
	assert runtime_stage.tool_cache == "validated hit"


def test_registered_roots_share_one_cleanup_and_leave_unowned_content(tmp_path: Path) -> None:
	unowned = tmp_path / "keep.txt"
	unowned.write_text("keep", encoding="utf-8")
	gate = GateOperation(tmp_path, "check", cleanup_seconds=3)
	with gate:
		with temporary_workspace(
			tmp_path,
			workspace_registered=gate.register_workspace,
			cleanup_owner=gate.handoff_workspace,
		) as owned:
			child = owned / "candidate"
			child.mkdir()
			(child / "read-only.txt").write_text("temporary", encoding="utf-8")
			(child / "read-only.txt").chmod(0o444)
			gate.register_cleanup(child)
		gate.results.append(CheckResult("core.check", Status.PASSED, "check passed"))
	verdict = gate.verdict()
	assert not owned.exists(), verdict.results
	assert unowned.read_text(encoding="utf-8") == "keep"
	assert verdict.exit_code == 0
	assert verdict.operation is not None
	assert len([stage for stage in verdict.operation.stages if stage.stage == "cleanup"]) == 1


@pytest.mark.parametrize("relative_system_temp", [False, True])
def test_read_only_workspace_fallback_is_owned_until_shared_cleanup(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative_system_temp: bool
) -> None:
	repository = tmp_path / "read-only-repository"
	repository.mkdir()
	repository_temp = repository / "temp"
	repository_temp.mkdir()
	lock = repository_temp / ".quality-gate.lock"
	lock.write_bytes(b"\0")
	system_temp = tmp_path / "system-temp"
	system_temp.mkdir()
	if relative_system_temp:
		monkeypatch.chdir(tmp_path)
		system_temp = Path("system-temp")
	monkeypatch.setattr(temp_workspace, "SYSTEM_TEMP_DIRECTORY", system_temp)
	fallback_root = temp_workspace._fallback_workspace_root(repository.resolve())
	gate = GateOperation(repository, "check", cleanup_seconds=3)
	lock.chmod(0o444)
	try:
		try:
			with lock.open("r+b"):
				pytest.skip("effective user can write the read-only lock")
		except PermissionError:
			pass
		with gate:
			with temporary_workspace(
				repository,
				deadline=gate.deadline,
				workspace_registered=gate.register_workspace,
				cleanup_owner=gate.handoff_workspace,
			) as workspace:
				assert workspace.parent.resolve() == (fallback_root / "temp").resolve()
				assert workspace.is_dir()
	finally:
		lock.chmod(0o644)
	verdict = gate.verdict()

	assert verdict.exit_code == 0
	assert not workspace.exists()
	assert verdict.operation is not None
	assert len([stage for stage in verdict.operation.stages if stage.stage == "cleanup"]) == 1


def test_python_temporary_directory_is_removed_by_shared_cleanup(tmp_path: Path) -> None:
	gate = GateOperation(tmp_path, "check", cleanup_seconds=3)
	with gate:
		with temporary_workspace(
			tmp_path,
			workspace_registered=gate.register_workspace,
			cleanup_owner=gate.handoff_workspace,
		):
			with runner.temporary_directory(tmp_path) as temporary_path:
				temporary_root = Path(temporary_path)
				(temporary_root / "output.txt").write_text("temporary", encoding="utf-8")
			assert temporary_root.is_dir(), "nested tool temp must survive until shared cleanup"
	verdict = gate.verdict()

	assert verdict.exit_code == 0
	assert not temporary_root.exists()
	assert verdict.operation is not None
	cleanup = [stage for stage in verdict.operation.stages if stage.stage == "cleanup"]
	assert len(cleanup) == 1
	assert cleanup[0].disposition == "executed"


def test_cleanup_failure_retains_completed_red_findings(tmp_path: Path) -> None:
	gate = GateOperation(tmp_path, "check", cleanup_seconds=0)
	with gate:
		with temporary_workspace(
			tmp_path,
			workspace_registered=gate.register_workspace,
			cleanup_owner=gate.handoff_workspace,
		) as owned:
			gate.results.append(
				CheckResult(
					"python.component_1.mypy",
					Status.FAILED,
					"wrong return type",
					findings=(Finding(path="example.py", line=1, message="expected int"),),
					recovery_action="repair the return type",
				)
			)
	verdict = gate.verdict()
	assert verdict.exit_code == EXIT_UNCHECKED
	assert verdict.results[0].status is Status.FAILED
	assert verdict.results[0].findings[0].message == "expected int"
	assert any(result.check_id == "runtime.cleanup" for result in verdict.results)
	assert owned.is_dir()
	with temporary_workspace(tmp_path):
		pass
	assert not owned.exists()


def test_cleanup_stage_reports_time_spent_in_the_shared_cleanup_phase(
	monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
	gate = GateOperation(tmp_path, "check", cleanup_seconds=3)
	owned = tmp_path / "temp" / "qg-cleanup-test"
	gate._roots.add((tmp_path / "temp", owned))

	def finish_cleanup(_roots: list[tuple[Path, Path]], _end: float) -> None:
		assert gate._cleanup_started is not None
		gate._cleanup_started -= 0.375
		gate._cleanup_process_started = True

	monkeypatch.setattr(gate, "_remove_roots", finish_cleanup)
	with gate:
		pass
	verdict = gate.verdict()
	assert verdict.operation is not None
	cleanup = next(stage for stage in verdict.operation.stages if stage.stage == "cleanup")
	assert cleanup.elapsed_ms >= 375
	assert cleanup.disposition == "executed"


@pytest.mark.parametrize("replaced", [False, True])
def test_snapshot_cleanup_uses_authenticated_workspace(
	tmp_path: Path,
	replaced: bool,
) -> None:
	with GateOperation(tmp_path, "check", cleanup_seconds=3) as gate:
		with temporary_workspace(
			tmp_path,
			workspace_registered=gate.register_workspace,
			cleanup_owner=gate.handoff_workspace,
		) as workspace:
			staging = workspace / "quality-gate-candidate"
			staging.mkdir()
			(staging / "staged.txt").write_text("candidate", encoding="utf-8")
			gate.register_cleanup(staging)
		if replaced:
			workspace.rename(workspace.with_name("displaced-workspace"))
			staging.mkdir(parents=True)
			(staging / "foreign.txt").write_text("foreign", encoding="utf-8")
	verdict = gate.verdict()
	if replaced:
		assert (staging / "foreign.txt").read_text(encoding="utf-8") == "foreign"
		assert verdict.exit_code == EXIT_UNCHECKED
	else:
		assert not staging.exists()
		assert verdict.exit_code == 0
	assert verdict.operation is not None
	cleanup = next(stage for stage in verdict.operation.stages if stage.stage == "cleanup")
	assert cleanup.disposition == "executed"


def test_cleanup_refuses_a_root_without_operation_ownership(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
	protected = tmp_path / "quality-gate-candidate-unregistered"
	protected.mkdir()
	with GateOperation(tmp_path, "check") as gate:
		with pytest.raises(ValueError, match="owned"):
			gate.register_cleanup(protected)
	assert protected.is_dir()


def test_workspace_registration_failure_reclaims_the_new_workspace(tmp_path: Path) -> None:
	def reject_registration(_temp_root: Path, _run_directory: Path) -> None:
		raise RuntimeError("registration rejected")

	with pytest.raises(TemporaryWorkspaceError, match="could not be registered"):
		with temporary_workspace(tmp_path, workspace_registered=reject_registration):
			pytest.fail("workspace registration must complete before yielding")
	assert not tuple((tmp_path / "temp").glob("qg-*"))
