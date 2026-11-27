from __future__ import annotations

import os
import threading
import time
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_temp_workspace import _start_workspace_script, _wait_for_workspace_marker

import quality_gate.temp_workspace as temp_workspace
import quality_gate.workspace_windows as workspace_windows
from quality_gate.edit_protocol import EXIT_UNCHECKED
from quality_gate.operation import GateOperation
from quality_gate.temp_workspace import (
	TemporaryWorkspaceError,
	cleanup_handed_off_workspace,
	temporary_workspace,
)


def test_temporary_workspace_handoff_is_reclaimed_by_the_verified_owner(
	tmp_path: Path,
) -> None:
	transferred: list[tuple[Path, Path]] = []
	foreign = tmp_path / "temp" / "keep.txt"
	foreign.parent.mkdir()
	foreign.write_text("keep", encoding="utf-8")
	with pytest.raises(RuntimeError, match="body failed"):
		with temporary_workspace(
			tmp_path,
			cleanup_owner=lambda temp_root, run_directory: transferred.append(
				(temp_root, run_directory)
			),
		) as workspace:
			(workspace / "candidate.txt").write_text("temporary", encoding="utf-8")
			raise RuntimeError("body failed")
	assert workspace.is_dir()
	assert len(transferred) == 1
	cleanup_handed_off_workspace(*transferred[0])
	assert not workspace.exists()
	assert foreign.read_text(encoding="utf-8") == "keep"


def test_temporary_workspace_handoff_failure_does_not_delete_locally(
	tmp_path: Path,
) -> None:
	def reject_transfer(_temp_root: Path, _run_directory: Path) -> None:
		raise RuntimeError("handoff rejected")

	with pytest.raises(TemporaryWorkspaceError) as observed:
		with temporary_workspace(tmp_path, cleanup_owner=reject_transfer):
			pass
	assert isinstance(observed.value.__cause__, RuntimeError)
	remaining = tuple((tmp_path / "temp").glob("qg-*"))
	assert len(remaining) == 1
	with temporary_workspace(tmp_path):
		pass
	assert not remaining[0].exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows delete-sharing handles")
def test_workspace_cleanup_unlinks_a_file_with_a_delete_sharing_reader(tmp_path: Path) -> None:
	kernel, _advapi = workspace_windows._windows_security_api()
	handle = None
	try:
		with temporary_workspace(tmp_path) as workspace:
			path = workspace / "venv" / "Scripts" / "python.exe"
			path.parent.mkdir(parents=True)
			path.write_bytes(b"owned temporary data")
			handle = kernel.CreateFileW(
				str(path),
				0x80000000,  # GENERIC_READ
				workspace_windows._WINDOWS_SHARE_READ
				| workspace_windows._WINDOWS_SHARE_WRITE
				| workspace_windows._WINDOWS_SHARE_DELETE,
				None,
				workspace_windows._WINDOWS_OPEN_EXISTING,
				0,
				None,
			)
			assert handle not in (None, wintypes.HANDLE(-1).value)
		assert not workspace.exists()
		# The reader remains valid after the owned name is removed.
		workspace_windows._windows_file_identity(handle, kernel)
	finally:
		if handle not in (None, wintypes.HANDLE(-1).value):
			kernel.CloseHandle(handle)
		with temporary_workspace(tmp_path):
			pass


@pytest.mark.skipif(os.name != "nt", reason="Windows native disposition errors")
@pytest.mark.parametrize("error_code,unsupported", [(1, True), (50, True), (87, True), (5, False)])
def test_windows_disposition_falls_back_only_when_unsupported(
	error_code: int, unsupported: bool
) -> None:
	calls: list[int] = []

	def set_information(_handle: int, kind: int, _data: object, _size: int) -> bool:
		calls.append(kind)
		if kind == 21:  # FileDispositionInfoEx
			workspace_windows.ctypes.set_last_error(error_code)
			return False
		return True

	kernel = SimpleNamespace(SetFileInformationByHandle=set_information)
	if unsupported:
		workspace_windows._mark_windows_handle_for_deletion(1, kernel)
		assert calls == [21, 4]
	else:
		with pytest.raises(OSError) as observed:
			workspace_windows._mark_windows_handle_for_deletion(1, kernel)
		assert observed.value.winerror == error_code
		assert calls == [21]


