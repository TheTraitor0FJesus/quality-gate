"""Exercise evidence shared by the public edit and staged Gate commands."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from edit_check_support78 import edit_cache as edit_cache
from edit_check_support78 import invoke, managed, request

from quality_gate import runner, runner_ruff
from quality_gate.contracts import Status
from quality_gate.distribution import PolicyCache, verify_release
from quality_gate.edit_policy import ruff_artifact, tool_path
from quality_gate.launcher import prepare
from quality_gate.operation import GateOperation
from quality_gate.reporting import render


@pytest.mark.parametrize(
	"boundary", ["discovery", "report", "configuration", "other", "argv", "syntax", "format-exit"]
)
def test_staged_gate_runs_full_ruff_when_file_evidence_is_unavailable(
	tmp_path: Path, edit_cache: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
	for flag in ("CI", "GITHUB_ACTIONS", "QUALITY_GATE_ADMISSION_CI"):
		monkeypatch.delenv(flag, raising=False)
	example = managed(tmp_path, edit_cache)
	manifest = tmp_path / "quality-gate.toml"
	manifest.write_bytes(manifest.read_bytes().replace(b"\r\n", b"\n"))
	example.write_bytes(b"value = 1\n")
	payload = request(tmp_path, edit_cache, "src/example.py")
	payload["phase"] = "validate"
	exit_code, report = invoke(payload)
	assert exit_code == 0, report
	policy_root = edit_cache / "releases/v2.4.2"
	artifact = ruff_artifact(policy_root, verify_release(policy_root))
	native = tool_path(PolicyCache(edit_cache), artifact.sha256)
	assert native.is_file()
	ordinary_tool = runner._tool_command
	monkeypatch.setattr(
		runner,
		"_tool_command",
		lambda prepared, python, name: (
			[str(native)] if name == "ruff" else ordinary_tool(prepared, python, name)
		),
	)
	example.write_bytes(b"value=missing\n")
	if boundary == "configuration":
		(tmp_path / "pyproject.toml").write_bytes(b"#" + b"x" * (256 * 1024) + b"\n")
	elif boundary == "other":
		(tmp_path / "src/pyproject.toml").write_bytes(b"[project\n")
	elif boundary == "argv":
		monkeypatch.setattr(runner_ruff, "_MAX_TARGET_ARGUMENT_CHARS", 2048)
	elif boundary in {"syntax", "format-exit"}:
		example.write_bytes(b"value = missing  # noqa: F821\n")
		(tmp_path / "src/broken.py").write_bytes(b"def broken(\n")
	for arguments in (("init",), ("add", ".")):
		subprocess.run(["git", *arguments], cwd=tmp_path, check=True, capture_output=True)

	cache_home = str(tmp_path.parent / f"{tmp_path.name}-user-cache")
	monkeypatch.setenv("LOCALAPPDATA", cache_home)
	monkeypatch.setenv("XDG_CACHE_HOME", cache_home)
	monkeypatch.setattr(
		runner, "prepare", lambda root, **options: prepare(root, cache_dir=edit_cache, **options)
	)
	real_run = GateOperation.run_process
	commands: list[list[str]] = []

	def run(self: GateOperation, command: list[str], **options: Any) -> tuple[int, bytes, bytes]:
		commands.append(command)
		if boundary == "format-exit" and "format" in command and "--output-format" in command:
			return 2, b"[]", b"formatter did not complete"
		if "--show-files" in command or "--output-format" in command:
			if (boundary == "discovery" and "--show-files" in command) or (
				boundary == "report" and "--extend-ignore" in command
			):
				options["limit"] = 16
		return real_run(self, command, **options)

	monkeypatch.setattr(GateOperation, "run_process", run)
	results = {result.check_id: result for result in runner.check(tmp_path).results}
	lint = results["python.component_1.ruff"]
	assert lint.status is Status.FAILED, lint.findings
	if boundary in {"syntax", "format-exit"}:
		assert any("invalid-syntax" in finding.message for finding in lint.findings)
		assert all("F821" not in finding.message for finding in lint.findings)
		if boundary == "format-exit":
			assert any("format" in command and "--output-format" in command for command in commands)
			assert any(
				"format" in command and "src" in command and "--output-format" not in command
				for command in commands
			)
		monkeypatch.setenv("CI", "true")
		full = {result.check_id: result for result in runner.check(tmp_path).results}
		for check_id in ("python.component_1.ruff", "python.component_1.format"):
			assert results[check_id].status is full[check_id].status is Status.FAILED
		assert all("F821" not in finding.message for finding in full[lint.check_id].findings)
		return
	assert any("F821" in finding.message for finding in lint.findings)
	assert results["python.component_1.format"].status is Status.FAILED
	assert any("--show-files" in command for command in commands)
	assert any(
		"check" in command
		and "src" in command
		and "--output-format" not in command
		and "--show-files" not in command
		for command in commands
	)


@pytest.mark.parametrize("unformatted", [False, True])
def test_edit_evidence_is_reused_by_staged_gate(
	tmp_path: Path, edit_cache: Path, monkeypatch: pytest.MonkeyPatch, unformatted: bool
) -> None:
	for flag in ("CI", "GITHUB_ACTIONS", "QUALITY_GATE_ADMISSION_CI"):
		monkeypatch.delenv(flag, raising=False)
	managed(tmp_path, edit_cache)
	manifest = tmp_path / "quality-gate.toml"
	manifest.write_bytes(manifest.read_bytes().replace(b"\r\n", b"\n"))
	paths = [f"src/file_{index}.py" for index in range(10)]
	for index, path in enumerate(paths):
		(tmp_path / path).write_bytes(b"value = missing\n" if index in {1, 2} else b"value = 1\n")
	if unformatted:
		(tmp_path / paths[9]).write_bytes(b"value=1\n")
	(tmp_path / "src/example.py").unlink()
	for args in (("init",), ("add", ".")):
		subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
	payload = request(tmp_path, edit_cache, *paths)
	payload["phase"] = "validate"
	payload["deadline_unix_ms"] += 20_000
	exit_code, first = invoke(payload)
	assert exit_code == 1, first
	(tmp_path / paths[1]).write_bytes(b"value = 2\n")
	subprocess.run(["git", "add", paths[1]], cwd=tmp_path, check=True, capture_output=True)
	cache_home = tmp_path.parent / f"{tmp_path.name}-user-cache"
	monkeypatch.setenv("LOCALAPPDATA", str(cache_home))
	monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
	original_prepare = prepare
	monkeypatch.setattr(
		runner,
		"prepare",
		lambda root, **kwargs: original_prepare(root, cache_dir=edit_cache, **kwargs),
	)
	verdict = runner.check(tmp_path)
	ruff = next(item for item in verdict.results if item.check_id == "python.component_1.ruff")
	assert ruff.status == Status.FAILED, ruff
	assert "9 per-file results reused, 1 executed" in ruff.summary
	assert verdict.operation is not None
	assert any(stage.stage == "cleanup" for stage in verdict.operation.stages)
	assert ruff.telemetry is not None and ruff.telemetry.stage == ruff.check_id
	format_check = next(
		item for item in verdict.results if item.check_id == "python.component_1.format"
	)
	assert (
		format_check.telemetry is not None and format_check.telemetry.stage == format_check.check_id
	)
	assert ruff.telemetry.disposition == "executed"
	assert format_check.telemetry.disposition == "executed"
	assert format_check.telemetry.original_elapsed_ms is not None
	assert format_check.telemetry.original_operation_id is not None
	assert any(item.path == paths[2] for item in ruff.findings)

	# A completely reused format result reports lookup cost without claiming a process ran.
	unchanged = runner.check(tmp_path)
	reused_format = next(
		item for item in unchanged.results if item.check_id == "python.component_1.format"
	)
	assert "10 per-file results reused, 0 executed" in reused_format.summary
	assert reused_format.status is (Status.FAILED if unformatted else Status.PASSED)
	assert "evidence validation" in reused_format.summary
	assert "original producer operation duration" in reused_format.summary
	assert reused_format.telemetry is not None
	assert reused_format.telemetry.disposition == "reused"
	assert reused_format.telemetry.lookup_ms is None
	assert reused_format.telemetry.validation_ms is not None
	assert reused_format.telemetry.original_elapsed_ms is not None
	assert reused_format.telemetry.original_operation_id is not None
	assert "reused" in reused_format.telemetry.reason
	format_line = next(
		line
		for line in render(unchanged, profile=True).splitlines()
		if "python.component_1.format: elapsed_ms=" in line
	)
	assert "disposition=reused" in format_line
	assert f"outcome={reused_format.status.value}" in format_line
	assert "original_elapsed_ms=" in format_line and "original_operation=" in format_line

	# The staged bytes stay unchanged while the raw working-tree bytes diverge.
	(tmp_path / paths[0]).write_bytes(b"value = 3\n")
	second = runner.check(tmp_path)
	ruff = next(item for item in second.results if item.check_id == "python.component_1.ruff")
	assert ruff.status == Status.FAILED, ruff
	assert "9 per-file results reused, 1 executed" in ruff.summary
	assert any(item.path == paths[2] for item in ruff.findings)

	# Changing only the staged mode must invalidate that file's previous evidence.
	subprocess.run(
		["git", "update-index", "--chmod=+x", "--", paths[3]],
		cwd=tmp_path,
		check=True,
		capture_output=True,
	)
	third = runner.check(tmp_path)
	ruff = next(item for item in third.results if item.check_id == "python.component_1.ruff")
	assert ruff.status == Status.FAILED, ruff
	assert "8 per-file results reused, 2 executed" in ruff.summary
	assert any(item.path == paths[2] for item in ruff.findings)

	# Newly staged members execute; deleted members leave the checked scope.
	new_path = "src/added.py"
	(tmp_path / new_path).write_bytes(b"value = 4\n")
	subprocess.run(["git", "add", new_path], cwd=tmp_path, check=True, capture_output=True)
	added = runner.check(tmp_path)
	ruff = next(item for item in added.results if item.check_id == "python.component_1.ruff")
	assert ruff.status == Status.FAILED, ruff
	assert "8 per-file results reused, 3 executed" in ruff.summary
	(tmp_path / paths[1]).unlink()
	subprocess.run(["git", "add", "-u", paths[1]], cwd=tmp_path, check=True, capture_output=True)
	deleted = runner.check(tmp_path)
	ruff = next(item for item in deleted.results if item.check_id == "python.component_1.ruff")
	assert ruff.status == Status.FAILED, ruff
	assert "8 per-file results reused, 2 executed" in ruff.summary
	assert any(item.path == paths[2] for item in ruff.findings)

	# Git normalizes the staged bytes while edit-check observes the raw CRLF file.
	(tmp_path / ".gitattributes").write_text("src/file_0.py text eol=lf\n", encoding="utf-8")
	(tmp_path / paths[0]).write_bytes(b"value = 3\r\n")
	subprocess.run(
		["git", "add", ".gitattributes", paths[0]], cwd=tmp_path, check=True, capture_output=True
	)
	payload = request(tmp_path, edit_cache, paths[0])
	payload["phase"] = "validate"
	payload["deadline_unix_ms"] += 20_000
	exit_code, raw = invoke(payload)
	assert exit_code == 0, raw
	result = runner.check(tmp_path)
	ruff = next(item for item in result.results if item.check_id == "python.component_1.ruff")
	assert ruff.status == Status.FAILED, ruff
	assert "8 per-file results reused, 2 executed" in ruff.summary
	assert any(item.path == paths[2] for item in ruff.findings)
