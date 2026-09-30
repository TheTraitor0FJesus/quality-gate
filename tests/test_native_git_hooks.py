from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest


def _settings_root(
	environment: Mapping[str, str] | None = None, profile: Path | None = None
) -> Path:
	selected_environment = os.environ if environment is None else environment
	configured_root = selected_environment.get("CODEX_HOME")
	if configured_root:
		return Path(configured_root)
	return (Path.home() if profile is None else profile) / ".codex"


def _hook_paths(
	environment: Mapping[str, str] | None = None, profile: Path | None = None
) -> tuple[Path, Path, Path]:
	hooks = _settings_root(environment, profile) / "MY-SETTINGS" / "hooks"
	return hooks, hooks / "setup_native_hooks.py", hooks.parent / "HOOKS.md"


MANIFEST = """\
waivers = []

[quality]
schema = 2
policy_release = "v2.0.5"

[repository]
name = "native-hook-test"
domains = ["repository"]
required_documents = ["quality-gate.toml"]

[repository.limits]
max_blob_size_mib = 5

[repository.defaults]
command_timeout_seconds = 120
test_timeout_seconds = 300
gate_timeout_seconds = 600
"""
SETUP_FAILURE_EXIT = 2
WORKFLOW = """\
name: Quality Gate
on:
  pull_request:
  push:
permissions: read
concurrency:
  group: quality-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true
jobs:
  quality-gate:
    name: Quality Gate
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@0123456789abcdef0123456789abcdef01234567
"""


def test_native_hook_paths_follow_explicit_settings_root(tmp_path: Path) -> None:
	selected_root = tmp_path / "selected"
	profile = tmp_path / "profile"
	assert _hook_paths({"CODEX_HOME": str(selected_root)}, profile) == (
		selected_root / "MY-SETTINGS" / "hooks",
		selected_root / "MY-SETTINGS" / "hooks" / "setup_native_hooks.py",
		selected_root / "MY-SETTINGS" / "HOOKS.md",
	)


def test_native_hook_paths_use_profile_root_when_unset(tmp_path: Path) -> None:
	profile = tmp_path / "profile"
	assert _hook_paths({}, profile)[0] == profile / ".codex" / "MY-SETTINGS" / "hooks"


def test_missing_explicit_native_root_does_not_select_profile_files(tmp_path: Path) -> None:
	selected_root = tmp_path / "missing"
	profile = tmp_path / "profile"
	(profile / ".codex" / "MY-SETTINGS" / "hooks").mkdir(parents=True)
	(profile / ".codex" / "MY-SETTINGS" / "hooks" / "pre-push").write_text(
		"decoy\n", encoding="utf-8"
	)
	selected_hooks = _hook_paths({"CODEX_HOME": str(selected_root)}, profile)[0]

	assert selected_hooks == selected_root / "MY-SETTINGS" / "hooks"
	assert not (selected_hooks / "pre-push").exists()


def _hook_environment(settings_root: Path) -> dict[str, str]:
	environment = os.environ.copy()
	environment["CODEX_HOME"] = str(settings_root)
	environment.setdefault("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
	return environment


def _git(
	root: Path,
	*arguments: str,
	check: bool = True,
	environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
	result = subprocess.run(
		["git", *arguments],
		cwd=root,
		capture_output=True,
		text=True,
		check=False,
		env=environment,
	)
	if check:
		assert result.returncode == 0, result.stderr
	return result


def _commit(root: Path, message: str) -> None:
	hooks, _, _ = _hook_paths()
	_git(
		root,
		"-c",
		"user.name=Native Hook Test",
		"-c",
		"user.email=native-hook@example.invalid",
		"-c",
		f"core.hooksPath={hooks}",
		"commit",
		"-m",
		message,
		environment=_hook_environment(_settings_root()),
	)


def _run_pre_commit(root: Path) -> subprocess.CompletedProcess[str]:
	hooks, _, _ = _hook_paths()
	return subprocess.run(
		["python", str(hooks / "git_pre_commit.py")],
		cwd=root,
		capture_output=True,
		text=True,
		check=False,
		env=_hook_environment(_settings_root()),
	)


def _write_contract(root: Path) -> None:
	(root / "quality-gate.toml").write_text(MANIFEST, encoding="utf-8")
	(root / "README.md").write_text("initial\n", encoding="utf-8")
	workflow = root / ".github" / "workflows" / "quality-gate.yml"
	workflow.parent.mkdir(parents=True)
	workflow.write_text(WORKFLOW, encoding="utf-8")


@pytest.mark.skipif(os.name != "nt", reason="machine native wrappers use Windows paths")
def test_real_git_commit_preserves_staged_and_unstaged_state(tmp_path: Path) -> None:
	hooks, _, _ = _hook_paths()
	if not (hooks / "pre-commit").is_file():
		pytest.skip(
			f"native pre-commit wrapper is unavailable under selected settings root: {hooks}"
		)
	_git(tmp_path, "init", "-b", "main")
	_write_contract(tmp_path)
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
	hooks, _, _ = _hook_paths()
	if not (hooks / "pre-push").is_file():
		pytest.skip(f"native pre-push wrapper is unavailable under selected settings root: {hooks}")
	remote = tmp_path / "remote.git"
	_git(tmp_path, "init", "--bare", str(remote))
	_git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
	_git(tmp_path, "init", "-b", "main")
	_git(tmp_path, "remote", "add", "origin", str(remote))
	_write_contract(tmp_path)
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
		f"core.hooksPath={hooks}",
		"push",
		"origin",
		"main",
		check=False,
		environment=_hook_environment(_settings_root()),
	)
	assert protected.returncode == 1
	assert "default branch" in protected.stderr.lower()

	_git(tmp_path, "switch", "-c", "feature/native-hook")
	(tmp_path / "feature.txt").write_text("feature\n", encoding="utf-8")
	_git(tmp_path, "add", "feature.txt")
	_commit(tmp_path, "feature")
	_git(
		tmp_path,
		"-c",
		f"core.hooksPath={hooks}",
		"push",
		"-u",
		"origin",
		"feature/native-hook",
		environment=_hook_environment(_settings_root()),
	)

	deletion = _git(
		tmp_path,
		"-c",
		f"core.hooksPath={hooks}",
		"push",
		"origin",
		":main",
		check=False,
		environment=_hook_environment(_settings_root()),
	)
	assert deletion.returncode == 1
	assert "default branch" in deletion.stderr.lower()


@pytest.mark.skipif(os.name != "nt", reason="machine setup wrapper uses Windows paths")
def test_native_hook_setup_preserves_unrelated_files_and_refuses_conflicts(
	tmp_path: Path,
) -> None:
	_, setup, _ = _hook_paths()
	if not setup.is_file():
		pytest.skip(f"native setup wrapper is unavailable under selected settings root: {setup}")
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
		["python", str(setup), "--repository", str(root), "--hooks-dir", str(target)],
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
		["python", str(setup), "--repository", str(root), "--hooks-dir", str(target)],
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
			str(setup),
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
			str(setup),
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
	_, _, documentation_path = _hook_paths()
	if not documentation_path.is_file():
		pytest.skip(
			"native hook documentation is unavailable under selected settings root: "
			f"{documentation_path}"
		)
	documentation = documentation_path.read_text(encoding="utf-8")

	assert "git commit --no-verify" in documentation
	assert "git push --no-verify" in documentation
	assert "changed global hooks path" in documentation
	assert "unconfigured machine" in documentation