def test_temporary_workspace_root_lock_wait_uses_the_operation_deadline(
	tmp_path: Path,
) -> None:
	repository = tmp_path / "repository"
	temp_root = repository / "temp"
	temp_root.mkdir(parents=True)
	root_lock = temp_root / temp_workspace._ROOT_LOCK_NAME
	ready = tmp_path / "root-lock-ready"
	script = "\n".join(
		(
			"import os, sys, time",
			"from pathlib import Path",
			"handle = Path(sys.argv[1]).open('a+b')",
			"handle.seek(0, os.SEEK_END)",
			"if handle.tell() == 0: handle.write(b'\\0'); handle.flush()",
			"handle.seek(0)",
			"if os.name == 'nt':",
			"    import msvcrt",
			"    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)",
			"else:",
			"    import fcntl",
			"    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)",
			"Path(sys.argv[2]).write_text('ready', encoding='utf-8')",
			"time.sleep(30)",
		)
	)
	process = _start_workspace_script(script, dict(os.environ), str(root_lock), str(ready))
	try:
		_wait_for_workspace_marker(ready, process)
		started = time.monotonic()
		with pytest.raises(TemporaryWorkspaceError, match="deadline"):
			with temporary_workspace(repository, deadline=started + 0.25):
				pytest.fail("workspace creation must stop at the locked root deadline")
		assert time.monotonic() - started < 2
		assert not tuple(temp_root.glob("qg-*"))
	finally:
		process.terminate()
		process.communicate(timeout=10)


def test_temporary_workspace_process_lock_wait_uses_the_operation_deadline(
	tmp_path: Path,
) -> None:
	locked = threading.Event()
	release = threading.Event()

	def hold_process_lock() -> None:
		temp_workspace._process_lock.acquire()
		locked.set()
		try:
			release.wait(timeout=1.5)
		finally:
			temp_workspace._process_lock.release()

	locker = threading.Thread(target=hold_process_lock)
	locker.start()
	try:
		assert locked.wait(timeout=2)
		started = time.monotonic()
		with pytest.raises((TemporaryWorkspaceError, TimeoutError), match="deadline"):
			with temporary_workspace(tmp_path, deadline=started + 0.2):
				pytest.fail("workspace preparation must stop at the process-lock deadline")
		elapsed = time.monotonic() - started
	finally:
		release.set()
		locker.join(timeout=2)

	assert not locker.is_alive()
	assert elapsed < 1


def test_gate_workspace_preparation_reaps_locked_root_probe_at_deadline(
	tmp_path: Path,
) -> None:
	repository = tmp_path / "repository"
	temp_root = repository / "temp"
	temp_root.mkdir(parents=True)
	root_lock = temp_root / temp_workspace._ROOT_LOCK_NAME
	ready = tmp_path / "gate-root-lock-ready"
	script = "\n".join(
		(
			"import os, sys, time",
			"from pathlib import Path",
			"handle = Path(sys.argv[1]).open('a+b')",
			"handle.seek(0, os.SEEK_END)",
			"if handle.tell() == 0: handle.write(b'\\0'); handle.flush()",
			"handle.seek(0)",
			"if os.name == 'nt':",
			"    import msvcrt",
			"    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)",
			"else:",
			"    import fcntl",
			"    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)",
			"Path(sys.argv[2]).write_text('ready', encoding='utf-8')",
			"time.sleep(30)",
		)
	)
	process = _start_workspace_script(script, dict(os.environ), str(root_lock), str(ready))
	gate = GateOperation(repository, "check", timeout_seconds=0.25, cleanup_seconds=1)
	started = time.monotonic()
	try:
		_wait_for_workspace_marker(ready, process)
		with gate:
			with gate.stage("temporary.workspace", "repository temporary workspace"):
				with temporary_workspace(repository, deadline=gate.deadline):
					pytest.fail("workspace preparation must stop at the operation deadline")
		verdict = gate.verdict()
		assert verdict.exit_code == EXIT_UNCHECKED
		assert verdict.operation is not None
		assert verdict.operation.timed_out
		assert verdict.operation.current_stage == "temporary.workspace"
		assert time.monotonic() - started < 2
		assert not tuple(temp_root.glob("qg-*"))
	finally:
		process.terminate()
		process.communicate(timeout=10)
