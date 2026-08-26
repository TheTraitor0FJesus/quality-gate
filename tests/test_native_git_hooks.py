from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

HOOKS = Path(r"C:\Users\Traitor\.codex\MY-SETTINGS\hooks")
SETUP = HOOKS / "setup_native_hooks.py"
HOOKS_DOCUMENTATION = HOOKS.parent / "HOOKS.md"
MANIFEST = "python = []\n\n[quality]\nschema = 1\n"
SETUP_FAILURE_EXIT = 2


def _git(root: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
	result = subprocess.run(
		["git", *arguments],
		cwd=root,
		capture_output=True,
		text=True,
		check=False,
	)
	if check:
		assert result.returncode == 0, result.stderr
	return result


def _commit(root: Path, message: str) -> None:
	_git(
		root,
		"-c",
		"user.name=Native Hook Test",
		"-c",
		"user.email=native-hook@example.invalid",
		"-c",
		f"core.hooksPath={HOOKS}",
		"commit",
		"-m",
		message,
	)


def _run_pre_commit(root: Path) -> subprocess.CompletedProcess[str]:
	return subprocess.run(
		["python", str(HOOKS / "git_pre_commit.py")],
		cwd=root,
		capture_output=True,
		text=True,
		check=False,
	)


@pytest.mark.skipif(os.name != "nt", reason="machine native wrappers use Windows paths")
def test_real_git_commit_preserves_staged_and_unstaged_state(tmp_path: Path) -> None:
	if not (HOOKS / "pre-commit").is_file():
		pytest.skip("machine native hooks are not installed")
	_git(tmp_path, "init", "-b", "main")
	(tmp_path / "quality-gate.toml").write_text(MANIFEST, encoding="utf-8")
	(tmp_path / "README.md").write_text("initial\n", encoding="utf-8")
	_git(tmp_path, "add", ".")
	_commit(tmp_path, "initial")

	tracked = tmp_path / "README.md"
	tracked.write_text("staged\n", encoding="utf-8")
	_git(tmp_path, "add", "README.md")
	tracked.write_text("unstaged\n", encoding="utf-8")
	index_before = _git(tmp_path, "diff", "--cached", "--binary").stdout
	root_entries_before = sorted(path.name for path in tmp_path.iterdir())
	worktree_before = tracked.read_text(encoding="utf-8")
	hook_result = _run_pre_commit(tmp_path)

	assert hook_result.returncode == 0, hook_result.stderr
	assert tracked.read_text(encoding="utf-8") == worktree_before
	assert _git(tmp_path, "diff", "--cached", "--binary").stdout == index_before
	assert sorted(path.name for path in tmp_path.iterdir()) == root_entries_before
	_commit(tmp_path, "staged change")
	assert tracked.read_text(encoding="utf-8") == worktree_before
	assert _git(tmp_path, "diff", "--cached", "--binary").stdout == ""


@pytest.mark.skipif(os.name != "nt", reason="machine native wrappers use Windows paths")
def test_real_git_pre_push_blocks_default_updates_and_allows_feature_pushes(
	tmp_path: Path,
) -> None:
	if not (HOOKS / "pre-push").is_file():
		pytest.skip("machine native hooks are not installed")
	remote = tmp_path / "remote.git"
	_git(tmp_path, "init", "--bare", str(remote))
	_git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
	_git(tmp_path, "init", "-b", "main")
	_git(tmp_path, "remote", "add", "origin", str(remote))
	(tmp_path / "quality-gate.toml").write_text(MANIFEST, encoding="utf-8")
	(tmp_path / "README.md").write_text("initial\n", encoding="utf-8")
	_git(tmp_path, "add", ".")
	_commit(tmp_path, "initial")
	bundle = tmp_path / "initial.bundle"
	_git(tmp_path, "bundle", "create", str(bundle), "HEAD")
	_git(remote, "fetch", str(bundle), "HEAD:refs/heads/main")
	_git(tmp_path, "fetch", "origin")
	_git(tmp_path, "remote", "set-head", "origin", "-a")
	(tmp_path / "README.md").write_text("main update\n", encoding="utf-8")
	_git(tmp_path, "add", "README.md")
	_commit(tmp_path, "main update")

	protected = _git(
		tmp_path,
		"-c",
		f"core.hooksPath={HOOKS}",
		"push",
		"origin",
		"main",
		check=False,
	)
	assert protected.returncode == 1
	assert "default branch" in protected.stderr.lower()

	_git(tmp_path, "switch", "-c", "feature/native-hook")
	(tmp_path / "feature.txt").write_text("feature\n", encoding="utf-8")
	_git(tmp_path, "add", "feature.txt")
	_commit(tmp_path, "feature")
	_git(tmp_path, "-c", f"core.hooksPath={HOOKS}", "push", "-u", "origin", "feature/native-hook")

	deletion = _git(
		tmp_path,
		"-c",
		f"core.hooksPath={HOOKS}",
		"push",
		"origin",
		":main",
		check=False,
	)
	assert deletion.returncode == 1
	assert "default branch" in deletion.stderr.lower()


@pytest.mark.skipif(os.name != "nt", reason="machine setup wrapper uses Windows paths")
def test_native_hook_setup_preserves_unrelated_files_and_refuses_conflicts(
	tmp_path: Path,
) -> None:
	if not SETUP.is_file():
		pytest.skip("machine setup wrapper is not installed")
	empty_global_config = tmp_path / "empty-global-gitconfig"
	empty_global_config.write_text("", encoding="utf-8")
	setup_environment = os.environ.copy()
	setup_environment["GIT_CONFIG_GLOBAL"] = str(empty_global_config)
	setup_environment["GIT_CEILING_DIRECTORIES"] = str(tmp_path)
	root = tmp_path / "repository"
	root.mkdir()
	_git(root, "init", "-b", "main")
	target = tmp_path / "managed-hooks"
	(target / "post-commit").parent.mkdir()
	(target / "post-commit").write_text("unrelated\n", encoding="utf-8")
	first = subprocess.run(
		["python", str(SETUP), "--repository", str(root), "--hooks-dir", str(target)],
		capture_output=True,
		text=True,
		check=False,
		env=setup_environment,
	)
	assert first.returncode == 0, first.stderr
	assert (target / "pre-commit").is_file()
	assert (target / "pre-push").is_file()
	assert (target / "post-commit").read_text(encoding="utf-8") == "unrelated\n"

	(target / "pre-push").write_text("conflict\n", encoding="utf-8")
	second = subprocess.run(
		["python", str(SETUP), "--repository", str(root), "--hooks-dir", str(target)],
		capture_output=True,
		text=True,
		check=False,
		env=setup_environment,
	)
	assert second.returncode == SETUP_FAILURE_EXIT
	assert "different content" in second.stderr

	not_a_repository = tmp_path / "not-a-repository"
	not_a_repository.mkdir()
	third = subprocess.run(
		[
			"python",
			str(SETUP),
			"--repository",
			str(not_a_repository),
			"--hooks-dir",
			str(tmp_path / "must-not-be-created"),
		],
		capture_output=True,
		text=True,
		check=False,
		env=setup_environment,
	)
	assert third.returncode == SETUP_FAILURE_EXIT
	assert "not a Git repository" in third.stderr
	assert not (tmp_path / "must-not-be-created").exists()

	local_hook_root = tmp_path / "local-hook-repository"
	local_hook_root.mkdir()
	_git(local_hook_root, "init", "-b", "main")
	(local_hook_root / ".git" / "hooks" / "post-commit").write_text("#!/bin/sh\n", encoding="utf-8")
	local_hook_result = subprocess.run(
		[
			"python",
			str(SETUP),
			"--repository",
			str(local_hook_root),
			"--hooks-dir",
			str(tmp_path / "local-hook-target"),
		],
		capture_output=True,
		text=True,
		check=False,
		env=setup_environment,
	)
	assert local_hook_result.returncode == SETUP_FAILURE_EXIT
	assert "repository-local hooks exist" in local_hook_result.stderr


@pytest.mark.skipif(os.name != "nt", reason="machine native wrappers use Windows paths")
def test_native_hook_documentation_records_local_bypass_limits() -> None:
	if not HOOKS_DOCUMENTATION.is_file():
		pytest.skip("native hook documentation is not installed")
	documentation = HOOKS_DOCUMENTATION.read_text(encoding="utf-8")

	assert "git commit --no-verify" in documentation
	assert "git push --no-verify" in documentation
	assert "changed global hooks path" in documentation
	assert "unconfigured machine" in documentation
