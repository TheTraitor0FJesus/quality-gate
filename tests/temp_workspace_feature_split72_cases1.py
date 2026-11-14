"""Feature test partition."""

from __future__ import annotations

from temp_workspace_feature_split72_support import (
	_INTERRUPTED_AFTER_UNLINK_EXIT as _INTERRUPTED_AFTER_UNLINK_EXIT,
)
from temp_workspace_feature_split72_support import (
	_INTERRUPTED_CLEANUP_EXIT as _INTERRUPTED_CLEANUP_EXIT,
)
from temp_workspace_feature_split72_support import (
	_INTERRUPTED_KEY_EXIT as _INTERRUPTED_KEY_EXIT,
)
from temp_workspace_feature_split72_support import (
	_SECOND_NESTED_LSTAT_CALL as _SECOND_NESTED_LSTAT_CALL,
)
from temp_workspace_feature_split72_support import (
	_SINGLE_LINK_COUNT as _SINGLE_LINK_COUNT,
)
from temp_workspace_feature_split72_support import (
	_TRANSITIONAL_HARD_LINK_COUNT as _TRANSITIONAL_HARD_LINK_COUNT,
)
from temp_workspace_feature_split72_support import (
	EXIT_UNCHECKED as EXIT_UNCHECKED,
)
from temp_workspace_feature_split72_support import (
	SYSTEM_TEMP_DIRECTORY as SYSTEM_TEMP_DIRECTORY,
)
from temp_workspace_feature_split72_support import (
	BinaryIO as BinaryIO,
)
from temp_workspace_feature_split72_support import (
	Callable as Callable,
)
from temp_workspace_feature_split72_support import (
	EditError as EditError,
)
from temp_workspace_feature_split72_support import (
	GateOperation as GateOperation,
)
from temp_workspace_feature_split72_support import (
	Path as Path,
)
from temp_workspace_feature_split72_support import (
	SimpleNamespace as SimpleNamespace,
)
from temp_workspace_feature_split72_support import (
	TemporaryWorkspaceError as TemporaryWorkspaceError,
)
from temp_workspace_feature_split72_support import (
	_apply_publisher_race_final_action as _apply_publisher_race_final_action,
)
from temp_workspace_feature_split72_support import (
	_assert_publisher_race_outcome as _assert_publisher_race_outcome,
)
from temp_workspace_feature_split72_support import (
	_assert_workspace_script_cleaned as _assert_workspace_script_cleaned,
)
from temp_workspace_feature_split72_support import (
	_collect_workspace_process as _collect_workspace_process,
)
from temp_workspace_feature_split72_support import (
	_publisher_race_script as _publisher_race_script,
)
from temp_workspace_feature_split72_support import (
	_recover_while_publisher_paused as _recover_while_publisher_paused,
)
from temp_workspace_feature_split72_support import (
	_recovery_reader_script as _recovery_reader_script,
)
from temp_workspace_feature_split72_support import (
	_run_workspace_script as _run_workspace_script,
)
from temp_workspace_feature_split72_support import (
	_start_workspace_script as _start_workspace_script,
)
from temp_workspace_feature_split72_support import (
	_wait_for_workspace_marker as _wait_for_workspace_marker,
)
from temp_workspace_feature_split72_support import (
	_workspace_parents as _workspace_parents,
)
from temp_workspace_feature_split72_support import (
	cast as cast,
)
from temp_workspace_feature_split72_support import (
	cleanup_handed_off_workspace as cleanup_handed_off_workspace,
)
from temp_workspace_feature_split72_support import (
	distribution as distribution,
)
from temp_workspace_feature_split72_support import (
	edit_cli as edit_cli,
)
from temp_workspace_feature_split72_support import (
	errno as errno,
)
from temp_workspace_feature_split72_support import (
	io as io,
)
from temp_workspace_feature_split72_support import (
	json as json,
)
from temp_workspace_feature_split72_support import (
	os as os,
)
from temp_workspace_feature_split72_support import (
	pytest as pytest,
)
from temp_workspace_feature_split72_support import (
	stat as stat,
)
from temp_workspace_feature_split72_support import (
	subprocess as subprocess,
)
from temp_workspace_feature_split72_support import (
	sys as sys,
)
from temp_workspace_feature_split72_support import (
	temp_workspace as temp_workspace,
)
from temp_workspace_feature_split72_support import (
	tempfile as tempfile,
)
from temp_workspace_feature_split72_support import (
	temporary_workspace as temporary_workspace,
)
from temp_workspace_feature_split72_support import (
	threading as threading,
)
from temp_workspace_feature_split72_support import (
	time as time,
)


