from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO, Literal, cast

import pytest

import quality_gate.native_cache as native_cache_module
from quality_gate import runner
from quality_gate.file_lock import open_locked_file
from quality_gate.launcher import PreparedEnvironment, prepare
from quality_gate.native_cache import (
	NativeToolCache,
	NativeToolCacheIdentity,
	NativeToolCacheSelection,
	native_tool_source_workspace,
)

MEASUREMENT_WARM_RUNS = 5
MEASUREMENT_EXPECTED_TOOL_RUNS = (MEASUREMENT_WARM_RUNS + 2) * 2
MEASUREMENT_SOURCE_MTIME_NS = 1_700_000_000_000_000_000


@pytest.fixture(autouse=True)
def use_long_windows_staging_limit_for_test_sandbox(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	if os.name == "nt":
		monkeypatch.setattr(native_cache_module, "_WINDOWS_MAX_PATH", 32767)


def _identity(
	*,
	tool: Literal["ruff", "mypy"] = "ruff",
	runtime_fingerprint: str = "runtime-a",
	effective_options: tuple[str, ...] = ("check", "--config=ruff.toml"),
) -> NativeToolCacheIdentity:
	return NativeToolCacheIdentity(
		repository="C:/repositories/quality-gate",
		engine_version="4.2.0",
		runtime_fingerprint=runtime_fingerprint,
		tool=tool,
		tool_version="0.16.4" if tool == "ruff" else "2.3.1",
		tool_digest="a" * 64 if tool == "ruff" else "b" * 64,
		policy_release="v4.2.0",
		policy_digest="c" * 64,
		platform="win32-amd64",
		python_version="3.11",
		effective_options=effective_options,
		import_roots=("quality_gate",),
	)


def test_compatible_native_cache_is_reused_across_private_snapshot_directories(
	tmp_path: Path,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	first_snapshot_cache = tmp_path / "snapshot-one" / "ruff"
	second_snapshot_cache = tmp_path / "snapshot-two" / "ruff"

	with cache.lease(identity, first_snapshot_cache) as first:
		assert first.storage == "persistent"
		(first.path / "native-state").write_text("warm", encoding="utf-8")

	with cache.lease(identity, second_snapshot_cache) as second:
		assert second.storage == "persistent"
		assert second.path == first.path
		assert (second.path / "native-state").read_text(encoding="utf-8") == "warm"
		assert second.namespace_id == first.namespace_id

	assert not first_snapshot_cache.exists()
	assert not second_snapshot_cache.exists()


def test_persistent_native_tool_workspace_reuses_stable_root_and_content_mtime(
	tmp_path: Path,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	private_cache = tmp_path / "private-cache"
	source = "def add(left: int, right: int) -> int:\n\treturn left + right\n"
	workspace_roots: list[Path] = []
	mtimes: list[int] = []

	for snapshot_name, source_mtime_ns in (("snapshot-one", 1), ("snapshot-two", 2)):
		snapshot_root = tmp_path / snapshot_name
		(snapshot_root / "src").mkdir(parents=True)
		source_path = snapshot_root / "src" / "module.py"
		source_path.write_text(source, encoding="utf-8")
		os.utime(source_path, ns=(source_mtime_ns, source_mtime_ns))
		with cache.lease(identity, private_cache) as selection:
			with native_tool_source_workspace(selection, snapshot_root, private_cache) as (
				effective,
				workspace_root,
			):
				assert effective.storage == "persistent"
				assert workspace_root != snapshot_root
				workspace_roots.append(workspace_root)
				mtimes.append((workspace_root / "src" / "module.py").stat().st_mtime_ns)
				assert (workspace_root / "src" / "module.py").read_text(encoding="utf-8") == source
				source_directory_mode = stat.S_IMODE((workspace_root / "src").stat().st_mode)
				source_file_mode = stat.S_IMODE(
					(workspace_root / "src" / "module.py").stat().st_mode
				)
				assert not source_directory_mode & stat.S_IWRITE
				assert not source_file_mode & stat.S_IWRITE
				if os.name != "nt":
					assert source_directory_mode == 0o500
					assert source_file_mode == 0o400
		staged_root = selection.path / "_s" / "c"
		assert not staged_root.exists()
		assert (selection.path / "_s" / "p").is_dir()

	assert workspace_roots[0] == workspace_roots[1]
	assert mtimes[0] == mtimes[1]


def test_source_cleanup_failure_does_not_override_successful_tool(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	private_cache = tmp_path / "private-cache"
	snapshot_root = tmp_path / "snapshot"
	snapshot_root.mkdir()
	(snapshot_root / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
	tool_completed = False

	def fail_cleanup(*args: Any, **kwargs: Any) -> None:
		raise native_cache_module.NativeToolCacheError("source-cleanup-unavailable")

	with cache.lease(identity, private_cache) as selection:
		with native_tool_source_workspace(selection, snapshot_root, private_cache) as (
			effective,
			workspace_root,
		):
			assert effective.storage == "persistent"
			assert (workspace_root / "module.py").is_file()
			monkeypatch.setattr(
				native_cache_module, "_remove_owned_source_workspace", fail_cleanup
			)
			tool_completed = True

	assert tool_completed


def test_interrupted_native_tool_source_workspace_is_recovered_under_lease(
	tmp_path: Path,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	private_cache = tmp_path / "private-cache"
	snapshot_root = tmp_path / "snapshot"
	(snapshot_root / "src").mkdir(parents=True)
	(snapshot_root.joinpath("src", "module.py")).write_text("VALUE = 2\n", encoding="utf-8")

	with cache.lease(identity, private_cache) as selection:
		stage_parent = selection.path / "_s"
		stable_root = stage_parent / "c"
		owner_marker = stage_parent / "o"
		parent_owner_marker = stage_parent / "p"
		stage_parent.mkdir()
		parent_owner_marker.mkdir()
		stable_root.mkdir(parents=True)
		(stable_root / "partial-copy").write_text("stale", encoding="utf-8")
		owner_marker.mkdir()
		with native_tool_source_workspace(selection, snapshot_root, private_cache) as (
			effective,
			workspace_root,
		):
			assert effective.storage == "persistent"
			assert workspace_root == stable_root
			assert not (workspace_root / "partial-copy").exists()
			assert (workspace_root / "src" / "module.py").read_text(encoding="utf-8") == (
				"VALUE = 2\n"
			)
		assert not owner_marker.exists()
		assert not stable_root.exists()
		assert parent_owner_marker.is_dir()


def test_source_workspace_cleanup_preserves_replacement_stage_parent(
	tmp_path: Path,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	private_cache = tmp_path / "private-cache"
	snapshot_root = tmp_path / "snapshot"
	(snapshot_root / "src").mkdir(parents=True)
	(snapshot_root / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")

	with cache.lease(identity, private_cache) as selection:
		stage_parent = selection.path / "_s"
		preserved_stage = selection.path / "_s-preserved"
		with pytest.raises(native_cache_module.NativeToolCacheError, match="unsafe-source-path"):
			with native_tool_source_workspace(selection, snapshot_root, private_cache):
				stage_parent.rename(preserved_stage)
				stage_parent.mkdir()
				(stage_parent / "p").mkdir()
				(stage_parent / "o").mkdir()
				(stage_parent / "c").mkdir()
				(stage_parent / "c" / "foreign.txt").write_text(
					"preserve", encoding="utf-8"
				)

		assert (stage_parent / "c" / "foreign.txt").read_text(encoding="utf-8") == "preserve"
		assert (preserved_stage / "c" / "src" / "module.py").is_file()


def test_stale_source_recovery_rechecks_stage_parent_identity_before_deletion(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	private_cache = tmp_path / "private-cache"
	snapshot_root = tmp_path / "snapshot"
	snapshot_root.mkdir()
	with cache.lease(identity, private_cache) as seeded:
		stage_parent = seeded.path / "_s"
		stable_root = stage_parent / "c"
		stage_parent.mkdir()
		(stage_parent / "p").mkdir()
		(stage_parent / "o").mkdir()
		stable_root.mkdir()
		(stable_root / "partial-copy").write_text("stale", encoding="utf-8")
	preserved_stage = stage_parent.with_name("_s-preserved")

	def replace_stage_parent(_path: Path) -> None:
		stage_parent.rename(preserved_stage)
		stage_parent.mkdir()
		(stage_parent / "p").mkdir()
		(stage_parent / "o").mkdir()
		(stage_parent / "c").mkdir()
		(stage_parent / "c" / "foreign.txt").write_text("preserve", encoding="utf-8")

	monkeypatch.setattr(native_cache_module, "_make_writable", replace_stage_parent)
	with cache.lease(identity, private_cache) as selection:
		with native_tool_source_workspace(selection, snapshot_root, private_cache) as (
			effective,
			workspace_root,
		):
			assert effective.storage == "private"
			assert effective.fallback_reason == "unsafe-source-path"
			assert workspace_root == snapshot_root

	assert (stage_parent / "c" / "foreign.txt").read_text(encoding="utf-8") == "preserve"
	assert (preserved_stage / "c" / "partial-copy").read_text(encoding="utf-8") == "stale"


def test_markerless_native_tool_source_workspace_is_preserved_and_falls_back(
	tmp_path: Path,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	private_cache = tmp_path / "private-cache"
	snapshot_root = tmp_path / "snapshot"
	snapshot_root.mkdir()

	with cache.lease(identity, private_cache) as selection:
		stage_parent = selection.path / "_s"
		stable_root = stage_parent / "c"
		stage_parent.mkdir()
		stable_root.mkdir(parents=True)
		if os.name != "nt":
			stage_parent.chmod(0o755)
		stage_mode = stat.S_IMODE(stage_parent.stat().st_mode)
		with native_tool_source_workspace(selection, snapshot_root, private_cache) as (
			effective,
			workspace_root,
		):
			assert effective.storage == "private"
			assert effective.path == private_cache
			assert effective.fallback_reason == "unsafe-source-path"
			assert workspace_root == snapshot_root
		assert stable_root.is_dir()
		if os.name != "nt":
			assert stat.S_IMODE(stage_parent.stat().st_mode) == stage_mode


def test_foreign_native_tool_workspace_state_uses_private_source_and_cache(
	tmp_path: Path,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	private_cache = tmp_path / "private-cache"
	snapshot_root = tmp_path / "snapshot"
	snapshot_root.mkdir()

	with cache.lease(identity, private_cache) as selection:
		foreign_state = selection.path / "_s"
		foreign_state.write_text("preserve", encoding="utf-8")
		with native_tool_source_workspace(selection, snapshot_root, private_cache) as (
			effective,
			workspace_root,
		):
			assert effective.storage == "private"
			assert effective.path == private_cache
			assert effective.fallback_reason == "unsafe-source-path"
			assert workspace_root == snapshot_root

	assert foreign_state.read_text(encoding="utf-8") == "preserve"


def test_unreadable_source_staging_uses_private_fallback(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	private_cache = tmp_path / "private-cache"
	snapshot_root = tmp_path / "snapshot"
	snapshot_root.mkdir()
	original_iterdir = Path.iterdir
	stage_parent: Path | None = None
	stage_reads = 0

	def guarded_iterdir(path: Path) -> Any:
		nonlocal stage_reads
		if path == stage_parent:
			stage_reads += 1
			if stage_reads in {3, 4}:
				raise PermissionError("staging path cannot be inspected")
		return original_iterdir(path)

	monkeypatch.setattr(Path, "iterdir", guarded_iterdir)
	with cache.lease(identity, private_cache) as selection:
		stage_parent = selection.path / "_s"
		with native_tool_source_workspace(selection, snapshot_root, private_cache) as (
			effective,
			workspace_root,
		):
			assert effective.storage == "private"
			assert effective.path == private_cache
			assert effective.fallback_reason == "unsafe-source-path"
			assert workspace_root == snapshot_root
	assert stage_reads == 4


def test_failed_initial_source_parent_setup_is_recoverable(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	private_cache = tmp_path / "private-cache"
	snapshot_root = tmp_path / "snapshot"
	snapshot_root.mkdir()
	original_chmod = os.chmod

	with cache.lease(identity, private_cache) as selection:
		stage_parent = selection.path / "_s"

		def fail_stage_parent_chmod(
			path: str | Path,
			mode: int,
			*args: Any,
			**kwargs: Any,
		) -> None:
			if Path(path) == stage_parent:
				raise PermissionError("staging parent permissions are unavailable")
			original_chmod(path, mode, *args, **kwargs)

		monkeypatch.setattr(native_cache_module.os, "chmod", fail_stage_parent_chmod)
		with native_tool_source_workspace(selection, snapshot_root, private_cache) as (
			effective,
			workspace_root,
		):
			assert effective.storage == "private"
			assert effective.fallback_reason == "source-staging-unavailable"
			assert workspace_root == snapshot_root
		assert not stage_parent.exists()

		monkeypatch.setattr(native_cache_module.os, "chmod", original_chmod)
		with native_tool_source_workspace(selection, snapshot_root, private_cache) as (
			effective,
			workspace_root,
		):
			assert effective.storage == "persistent"
			assert workspace_root == selection.path / "_s" / "c"


def test_interrupted_empty_source_parent_setup_is_recovered(tmp_path: Path) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	private_cache = tmp_path / "private-cache"
	snapshot_root = tmp_path / "snapshot"
	snapshot_root.mkdir()
	with cache.lease(identity, private_cache) as selection:
		assert selection.storage == "persistent"
		stage_parent = selection.path / "_s"
		stage_parent.mkdir()
	assert not (stage_parent / "p").exists()

	with cache.lease(identity, private_cache) as selection:
		with native_tool_source_workspace(selection, snapshot_root, private_cache) as (
			effective,
			workspace_root,
		):
			assert effective.storage == "persistent"
			assert workspace_root == stage_parent / "c"
	assert (stage_parent / "p").is_dir()


def test_windows_native_tool_workspace_path_limit_uses_private_source_and_cache(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	if os.name != "nt":
		pytest.skip("Windows subprocess working-directory paths have MAX_PATH limit")
	monkeypatch.setattr(native_cache_module, "_WINDOWS_MAX_PATH", 260)
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	private_cache = tmp_path / "private-cache"
	snapshot_root = tmp_path / "snapshot"
	snapshot_root.mkdir()
	long_cache_root = tmp_path / ("p" * 240)

	with cache.lease(identity, private_cache) as selection:
		long_selection = replace(selection, path=long_cache_root)
		with native_tool_source_workspace(long_selection, snapshot_root, private_cache) as (
			effective,
			workspace_root,
		):
			assert effective.storage == "private"
			assert effective.path == private_cache
			assert effective.fallback_reason == "source-staging-unavailable"
			assert workspace_root == snapshot_root


def test_windows_reparse_point_is_detected_without_path_is_junction(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	path = tmp_path / "junction"
	metadata = SimpleNamespace(st_mode=0, st_file_attributes=0x0400)
	monkeypatch.setattr(Path, "lstat", lambda _path: metadata)
	monkeypatch.setattr(native_cache_module.os, "name", "nt")

	assert native_cache_module._is_link(path)


def test_file_lock_rejects_windows_reparse_point(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	path = tmp_path / "reparse-lock"
	metadata = SimpleNamespace(st_mode=0, st_file_attributes=0x0400)
	original_lstat = Path.lstat

	def reparse_lock_lstat(candidate: Path) -> os.stat_result | SimpleNamespace:
		return metadata if candidate == path else original_lstat(candidate)

	monkeypatch.setattr(Path, "lstat", reparse_lock_lstat)
	with pytest.raises(OSError, match="link or reparse point"):
		open_locked_file(path, blocking=False)


def test_file_lock_rejects_open_handle_that_does_not_match_path(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	path = tmp_path / "lock"
	other_path = tmp_path / "other-lock"
	path.write_bytes(b"\0")
	other_path.write_bytes(b"\0")
	original_open = Path.open

	def open_other_lock(candidate: Path, *args: Any, **kwargs: Any) -> BinaryIO:
		if candidate == path:
			return cast(BinaryIO, original_open(other_path, *args, **kwargs))
		return cast(BinaryIO, original_open(candidate, *args, **kwargs))

	monkeypatch.setattr(Path, "open", open_other_lock)
	with pytest.raises(OSError, match="lock path changed while opening"):
		open_locked_file(path, blocking=True)


def test_file_lock_rejects_hard_linked_lock_files(tmp_path: Path) -> None:
	path = tmp_path / "lock"
	linked_path = tmp_path / "linked-lock"
	path.write_bytes(b"\0")
	try:
		os.link(path, linked_path)
	except OSError:
		pytest.skip("hard links are unavailable for this temporary directory")

	with pytest.raises(OSError, match="unsafe file used as a lock"):
		open_locked_file(path, blocking=False)


def test_cache_directory_reparse_point_falls_back_before_creation(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	blocked_path = cache._cache_root() / identity.tool
	original_is_link = native_cache_module._is_link

	def detect_blocked_path(path: Path) -> bool:
		return path == blocked_path or original_is_link(path)

	monkeypatch.setattr(native_cache_module, "_is_link", detect_blocked_path)
	with pytest.raises(native_cache_module.NativeToolCacheError, match="unsafe-cache-path"):
		cache._ensure_directory(cache._repository_tool_root(identity))
	assert not blocked_path.exists()


def test_cache_root_keeps_lexical_path_and_rejects_reparse_ancestor(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	target = tmp_path / "cache-target"
	target.mkdir()
	link = tmp_path / "cache-link"
	try:
		link.symlink_to(target, target_is_directory=True)
	except OSError:
		pytest.skip("directory symlinks are unavailable for this temporary directory")
	cache_root = link / "provider-cache"
	with monkeypatch.context() as patcher:
		patcher.setattr(
			Path,
			"resolve",
			lambda *_args, **_kwargs: pytest.fail("cache root must remain lexical"),
		)
		cache = NativeToolCache(cache_root)
	assert cache.root == cache_root.absolute()

	with cache.lease(_identity(), tmp_path / "private-cache") as selection:
		assert selection.storage == "private"
		assert selection.fallback_reason == "unsafe-cache-path"
	assert not (target / "provider-cache" / "native-tools").exists()


def test_cache_root_does_not_normalize_symlink_before_parent_component(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	link = tmp_path / "cache-link"
	cache_root = link / ".." / "provider-cache"
	cache = NativeToolCache(cache_root)
	original_is_link = native_cache_module._is_link

	def identify_link(path: Path) -> bool:
		return path == link or original_is_link(path)

	monkeypatch.setattr(native_cache_module, "_is_link", identify_link)

	with cache.lease(_identity(), tmp_path / "private-cache") as selection:
		assert selection.storage == "private"
		assert selection.fallback_reason == "unsafe-cache-path"
	assert not (tmp_path / "provider-cache" / "native-tools").exists()


def test_posix_repository_budget_key_preserves_case_sensitivity(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	if os.name == "nt":
		pytest.skip("Windows repository paths are case-insensitive")
	monkeypatch.setattr(native_cache_module.os, "name", "posix")
	upper = replace(_identity(), repository="/repositories/Foo")
	lower = replace(_identity(), repository="/repositories/foo")

	assert upper.repository_key != lower.repository_key


def test_native_cache_namespace_changes_with_tool_and_compatibility_inputs(
	tmp_path: Path,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	base = _identity()
	identities = (
		base,
		replace(base, repository="C:/repositories/other-checkout"),
		replace(base, engine_version="4.3.0"),
		replace(base, tool="mypy", tool_version="2.3.1", tool_digest="b" * 64),
		replace(base, runtime_fingerprint="runtime-b"),
		replace(base, tool_version="0.17.0"),
		replace(base, tool_digest="d" * 64),
		replace(base, policy_release="v4.3.0"),
		replace(base, policy_digest="e" * 64),
		replace(base, platform="linux-x86_64"),
		replace(base, python_version="3.12"),
		replace(base, effective_options=("check", "--config=other.toml")),
		replace(base, import_roots=("src",)),
	)
	selections: list[NativeToolCacheSelection] = []

	for identity in identities:
		with cache.lease(identity, tmp_path / "private" / identity.tool) as selection:
			selections.append(selection)

	assert all(selection.storage == "persistent" for selection in selections)
	assert len({selection.namespace_id for selection in selections}) == len(identities)
	assert selections[0].path != selections[1].path
	assert selections[0].path != selections[2].path
	assert selections[0].path != selections[3].path


def test_busy_native_cache_uses_private_fallback_and_keeps_tool_executable(
	tmp_path: Path,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache", lease_timeout_seconds=0)
	identity = _identity()
	private_path = tmp_path / "snapshot" / "ruff"

	with cache.lease(identity, private_path) as active:
		assert active.storage == "persistent"
		with cache.lease(identity, private_path) as fallback:
			assert fallback.storage == "private"
			assert fallback.path == private_path
			assert fallback.fallback_reason == "lock-busy"
			assert fallback.namespace_id == active.namespace_id


def test_source_workspace_falls_back_when_namespace_changes_after_lease(
	tmp_path: Path,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	private_cache = tmp_path / "private-cache"
	snapshot_root = tmp_path / "snapshot"
	snapshot_root.mkdir()

	with cache.lease(identity, private_cache) as selection:
		assert selection.storage == "persistent"
		preserved_namespace = selection.path.with_name("preserved-namespace")
		selection.path.rename(preserved_namespace)
		selection.path.mkdir()
		foreign_state = selection.path / "foreign-state"
		foreign_state.write_text("preserve", encoding="utf-8")

		with native_tool_source_workspace(selection, snapshot_root, private_cache) as (
			effective,
			workspace_root,
		):
			assert effective.storage == "private"
			assert effective.fallback_reason == "unsafe-source-path"
			assert effective.path == private_cache
			assert workspace_root == snapshot_root

		assert foreign_state.read_text(encoding="utf-8") == "preserve"
		assert (preserved_namespace / ".quality-gate-native-cache.json").is_file()


def test_unowned_native_cache_directory_is_preserved_and_not_used(tmp_path: Path) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	path = cache.namespace_path(identity)
	path.mkdir(parents=True)
	unknown = path / "user-data"
	unknown.write_text("keep", encoding="utf-8")

	with cache.lease(identity, tmp_path / "snapshot" / "ruff") as selection:
		assert selection.storage == "private"
		assert selection.fallback_reason == "namespace-marker-missing"

	assert unknown.read_text(encoding="utf-8") == "keep"


def test_corrupt_native_cache_marker_uses_private_fallback_without_deleting_state(
	tmp_path: Path,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	with cache.lease(identity, tmp_path / "private-one") as selection:
		assert selection.storage == "persistent"
		state = selection.path / "native-state"
		state.write_text("keep", encoding="utf-8")
		(selection.path / ".quality-gate-native-cache.json").write_text("{", encoding="utf-8")

	with cache.lease(identity, tmp_path / "private-two") as fallback:
		assert fallback.storage == "private"
		assert fallback.fallback_reason == "namespace-marker-invalid"

	assert state.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize(
	"payload",
	(
		"[" * 1500 + "0" + "]" * 1500,
		" " * 5000,
	),
)
def test_pathological_native_cache_marker_uses_private_fallback(
	tmp_path: Path,
	payload: str,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	with cache.lease(identity, tmp_path / "private-one") as selection:
		assert selection.storage == "persistent"
		state = selection.path / "native-state"
		state.write_text("keep", encoding="utf-8")
		(selection.path / ".quality-gate-native-cache.json").write_text(
			payload, encoding="utf-8"
		)

	with cache.lease(identity, tmp_path / "private-two") as fallback:
		assert fallback.storage == "private"
		assert fallback.fallback_reason == "namespace-marker-invalid"

	assert state.read_text(encoding="utf-8") == "keep"


def test_incomplete_native_cache_marker_temporary_is_recovered(tmp_path: Path) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	with cache.lease(identity, tmp_path / "private-one") as selection:
		assert selection.storage == "persistent"
		namespace_path = selection.path
	(namespace_path / ".quality-gate-native-cache.json").unlink()
	temporary = namespace_path / f"..quality-gate-native-cache.json.{os.getpid()}.tmp"
	temporary.write_text("{\"owner\":", encoding="utf-8")

	with cache.lease(identity, tmp_path / "private-two") as recovered:
		assert recovered.storage == "persistent"
		assert recovered.path == namespace_path
	assert not temporary.exists()
	assert (namespace_path / ".quality-gate-native-cache.json").is_file()


def test_cache_eviction_removes_only_inactive_provider_owned_namespaces(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	monkeypatch.setattr(native_cache_module, "_MAX_NAMESPACES_PER_REPOSITORY_TOOL", 4)
	cache = NativeToolCache(tmp_path / "provider-cache")
	identities = tuple(
		replace(_identity(), effective_options=("check", f"--config={index}.toml"))
		for index in range(3)
	)
	paths: list[Path] = []
	for index, identity in enumerate(identities):
		with cache.lease(identity, tmp_path / f"private-{index}") as selection:
			paths.append(selection.path)
			assert selection.storage == "persistent"
	assert all(path.is_dir() for path in paths)

	monkeypatch.setattr(native_cache_module, "_MAX_NAMESPACES_PER_REPOSITORY_TOOL", 2)
	new_identity = replace(
		_identity(),
		effective_options=("check", "--config=new.toml"),
	)
	with cache.lease(new_identity, tmp_path / "private-new") as second:
		assert second.storage == "persistent"
	assert sum(path.exists() for path in paths) == 1


def test_cache_eviction_preserves_namespace_replaced_before_quarantine(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	with cache.lease(identity, tmp_path / "private") as selection:
		assert selection.storage == "persistent"
		namespace_path = selection.path
	owned_state = namespace_path / "owned-state"
	owned_state.write_text("owned", encoding="utf-8")
	preserved_path = namespace_path.with_name("preserved-namespace")
	foreign_state: Path | None = None
	replaced = False
	original_rename = os.rename

	def replace_namespace_before_quarantine(source: str | Path, destination: str | Path) -> None:
		nonlocal foreign_state, replaced
		source_path = Path(source)
		destination_path = Path(destination)
		if (
			not replaced
			and source_path == namespace_path
			and destination_path.name.startswith(".evict-")
		):
			replaced = True
			original_rename(source_path, preserved_path)
			namespace_path.mkdir()
			foreign_state = namespace_path / "foreign-state"
			foreign_state.write_text("keep", encoding="utf-8")
		original_rename(source, destination)

	monkeypatch.setattr(native_cache_module.os, "rename", replace_namespace_before_quarantine)

	assert not cache._try_evict(namespace_path, identity)
	assert replaced
	assert foreign_state is not None
	assert foreign_state.read_text(encoding="utf-8") == "keep"
	assert (preserved_path / owned_state.name).read_text(encoding="utf-8") == "owned"
	assert (preserved_path / ".quality-gate-native-cache.json").is_file()


def test_cache_recovers_interrupted_namespace_eviction(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	with cache.lease(identity, tmp_path / "private-before") as selection:
		assert selection.storage == "persistent"
		namespace_path = selection.path
	original_remove = cache._remove_evicted_namespace

	def interrupt_removal(*_args: Any) -> bool:
		raise SystemExit("simulated process interruption after quarantine")

	monkeypatch.setattr(cache, "_remove_evicted_namespace", interrupt_removal)
	with pytest.raises(SystemExit, match="simulated process interruption"):
		cache._try_evict(namespace_path, identity)
	quarantines = tuple(namespace_path.parent.glob(".evict-*"))
	assert len(quarantines) == 1
	assert not namespace_path.exists()

	monkeypatch.setattr(cache, "_remove_evicted_namespace", original_remove)
	with cache.lease(identity, tmp_path / "private-after") as recovered:
		assert recovered.storage == "persistent"
		assert recovered.path == namespace_path
	assert not tuple(namespace_path.parent.glob(".evict-*"))


def test_cache_recovers_eviction_interrupted_after_marker_removal(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	with cache.lease(identity, tmp_path / "private-before") as selection:
		assert selection.storage == "persistent"
		namespace_path = selection.path
	marker_path = namespace_path / ".quality-gate-native-cache.json"
	marker = json.loads(marker_path.read_text(encoding="utf-8"))
	marker["last_used_ns"] = 0
	marker_path.write_text(json.dumps(marker), encoding="utf-8")
	replacement = replace(
		identity,
		effective_options=("check", "--config=replacement.toml"),
	)
	original_rmdir = Path.rmdir
	interrupted_path: Path | None = None

	def interrupt_final_eviction_rmdir(path: Path) -> None:
		nonlocal interrupted_path
		if path.parent == namespace_path.parent and path.name.startswith(".evict-"):
			interrupted_path = path
			raise SystemExit("simulated process interruption after marker removal")
		original_rmdir(path)

	monkeypatch.setattr(Path, "rmdir", interrupt_final_eviction_rmdir)
	with pytest.raises(SystemExit, match="after marker removal"):
		with cache.lease(replacement, tmp_path / "private-replacement"):
			pytest.fail("eviction interruption must happen before the new lease")
	assert interrupted_path is not None
	assert interrupted_path.is_dir()
	assert not (interrupted_path / ".quality-gate-native-cache.json").exists()
	owner_path = interrupted_path.with_name(interrupted_path.name + ".owner.json")
	assert owner_path.is_file()

	monkeypatch.setattr(Path, "rmdir", original_rmdir)
	with cache.lease(identity, tmp_path / "private-after") as recovered:
		assert recovered.storage == "persistent"
	assert not interrupted_path.exists()
	assert not owner_path.exists()


def test_cache_preserves_unowned_empty_eviction_directory(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	monkeypatch.setattr(native_cache_module, "_MAX_NAMESPACES_PER_REPOSITORY_TOOL", 1)
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	repository_tool_root = cache._repository_tool_root(identity)
	repository_tool_root.mkdir(parents=True)
	unowned = repository_tool_root / f".evict-{identity.namespace_id}-{'0' * 32}"
	unowned.mkdir()

	with cache.lease(identity, tmp_path / "private") as selection:
		assert selection.storage == "private"
		assert selection.fallback_reason == "capacity-limit"
	assert unowned.is_dir()
	assert not tuple(unowned.iterdir())


def test_expired_native_cache_namespace_is_evicted(tmp_path: Path) -> None:
	cache = NativeToolCache(tmp_path / "provider-cache")
	identity = _identity()
	with cache.lease(identity, tmp_path / "private-old") as old:
		old_path = old.path
	marker_path = old_path / ".quality-gate-native-cache.json"
	marker = json.loads(marker_path.read_text(encoding="utf-8"))
	marker["last_used_ns"] = 0
	marker_path.write_text(json.dumps(marker), encoding="utf-8")
	new_identity = replace(
		identity,
		effective_options=("check", "--config=new.toml"),
	)
	with cache.lease(new_identity, tmp_path / "private-new") as new:
		assert new.storage == "persistent"
	assert not old_path.exists()


def test_concurrent_worktrees_use_private_cache_when_namespace_is_busy(
	tmp_path: Path,
) -> None:
	cache_root = tmp_path / "provider-cache"
	cache = NativeToolCache(cache_root, lease_timeout_seconds=0)
	identity = _identity()
	private_path = tmp_path / "worktree-one" / ".ruff_cache"
	other_private_path = tmp_path / "worktree-two" / ".ruff_cache"
	child_script = (
		"import json, sys; "
		"from pathlib import Path; "
		"from quality_gate.native_cache import NativeToolCache, NativeToolCacheIdentity; "
		"identity = NativeToolCacheIdentity(**json.loads(sys.argv[2])); "
		"cache = NativeToolCache(Path(sys.argv[1]), lease_timeout_seconds=0); "
		"context = cache.lease(identity, Path(sys.argv[3])); "
		"selection = context.__enter__(); "
		"selection.path.mkdir(parents=True, exist_ok=True); "
		"(selection.path / 'worktree-state').write_text('child', encoding='utf-8'); "
		"print(json.dumps({'storage': selection.storage, 'reason': selection.fallback_reason, "
		"'path': str(selection.path), 'namespace_id': selection.namespace_id})); "
		"context.__exit__(None, None, None)"
	)

	with cache.lease(identity, private_path) as parent:
		assert parent.storage == "persistent"
		parent_state = parent.path / "owner-state"
		parent_state.write_text("parent", encoding="utf-8")
		result = subprocess.run(
			[
				sys.executable,
				"-c",
				child_script,
				str(cache_root),
				json.dumps(asdict(identity)),
				str(other_private_path),
			],
			capture_output=True,
			check=False,
			text=True,
			timeout=10,
			cwd=Path(__file__).parents[1],
		)
	assert result.returncode == 0, result.stderr
	assert json.loads(result.stdout) == {
		"storage": "private",
		"reason": "lock-busy",
		"path": str(other_private_path),
		"namespace_id": parent.namespace_id,
	}
	assert parent_state.read_text(encoding="utf-8") == "parent"
	assert (other_private_path / "worktree-state").read_text(encoding="utf-8") == "child"


def test_cache_eviction_skips_an_active_namespace(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	monkeypatch.setattr(native_cache_module, "_MAX_NAMESPACES_PER_REPOSITORY_TOOL", 1)
	cache = NativeToolCache(tmp_path / "provider-cache", lease_timeout_seconds=0)
	first_identity = _identity()
	second_identity = replace(
		first_identity,
		effective_options=("check", "--config=second.toml"),
	)
	with cache.lease(first_identity, tmp_path / "private-one") as first:
		with cache.lease(second_identity, tmp_path / "private-two") as second:
			assert second.storage == "private"
			assert second.fallback_reason == "capacity-limit"
		assert first.path.is_dir()


class _NativeToolRunRecorder:
	"""Collect real subprocess duration and the cache selection passed to each tool."""

	def __init__(self, delegate: Callable[..., str]) -> None:
		self.delegate = delegate
		self.current_run = ""
		self.observed: list[dict[str, object]] = []
		self.outputs: list[str] = []
		self.workspace_roots: list[Path] = []

	def __call__(
		self,
		command: list[str],
		root: Path,
		environment: dict[str, str],
		*,
		timeout: float | None = None,
	) -> str:
		tool_name = (
			command[command.index("-m") + 1]
			if "-m" in command
			else Path(command[0]).stem.casefold()
		)
		cache_key = "RUFF_CACHE_DIR" if tool_name == "ruff" else "MYPY_CACHE_DIR"
		started = time.perf_counter()
		status = "passed"
		output = ""
		try:
			output = self.delegate(command, root, environment, timeout=timeout)
			return output
		except runner.QualityGateError:
			status = "failed"
			raise
		finally:
			self.outputs.append(output)
			self.workspace_roots.append(root)
			self.observed.append(
				{
					"run": self.current_run,
					"tool": tool_name,
					"namespace_id": Path(environment[cache_key]).name,
					"cache_dir": environment[cache_key],
					"duration_ms": round((time.perf_counter() - started) * 1000, 2),
					"status": status,
				}
			)


def _assert_persistent_mypy_run(
	recorder: _NativeToolRunRecorder,
	cache_root: Path,
	run_name: str,
) -> str:
	rows = [row for row in recorder.observed if row["run"] == run_name and row["tool"] == "mypy"]
	assert len(rows) == 1
	cache_dir = Path(str(rows[0]["cache_dir"]))
	assert cache_dir.resolve().is_relative_to(cache_root.resolve())
	assert rows[0]["namespace_id"] == cache_dir.name
	return str(rows[0]["namespace_id"])


def _open_file_without_shared_reads(path: Path) -> tuple[object, object]:
	"""Hold a Windows source handle that prevents Ruff from reopening the file."""
	import ctypes
	from ctypes import wintypes

	kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
	create_file = kernel32.CreateFileW
	create_file.argtypes = [
		wintypes.LPCWSTR,
		wintypes.DWORD,
		wintypes.DWORD,
		wintypes.LPVOID,
		wintypes.DWORD,
		wintypes.DWORD,
		wintypes.HANDLE,
	]
	create_file.restype = ctypes.c_void_p
	handle = create_file(
		str(path),
		0x80000000,
		0,
		None,
		3,
		0x80,
		None,
	)
	if handle == ctypes.c_void_p(-1).value or handle is None:
		raise getattr(ctypes, "WinError")(ctypes.get_last_error())
	return kernel32, handle


def _close_file_handle(kernel32: object, handle: object) -> None:
	import ctypes

	close_handle = getattr(kernel32, "CloseHandle")
	close_handle.argtypes = [ctypes.c_void_p]
	close_handle.restype = ctypes.c_int
	if not close_handle(handle):
		raise getattr(ctypes, "WinError")(ctypes.get_last_error())


def _native_tool_command_templates(
	prepared: PreparedEnvironment,
	python: Path,
) -> tuple[tuple[str, list[str]], ...]:
	return (
		(
			"python.component_1.ruff",
			[
				*runner._tool_command(prepared, python, "ruff"),
				"check",
				"--config",
				str(prepared.policy_root / "quality_gate" / "policy" / "ruff.toml"),
				"src",
			],
		),
		(
			"python.component_1.mypy",
			[
				*runner._tool_command(prepared, python, "mypy"),
				"--config-file",
				str(prepared.policy_root / "quality_gate" / "policy" / "mypy.ini"),
				"src",
			],
		),
	)


def _mypy_tool_command_templates(
	prepared: PreparedEnvironment,
	python: Path,
) -> tuple[tuple[str, list[str]], ...]:
	return tuple(
		command
		for command in _native_tool_command_templates(prepared, python)
		if command[0].endswith(".mypy")
	)


def _execute_measured_snapshot(
	tmp_path: Path,
	prepared: PreparedEnvironment,
	recorder: _NativeToolRunRecorder,
	commands: tuple[tuple[str, list[str]], ...],
	name: str,
	source: str,
	additional_sources: dict[str, str] | None = None,
	*,
	package_init: bool = True,
	mtime_overrides_ns: dict[str, int] | None = None,
) -> list[tuple[str, int, str]]:
	recorder.current_run = name
	snapshot_root = tmp_path / name
	source_root = snapshot_root / "src"
	source_root.mkdir(parents=True)
	sources = {"module.py": source, **(additional_sources or {})}
	if package_init:
		sources["__init__.py"] = ""
	for relative_path, content in sources.items():
		path = source_root / relative_path
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(content, encoding="utf-8")
		mtime_ns = (mtime_overrides_ns or {}).get(relative_path, MEASUREMENT_SOURCE_MTIME_NS)
		os.utime(path, ns=(mtime_ns, mtime_ns))
	component = runner.PythonComponent(source_root, None, None, True)
	executed, errors, _outputs = runner._execute_commands(
		snapshot_root,
		runner._safe_environment(str(tmp_path / f"private-{name}")),
		[(check_id, list(command), 30) for check_id, command in commands],
		30,
		time.monotonic() + 90,
		prepared=prepared,
		component=component,
		component_index=1,
	)
	assert executed == [check_id for check_id, _command in commands]
	return [
		(
			error.check_id,
			error.exit_code,
			_tool_finding_text(error),
		)
		for error in errors
	]


def _tool_finding_text(error: runner.QualityGateError) -> str:
	"""Return tool output without the invocation wrapper or temporary config path."""
	message = str(error)
	tool_output = message.partition("\n\n")[2]
	return tool_output or message


def _prepared_with_fixture_dependencies(
	prepared: PreparedEnvironment,
	dependencies: dict[str, str],
) -> PreparedEnvironment:
	"""Add content hashes for isolated fixture dependencies to the prepared runtime."""
	runtime_identity = prepared.runtime_identities[0]
	dependency_inputs = tuple(
		{
			"path": path.replace("\\", "/"),
			"sha256": hashlib.sha256(contents.encode("utf-8")).hexdigest(),
		}
		for path, contents in sorted(dependencies.items())
	)
	updated_identity = replace(
		runtime_identity,
		dependency_inputs=(*runtime_identity.dependency_inputs, *dependency_inputs),
	)
	return replace(
		prepared,
		runtime_identities=(updated_identity, *prepared.runtime_identities[1:]),
	)


def _collect_native_cache_measurements(
	tmp_path: Path,
	prepared: PreparedEnvironment,
	recorder: _NativeToolRunRecorder,
	commands: tuple[tuple[str, list[str]], ...],
) -> None:
	valid_source = "def add(left: int, right: int) -> int:\n\treturn left + right\n"
	assert (
		_execute_measured_snapshot(
			tmp_path, prepared, recorder, commands, "snapshot-cold", valid_source
		)
		== []
	)
	for index in range(MEASUREMENT_WARM_RUNS):
		assert (
			_execute_measured_snapshot(
				tmp_path,
				prepared,
				recorder,
				commands,
				f"snapshot-warm-{index + 1}",
				valid_source,
			)
			== []
		)
	invalid_source = "import os\n\ndef add(left: int, right: int) -> int:\n\treturn 'wrong type'\n"
	changed_source_errors = _execute_measured_snapshot(
		tmp_path, prepared, recorder, commands, "snapshot-source-changed", invalid_source
	)
	assert [error[0] for error in changed_source_errors] == [
		"python.component_1.ruff",
		"python.component_1.mypy",
	]


def _assert_native_cache_measurements(
	observed: list[dict[str, object]],
	cache_root: Path,
) -> dict[str, int]:
	assert len(observed) == MEASUREMENT_EXPECTED_TOOL_RUNS
	payload_counts: dict[str, int] = {}
	for tool_name in ("ruff", "mypy"):
		rows = [row for row in observed if row["tool"] == tool_name]
		assert len({row["namespace_id"] for row in rows}) == 1
		assert all(str(row["cache_dir"]).startswith(str(cache_root)) for row in rows)
		cache_path = Path(str(rows[0]["cache_dir"]))
		assert cache_path.is_dir()
		payload = [
			path
			for path in cache_path.rglob("*")
			if path.is_file() and path.name != ".quality-gate-native-cache.json"
		]
		assert payload, f"{tool_name} did not write native cache payload files"
		payload_counts[tool_name] = len(payload)
	return payload_counts


def _write_native_cache_measurement_report(
	report_path: Path,
	prepared: PreparedEnvironment,
	observed: list[dict[str, object]],
	payload_counts: dict[str, int],
) -> None:
	report_path.write_text(
		json.dumps(
			{
				"python": sys.version.split()[0],
				"platform": platform.platform(),
				"policy_release": prepared.release_manifest.version,
				"pinned_tools": {
					tool.name: tool.version
					for tool in prepared.release_manifest.tools
					if tool.name in {"ruff", "mypy"}
				},
				"snapshot_count": MEASUREMENT_WARM_RUNS + 2,
				"native_cache_payload_files": payload_counts,
				"tool_runs": observed,
			},
			indent=2,
		)
		+ "\n",
		encoding="utf-8",
	)


def test_pinned_native_tools_reuse_and_invalidate_snapshot_cache(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""Opt-in real-tool measurement for snapshots and changed source content."""
	if os.environ.get("QUALITY_GATE_NATIVE_CACHE_INTEGRATION") != "1":
		pytest.skip("set QUALITY_GATE_NATIVE_CACHE_INTEGRATION=1 for pinned-tool evidence")
	report_raw = os.environ.get("QUALITY_GATE_NATIVE_CACHE_REPORT")
	if not report_raw:
		pytest.fail("QUALITY_GATE_NATIVE_CACHE_REPORT must name an external report path")
	repository_root = Path(__file__).parents[1].resolve()
	prepared = prepare(repository_root, repository_root=repository_root)
	python = prepared.runtimes[0].python
	if python is None:
		pytest.fail("the selected pinned component runtime is not ready")
	cache_root = tmp_path / "provider-cache"
	prepared = replace(prepared, cache_root=cache_root)
	recorder = _NativeToolRunRecorder(runner.run)
	monkeypatch.setattr(runner, "run", recorder)
	commands = _native_tool_command_templates(prepared, python)
	_collect_native_cache_measurements(tmp_path, prepared, recorder, commands)
	payload_counts = _assert_native_cache_measurements(recorder.observed, cache_root)
	_write_native_cache_measurement_report(
		Path(report_raw), prepared, recorder.observed, payload_counts
	)


def test_pinned_native_tools_reuse_native_entries_across_snapshots(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""Prove warm native entries are read while both tools execute again."""
	if os.environ.get("QUALITY_GATE_NATIVE_CACHE_INTEGRATION") != "1":
		pytest.skip("set QUALITY_GATE_NATIVE_CACHE_INTEGRATION=1 for pinned-tool evidence")
	if os.name != "nt":
		pytest.skip("Ruff read-block proof uses Windows file-sharing semantics")
	repository_root = Path(__file__).parents[1].resolve()
	prepared = prepare(repository_root, repository_root=repository_root)
	python = prepared.runtimes[0].python
	if python is None:
		pytest.fail("the selected pinned component runtime is not ready")
	prepared = replace(prepared, cache_root=tmp_path / "provider-cache")
	delegate = runner.run

	class _CacheReadProbeRecorder(_NativeToolRunRecorder):
		def __call__(
			self,
			command: list[str],
			root: Path,
			environment: dict[str, str],
			*,
			timeout: float | None = None,
		) -> str:
			if self.current_run == "snapshot-warm" and "RUFF_CACHE_DIR" in environment:
				kernel32, handle = _open_file_without_shared_reads(root / "src" / "module.py")
				try:
					return super().__call__(command, root, environment, timeout=timeout)
				finally:
					_close_file_handle(kernel32, handle)
			return super().__call__(command, root, environment, timeout=timeout)

	base_recorder = _CacheReadProbeRecorder(delegate)
	monkeypatch.setattr(runner, "run", base_recorder)
	commands = tuple(
		(
			check_id,
			[*command[:3], "--verbose", *command[3:]]
			if check_id.endswith(".ruff")
			else [*command[:3], "-v", *command[3:]],
		)
		for check_id, command in _native_tool_command_templates(prepared, python)
	)
	source = "def add(left: int, right: int) -> int:\n\treturn left + right\n"
	cold_findings = _execute_measured_snapshot(
		tmp_path, prepared, base_recorder, commands, "snapshot-cold", source
	)
	warm_findings = _execute_measured_snapshot(
		tmp_path, prepared, base_recorder, commands, "snapshot-warm", source
	)
	assert cold_findings == warm_findings == []
	assert [row["tool"] for row in base_recorder.observed] == [
		"ruff",
		"mypy",
		"ruff",
		"mypy",
	]
	assert base_recorder.observed[0]["namespace_id"] == base_recorder.observed[2]["namespace_id"]
	assert base_recorder.observed[1]["namespace_id"] == base_recorder.observed[3]["namespace_id"]
	assert base_recorder.workspace_roots[0] == base_recorder.workspace_roots[2]
	assert base_recorder.workspace_roots[1] == base_recorder.workspace_roots[3]
	mypy_warm_output = base_recorder.outputs[3].casefold()
	assert "success: no issues found in 2 source files" in mypy_warm_output
	assert "build finished" in mypy_warm_output and "with 0 modules" in mypy_warm_output


def test_pinned_native_cache_invalidation_matches_clean_cache(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""Changed imported stubs and policy inputs match fresh native-cache findings."""
	if os.environ.get("QUALITY_GATE_NATIVE_CACHE_INTEGRATION") != "1":
		pytest.skip("set QUALITY_GATE_NATIVE_CACHE_INTEGRATION=1 for pinned-tool evidence")
	repository_root = Path(__file__).parents[1].resolve()
	prepared = prepare(repository_root, repository_root=repository_root)
	python = prepared.runtimes[0].python
	if python is None:
		pytest.fail("the selected pinned component runtime is not ready")
	base_prepared = replace(prepared, cache_root=tmp_path / "provider-cache")
	recorder = _NativeToolRunRecorder(runner.run)
	monkeypatch.setattr(runner, "run", recorder)
	base_commands = _mypy_tool_command_templates(base_prepared, python)
	baseline_source = (
		"from support import VALUE\n\ndef add(value: int) -> int:\n\treturn value + VALUE\n"
	)
	module_source = (
		"from typing import assert_type\n"
		"from support import VALUE\n\n"
		"assert_type(VALUE, int)\n\n"
		"def add(value: int) -> int:\n\treturn value + VALUE\n"
	)
	integer_dependency = {"support.py": "VALUE = 1\n", "support.pyi": "VALUE: int\n"}
	assert not _execute_measured_snapshot(
		tmp_path,
		base_prepared,
		recorder,
		base_commands,
		"default-policy",
		baseline_source,
		integer_dependency,
		package_init=False,
	)
	default_policy_namespace = next(
		str(row["namespace_id"])
		for row in recorder.observed
		if row["run"] == "default-policy" and row["tool"] == "mypy"
	)

	source_policy_dir = base_prepared.policy_root / "quality_gate" / "policy"
	fixture_policy_root = tmp_path / "fixture-policy"
	fixture_policy_dir = fixture_policy_root / "quality_gate" / "policy"
	shutil.copytree(source_policy_dir, fixture_policy_dir)
	mypy_config = fixture_policy_dir / "mypy.ini"
	original_config = mypy_config.read_text(encoding="utf-8")
	assert "follow_imports = skip" in original_config
	assert "ignore_missing_imports = True" in original_config
	fixture_config = original_config.replace("follow_imports = skip", "follow_imports = normal")
	fixture_config = fixture_config.replace(
		"ignore_missing_imports = True", "ignore_missing_imports = False"
	)
	fixture_config += "\nmypy_path = src\nexplicit_package_bases = True\n"
	assert fixture_config != original_config
	assert "follow_imports = normal" in fixture_config
	assert "ignore_missing_imports = False" in fixture_config
	assert "mypy_path = src" in fixture_config
	assert "explicit_package_bases = True" in fixture_config
	mypy_config.write_text(fixture_config, encoding="utf-8")
	prepared = replace(base_prepared, policy_root=fixture_policy_root)
	commands = _mypy_tool_command_templates(prepared, python)
	assert not _execute_measured_snapshot(
		tmp_path,
		prepared,
		recorder,
		commands,
		"stub-cache-cold",
		module_source,
		integer_dependency,
		package_init=False,
	)
	stub_cold_namespace = _assert_persistent_mypy_run(
		recorder, base_prepared.cache_root, "stub-cache-cold"
	)
	string_dependency = {"support.py": "VALUE = 1\n", "support.pyi": "VALUE: str\n"}
	changed_stub_mtime = MEASUREMENT_SOURCE_MTIME_NS + 1_000_000_000
	changed_snapshot_mtimes = {
		"module.py": changed_stub_mtime,
		"support.pyi": changed_stub_mtime,
	}
	changed_stub_errors = _execute_measured_snapshot(
		tmp_path,
		prepared,
		recorder,
		commands,
		"stub-cache-changed",
		module_source,
		string_dependency,
		package_init=False,
		mtime_overrides_ns=changed_snapshot_mtimes,
	)
	assert len(changed_stub_errors) == 1
	diagnostic = changed_stub_errors[0][2].casefold()
	assert "expression is of type" in diagnostic
	assert "int" in diagnostic and "str" in diagnostic
	clean_stub_prepared = replace(prepared, cache_root=tmp_path / "clean-stub-cache")
	clean_stub_errors = _execute_measured_snapshot(
		tmp_path,
		clean_stub_prepared,
		recorder,
		_mypy_tool_command_templates(clean_stub_prepared, python),
		"stub-clean-changed",
		module_source,
		string_dependency,
		package_init=False,
		mtime_overrides_ns=changed_snapshot_mtimes,
	)
	assert changed_stub_errors == clean_stub_errors, (
		f"persistent findings={changed_stub_errors!r}; clean findings={clean_stub_errors!r}"
	)
	assert [error[0] for error in changed_stub_errors] == ["python.component_1.mypy"]
	stub_changed_namespace = _assert_persistent_mypy_run(
		recorder, base_prepared.cache_root, "stub-cache-changed"
	)
	assert stub_changed_namespace == stub_cold_namespace

	base_mypy_namespace = str(
		next(
			row["namespace_id"]
			for row in recorder.observed
			if row["run"] == "stub-cache-cold" and row["tool"] == "mypy"
		)
	)
	assert base_mypy_namespace != default_policy_namespace

	changed_policy_root = tmp_path / "changed-policy"
	changed_policy_dir = changed_policy_root / "quality_gate" / "policy"
	shutil.copytree(fixture_policy_dir, changed_policy_dir)
	mypy_config = changed_policy_dir / "mypy.ini"
	mypy_config.write_text(
		mypy_config.read_text(encoding="utf-8") + "\n# changed native-cache compatibility input\n",
		encoding="utf-8",
	)
	changed_config_prepared = replace(prepared, policy_root=changed_policy_root)
	changed_config_commands = _mypy_tool_command_templates(changed_config_prepared, python)
	changed_config_errors = _execute_measured_snapshot(
		tmp_path,
		changed_config_prepared,
		recorder,
		changed_config_commands,
		"config-cache-changed",
		module_source,
		string_dependency,
		package_init=False,
		mtime_overrides_ns=changed_snapshot_mtimes,
	)
	clean_config_prepared = replace(
		changed_config_prepared, cache_root=tmp_path / "clean-config-cache"
	)
	clean_config_errors = _execute_measured_snapshot(
		tmp_path,
		clean_config_prepared,
		recorder,
		_mypy_tool_command_templates(clean_config_prepared, python),
		"config-clean-changed",
		module_source,
		string_dependency,
		package_init=False,
		mtime_overrides_ns=changed_snapshot_mtimes,
	)
	assert changed_config_errors == clean_config_errors
	assert changed_config_errors == changed_stub_errors

	changed_config_namespace = next(
		row["namespace_id"]
		for row in recorder.observed
		if row["run"] == "config-cache-changed" and row["tool"] == "mypy"
	)
	assert (
		_assert_persistent_mypy_run(recorder, base_prepared.cache_root, "config-cache-changed")
		== changed_config_namespace
	)
	assert changed_config_namespace != base_mypy_namespace

	dependency_source = (
		"from typing import assert_type\nfrom support import VALUE\n\nassert_type(VALUE, int)\n"
	)
	integer_source_dependency = {"support.py": "VALUE = 1\n"}
	dependency_before = _prepared_with_fixture_dependencies(prepared, integer_source_dependency)
	dependency_before_errors = _execute_measured_snapshot(
		tmp_path,
		dependency_before,
		recorder,
		_mypy_tool_command_templates(dependency_before, python),
		"dependency-cache-before",
		dependency_source,
		integer_source_dependency,
		package_init=False,
	)
	assert dependency_before_errors == []
	dependency_before_namespace = _assert_persistent_mypy_run(
		recorder,
		base_prepared.cache_root,
		"dependency-cache-before",
	)

	string_source_dependency = {"support.py": "VALUE = 'text'\n"}
	dependency_after = _prepared_with_fixture_dependencies(prepared, string_source_dependency)
	dependency_after_commands = _mypy_tool_command_templates(dependency_after, python)
	dependency_after_errors = _execute_measured_snapshot(
		tmp_path,
		dependency_after,
		recorder,
		dependency_after_commands,
		"dependency-cache-after",
		dependency_source,
		string_source_dependency,
		package_init=False,
	)
	assert len(dependency_after_errors) == 1
	dependency_diagnostic = dependency_after_errors[0][2].casefold()
	assert (
		"assert_type" in dependency_diagnostic or "expression is of type" in dependency_diagnostic
	)
	assert "int" in dependency_diagnostic and "str" in dependency_diagnostic
	dependency_after_namespace = _assert_persistent_mypy_run(
		recorder,
		base_prepared.cache_root,
		"dependency-cache-after",
	)
	assert dependency_after_namespace != dependency_before_namespace

	clean_dependency_after = replace(
		dependency_after,
		cache_root=tmp_path / "clean-dependency-cache",
	)
	clean_dependency_errors = _execute_measured_snapshot(
		tmp_path,
		clean_dependency_after,
		recorder,
		_mypy_tool_command_templates(clean_dependency_after, python),
		"dependency-clean-after",
		dependency_source,
		string_source_dependency,
		package_init=False,
	)
	assert dependency_after_errors == clean_dependency_errors
