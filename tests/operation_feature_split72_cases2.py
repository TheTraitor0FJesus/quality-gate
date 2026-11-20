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


def test_operation_timeout_reaps_a_real_grandchild_before_reporting(tmp_path: Path) -> None:
	base_python = Path(sys.base_prefix) / "python.exe" if os.name == "nt" else Path(sys.executable)
	marker = tmp_path / "grandchild.pid"
	body = (
		"import subprocess,sys,time; from pathlib import Path; "
		"child=subprocess.Popen([sys.executable,'-I','-S','-B','-c',"
		"'import time; time.sleep(30)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
		f"Path({str(marker)!r}).write_text(str(child.pid)); time.sleep(30)"
	)
	gate = GateOperation(tmp_path, "check", timeout_seconds=1, cleanup_seconds=1)
	started = time.monotonic()
	try:
		with gate:
			with gate.stage("preparation.probe", "runtime readiness"):
				gate.run_process(
					[str(base_python), "-I", "-S", "-B", "-c", body],
					root=tmp_path,
					environment=dict(os.environ),
					timeout_seconds=30,
				)
		verdict = gate.verdict()
		assert marker.is_file(), "The grandchild must start before the supervised timeout."
		assert not process_alive(int(marker.read_text()))
		assert verdict.exit_code == EXIT_UNCHECKED
		assert verdict.operation is not None
		assert verdict.operation.current_stage == "preparation.probe"
		assert verdict.operation.timed_out
		assert time.monotonic() - started < TREE_TERMINATION_BOUND_SECONDS
	finally:
		if marker.exists() and process_alive(int(marker.read_text())):
			os.kill(int(marker.read_text()), signal.SIGTERM)


def test_runner_command_cannot_renew_the_operation_work_budget(tmp_path: Path) -> None:
	base_python = Path(sys.base_prefix) / "python.exe" if os.name == "nt" else Path(sys.executable)
	gate = GateOperation(tmp_path, "check", timeout_seconds=0.2, cleanup_seconds=1)
	started = time.monotonic()
	with gate:
		with gate.stage("preparation.probe", "runtime readiness"):
			runner.run(
				[str(base_python), "-I", "-S", "-B", "-c", "import time; time.sleep(3)"],
				tmp_path,
				dict(os.environ),
				timeout=30,
			)
	assert gate.verdict().exit_code == EXIT_UNCHECKED
	assert time.monotonic() - started < PROBE_TERMINATION_BOUND_SECONDS


def test_runner_reaps_descendants_when_the_command_parent_exits(tmp_path: Path) -> None:
	base_python = Path(sys.base_prefix) / "python.exe" if os.name == "nt" else Path(sys.executable)
	marker = tmp_path / "orphan.pid"
	body = (
		"import subprocess,sys; from pathlib import Path; "
		"child=subprocess.Popen([sys.executable,'-I','-S','-B','-c',"
		"'import time; time.sleep(30)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
		f"Path({str(marker)!r}).write_text(str(child.pid)); print('completed')"
	)
	try:
		with GateOperation(tmp_path, "check", timeout_seconds=3) as gate:
			with gate.stage("python.component_1.pytest", "tests", check=True):
				output = runner.run(
					[str(base_python), "-I", "-S", "-B", "-c", body],
					tmp_path,
					dict(os.environ),
					timeout=30,
				)
		assert output.strip() == "completed"
		assert marker.is_file()
		pid = int(marker.read_text())
		cleanup_deadline = time.monotonic() + 1
		while process_alive(pid) and time.monotonic() < cleanup_deadline:
			time.sleep(0.01)
		assert not process_alive(pid)
	finally:
		if marker.exists() and process_alive(int(marker.read_text())):
			os.kill(int(marker.read_text()), signal.SIGTERM)


def test_bounded_runner_output_preserves_a_completed_failed_outcome(tmp_path: Path) -> None:
	base_python = Path(sys.base_prefix) / "python.exe" if os.name == "nt" else Path(sys.executable)
	with GateOperation(tmp_path, "check", timeout_seconds=3) as gate:
		with gate.stage("python.component_1.mypy", "quality_gate", check=True):
			with pytest.raises(runner.QualityGateError) as observed:
				runner.run(
					[
						str(base_python),
						"-I",
						"-S",
						"-B",
						"-c",
						"import sys; print('finding ' + 'x' * 300000); sys.exit(1)",
					],
					tmp_path,
					dict(os.environ),
					timeout=30,
				)
	assert observed.value.exit_code == 1
	assert "finding" in str(observed.value)
	assert "[output truncated]" in str(observed.value)


def test_readiness_probe_consumes_operation_deadline(tmp_path: Path) -> None:
	base_python = Path(sys.base_prefix) / "python.exe" if os.name == "nt" else Path(sys.executable)
	gate = GateOperation(tmp_path, "check", timeout_seconds=0.2, cleanup_seconds=1)
	started = time.monotonic()
	with gate:
		with gate.stage("readiness.probe", "runtime readiness"):
			operation.run_probe(
				[str(base_python), "-I", "-S", "-B", "-c", "import time; time.sleep(3)"],
				cwd=tmp_path,
				timeout=30,
			)
	assert gate.verdict().exit_code == EXIT_UNCHECKED
	assert time.monotonic() - started < PROBE_TERMINATION_BOUND_SECONDS


def test_scanner_consumes_operation_deadline(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	release = tmp_path / "release"
	release.mkdir()
	scanner = release / "gitleaks.exe"
	scanner.write_bytes(b"native test double")
	prepared = SimpleNamespace(
		policy_root=release,
		release_manifest=SimpleNamespace(
			tools=(SimpleNamespace(name="gitleaks", path=scanner.name),)
		),
	)
	manifest = load_manifest(Path(__file__).parent / "fixtures" / "valid")
	seen: list[float] = []

	# Only the native process boundary is controlled; scanner selection and transport are real.
	def native_process(
		_command: list[str], *, deadline: float, **_kwargs: object
	) -> tuple[int, bytes, bytes]:
		seen.append(deadline)
		return 0, b"[]", b""

	monkeypatch.setattr(secrets_module, "run_process", native_process)
	monkeypatch.setattr(operation, "supervise_process", native_process)
	gate = GateOperation(tmp_path, "check", timeout_seconds=0.5)
	with gate:
		result = secrets_module.secret_candidate_result(tmp_path, manifest, prepared)
	assert result.status is Status.PASSED
	assert len(seen) == 1
	assert seen[0] <= gate.deadline
