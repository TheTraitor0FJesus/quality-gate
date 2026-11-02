from __future__ import annotations

import io
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, redirect_stderr
from pathlib import Path

import pytest

from quality_gate import admission, runner
from quality_gate.admission import AdmissionConfigurationError, AdmissionError

DEFAULT_QUEUE_WAIT_SECONDS = 120
MAX_QUEUE_WAIT_SECONDS = 240
MAX_RELEASE_CLEANUP_SECONDS = 2
UNCHECKED_EXIT = 2
_HOLDER_SCRIPT = "\n".join(
	(
		"import sys",
		"from pathlib import Path",
		"from quality_gate.admission import gate_admission",
		"with gate_admission(cache_root=Path(sys.argv[1])):",
		"\tPath(sys.argv[2]).write_text('acquired', encoding='ascii')",
		"\tsys.stdin.readline()",
	)
)
_CONTROL_HOLDER_SCRIPT = "\n".join(
	(
		"import sys",
		"from pathlib import Path",
		"from quality_gate import admission",
		"root = Path(sys.argv[1])",
		"root.mkdir(parents=True, exist_ok=True)",
		"handle = admission._try_open_locked_file(root / admission._CONTROL_LOCK_NAME)",
		"if handle is None: raise RuntimeError('control lock was unavailable')",
		"Path(sys.argv[2]).write_text('locked', encoding='ascii')",
		"sys.stdin.readline()",
		"admission._unlock(handle)",
	)
)
_SLOT_PROBE_SCRIPT = "\n".join(
	(
		"import sys",
		"from pathlib import Path",
		"from quality_gate import admission",
		"root = Path(sys.argv[1])",
		"slot = int(sys.argv[2])",
		"handle = admission._try_open_slot(root, slot)",
		"print('occupied' if handle is None else 'available')",
		"if handle is not None:",
		"    admission._unlock(handle, lock_offset=admission._SLOT_LOCK_OFFSET)",
	)
)


def _start_holder(
	cache_root: Path,
	repository: Path,
	*,
	inherit_parent_lease: bool = False,
) -> tuple[subprocess.Popen[str], Path]:
	repository.mkdir(parents=True)
	ready = repository / "admission-ready"
	environment = os.environ.copy()
	environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
	if inherit_parent_lease:
		environment.update(admission.child_admission_environment())
	process = subprocess.Popen(
		[sys.executable, "-B", "-c", _HOLDER_SCRIPT, str(cache_root), str(ready)],
		cwd=repository,
		env=environment,
		stdin=subprocess.PIPE,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.PIPE,
		text=True,
	)
	deadline = time.monotonic() + 10
	while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
		time.sleep(0.01)
	if not ready.exists():
		process.kill()
		_, errors = process.communicate(timeout=10)
		raise AssertionError(f"admission owner did not acquire a slot: {errors}")
	return process, ready


def _start_control_holder(
	cache_root: Path,
	repository: Path,
) -> tuple[subprocess.Popen[str], Path]:
	repository.mkdir(parents=True)
	ready = repository / "admission-control-ready"
	environment = os.environ.copy()
	environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
	process = subprocess.Popen(
		[sys.executable, "-B", "-c", _CONTROL_HOLDER_SCRIPT, str(cache_root), str(ready)],
		cwd=repository,
		env=environment,
		stdin=subprocess.PIPE,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.PIPE,
		text=True,
	)
	deadline = time.monotonic() + 10
	while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
		time.sleep(0.01)
	if not ready.exists():
		process.kill()
		_, errors = process.communicate(timeout=10)
		raise AssertionError(f"control-lock owner did not acquire the lock: {errors}")
	return process, ready


def _probe_slot(cache_root: Path, slot: int) -> str:
	environment = os.environ.copy()
	environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
	result = subprocess.run(
		[sys.executable, "-B", "-c", _SLOT_PROBE_SCRIPT, str(cache_root), str(slot)],
		cwd=cache_root,
		env=environment,
		capture_output=True,
		text=True,
		timeout=10,
		check=False,
	)
	assert result.returncode == 0, result.stderr
	return result.stdout.strip()


