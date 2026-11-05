from __future__ import annotations

import errno
import io
import json
import os
import stat
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, cast

import pytest
from test_temp_workspace import (
	_SECOND_NESTED_LSTAT_CALL,
)

import quality_gate.edit_cli as edit_cli
import quality_gate.temp_workspace as temp_workspace
import quality_gate.workspace_keys as workspace_keys
import quality_gate.workspace_quarantine as workspace_quarantine
import quality_gate.workspace_tree as workspace_tree
import quality_gate.workspace_windows as workspace_windows
from quality_gate.edit_protocol import EXIT_UNCHECKED
from quality_gate.temp_workspace import TemporaryWorkspaceError, temporary_workspace


def test_removal_rescans_tree_after_repeated_nonempty_errors(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	temp_root = tmp_path / "temp"
	workspace = temp_root / "qg-retry"
	victim = workspace / "victim"
	victim.mkdir(parents=True)
	identity = victim.lstat()
	original_remove = workspace_tree._remove_collected_directory
	transient_errors = 8
	attempts = 0

	def fail_first_removals(path: Path, expected: os.stat_result) -> None:
		nonlocal attempts
		if path == victim and attempts < transient_errors:
			attempts += 1
			raise OSError(errno.ENOTEMPTY, "directory not empty", os.fspath(path))
		original_remove(path, expected)

	monkeypatch.setattr(workspace_tree, "_remove_collected_directory", fail_first_removals)

	temp_workspace._remove_entry(victim, expected_identity=identity)

	assert attempts == transient_errors
	assert not victim.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX quarantine uses directory descriptors")
def test_removal_rescans_directory_when_entries_keep_appearing_after_quarantine(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	temp_root = tmp_path / "temp"
	workspace = temp_root / "qg-rescan"
	workspace.mkdir(parents=True)
	victim = workspace / "victim"
	victim.mkdir()
	identity = victim.lstat()
	original_move = workspace_quarantine._rename_entry_to_cleanup_area
	late_entry_count = 8
	injected_entries = 0

	def add_entry_after_quarantine(
		path: Path,
		quarantine_name: str,
		parent_descriptor: int,
		cleanup_descriptor: int,
	) -> None:
		nonlocal injected_entries
		original_move(path, quarantine_name, parent_descriptor, cleanup_descriptor)
		if path != victim or injected_entries == late_entry_count:
			return
		late_entry_name = f"late-entry-{injected_entries}"
		injected_entries += 1
		directory_descriptor = os.open(
			quarantine_name,
			os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
			dir_fd=cleanup_descriptor,
		)
		try:
			file_descriptor = os.open(
				late_entry_name,
				os.O_WRONLY | os.O_CREAT | os.O_EXCL,
				0o600,
				dir_fd=directory_descriptor,
			)
			with os.fdopen(file_descriptor, "wb") as late_entry:
				late_entry.write(b"created during quarantine")
		finally:
			os.close(directory_descriptor)

	monkeypatch.setattr(
		workspace_quarantine, "_rename_entry_to_cleanup_area", add_entry_after_quarantine
	)

	temp_workspace._remove_entry(victim, expected_identity=identity)

	assert injected_entries == late_entry_count
	assert not victim.exists()


def test_temporary_workspace_cleanup_retries_each_directory_independently(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	monkeypatch.setattr(workspace_keys, "_user_state_directory", lambda: tmp_path / "state")
	original_remove = workspace_tree._remove_collected_directory
	victims: set[Path] = set()
	interrupted: set[Path] = set()

	def interrupt_each_directory_once(path: Path, expected: os.stat_result) -> None:
		if path in victims and path not in interrupted:
			interrupted.add(path)
			raise OSError(errno.ENOTEMPTY, "directory not empty", os.fspath(path))
		original_remove(path, expected)

	monkeypatch.setattr(
		workspace_tree, "_remove_collected_directory", interrupt_each_directory_once
	)

	with temporary_workspace(tmp_path / "repository") as workspace:
		for index in range(workspace_tree._DIRECTORY_REMOVAL_MAX_RESCANS + 8):
			victim = workspace / f"victim-{index}"
			victim.mkdir()
			victims.add(victim)

	assert interrupted == victims
	assert not workspace.exists()


@pytest.mark.skipif(os.name == "nt", reason="Windows deletion uses file-handle identity")
@pytest.mark.parametrize("entry_kind", ["file", "link", "directory"])
def test_removal_preserves_entry_replaced_after_identity_check(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	entry_kind: str,
) -> None:
	temp_root = tmp_path / "temp"
	workspace = temp_root / "qg-race"
	workspace.mkdir(parents=True)
	victim = workspace / "victim"
	displaced = tmp_path / f"displaced-{entry_kind}"
	foreign_target = tmp_path / "foreign-target"
	foreign_target.write_text("outside", encoding="utf-8")
	if entry_kind == "file":
		victim.write_text("owned", encoding="utf-8")
	elif entry_kind == "link":
		try:
			victim.symlink_to(foreign_target)
		except OSError as error:
			pytest.skip(f"file symlinks are unavailable: {error}")
	else:
		victim.mkdir()
	identity = victim.lstat()
	original_move = workspace_quarantine._rename_entry_to_cleanup_area
	replaced = False

	def replace_before_move(
		path: Path,
		quarantine_name: str,
		parent_descriptor: int,
		cleanup_descriptor: int,
	) -> None:
		nonlocal replaced
		if path == victim and not replaced:
			path.replace(displaced)
			if entry_kind == "file":
				path.write_text("foreign", encoding="utf-8")
			elif entry_kind == "link":
				path.symlink_to(foreign_target)
			else:
				path.mkdir()
				(path / "foreign.txt").write_text("foreign", encoding="utf-8")
			replaced = True
		original_move(path, quarantine_name, parent_descriptor, cleanup_descriptor)

	monkeypatch.setattr(workspace_quarantine, "_rename_entry_to_cleanup_area", replace_before_move)
	with pytest.raises(OSError, match="changed"):
		temp_workspace._remove_entry(victim, expected_identity=identity)

	assert replaced
	if entry_kind == "file":
		assert victim.read_text(encoding="utf-8") == "foreign"
		assert displaced.read_text(encoding="utf-8") == "owned"
	elif entry_kind == "link":
		assert victim.is_symlink()
		assert displaced.is_symlink()
		assert foreign_target.read_text(encoding="utf-8") == "outside"
	else:
		assert (victim / "foreign.txt").read_text(encoding="utf-8") == "foreign"
		assert displaced.is_dir()
		assert not list(displaced.iterdir())


@pytest.mark.skipif(os.name == "nt", reason="POSIX retry uses directory permissions")
def test_removal_rechecks_identity_after_permission_retry(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	temp_root = tmp_path / "temp"
	workspace = temp_root / "qg-permission-retry"
	workspace.mkdir(parents=True)
	victim = workspace / "victim"
	displaced = tmp_path / "displaced-original"
	victim.write_text("owned", encoding="utf-8")
	identity = victim.lstat()
	original_rename = os.rename
	replaced = False

	def replace_before_retry(
		source: str | os.PathLike[str],
		destination: str | os.PathLike[str],
		*,
		src_dir_fd: int | None = None,
		dst_dir_fd: int | None = None,
	) -> None:
		nonlocal replaced
		if source == victim.name and not replaced:
			victim.replace(displaced)
			victim.write_text("foreign", encoding="utf-8")
			replaced = True
			raise PermissionError("simulated first rename failure")
		original_rename(
			source,
			destination,
			src_dir_fd=src_dir_fd,
			dst_dir_fd=dst_dir_fd,
		)

	monkeypatch.setattr(temp_workspace.os, "rename", replace_before_retry)
	with pytest.raises(OSError, match="changed"):
		temp_workspace._remove_entry(victim, expected_identity=identity)

	assert replaced
	assert victim.read_text(encoding="utf-8") == "foreign"
	assert displaced.read_text(encoding="utf-8") == "owned"


@pytest.mark.skipif(os.name != "nt", reason="Windows deletion uses file-handle identity")
def test_windows_removal_deletes_the_opened_identity_after_path_replacement(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	from ctypes import wintypes

	temp_root = tmp_path / "temp"
	workspace = temp_root / "qg-windows-race"
	workspace.mkdir(parents=True)
	victim = workspace / "victim"
	displaced = tmp_path / "displaced-original"
	victim.write_text("owned", encoding="utf-8")
	identity = victim.lstat()
	kernel32, advapi32 = workspace_windows._windows_security_api()
	original_create_file = cast(Callable[..., object], kernel32.CreateFileW)
	replaced = False

	def open_then_replace(*arguments: object) -> object:
		nonlocal replaced
		handle = original_create_file(*arguments)
		if handle not in (None, wintypes.HANDLE(-1).value):
			victim.replace(displaced)
			victim.write_text("foreign", encoding="utf-8")
			replaced = True
		return handle

	class KernelProxy:
		def __getattr__(self, name: str) -> object:
			if name == "CreateFileW":
				return open_then_replace
			return getattr(kernel32, name)

	monkeypatch.setattr(
		workspace_windows,
		"_windows_security_api",
		lambda: (KernelProxy(), advapi32),
	)
	workspace_windows._remove_by_windows_handle(victim, identity, kind="file")

	assert replaced
	assert victim.read_text(encoding="utf-8") == "foreign"
	assert not displaced.exists()


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
			if nested_lstat_calls == _SECOND_NESTED_LSTAT_CALL:
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

	def reject_read_only_root(
		root: Path,
	) -> tuple[Path, Path, BinaryIO, os.stat_result, os.stat_result]:
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
