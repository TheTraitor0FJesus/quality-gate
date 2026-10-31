from __future__ import annotations

import io
import os
import stat
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from quality_gate import snapshot as snapshot_module

SnapshotError = snapshot_module.SnapshotError
candidate_snapshot = snapshot_module.candidate_snapshot
EXPECTED_FIXED_DISCOVERY_PROCESSES = 5
EXPECTED_BATCH_ATTRIBUTE_PROCESSES = 1
EXPECTED_BATCH_OBJECT_PROCESSES = 1
BLOCKING_OUTPUT_FAILSAFE_SECONDS = 2


def _git(
	root: Path, *arguments: str, input_data: str | None = None
) -> subprocess.CompletedProcess[str]:
	environment = os.environ.copy()
	environment["GIT_CONFIG_GLOBAL"] = str(root / "missing-global-config")
	environment["GIT_CONFIG_NOSYSTEM"] = "1"
	return subprocess.run(
		["git", *arguments],
		cwd=root,
		env=environment,
		capture_output=True,
		input=input_data,
		text=True,
		check=False,
	)


def _init_and_stage(root: Path, *paths: str) -> None:
	assert _git(root, "init").returncode == 0
	assert _git(root, "add", *paths).returncode == 0


class _CapturedInput(io.BytesIO):
	def __init__(self) -> None:
		super().__init__()
		self.captured: bytes | None = None

	def close(self) -> None:
		if not self.closed:
			self.captured = self.getvalue()
		super().close()


class _FakeBatchProcess:
	def __init__(self, response: bytes | object) -> None:
		self.stdin = _CapturedInput()
		self.stdout = io.BytesIO(response) if isinstance(response, bytes) else response
		self.returncode: int | None = None
		self.killed = False
		self.waited = False
		self.wait_timeouts: list[float | None] = []

	def poll(self) -> int | None:
		return self.returncode

	def kill(self) -> None:
		self.killed = True
		self.returncode = -9
		release = getattr(self.stdout, "release", None)
		if release is not None:
			release()

	def wait(self, timeout: float | None = None) -> int:
		self.waited = True
		self.wait_timeouts.append(timeout)
		if self.returncode is None:
			self.returncode = 0
		return self.returncode


def _staged_entry(
	*,
	mode: bytes = b"100644",
	object_id: bytes = b"e69de29bb2d1d6434b8b29ae775ad8c2e48c5391",
	path: bytes = b"file.txt",
) -> snapshot_module._StagedEntry:
	return snapshot_module._StagedEntry(
		mode=mode,
		object_id=object_id,
		raw_path=path,
		relative_path=path.decode("utf-8"),
	)


def _materialization_context(
	root: Path, *, max_blob_bytes: int = 16
) -> snapshot_module._MaterializationContext:
	candidate_root = root / "candidate"
	candidate_root.mkdir()
	return snapshot_module._MaterializationContext(
		repository=root,
		candidate_root=candidate_root,
		index_path=root / "index",
		source_fingerprint=None,
		index_copy=root / "copied-index",
		timeout_seconds=1,
		max_blob_bytes=max_blob_bytes,
	)


def _fake_batch_process(
	monkeypatch: pytest.MonkeyPatch, response: bytes | object
) -> _FakeBatchProcess:
	process = _FakeBatchProcess(response)
	monkeypatch.setattr(snapshot_module.subprocess, "Popen", lambda *args, **kwargs: process)
	return process


def test_candidate_snapshot_preserves_index_and_worktree_and_cleans_up(tmp_path: Path) -> None:
	(tmp_path / "tracked.txt").write_text("staged\n", encoding="utf-8")
	_init_and_stage(tmp_path, "tracked.txt")
	(tmp_path / "tracked.txt").write_text("unstaged\n", encoding="utf-8")
	index_before = _git(tmp_path, "diff", "--cached", "--binary").stdout

	with candidate_snapshot(tmp_path) as snapshot:
		snapshot_path = snapshot.root
		assert (snapshot_path / "tracked.txt").read_text(encoding="utf-8") == "staged\n"
		assert (tmp_path / "tracked.txt").read_text(encoding="utf-8") == "unstaged\n"
		with pytest.raises(PermissionError):
			(snapshot_path / "tracked.txt").write_text("mutated\n", encoding="utf-8")

	assert not snapshot_path.exists()
	assert (tmp_path / "tracked.txt").read_text(encoding="utf-8") == "unstaged\n"
	assert _git(tmp_path, "diff", "--cached", "--binary").stdout == index_before


def test_candidate_snapshot_preserves_partial_staging(tmp_path: Path) -> None:
	(tmp_path / "partial.txt").write_text("one\nkeep\nthree\nfour\n", encoding="utf-8")
	_init_and_stage(tmp_path, "partial.txt")
	(tmp_path / "partial.txt").write_text(
		"staged-one\nkeep\nthree\nunstaged-four\n", encoding="utf-8"
	)
	patch = """diff --git a/partial.txt b/partial.txt
--- a/partial.txt
+++ b/partial.txt
@@ -1,4 +1,4 @@
-one
+staged-one
 keep
 three
 four
"""
	assert _git(tmp_path, "apply", "--cached", input_data=patch).returncode == 0

	with candidate_snapshot(tmp_path) as snapshot:
		assert (snapshot.root / "partial.txt").read_text(encoding="utf-8") == (
			"staged-one\nkeep\nthree\nfour\n"
		)
	assert (tmp_path / "partial.txt").read_text(encoding="utf-8") == (
		"staged-one\nkeep\nthree\nunstaged-four\n"
	)