@pytest.mark.parametrize("mode", ["ruff", "validate", "secrets", "integrity", "types"])
@pytest.mark.parametrize("worker_raises", [False, True])
def test_edit_worker_uses_and_cleans_managed_project_temp(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, worker_raises: bool
) -> None:
	project = tmp_path / "project"
	project.mkdir()
	workspaces: list[Path] = []

	def run_worker(
		*_arguments: object, root: Path, environment: dict[str, str], **_options: object
	) -> tuple[int, bytes, bytes]:
		workspaces.append(root)
		assert root.parent == project / "temp"
		assert root.name.startswith("qg-")
		assert environment["TEMP"] == str(root)
		(root / "source").mkdir()
		(root / "source" / "owned.py").write_text("VALUE = 1\n", encoding="utf-8")
		if worker_raises:
			raise EditError("deadline")
		response = {"schema": 1, "status": "unchecked", "reason": "deadline", "files": []}
		if mode == "validate":
			response.update(phase="validate", writes_may_have_occurred=False)
		return 2, json.dumps(response).encode(), b""

	monkeypatch.setattr(edit_cli, "run_process", run_worker)
	request = {"root": str(project), "schema": 1}
	if mode == "validate":
		request["phase"] = "validate"
	arguments = {
		"secrets": mode == "secrets",
		"types": mode == "types",
		"integrity": mode == "integrity",
	}
	if worker_raises:
		with pytest.raises(EditError, match="deadline"):
			edit_cli._run_worker(request, b"{}", time.monotonic() + 10, **arguments)
	else:
		result = edit_cli._run_worker(request, b"{}", time.monotonic() + 10, **arguments)
		assert result["reason"] == "deadline"
	assert len(workspaces) == 1
	assert not workspaces[0].exists()
	assert not list(project.glob(".quality-gate-edit-*"))


