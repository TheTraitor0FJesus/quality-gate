"""Shared fixtures for working-tree edit-check tests."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

from quality_gate.contracts import load_manifest
from quality_gate.distribution import (
	DistributionError,
	ExternalTool,
	default_cache_root,
	verify_release,
)
from quality_gate.edit_protocol import SUPPORTED_POLICIES as EDIT_SUPPORTED_POLICIES
from quality_gate.release_contract import (
	UNIFIED_RELEASE_DEPENDENCIES,
	UNIFIED_RELEASE_PLATFORM_TOOL_PATHS,
	UNIFIED_RELEASE_POLICY_FILES,
	UNIFIED_RELEASE_TOOLS,
)
from quality_gate.runtime import RuntimeManager

REPOSITORY = Path(__file__).resolve().parents[1]
EXIT_UNCHECKED = 2
EXPECTED_SELECTIONS = 2
FRESH_PROCESSES = 8
REUSED_PROCESSES = 3
REUSED_TWO_SELECTION_PROCESSES = 4
STILL_ACTIVE = 259
TIMEOUT_WITH_CLEANUP_SECONDS = 5
RELEASED_EDIT_POLICIES = frozenset(
	{
		"v2.4.2",
		"v2.5.0",
		"v2.6.0",
		"v2.6.1",
		"v2.6.2",
		"v3.0.0",
		"v3.1.0",
		"v3.2.0",
		"v4.0.0",
		"v4.1.0",
		"v4.2.0",
		"v4.2.1",
		"v4.3.0",
		"v4.4.0",
		"v4.5.0",
		"v4.6.0",
	}
)


def assert_project_files_after_cleanup(root: Path, *names: str) -> None:
	assert {item.name for item in root.iterdir()} == {*names, "temp"}
	temp_root = root / "temp"
	if os.name == "nt":
		assert {item.name for item in temp_root.iterdir()} == {".quality-gate.lock"}
		return
	entries = list(temp_root.iterdir())
	areas = [item for item in entries if item.name.startswith(".quality-gate-cleanup-")]
	assert len(areas) == 1
	area = areas[0]
	assert {item.name for item in entries} == {".quality-gate.lock", area.name}
	assert area.is_dir() and not area.is_symlink()
	assert area.stat().st_mode & 0o077 == 0
	assert {item.name for item in area.iterdir()} == {".quality-gate-cleanup-area"}
	assert (area / ".quality-gate-cleanup-area").is_file()


def digest(content: bytes) -> str:
	return hashlib.sha256(content).hexdigest()


@lru_cache(maxsize=8)
def _selected_ruff_artifact(
	configured_release_path: Path | None,
	selected_cache_path: Path,
	pinned_release: str,
) -> tuple[ExternalTool, bytes]:
	# Release audits select the candidate explicitly while the source manifest remains pinned.
	release_paths: list[tuple[Path, bool]] = []
	if configured_release_path is not None:
		release_paths.append((configured_release_path, True))
	if configured_release_path != selected_cache_path:
		release_paths.append((selected_cache_path, False))

	last_error: DistributionError | OSError | None = None
	for release_path, is_configured in release_paths:
		try:
			manifest = verify_release(release_path)
			if not is_configured and manifest.version != pinned_release:
				last_error = DistributionError(
					f"cached release {pinned_release} contains manifest {manifest.version}"
				)
				continue
			ruff = next((tool for tool in manifest.tools if tool.name == "ruff"), None)
			if ruff is None:
				last_error = DistributionError(
					f"selected release {manifest.version} does not declare Ruff"
				)
				continue
			content = (release_path / ruff.path).read_bytes()
			if digest(content) != ruff.sha256:
				last_error = DistributionError(f"release checksum mismatch: {ruff.path}")
				continue
			return ruff, content
		except (DistributionError, OSError) as error:
			last_error = error

	error = last_error or DistributionError(f"pinned release {pinned_release} is unavailable")
	raise AssertionError(
		f"Prepare the pinned policy release {pinned_release} in the local cache with "
		f"quality-gate sync and quality-gate setup before running this suite: {error}"
	) from error


@pytest.fixture(scope="module")
def edit_cache() -> Iterator[Path]:
	"""Immutable test infrastructure: selected release Ruff, synthetic other inventory."""
	# Avoid ensurepip's Windows path limit inside Gate's nested temporary workspace.
	parent = Path(os.environ["SYSTEMROOT"]) / "Temp" if os.name == "nt" else None
	with tempfile.TemporaryDirectory(prefix="quality-gate-edit-", dir=parent) as directory:
		yield prepare_edit_cache(Path(directory))


def prepare_edit_cache(cache: Path, version: str = "v2.4.2") -> Path:
	selected_release = load_manifest(REPOSITORY).policy_release
	configured_policy_root = os.environ.get("QUALITY_GATE_POLICY_ROOT")
	configured_release_root = Path(configured_policy_root) if configured_policy_root else None
	selected_cache_root = default_cache_root() / "releases" / selected_release
	ruff_tool, ruff_content = _selected_ruff_artifact(
		configured_release_root, selected_cache_root, selected_release
	)
	release = cache / "releases" / version
	release.mkdir(parents=True)
	files = {
		f"quality_gate-{version.removeprefix('v')}-py3-none-any.whl": (
			"artifact",
			b"policy wheel fixture",
		)
	}
	files.update(
		{
			f"{name}-{version}-py3-none-any.whl": ("dependency", b"dependency fixture")
			for name, version in UNIFIED_RELEASE_DEPENDENCIES.items()
		}
	)
	files.update(
		{
			path: ("policy", (REPOSITORY / path).read_bytes())
			for path in UNIFIED_RELEASE_POLICY_FILES
		}
	)
	manifest = ["[release]", f'version = "{version}"']
	for path, (kind, content) in files.items():
		target = release / path
		target.parent.mkdir(parents=True, exist_ok=True)
		target.write_bytes(content)
		manifest.extend(
			[
				"[[release.files]]",
				f'path = "{path}"',
				f'kind = "{kind}"',
				f'sha256 = "{digest(content)}"',
			]
		)
	platform = "windows" if os.name == "nt" else "linux"
	for name, tool_version in UNIFIED_RELEASE_TOOLS.items():
		source_tool = ruff_tool if name == "ruff" else None
		path = (
			source_tool.path
			if source_tool is not None
			else UNIFIED_RELEASE_PLATFORM_TOOL_PATHS[platform].get(
				name, f"{name}-{tool_version}-py3-none-any.whl"
			)
		)
		if source_tool is not None:
			tool_version = source_tool.version
			content = ruff_content
		else:
			content = b"tool fixture"
		(release / path).write_bytes(content)
		manifest.extend(
			[
				"[[release.tools]]",
				f'name = "{name}"',
				f'version = "{tool_version}"',
				f'path = "{path}"',
				f'sha256 = "{digest(content)}"',
			]
		)
	(release / "release.toml").write_text("\n".join(manifest) + "\n", encoding="utf-8")
	result = subprocess.run(
		[
			sys.executable,
			"-B",
			"-m",
			"quality_gate.cli",
			"edit-setup",
			"--policy-release",
			version,
			"--cache-dir",
			str(cache),
		],
		cwd=REPOSITORY,
		capture_output=True,
		check=False,
	)
	assert result.returncode == 0, result.stdout + result.stderr
	return cache


@pytest.fixture(scope="module")
def wheel_cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
	cache = tmp_path_factory.mktemp("wheel-cache")
	for version in sorted(EDIT_SUPPORTED_POLICIES):
		prepare_edit_cache(cache, version)
	return cache


@pytest.fixture(scope="module")
def engine_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
	"""Build immutable packaging infrastructure once; never extract it for execution."""
	root = tmp_path_factory.mktemp("engine-wheel")
	source = root / "source"
	source.mkdir()
	shutil.copy2(REPOSITORY / "pyproject.toml", source)
	shutil.copytree(
		REPOSITORY / "quality_gate",
		source / "quality_gate",
		ignore=shutil.ignore_patterns("__pycache__"),
	)
	# The release build backend lives in the base build environment, not project runtimes.
	build_python = Path(sys.base_prefix) / ("python.exe" if os.name == "nt" else "bin/python3")
	result = subprocess.run(
		[
			str(build_python),
			"-B",
			"-m",
			"pip",
			"wheel",
			"--no-deps",
			"--no-build-isolation",
			"--no-cache-dir",
			"--wheel-dir",
			str(root),
			str(source),
		],
		cwd=root,
		capture_output=True,
		check=False,
		timeout=60,
	)
	assert result.returncode == 0, result.stdout + result.stderr
	return next(root.glob("quality_gate-*.whl"))


def request(root: Path, cache: Path, *paths: str) -> dict[str, Any]:
	return {
		"schema": 1,
		"root": str(root),
		"files": [{"path": path, "sha256": digest((root / path).read_bytes())} for path in paths],
		"cache_dir": str(cache),
		"baseline": {
			"policy_release": "v2.4.2",
			"python": sys.executable,
			"python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
		},
		"deadline_unix_ms": int(time.time() * 1000) + 10_000,
	}


def invoke(payload: object, *, engine_wheel: Path | None = None) -> tuple[int, dict[str, Any]]:
	invocation_environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
	if engine_wheel is not None:
		invocation_environment["PYTHONPATH"] = str(engine_wheel)
	if isinstance(payload, dict) and isinstance(payload.get("root"), str):
		project = Path(payload["root"])
		# Keep mutable result reuse local to this test, independently of shared releases.
		cache_home = str(project.parent / f"{project.name}-user-cache")
		invocation_environment.update(LOCALAPPDATA=cache_home, XDG_CACHE_HOME=cache_home)
	result = subprocess.run(
		[sys.executable, "-B", "-m", "quality_gate.cli", "edit-check"],
		input=payload if isinstance(payload, bytes) else json.dumps(payload).encode(),
		cwd=REPOSITORY if engine_wheel is None else engine_wheel.parent,
		env=invocation_environment,
		capture_output=True,
		check=False,
		timeout=15,
	)
	assert result.stderr == b"", result.stderr.decode(errors="replace")
	return result.returncode, json.loads(result.stdout)


def managed(root: Path, cache: Path, *, version: str = "v2.4.2", setup: bool = True) -> Path:
	(root / "src").mkdir(exist_ok=True)
	(root / "requirements.txt").write_bytes(b"")
	(root / "quality-gate.toml").write_text(
		f'''[quality]
schema = 2
policy_release = "{version}"
[repository]
name = "edit-fixture"
domains = ["repository", "python"]
required_documents = ["quality-gate.toml"]
[[python]]
name = "app"
path = "src"
python_version = "{sys.version_info.major}.{sys.version_info.minor}"
dependency_inputs = ["requirements.txt"]
test_paths = []
tests_applicable = false
tests_reason = "Disposable lint-only fixture."
''',
		encoding="utf-8",
	)
	if setup:
		manifest = load_manifest(root)
		RuntimeManager(cache).ensure(
			root,
			manifest,
			manifest.python[0],
			python_executable=Path(sys.executable),
			install=lambda *_: None,
		)
	path = root / "src/example.py"
	path.write_bytes(b"value=  1\n")
	return path


def runtime_wrapper(root: Path, body: str) -> Path:
	wrapper = root / ("runtime.cmd" if os.name == "nt" else "runtime")
	helper = root / "runtime_helper.py"
	helper.write_text(body, encoding="utf-8")
	if os.name == "nt":
		wrapper.write_text(f'@"{sys.executable}" "{helper}"\n', encoding="utf-8")
	else:
		wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{helper}"\n', encoding="utf-8")
		wrapper.chmod(0o700)
	return wrapper


def process_alive(pid: int) -> bool:
	if os.name == "nt":
		import ctypes
		from ctypes import wintypes

		kernel = ctypes.WinDLL("kernel32", use_last_error=True)
		kernel.OpenProcess.restype = wintypes.HANDLE
		kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
		kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
		kernel.CloseHandle.argtypes = [wintypes.HANDLE]
		handle = kernel.OpenProcess(0x1000, False, pid)
		if not handle:
			return False
		try:
			code = wintypes.DWORD()
			assert kernel.GetExitCodeProcess(handle, ctypes.byref(code))
			return code.value == STILL_ACTIVE
		finally:
			kernel.CloseHandle(handle)
	try:
		os.kill(pid, 0)
	except ProcessLookupError:
		return False
	return True
