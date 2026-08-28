from __future__ import annotations

import hashlib
import os
import sys
import time
import zipfile
from pathlib import Path

import pytest

import quality_gate.distribution as distribution
from quality_gate.cli import main
from quality_gate.contracts import load_manifest
from quality_gate.distribution import DistributionError, PolicyCache
from quality_gate.runner import EXIT_UNCHECKED
from quality_gate.runtime import (
	RuntimeManager,
	RuntimeUnavailable,
	runtime_fingerprint,
	runtime_identity,
)


def _release(source: Path, version: str = "v2.0.0") -> None:
	(source / "quality_gate-2.0.0-py3-none-any.whl").write_bytes(b"wheel")
	(source / "policy.txt").write_text("shared policy\n", encoding="utf-8")
	(source / "ruff.exe").write_bytes(b"ruff")

	def digest(name: str) -> str:
		return hashlib.sha256((source / name).read_bytes()).hexdigest()

	wheel_digest = digest("quality_gate-2.0.0-py3-none-any.whl")
	policy_digest = digest("policy.txt")
	ruff_digest = digest("ruff.exe")
	(source / "release.toml").write_text(
		f"""[release]
version = "{version}"

[[release.files]]
path = "quality_gate-2.0.0-py3-none-any.whl"
sha256 = "{wheel_digest}"

[[release.files]]
path = "policy.txt"
sha256 = "{policy_digest}"
kind = "policy"

[[release.tools]]
name = "ruff"
version = "0.15.12"
path = "ruff.exe"
sha256 = "{ruff_digest}"
""",
		encoding="utf-8",
	)


def test_sync_installs_verified_release_and_keeps_previous_for_rollback(tmp_path: Path) -> None:
	source = tmp_path / "source"
	source.mkdir()
	_release(source)
	cache = PolicyCache(tmp_path / "cache")

	cache.sync(source)
	second = tmp_path / "second"
	second.mkdir()
	_release(second, "v2.1.0")
	cache.sync(second)

	assert cache.select("v2.1.0").is_dir()
	assert cache.status()["active"] == "v2.1.0"
	assert cache.rollback().name == "v2.0.0"


def test_sync_installs_a_verified_zip_release(tmp_path: Path) -> None:
	source = tmp_path / "source"
	source.mkdir()
	_release(source)
	archive = tmp_path / "release.zip"
	with zipfile.ZipFile(archive, "w") as zipped:
		for path in source.iterdir():
			zipped.write(path, path.name)

	cache = PolicyCache(tmp_path / "cache")

	assert cache.sync(archive).version == "v2.0.0"
	assert cache.select("v2.0.0").is_dir()


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable permissions are not portable")
def test_sync_restores_executable_permissions_for_zip_tools(tmp_path: Path) -> None:
	source = tmp_path / "source"
	source.mkdir()
	_release(source)
	archive = tmp_path / "release.zip"
	with zipfile.ZipFile(archive, "w") as zipped:
		for path in source.iterdir():
			zipped.write(path, path.name)

	cache = PolicyCache(tmp_path / "cache")
	cache.sync(archive)

	assert os.access(cache.select("v2.0.0") / "ruff.exe", os.X_OK)


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable permissions are not portable")
def test_repeat_sync_repairs_existing_zip_tool_permissions(tmp_path: Path) -> None:
	source = tmp_path / "source"
	source.mkdir()
	_release(source)
	archive = tmp_path / "release.zip"
	with zipfile.ZipFile(archive, "w") as zipped:
		for path in source.iterdir():
			zipped.write(path, path.name)

	cache = PolicyCache(tmp_path / "cache")
	cache.sync(archive)
	installed = cache.select("v2.0.0") / "ruff.exe"
	installed.chmod(installed.stat().st_mode & ~0o111)

	cache.sync(archive)

	assert os.access(installed, os.X_OK)


def test_consumer_policy_sync_is_independent_of_lessons(tmp_path: Path) -> None:
	source = tmp_path / "source"
	source.mkdir()
	_release(source)
	(tmp_path / "lessons").mkdir()
	(tmp_path / "lessons" / "incident.md").write_text("not learned\n", encoding="utf-8")

	cache = PolicyCache(tmp_path / "cache")

	assert cache.sync(source).version == "v2.0.0"
	assert cache.status()["active"] == "v2.0.0"


