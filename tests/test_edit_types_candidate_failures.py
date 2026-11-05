"""Related behavior tests."""

from __future__ import annotations

import cProfile
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from test_edit_types import (
	MEASUREMENT_CASES,
	_argus_typing_manifest,
	_prepare_argus_layout,
	digest,
	invoke,
	invoke_candidate_wheel,
	request,
)
from typing_test_support import full_component_diagnostics

from quality_gate.edit_type_inputs import environment_tree, inventory, request_files
from quality_gate.edit_types import execute

pytest_plugins = ("test_edit_types",)


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
	cache_policy = candidate_typing_cache / "releases/v4.7.0"
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
	policy = candidate_typing_cache / "releases/v4.7.0"
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
	policy = candidate_typing_cache / "releases/v4.7.0"
	code, result = invoke_candidate_wheel(
		request(argus_project, policy, "bot/main.py", background=True),
		candidate_engine_wheel,
	)
	assert (code, result["status"], result["reason"]) == (2, "unchecked", "dependencies"), result
	assert result["components"] == []
	assert sorted(item.name for item in (candidate_typing_cache / "releases").iterdir()) == [
		"v4.7.0"
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
