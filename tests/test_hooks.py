from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
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


def _selected_settings_root(
	environment: Mapping[str, str] | None = None, profile: Path | None = None
) -> Path:
	selected_environment = os.environ if environment is None else environment
	configured_root = selected_environment.get("CODEX_HOME")
	if configured_root:
		return Path(configured_root)
	return (Path.home() if profile is None else profile) / ".codex"


def _native_pre_push_hook(settings_root: Path | None = None) -> Path:
	root = _selected_settings_root() if settings_root is None else settings_root
	hook = root / "MY-SETTINGS" / "hooks" / "git_pre_push.py"
	if not hook.is_file():
		pytest.skip(f"native pre-push hook is unavailable under selected settings root: {hook}")
	return hook


def test_native_pre_push_hook_uses_explicit_root_and_does_not_fall_back(tmp_path: Path) -> None:
	selected_root = tmp_path / "missing"
	profile = tmp_path / "profile"
	profile_hook = profile / ".codex" / "MY-SETTINGS" / "hooks" / "git_pre_push.py"
	profile_hook.parent.mkdir(parents=True)
	profile_hook.write_text("decoy\n", encoding="utf-8")

	selected_hook_root = _selected_settings_root({"CODEX_HOME": str(selected_root)}, profile)

	assert selected_hook_root == selected_root
	with pytest.raises(pytest.skip.Exception, match="native pre-push hook is unavailable") as missing:
		_native_pre_push_hook(selected_hook_root)
	assert str(selected_root) in str(missing.value)


def test_native_pre_push_hook_uses_profile_root_when_codex_home_is_unset(tmp_path: Path) -> None:
	assert _selected_settings_root({}, tmp_path) == tmp_path / ".codex"


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


def _push_record(
	local_ref: str = "refs/heads/feature",
	local_sha: str = "1" * 40,
	remote_ref: str = "refs/heads/feature",
	remote_sha: str = "2" * 40,
) -> str:
	return f"{local_ref} {local_sha} {remote_ref} {remote_sha}"


def _push_record_of_length(line_length: int) -> str:
	remote_ref = "refs/heads/main"
	local_ref_length = line_length - len(remote_ref) - (2 * 40 + 3)  # two 40-char SHAs + 3 spaces
	local_ref = f"refs/heads/{'a' * (local_ref_length - len('refs/heads/'))}"
	return _push_record(local_ref=local_ref, remote_ref=remote_ref)


@pytest.mark.parametrize(
	"record",
	[
		_push_record(local_sha="g" * 40),
		_push_record(remote_sha="g" * 40),
	],
	ids=["local-sha", "remote-sha"],
)
def test_read_push_refs_rejects_invalid_object_id_in_either_sha_field(record: str) -> None:
	with pytest.raises(HookInputError) as error:
		read_push_refs(f"{record}\n")

	assert str(error.value) == "pre-push input line 1 contains an invalid object id"


@pytest.mark.parametrize(
	"record",
	[
		_push_record(local_ref="refs/heads/bad^name"),
		_push_record(remote_ref="refs/heads/bad^name"),
	],
	ids=["local-ref", "remote-ref"],
)
def test_read_push_refs_rejects_invalid_local_or_remote_ref(record: str) -> None:
	with pytest.raises(HookInputError) as error:
		read_push_refs(f"{record}\n")

	assert str(error.value) == "pre-push input line 1 contains an invalid ref"


def test_read_push_refs_rejects_oversized_payload_before_line_or_record_limits() -> None:
	line = _push_record_of_length(4096)
	assert len(line) == 4096
	payload = (line + "\n") * 256
	assert len(payload.encode("utf-8")) > 1024 * 1024
	assert len(payload.splitlines()) == 256

	with pytest.raises(HookInputError) as error:
		read_push_refs(payload)

	assert str(error.value) == "pre-push input exceeds the size limit"


def test_read_push_refs_accepts_payload_at_size_limit() -> None:
	line = _push_record_of_length(4096)
	payload = (line + "\n") * 255
	remaining_bytes = 1024 * 1024 - len(payload.encode("utf-8"))
	payload += f"{_push_record_of_length(remaining_bytes - 1)}\n"
	assert len(payload.encode("utf-8")) == 1024 * 1024

	assert len(read_push_refs(payload)) == 256


def test_read_push_refs_rejects_overlong_line_within_payload_and_record_limits() -> None:
	line = _push_record_of_length(4097)
	assert len(line) == 4097
	assert len(line.encode("utf-8")) < 1024 * 1024

	with pytest.raises(HookInputError) as error:
		read_push_refs(f"{line}\n")

	assert str(error.value) == "pre-push input exceeds the line limit"


def test_read_push_refs_accepts_line_at_length_limit() -> None:
	line = _push_record_of_length(4096)

	assert len(line) == 4096
	assert len(read_push_refs(f"{line}\n")) == 1


def test_read_push_refs_rejects_too_many_records_within_payload_limit() -> None:
	payload = (_push_record() + "\n") * 1001
	assert len(payload.encode("utf-8")) < 1024 * 1024
	assert all(len(line) < 4096 for line in payload.splitlines())

	with pytest.raises(HookInputError) as error:
		read_push_refs(payload)

	assert str(error.value) == "pre-push input exceeds the line limit"


def test_read_push_refs_accepts_record_count_at_limit() -> None:
	payload = (_push_record() + "\n") * 1000

	assert len(read_push_refs(payload)) == 1000


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
	hook = _native_pre_push_hook()
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
