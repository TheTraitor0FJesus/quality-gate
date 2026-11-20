"""Feature test partition."""

from __future__ import annotations

from snapshot_feature_split72_support import (
	BLOCKING_OUTPUT_FAILSAFE_SECONDS as BLOCKING_OUTPUT_FAILSAFE_SECONDS,
)
from snapshot_feature_split72_support import (
	EXPECTED_BATCH_ATTRIBUTE_PROCESSES as EXPECTED_BATCH_ATTRIBUTE_PROCESSES,
)
from snapshot_feature_split72_support import (
	EXPECTED_BATCH_OBJECT_PROCESSES as EXPECTED_BATCH_OBJECT_PROCESSES,
)
from snapshot_feature_split72_support import (
	Callable as Callable,
)
from snapshot_feature_split72_support import (
	Path as Path,
)
from snapshot_feature_split72_support import (
	SnapshotError as SnapshotError,
)
from snapshot_feature_split72_support import (
	_BlockingOutput as _BlockingOutput,
)
from snapshot_feature_split72_support import (
	_CapturedInput as _CapturedInput,
)
from snapshot_feature_split72_support import (
	_fake_batch_process as _fake_batch_process,
)
from snapshot_feature_split72_support import (
	_FakeBatchProcess as _FakeBatchProcess,
)
from snapshot_feature_split72_support import (
	_git as _git,
)
from snapshot_feature_split72_support import (
	_init_and_stage as _init_and_stage,
)
from snapshot_feature_split72_support import (
	_materialization_context as _materialization_context,
)
from snapshot_feature_split72_support import (
	_staged_entry as _staged_entry,
)
from snapshot_feature_split72_support import (
	candidate_snapshot as candidate_snapshot,
)
from snapshot_feature_split72_support import (
	hashlib as hashlib,
)
from snapshot_feature_split72_support import (
	io as io,
)
from snapshot_feature_split72_support import (
	os as os,
)
from snapshot_feature_split72_support import (
	pytest as pytest,
)
from snapshot_feature_split72_support import (
	snapshot_module as snapshot_module,
)
from snapshot_feature_split72_support import (
	stat as stat,
)
from snapshot_feature_split72_support import (
	subprocess as subprocess,
)
from snapshot_feature_split72_support import (
	threading as threading,
)
from snapshot_feature_split72_support import (
	time as time,
)

from quality_gate import snapshot_batch


def test_candidate_snapshot_preserves_construction_failure_if_registration_fails(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	(tmp_path / "tracked.txt").write_bytes(b"staged\n")
	_init_and_stage(tmp_path, "tracked.txt")
	registered: list[Path] = []
	removed: list[Path] = []
	registration_error = RuntimeError("owner rejected cleanup registration")
	construction_error = SnapshotError("construction failed after staging allocation")
	real_remove = snapshot_module.remove_snapshot

	def fail_candidate_limits(*args: object, **kwargs: object) -> tuple[int, int]:
		raise construction_error

	def reject_registration(path: Path) -> None:
		registered.append(path)
		raise registration_error

	monkeypatch.setattr(snapshot_batch, "_candidate_limits", fail_candidate_limits)
	monkeypatch.setattr(snapshot_module, "remove_snapshot", removed.append)
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
	real_prepare_index = snapshot_batch._prepare_index

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
	monkeypatch.setattr(snapshot_batch, "_prepare_index", expire_after_index_copy)
	with pytest.raises(SnapshotError, match="deadline"):
		with candidate_snapshot(tmp_path, deadline=deadline, register_cleanup=register):
			pass
	try:
		assert len(registered) == 1
		assert registered_at == [deadline + 1]
		assert registered[0].is_dir()
	finally:
		for path in registered:
			snapshot_module.remove_snapshot(path)


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
	real_batch = snapshot_batch._run_git_batch

	def record_git(
		repository: Path,
		arguments: tuple[str, ...],
		timeout_seconds: int,
		*,
		index_file: Path | None = None,
		deadline: float | None = None,
		limit: int = snapshot_module._MAX_DISCOVERY_BYTES,
	) -> subprocess.CompletedProcess[bytes]:
		git_deadlines.append(deadline)
		return real_git(
			repository,
			arguments,
			timeout_seconds,
			index_file=index_file,
			deadline=deadline,
			limit=limit,
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
	monkeypatch.setattr(snapshot_batch, "_run_git_batch", record_batch)
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
	real_prepare_index = snapshot_batch._prepare_index

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
	monkeypatch.setattr(snapshot_batch, "_prepare_index", expire_after_index_copy)
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
@pytest.mark.parametrize("compound", [False, True])
def test_candidate_snapshot_rejects_staged_symlink_escape(tmp_path: Path, compound: bool) -> None:
	assert _git(tmp_path, "init").returncode == 0
	if compound:
		os.symlink("b/../outside", tmp_path / "a")
		os.symlink(".", tmp_path / "b")
	else:
		os.symlink("../outside", tmp_path / "a")
	assert _git(tmp_path, "add", "--", ".").returncode == 0

	with pytest.raises(SnapshotError, match="escapes its snapshot root"):
		with candidate_snapshot(tmp_path):
			pass


def test_candidate_snapshot_rejects_cached_filter_attribute(tmp_path: Path) -> None:
	(tmp_path / "tracked.txt").write_text("content\n", encoding="utf-8")
	_init_and_stage(tmp_path, "tracked.txt")
	(tmp_path / ".gitattributes").write_text("tracked.txt filter=blocked\n", encoding="utf-8")
	assert _git(tmp_path, "add", ".gitattributes").returncode == 0
	assert _git(tmp_path, "config", "filter.blocked.clean", "touch filter-ran; cat").returncode == 0
	(tmp_path / "tracked.txt").write_text("different worktree content\n", encoding="utf-8")

	with pytest.raises(SnapshotError, match="filters are not supported"):
		with candidate_snapshot(tmp_path):
			pass
	assert not (tmp_path / "filter-ran").exists()


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
