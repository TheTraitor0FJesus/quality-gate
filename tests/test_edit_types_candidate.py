"""Related behavior tests."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
from test_edit_types import (
	_assert_candidate_stale_mutation,
	_prepare_argus_layout,
	_prepare_candidate_runtime,
	_run_candidate_types,
	digest,
	invoke_candidate_wheel,
	request,
)

pytest_plugins = ("test_edit_types",)


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
	policy = candidate_typing_cache / "releases/v4.7.0"
	payload = request(project, policy, "example.py")
	payload["baseline"] = {
		"policy_release": "v4.7.0",
		"python": sys.executable,
		"python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
	}
	payload["phase"] = phase
	code, result = invoke_candidate_wheel(payload, candidate_engine_wheel, command="edit-check")
	assert (code, result["status"], result["engine_version"]) == (0, "passed", "4.7.0"), result
	assert result["selections"][0]["policy_release"] == "v4.7.0"
	assert result["files"][0]["format"] == "passed"
	assert result["files"][0]["sha256"] == digest(original)
	assert path.read_bytes() == original
	assert sorted(item.name for item in (candidate_typing_cache / "releases").iterdir()) == [
		"v4.7.0"
	]


def test_candidate_wheel_runs_secrets_with_candidate_only_cache(
	tmp_path: Path,
	candidate_typing_cache: Path,
	candidate_engine_wheel: Path,
) -> None:
	project = tmp_path / "secrets-candidate"
	project.mkdir()
	(project / "settings.py").write_text("token = 'ordinary value'\n", encoding="utf-8")
	policy = candidate_typing_cache / "releases/v4.7.0"
	code, result = invoke_candidate_wheel(
		request(project, policy, "settings.py"), candidate_engine_wheel, command="edit-secrets"
	)
	assert (code, result["status"], result["engine_version"]) == (0, "passed", "4.7.0"), result
	assert result["selections"][0]["policy_release"] == "v4.7.0"
	assert result["selections"][0]["scanner_version"] == "8.30.1"
	assert result["files"][0]["findings"] == []
	assert sorted(item.name for item in (candidate_typing_cache / "releases").iterdir()) == [
		"v4.7.0"
	]


def test_candidate_wheel_runs_integrity_with_candidate_only_cache(
	tmp_path: Path,
	candidate_typing_cache: Path,
	candidate_engine_wheel: Path,
) -> None:
	project = tmp_path / "integrity-candidate"
	project.mkdir()
	(project / "clean.py").write_text("value: int = 1\n", encoding="utf-8")
	policy = candidate_typing_cache / "releases/v4.7.0"
	code, result = invoke_candidate_wheel(
		request(project, policy, "clean.py"), candidate_engine_wheel, command="edit-integrity"
	)
	assert (code, result["status"], result["engine_version"]) == (0, "passed", "4.7.0"), result
	assert result["selections"][0]["policy_release"] == "v4.7.0"
	assert result["files"][0]["status"] == "passed"
	assert sorted(item.name for item in (candidate_typing_cache / "releases").iterdir()) == [
		"v4.7.0"
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
