from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from quality_gate.distribution import PolicyCache, verify_release
from quality_gate.hooks import pre_push


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


def test_first_push_scans_complete_history_with_released_scanner(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	release = PolicyCache().releases / "v4.7.0"
	if not release.is_dir():
		pytest.skip("the v4.7.0 scanner release is not installed")
	verify_release(release)
	_git(tmp_path, "init", "-b", "main")
	_git(tmp_path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
	(tmp_path / "quality-gate.toml").write_text(
		MANIFEST.replace("v2.0.5", "v4.7.0"), encoding="utf-8"
	)
	(tmp_path / "example.txt").write_text("safe content\n", encoding="utf-8")
	_git(tmp_path, "add", ".")
	_git(
		tmp_path,
		"-c",
		"core.hooksPath=/dev/null",
		"-c",
		"user.name=Test",
		"-c",
		"user.email=test@example.invalid",
		"commit",
		"-m",
		"initial",
	)
	head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
	payload = f"refs/heads/feature {head} refs/heads/feature {'0' * 40}\n"
	assert pre_push(tmp_path, "origin", payload) == 0, capsys.readouterr().err


def test_push_rejects_partial_clone_without_fetching(tmp_path: Path) -> None:
	_git(tmp_path, "init", "-b", "main")
	_git(tmp_path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
	_git(tmp_path, "config", "remote.origin.promisor", "true")
	(tmp_path / "quality-gate.toml").write_text(
		MANIFEST.replace("v2.0.5", "v4.7.0"), encoding="utf-8"
	)
	_git(tmp_path, "add", ".")
	_git(
		tmp_path,
		"-c",
		"core.hooksPath=/dev/null",
		"-c",
		"user.name=Test",
		"-c",
		"user.email=test@example.invalid",
		"commit",
		"-m",
		"initial",
	)
	head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
	assert (
		pre_push(tmp_path, "origin", f"refs/heads/main {head} refs/heads/feature {'0' * 40}\n") == 2
	)


def test_push_rejects_shallow_history(tmp_path: Path) -> None:
	source = tmp_path / "source"
	source.mkdir()
	_git(source, "init", "-b", "main")
	(source / "quality-gate.toml").write_text(
		MANIFEST.replace("v2.0.5", "v4.7.0"), encoding="utf-8"
	)
	_git(source, "add", ".")
	_git(
		source,
		"-c",
		"core.hooksPath=/dev/null",
		"-c",
		"user.name=Test",
		"-c",
		"user.email=test@example.invalid",
		"commit",
		"-m",
		"initial",
	)
	clone = tmp_path / "clone"
	_git(tmp_path, "clone", "--depth=1", source.as_uri(), str(clone))
	_git(clone, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
	head = _git(clone, "rev-parse", "HEAD").stdout.strip()
	assert pre_push(clone, "origin", f"refs/heads/main {head} refs/heads/feature {'0' * 40}\n") == 2


def test_push_rejects_noncommit_tag_target(tmp_path: Path) -> None:
	_git(tmp_path, "init", "-b", "main")
	_git(tmp_path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
	(tmp_path / "quality-gate.toml").write_text(
		MANIFEST.replace("v2.0.5", "v4.7.0"), encoding="utf-8"
	)
	file = tmp_path / "blob.txt"
	file.write_text("not a commit\n", encoding="utf-8")
	blob = _git(tmp_path, "hash-object", "-w", str(file)).stdout.strip()
	assert pre_push(tmp_path, "origin", f"refs/tags/blob {blob} refs/tags/blob {'0' * 40}\n") == 2


def test_first_push_blocks_secret_removed_from_tip(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	release = PolicyCache().releases / "v4.7.0"
	if not release.is_dir():
		pytest.skip("the v4.7.0 scanner release is not installed")
	verify_release(release)
	_git(tmp_path, "init", "-b", "main")
	_git(tmp_path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
	(tmp_path / "quality-gate.toml").write_text(
		MANIFEST.replace("v2.0.5", "v4.7.0"), encoding="utf-8"
	)
	secret = "ghp_" + "aB3dE5fG7hJ9kL1mN2pQ4rS6tU8vW0xYz123"
	file = tmp_path / "example.txt"
	file.write_text(f"token={secret}\n", encoding="utf-8")
	_git(tmp_path, "add", ".")
	commit_args = (
		"-c",
		"core.hooksPath=/dev/null",
		"-c",
		"user.name=Test",
		"-c",
		"user.email=test@example.invalid",
		"commit",
		"-m",
	)
	_git(tmp_path, *commit_args, "add secret")
	file.write_text("safe content\n", encoding="utf-8")
	_git(tmp_path, "add", ".")
	_git(tmp_path, *commit_args, "remove secret")
	head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
	payload = f"refs/heads/feature {head} refs/heads/feature {'0' * 40}\n"
	assert pre_push(tmp_path, "origin", payload) == 1
	output = capsys.readouterr().err
	assert "fingerprint:" in output
	assert secret not in output


@pytest.mark.parametrize("case", ["linear", "force", "overlap", "annotated-tag"])
def test_push_scans_each_ref_history_with_released_scanner(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], case: str
) -> None:
	release = PolicyCache().releases / "v4.7.0"
	if not release.is_dir():
		pytest.skip("the v4.7.0 scanner release is not installed")
	verify_release(release)
	_git(tmp_path, "init", "-b", "main")
	_git(tmp_path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
	(tmp_path / "quality-gate.toml").write_text(
		MANIFEST.replace("v2.0.5", "v4.7.0"), encoding="utf-8"
	)
	file = tmp_path / "example.txt"
	file.write_text("safe\n", encoding="utf-8")
	commit_args = (
		"-c",
		"core.hooksPath=/dev/null",
		"-c",
		"user.name=Test",
		"-c",
		"user.email=test@example.invalid",
		"commit",
		"-m",
	)
	_git(tmp_path, "add", ".")
	_git(tmp_path, *commit_args, "base")
	base = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
	secret = "ghp_" + "aB3dE5fG7hJ9kL1mN2pQ4rS6tU8vW0xYz123"
	file.write_text(f"token={secret}\n", encoding="utf-8")
	_git(tmp_path, "add", ".")
	_git(tmp_path, *commit_args, "secret")
	file.write_text("safe again\n", encoding="utf-8")
	_git(tmp_path, "add", ".")
	_git(tmp_path, *commit_args, "remove")
	head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
	remote = base
	if case == "force":
		_git(tmp_path, "switch", "--detach", base)
		(tmp_path / "other.txt").write_text("other\n", encoding="utf-8")
		_git(tmp_path, "add", ".")
		_git(tmp_path, *commit_args, "divergent")
		remote = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
	local_ref = "refs/heads/feature"
	if case == "annotated-tag":
		_git(
			tmp_path,
			"-c",
			"user.name=Test",
			"-c",
			"user.email=test@example.invalid",
			"tag",
			"-a",
			"fixture",
			head,
			"-m",
			"fixture",
		)
		head = _git(tmp_path, "rev-parse", "refs/tags/fixture").stdout.strip()
		local_ref = "refs/tags/fixture"
		remote = "0" * 40
	payload = f"{local_ref} {head} refs/heads/feature {remote}\n"
	if case == "overlap":
		payload += f"refs/heads/other {head} refs/heads/other {head}\n"
	assert pre_push(tmp_path, "origin", payload) == 1
	output = capsys.readouterr().err
	assert "fingerprint:" in output
	assert secret not in output


def test_git_push_blocks_removed_secret_and_preserves_remote(
	tmp_path: Path,
) -> None:
	release = PolicyCache().releases / "v4.7.0"
	if not release.is_dir():
		pytest.skip("the v4.7.0 scanner release is not installed")
	verify_release(release)
	remote = tmp_path / "remote.git"
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(tmp_path, "init", "--bare", str(remote))
	_git(repo, "init", "-b", "main")
	_git(repo, "remote", "add", "origin", str(remote))
	_git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
	(repo / "quality-gate.toml").write_text(MANIFEST.replace("v2.0.5", "v4.7.0"), encoding="utf-8")
	file = repo / "example.txt"
	file.write_text("safe\n", encoding="utf-8")
	commit_args = (
		"-c",
		"core.hooksPath=/dev/null",
		"-c",
		"user.name=Test",
		"-c",
		"user.email=test@example.invalid",
		"commit",
		"-m",
	)
	_git(repo, "add", ".")
	_git(repo, *commit_args, "base")
	_git(repo, "-c", "core.hooksPath=/dev/null", "push", "origin", "HEAD:refs/heads/feature")
	base = _git(repo, "rev-parse", "HEAD").stdout.strip()
	secret = "ghp_" + "aB3dE5fG7hJ9kL1mN2pQ4rS6tU8vW0xYz123"
	file.write_text(f"token={secret}\n", encoding="utf-8")
	_git(repo, "add", ".")
	_git(repo, *commit_args, "add secret")
	file.write_text("safe again\n", encoding="utf-8")
	_git(repo, "add", ".")
	_git(repo, *commit_args, "remove secret")
	hooks = tmp_path / "hooks"
	hooks.mkdir()
	hook = hooks / "pre-push"
	hook.write_text(
		"#!/bin/sh\n"
		"python -c 'import sys; from pathlib import Path; "
		"from quality_gate.hooks import pre_push; "
		'sys.exit(pre_push(Path.cwd(), sys.argv[1], sys.stdin.read()))\' "$1"\n',
		encoding="utf-8",
	)
	hook.chmod(0o755)
	environment = os.environ.copy()
	environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
	environment["PATH"] = str(Path(sys.executable).parent) + os.pathsep + environment["PATH"]
	_git(repo, "update-ref", "refs/heads/clean", base)
	_git(
		repo,
		"-c",
		f"core.hooksPath={hooks}",
		"push",
		"origin",
		"refs/heads/clean:refs/heads/clean",
		environment=environment,
	)
	assert _git(remote, "rev-parse", "refs/heads/clean").stdout.strip() == base
	result = _git(
		repo,
		"-c",
		f"core.hooksPath={hooks}",
		"push",
		"origin",
		"refs/heads/main:refs/heads/feature",
		check=False,
		environment=environment,
	)
	assert result.returncode != 0
	assert "PUSH BLOCKED" in result.stderr
	assert secret not in result.stderr
	assert _git(remote, "rev-parse", "refs/heads/feature").stdout.strip() == base


def test_push_scans_secret_introduced_by_merge(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	release = PolicyCache().releases / "v4.7.0"
	if not release.is_dir():
		pytest.skip("the v4.7.0 scanner release is not installed")
	verify_release(release)
	_git(tmp_path, "init", "-b", "main")
	_git(tmp_path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
	(tmp_path / "quality-gate.toml").write_text(
		MANIFEST.replace("v2.0.5", "v4.7.0"), encoding="utf-8"
	)
	file = tmp_path / "example.txt"
	file.write_text("base\n", encoding="utf-8")
	commit_args = (
		"-c",
		"core.hooksPath=/dev/null",
		"-c",
		"user.name=Test",
		"-c",
		"user.email=test@example.invalid",
		"commit",
		"-m",
	)
	_git(tmp_path, "add", ".")
	_git(tmp_path, *commit_args, "base")
	base = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
	_git(tmp_path, "switch", "-c", "side")
	file.write_text("side\n", encoding="utf-8")
	_git(tmp_path, "add", ".")
	_git(tmp_path, *commit_args, "side")
	_git(tmp_path, "switch", "main")
	file.write_text("main\n", encoding="utf-8")
	_git(tmp_path, "add", ".")
	_git(tmp_path, *commit_args, "main")
	merge = _git(
		tmp_path,
		"-c",
		"user.name=Test",
		"-c",
		"user.email=test@example.invalid",
		"merge",
		"--no-commit",
		"side",
		check=False,
	)
	assert merge.returncode != 0
	secret = "ghp_" + "aB3dE5fG7hJ9kL1mN2pQ4rS6tU8vW0xYz123"
	file.write_text(f"token={secret}\n", encoding="utf-8")
	_git(tmp_path, "add", ".")
	_git(tmp_path, *commit_args, "merge")
	head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
	assert pre_push(tmp_path, "origin", f"refs/heads/main {head} refs/heads/feature {base}\n") == 1
	output = capsys.readouterr().err
	assert "fingerprint:" in output
	assert secret not in output
