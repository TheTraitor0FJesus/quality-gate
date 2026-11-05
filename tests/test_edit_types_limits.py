"""Related behavior tests."""

from __future__ import annotations

import json
import os
import py_compile
import shutil
import sys
import tempfile
import threading
import time
from dataclasses import asdict
from importlib.util import cache_from_source
from pathlib import Path

import pytest
from test_edit_types import (
	_run_candidate_types,
	digest,
	invoke,
	request,
)
from test_typing_optout import manifest_text
from typing_test_support import full_component_diagnostics

from quality_gate.contracts import load_manifest
from quality_gate.distribution import verify_release
from quality_gate.runtime import RuntimeManager, runtime_fingerprint, runtime_identity

pytest_plugins = ("test_edit_types",)


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
	assert result["engine_version"] == "4.7.0"
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
	policy = candidate_typing_cache / "releases/v4.7.0"
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
		"v4.7.0"
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
