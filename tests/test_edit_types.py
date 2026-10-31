"""Executable typing contract with released mypy, complete scope and raw-byte freshness."""

from __future__ import annotations

import cProfile
import hashlib
import json
import os
import py_compile
import re
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
from importlib.util import cache_from_source
from pathlib import Path
from typing import Any, ClassVar

import pytest
from test_typing_optout import manifest_text

from quality_gate.contracts import PythonComponent, ValidationError, load_manifest, parse_manifest
from quality_gate.distribution import ReleaseManifest, default_cache_root, verify_release
from quality_gate.edit_inputs import Configuration, SourceFile
from quality_gate.edit_protocol import EditError
from quality_gate.edit_type_environment import (
	_MANAGED_RUNTIME_PTH_SHA256,
	MYPY_DEPENDENCIES,
	_validate_site,
)
from quality_gate.edit_type_inputs import environment_tree, inventory, request_files
from quality_gate.edit_types import (
	_import_context,
	_local_stub_roots,
	_local_typed_dependency,
	_mypy_environment,
	_typed_dependency,
	_TypingInvocation,
	execute,
)
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

	cache_parent = typing_policy.parent.parent
	if is_link_or_reparse(cache_parent.lstat()) or not cache_parent.is_dir():
		raise AssertionError("Candidate typing cache requires an ordinary policy cache root.")
	# Gate redirects TEMP into its nested workspace. Keep disposable runtimes beside
	# the verified user-local policy cache to avoid overlong Windows paths.
	with tempfile.TemporaryDirectory(prefix="qg-candidate-", dir=cache_parent) as directory:
		cache = Path(directory).resolve()
		prepare_edit_cache(cache, "v4.6.0")
		candidate_root = cache / "releases/v4.6.0"
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
		assert verified.version == "v4.6.0"
		assert (candidate_root / product.path).read_bytes() == candidate_engine_wheel.read_bytes()
		yield cache


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


def test_empty_typing_input_is_explicitly_inapplicable(tmp_path: Path, typing_policy: Path) -> None:
	code, result = invoke(request(tmp_path, typing_policy))
	assert (code, result["status"]) == (0, "not_applicable"), result
	assert result["components"] == []
	assert result["writes_may_have_occurred"] is False


def test_background_cancellation_reaps_active_public_worker(
	tmp_path: Path, typing_policy: Path, typing_environment: Path
) -> None:
	shutil.copytree(typing_environment, tmp_path / ".venv", symlinks=True)
	(tmp_path / "source.py").write_bytes(b"value: int = 1\n")
	payload = request(tmp_path, typing_policy, "source.py", background=True)
	signal = Path(payload["cancel_path"])
	created_parent = not signal.parent.exists()
	signal.parent.mkdir(exist_ok=True)
	stop = threading.Event()
	signalled = threading.Event()

	def cancel() -> None:
		while not stop.wait(0.01):
			if any(tmp_path.glob("temp/qg-*/source/source.py")):
				signal.write_bytes(b"")
				signalled.set()
				return

	thread = threading.Thread(target=cancel)
	thread.start()
	try:
		code, result = invoke(payload)
		assert signalled.is_set(), "Cancellation must reach an active worker."
		assert (code, result["status"], result["reason"]) == (2, "unchecked", "cancelled"), result
		assert result["event_id"] == payload["event_id"]
		assert not list(tmp_path.glob("temp/qg-*"))
	finally:
		stop.set()
		thread.join(timeout=3)
		signal.unlink(missing_ok=True)
		if created_parent and not any(signal.parent.iterdir()):
			signal.parent.rmdir()
	assert result["writes_may_have_occurred"] is False


def test_background_envelope_binds_event_and_rejects_unowned_cancel_path(
	tmp_path: Path, typing_policy: Path
) -> None:
	payload = request(tmp_path, typing_policy, background=True)
	code, result = invoke(payload)
	assert (code, result["schema"], result["status"]) == (0, 2, "not_applicable"), result
	assert result["mode"] == "background"
	assert result["event_id"] == payload["event_id"]
	payload["cancel_path"] = str(tmp_path / "unowned.cancel")
	code, result = invoke(payload)
	assert (code, result["status"], result["reason"]) == (2, "unchecked", "unsafe_path"), result


def test_expired_valid_background_request_keeps_event_identity(
	tmp_path: Path, typing_policy: Path
) -> None:
	payload = request(tmp_path, typing_policy, background=True)
	payload["deadline_unix_ms"] = int(time.time() * 1000) - 1
	code, result = invoke(payload)
	assert (code, result["schema"], result["status"], result["reason"]) == (
		2,
		2,
		"unchecked",
		"deadline",
	), result
	assert result["event_id"] == payload["event_id"]
	payload["unexpected"] = True
	code, result = invoke(payload)
	assert (code, result["schema"], result["reason"]) == (2, 1, "invalid_request"), result
	assert "event_id" not in result


def test_existing_background_signal_cancels_before_runtime_inspection(
	tmp_path: Path, typing_policy: Path
) -> None:
	(tmp_path / "source.py").write_text("value: int = 1\n", encoding="utf-8")
	payload = request(tmp_path, typing_policy, "source.py", background=True)
	signal = Path(payload["cancel_path"])
	created_parent = not signal.parent.exists()
	signal.parent.mkdir(exist_ok=True)
	signal.write_bytes(b"")
	try:
		code, result = invoke(payload)
		assert (code, result["schema"], result["status"], result["reason"]) == (
			2,
			2,
			"unchecked",
			"cancelled",
		), result
		assert result["event_id"] == payload["event_id"]
		assert not list(tmp_path.glob("temp/qg-*"))
	finally:
		signal.unlink(missing_ok=True)
		if created_parent and not any(signal.parent.iterdir()):
			signal.parent.rmdir()


def test_changed_signature_reports_unchanged_consumer_with_real_mypy(
	tmp_path: Path, typing_policy: Path, typing_environment: Path
) -> None:
	shutil.copytree(typing_environment, tmp_path / ".venv", symlinks=True)
	(tmp_path / "api.py").write_text(
		"def accept(value: str) -> str:\n    return value\n", encoding="utf-8"
	)
	(tmp_path / "consumer.py").write_text(
		"from api import accept\nresult = accept(123)\n", encoding="utf-8"
	)
	code, result = invoke(request(tmp_path, typing_policy, "api.py", background=True))
	assert (code, result["status"]) == (1, "failed"), json.dumps(result, indent=2)
	component = result["components"][0]
	assert component["diagnostics"] == [
		{"path": "consumer.py", "line": 2, "column": 17, "code": "arg-type"}
	]
	assert {item["path"] for item in component["inventory"]} == {"api.py", "consumer.py"}
	assert result["selections"][0]["policy_release"] == typing_policy.name
	assert result["selections"][0]["mypy_version"] == "2.3.1"
	assert component["diagnostics"] == full_component_diagnostics(tmp_path, typing_policy, ".")
	assert not list(tmp_path.glob("temp/qg-*"))