def test_release_manifest_selects_the_policy_wheel_after_dependency_wheels(
	tmp_path: Path,
) -> None:
	source = tmp_path / "source"
	source.mkdir()
	dependency = source / "dependency-1.0-py3-none-any.whl"
	dependency.write_bytes(b"dependency")
	_release(source)
	manifest_path = source / "release.toml"
	manifest_path.write_text(
		manifest_path.read_text(encoding="utf-8").replace(
			'[[release.files]]\npath = "quality_gate-2.0.0-py3-none-any.whl"',
			f'''[[release.files]]
path = "{dependency.name}"
sha256 = "{hashlib.sha256(dependency.read_bytes()).hexdigest()}"
kind = "dependency"

[[release.files]]
path = "quality_gate-2.0.0-py3-none-any.whl"''',
		),
		encoding="utf-8",
	)

	manifest = distribution.load_release_manifest(source)

	assert manifest.wheel is not None
	assert manifest.wheel.path == "quality_gate-2.0.0-py3-none-any.whl"


def test_sync_rejects_corrupt_artifact_without_installing_it(tmp_path: Path) -> None:
	source = tmp_path / "source"
	source.mkdir()
	_release(source)
	(source / "policy.txt").write_text("tampered\n", encoding="utf-8")
	cache = PolicyCache(tmp_path / "cache")

	with pytest.raises(DistributionError, match="checksum mismatch"):
		cache.sync(source)

	assert not (tmp_path / "cache" / "releases" / "v2.0.0").exists()


def test_corrupt_cached_release_is_quarantined(tmp_path: Path) -> None:
	source = tmp_path / "source"
	source.mkdir()
	_release(source)
	cache = PolicyCache(tmp_path / "cache")
	cache.sync(source)
	(cache.releases / "v2.0.0" / "policy.txt").write_text("tampered\n", encoding="utf-8")

	with pytest.raises(DistributionError, match="unavailable"):
		cache.select("v2.0.0")

	assert list(cache.quarantine.glob("v2.0.0-*"))


def test_sync_lock_blocks_a_second_mutation(tmp_path: Path) -> None:
	source = tmp_path / "source"
	source.mkdir()
	_release(source)
	cache = PolicyCache(tmp_path / "cache")
	cache.root.mkdir()
	cache.lock_path.write_text("other process\n", encoding="ascii")

	with pytest.raises(DistributionError, match="locked"):
		cache.sync(source, lock_timeout_seconds=0)