def test_candidate_snapshot_reports_git_timeout_as_unchecked(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	(tmp_path / "tracked.txt").write_text("content\n", encoding="utf-8")
	_init_and_stage(tmp_path, "tracked.txt")
	index_before = _git(tmp_path, "diff", "--cached", "--binary").stdout
	created: list[Path] = []
	real_mkdtemp = snapshot_module.tempfile.mkdtemp

	def capture_mkdtemp(
		*,
		suffix: str | None = None,
		prefix: str | None = None,
		dir: str | os.PathLike[str] | None = None,
	) -> str:
		path = Path(real_mkdtemp(suffix=suffix, prefix=prefix, dir=dir))
		created.append(path)
		return str(path)

	def timeout(*args: object, **kwargs: object) -> None:
		raise subprocess.TimeoutExpired("git", 1)

	monkeypatch.setattr(snapshot_module.tempfile, "mkdtemp", capture_mkdtemp)
	monkeypatch.setattr(snapshot_module.subprocess, "run", timeout)

	with pytest.raises(SnapshotError, match="Git could not provide"):
		with candidate_snapshot(tmp_path):
			pass

	monkeypatch.undo()
	assert created and all(not path.exists() for path in created)
	assert _git(tmp_path, "diff", "--cached", "--binary").stdout == index_before


def test_candidate_snapshot_converts_interruption_and_cleans_up(tmp_path: Path) -> None:
	(tmp_path / "tracked.txt").write_text("content\n", encoding="utf-8")
	_init_and_stage(tmp_path, "tracked.txt")
	index_before = _git(tmp_path, "diff", "--cached", "--binary").stdout
	with pytest.raises(SnapshotError, match="interrupted"):
		with candidate_snapshot(tmp_path) as snapshot:
			snapshot_path = snapshot.root
			raise KeyboardInterrupt

	assert not snapshot_path.exists()
	assert _git(tmp_path, "diff", "--cached", "--binary").stdout == index_before


def test_candidate_snapshot_reports_construction_cleanup_failure(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	(tmp_path / "tracked.txt").write_text("content\n", encoding="utf-8")
	_init_and_stage(tmp_path, "tracked.txt")
	index_before = _git(tmp_path, "diff", "--cached", "--binary").stdout
	staging_file = tmp_path / "staging-file"
	staging_file.write_text("not a directory", encoding="utf-8")
	monkeypatch.setattr(snapshot_module.tempfile, "mkdtemp", lambda **kwargs: str(staging_file))

	with pytest.raises(SnapshotError, match="cleanup failed"):
		with candidate_snapshot(tmp_path):
			pass

	assert staging_file.exists()
	assert _git(tmp_path, "diff", "--cached", "--binary").stdout == index_before


def test_candidate_snapshot_reports_cleanup_failure_after_verification(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	(tmp_path / "tracked.txt").write_text("content\n", encoding="utf-8")
	_init_and_stage(tmp_path, "tracked.txt")
	index_before = _git(tmp_path, "diff", "--cached", "--binary").stdout

	def fail_cleanup(path: Path) -> None:
		raise OSError("locked")

	monkeypatch.setattr(snapshot_module.shutil, "rmtree", fail_cleanup)

	with pytest.raises(SnapshotError, match="cleanup failed"):
		with candidate_snapshot(tmp_path):
			pass

	assert _git(tmp_path, "diff", "--cached", "--binary").stdout == index_before


def test_candidate_snapshot_contains_staged_renames_and_deletions_only(tmp_path: Path) -> None:
	(tmp_path / "renamed.md").write_text("renamed\n", encoding="utf-8")
	(tmp_path / "deleted.md").write_text("deleted\n", encoding="utf-8")
	(tmp_path / "untracked.md").write_text("untracked\n", encoding="utf-8")
	_init_and_stage(tmp_path, "renamed.md", "deleted.md")
	assert _git(tmp_path, "mv", "renamed.md", "moved.md").returncode == 0
	assert _git(tmp_path, "rm", "--cached", "deleted.md").returncode == 0

	with candidate_snapshot(tmp_path) as snapshot:
		assert (snapshot.root / "moved.md").read_text(encoding="utf-8") == "renamed\n"
		assert not (snapshot.root / "renamed.md").exists()
		assert not (snapshot.root / "deleted.md").exists()
		assert not (snapshot.root / "untracked.md").exists()


def test_candidate_snapshot_rejects_unmerged_index(tmp_path: Path) -> None:
	(tmp_path / "tracked.txt").write_text("base\n", encoding="utf-8")
	_init_and_stage(tmp_path, "tracked.txt")
	assert (
		_git(
			tmp_path,
			"-c",
			"user.name=Quality Gate Test",
			"-c",
			"user.email=quality-gate@example.test",
			"commit",
			"-m",
			"base",
		).returncode
		== 0
	)
	assert _git(tmp_path, "checkout", "-b", "other").returncode == 0
	(tmp_path / "tracked.txt").write_text("other\n", encoding="utf-8")
	assert _git(tmp_path, "add", "tracked.txt").returncode == 0
	assert (
		_git(
			tmp_path,
			"-c",
			"user.name=Quality Gate Test",
			"-c",
			"user.email=quality-gate@example.test",
			"commit",
			"-m",
			"other",
		).returncode
		== 0
	)
	assert _git(tmp_path, "checkout", "-b", "mainline", "HEAD~1").returncode == 0
	(tmp_path / "tracked.txt").write_text("mainline\n", encoding="utf-8")
	assert _git(tmp_path, "add", "tracked.txt").returncode == 0
	assert (
		_git(
			tmp_path,
			"-c",
			"user.name=Quality Gate Test",
			"-c",
			"user.email=quality-gate@example.test",
			"commit",
			"-m",
			"mainline",
		).returncode
		== 0
	)
	assert _git(tmp_path, "read-tree", "-m", "HEAD~1", "HEAD", "other").returncode == 0

	with pytest.raises(SnapshotError, match="unresolved merge"):
		with candidate_snapshot(tmp_path):
			pass


def test_candidate_snapshot_rejects_index_mutation_during_verification(tmp_path: Path) -> None:
	(tmp_path / "tracked.txt").write_text("one\n", encoding="utf-8")
	_init_and_stage(tmp_path, "tracked.txt")

	with pytest.raises(SnapshotError, match="index changed"):
		with candidate_snapshot(tmp_path):
			(tmp_path / "tracked.txt").write_text("two\n", encoding="utf-8")
			assert _git(tmp_path, "add", "tracked.txt").returncode == 0


def test_candidate_snapshot_rejects_non_positive_timeout(tmp_path: Path) -> None:
	with pytest.raises(SnapshotError, match="snapshot timeout"):
		with candidate_snapshot(tmp_path, timeout_seconds=0):
			pass


def test_candidate_snapshot_registers_full_owned_root_once_on_normal_exit(tmp_path: Path) -> None:
	(tmp_path / "tracked.txt").write_bytes(b"staged\n")
	_init_and_stage(tmp_path, "tracked.txt")
	registered: list[Path] = []
	owned_root: Path | None = None
	try:
		with candidate_snapshot(tmp_path, register_cleanup=registered.append) as snapshot:
			owned_root = snapshot.root.parent
			assert (snapshot.root / "tracked.txt").read_bytes() == b"staged\n"
		assert registered == [owned_root]
		assert owned_root is not None
		assert owned_root.is_dir()
	finally:
		for path in registered:
			snapshot_module._remove_snapshot(path)


@pytest.mark.parametrize("failure", ["body", "index"])
def test_candidate_snapshot_registers_root_once_after_body_or_index_failure(
	tmp_path: Path, failure: str
) -> None:
	(tmp_path / "tracked.txt").write_bytes(b"staged\n")
	_init_and_stage(tmp_path, "tracked.txt")
	registered: list[Path] = []
	owned_root: Path | None = None
	try:
		if failure == "body":
			with pytest.raises(RuntimeError, match="body failed"):
				with candidate_snapshot(tmp_path, register_cleanup=registered.append) as snapshot:
					owned_root = snapshot.root.parent
					raise RuntimeError("body failed")
		else:
			with pytest.raises(SnapshotError, match="index changed"):
				with candidate_snapshot(tmp_path, register_cleanup=registered.append) as snapshot:
					owned_root = snapshot.root.parent
					(tmp_path / "tracked.txt").write_bytes(b"changed\n")
					assert _git(tmp_path, "add", "tracked.txt").returncode == 0
		assert registered == [owned_root]
		assert owned_root is not None
		assert owned_root.is_dir()
	finally:
		for path in registered:
			snapshot_module._remove_snapshot(path)


def test_candidate_snapshot_registers_once_after_construction_failure(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	(tmp_path / "tracked.txt").write_bytes(b"staged\n")
	_init_and_stage(tmp_path, "tracked.txt")
	allocated: list[Path] = []
	registered: list[Path] = []
	real_mkdtemp = snapshot_module.tempfile.mkdtemp

	def capture_mkdtemp(
		*,
		suffix: str | None = None,
		prefix: str | None = None,
		dir: str | os.PathLike[str] | None = None,
	) -> str:
		path = Path(real_mkdtemp(suffix=suffix, prefix=prefix, dir=dir))
		allocated.append(path)
		return str(path)

	def fail_candidate_limits(*args: object, **kwargs: object) -> tuple[int, int]:
		raise SnapshotError("construction failed after staging allocation")

	monkeypatch.setattr(snapshot_module.tempfile, "mkdtemp", capture_mkdtemp)
	monkeypatch.setattr(snapshot_module, "_candidate_limits", fail_candidate_limits)
	with pytest.raises(SnapshotError, match="construction failed after staging allocation"):
		with candidate_snapshot(tmp_path, register_cleanup=registered.append):
			pass
	try:
		assert len(allocated) == 1
		assert registered == allocated
		assert registered[0].is_dir()
	finally:
		for path in registered:
			snapshot_module._remove_snapshot(path)


def test_candidate_snapshot_registers_root_when_batch_worker_cannot_start(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	(tmp_path / "tracked.txt").write_bytes(b"staged\n")
	_init_and_stage(tmp_path, "tracked.txt")
	allocated: list[Path] = []
	registered: list[Path] = []
	real_mkdtemp = snapshot_module.tempfile.mkdtemp

	def capture_mkdtemp(
		*,
		suffix: str | None = None,
		prefix: str | None = None,
		dir: str | os.PathLike[str] | None = None,
	) -> str:
		path = Path(real_mkdtemp(suffix=suffix, prefix=prefix, dir=dir))
		allocated.append(path)
		return str(path)

	real_thread = threading.Thread

	class FailingBatchWorker(real_thread):
		def start(self) -> None:
			raise RuntimeError("could not start Git batch worker")

	class ThreadProxy:
		Thread = FailingBatchWorker

	monkeypatch.setattr(snapshot_module.tempfile, "mkdtemp", capture_mkdtemp)
	monkeypatch.setattr(snapshot_module, "threading", ThreadProxy)
	with pytest.raises(SnapshotError, match="construction failed"):
		with candidate_snapshot(tmp_path, register_cleanup=registered.append):
			pass
	try:
		assert len(allocated) == 1
		assert registered == allocated
		assert registered[0].is_dir()
	finally:
		for path in registered:
			snapshot_module._remove_snapshot(path)


def test_candidate_snapshot_does_not_register_when_root_allocation_fails(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	registered: list[Path] = []

	def fail_mkdtemp(
		*,
		suffix: str | None = None,
		prefix: str | None = None,
		dir: str | os.PathLike[str] | None = None,
	) -> str:
		raise OSError("temporary storage unavailable")

	monkeypatch.setattr(snapshot_module.tempfile, "mkdtemp", fail_mkdtemp)
	with pytest.raises(SnapshotError, match="construction failed"):
		with candidate_snapshot(tmp_path, register_cleanup=registered.append):
			pass
	assert registered == []


def test_candidate_snapshot_wraps_registration_failure_without_local_cleanup(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	(tmp_path / "tracked.txt").write_bytes(b"staged\n")
	_init_and_stage(tmp_path, "tracked.txt")
	registered: list[Path] = []
	removed: list[Path] = []
	registration_error = RuntimeError("owner rejected cleanup registration")
	owned_root: Path | None = None
	real_remove = snapshot_module._remove_snapshot

	def reject_registration(path: Path) -> None:
		registered.append(path)
		raise registration_error

	monkeypatch.setattr(snapshot_module, "_remove_snapshot", removed.append)
	try:
		with pytest.raises(SnapshotError, match="registration failed") as raised:
			with candidate_snapshot(tmp_path, register_cleanup=reject_registration) as snapshot:
				owned_root = snapshot.root.parent
				raise ValueError("original body failure")
		assert registered == [owned_root]
		assert raised.value.__cause__ is registration_error
		assert isinstance(raised.value.__context__, ValueError)
		assert removed == []
		assert owned_root is not None and owned_root.is_dir()
	finally:
		monkeypatch.undo()
		if owned_root is not None and owned_root.exists():
			real_remove(owned_root)


def test_candidate_snapshot_preserves_construction_failure_if_registration_fails(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	(tmp_path / "tracked.txt").write_bytes(b"staged\n")
	_init_and_stage(tmp_path, "tracked.txt")
	registered: list[Path] = []
	removed: list[Path] = []
	registration_error = RuntimeError("owner rejected cleanup registration")
	construction_error = SnapshotError("construction failed after staging allocation")
	real_remove = snapshot_module._remove_snapshot

	def fail_candidate_limits(*args: object, **kwargs: object) -> tuple[int, int]:
		raise construction_error

	def reject_registration(path: Path) -> None:
		registered.append(path)
		raise registration_error

	monkeypatch.setattr(snapshot_module, "_candidate_limits", fail_candidate_limits)
	monkeypatch.setattr(snapshot_module, "_remove_snapshot", removed.append)
	with pytest.raises(SnapshotError, match="registration failed") as raised:
		with candidate_snapshot(tmp_path, register_cleanup=reject_registration):
			pass
	assert len(registered) == 1
	assert raised.value.__cause__ is registration_error
	assert raised.value.__context__ is construction_error
	assert removed == []
	assert registered[0].is_dir()
	monkeypatch.undo()
	for path in registered:
		real_remove(path)


def test_candidate_snapshot_registers_allocated_root_after_deadline_expires(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	(tmp_path / "tracked.txt").write_bytes(b"staged\n")
	_init_and_stage(tmp_path, "tracked.txt")
	real_monotonic = time.monotonic
	deadline = real_monotonic() + 60
	expired = False
	registered: list[Path] = []
	registered_at: list[float] = []
	real_prepare_index = snapshot_module._prepare_index

	def controlled_monotonic() -> float:
		return deadline + 1 if expired else real_monotonic()

	def expire_after_index_copy(
		repository: Path,
		staging_root: Path,
		timeout_seconds: int,
		deadline: float | None = None,
	) -> tuple[Path, snapshot_module._IndexFingerprint, Path]:
		nonlocal expired
		result = real_prepare_index(repository, staging_root, timeout_seconds, deadline=deadline)
		expired = True
		return result

	def register(path: Path) -> None:
		registered.append(path)
		registered_at.append(time.monotonic())

	monkeypatch.setattr(snapshot_module.time, "monotonic", controlled_monotonic)
	monkeypatch.setattr(snapshot_module, "_prepare_index", expire_after_index_copy)
	with pytest.raises(SnapshotError, match="deadline"):
		with candidate_snapshot(tmp_path, deadline=deadline, register_cleanup=register):
			pass
	try:
		assert len(registered) == 1
		assert registered_at == [deadline + 1]
		assert registered[0].is_dir()
	finally:
		for path in registered:
			snapshot_module._remove_snapshot(path)


def test_candidate_snapshot_rejects_expired_deadline_before_allocating_root(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	allocated: list[str] = []
	real_mkdtemp = snapshot_module.tempfile.mkdtemp

	def record_mkdtemp(
		*,
		suffix: str | None = None,
		prefix: str | None = None,
		dir: str | os.PathLike[str] | None = None,
	) -> str:
		path = real_mkdtemp(suffix=suffix, prefix=prefix, dir=dir)
		allocated.append(path)
		return path

	monkeypatch.setattr(snapshot_module.tempfile, "mkdtemp", record_mkdtemp)
	with pytest.raises(SnapshotError, match="deadline"):
		with candidate_snapshot(tmp_path, deadline=time.monotonic() - 1):
			pass
	assert allocated == []


def test_candidate_snapshot_propagates_one_absolute_deadline_to_git_batches(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	(tmp_path / "tracked.txt").write_bytes(b"staged\n")
	_init_and_stage(tmp_path, "tracked.txt")
	deadline = time.monotonic() + 60
	git_deadlines: list[float | None] = []
	batch_deadlines: list[float | None] = []
	real_git = snapshot_module._git
	real_batch = snapshot_module._run_git_batch

	def record_git(
		repository: Path,
		arguments: tuple[str, ...],
		timeout_seconds: int,
		*,
		index_file: Path | None = None,
		deadline: float | None = None,
	) -> subprocess.CompletedProcess[bytes]:
		git_deadlines.append(deadline)
		return real_git(
			repository,
			arguments,
			timeout_seconds,
			index_file=index_file,
			deadline=deadline,
		)

	def record_batch(
		repository: Path,
		index_file: Path,
		timeout_seconds: int,
		arguments: tuple[str, ...],
		write_input: Callable[[snapshot_module.BinaryIO], None],
		read_output: Callable[[snapshot_module.BinaryIO], None],
		*,
		deadline: float | None = None,
	) -> None:
		batch_deadlines.append(deadline)
		real_batch(
			repository,
			index_file,
			timeout_seconds,
			arguments,
			write_input,
			read_output,
			deadline=deadline,
		)

	monkeypatch.setattr(snapshot_module, "_git", record_git)
	monkeypatch.setattr(snapshot_module, "_run_git_batch", record_batch)
	with candidate_snapshot(tmp_path, deadline=deadline) as snapshot:
		assert (snapshot.root / "tracked.txt").read_bytes() == b"staged\n"

	assert git_deadlines and all(value == deadline for value in git_deadlines)
	assert batch_deadlines == [deadline, deadline]


def test_candidate_snapshot_stops_preparation_at_the_shared_deadline(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	(tmp_path / "tracked.txt").write_bytes(b"staged\n")
	_init_and_stage(tmp_path, "tracked.txt")
	real_monotonic = time.monotonic
	deadline = real_monotonic() + 60
	expired = False
	real_prepare_index = snapshot_module._prepare_index

	def controlled_monotonic() -> float:
		return deadline + 1 if expired else real_monotonic()

	def expire_after_index_copy(
		repository: Path,
		staging_root: Path,
		timeout_seconds: int,
		deadline: float | None = None,
	) -> tuple[Path, snapshot_module._IndexFingerprint, Path]:
		result = real_prepare_index(repository, staging_root, timeout_seconds, deadline=deadline)
		nonlocal expired
		expired = True
		return result

	monkeypatch.setattr(snapshot_module.time, "monotonic", controlled_monotonic)
	monkeypatch.setattr(snapshot_module, "_prepare_index", expire_after_index_copy)
	with pytest.raises(SnapshotError, match="deadline"):
		with candidate_snapshot(tmp_path, deadline=deadline):
			pass


def test_candidate_snapshot_rejects_intent_to_add_entries(tmp_path: Path) -> None:
	_init_and_stage(tmp_path)
	(tmp_path / "planned.txt").write_text("planned\n", encoding="utf-8")
	assert _git(tmp_path, "add", "-N", "planned.txt").returncode == 0

	with pytest.raises(SnapshotError, match="intent-to-add"):
		with candidate_snapshot(tmp_path):
			pass


@pytest.mark.skipif(os.name == "nt", reason="Windows does not expose executable mode bits")
def test_candidate_snapshot_preserves_staged_executable_mode(tmp_path: Path) -> None:
	(tmp_path / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
	_init_and_stage(tmp_path, "run.sh")
	assert _git(tmp_path, "update-index", "--chmod=+x", "run.sh").returncode == 0

	with candidate_snapshot(tmp_path) as snapshot:
		assert snapshot.root.joinpath("run.sh").stat().st_mode & stat.S_IXUSR


def test_candidate_snapshot_rejects_oversized_blob(tmp_path: Path) -> None:
	(tmp_path / "large.bin").write_bytes(b"x" * (1024 * 1024 + 1))
	_init_and_stage(tmp_path, "large.bin")

	with pytest.raises(SnapshotError, match="exceeds"):
		with candidate_snapshot(tmp_path, max_blob_size_mib=1):
			pass


def test_candidate_snapshot_batches_preserve_raw_bytes_unicode_spaces_and_crlf(
	tmp_path: Path,
) -> None:
	assert _git(tmp_path, "init").returncode == 0
	(tmp_path / ".gitattributes").write_text("*.txt text eol=crlf\n", encoding="utf-8")
	assert _git(tmp_path, "add", ".gitattributes").returncode == 0
	odd_path = Path("space directory") / "naïve name.txt"
	(tmp_path / odd_path).parent.mkdir()
	(tmp_path / odd_path).write_bytes(b"same content\r\n")
	(tmp_path / "duplicate.txt").write_bytes(b"same content\r\n")
	(tmp_path / "windows.txt").write_bytes(b"first\r\nsecond\r\n")
	assert (
		_git(tmp_path, "add", "--", odd_path.as_posix(), "duplicate.txt", "windows.txt").returncode
		== 0
	)
	index_before = _git(tmp_path, "diff", "--cached", "--binary").stdout
	status_before = _git(tmp_path, "status", "--porcelain=v1").stdout
	worktree_before = {
		str(path.relative_to(tmp_path)): path.read_bytes()
		for path in (tmp_path / odd_path, tmp_path / "duplicate.txt", tmp_path / "windows.txt")
	}

	with candidate_snapshot(tmp_path) as snapshot:
		assert (snapshot.root / odd_path).read_bytes() == b"same content\n"
		assert (snapshot.root / "duplicate.txt").read_bytes() == b"same content\n"
		assert (snapshot.root / "windows.txt").read_bytes() == b"first\nsecond\n"
		assert {
			path.relative_to(snapshot.root).as_posix()
			for path in snapshot.root.rglob("*")
			if path.is_file() or path.is_symlink()
		} == {
			".gitattributes",
			"duplicate.txt",
			"space directory/naïve name.txt",
			"windows.txt",
		}

	assert _git(tmp_path, "diff", "--cached", "--binary").stdout == index_before
	assert _git(tmp_path, "status", "--porcelain=v1").stdout == status_before
	assert worktree_before == {
		str(path.relative_to(tmp_path)): path.read_bytes()
		for path in (tmp_path / odd_path, tmp_path / "duplicate.txt", tmp_path / "windows.txt")
	}


@pytest.mark.skipif(
	os.name == "nt", reason="Windows file systems reject newline characters in file names"
)
def test_candidate_snapshot_batches_preserve_newline_paths_on_posix(tmp_path: Path) -> None:
	assert _git(tmp_path, "init").returncode == 0
	newline_path = "line\nbreak.txt"
	(tmp_path / newline_path).write_bytes(b"staged\nbytes\n")
	assert _git(tmp_path, "add", "--", newline_path).returncode == 0
	index_before = _git(tmp_path, "diff", "--cached", "--binary").stdout
	status_before = _git(tmp_path, "status", "--porcelain=v1").stdout

	with candidate_snapshot(tmp_path) as snapshot:
		assert (snapshot.root / newline_path).read_bytes() == b"staged\nbytes\n"
		assert {
			path.relative_to(snapshot.root).as_posix()
			for path in snapshot.root.rglob("*")
			if path.is_file() or path.is_symlink()
		} == {newline_path}

	assert _git(tmp_path, "diff", "--cached", "--binary").stdout == index_before
	assert _git(tmp_path, "status", "--porcelain=v1").stdout == status_before
	assert (tmp_path / newline_path).read_bytes() == b"staged\nbytes\n"


@pytest.mark.skipif(
	os.name == "nt", reason="Windows symlink creation can require elevated privileges"
)
def test_candidate_snapshot_deduplicates_shared_file_and_symlink_objects(tmp_path: Path) -> None:
	assert _git(tmp_path, "init").returncode == 0
	(tmp_path / "target.txt").write_bytes(b"target.txt")
	(tmp_path / "same-object.txt").write_bytes(b"target.txt")
	os.symlink("target.txt", tmp_path / "symbolic link")
	assert (
		_git(tmp_path, "add", "--", "target.txt", "same-object.txt", "symbolic link").returncode
		== 0
	)
	index_before = _git(tmp_path, "diff", "--cached", "--binary").stdout

	with candidate_snapshot(tmp_path) as snapshot:
		assert (snapshot.root / "target.txt").read_bytes() == b"target.txt"
		assert (snapshot.root / "same-object.txt").read_bytes() == b"target.txt"
		assert (snapshot.root / "symbolic link").is_symlink()
		assert os.readlink(snapshot.root / "symbolic link") == "target.txt"

	assert _git(tmp_path, "diff", "--cached", "--binary").stdout == index_before


@pytest.mark.skipif(
	os.name == "nt", reason="Windows symlink creation can require elevated privileges"
)
def test_candidate_snapshot_rejects_staged_symlink_escape(tmp_path: Path) -> None:
	assert _git(tmp_path, "init").returncode == 0
	os.symlink("../outside", tmp_path / "escaping-link")
	assert _git(tmp_path, "add", "--", "escaping-link").returncode == 0

	with pytest.raises(SnapshotError, match="symbolic link escapes"):
		with candidate_snapshot(tmp_path):
			pass


def test_candidate_snapshot_rejects_cached_filter_attribute(tmp_path: Path) -> None:
	(tmp_path / "tracked.txt").write_text("content\n", encoding="utf-8")
	_init_and_stage(tmp_path, "tracked.txt")
	(tmp_path / ".gitattributes").write_text("tracked.txt filter=blocked\n", encoding="utf-8")
	assert _git(tmp_path, "add", ".gitattributes").returncode == 0

	with pytest.raises(SnapshotError, match="filters are not supported"):
		with candidate_snapshot(tmp_path):
			pass


def test_candidate_snapshot_reads_filter_attributes_from_the_staged_index(tmp_path: Path) -> None:
	(tmp_path / "tracked.txt").write_text("content\n", encoding="utf-8")
	_init_and_stage(tmp_path, "tracked.txt")
	(tmp_path / ".gitattributes").write_text("tracked.txt text\n", encoding="utf-8")
	assert _git(tmp_path, "add", ".gitattributes").returncode == 0
	(tmp_path / ".gitattributes").write_text("tracked.txt filter=worktree-only\n", encoding="utf-8")

	with candidate_snapshot(tmp_path) as snapshot:
		assert (snapshot.root / "tracked.txt").read_text(encoding="utf-8") == "content\n"
		assert (snapshot.root / ".gitattributes").read_text(
			encoding="utf-8"
		) == "tracked.txt text\n"


def test_candidate_snapshot_process_count_is_fixed_discovery_plus_two_batches(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	small = tmp_path / "small"
	large = tmp_path / "large"
	small.mkdir()
	large.mkdir()
	path_counts = ((small, 2), (large, 256))
	for root, count in path_counts:
		assert _git(root, "init").returncode == 0
		for index in range(count):
			(root / f"file-{index:04}.txt").write_bytes(f"content {index}\n".encode())
		assert _git(root, "add", "--", ".").returncode == 0

	monkeypatch.delenv("GIT_INDEX_FILE", raising=False)
	real_popen: Callable[..., subprocess.Popen[bytes]] = snapshot_module.subprocess.Popen
	processes: list[tuple[str, ...]] = []

	def record(command: object) -> None:
		if isinstance(command, (list, tuple)) and command and command[0] == "git":
			processes.append(tuple(str(argument) for argument in command[1:]))

	def count_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
		record(args[0] if args else kwargs.get("args"))
		return real_popen(*args, **kwargs)

	monkeypatch.setattr(snapshot_module.subprocess, "Popen", count_popen)
	process_counts: list[tuple[int, int, int, int]] = []
	for root, path_count in path_counts:
		processes.clear()
		expected_paths = {f"file-{index:04}.txt" for index in range(path_count)}
		with candidate_snapshot(root) as snapshot:
			assert {
				path.relative_to(snapshot.root).as_posix()
				for path in snapshot.root.rglob("*")
				if path.is_file() or path.is_symlink()
			} == expected_paths
			assert all(
				(snapshot.root / path).read_bytes() == f"content {index}\n".encode()
				for index, path in enumerate(sorted(expected_paths))
			)
		attribute_count = sum(command[:1] == ("check-attr",) for command in processes)
		object_count = sum(command[:1] == ("cat-file",) for command in processes)
		fixed_count = len(processes) - attribute_count - object_count
		process_counts.append((path_count, fixed_count, attribute_count, object_count))
	assert process_counts == [
		(
			2,
			EXPECTED_FIXED_DISCOVERY_PROCESSES,
			EXPECTED_BATCH_ATTRIBUTE_PROCESSES,
			EXPECTED_BATCH_OBJECT_PROCESSES,
		),
		(
			256,
			EXPECTED_FIXED_DISCOVERY_PROCESSES,
			EXPECTED_BATCH_ATTRIBUTE_PROCESSES,
			EXPECTED_BATCH_OBJECT_PROCESSES,
		),
	]


@pytest.mark.parametrize(
	("response", "message"),
	[
		(b"fale.txt\0filter\0unspecified\0", "malformed candidate attribute"),
		(b"file.txt\0filter", "truncated candidate batch"),
		(b"file.txt\0filter\0" + b"x" * 1025 + b"\0", "oversized candidate batch field"),
		(
			b"file.txt\0filter\0unspecified\0extra\0",
			"unexpected candidate attribute response",
		),
	],
)
def test_candidate_snapshot_reaps_process_after_malformed_attribute_response(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, response: bytes, message: str
) -> None:
	process = _fake_batch_process(monkeypatch, response)
	entry = _staged_entry()

	with pytest.raises(SnapshotError, match=message):
		snapshot_module._reject_filtered_paths(tmp_path, (entry,), tmp_path / "index", 1)

	assert process.killed
	assert process.waited
	assert process.wait_timeouts and all(timeout is not None for timeout in process.wait_timeouts)
	assert process.stdin.captured == b"file.txt\0"


@pytest.mark.parametrize(
	("response_factory", "max_blob_bytes", "message"),
	[
		(lambda oid: oid + b" missing\n", 16, "staged blob object"),
		(lambda oid: b"f" * 40 + b" blob 0\n\n", 16, "unexpected candidate object"),
		(lambda oid: oid + b" tree 0\n\n", 16, "non-blob candidate object"),
		(lambda oid: oid + b" blob -1\n\n", 16, "invalid candidate object length"),
		(lambda oid: oid + b" blob 3\nabc\n", 2, "exceeds"),
		(lambda oid: oid + b" blob 3\nab", 16, "truncated candidate batch"),
		(lambda oid: oid + b" blob 3\nabc\n", 16, "inconsistent with its identity"),
		(lambda oid: oid + b" blob 0\nX", 16, "malformed candidate object framing"),
		(lambda oid: oid + b" blob 0\n\nextra", 16, "unexpected candidate object data"),
	],
)
def test_candidate_snapshot_reaps_process_after_invalid_object_response(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	response_factory: Callable[[bytes], bytes],
	max_blob_bytes: int,
	message: str,
) -> None:
	entry = _staged_entry()
	process = _fake_batch_process(monkeypatch, response_factory(entry.object_id))
	context = _materialization_context(tmp_path, max_blob_bytes=max_blob_bytes)

	with pytest.raises(SnapshotError, match=message):
		snapshot_module._read_git_blobs(context, (entry,), tmp_path / "objects")

	assert process.killed
	assert process.waited
	assert process.wait_timeouts and all(timeout is not None for timeout in process.wait_timeouts)
	assert process.stdin.captured == entry.object_id + b"\n"


def test_candidate_snapshot_requests_repeated_object_only_once(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	assert _git(tmp_path, "init").returncode == 0
	object_id = (
		_git(tmp_path, "hash-object", "--stdin", input_data="shared").stdout.strip().encode("ascii")
	)
	entry = _staged_entry(object_id=object_id)
	second = _staged_entry(object_id=object_id, path=b"duplicate.txt")
	process = _fake_batch_process(monkeypatch, entry.object_id + b" blob 6\nshared\n")
	context = _materialization_context(tmp_path)
	object_directory = tmp_path / "objects"

	object_sizes = snapshot_module._read_git_blobs(context, (entry, second), object_directory)

	assert object_sizes == {entry.object_id: 6}
	assert process.stdin.captured == entry.object_id + b"\n"
	assert (object_directory / entry.object_id.decode("ascii")).read_bytes() == b"shared"
	assert process.waited
	assert process.wait_timeouts and all(timeout is not None for timeout in process.wait_timeouts)
	assert not process.killed


def test_candidate_snapshot_aggregate_bound_scales_with_indexed_paths(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""The index-derived bound must exceed the former fixed 1 GiB ceiling."""
	max_blob_bytes = 64 * 1024 * 1024
	path_count = 1024 * 1024 * 1024 // max_blob_bytes + 1
	entry = _staged_entry()
	entries = tuple(
		_staged_entry(path=f"copy-{index}.txt".encode("ascii")) for index in range(path_count)
	)
	large_object = entry.object_id + b" blob " + str(max_blob_bytes).encode("ascii") + b"\n"
	process = _fake_batch_process(monkeypatch, large_object)
	context = _materialization_context(tmp_path, max_blob_bytes=max_blob_bytes)

	with pytest.raises(SnapshotError, match="truncated candidate batch"):
		snapshot_module._read_git_blobs(context, entries, tmp_path / "objects")

	assert process.killed
	assert process.waited
	assert process.wait_timeouts and all(timeout is not None for timeout in process.wait_timeouts)
	assert process.stdin.captured == entry.object_id + b"\n"


class _BlockingOutput:
	def __init__(self) -> None:
		self.released = threading.Event()

	def read1(self, size: int) -> bytes:
		self.released.wait(BLOCKING_OUTPUT_FAILSAFE_SECONDS)
		return b""

	def read(self, size: int) -> bytes:
		return self.read1(size)

	def release(self) -> None:
		self.released.set()

	def close(self) -> None:
		self.release()


def test_candidate_snapshot_reaps_batch_process_at_deadline(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	process = _fake_batch_process(monkeypatch, _BlockingOutput())

	with pytest.raises(SnapshotError, match="before its deadline"):
		snapshot_module._reject_filtered_paths(tmp_path, (_staged_entry(),), tmp_path / "index", 1)

	assert process.killed
	assert process.waited
	assert process.wait_timeouts and all(timeout is not None for timeout in process.wait_timeouts)