def test_typing_does_not_require_unrelated_released_pytest_dependencies(
	tmp_path: Path, typing_policy: Path, typing_environment: Path
) -> None:
	shutil.copytree(typing_environment, tmp_path / ".venv", symlinks=True)
	site = (
		tmp_path
		/ ".venv"
		/ (
			"Lib/site-packages"
			if os.name == "nt"
			else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
		)
	)
	for path in site.glob("pluggy*"):
		if path.is_dir():
			shutil.rmtree(path)
		else:
			path.unlink()
	(tmp_path / "source.py").write_text("value: int = 1\n", encoding="utf-8")
	code, result = invoke(request(tmp_path, typing_policy, "source.py", background=True))
	assert (code, result["status"]) == (0, "passed"), result


def test_standard_managed_runtime_pth_reaches_component_inventory_and_mypy(
	tmp_path: Path, typing_policy: Path, typing_environment: Path
) -> None:
	shutil.copytree(typing_environment, tmp_path / ".venv", symlinks=True)
	site = (
		tmp_path
		/ ".venv"
		/ (
			"Lib/site-packages"
			if os.name == "nt"
			else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
		)
	)
	for name, content in STANDARD_MANAGED_RUNTIME_PTH.items():
		(site / name).write_bytes(content)
	source = b"value: int = 1\n"
	(tmp_path / "source.py").write_bytes(source)

	code, result = invoke(request(tmp_path, typing_policy, "source.py", background=True))

	assert (code, result["status"]) == (0, "passed"), result
	assert result["components"][0]["inventory"] == [{"path": "source.py", "sha256": digest(source)}]
	assert result["selections"][0]["mypy_version"] == "2.3.1"
	assert not list(tmp_path.glob("temp/qg-*"))


