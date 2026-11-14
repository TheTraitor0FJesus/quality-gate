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


def test_repository_temp_link_is_rejected_without_touching_its_target(tmp_path: Path) -> None:
	repository = tmp_path / "repository"
	repository.mkdir()
	target = tmp_path / "foreign-temp"
	target.mkdir()
	(target / "keep.txt").write_text("outside", encoding="utf-8")
	link = repository / "temp"
	try:
		os.symlink(target, link, target_is_directory=True)
	except OSError as error:
		pytest.skip(f"directory symlink creation is unavailable: {error}")

	with pytest.raises(temp_workspace.TemporaryWorkspaceError):
		with temporary_workspace(repository):
			pytest.fail("repository temp link must be rejected")
	assert link.is_symlink()
	assert (target / "keep.txt").read_text(encoding="utf-8") == "outside"


def test_owned_workspace_removal_does_not_follow_a_link_target(tmp_path: Path) -> None:
	repository = tmp_path / "repository"
	target = tmp_path / "foreign-target"
	target.mkdir()
	(target / "keep.txt").write_text("outside", encoding="utf-8")
	link: Path | None = None
	with temporary_workspace(repository) as workspace:
		link = workspace / "foreign-link"
		try:
			os.symlink(target, link, target_is_directory=True)
		except OSError as error:
			pytest.skip(f"directory symlink creation is unavailable: {error}")

	assert link is not None and not link.exists()
	assert (target / "keep.txt").read_text(encoding="utf-8") == "outside"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction boundary only")
def test_workspace_reclamation_preserves_foreign_junction_and_target(tmp_path: Path) -> None:
	repository = tmp_path / "repository"
	temp_root = repository / "temp"
	temp_root.mkdir(parents=True)
	foreign_target = tmp_path / "foreign-target"
	owned_target = tmp_path / "owned-target"
	foreign_target.mkdir()
	owned_target.mkdir()
	(foreign_target / "keep.txt").write_text("foreign", encoding="utf-8")
	(owned_target / "keep.txt").write_text("owned", encoding="utf-8")
	foreign_junction = temp_root / "qg-foreign-junction"
	created = subprocess.run(
		["cmd.exe", "/c", "mklink", "/J", str(foreign_junction), str(foreign_target)],
		capture_output=True,
		check=False,
		timeout=10,
	)
	if created.returncode != 0:
		pytest.skip("junction creation is unavailable")
	owned_junction: Path | None = None
	with temporary_workspace(repository) as workspace:
		owned_junction = workspace / "junction"
		created = subprocess.run(
			["cmd.exe", "/c", "mklink", "/J", str(owned_junction), str(owned_target)],
			capture_output=True,
			check=False,
			timeout=10,
		)
		if created.returncode != 0:
			pytest.skip("junction creation is unavailable")
		metadata = owned_junction.lstat()
		assert getattr(metadata, "st_file_attributes", 0) & getattr(
			stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
		)

	assert owned_junction is not None and not owned_junction.exists()
	assert foreign_junction.exists()
	assert getattr(foreign_junction.lstat(), "st_file_attributes", 0) & getattr(
		stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
	)
	assert (foreign_target / "keep.txt").read_text(encoding="utf-8") == "foreign"
	assert (owned_target / "keep.txt").read_text(encoding="utf-8") == "owned"


@pytest.mark.parametrize(
	("mode", "phase", "arguments"),
	[
		("ruff-write", "write", {}),
		("ruff-validate", "validate", {}),
		("secrets", None, {"secrets": True}),
		("integrity", None, {"integrity": True}),
		("types", None, {"types": True}),
	],
)
def test_public_edit_modes_preserve_foreign_temp_content(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capfd: pytest.CaptureFixture[str],
	mode: str,
	phase: str | None,
	arguments: dict[str, bool],
) -> None:
	project = tmp_path / "project"
	project.mkdir()
	temp_root = project / "temp"
	temp_root.mkdir()
	foreign_file = temp_root / "qg-user-data.bin"
	foreign_file.write_bytes(b"preserve")
	foreign_directory = temp_root / "qg-candidate"
	foreign_directory.mkdir()
	(foreign_directory / "input.py").write_text("preserve", encoding="utf-8")
	legacy_directory = temp_root / ".quality-gate-edit-old"
	legacy_directory.mkdir()
	(legacy_directory / "input.py").write_text("preserve", encoding="utf-8")
	request: dict[str, object] = {
		"schema": 1,
		"root": str(project.resolve()),
		"files": [],
		"deadline_unix_ms": int(time.time() * 1000) + 5000,
	}
	if phase is not None:
		request["phase"] = phase
	if mode == "types":
		request["baseline"] = {"policy_release": "v4.4.0"}
	stdin = io.TextIOWrapper(io.BytesIO(json.dumps(request).encode()), encoding="utf-8")
	monkeypatch.setattr(edit_cli.sys, "stdin", stdin)
	workspaces: list[Path] = []

	def run_worker(
		*_arguments: object, root: Path, environment: dict[str, str], **_options: object
	) -> tuple[int, bytes, bytes]:
		workspaces.append(root)
		assert root.parent == temp_root
		assert environment["TEMP"] == str(root)
		result = edit_cli._failure(
			"deadline",
			secrets=bool(arguments.get("secrets")),
			types=bool(arguments.get("types")),
			integrity=bool(arguments.get("integrity")),
		)
		return EXIT_UNCHECKED, json.dumps(result).encode(), b""

	monkeypatch.setattr(edit_cli, "run_process", run_worker)
	assert edit_cli.main(**arguments) == EXIT_UNCHECKED
	assert json.loads(capfd.readouterr().out)["status"] == "unchecked"
	assert len(workspaces) == 1 and not workspaces[0].exists()
	assert foreign_file.read_bytes() == b"preserve"
	assert (foreign_directory / "input.py").read_text(encoding="utf-8") == "preserve"
	assert (legacy_directory / "input.py").read_text(encoding="utf-8") == "preserve"


def test_read_only_repository_uses_a_per_repository_system_temp_fallback(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	repository = tmp_path / "readonly-repository"
	repository.mkdir()
	system_temp = tmp_path / "system-temp"
	system_temp.mkdir()
	monkeypatch.setattr(temp_workspace, "SYSTEM_TEMP_DIRECTORY", system_temp)
	prepare_workspace = temp_workspace._prepare_workspace
	attempted_roots: list[Path] = []

	def reject_read_only_root(
		root: Path,
		*,
		deadline: float | None = None,
	) -> tuple[Path, Path, BinaryIO, os.stat_result, os.stat_result]:
		attempted_roots.append(root)
		if root == repository.resolve():
			error = TemporaryWorkspaceError("repository temp is not writable")
			raise error from PermissionError(errno.EACCES, "read-only repository")
		return prepare_workspace(root, deadline=deadline)

	monkeypatch.setattr(temp_workspace, "_prepare_workspace", reject_read_only_root)
	fallback_root = temp_workspace._fallback_workspace_root(repository.resolve())
	with temporary_workspace(repository) as workspace:
		assert workspace.parent == fallback_root / "temp"
		assert workspace.is_dir()
		with temporary_workspace(repository) as nested_workspace:
			assert nested_workspace == workspace

	assert not workspace.exists()
	assert attempted_roots == [repository.resolve(), fallback_root]


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
