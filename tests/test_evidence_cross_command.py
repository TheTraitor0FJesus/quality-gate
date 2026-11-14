"""Exercise evidence shared by the public edit and staged Gate commands."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from edit_check_support78 import edit_cache as edit_cache
from edit_check_support78 import invoke, managed, request

from quality_gate import runner
from quality_gate.contracts import Status
from quality_gate.launcher import prepare


def test_edit_evidence_is_reused_by_staged_gate(
	tmp_path: Path, edit_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	managed(tmp_path, edit_cache)
	manifest = tmp_path / "quality-gate.toml"
	manifest.write_bytes(manifest.read_bytes().replace(b"\r\n", b"\n"))
	paths = [f"src/file_{index}.py" for index in range(10)]
	for index, path in enumerate(paths):
		(tmp_path / path).write_bytes(
			b"value = missing\n" if index in {1, 2} else b"value = 1\n"
		)
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
	assert any(item.path == paths[2] for item in ruff.findings)

	# A completely reused format result reports lookup cost without claiming a process ran.
	unchanged = runner.check(tmp_path)
	reused_format = next(
		item for item in unchanged.results if item.check_id == "python.component_1.format"
	)
	assert "10 per-file results reused, 0 executed" in reused_format.summary
	assert "shared evidence lookup" in reused_format.summary
	assert "original producer operation duration" in reused_format.summary
	assert reused_format.telemetry is not None
	assert reused_format.telemetry.disposition == "not-run"
	assert reused_format.telemetry.lookup_ms is not None
	assert "reused" in reused_format.telemetry.reason

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