def test_temporary_workspace_routes_and_restores_temp_state(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	repository = tmp_path / "repository"
	repository.mkdir()
	system_temp = tmp_path / "system-temp"
	system_temp.mkdir()
	monkeypatch.setattr(tempfile, "tempdir", str(system_temp))
	previous_environment = {key: f"previous-{key}" for key in ("TMP", "TEMP", "TMPDIR")}
	for key, value in previous_environment.items():
		monkeypatch.setenv(key, value)
	monkeypatch.delenv("QUALITY_GATE_SYSTEM_TEMP", raising=False)

	temp_root = repository / "temp"
	foreign_file = temp_root / "qg-foreign-file"
	foreign_directory = temp_root / "qg-user-candidate"
	legacy_directory = temp_root / ".quality-gate-edit-legacy"
	foreign_file.parent.mkdir(parents=True)
	foreign_file.write_text("keep", encoding="utf-8")
	foreign_directory.mkdir()
	(foreign_directory / "candidate.py").write_text("keep", encoding="utf-8")
	(foreign_directory / ".quality-gate-workspace").write_text(
		'{"format":"quality-gate-temp-workspace","root":"elsewhere",'
		'"run":"qg-user-candidate","schema":1}\n',
		encoding="utf-8",
	)
	legacy_directory.mkdir()
	(legacy_directory / "candidate.py").write_text("keep", encoding="utf-8")
	workspace: Path | None = None
	with pytest.raises(RuntimeError, match="stop after checking cleanup"):
		with temporary_workspace(repository) as active_workspace:
			workspace = active_workspace
			assert active_workspace.parent == repository / "temp"
			assert active_workspace.is_dir()
			assert foreign_file.read_text(encoding="utf-8") == "keep"
			assert (foreign_directory / "candidate.py").read_text(encoding="utf-8") == "keep"
			assert (legacy_directory / "candidate.py").read_text(encoding="utf-8") == "keep"
			assert tempfile.gettempdir() == str(active_workspace)
			assert all(os.environ[key] == str(active_workspace) for key in previous_environment)
			assert os.environ["QUALITY_GATE_SYSTEM_TEMP"] == str(SYSTEM_TEMP_DIRECTORY)
			with temporary_workspace(repository) as nested_workspace:
				assert nested_workspace == active_workspace
			child_temporary = Path(tempfile.mkdtemp(prefix="child-"))
			assert child_temporary.parent == active_workspace
			raise RuntimeError("stop after checking cleanup")

	assert workspace is not None
	assert not workspace.exists()
	assert not list(temp_root.glob(".quality-gate-owner-*"))
	assert foreign_file.read_text(encoding="utf-8") == "keep"
	assert (foreign_directory / "candidate.py").read_text(encoding="utf-8") == "keep"
	assert (legacy_directory / "candidate.py").read_text(encoding="utf-8") == "keep"
	assert (temp_root / ".quality-gate.lock").is_file()
	assert tempfile.tempdir == str(system_temp)
	assert {key: os.environ[key] for key in previous_environment} == previous_environment
	assert "QUALITY_GATE_SYSTEM_TEMP" not in os.environ


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
	from ctypes import wintypes

	kernel, _advapi = temp_workspace._windows_security_api()
	handle = None
	try:
		with temporary_workspace(tmp_path) as workspace:
			path = workspace / "venv" / "Scripts" / "python.exe"
			path.parent.mkdir(parents=True)
			path.write_bytes(b"owned temporary data")
			handle = kernel.CreateFileW(
				str(path),
				0x80000000,  # GENERIC_READ
				temp_workspace._WINDOWS_SHARE_READ
				| temp_workspace._WINDOWS_SHARE_WRITE
				| temp_workspace._WINDOWS_SHARE_DELETE,
				None,
				temp_workspace._WINDOWS_OPEN_EXISTING,
				0,
				None,
			)
			assert handle not in (None, wintypes.HANDLE(-1).value)
		assert not workspace.exists()
		# The reader remains valid after the owned name is removed.
		temp_workspace._windows_file_identity(handle, kernel)
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
			temp_workspace.ctypes.set_last_error(error_code)
			return False
		return True

	kernel = SimpleNamespace(SetFileInformationByHandle=set_information)
	if unsupported:
		temp_workspace._mark_windows_handle_for_deletion(1, kernel)
		assert calls == [21, 4]
	else:
		with pytest.raises(OSError) as observed:
			temp_workspace._mark_windows_handle_for_deletion(1, kernel)
		assert observed.value.winerror == error_code
		assert calls == [21]


@pytest.mark.skipif(os.name != "nt", reason="Windows read-only file attributes only")
def test_temporary_workspace_removes_read_only_git_objects(tmp_path: Path) -> None:
	repository = tmp_path / "repository"
	repository.mkdir()
	with temporary_workspace(repository) as workspace:
		object_file = workspace / "pytest" / "repository" / ".git" / "objects" / "aa" / "object"
		object_file.parent.mkdir(parents=True)
		object_file.write_bytes(b"git object")
		object_file.chmod(stat.S_IREAD)

	assert not workspace.exists()


def test_release_workspace_uses_active_repository_temp_and_keeps_explicit_override(
	tmp_path: Path,
) -> None:
	repository = tmp_path / "repository"
	repository.mkdir()
	explicit_parent = tmp_path / "short-workspace"
	explicit_parent.mkdir()

	with temporary_workspace(repository) as workspace:
		assert _workspace_parents(None) == (workspace,)
	assert _workspace_parents(explicit_parent) == (explicit_parent.resolve(),)


@pytest.mark.skipif(os.name != "nt", reason="Windows cache fallback only")
def test_windows_cache_fallback_uses_the_original_system_temp(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	system_temp = tmp_path / "system-temp"
	monkeypatch.delenv("LOCALAPPDATA", raising=False)
	monkeypatch.setattr(distribution, "SYSTEM_TEMP_DIRECTORY", system_temp)

	assert distribution.default_cache_root() == system_temp / "quality-gate"


def test_temporary_workspace_preserves_an_active_process_and_misleading_entries(
	tmp_path: Path,
) -> None:
	repository = tmp_path / "repository"
	repository.mkdir()
	temp_root = repository / "temp"
	misleading_directory = temp_root / "qg-stale-run"
	misleading_directory.mkdir(parents=True)
	(misleading_directory / "old-file").write_text("preserve", encoding="utf-8")
	misleading_file = temp_root / "qg-stale-file"
	misleading_file.write_text("preserve", encoding="utf-8")
	coordination = tmp_path / "coordination"
	coordination.mkdir()
	ready = coordination / "ready"
	release = coordination / "release"
	script = "\n".join(
		(
			"import sys",
			"import time",
			"from pathlib import Path",
			"from quality_gate.temp_workspace import temporary_workspace",
			"root, ready, release = (Path(value) for value in sys.argv[1:])",
			"with temporary_workspace(root) as workspace:",
			"    ready.write_text(str(workspace), encoding='utf-8')",
			"    deadline = time.monotonic() + 15",
			"    while not release.exists() and time.monotonic() < deadline:",
			"        time.sleep(0.01)",
			"    if not release.exists():",
			"        raise SystemExit(3)",
		)
	)
	process = subprocess.Popen(
		[sys.executable, "-c", script, str(repository), str(ready), str(release)],
		cwd=Path(__file__).resolve().parents[1],
		stdout=subprocess.DEVNULL,
		stderr=subprocess.PIPE,
		text=True,
	)
	active_workspace: Path | None = None
	try:
		deadline = time.monotonic() + 15
		while not ready.exists() and time.monotonic() < deadline:
			if process.poll() is not None:
				_, stderr = process.communicate()
				pytest.fail(f"workspace subprocess exited early: {stderr}")
			time.sleep(0.01)
		assert ready.exists(), "workspace subprocess did not become active"
		active_workspace = Path(ready.read_text(encoding="utf-8"))
		assert active_workspace.is_dir()
		(active_workspace / "candidate").mkdir()
		(active_workspace / "candidate" / "module.py").write_text(
			"value: int = 1\n", encoding="utf-8"
		)

		with temporary_workspace(repository) as current_workspace:
			assert current_workspace != active_workspace
			assert active_workspace.is_dir()
			assert (active_workspace / "candidate" / "module.py").read_text(
				encoding="utf-8"
			) == "value: int = 1\n"
			assert (misleading_directory / "old-file").read_text(encoding="utf-8") == "preserve"
			assert misleading_file.read_text(encoding="utf-8") == "preserve"
		assert active_workspace.is_dir()
	finally:
		release.touch()
		try:
			_, stderr = process.communicate(timeout=15)
		except subprocess.TimeoutExpired:
			process.kill()
			_, stderr = process.communicate(timeout=15)
			pytest.fail(f"workspace subprocess did not exit after release: {stderr}")

	assert process.returncode == 0, f"workspace subprocess failed: {stderr}"
	assert active_workspace is not None
	assert not active_workspace.exists()
	assert (misleading_directory / "old-file").read_text(encoding="utf-8") == "preserve"
	assert misleading_file.read_text(encoding="utf-8") == "preserve"


def test_temporary_workspace_preserves_qg_directory_with_forged_valid_marker(
	tmp_path: Path,
) -> None:
	repository = tmp_path / "repository"
	temp_root = repository / "temp"
	foreign = temp_root / "qg-user-owned"
	foreign.mkdir(parents=True)
	(foreign / ".active.lock").write_bytes(b"\0")
	(foreign / ".quality-gate-workspace").write_bytes(
		temp_workspace._ownership_marker(temp_root, foreign)
	)
	(foreign / "candidate.py").write_text("keep", encoding="utf-8")

	with temporary_workspace(repository):
		assert (foreign / "candidate.py").read_text(encoding="utf-8") == "keep"

	assert foreign.is_dir()
	assert (foreign / "candidate.py").read_text(encoding="utf-8") == "keep"
