from __future__ import annotations

import errno
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import BinaryIO

import pytest

import quality_gate.distribution as distribution
import quality_gate.edit_cli as edit_cli
import quality_gate.temp_workspace as temp_workspace
from quality_gate.edit_protocol import EXIT_UNCHECKED, EditError
from quality_gate.release import _workspace_parents
from quality_gate.temp_workspace import (
	SYSTEM_TEMP_DIRECTORY,
	TemporaryWorkspaceError,
	temporary_workspace,
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


def test_temporary_workspace_reclaims_owned_run_after_process_interruption(
	tmp_path: Path,
) -> None:
	repository = tmp_path / "repository"
	repository.mkdir()
	coordination = tmp_path / "coordination"
	coordination.mkdir()
	ready = coordination / "ready"
	script = "\n".join(
		(
			"import sys",
			"import time",
			"from pathlib import Path",
			"from quality_gate.temp_workspace import temporary_workspace",
			"root, ready = Path(sys.argv[1]), Path(sys.argv[2])",
			"with temporary_workspace(root) as workspace:",
			"    child = workspace / 'candidate'",
			"    child.mkdir()",
			"    (child / 'module.py').write_text('value = 1\\n', encoding='utf-8')",
			"    ready.write_text(str(workspace), encoding='utf-8')",
			"    while True:",
			"        time.sleep(1)",
		)
	)
	process = subprocess.Popen(
		[sys.executable, "-c", script, str(repository), str(ready)],
		cwd=Path(__file__).resolve().parents[1],
		stdout=subprocess.DEVNULL,
		stderr=subprocess.PIPE,
		text=True,
	)
	owned_workspace: Path | None = None
	try:
		deadline = time.monotonic() + 15
		while not ready.exists() and time.monotonic() < deadline:
			if process.poll() is not None:
				_, stderr = process.communicate()
				pytest.fail(f"workspace subprocess exited early: {stderr}")
			time.sleep(0.01)
		assert ready.exists(), "workspace subprocess did not become active"
		owned_workspace = Path(ready.read_text(encoding="utf-8"))
		assert (owned_workspace / ".quality-gate-workspace").is_file()
		assert (owned_workspace / "candidate" / "module.py").exists()
		process.kill()
		process.communicate(timeout=15)
		assert process.returncode != 0

		with temporary_workspace(repository) as current_workspace:
			assert current_workspace != owned_workspace
			assert not owned_workspace.exists()
			assert current_workspace.is_dir()
	finally:
		if process.poll() is None:
			process.kill()
			process.communicate(timeout=15)
	assert owned_workspace is not None
	assert not owned_workspace.exists()


def test_reclamation_preserves_a_directory_replaced_after_ownership_check(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	repository = tmp_path / "repository"
	temp_root = repository / "temp"
	stale = temp_root / "qg-stale-owned"
	displaced = temp_root / "qg-displaced-owned"
	stale.mkdir(parents=True)
	(stale / ".active.lock").write_bytes(b"\0")
	(stale / "owned-content").write_text("owned", encoding="utf-8")
	(stale / ".quality-gate-workspace").write_bytes(
		temp_workspace._ownership_marker(temp_root, stale)
	)
	stale_identity = stale.lstat()
	temp_workspace._write_ownership_record(temp_root, stale, stale_identity)

	original_remove = temp_workspace._remove_entry
	replaced = False

	def replace_before_remove(
		path: Path, *, expected_identity: os.stat_result | None = None
	) -> None:
		nonlocal replaced
		if path == stale and expected_identity is not None and not replaced:
			path.replace(displaced)
			path.mkdir()
			(path / "foreign-content").write_text("preserve", encoding="utf-8")
			replaced = True
		original_remove(path, expected_identity=expected_identity)

	monkeypatch.setattr(temp_workspace, "_remove_entry", replace_before_remove)
	with temporary_workspace(repository) as current:
		assert current.is_dir()
		assert (stale / "foreign-content").read_text(encoding="utf-8") == "preserve"
		assert (displaced / "owned-content").read_text(encoding="utf-8") == "owned"

	assert replaced
	assert (stale / "foreign-content").read_text(encoding="utf-8") == "preserve"
	assert (displaced / ".quality-gate-workspace").is_file()


@pytest.mark.parametrize("replacement_kind", ["file", "link"])
def test_recursive_removal_preserves_a_nested_directory_replaced_before_reclamation(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	replacement_kind: str,
) -> None:
	workspace = tmp_path / "qg-workspace"
	nested = workspace / "nested"
	displaced = tmp_path / "displaced-nested"
	foreign_target = tmp_path / "foreign-target"
	nested.mkdir(parents=True)
	(nested / "owned-content").write_text("owned", encoding="utf-8")
	foreign_target.write_text("preserve", encoding="utf-8")
	original_lstat = Path.lstat
	nested_lstat_calls = 0
	replaced = False

	def replace_when_queued(path: Path) -> os.stat_result:
		nonlocal nested_lstat_calls, replaced
		if path == nested:
			nested_lstat_calls += 1
			if nested_lstat_calls == 2:
				path.replace(displaced)
				if replacement_kind == "file":
					path.write_text("foreign", encoding="utf-8")
				else:
					try:
						path.symlink_to(foreign_target)
					except OSError as error:
						pytest.skip(f"File symlinks are unavailable: {error}")
				replaced = True
		return original_lstat(path)

	monkeypatch.setattr(Path, "lstat", replace_when_queued)
	with pytest.raises(OSError, match="changed"):
		temp_workspace._remove_entry(workspace)

	assert replaced
	assert (displaced / "owned-content").read_text(encoding="utf-8") == "owned"
	if replacement_kind == "file":
		assert nested.read_text(encoding="utf-8") == "foreign"
	else:
		assert nested.is_symlink()
		assert foreign_target.read_text(encoding="utf-8") == "preserve"


def test_temporary_workspace_preserves_foreign_link_target(tmp_path: Path) -> None:
	repository = tmp_path / "repository"
	temp_root = repository / "temp"
	target = tmp_path / "foreign-target"
	temp_root.mkdir(parents=True)
	target.mkdir()
	(target / "keep.txt").write_text("outside", encoding="utf-8")
	link = temp_root / "qg-foreign-link"
	try:
		os.symlink(target, link, target_is_directory=True)
	except OSError as error:
		pytest.skip(f"directory symlink creation is unavailable: {error}")

	with temporary_workspace(repository):
		assert link.is_symlink()
		assert (target / "keep.txt").read_text(encoding="utf-8") == "outside"

	assert link.is_symlink()
	assert (target / "keep.txt").read_text(encoding="utf-8") == "outside"


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

	def reject_read_only_root(root: Path) -> tuple[Path, Path, BinaryIO]:
		attempted_roots.append(root)
		if root == repository.resolve():
			error = TemporaryWorkspaceError("repository temp is not writable")
			raise error from PermissionError(errno.EACCES, "read-only repository")
		return prepare_workspace(root)

	monkeypatch.setattr(temp_workspace, "_prepare_workspace", reject_read_only_root)
	fallback_root = temp_workspace._fallback_workspace_root(repository.resolve())
	with temporary_workspace(repository) as workspace:
		assert workspace.parent == fallback_root / "temp"
		assert workspace.is_dir()
		with temporary_workspace(repository) as nested_workspace:
			assert nested_workspace == workspace

	assert not workspace.exists()
	assert attempted_roots == [repository.resolve(), fallback_root]
