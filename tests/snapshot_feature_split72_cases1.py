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
	EXPECTED_FIXED_DISCOVERY_PROCESSES as EXPECTED_FIXED_DISCOVERY_PROCESSES,
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


def test_candidate_index_identity_stays_bound_to_captured_index(tmp_path: Path) -> None:
	(tmp_path / "tracked.txt").write_text("one\n", encoding="utf-8")
	_init_and_stage(tmp_path, "tracked.txt")
	with pytest.raises(SnapshotError, match="index changed"):
		with candidate_snapshot(tmp_path) as snapshot:
			expected = (
				"git-index-sha256:"
				+ hashlib.sha256(snapshot.repository_index.read_bytes()).hexdigest()
			)
			assert snapshot.index_identity == expected
			(tmp_path / "tracked.txt").write_text("two\n", encoding="utf-8")
			assert _git(tmp_path, "add", "tracked.txt").returncode == 0
			assert snapshot.index_identity == expected


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
			snapshot_module.remove_snapshot(path)


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
			snapshot_module.remove_snapshot(path)


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
			snapshot_module.remove_snapshot(path)


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
			snapshot_module.remove_snapshot(path)


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
	real_remove = snapshot_module.remove_snapshot

	def reject_registration(path: Path) -> None:
		registered.append(path)
		raise registration_error

	monkeypatch.setattr(snapshot_module, "remove_snapshot", removed.append)
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
