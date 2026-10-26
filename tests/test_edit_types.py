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
from collections.abc import Iterator
from dataclasses import asdict
from importlib.util import cache_from_source
from pathlib import Path
from typing import Any

import pytest
from test_typing_optout import manifest_text

from quality_gate.contracts import load_manifest
from quality_gate.distribution import default_cache_root, verify_release
from quality_gate.edit_protocol import EditError
from quality_gate.edit_type_environment import _MANAGED_RUNTIME_PTH_SHA256, _validate_site
from quality_gate.edit_type_inputs import environment_tree, inventory, request_files
from quality_gate.edit_types import execute
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
			if any(tmp_path.glob(".quality-gate-edit-*/source/source.py")):
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
		assert not list(tmp_path.glob(".quality-gate-edit-*"))
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
		assert not list(tmp_path.glob(".quality-gate-edit-*"))
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
	assert (code, result["status"]) == (1, "failed"), result
	component = result["components"][0]
	assert component["diagnostics"] == [
		{"path": "consumer.py", "line": 2, "column": 17, "code": "arg-type"}
	]
	assert {item["path"] for item in component["inventory"]} == {"api.py", "consumer.py"}
	assert result["selections"][0]["policy_release"] == typing_policy.name
	assert result["selections"][0]["mypy_version"] == "2.3.1"
	assert component["diagnostics"] == full_component_diagnostics(tmp_path, typing_policy, ".")
	assert not list(tmp_path.glob(".quality-gate-edit-*"))


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
	assert not list(tmp_path.glob(".quality-gate-edit-*"))


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
			if any(tmp_path.glob(".quality-gate-edit-*/source/source.py")):
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
	with tempfile.TemporaryDirectory(prefix=".quality-gate-edit-", dir=tmp_path) as directory:
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