def _stop_holders(
	holders: list[tuple[subprocess.Popen[str], Path]],
) -> None:
	for process, _ready in holders:
		if process.poll() is None and process.stdin is not None:
			process.stdin.write("release\n")
			process.stdin.flush()
			process.stdin.close()
	for process, _ready in holders:
		if process.poll() is None:
			try:
				process.wait(10)
			except subprocess.TimeoutExpired:
				process.terminate()
				process.wait(10)
		if process.stderr is not None:
			process.stderr.close()


@contextmanager
def _full_capacity(
	tmp_path: Path,
) -> Iterator[list[tuple[subprocess.Popen[str], Path]]]:
	cache_root = tmp_path / "machine-cache" / "quality-gate"
	holders = [
		_start_holder(cache_root, tmp_path / "repository-a"),
		_start_holder(cache_root, tmp_path / "repository-b"),
	]
	try:
		yield holders
	finally:
		_stop_holders(holders)


def test_capacity_is_shared_across_repository_processes_and_waiter_continues(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	monkeypatch.setenv(admission.QUEUE_TIMEOUT_ENV, "10")
	with _full_capacity(tmp_path) as holders:
		cache_root = tmp_path / "machine-cache" / "quality-gate"
		waiting_output = io.StringIO()
		continued = threading.Event()
		waiter = threading.Thread(target=lambda: _wait_for_slot(cache_root, continued), daemon=True)
		with redirect_stderr(waiting_output):
			waiter.start()
			assert _wait_until(lambda: "waiting for a local run slot" in waiting_output.getvalue())
			assert not continued.is_set()
			_release_holder(holders[0])
			assert continued.wait(10)
			waiter.join(10)
		assert "has not started" in waiting_output.getvalue()
		assert "slot acquired; continuing" in waiting_output.getvalue()


def _wait_for_slot(cache_root: Path, continued: threading.Event) -> None:
	with admission.gate_admission(cache_root=cache_root):
		continued.set()


def _release_holder(holder: tuple[subprocess.Popen[str], Path]) -> None:
	process = holder[0]
	if process.stdin is not None:
		process.stdin.write("release\n")
		process.stdin.flush()


def _wait_until(predicate: Callable[[], bool], timeout_seconds: float = 5) -> bool:
	deadline = time.monotonic() + timeout_seconds
	while time.monotonic() < deadline:
		if predicate():
			return True
		time.sleep(0.01)
	return False


def test_queue_exhaustion_is_visible_and_does_not_enter_the_gate(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
	monkeypatch.setenv(admission.QUEUE_TIMEOUT_ENV, "0")
	with _full_capacity(tmp_path):
		with pytest.raises(AdmissionError, match="no check was run"):
			with admission.gate_admission(cache_root=tmp_path / "machine-cache" / "quality-gate"):
				pytest.fail("the full queue admitted an independent third run")

	assert "waiting for a local run slot" in capsys.readouterr().err


def test_wait_cancellation_is_visible_and_releases_no_slot(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	monkeypatch.setenv(admission.QUEUE_TIMEOUT_ENV, "10")
	with _full_capacity(tmp_path):
		with monkeypatch.context() as cancellation:
			cancellation.setattr(
				admission.time,
				"sleep",
				lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt),
			)
			with pytest.raises(AdmissionError, match="cancelled"):
				with admission.gate_admission(
					cache_root=tmp_path / "machine-cache" / "quality-gate"
				):
					pytest.fail("a cancelled waiter entered the gate")

	assert "cancelled; no check was run" in capsys.readouterr().err


def test_owner_death_releases_slot_for_the_next_run(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	monkeypatch.setenv(admission.QUEUE_TIMEOUT_ENV, "0")
	cache_root = tmp_path / "machine-cache" / "quality-gate"
	process, _ready = _start_holder(cache_root, tmp_path / "owner-that-exits")
	process.kill()
	process.wait(10)
	if process.stdin is not None:
		process.stdin.close()
	if process.stderr is not None:
		process.stderr.close()

	with admission.gate_admission(cache_root=cache_root):
		assert admission.child_admission_environment()["QUALITY_GATE_ADMISSION_TOKEN"]


def test_stale_and_malformed_inherited_flags_do_not_prove_ownership(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	monkeypatch.setenv(admission.QUEUE_TIMEOUT_ENV, "0")
	cache_root = tmp_path / "machine-cache" / "quality-gate"
	monkeypatch.setenv(admission._LEASE_ROOT_ENVIRONMENT, str(cache_root.resolve()))
	monkeypatch.setenv(admission._LEASE_SLOT_ENVIRONMENT, "0")
	monkeypatch.setenv(admission._LEASE_PROOF_ENVIRONMENT, "0" * 32)

	with admission.gate_admission(cache_root=cache_root):
		assert (
			admission.child_admission_environment()[admission._LEASE_PROOF_ENVIRONMENT] != "0" * 32
		)

	monkeypatch.setenv(admission._LEASE_PROOF_ENVIRONMENT, "not-a-valid-proof")
	with pytest.raises(AdmissionConfigurationError, match="malformed"):
		with admission.gate_admission(cache_root=cache_root):
			pytest.fail("a malformed inherited proof was accepted")

	monkeypatch.setenv(admission._LEASE_PROOF_ENVIRONMENT, "0" * 32)
	monkeypatch.delenv(admission._LEASE_PROOF_ENVIRONMENT)
	with pytest.raises(AdmissionConfigurationError, match="incomplete"):
		with admission.gate_admission(cache_root=cache_root):
			pytest.fail("a malformed inherited identity was accepted")


def test_wrong_inherited_token_cannot_reuse_a_live_independent_slot(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	monkeypatch.setenv(admission.QUEUE_TIMEOUT_ENV, "0")
	cache_root = tmp_path / "machine-cache" / "quality-gate"
	with _full_capacity(tmp_path):
		monkeypatch.setenv(admission._LEASE_ROOT_ENVIRONMENT, str(cache_root.resolve()))
		monkeypatch.setenv(admission._LEASE_SLOT_ENVIRONMENT, "0")
		monkeypatch.setenv(admission._LEASE_PROOF_ENVIRONMENT, "f" * 32)
		with pytest.raises(AdmissionError, match="no check was run"):
			with admission.gate_admission(cache_root=cache_root):
				pytest.fail("a wrong inherited identity reused an occupied slot")


def test_wrong_inherited_root_cannot_select_an_unoccupied_queue(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	monkeypatch.setenv(admission.QUEUE_TIMEOUT_ENV, "0")
	cache_root = tmp_path / "machine-cache" / "quality-gate"
	misdirected_root = tmp_path / "other-cache" / "quality-gate"
	monkeypatch.setattr(admission, "default_cache_root", lambda: cache_root)
	with _full_capacity(tmp_path):
		monkeypatch.setenv(admission._LEASE_ROOT_ENVIRONMENT, str(misdirected_root.resolve()))
		monkeypatch.setenv(admission._LEASE_SLOT_ENVIRONMENT, "0")
		monkeypatch.setenv(admission._LEASE_PROOF_ENVIRONMENT, "f" * 32)
		with pytest.raises(AdmissionError, match="no check was run"):
			with admission.gate_admission():
				pytest.fail("an unverified inherited root bypassed machine-wide admission")


def test_explicit_fixture_root_is_independent_of_a_live_parent_lease(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	monkeypatch.setenv(admission.QUEUE_TIMEOUT_ENV, "0")
	parent_root = tmp_path / "parent-cache" / "quality-gate"
	fixture_root = tmp_path / "fixture-cache" / "quality-gate"
	with admission.gate_admission(cache_root=parent_root):
		fixture_holder = _start_holder(
			fixture_root,
			tmp_path / "fixture-repository",
			inherit_parent_lease=True,
		)
		try:
			assert fixture_holder[1].exists()
		finally:
			_stop_holders([fixture_holder])


def test_same_process_nesting_reuses_only_the_active_cache_root(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	monkeypatch.setenv(admission.QUEUE_TIMEOUT_ENV, "0")
	cache_root = tmp_path / "machine-cache" / "quality-gate"
	with admission.gate_admission(cache_root=cache_root):
		identity = admission.child_admission_environment()
		with admission.gate_admission(cache_root=cache_root):
			assert admission.child_admission_environment() == identity
		with pytest.raises(AdmissionConfigurationError, match="different cache root"):
			with admission.gate_admission(cache_root=tmp_path / "other-cache"):
				pytest.fail("nested verification changed the active admission namespace")


def test_gate_error_exit_preserves_error_and_releases_the_slot(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	monkeypatch.setenv(admission.QUEUE_TIMEOUT_ENV, "0")
	cache_root = tmp_path / "machine-cache" / "quality-gate"
	with pytest.raises(RuntimeError, match="Gate command failed"):
		with admission.gate_admission(cache_root=cache_root):
			raise RuntimeError("Gate command failed")

	with admission.gate_admission(cache_root=cache_root):
		assert admission.child_admission_environment()[admission._LEASE_PROOF_ENVIRONMENT]


def test_busy_control_lock_does_not_retain_slot_on_error_exit(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
	monkeypatch.setenv(admission.QUEUE_TIMEOUT_ENV, "0")
	monkeypatch.setattr(admission, "RELEASE_CONTROL_WAIT_SECONDS", 0.05)
	cache_root = tmp_path / "machine-cache" / "quality-gate"
	control_holder: tuple[subprocess.Popen[str], Path] | None = None
	started: float | None = None
	try:
		with pytest.raises(RuntimeError, match="original Gate error"):
			with admission.gate_admission(cache_root=cache_root):
				slot = int(
					admission.child_admission_environment()[admission._LEASE_SLOT_ENVIRONMENT]
				)
				control_holder = _start_control_holder(cache_root, tmp_path / "control-lock-owner")
				started = time.monotonic()
				raise RuntimeError("original Gate error")
		assert started is not None
		assert time.monotonic() - started < MAX_RELEASE_CLEANUP_SECONDS
		assert control_holder is not None and control_holder[0].poll() is None
		assert _probe_slot(cache_root, slot) == "available"
		assert "stale lease data will be replaced" in capsys.readouterr().err
	finally:
		if control_holder is not None:
			_stop_holders([control_holder])


def test_cancellation_during_gate_body_releases_the_slot(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	monkeypatch.setenv(admission.QUEUE_TIMEOUT_ENV, "0")
	cache_root = tmp_path / "machine-cache" / "quality-gate"
	with pytest.raises(KeyboardInterrupt):
		with admission.gate_admission(cache_root=cache_root):
			raise KeyboardInterrupt

	with admission.gate_admission(cache_root=cache_root):
		assert admission.child_admission_environment()[admission._LEASE_PROOF_ENVIRONMENT]


def test_cancellation_during_release_wait_closes_the_slot_handle(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	monkeypatch.setenv(admission.QUEUE_TIMEOUT_ENV, "0")
	cache_root = tmp_path / "machine-cache" / "quality-gate"
	control_holder: tuple[subprocess.Popen[str], Path] | None = None
	try:
		with pytest.raises(KeyboardInterrupt), monkeypatch.context() as cancellation:
			with admission.gate_admission(cache_root=cache_root):
				slot = int(
					admission.child_admission_environment()[admission._LEASE_SLOT_ENVIRONMENT]
				)
				control_holder = _start_control_holder(
					cache_root, tmp_path / "cancelled-control-lock-owner"
				)
				cancellation.setattr(
					admission.time,
					"sleep",
					lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt),
				)
		assert control_holder is not None and control_holder[0].poll() is None
		assert _probe_slot(cache_root, slot) == "available"
	finally:
		if control_holder is not None:
			_stop_holders([control_holder])


@pytest.mark.parametrize("raw", ["-1", "241", "abc", "1.5", " 10", "1000"])
def test_invalid_queue_wait_settings_are_rejected(raw: str) -> None:
	with pytest.raises(AdmissionConfigurationError):
		admission.queue_timeout_seconds({admission.QUEUE_TIMEOUT_ENV: raw})


def test_queue_wait_setting_has_bounded_default_and_zero_wait_option() -> None:
	assert admission.queue_timeout_seconds({}) == DEFAULT_QUEUE_WAIT_SECONDS
	assert admission.queue_timeout_seconds({admission.QUEUE_TIMEOUT_ENV: "0"}) == 0
	assert (
		admission.queue_timeout_seconds({admission.QUEUE_TIMEOUT_ENV: str(MAX_QUEUE_WAIT_SECONDS)})
		== MAX_QUEUE_WAIT_SECONDS
	)


@pytest.mark.parametrize("ci_variable", ["CI", "GITHUB_ACTIONS", "QUALITY_GATE_ADMISSION_CI"])
def test_nested_ci_check_skips_a_full_local_queue(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ci_variable: str
) -> None:
	monkeypatch.setenv(admission.QUEUE_TIMEOUT_ENV, "0")
	for name in ("CI", "GITHUB_ACTIONS", "QUALITY_GATE_ADMISSION_CI"):
		monkeypatch.delenv(name, raising=False)
	project_root = _nested_project(tmp_path / "ci-project")
	with _full_capacity(tmp_path):
		monkeypatch.setenv(ci_variable, "true")
		environment = runner._safe_environment(str(tmp_path / "ci-temporary"))
		assert "CI" not in environment
		assert "GITHUB_ACTIONS" not in environment
		assert admission.running_in_ci(environment)
		# A lost CI context must encounter the occupied fixture queue.
		environment["LOCALAPPDATA"] = str(tmp_path / "machine-cache")
		environment["XDG_CACHE_HOME"] = str(tmp_path / "machine-cache")
		environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
		# Gate runs put tmp_path inside a checkout; keep this non-Git fixture isolated.
		environment["GIT_CEILING_DIRECTORIES"] = str(tmp_path.resolve())
		result = subprocess.run(
			[
				sys.executable,
				"-B",
				"-m",
				"quality_gate.cli",
				"--root",
				str(project_root),
				"check",
			],
			capture_output=True,
			text=True,
			env=environment,
			timeout=10,
			check=False,
		)
	assert result.returncode == UNCHECKED_EXIT
	assert "candidate.snapshot" in result.stdout
	assert "runtime.admission" not in result.stdout


def test_nested_cli_check_uses_its_live_parent_lease(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	monkeypatch.setenv(admission.QUEUE_TIMEOUT_ENV, "0")
	for name in ("CI", "GITHUB_ACTIONS", "QUALITY_GATE_ADMISSION_CI"):
		monkeypatch.delenv(name, raising=False)
	monkeypatch.setenv("LOCALAPPDATA", "unrelated-parent-appdata")
	monkeypatch.setenv("XDG_CACHE_HOME", "unrelated-parent-xdg-cache")
	cache_root = tmp_path / "machine-cache" / "quality-gate"
	other = _start_holder(cache_root, tmp_path / "other-repository")
	project_root = _nested_project(tmp_path / "nested-project")

	try:
		with admission.gate_admission(cache_root=cache_root):
			environment = runner._safe_environment(str(tmp_path / "nested-temporary"))
			assert environment[admission._LEASE_ROOT_ENVIRONMENT] == str(cache_root.resolve())
			assert environment[admission._LEASE_SLOT_ENVIRONMENT] in {"0", "1"}
			assert environment[admission._LEASE_PROOF_ENVIRONMENT]
			assert "LOCALAPPDATA" not in environment
			assert "XDG_CACHE_HOME" not in environment
			assert "CI" not in environment
			assert "GITHUB_ACTIONS" not in environment
			environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
			environment["GIT_CEILING_DIRECTORIES"] = str(tmp_path.resolve())
			result = subprocess.run(
				[
					sys.executable,
					"-B",
					"-m",
					"quality_gate.cli",
					"--root",
					str(project_root),
					"check",
				],
				capture_output=True,
				text=True,
				env=environment,
				timeout=10,
				check=False,
			)
		assert result.returncode == UNCHECKED_EXIT
		assert "candidate.snapshot" in result.stdout
		assert "queue wait" not in result.stdout
	finally:
		_stop_holders([other])


def _nested_project(root: Path) -> Path:
	root.mkdir()
	manifest = Path(__file__).parent / "fixtures" / "no-python" / "quality-gate.toml"
	(root / "quality-gate.toml").write_bytes(manifest.read_bytes())
	return root
