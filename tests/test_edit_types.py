"""Shared fixtures and helpers for related tests."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any, ClassVar

import pytest

from quality_gate.contracts import load_manifest
from quality_gate.distribution import ReleaseManifest, default_cache_root, verify_release
from quality_gate.edit_type_environment import MYPY_DEPENDENCIES
from quality_gate.path_safety import is_link_or_reparse
from quality_gate.runtime import RuntimeManager, runtime_fingerprint, runtime_identity

REPOSITORY = Path(__file__).resolve().parents[1]
BACKGROUND_SCHEMA = 2
MEASUREMENT_CASES = 4


def digest(content: bytes) -> str:
	return hashlib.sha256(content).hexdigest()


STANDARD_MANAGED_RUNTIME_PTH = {
	"a1_coverage.pth": (
		b'import sys; exec(\'import os\\n\\nif os.getenv("COVERAGE_PROCESS_START") or '
		b'os.getenv("COVERAGE_PROCESS_CONFIG"):\\n try:\\n  import coverage\\n except:\\n  pass'
		b'\\n else:\\n  coverage.process_startup(slug="pth")\')\r\n'
	),
	"pytest-cov.pth": (
		b"import os, sys;exec('if \\'COV_CORE_SOURCE\\' in os.environ:\\n try:\\n  "
		b"from pytest_cov.embed import init\\n  init()\\n except Exception as exc:\\n  "
		b'sys.stderr.write(\\n   "pytest-cov: Failed to setup subprocess coverage. "\\n   '
		b'"Environ: {0!r} "\\n   "Exception: {1!r}\\\\n".format(\\n    '
		b"dict((k, v) for k, v in os.environ.items() if k.startswith(\\'COV_CORE\\')),\\n"
		b"    exc\\n   )\\n  )\\n')\n"
	),
	"distutils-precedence.pth": (
		b"import os; var = 'SETUPTOOLS_USE_DISTUTILS'; enabled = os.environ.get(var, 'local') "
		b"== 'local'; enabled and __import__('_distutils_hack').add_shim(); \n"
	),
}


@pytest.fixture(scope="module")
def typing_policy() -> Path:
	configured = os.environ.get("QUALITY_GATE_POLICY_ROOT")
	root = Path(configured) if configured else default_cache_root() / "releases/v2.4.2"
	assert root.is_dir(), "Sync the pinned policy before running real typing integration."
	verify_release(root)
	return root


@pytest.fixture(scope="module")
def typing_environment(typing_policy: Path) -> Iterator[Path]:
	"""Share immutable offline tool installation; each mutable project gets its own copy."""
	with tempfile.TemporaryDirectory(prefix="typing-tools-") as directory:
		root = Path(directory)
		completed = subprocess.run(
			[sys.executable, "-B", "-m", "venv", "--without-pip", str(root)],
			capture_output=True,
			check=False,
		)
		assert completed.returncode == 0, "Fixture venv preparation failed."
		site = root / (
			"Lib/site-packages"
			if os.name == "nt"
			else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
		)
		release = verify_release(typing_policy)
		wheels = [typing_policy / tool.path for tool in release.tools if tool.name == "mypy"]
		wheels.extend(
			typing_policy / item.path for item in release.files if item.kind == "dependency"
		)
		completed = subprocess.run(
			[
				sys.executable,
				"-B",
				"-m",
				"pip",
				"install",
				"--no-index",
				"--no-deps",
				"--no-compile",
				"--target",
				str(site),
				*(str(wheel) for wheel in wheels),
			],
			capture_output=True,
			check=False,
		)
		assert completed.returncode == 0, "Fixture offline released-tool installation failed."
		yield root


@pytest.fixture(scope="module")
def candidate_engine_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
	root = tmp_path_factory.mktemp("candidate-engine-wheel")
	source = root / "source"
	source.mkdir()
	shutil.copy2(REPOSITORY / "pyproject.toml", source)
	shutil.copytree(
		REPOSITORY / "quality_gate",
		source / "quality_gate",
		ignore=shutil.ignore_patterns("__pycache__"),
	)
	build_python = Path(sys.base_prefix) / ("python.exe" if os.name == "nt" else "bin/python3")
	completed = subprocess.run(
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
		capture_output=True,
		check=False,
		timeout=45,
	)
	assert completed.returncode == 0, completed.stdout + completed.stderr
	wheels = list(root.glob("quality_gate-*.whl"))
	assert len(wheels) == 1
	with zipfile.ZipFile(wheels[0]) as archive:
		version_source = archive.read("quality_gate/__init__.py").decode("utf-8")
	assert f'__version__ = "{wheels[0].name.split("-")[1]}"' in version_source
	return wheels[0]


@pytest.fixture(scope="module")
def candidate_typing_cache(
	typing_policy: Path,
	candidate_engine_wheel: Path,
) -> Iterator[Path]:
	from test_edit_check import prepare_edit_cache

	cache_parent = _candidate_typing_cache_parent(typing_policy)
	with tempfile.TemporaryDirectory(prefix="qg-candidate-", dir=cache_parent) as directory:
		cache = Path(directory).resolve()
		prepare_edit_cache(cache, "v4.7.0")
		candidate_root = cache / "releases/v4.7.0"
		candidate = verify_release(candidate_root)
		published = verify_release(typing_policy)
		replacements: dict[str, Path] = {}
		product = next(item for item in candidate.files if item.kind == "artifact")
		assert Path(product.path).name == candidate_engine_wheel.name
		replacements[product.path] = candidate_engine_wheel
		old_mypy = next(item for item in published.tools if item.name == "mypy")
		new_mypy = next(item for item in candidate.tools if item.name == "mypy")
		if new_mypy.path != old_mypy.path:
			(candidate_root / new_mypy.path).unlink()
			manifest_path = candidate_root / "release.toml"
			manifest_text = manifest_path.read_text(encoding="utf-8")
			old_path = f'path = "{new_mypy.path}"'
			assert manifest_text.count(old_path) == 1
			manifest_path.write_text(
				manifest_text.replace(old_path, f'path = "{old_mypy.path}"', 1),
				encoding="utf-8",
			)
			destination = candidate_root / old_mypy.path
			destination.parent.mkdir(parents=True, exist_ok=True)
			shutil.copy2(typing_policy / old_mypy.path, destination)
			replacements[old_mypy.path] = typing_policy / old_mypy.path
		else:
			replacements[new_mypy.path] = typing_policy / old_mypy.path
		replacements.update(
			_candidate_mypy_dependency_replacements(candidate, published, typing_policy)
		)
		provider_policy_root = typing_policy
		provider_policy = verify_release(provider_policy_root)
		old_gitleaks = next(item for item in provider_policy.tools if item.name == "gitleaks")
		new_gitleaks = next(item for item in candidate.tools if item.name == "gitleaks")
		assert old_gitleaks.version == new_gitleaks.version
		replacements[new_gitleaks.path] = provider_policy_root / old_gitleaks.path
		# The real mypy runtime is seeded from the supported-policy fixture above.
		# Its dependency wheels must match that copied environment's RECORD files.
		for relative, source_path in replacements.items():
			shutil.copy2(source_path, candidate_root / relative)
		_update_candidate_release_digests(candidate_root, replacements)
		verified = verify_release(candidate_root)
		assert verified.version == "v4.7.0"
		assert (candidate_root / product.path).read_bytes() == candidate_engine_wheel.read_bytes()
		yield cache


def _candidate_typing_cache_parent(typing_policy: Path) -> Path:
	cache_parent = typing_policy.parent.parent
	if os.name == "nt":
		temp_parent = Path(tempfile.gettempdir()).parent
		try:
			temp_parent_identity = temp_parent.lstat()
		except OSError:
			temp_parent_identity = None
		if (
			temp_parent_identity is not None
			and not is_link_or_reparse(temp_parent_identity)
			and temp_parent.is_dir()
			and os.access(temp_parent, os.W_OK)
			and len(os.fspath(temp_parent)) < len(os.fspath(cache_parent))
		):
			# Keep the candidate runtime short enough for Windows to open mypy's typeshed files.
			cache_parent = temp_parent
	if is_link_or_reparse(cache_parent.lstat()) or not cache_parent.is_dir():
		raise AssertionError("Candidate typing cache requires an ordinary parent directory.")
	return cache_parent


def _candidate_mypy_dependency_replacements(
	candidate: ReleaseManifest, published: ReleaseManifest, typing_policy: Path
) -> dict[str, Path]:
	replacements: dict[str, Path] = {}
	for item in candidate.files:
		if item.kind != "dependency":
			continue
		name, version, *_tags = Path(item.path).name.split("-")
		if name not in MYPY_DEPENDENCIES:
			continue
		source = next(
			(
				entry
				for entry in published.files
				if entry.kind == "dependency" and Path(entry.path).name.split("-")[0] == name
			),
			None,
		)
		if source is None:
			raise AssertionError(f"No supported-policy wheel for mypy dependency {name}")
		source_version = Path(source.path).name.split("-")[1]
		assert source_version == version, f"Mypy dependency {name} changed version."
		replacements[item.path] = typing_policy / source.path
	return replacements


def _candidate_worker_guard() -> str:
	return (
		"import socket, subprocess\n"
		"def deny_network(*args, **kwargs):\n"
		"    raise RuntimeError('network access is disabled for this edit check')\n"
		"socket.socket.connect = deny_network\n"
		"socket.socket.connect_ex = deny_network\n"
		"socket.create_connection = deny_network\n"
		"original_popen = subprocess.Popen\n"
		"def guard_worker_popen(args, *positional, **keywords):\n"
		"    values = [str(value) for value in args] if not isinstance(args, str) else [args]\n"
		"    command = ' '.join(values)\n"
		"    mypy = \"runpy.run_module('mypy'\" in command\n"
		"    runtime_probe = \"'base_executable'\" in command and \"'stdlib'\" in command\n"
		"    if not (mypy or runtime_probe):\n"
		"        raise RuntimeError('unexpected worker child process: ' + command)\n"
		"    return original_popen(args, *positional, **keywords)\n"
		"subprocess.Popen = guard_worker_popen\n"
	)


def _update_candidate_release_digests(candidate_root: Path, replacements: dict[str, Path]) -> None:
	manifest_path = candidate_root / "release.toml"
	lines = manifest_path.read_text(encoding="utf-8").splitlines()
	for relative in replacements:
		declaration = f'path = "{relative}"'
		try:
			path_index = next(i for i, line in enumerate(lines) if line.strip() == declaration)
		except StopIteration:
			raise AssertionError(f"No release declaration for {relative}") from None
		section_end = next(
			(i for i in range(path_index + 1, len(lines)) if lines[i].startswith("[[release.")),
			len(lines),
		)
		digest_index = next(
			(i for i in range(path_index + 1, section_end) if lines[i].startswith("sha256 = ")),
			None,
		)
		if digest_index is None:
			raise AssertionError(f"No digest declaration for {relative}")
		lines[digest_index] = f'sha256 = "{digest((candidate_root / relative).read_bytes())}"'
	manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def request(root: Path, policy: Path, *paths: str, background: bool = False) -> dict[str, Any]:
	payload: dict[str, Any] = {
		"schema": 1,
		"root": str(root),
		"files": [
			{
				"path": path,
				"sha256": digest((root / path).read_bytes()) if (root / path).is_file() else None,
			}
			for path in paths
		],
		"cache_dir": str(policy.parent.parent),
		"baseline": {"policy_release": policy.name},
		"deadline_unix_ms": int(time.time() * 1000) + 10_000,
	}
	if background:
		event_id = str(uuid.uuid4())
		payload.update(
			{
				"schema": 2,
				"mode": "background",
				"event_id": event_id,
				"cancel_path": str(
					policy.parent.parent / "edit-types-events" / f"{event_id}.cancel"
				),
				"deadline_unix_ms": int(time.time() * 1000) + 30_000,
			}
		)
	return payload


def invoke(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
	completed = subprocess.run(
		[sys.executable, "-B", "-m", "quality_gate.cli", "edit-types"],
		cwd=REPOSITORY,
		input=json.dumps(payload).encode(),
		capture_output=True,
		check=False,
		timeout=33 if payload["schema"] == BACKGROUND_SCHEMA else 13,
	)
	assert completed.stderr == b"", "The public typing transport leaked stderr."
	return completed.returncode, json.loads(completed.stdout)


def invoke_candidate_wheel(
	payload: dict[str, Any],
	wheel: Path,
	*,
	command: str = "edit-types",
	network_guard: bool = False,
) -> tuple[int, dict[str, Any]]:
	project = Path(payload["root"])
	cache = Path(payload["cache_dir"])
	launcher = [sys.executable, "-B", "-m", "quality_gate.cli", command]
	if network_guard:
		worker_guard = _candidate_worker_guard()
		guard = (
			"import runpy, socket, subprocess, sys\n"
			"def deny_network(*args, **kwargs):\n"
			"    raise RuntimeError('network access is disabled for this edit check')\n"
			"socket.socket.connect = deny_network\n"
			"socket.socket.connect_ex = deny_network\n"
			"socket.create_connection = deny_network\n"
			"original_popen = subprocess.Popen\n"
			"worker_guard = sys.argv[2]\n"
			"def guard_popen(args, *positional, **keywords):\n"
			"    values = [str(value) for value in args] if not isinstance(args, str) else [args]\n"
			"    command = ' '.join(values)\n"
			"    mypy = \"runpy.run_module('mypy'\" in command\n"
			"    runtime_probe = \"'base_executable'\" in command and \"'stdlib'\" in command\n"
			"    worker = 'from quality_gate.edit_worker import main' in command\n"
			"    if not (mypy or runtime_probe or worker):\n"
			"        raise RuntimeError('unexpected installer or network process: ' + command)\n"
			"    if worker:\n"
			"        code_index = values.index('-c') + 1\n"
			"        values[code_index] = worker_guard + '\\n' + values[code_index]\n"
			"        args = values\n"
			"    return original_popen(args, *positional, **keywords)\n"
			"subprocess.Popen = guard_popen\n"
			"sys.argv = ['quality-gate', sys.argv[1]]\n"
			"runpy.run_module('quality_gate.cli', run_name='__main__')\n"
		)
		launcher = [sys.executable, "-B", "-c", guard, command, worker_guard]
	completed = subprocess.run(
		launcher,
		cwd=project.parent,
		env={
			**os.environ,
			"PYTHONPATH": str(wheel),
			"PYTHONDONTWRITEBYTECODE": "1",
			"LOCALAPPDATA": str(cache.parent / "cli-user-cache"),
			"XDG_CACHE_HOME": str(cache.parent / "cli-user-cache"),
		},
		input=json.dumps(payload).encode(),
		capture_output=True,
		check=False,
		timeout=33 if payload["schema"] == BACKGROUND_SCHEMA else 13,
	)
	assert completed.stderr == b"", completed.stderr.decode("utf-8", errors="replace")
	result = json.loads(completed.stdout)
	return completed.returncode, result


class _StubSelection:
	stdlib: ClassVar[set[str]] = set()
	site = Path("/missing/site")


def _argus_typing_manifest() -> str:
	return f'''[quality]
schema = 2
policy_release = "v4.7.0"
[repository]
name = "argus-source-roots"
domains = ["repository", "python"]
required_documents = ["quality-gate.toml"]
[[python]]
name = "bot"
path = "bot"
python_version = "{sys.version_info.major}.{sys.version_info.minor}"
dependency_inputs = ["bot/requirements.txt", "bot/requirements-dev.txt"]
test_paths = []
tests_applicable = false
tests_reason = "Disposable ARGUS layout acceptance fixture."
source_roots = ["scripts", "apscheduler"]
stub_roots = ["stubs"]
[[python]]
name = "worker"
path = "worker"
python_version = "{sys.version_info.major}.{sys.version_info.minor}"
dependency_inputs = ["requirements.txt"]
test_paths = []
tests_applicable = false
tests_reason = "Opt-out fixture for shared-source selection."
typecheck = false
source_roots = ["scripts"]
'''


def _prepare_argus_layout(root: Path, *, signature: str = "str") -> None:
	(root / "bot").mkdir(parents=True)
	(root / "bot/__init__.py").write_text("", encoding="utf-8")
	(root / "bot/stubs").mkdir()
	(root / "bot/stubs/local_api.pyi").write_text(
		"def format_label(value: str) -> str: ...\n", encoding="utf-8"
	)
	(root / "bot/main.py").write_text(
		"from apscheduler.schedulers.asyncio import AsyncIOScheduler\n"
		"from scripts.rendering import render\n"
		"scheduler = AsyncIOScheduler()\n"
		"scheduler.start()\n"
		"label: str = render(1)\n",
		encoding="utf-8",
	)
	(root / "scripts").mkdir()
	(root / "scripts/__init__.py").write_text("", encoding="utf-8")
	(root / "scripts/rendering.py").write_text(
		f"def render(value: {signature}) -> str:\n    return str(value)\n", encoding="utf-8"
	)
	(root / "apscheduler/schedulers").mkdir(parents=True)
	(root / "apscheduler/py.typed").write_bytes(b"\n")
	(root / "apscheduler/__init__.pyi").write_text("", encoding="utf-8")
	(root / "apscheduler/schedulers/__init__.pyi").write_text("", encoding="utf-8")
	(root / "apscheduler/schedulers/asyncio.pyi").write_text(
		"class AsyncIOScheduler:\n    def start(self) -> None: ...\n",
		encoding="utf-8",
	)
	(root / "bot/requirements.txt").write_text("APScheduler==3.10.4\n", encoding="utf-8")
	(root / "bot/requirements-dev.txt").write_text(
		"-r requirements.txt\npytest\n", encoding="utf-8"
	)
	(root / "quality-gate.toml").write_text(_argus_typing_manifest(), encoding="utf-8")


def _prepare_candidate_runtime(root: Path, cache: Path, environment: Path) -> Path:
	manifest = load_manifest(root)
	component = manifest.python[0]
	identity = runtime_identity(root, manifest, component)
	runtime = RuntimeManager(cache).path_for(root, identity)
	runtime.parent.mkdir(parents=True, exist_ok=True)
	shutil.copytree(environment, runtime, symlinks=True)
	(runtime / "runtime.json").write_text(
		json.dumps(asdict(identity) | {"fingerprint": runtime_fingerprint(identity)}),
		encoding="utf-8",
	)
	return runtime


@pytest.fixture
def argus_project(
	tmp_path: Path, typing_environment: Path, candidate_typing_cache: Path
) -> Iterator[Path]:
	project = tmp_path / "argus"
	project.mkdir()
	_prepare_argus_layout(project)
	runtime = _prepare_candidate_runtime(project, candidate_typing_cache, typing_environment)
	try:
		yield project
	finally:
		if runtime.exists():
			shutil.rmtree(runtime)


def _run_candidate_types(
	project: Path, cache: Path, wheel: Path, *paths: str, network_guard: bool = True
) -> tuple[int, dict[str, Any]]:
	policy = cache / "releases/v4.7.0"
	return invoke_candidate_wheel(
		request(project, policy, *paths, background=True), wheel, network_guard=network_guard
	)


def _assert_candidate_stale_mutation(
	project: Path,
	cache: Path,
	wheel: Path,
	*,
	relative: str,
	replacement: bytes | None,
	snapshot_relative: str | None = None,
) -> None:
	path = project / relative
	before = path.read_bytes() if path.is_file() else None
	captured = threading.Event()
	stop = threading.Event()

	def mutate_after_snapshot() -> None:
		snapshot_path = f"temp/qg-*/source/{snapshot_relative or relative}"
		while not stop.wait(0.01):
			if any(project.glob(snapshot_path)):
				if replacement is None:
					path.unlink()
				else:
					path.write_bytes(replacement)
				captured.set()
				return

	thread = threading.Thread(target=mutate_after_snapshot)
	thread.start()
	try:
		try:
			code, result = _run_candidate_types(project, cache, wheel, "bot/main.py")
		finally:
			stop.set()
			thread.join(timeout=3)
	finally:
		if before is None:
			path.unlink(missing_ok=True)
		else:
			path.write_bytes(before)
	assert captured.is_set(), "Mutation must follow snapshot capture."
	assert (code, result["status"], result["reason"]) == (2, "unchecked", "stale_input"), result
	assert result["components"] == []
