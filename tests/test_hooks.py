from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from quality_gate.hook_setup import HookIntegrationError, install_hooks
from quality_gate.hooks import (
	HookInputError,
	PushRef,
	default_branch_names,
	pre_push,
	protected_push_refs,
	read_push_refs,
	run_commit_gate,
)

UNCHECKED_EXIT = 2


def test_read_push_refs_accepts_multiple_push_lines() -> None:
	assert read_push_refs(
		"refs/heads/feature 1111111111111111111111111111111111111111 "
		"refs/heads/feature 2222222222222222222222222222222222222222\n"
		"refs/heads/topic 3333333333333333333333333333333333333333 "
		"refs/heads/topic 4444444444444444444444444444444444444444\n"
	) == (
		PushRef("refs/heads/feature", "refs/heads/feature"),
		PushRef("refs/heads/topic", "refs/heads/topic"),
	)


def test_read_push_refs_keeps_deletion_as_a_push_ref() -> None:
	deleted = "0" * 40
	assert read_push_refs(f"refs/heads/main {'1' * 40} refs/heads/main {deleted}\n") == (
		PushRef("refs/heads/main", "refs/heads/main"),
	)


def test_read_push_refs_rejects_malformed_input() -> None:
	with pytest.raises(HookInputError, match="four fields"):
		read_push_refs("refs/heads/main deadbeef\n")


def test_default_branch_discovery_uses_remote_head_then_safe_fallback() -> None:
	assert default_branch_names("origin", "refs/remotes/origin/trunk") == frozenset({"trunk"})
	assert default_branch_names("origin", "refs/remotes/origin/team/trunk") == frozenset(
		{"team/trunk"}
	)
	assert default_branch_names("origin", None) is None


def test_protected_push_refs_only_contains_default_branch_updates() -> None:
	refs = (
		PushRef("refs/heads/feature", "refs/heads/feature"),
		PushRef("refs/heads/main", "refs/heads/main"),
		PushRef("refs/heads/topic", "refs/heads/topic"),
	)
	assert protected_push_refs(refs, {"main"}) == (refs[1],)


def test_install_hooks_preserves_unrelated_files_and_rejects_conflicts(tmp_path: Path) -> None:
	unrelated = tmp_path / "post-commit"
	unrelated.write_text("unrelated\n", encoding="utf-8")
	wrapper = {"pre-commit": "managed\n", "pre-push": "managed push\n"}

	assert install_hooks(tmp_path, wrapper) == ("pre-commit", "pre-push")
	assert unrelated.read_text(encoding="utf-8") == "unrelated\n"
	assert install_hooks(tmp_path, wrapper) == ()
	(tmp_path / "pre-push").write_text("different\n", encoding="utf-8")

	with pytest.raises(HookIntegrationError, match="different content"):
		install_hooks(tmp_path, wrapper)


def test_install_hooks_rejects_a_non_directory_target(tmp_path: Path) -> None:
	target = tmp_path / "hooks"
	target.write_text("not a directory\n", encoding="utf-8")

	with pytest.raises(HookIntegrationError, match="not a directory"):
		install_hooks(target, {"pre-commit": "managed\n"})


def test_run_commit_gate_reports_an_unavailable_runtime(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	(tmp_path / "quality-gate.toml").write_text("", encoding="utf-8")

	assert run_commit_gate(tmp_path, tmp_path / "missing-runtime") == UNCHECKED_EXIT
	assert "runtime is unavailable" in capsys.readouterr().err


def test_pre_push_reports_invalid_input_as_unchecked(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	import subprocess

	subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
	assert (
		pre_push(
			tmp_path,
			"origin",
			"not a Git pre-push record\n",
		)
		== UNCHECKED_EXIT
	)
	assert "PUSH UNCHECKED" in capsys.readouterr().err


def test_pre_push_reports_unknown_remote_head_as_unchecked(tmp_path: Path) -> None:
	import subprocess

	subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
	assert (
		pre_push(
			tmp_path,
			"origin",
			f"refs/heads/feature {'1' * 40} refs/heads/feature {'2' * 40}\n",
		)
		== UNCHECKED_EXIT
	)


def test_native_pre_push_hook_blocks_default_branch_and_allows_feature_branch(
	tmp_path: Path,
) -> None:
	hook = Path(r"C:\Users\Traitor\.codex\MY-SETTINGS\hooks\git_pre_push.py")
	if not hook.is_file():
		pytest.skip("machine hook is not installed")
	subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
	subprocess.run(
		[
			"git",
			"symbolic-ref",
			"refs/remotes/origin/HEAD",
			"refs/remotes/origin/main",
		],
		cwd=tmp_path,
		check=True,
		capture_output=True,
	)

	def invoke(payload: str) -> subprocess.CompletedProcess[str]:
		return subprocess.run(
			[sys.executable, str(hook), "origin"],
			input=payload,
			capture_output=True,
			text=True,
			cwd=tmp_path,
			check=False,
		)

	feature = invoke(f"refs/heads/feature {'1' * 40} refs/heads/feature {'2' * 40}\n")
	protected = invoke(
		f"refs/heads/feature {'1' * 40} refs/heads/feature {'2' * 40}\n"
		f"refs/heads/main {'1' * 40} refs/heads/main {'2' * 40}\n"
	)
	deletion = invoke(f"(delete) {'0' * 40} refs/heads/main {'0' * 40}\n")
	malformed = invoke("not a Git pre-push record\n")

	assert feature.returncode == 0
	assert protected.returncode == 1
	assert "default branch" in protected.stderr.lower()
	assert deletion.returncode == 1
	assert malformed.returncode == UNCHECKED_EXIT
	assert "push unchecked" in malformed.stderr.lower()
