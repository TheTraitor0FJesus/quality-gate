"""Working-tree edit-check contract tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from importlib.metadata import Distribution
from pathlib import Path
from typing import cast

import pytest
import test_edit_check as edit_check_support
from test_edit_check import (
	FRESH_PROCESSES,
	RELEASED_EDIT_POLICIES,
	REPOSITORY,
	REUSED_PROCESSES,
	assert_project_files_after_cleanup,
	digest,
	invoke,
	prepare_edit_cache,
	request,
)

from quality_gate import __version__
from quality_gate.contracts import load_manifest
from quality_gate.distribution import ExternalTool, ReleaseManifest
from quality_gate.distribution import verify_release as verify_release_cache
from quality_gate.edit_inputs import Configuration
from quality_gate.edit_protocol import SUPPORTED_POLICIES as EDIT_SUPPORTED_POLICIES
from quality_gate.edit_protocol import EditError
from quality_gate.release_contract import UNIFIED_RELEASE_PLATFORM_TOOL_PATHS, UNIFIED_RELEASE_TOOLS

pytest_plugins = ("test_edit_check",)


def test_supported_policy_matrix_keeps_all_released_pins() -> None:
	assert EDIT_SUPPORTED_POLICIES == RELEASED_EDIT_POLICIES | {f"v{__version__}"}


@pytest.mark.parametrize(
	"initial,changed",
	[(b"# first\n", b"# changed\n"), (b"# first\n", None), (None, b"# created\n")],
)
def test_configuration_recapture_preserves_first_identity(
	tmp_path: Path, initial: bytes | None, changed: bytes | None
) -> None:
	path = tmp_path / "ruff.toml"
	if initial is not None:
		path.write_bytes(initial)
	configuration = Configuration(tmp_path)
	configuration.capture("ruff.toml")
	if changed is None:
		path.unlink()
	else:
		path.write_bytes(changed)
	with pytest.raises(EditError, match="stale_input"):
		configuration.capture("ruff.toml")


def test_prepare_edit_cache_uses_selected_release_ruff_not_dev_distribution(
	monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
	import importlib.metadata

	selected_release = load_manifest(REPOSITORY).policy_release
	isolated_cache = tmp_path / "default-cache"
	platform = "windows" if os.name == "nt" else "linux"
	tool_version = UNIFIED_RELEASE_TOOLS["ruff"]
	tool_path = UNIFIED_RELEASE_PLATFORM_TOOL_PATHS[platform].get(
		"ruff", f"ruff-{tool_version}-py3-none-any.whl"
	)
	source_content = b"verified selected-release Ruff artifact"
	source_tool = ExternalTool("ruff", tool_version, tool_path, digest(source_content))
	source_manifest = ReleaseManifest(selected_release, (), (source_tool,))
	source_root = tmp_path / "selected-release" / selected_release
	(source_root / source_tool.path).parent.mkdir(parents=True)
	(source_root / source_tool.path).write_bytes(source_content)
	verified_paths: list[Path] = []

	def verify_selected_release(path: Path) -> ReleaseManifest:
		verified_paths.append(path)
		assert path == source_root
		return source_manifest

	monkeypatch.setattr(edit_check_support, "default_cache_root", lambda: isolated_cache)
	monkeypatch.setenv("QUALITY_GATE_POLICY_ROOT", str(source_root))
	monkeypatch.setattr(edit_check_support, "verify_release", verify_selected_release)

	looked_up_distributions: list[str] = []
	dev_ruff = cast(Distribution, type("DevRuff", (), {"version": "0.17.2"})())
	original_distribution = importlib.metadata.distribution

	def distribution(name: str) -> Distribution:
		looked_up_distributions.append(name)
		if name == "ruff":
			return dev_ruff
		return original_distribution(name)

	monkeypatch.setattr(importlib.metadata, "distribution", distribution)
	monkeypatch.setattr(
		subprocess,
		"run",
		lambda command, **kwargs: subprocess.CompletedProcess(command, 0, b"", b""),
	)

	cache = prepare_edit_cache(tmp_path / "fixture", "v3.1.0")
	generated = verify_release_cache(cache / "releases" / "v3.1.0")
	generated_tool = next(tool for tool in generated.tools if tool.name == "ruff")

	assert generated.version == "v3.1.0"
	assert generated_tool.version == source_tool.version
	assert generated_tool.path == source_tool.path
	assert (cache / "releases" / "v3.1.0" / generated_tool.path).read_bytes() == source_content
	assert "ruff" not in looked_up_distributions
	assert verified_paths == [source_root]


def test_prepare_edit_cache_uses_candidate_when_configured_root_is_selected(
	monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
	selected_release = load_manifest(REPOSITORY).policy_release
	candidate_release = "v0.0.1" if selected_release != "v0.0.1" else "v0.0.2"
	platform = "windows" if os.name == "nt" else "linux"
	tool_version = UNIFIED_RELEASE_TOOLS["ruff"]
	tool_path = UNIFIED_RELEASE_PLATFORM_TOOL_PATHS[platform].get(
		"ruff", f"ruff-{tool_version}-py3-none-any.whl"
	)
	candidate_content = b"verified candidate Ruff artifact"
	candidate_tool = ExternalTool("ruff", tool_version, tool_path, digest(candidate_content))
	cache_root = tmp_path / "default-cache"
	candidate_root = tmp_path / "candidate-release" / candidate_release
	artifact = candidate_root / tool_path
	artifact.parent.mkdir(parents=True)
	artifact.write_bytes(candidate_content)
	candidate_manifest = ReleaseManifest(candidate_release, (), (candidate_tool,))
	verified_paths: list[Path] = []

	def verify_release_for_test(path: Path) -> ReleaseManifest:
		verified_paths.append(path)
		assert path == candidate_root
		return candidate_manifest

	monkeypatch.setenv("QUALITY_GATE_POLICY_ROOT", str(candidate_root))
	monkeypatch.setattr(edit_check_support, "default_cache_root", lambda: cache_root)
	monkeypatch.setattr(edit_check_support, "verify_release", verify_release_for_test)
	monkeypatch.setattr(
		subprocess,
		"run",
		lambda command, **kwargs: subprocess.CompletedProcess(command, 0, b"", b""),
	)

	cache = prepare_edit_cache(tmp_path / "fixture", "v3.1.0")
	generated = verify_release_cache(cache / "releases" / "v3.1.0")
	generated_tool = next(tool for tool in generated.tools if tool.name == "ruff")

	assert generated_tool.version == candidate_tool.version
	assert generated_tool.path == candidate_tool.path
	assert (cache / "releases" / "v3.1.0" / generated_tool.path).read_bytes() == candidate_content
	assert verified_paths == [candidate_root]


@pytest.mark.parametrize("release_state", ["missing", "corrupt"])
def test_prepare_edit_cache_requires_selected_verified_release(
	monkeypatch: pytest.MonkeyPatch, tmp_path: Path, release_state: str
) -> None:
	selected_release = load_manifest(REPOSITORY).policy_release
	cache_root = tmp_path / "policy-cache"
	manifest_path = cache_root / "releases" / selected_release / "release.toml"
	corrupt_manifest = '[release]\nversion = "invalid"\n'
	if release_state == "corrupt":
		manifest_path.parent.mkdir(parents=True)
		manifest_path.write_text(corrupt_manifest, encoding="utf-8")
	monkeypatch.delenv("QUALITY_GATE_POLICY_ROOT", raising=False)
	monkeypatch.setattr(edit_check_support, "default_cache_root", lambda: cache_root)

	with pytest.raises(AssertionError, match=r"quality-gate sync.*quality-gate setup"):
		prepare_edit_cache(tmp_path / "fixture", "v3.1.0")

	assert not (tmp_path / "fixture").exists()
	if release_state == "corrupt":
		assert manifest_path.read_text(encoding="utf-8") == corrupt_manifest
		assert not (cache_root / "quarantine").exists()
	else:
		assert not cache_root.exists()


@pytest.mark.parametrize(
	("command", "check_id"),
	[("edit-types", "mypy"), ("edit-integrity", "repository.git.conflict_markers")],
)
def test_wheel_exposes_read_only_edits_without_extraction(
	tmp_path: Path, engine_wheel: Path, command: str, check_id: str
) -> None:
	project = tmp_path / "project"
	project.mkdir()
	payload = request(project, tmp_path / "cache")
	payload["baseline"] = {"policy_release": "v2.4.2"}
	wheel_hash = digest(engine_wheel.read_bytes())
	completed = subprocess.run(
		[sys.executable, "-B", "-m", "quality_gate.cli", command],
		cwd=tmp_path,
		env=os.environ | {"PYTHONPATH": str(engine_wheel)},
		input=json.dumps(payload).encode(),
		capture_output=True,
		check=False,
		timeout=13,
	)
	result = json.loads(completed.stdout)
	assert completed.stderr == b""
	assert (completed.returncode, result["check_id"], result["status"]) == (
		0,
		check_id,
		"not_applicable",
	), result
	assert result["writes_may_have_occurred"] is False
	assert_project_files_after_cleanup(project)
	assert digest(engine_wheel.read_bytes()) == wheel_hash


@pytest.mark.parametrize("policy", sorted(EDIT_SUPPORTED_POLICIES))
@pytest.mark.parametrize("module", ["quality_gate.cli", "quality_gate.edit_cli"])
def test_wheel_on_pythonpath_checks_working_files_without_extraction(
	tmp_path: Path,
	wheel_cache: Path,
	engine_wheel: Path,
	policy: str,
	module: str,
) -> None:
	project = tmp_path / "project with spaces"
	project.mkdir()
	path = project / "example.py"
	path.write_bytes(b"import sys\nimport os\n\nvalue= (os.name, sys.version)\n")
	launcher = tmp_path / "launcher"
	launcher.mkdir()
	payload = request(project, wheel_cache, "example.py")
	payload["baseline"]["policy_release"] = policy
	wheel_hash = digest(engine_wheel.read_bytes())
	for expected_processes in (FRESH_PROCESSES, REUSED_PROCESSES):
		payload["files"][0]["sha256"] = digest(path.read_bytes())
		payload["deadline_unix_ms"] = int(time.time() * 1000) + 10_000
		completed = subprocess.run(
			[
				sys.executable,
				"-B",
				"-m",
				module,
				*(["edit-check"] if module.endswith(".cli") else []),
			],
			cwd=launcher,
			env={
				**os.environ,
				"PYTHONPATH": str(engine_wheel),
				"LOCALAPPDATA": str(tmp_path / "user-cache"),
				"XDG_CACHE_HOME": str(tmp_path / "user-cache"),
			},
			input=json.dumps(payload).encode(),
			capture_output=True,
			check=False,
			timeout=15,
		)
		assert completed.stderr == b"", completed.stderr.decode(errors="replace")
		result = json.loads(completed.stdout)
		assert completed.returncode == 0, result
		assert result["status"] == "passed"
		assert result["engine_version"] == __version__
		assert result["files"][0]["findings"] == []
		assert result["files"][0]["input_sha256"] == payload["files"][0]["sha256"]
		assert result["files"][0]["sha256"] == digest(path.read_bytes())
		assert result["files"][0]["format"] == "passed"
		assert result["selections"][0]["policy_release"] == policy
		assert result["process_count"] == expected_processes
	assert path.read_bytes() == b"import os\nimport sys\n\nvalue = (os.name, sys.version)\n"
	assert_project_files_after_cleanup(project, "example.py")
	assert list(launcher.iterdir()) == []
	assert digest(engine_wheel.read_bytes()) == wheel_hash


@pytest.mark.parametrize(
	("initial", "expected_exit", "expected_rules"),
	[
		(b"value=  1\n", 0, set()),
		(b"value= missing_name\n", 1, {"F821"}),
	],
)
def test_unchanged_final_result_reuses_verified_selection(
	tmp_path: Path,
	edit_cache: Path,
	initial: bytes,
	expected_exit: int,
	expected_rules: set[str],
) -> None:
	path = tmp_path / "example.py"
	path.write_bytes(initial)
	first_exit, first = invoke(request(tmp_path, edit_cache, "example.py"))
	assert first_exit == expected_exit, first
	assert first["files"][0]["changed"] is True
	final_bytes = path.read_bytes()
	reused_exit, reused = invoke(request(tmp_path, edit_cache, "example.py"))
	assert reused_exit == expected_exit, reused
	assert reused["status"] == first["status"]
	assert reused["files"][0]["findings"] == first["files"][0]["findings"]
	assert {finding["rule"] for finding in reused["files"][0]["findings"]} == expected_rules
	assert reused["files"][0]["sha256"] == digest(final_bytes)
	assert reused["files"][0]["input_sha256"] == digest(final_bytes)
	assert reused["files"][0]["changed"] is False
	assert reused["selections"] == first["selections"]
	assert reused["inputs"] == first["inputs"]
	assert reused["writes_may_have_occurred"] is False
	assert path.read_bytes() == final_bytes
	assert first["process_count"] == FRESH_PROCESSES  # Entry, worker, probe and five Ruff starts.
	assert (
		reused["process_count"] == REUSED_PROCESSES
	)  # Revalidation still probes the selected Python.