def full_component_diagnostics(
	root: Path,
	policy: Path,
	scope: str,
	python: Path | None = None,
	metrics: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
	"""Independent same-release full directory invocation, without edit routing."""
	python = python or root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
	with tempfile.TemporaryDirectory(prefix="typing-full-") as cache:
		completed = subprocess.run(
			[
				str(python),
				"-I",
				"-B",
				"-m",
				"mypy",
				"--config-file",
				str(policy / "quality_gate/policy/mypy.ini"),
				"--show-column-numbers",
				"--show-error-codes",
				"--no-pretty",
				"--no-error-summary",
				"--cache-dir",
				cache,
				*(["--verbose"] if metrics is not None else []),
				scope,
			],
			cwd=root,
			capture_output=True,
			check=False,
			timeout=30,
		)
	assert completed.returncode in {0, 1}
	if metrics is None:
		assert not completed.stderr
	else:
		graph_match = re.search(rb"Loaded graph with (\d+) nodes", completed.stderr)
		assert graph_match is not None, "Real mypy must report a completed import graph."
		metrics["mypy_graph_nodes"] = int(graph_match[1])
	diagnostics = []
	for line in completed.stdout.decode().splitlines():
		match = re.fullmatch(r"(.+?):(\d+):(\d+): error: .*  \[([a-z0-9-]+)\]", line)
		if match:
			diagnostics.append(
				{
					"path": Path(match[1]).as_posix().removeprefix("./"),
					"line": int(match[2]),
					"column": int(match[3]),
					"code": match[4],
				}
			)
	return sorted(
		diagnostics, key=lambda item: (item["path"], item["line"], item["column"], item["code"])
	)


def test_released_typing_extensions_context_is_supported_without_project_execution(
	tmp_path: Path, typing_policy: Path, typing_environment: Path
) -> None:
	shutil.copytree(typing_environment, tmp_path / ".venv", symlinks=True)
	(tmp_path / "source.py").write_text(
		"import email.message\nimport os.path\nimport importlib.metadata\n"
		"from typing_extensions import TypeAlias\nValue: TypeAlias = int\n"
		"raise RuntimeError('must never execute')\n",
		encoding="utf-8",
	)
	code, result = invoke(request(tmp_path, typing_policy, "source.py", background=True))
	assert (code, result["status"]) == (0, "passed"), result


def test_deleted_module_and_missing_stub_context_are_unchecked(
	tmp_path: Path, typing_policy: Path, typing_environment: Path
) -> None:
	shutil.copytree(typing_environment, tmp_path / ".venv", symlinks=True)
	(tmp_path / "consumer.py").write_text(
		"from removed import Value\nvalue: Value\n", encoding="utf-8"
	)
	code, result = invoke(request(tmp_path, typing_policy, "removed.py", background=True))
	assert (code, result["status"], result["reason"]) == (2, "unchecked", "dependencies"), result
	(tmp_path / "consumer.py").write_text(
		"from .removed import Value\nvalue: Value\n", encoding="utf-8"
	)
	code, result = invoke(request(tmp_path, typing_policy, "removed.py", background=True))
	assert (code, result["status"], result["reason"]) == (2, "unchecked", "dependencies"), result


def test_missing_environment_never_uses_global_python(tmp_path: Path, typing_policy: Path) -> None:
	(tmp_path / "example.py").write_text("value: int = 1\n", encoding="utf-8")
	code, result = invoke(request(tmp_path, typing_policy, "example.py"))
	assert (code, result["status"], result["reason"]) == (2, "unchecked", "runtime"), result


def test_unmanaged_dependency_evidence_is_path_ordered(
	tmp_path: Path, typing_policy: Path, typing_environment: Path
) -> None:
	shutil.copytree(typing_environment, tmp_path / ".venv", symlinks=True)
	(tmp_path / "source.py").write_bytes(b"value: int = 1\n")
	(tmp_path / "pyproject.toml").write_bytes(b"[project]\nname = 'fixture'\n")
	(tmp_path / ".mypy.ini").write_bytes(b"[mypy]\n")
	code, result = invoke(request(tmp_path, typing_policy, "source.py", background=True))
	assert (code, result["status"]) == (0, "passed"), result
	assert result["components"][0]["dependency_inputs"] == [
		{"path": ".mypy.ini", "sha256": digest(b"[mypy]\n")},
		{"path": "pyproject.toml", "sha256": digest(b"[project]\nname = 'fixture'\n")},
	]


def test_component_local_stubs_resolve_and_are_included_in_freshness(tmp_path: Path) -> None:
	(tmp_path / "app/stubs").mkdir(parents=True)
	stub = tmp_path / "app/stubs/vendor.pyi"
	stub.write_text("class Value: ...\n", encoding="utf-8")
	component = PythonComponent(
		"app", "app", "3.12", (), (), False, "unused", 300, True, ("stubs",)
	)
	directories = [
		{"path": "app", "entries": [{"name": "stubs", "kind": "directory"}]},
		{"path": "app/stubs", "entries": [{"name": "vendor.pyi", "kind": "file"}]},
	]
	roots = _local_stub_roots(tmp_path, component, directories)
	assert roots == ("app/stubs",)
	assert _local_typed_dependency(tmp_path / roots[0], "vendor")
	sources, captured_directories = inventory(
		tmp_path, "app", managed=True, deadline=time.monotonic() + 5
	)
	invocation = object.__new__(_TypingInvocation)
	invocation.snapshot = tmp_path / "snapshot"
	invocation.snapshot.mkdir()
	invocation.configuration = Configuration(tmp_path)
	invocation._snapshot_sources(sources, "app", [])
	assert (invocation.snapshot / "app/stubs/vendor.pyi").read_bytes() == stub.read_bytes()
	assert (
		_import_context(
			[SourceFile("app/main.py", b"import vendor\n", digest(b"import vendor\n"))],
			"app",
			_StubSelection(),
			tuple(str(tmp_path / root) for root in roots),
		)
		== 1
	)
	before = sources, captured_directories
	stub.write_text("class Value: ...\nchanged: int\n", encoding="utf-8")
	after = inventory(tmp_path, "app", managed=True, deadline=time.monotonic() + 5)
	assert before != after
	environment = _mypy_environment(tmp_path, tmp_path / "snapshot", roots)
	assert environment["MYPYPATH"] == str(tmp_path / "snapshot/app/stubs")
	environment = _mypy_environment(tmp_path, tmp_path / "snapshot", (*roots, "app/extra-stubs"))
	assert environment["MYPYPATH"].split(os.pathsep) == [
		str(tmp_path / "snapshot/app/stubs"),
		str(tmp_path / "snapshot/app/extra-stubs"),
	]


class _StubSelection:
	stdlib: ClassVar[set[str]] = set()
	site = Path("/missing/site")


def test_missing_local_stub_root_is_unchecked_by_preflight(tmp_path: Path) -> None:
	with pytest.raises(EditError) as error:
		_local_stub_roots(
			tmp_path,
			PythonComponent("app", "app", "3.12", (), (), False, "unused", 300, True, ("stubs",)),
			[{"path": "app"}],
		)
	assert error.value.reason == "dependencies"


def test_local_stub_root_cannot_escape_component(tmp_path: Path) -> None:
	with pytest.raises(EditError) as error:
		_local_stub_roots(
			tmp_path,
			PythonComponent(
				"app", "app", "3.12", (), (), False, "unused", 300, True, ("../shared-stubs",)
			),
			[{"path": "app"}],
		)
	assert error.value.reason == "unsafe_path"


def test_installed_site_stub_is_accepted_without_local_root(tmp_path: Path) -> None:
	site = tmp_path / "site"
	(site / "vendor-stubs").mkdir(parents=True)
	(site / "vendor-stubs/__init__.pyi").write_text("class Value: ...\n", encoding="utf-8")
	assert _typed_dependency(site, "vendor")


def test_manifest_declares_component_local_stub_roots_only_on_supported_policy() -> None:
	manifest = manifest_text("v3.2.0").replace(
		"typecheck = false\n", 'typecheck = false\nstub_roots = ["stubs"]\n'
	)
	assert parse_manifest(manifest).python[0].stub_roots == ("stubs",)
	with pytest.raises(ValidationError, match="stub_roots"):
		parse_manifest(manifest.replace("v3.2.0", "v3.1.0"))
	for path in ["../stubs", "stubs/../shared", "D:/outside/stubs"]:
		with pytest.raises(ValidationError, match="stub_roots"):
			parse_manifest(manifest.replace('"stubs"', f'"{path}"'))
	with pytest.raises(ValidationError, match="stub_roots"):
		parse_manifest(manifest.replace('["stubs"]', "['stubs\\\\nested']"))


def test_manifest_declares_repository_source_roots_only_on_supported_policy() -> None:
	manifest = manifest_text("v4.6.0").replace(
		"typecheck = false\n", 'typecheck = false\nsource_roots = ["stubs", "scripts"]\n'
	)
	assert parse_manifest(manifest).python[0].source_roots == ("scripts", "stubs")
	with pytest.raises(ValidationError, match="source_roots"):
		parse_manifest(manifest.replace("v4.6.0", "v4.5.0"))
	for roots in [
		'["../scripts"]',
		'["D:/outside"]',
		'["scripts\\\\nested"]',
		'["scripts", "scripts"]',
		'["scripts", "scripts/private"]',
		'["scripts", "Scripts/private"]',
		'["app/shared"]',
	]:
		with pytest.raises(ValidationError, match="source_roots"):
			parse_manifest(manifest.replace('["stubs", "scripts"]', roots))


@pytest.mark.parametrize(
	"name,content",
	[
		("uv.lock", b"[broken\n"),
		("poetry.lock", b"[broken\n"),
		("Pipfile", b"[broken\n"),
		("Pipfile.lock", b'{"default": '),
		pytest.param("Pipfile.lock", b'{"default": {}, "default": {}}', id="duplicate-json"),
		pytest.param("Pipfile.lock", b'{"default": NaN}', id="non-json-constant"),
		("mypy.ini", b"[broken\n"),
		(".mypy.ini", b"[broken\n"),
		("setup.cfg", b"[broken\n"),
	],
)
def test_malformed_project_settings_are_unchecked(
	tmp_path: Path, typing_policy: Path, typing_environment: Path, name: str, content: bytes
) -> None:
	shutil.copytree(typing_environment, tmp_path / ".venv", symlinks=True)
	(tmp_path / "source.py").write_bytes(b"value: int = 1\n")
	(tmp_path / name).write_bytes(content)
	code, result = invoke(request(tmp_path, typing_policy, name, background=True))
	assert (code, result["status"], result["reason"]) == (2, "unchecked", "configuration"), result
	assert result["components"] == []


@pytest.mark.parametrize(("name", "content"), STANDARD_MANAGED_RUNTIME_PTH.items())
def test_standard_managed_runtime_pth_payloads_are_allowed(
	tmp_path: Path, name: str, content: bytes
) -> None:
	assert digest(content.replace(b"\r\n", b"\n")) == _MANAGED_RUNTIME_PTH_SHA256[name]
	site = tmp_path / "site"
	site.mkdir()
	(site / name).write_bytes(content)

	_validate_site(site)


@pytest.mark.parametrize(
	"name,content",
	[
		pytest.param("unknown.pth", b"../outside/site-packages\n", id="path-entry"),
		pytest.param(
			"unknown.pth", b"  # relative path-looking entry\n", id="indented-comment-path"
		),
		pytest.param("unknown.pth", b"import os; os.environ['BAD'] = '1'\n", id="arbitrary-code"),
		pytest.param(
			"a1_coverage.pth",
			STANDARD_MANAGED_RUNTIME_PTH["a1_coverage.pth"] + b"import os\n",
			id="modified-approved-name",
		),
		pytest.param(
			"unapproved-name.pth",
			STANDARD_MANAGED_RUNTIME_PTH["a1_coverage.pth"],
			id="approved-content-wrong-name",
		),
		pytest.param("pytest-cov.pth", b"\xff\n", id="malformed-bytes"),
	],
)
def test_unrecognized_or_modified_managed_runtime_pth_is_rejected(
	tmp_path: Path, name: str, content: bytes
) -> None:
	site = tmp_path / "site"
	site.mkdir()
	(site / name).write_bytes(content)

	with pytest.raises(EditError) as error:
		_validate_site(site)

	assert error.value.reason == "dependencies"


@pytest.mark.parametrize("content", [b"", b"\n# harmless comment\r\n"])
def test_blank_and_comment_only_managed_runtime_pth_remains_allowed(
	tmp_path: Path, content: bytes
) -> None:
	site = tmp_path / "site"
	site.mkdir()
	(site / "unrecognized.pth").write_bytes(content)

	_validate_site(site)


@pytest.mark.parametrize(
	"mutation,reason",
	[
		("missing-venv-config", "runtime"),
		("system-site", "runtime"),
		("corrupt-tool", "tool"),
		("missing-tool-dependency", "tool"),
		("missing-package", "dependencies"),
		("missing-stdlib-submodule", "dependencies"),
		("untyped-package", "dependencies"),
		("missing-stub-submodule", "dependencies"),
		("nested-project", "configuration"),
	],
)
def test_unavailable_context_never_becomes_policy_ignore_success(
	tmp_path: Path, typing_policy: Path, typing_environment: Path, mutation: str, reason: str
) -> None:
	shutil.copytree(typing_environment, tmp_path / ".venv", symlinks=True)
	site = (
		tmp_path
		/ ".venv"
		/ (
			"Lib/site-packages"
			if os.name == "nt"
			else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
		)
	)
	body = "value: int = 1\n"
	if mutation == "missing-venv-config":
		(tmp_path / ".venv/pyvenv.cfg").unlink()
	elif mutation == "system-site":
		config = tmp_path / ".venv/pyvenv.cfg"
		config.write_text(config.read_text().replace("false", "true"), encoding="utf-8")
	elif mutation == "corrupt-tool":
		(site / "mypy/__init__.py").write_bytes(b"raise RuntimeError('never execute')\n")
	elif mutation == "missing-tool-dependency":
		shutil.rmtree(site / "pathspec")
	elif mutation == "missing-package":
		body = "import typing_fixture_missing_package\n"
	elif mutation == "missing-stdlib-submodule":
		body = "import email.typing_fixture_missing_module\n"
	elif mutation == "untyped-package":
		(site / "fixture_package").mkdir()
		(site / "fixture_package/__init__.py").write_bytes(b"")
		body = "import fixture_package\n"
	elif mutation == "missing-stub-submodule":
		(site / "fixture_package-stubs").mkdir()
		(site / "fixture_package-stubs/__init__.pyi").write_bytes(b"")
		body = "import fixture_package.missing\n"
	elif mutation == "nested-project":
		(tmp_path / "nested").mkdir()
		(tmp_path / "nested/pyproject.toml").write_bytes(b"[project]\nname='nested'\n")
	(tmp_path / "source.py").write_text(body, encoding="utf-8")
	code, result = invoke(request(tmp_path, typing_policy, "source.py", background=True))
	assert (code, result["status"], result["reason"]) == (2, "unchecked", reason), result
	assert result["components"] == []


def test_shadowed_tool_module_is_rejected_without_execution(
	tmp_path: Path, typing_policy: Path, typing_environment: Path
) -> None:
	shutil.copytree(typing_environment, tmp_path / ".venv", symlinks=True)
	site = (
		tmp_path
		/ ".venv"
		/ (
			"Lib/site-packages"
			if os.name == "nt"
			else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
		)
	)
	shadow = site / "typing_extensions"
	shadow.mkdir()
	marker = tmp_path / "must-not-execute"
	(shadow / "__init__.py").write_text(
		f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
		"raise RuntimeError('must never execute')\n",
		encoding="utf-8",
	)
	(tmp_path / "source.py").write_bytes(b"value: int = 1\n")
	code, result = invoke(request(tmp_path, typing_policy, "source.py", background=True))
	assert not marker.exists()
	assert (code, result["status"], result["reason"]) == (2, "unchecked", "tool"), result


def test_untrusted_managed_runtime_pth_is_rejected_without_execution(
	tmp_path: Path, typing_policy: Path, typing_environment: Path
) -> None:
	shutil.copytree(typing_environment, tmp_path / ".venv", symlinks=True)
	site = (
		tmp_path
		/ ".venv"
		/ (
			"Lib/site-packages"
			if os.name == "nt"
			else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
		)
	)
	marker = tmp_path / "pth-startup-executed"
	(site / "untrusted.pth").write_text(
		f"import pathlib; pathlib.Path({str(marker)!r}).touch()\n", encoding="utf-8"
	)
	(tmp_path / "source.py").write_bytes(b"value: int = 1\n")

	code, result = invoke(request(tmp_path, typing_policy, "source.py", background=True))

	assert not marker.exists()
	assert (code, result["status"], result["reason"]) == (2, "unchecked", "dependencies"), result


def test_unverified_bytecode_cache_is_never_executed(
	tmp_path: Path, typing_policy: Path, typing_environment: Path
) -> None:
	shutil.copytree(typing_environment, tmp_path / ".venv", symlinks=True)
	site = (
		tmp_path
		/ ".venv"
		/ (
			"Lib/site-packages"
			if os.name == "nt"
			else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
		)
	)
	marker = tmp_path / "must-not-execute"
	with tempfile.TemporaryDirectory(prefix="typing-bytecode-") as directory:
		payload = Path(directory) / "payload.py"
		payload.write_text(
			f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
			"raise RuntimeError('must never execute')\n",
			encoding="utf-8",
		)
		py_compile.compile(
			str(payload),
			cfile=cache_from_source(str(site / "typing_extensions.py")),
			doraise=True,
			invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
		)
	(tmp_path / "source.py").write_bytes(b"value: int = 1\n")
	code, result = invoke(request(tmp_path, typing_policy, "source.py", background=True))
	assert not marker.exists()
	assert (code, result["status"]) == (0, "passed"), result


@pytest.mark.parametrize("mutation", ["source-addition", "configuration", "environment"])
def test_concurrent_mutation_invalidates_whole_real_analysis(
	tmp_path: Path, typing_policy: Path, typing_environment: Path, mutation: str
) -> None:
	shutil.copytree(typing_environment, tmp_path / ".venv", symlinks=True)
	(tmp_path / "source.py").write_bytes(b"value: int = 1\n")
	payload = request(tmp_path, typing_policy, "source.py", background=True)
	changed = threading.Event()

	def mutate() -> None:
		end = time.monotonic() + 25
		while time.monotonic() < end:
			if any(tmp_path.glob("temp/qg-*/source/source.py")):
				if mutation == "source-addition":
					(tmp_path / "added.py").write_bytes(b"other: int = 2\n")
				elif mutation == "configuration":
					(tmp_path / "requirements.txt").write_bytes(b"# changed dependency context\n")
				else:
					(tmp_path / ".venv/extra-context").write_bytes(b"changed")
				changed.set()
				return
			time.sleep(0.01)

	thread = threading.Thread(target=mutate)
	thread.start()
	try:
		code, result = invoke(payload)
	finally:
		thread.join(timeout=26)
	assert changed.is_set(), "The mutation must occur after initial source/environment capture."
	assert (code, result["status"], result["reason"]) == (2, "unchecked", "stale_input"), result
	assert result["components"] == []


def test_optout_requires_a_supported_verified_new_policy(
	tmp_path: Path, typing_policy: Path
) -> None:
	(tmp_path / "app").mkdir()
	(tmp_path / "app/broken.py").write_text("not valid Python !\n", encoding="utf-8")
	(tmp_path / "quality-gate.toml").write_text(manifest_text("v99.0.0"), encoding="utf-8")
	code, result = invoke(request(tmp_path, typing_policy, "app/broken.py"))
	assert (code, result["status"], result["reason"]) == (2, "unchecked", "unsupported_policy"), (
		result
	)
	(tmp_path / "quality-gate.toml").write_text(manifest_text("v2.4.2"), encoding="utf-8")
	code, result = invoke(request(tmp_path, typing_policy, "app/broken.py"))
	assert (code, result["status"], result["reason"]) == (2, "unchecked", "manifest"), result


@pytest.mark.parametrize("configuration_exists", [True, False])
def test_valid_new_policy_optout_is_distinct_from_unavailable_typing(
	tmp_path: Path, configuration_exists: bool
) -> None:
	from test_edit_check import prepare_edit_cache

	project = tmp_path / "project"
	(project / "app").mkdir(parents=True)
	(project / "app/source.py").write_bytes(b"not even valid Python !\n")
	(project / "quality-gate.toml").write_text(manifest_text(), encoding="utf-8")
	configuration_path = "nested/requirements.txt"
	(project / "nested").mkdir()
	if configuration_exists:
		(project / configuration_path).write_bytes(b"# explicit dependency mutation\n")
	cache = prepare_edit_cache(tmp_path / "cache", "v3.1.0")
	code, result = invoke(
		request(project, cache / "releases/v3.1.0", "app/source.py", configuration_path)
	)
	assert (code, result["status"]) == (0, "not_applicable"), result
	assert result["selections"] == []
	assert result["components"][0]["reason"] == "typecheck_disabled"
	assert result["components"][0]["inventory"] == []
	assert {item["path"]: item["sha256"] for item in result["configuration"]}[
		configuration_path
	] == (digest((project / configuration_path).read_bytes()) if configuration_exists else None)


def test_source_limit_never_certifies_partial_component(
	tmp_path: Path, typing_policy: Path
) -> None:
	for index in range(1025):
		(tmp_path / f"source{index}.py").write_bytes(b"value: int = 1\n")
	code, result = invoke(request(tmp_path, typing_policy, "source0.py", background=True))
	assert (code, result["status"], result["reason"]) == (2, "unchecked", "limits"), result
	assert result["components"] == []


def test_real_managed_component_includes_visible_build_and_imported_types(
	tmp_path: Path, typing_policy: Path, typing_environment: Path
) -> None:
	(root := tmp_path / "app").mkdir()
	(root / "build").mkdir()
	(root / "models.py").write_text("Value = str\n", encoding="utf-8")
	(root / "build/consumer.py").write_text(
		"from models import Value\nvalue: Value = 123\n", encoding="utf-8"
	)
	(tmp_path / "requirements.txt").write_bytes(b"")
	(tmp_path / "requirements-dev.txt").write_bytes(b"# fixture dependencies\n")
	(tmp_path / "quality-gate.toml").write_text(
		f'''[quality]
schema = 2
policy_release = "{typing_policy.name}"
[repository]
name = "typing-managed"
domains = ["repository", "python"]
required_documents = ["quality-gate.toml"]
[[python]]
name = "app"
path = "app"
python_version = "{sys.version_info.major}.{sys.version_info.minor}"
dependency_inputs = ["requirements.txt", "requirements-dev.txt", "requirements.txt"]
test_paths = []
tests_applicable = false
tests_reason = "Disposable typing integration."
''',
		encoding="utf-8",
	)
	manifest = load_manifest(tmp_path)
	identity = runtime_identity(tmp_path, manifest, manifest.python[0])
	runtime = RuntimeManager(typing_policy.parent.parent).path_for(tmp_path, identity)
	try:
		shutil.copytree(typing_environment, runtime, symlinks=True)
		(runtime / "runtime.json").write_text(
			json.dumps(asdict(identity) | {"fingerprint": runtime_fingerprint(identity)}),
			encoding="utf-8",
		)
		code, result = invoke(request(tmp_path, typing_policy, "app/models.py", background=True))
		assert (code, result["status"]) == (1, "failed"), json.dumps(result)
		component = result["components"][0]
		assert component["coverage"] == "managed"
		assert component["dependency_inputs"] == [
			{"path": "requirements-dev.txt", "sha256": digest(b"# fixture dependencies\n")},
			{"path": "requirements.txt", "sha256": digest(b"")},
		]
		assert {item["path"] for item in component["inventory"]} == {
			"app/models.py",
			"app/build/consumer.py",
		}
		assert component["diagnostics"][0]["path"] == "app/build/consumer.py"
		assert component["diagnostics"] == full_component_diagnostics(
			tmp_path,
			typing_policy,
			"app",
			runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python"),
		)
	finally:
		if runtime.exists():
			shutil.rmtree(runtime)


def _argus_typing_manifest() -> str:
	return f'''[quality]
schema = 2
policy_release = "v4.6.0"
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
	policy = cache / "releases/v4.6.0"
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


def test_candidate_wheel_checks_argus_sibling_sources_and_local_stubs(
	argus_project: Path,
	candidate_typing_cache: Path,
	candidate_engine_wheel: Path,
) -> None:
	code, result = _run_candidate_types(
		argus_project,
		candidate_typing_cache,
		candidate_engine_wheel,
		"scripts/rendering.py",
		network_guard=True,
	)
	assert (code, result["status"]) == (1, "failed"), json.dumps(result, indent=2)
	assert result["engine_version"] == "4.6.0"
	assert [record["name"] for record in result["components"]] == ["bot", "worker"]
	component = result["components"][0]
	assert {item["path"] for item in component["inventory"]} == {
		"bot/__init__.py",
		"bot/main.py",
		"bot/stubs/local_api.pyi",
		"scripts/__init__.py",
		"scripts/rendering.py",
		"apscheduler/__init__.pyi",
		"apscheduler/schedulers/__init__.pyi",
		"apscheduler/schedulers/asyncio.pyi",
	}
	assert component["diagnostics"] == [
		{"path": "bot/main.py", "line": 5, "column": 21, "code": "arg-type"}
	]
	assert result["selections"][0]["mypy_version"] == "2.3.1"
	policy = candidate_typing_cache / "releases/v4.6.0"
	candidate = verify_release(policy)
	mypy = next(item for item in candidate.tools if item.name == "mypy")
	assert result["selections"][0]["mypy_sha256"] == digest((policy / mypy.path).read_bytes())
	assert "py.typed" in {
		entry["name"]
		for directory in component["directories"]
		if directory["path"] == "apscheduler"
		for entry in directory["entries"]
	}
	assert result["components"][1]["reason"] == "typecheck_disabled"

	(argus_project / "scripts/rendering.py").write_text(
		"def render(value: int) -> str:\n    return str(value)\n", encoding="utf-8"
	)
	code, result = _run_candidate_types(
		argus_project,
		candidate_typing_cache,
		candidate_engine_wheel,
		"scripts/rendering.py",
		network_guard=True,
	)
	assert (code, result["status"]) == (0, "passed"), result
	assert [record["name"] for record in result["components"]] == ["bot", "worker"]
	assert result["components"][0]["diagnostics"] == []
	assert result["components"][1]["reason"] == "typecheck_disabled"
	assert sorted(item.name for item in (candidate_typing_cache / "releases").iterdir()) == [
		"v4.6.0"
	]


def test_candidate_wheel_uses_component_local_stub_with_real_mypy(
	argus_project: Path,
	candidate_typing_cache: Path,
	candidate_engine_wheel: Path,
) -> None:
	(argus_project / "bot/main.py").write_text(
		"from local_api import format_label\nlocal_label: str = format_label(1)\n",
		encoding="utf-8",
	)
	code, result = _run_candidate_types(
		argus_project, candidate_typing_cache, candidate_engine_wheel, "bot/main.py"
	)
	assert (code, result["status"]) == (1, "failed"), (
		result.get("reason"),
		[(item["name"], item["status"], item["diagnostics"]) for item in result["components"]],
	)
	assert result["components"][0]["diagnostics"][0]["code"] == "arg-type"
	assert result["components"][0]["diagnostics"][0]["path"] == "bot/main.py"

	(argus_project / "bot/stubs/local_api.pyi").write_text(
		"def format_label(value: int) -> str: ...\n", encoding="utf-8"
	)
	code, result = _run_candidate_types(
		argus_project, candidate_typing_cache, candidate_engine_wheel, "bot/main.py"
	)
	assert (code, result["status"]) == (0, "passed"), result
	assert result["components"][0]["diagnostics"] == []


def test_candidate_wheel_routes_added_and_removed_py_typed_markers(
	argus_project: Path,
	candidate_typing_cache: Path,
	candidate_engine_wheel: Path,
) -> None:
	(argus_project / "scripts/rendering.py").write_text(
		"def render(value: int) -> str:\n    return str(value)\n", encoding="utf-8"
	)
	marker = argus_project / "apscheduler/py.typed"
	marker.unlink()
	code, result = _run_candidate_types(
		argus_project, candidate_typing_cache, candidate_engine_wheel, "apscheduler/py.typed"
	)
	assert (code, result["status"]) == (0, "passed"), (
		result.get("reason"),
		[
			(item["name"], item["status"], item.get("reason"), item["diagnostics"])
			for item in result["components"]
		],
	)
	assert [record["name"] for record in result["components"]] == ["bot"]
	files = {item["path"]: item for item in result["files"]}
	assert files["apscheduler/py.typed"]["input_sha256"] is None
	assert files["apscheduler/py.typed"]["final_sha256"] is None

	marker.write_bytes(b"partial\n")
	code, result = _run_candidate_types(
		argus_project, candidate_typing_cache, candidate_engine_wheel, "apscheduler/py.typed"
	)
	assert (code, result["status"]) == (0, "passed"), result
	assert [record["name"] for record in result["components"]] == ["bot"]
	configuration = {item["path"]: item["sha256"] for item in result["configuration"]}
	assert configuration["apscheduler/py.typed"] == digest(b"partial\n")

	nested_marker = argus_project / "apscheduler/schedulers/py.typed"
	nested_marker.write_bytes(b"partial\n")
	code, result = _run_candidate_types(
		argus_project,
		candidate_typing_cache,
		candidate_engine_wheel,
		"apscheduler/schedulers/py.typed",
	)
	assert (code, result["status"]) == (0, "passed"), result
	assert [record["name"] for record in result["components"]] == ["bot"]
	configuration = {item["path"]: item["sha256"] for item in result["configuration"]}
	assert configuration["apscheduler/schedulers/py.typed"] == digest(b"partial\n")


@pytest.mark.parametrize("phase", ["write", "validate"])
def test_candidate_wheel_runs_ruff_writer_and_validator_with_candidate_only_cache(
	tmp_path: Path,
	candidate_typing_cache: Path,
	candidate_engine_wheel: Path,
	phase: str,
) -> None:
	project = tmp_path / "ruff-candidate"
	project.mkdir()
	path = project / "example.py"
	original = b"import os\nimport sys\n\nvalue = (os.name, sys.version)\n"
	path.write_bytes(original)
	policy = candidate_typing_cache / "releases/v4.6.0"
	payload = request(project, policy, "example.py")
	payload["baseline"] = {
		"policy_release": "v4.6.0",
		"python": sys.executable,
		"python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
	}
	payload["phase"] = phase
	code, result = invoke_candidate_wheel(payload, candidate_engine_wheel, command="edit-check")
	assert (code, result["status"], result["engine_version"]) == (0, "passed", "4.6.0"), result
	assert result["selections"][0]["policy_release"] == "v4.6.0"
	assert result["files"][0]["format"] == "passed"
	assert result["files"][0]["sha256"] == digest(original)
	assert path.read_bytes() == original
	assert sorted(item.name for item in (candidate_typing_cache / "releases").iterdir()) == [
		"v4.6.0"
	]


def test_candidate_wheel_runs_secrets_with_candidate_only_cache(
	tmp_path: Path,
	candidate_typing_cache: Path,
	candidate_engine_wheel: Path,
) -> None:
	project = tmp_path / "secrets-candidate"
	project.mkdir()
	(project / "settings.py").write_text("token = 'ordinary value'\n", encoding="utf-8")
	policy = candidate_typing_cache / "releases/v4.6.0"
	code, result = invoke_candidate_wheel(
		request(project, policy, "settings.py"), candidate_engine_wheel, command="edit-secrets"
	)
	assert (code, result["status"], result["engine_version"]) == (0, "passed", "4.6.0"), result
	assert result["selections"][0]["policy_release"] == "v4.6.0"
	assert result["selections"][0]["scanner_version"] == "8.30.1"
	assert result["files"][0]["findings"] == []
	assert sorted(item.name for item in (candidate_typing_cache / "releases").iterdir()) == [
		"v4.6.0"
	]


def test_candidate_wheel_runs_integrity_with_candidate_only_cache(
	tmp_path: Path,
	candidate_typing_cache: Path,
	candidate_engine_wheel: Path,
) -> None:
	project = tmp_path / "integrity-candidate"
	project.mkdir()
	(project / "clean.py").write_text("value: int = 1\n", encoding="utf-8")
	policy = candidate_typing_cache / "releases/v4.6.0"
	code, result = invoke_candidate_wheel(
		request(project, policy, "clean.py"), candidate_engine_wheel, command="edit-integrity"
	)
	assert (code, result["status"], result["engine_version"]) == (0, "passed", "4.6.0"), result
	assert result["selections"][0]["policy_release"] == "v4.6.0"
	assert result["files"][0]["status"] == "passed"
	assert sorted(item.name for item in (candidate_typing_cache / "releases").iterdir()) == [
		"v4.6.0"
	]


def test_candidate_wheel_routes_shared_tree_additions_renames_and_configuration(
	argus_project: Path,
	candidate_typing_cache: Path,
	candidate_engine_wheel: Path,
) -> None:
	(argus_project / "scripts/rendering.py").write_text(
		"def render(value: int) -> str:\n    return str(value)\n", encoding="utf-8"
	)
	added = argus_project / "scripts/temporary.py"
	added.write_text("value: int = 1\n", encoding="utf-8")
	code, result = _run_candidate_types(
		argus_project, candidate_typing_cache, candidate_engine_wheel, "scripts/temporary.py"
	)
	assert (code, result["status"]) == (0, "passed"), result
	assert [record["name"] for record in result["components"]] == ["bot", "worker"]
	assert "scripts/temporary.py" in {item["path"] for item in result["components"][0]["inventory"]}

	renamed = argus_project / "scripts/renamed.py"
	added.replace(renamed)
	code, result = _run_candidate_types(
		argus_project,
		candidate_typing_cache,
		candidate_engine_wheel,
		"scripts/temporary.py",
		"scripts/renamed.py",
	)
	assert (code, result["status"]) == (0, "passed"), result
	paths = {item["path"] for item in result["components"][0]["inventory"]}
	assert "scripts/renamed.py" in paths and "scripts/temporary.py" not in paths
	renamed.unlink()
	code, result = _run_candidate_types(
		argus_project, candidate_typing_cache, candidate_engine_wheel, "scripts/renamed.py"
	)
	assert (code, result["status"]) == (0, "passed"), result
	assert "scripts/renamed.py" not in {
		item["path"] for item in result["components"][0]["inventory"]
	}

	initializer = argus_project / "scripts/__init__.py"
	initializer.write_text("# changed package initializer\n", encoding="utf-8")
	code, result = _run_candidate_types(
		argus_project, candidate_typing_cache, candidate_engine_wheel, "scripts/__init__.py"
	)
	assert (code, result["status"]) == (0, "passed"), result
	assert "scripts/__init__.py" in {item["path"] for item in result["components"][0]["inventory"]}
	configuration = argus_project / "scripts/pyproject.toml"
	configuration.write_text("[tool.mypy]\n", encoding="utf-8")
	code, result = _run_candidate_types(
		argus_project, candidate_typing_cache, candidate_engine_wheel, "scripts/pyproject.toml"
	)
	assert (code, result["status"]) == (0, "passed"), result
	assert "scripts/pyproject.toml" in {item["path"] for item in result["configuration"]}


@pytest.mark.parametrize("namespace", [False, True], ids=["package", "namespace"])
def test_candidate_wheel_checks_import_names_for_nested_source_roots(
	tmp_path: Path,
	typing_environment: Path,
	candidate_typing_cache: Path,
	candidate_engine_wheel: Path,
	namespace: bool,
) -> None:
	project = tmp_path / "argus-nested-source"
	project.mkdir()
	_prepare_argus_layout(project)
	manifest = project / "quality-gate.toml"
	manifest.write_text(
		manifest.read_text(encoding="utf-8").replace(
			'source_roots = ["scripts", "apscheduler"]',
			'source_roots = ["shared/scripts", "apscheduler"]',
		),
		encoding="utf-8",
	)
	shared_scripts = project / "shared/scripts"
	shared_scripts.parent.mkdir()
	(project / "scripts").replace(shared_scripts)
	if namespace:
		(shared_scripts / "__init__.py").unlink()
	else:
		(project / "shared/__init__.py").write_bytes(b"# package parent\n")
	(project / "bot/main.py").write_text(
		"from apscheduler.schedulers.asyncio import AsyncIOScheduler\n"
		"from rendering import render\n"
		"scheduler = AsyncIOScheduler()\n"
		"scheduler.start()\n"
		"label: str = render(1)\n",
		encoding="utf-8",
	)
	runtime = _prepare_candidate_runtime(
		project,
		candidate_typing_cache,
		typing_environment,
	)
	try:
		code, result = _run_candidate_types(
			project,
			candidate_typing_cache,
			candidate_engine_wheel,
			"shared/scripts/rendering.py",
		)
		assert (code, result["status"]) == (1, "failed"), (
			result.get("reason"),
			[(item["name"], item["status"], item["diagnostics"]) for item in result["components"]],
		)
		assert result["components"][0]["diagnostics"] == [
			{"path": "bot/main.py", "line": 5, "column": 21, "code": "arg-type"}
		]
		(shared_scripts / "rendering.py").write_text(
			"def render(value: int) -> str:\n    return str(value)\n", encoding="utf-8"
		)
		code, result = _run_candidate_types(
			project,
			candidate_typing_cache,
			candidate_engine_wheel,
			"shared/scripts/rendering.py",
		)
	finally:
		if runtime.exists():
			shutil.rmtree(runtime)
	assert (code, result["status"]) == (0, "passed"), (
		result.get("reason"),
		[(item["name"], item["status"], item["diagnostics"]) for item in result["components"]],
	)
	assert [record["name"] for record in result["components"]] == ["bot"]
	configuration = {item["path"]: item["sha256"] for item in result["configuration"]}
	assert configuration["shared/__init__.py"] == (
		digest(b"# package parent\n") if not namespace else None
	)


@pytest.mark.parametrize(
	("relative", "replacement", "snapshot_relative"),
	[
		(
			"scripts/rendering.py",
			b"def render(value: object) -> str:\n    return str(value)\n",
			None,
		),
		(
			"apscheduler/schedulers/asyncio.pyi",
			b"class AsyncIOScheduler:\n    def start(self, value: int) -> None: ...\n",
			None,
		),
		(
			"bot/stubs/local_api.pyi",
			b"def format_label(value: int) -> str: ...\n",
			None,
		),
		("apscheduler/py.typed", b"partial\n", None),
		(
			"apscheduler/schedulers/py.typed",
			b"partial\n",
			"apscheduler/py.typed",
		),
		(
			"bot/stubs/added.pyi",
			b"class Added: ...\n",
			"bot/stubs/local_api.pyi",
		),
		(
			"apscheduler/schedulers/added.pyi",
			b"class Added: ...\n",
			"apscheduler/schedulers/asyncio.pyi",
		),
		("apscheduler/schedulers/asyncio.pyi", None, None),
		("bot/stubs/local_api.pyi", None, None),
		("apscheduler/py.typed", None, None),
	],
)
def test_candidate_wheel_rejects_in_flight_shared_and_stub_mutations(
	argus_project: Path,
	candidate_typing_cache: Path,
	candidate_engine_wheel: Path,
	relative: str,
	replacement: bytes | None,
	snapshot_relative: str | None,
) -> None:
	_assert_candidate_stale_mutation(
		argus_project,
		candidate_typing_cache,
		candidate_engine_wheel,
		relative=relative,
		replacement=replacement,
		snapshot_relative=snapshot_relative,
	)


@pytest.mark.parametrize("unsafe", [False, True])
def test_candidate_cli_fails_closed_for_missing_or_linked_source_roots(
	tmp_path: Path,
	candidate_typing_cache: Path,
	candidate_engine_wheel: Path,
	unsafe: bool,
) -> None:
	project = tmp_path / "argus-invalid-root"
	(project / "bot").mkdir(parents=True)
	(project / "bot/main.py").write_text("value: int = 1\n", encoding="utf-8")
	(project / "bot/requirements.txt").write_text("APScheduler==3.10.4\n", encoding="utf-8")
	(project / "bot/requirements-dev.txt").write_text(
		"-r requirements.txt\npytest\n", encoding="utf-8"
	)
	(project / "quality-gate.toml").write_text(_argus_typing_manifest(), encoding="utf-8")
	if unsafe:
		(project / "outside").mkdir()
		try:
			(project / "scripts").symlink_to(project / "outside", target_is_directory=True)
		except OSError as error:
			pytest.skip(f"Directory symlinks are unavailable: {error}")
		(project / "apscheduler").mkdir()
	cache_policy = candidate_typing_cache / "releases/v4.6.0"
	code, result = invoke_candidate_wheel(
		request(project, cache_policy, "bot/main.py", background=True), candidate_engine_wheel
	)
	expected = "unsafe_path" if unsafe else "dependencies"
	assert (code, result["status"], result["reason"]) == (2, "unchecked", expected), result
	assert result["components"] == []


@pytest.mark.parametrize(
	("stub_roots", "expected"),
	[('["missing"]', "dependencies"), ('["../outside"]', "manifest")],
)
def test_candidate_cli_fails_closed_for_missing_or_unsafe_component_stub_roots(
	tmp_path: Path,
	candidate_typing_cache: Path,
	candidate_engine_wheel: Path,
	stub_roots: str,
	expected: str,
) -> None:
	project = tmp_path / "argus-invalid-stub-root"
	project.mkdir()
	_prepare_argus_layout(project)
	manifest = project / "quality-gate.toml"
	manifest.write_text(
		manifest.read_text(encoding="utf-8").replace(
			'stub_roots = ["stubs"]', f"stub_roots = {stub_roots}"
		),
		encoding="utf-8",
	)
	policy = candidate_typing_cache / "releases/v4.6.0"
	code, result = invoke_candidate_wheel(
		request(project, policy, "bot/main.py", background=True), candidate_engine_wheel
	)
	assert (code, result["status"], result["reason"]) == (2, "unchecked", expected), result
	assert result["components"] == []


def test_candidate_cli_does_not_import_undeclared_working_tree_modules(
	argus_project: Path,
	candidate_typing_cache: Path,
	candidate_engine_wheel: Path,
) -> None:
	(argus_project / "unlisted.py").write_text("def helper() -> int: ...\n", encoding="utf-8")
	(argus_project / "bot/main.py").write_text(
		"from unlisted import helper\nvalue: int = helper()\n",
		encoding="utf-8",
	)
	policy = candidate_typing_cache / "releases/v4.6.0"
	code, result = invoke_candidate_wheel(
		request(argus_project, policy, "bot/main.py", background=True),
		candidate_engine_wheel,
	)
	assert (code, result["status"], result["reason"]) == (2, "unchecked", "dependencies"), result
	assert result["components"] == []
	assert sorted(item.name for item in (candidate_typing_cache / "releases").iterdir()) == [
		"v4.6.0"
	]


def test_typing_measurements(tmp_path: Path, typing_policy: Path, typing_environment: Path) -> None:
	"""Opt-in measurement; completed cases and incomplete cases remain distinct."""
	if os.environ.get("QUALITY_GATE_TYPING_BENCHMARK") != "1":
		pytest.skip("Opt-in real-tool latency measurement, separate from behavior verification.")
	shutil.copytree(typing_environment, tmp_path / ".venv", symlinks=True)
	(tmp_path / "api.py").write_text(
		"def accept(value: str) -> str:\n    return value\n", encoding="utf-8"
	)
	(tmp_path / "consumer.py").write_text(
		"from api import accept\nresult = accept(123)\n", encoding="utf-8"
	)
	records = []
	for label in ("cold", "warm-unchanged-1", "warm-unchanged-2", "invalidation"):
		if label == "invalidation":
			(tmp_path / "api.py").write_text(
				"def accept(value: int) -> int:\n    return value\n", encoding="utf-8"
			)
		started = time.monotonic()
		payload = request(tmp_path, typing_policy, "api.py", background=True)
		code, result = invoke(payload)
		wall_ms = round((time.monotonic() - started) * 1000)
		assert (code, result["status"]) == (
			(0, "passed") if label == "invalidation" else (1, "failed")
		), result
		selection = result["selections"][0]
		validation_started = time.monotonic()
		# The host owns its independent implementation. This measures the agreed
		# filesystem work, not installed host delivery or aggregate hook latency.
		assert (
			environment_tree(
				Path(selection["environment"]["path"]),
				Path(selection["base_python"]),
				time.monotonic() + 10,
			)
			== selection["environment"]
		)
		inventory(tmp_path, ".", managed=False, deadline=time.monotonic() + 10)
		for record in result["configuration"]:
			path = tmp_path / record["path"]
			assert (digest(path.read_bytes()) if path.is_file() else None) == record["sha256"]
		assert request_files(tmp_path, payload["files"]) == result["files"]
		validation_ms = round((time.monotonic() - validation_started) * 1000)
		records.append(
			{
				"case": label,
				"exit_code": code,
				"status": result["status"],
				"reason": result["reason"],
				"wall_ms": wall_ms,
				"equivalent_delivery_rehash_ms": validation_ms,
				"provider_plus_rehash_ms": wall_ms + validation_ms,
				"provider_ms": result["elapsed_ms"],
				"selections": result["selections"],
				"components": [
					{
						key: component[key]
						for key in ("name", "status", "source_count", "import_count", "analysis_ms")
					}
					for component in result["components"]
				],
			}
		)
	python = tmp_path / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
	startup = time.monotonic()
	version = subprocess.run(
		[str(python), "-I", "-B", "-m", "mypy", "--version"],
		cwd=tmp_path,
		capture_output=True,
		check=False,
		timeout=10,
	)
	assert version.returncode == 0 and version.stdout.startswith(b"mypy 2.3.1")
	startup_ms = round((time.monotonic() - startup) * 1000)
	graph: dict[str, int] = {}
	assert full_component_diagnostics(tmp_path, typing_policy, ".", metrics=graph) == []
	from quality_gate.temp_workspace import temporary_workspace

	with temporary_workspace(tmp_path) as directory:
		profiler = cProfile.Profile()
		profiled = profiler.runcall(
			execute,
			request(tmp_path, typing_policy, "api.py", background=True),
			time.monotonic() + 30,
			Path(directory),
		)
		assert profiled["status"] == "passed"
	phases = [
		{"name": item.code.co_name, "calls": item.callcount, "seconds": item.totaltime}
		for item in profiler.getstats()
		if not isinstance(item.code, str)
		and item.code.co_name
		in {
			"select_environment",
			"environment_tree",
			"_verify_installed_wheel",
			"_analyze",
			"assert_environment_current",
			"run_process",
			"verify_release",
		}
	]
	(tmp_path.parent / "typing-measurements.json").write_text(
		json.dumps(
			{
				"cases": records,
				"mypy_version_startup_ms": startup_ms,
				"graph": graph,
				"application_profile": phases,
				"persistent_mypy_cache": False,
			},
			indent=2,
		),
		encoding="utf-8",
	)
	assert len(records) == MEASUREMENT_CASES