def test_interrupted_sync_leaves_no_partial_release_and_can_recover(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	source = tmp_path / "source"
	source.mkdir()
	_release(source)
	cache = PolicyCache(tmp_path / "cache")
	real_replace = distribution.os.replace

	def interrupt_staged_release(source_path: str, target_path: str) -> None:
		if Path(target_path).parent == cache.releases:
			raise OSError("simulated replacement interruption")
		real_replace(source_path, target_path)

	monkeypatch.setattr(distribution.os, "replace", interrupt_staged_release)
	try:
		with pytest.raises(OSError, match="interruption"):
			cache.sync(source)
	finally:
		monkeypatch.undo()

	assert not (cache.releases / "v2.0.0").exists()
	assert cache.sync(source).version == "v2.0.0"
	assert cache.select("v2.0.0").is_dir()


def test_prune_requires_confirmation_and_keeps_active_releases(tmp_path: Path) -> None:
	source = tmp_path / "source"
	source.mkdir()
	_release(source)
	cache = PolicyCache(tmp_path / "cache")
	cache.sync(source)
	old = cache.releases / "v9.9.9"
	old.mkdir(parents=True)
	old_timestamp = time.time() - 31 * 24 * 60 * 60
	os.utime(old, (old_timestamp, old_timestamp))

	assert cache.prune(older_than_days=30) == ("v9.9.9",)
	assert old.exists()
	assert cache.prune(confirm=True, older_than_days=30) == ("v9.9.9",)
	assert not old.exists()
	assert cache.select("v2.0.0").is_dir()


def test_runtime_fingerprint_changes_when_dependency_input_changes(tmp_path: Path) -> None:
	manifest_root = Path(__file__).parent / "fixtures" / "valid"
	manifest = load_manifest(manifest_root)
	component = manifest.python[0]
	dependency = tmp_path / "pyproject.toml"
	dependency.write_text("[project]\nname='fixture'\n", encoding="utf-8")
	component = component.__class__(
		component.name,
		component.path,
		component.python_version,
		("pyproject.toml",),
		component.test_paths,
		component.tests_applicable,
		component.tests_reason,
		component.timeout_seconds,
	)
	identity = runtime_identity(
		tmp_path,
		manifest,
		component,
	)
	first = runtime_fingerprint(identity)
	first_digest = identity.dependency_inputs[0]["sha256"]
	dependency.write_text("[project]\nname='changed'\n", encoding="utf-8")
	changed = runtime_fingerprint(runtime_identity(tmp_path, manifest, component))
	assert first != changed
	assert first_digest != hashlib.sha256(dependency.read_bytes()).hexdigest()


def test_runtime_fingerprint_ignores_dependency_line_ending_conversion(tmp_path: Path) -> None:
	manifest_root = Path(__file__).parent / "fixtures" / "valid"
	manifest = load_manifest(manifest_root)
	component = manifest.python[0]
	dependency = tmp_path / "requirements.txt"
	component = component.__class__(
		component.name,
		component.path,
		component.python_version,
		("requirements.txt",),
		component.test_paths,
		component.tests_applicable,
		component.tests_reason,
		component.timeout_seconds,
	)
	dependency.write_bytes(b"package==1.0\r\nother==2.0\r\n")
	windows_fingerprint = runtime_fingerprint(runtime_identity(tmp_path, manifest, component))

	dependency.write_bytes(b"package==1.0\nother==2.0\n")
	git_fingerprint = runtime_fingerprint(runtime_identity(tmp_path, manifest, component))

	assert windows_fingerprint == git_fingerprint


def test_missing_dependency_input_is_unchecked(tmp_path: Path) -> None:
	manifest = load_manifest(Path(__file__).parent / "fixtures" / "valid")
	component = manifest.python[0].__class__(
		manifest.python[0].name,
		manifest.python[0].path,
		manifest.python[0].python_version,
		("missing-lock.txt",),
		(),
		False,
		"missing input test",
		manifest.python[0].timeout_seconds,
	)

	with pytest.raises(RuntimeUnavailable, match="dependency input is unavailable"):
		runtime_identity(tmp_path, manifest, component)


def test_missing_python_executable_is_unchecked(tmp_path: Path) -> None:
	manifest = load_manifest(Path(__file__).parent / "fixtures" / "valid")
	component = manifest.python[0].__class__(
		manifest.python[0].name,
		manifest.python[0].path,
		manifest.python[0].python_version,
		(),
		(),
		False,
		"missing Python test",
		manifest.python[0].timeout_seconds,
	)
	manager = RuntimeManager(tmp_path / "cache")

	with pytest.raises(RuntimeUnavailable, match="Python executable is unavailable"):
		manager.ensure(
			tmp_path,
			manifest,
			component,
			python_executable=tmp_path / "missing-python",
		)


def test_runtime_inspection_is_unchecked_until_metadata_and_python_exist(tmp_path: Path) -> None:
	manifest_root = Path(__file__).parent / "fixtures" / "valid"
	manifest = load_manifest(manifest_root)
	component = manifest.python[0]
	component = component.__class__(
		component.name,
		component.path,
		f"{sys.version_info.major}.{sys.version_info.minor}",
		(),
		component.test_paths,
		component.tests_applicable,
		component.tests_reason,
		component.timeout_seconds,
	)
	manager = RuntimeManager(tmp_path / "cache")

	inspection = manager.inspect(tmp_path, runtime_identity(tmp_path, manifest, component))

	assert not inspection.current
	assert inspection.reason == "runtime is missing"


def test_setup_creates_an_isolated_runtime_and_identity_change_makes_it_stale(
	tmp_path: Path,
) -> None:
	manifest_root = Path(__file__).parent / "fixtures" / "valid"
	manifest = load_manifest(manifest_root)
	component = manifest.python[0]
	component = component.__class__(
		component.name,
		component.path,
		f"{sys.version_info.major}.{sys.version_info.minor}",
		(),
		(),
		False,
		"runtime setup test has no test suite",
		component.timeout_seconds,
	)
	manager = RuntimeManager(tmp_path / "cache")

	prepared = manager.ensure(
		tmp_path,
		manifest,
		component,
		python_executable=Path(sys.executable),
	)
	changed = component.__class__(
		component.name,
		component.path,
		component.python_version,
		(),
		(),
		False,
		component.tests_reason,
		component.timeout_seconds + 1,
	)
	stale = manager.inspect(tmp_path, runtime_identity(tmp_path, manifest, changed))

	assert prepared.current
	assert prepared.python is not None
	assert not stale.current
	assert stale.reason == "runtime is missing"


def test_sync_cli_requires_an_explicit_source_and_installs_release(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
	source = tmp_path / "source"
	source.mkdir()
	_release(source)
	monkeypatch.setattr(
		sys,
		"argv",
		[
			"quality-gate",
			"sync",
			"--source",
			str(source),
			"--cache-dir",
			str(tmp_path / "cache"),
		],
	)

	assert main() == 0
	assert "policy release synced: v2.0.0" in capsys.readouterr().out


def test_doctor_blocks_when_declared_policy_is_not_cached(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	root = tmp_path / "repository"
	root.mkdir()
	manifest = Path(__file__).parent / "fixtures" / "no-python" / "quality-gate.toml"
	(root / "quality-gate.toml").write_bytes(manifest.read_bytes())
	monkeypatch.setattr(
		sys,
		"argv",
		[
			"quality-gate",
			"--root",
			str(root),
			"doctor",
			"--cache-dir",
			str(tmp_path / "cache"),
		],
	)

	assert main() == EXIT_UNCHECKED
	assert "doctor: unchecked" in capsys.readouterr().out
