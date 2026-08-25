from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from quality_gate import snapshot as snapshot_module

SnapshotError = snapshot_module.SnapshotError
candidate_snapshot = snapshot_module.candidate_snapshot


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

	def capture_mkdtemp(**kwargs: object) -> str:
		path = Path(real_mkdtemp(**kwargs))
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
