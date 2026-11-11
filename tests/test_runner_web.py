"""Related behavior tests."""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

import pytest
from test_runner import (
	_assert_biome_invocations,
	_fake_biome_command,
	_fake_candidate_snapshot,
	_web_fixture,
	_web_manifest,
	_web_prepared,
)

from quality_gate import runner


def test_check_runs_read_only_biome_lint_and_format_for_declared_assets(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	policy_root = _web_fixture(tmp_path)
	prepared = _web_prepared(policy_root)
	argv_log = _fake_biome_command(tmp_path, monkeypatch)
	(tmp_path / "assets/other/undeclared.js").parent.mkdir(parents=True, exist_ok=True)
	(tmp_path / "assets/other/undeclared.js").write_text("const unused = 1;\n", encoding="utf-8")
	before = {
		relative: (tmp_path / relative).read_bytes()
		for relative in ("assets/js/app.js", "assets/css/app.css", "assets/other/undeclared.js")
	}

	monkeypatch.setattr(runner, "candidate_snapshot", _fake_candidate_snapshot)
	monkeypatch.setattr(runner, "prepare", lambda *args, **kwargs: prepared)

	verdict = runner.check(tmp_path)

	assert [result.check_id for result in verdict.results if ".biome_" in result.check_id] == [
		"web.component_1.biome_lint",
		"web.component_1.biome_format",
	]
	assert all(
		result.status is runner.Status.PASSED
		for result in verdict.results
		if ".biome_" in result.check_id
	)
	assert {relative: (tmp_path / relative).read_bytes() for relative in before} == before
	_assert_biome_invocations(argv_log, {"assets/js/app.js", "assets/css/app.css"})


@pytest.mark.parametrize(
	("javascript", "css"),
	((True, False), (False, True)),
)
def test_check_runs_biome_for_components_with_one_declared_web_language(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	javascript: bool,
	css: bool,
) -> None:
	policy_root = _web_fixture(tmp_path, javascript=javascript, css=css)
	argv_log = _fake_biome_command(tmp_path, monkeypatch)
	monkeypatch.setattr(runner, "candidate_snapshot", _fake_candidate_snapshot)
	monkeypatch.setattr(runner, "prepare", lambda *args, **kwargs: _web_prepared(policy_root))

	verdict = runner.check(tmp_path)

	biome_results = [result for result in verdict.results if ".biome_" in result.check_id]
	assert [result.status for result in biome_results] == [runner.Status.PASSED] * 2
	expected_assets = {"assets/js/app.js"} if javascript else {"assets/css/app.css"}
	_assert_biome_invocations(argv_log, expected_assets)


def test_biome_policy_is_version_locked_to_stable_raw_asset_checks() -> None:
	config = json.loads((runner.POLICY_DIR / "biome.json").read_text(encoding="utf-8"))
	inventory = tomllib.loads((runner.POLICY_DIR / "biome.toml").read_text(encoding="utf-8"))

	assert config["$schema"].endswith("/2.2.6/schema.json")
	assert config["linter"] == {"enabled": True, "rules": {"recommended": True}}
	assert config["assist"] == {"enabled": False}
	assert config["css"] == {"formatter": {"enabled": True}}
	assert config["files"] == {"maxSize": 5 * 1024 * 1024}
	assert inventory["biome"] == {
		"version": "2.2.6",
		"platforms": {
			"linux-x64": {
				"path": "biome-linux-x64",
				"url": "https://github.com/biomejs/biome/releases/download/%40biomejs/biome%402.2.6/biome-linux-x64",
				"sha256": "ad989034fff59c9b4e45a55d0d71cbfbd5bc673cfb7193ee01268773e1eb84a7",
			},
			"windows-x64": {
				"path": "biome-win32-x64.exe",
				"url": "https://github.com/biomejs/biome/releases/download/%40biomejs/biome%402.2.6/biome-win32-x64.exe",
				"sha256": "157039ed9aa4817595be54c7d929acab354a3284fbd05e78218e69fa5773e92b",
			},
		},
	}


def test_check_marks_biome_unchecked_when_asset_exceeds_policy_file_limit(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	policy_root = _web_fixture(tmp_path)
	prepared = _web_prepared(policy_root)
	(tmp_path / "assets/js/app.js").write_bytes(b"x" * (5 * 1024 * 1024 + 1))
	monkeypatch.setattr(runner, "candidate_snapshot", _fake_candidate_snapshot)
	monkeypatch.setattr(runner, "prepare", lambda *args, **kwargs: prepared)

	verdict = runner.check(tmp_path)

	biome_results = [result for result in verdict.results if ".biome_" in result.check_id]
	assert [result.status for result in biome_results] == [runner.Status.UNCHECKED] * 2


def test_check_marks_biome_unchecked_when_standalone_binary_is_missing(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	policy_root = _web_fixture(tmp_path, binary=None)
	monkeypatch.setattr(runner, "candidate_snapshot", _fake_candidate_snapshot)
	monkeypatch.setattr(runner, "prepare", lambda *args, **kwargs: _web_prepared(policy_root))

	verdict = runner.check(tmp_path)

	biome_results = [result for result in verdict.results if ".biome_" in result.check_id]
	assert [result.status for result in biome_results] == [runner.Status.UNCHECKED] * 2
	assert verdict.exit_code == runner.EXIT_UNCHECKED


def test_check_marks_biome_unchecked_when_standalone_binary_is_corrupt(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	policy_root = _web_fixture(tmp_path)
	platform = "windows-x64" if os.name == "nt" else "linux-x64"
	entry = tomllib.loads((policy_root / "quality_gate" / "policy" / "biome.toml").read_text())[
		"biome"
	]["platforms"][platform]
	(policy_root / entry["path"]).write_bytes(b"corrupt biome")

	monkeypatch.setattr(runner, "candidate_snapshot", _fake_candidate_snapshot)
	monkeypatch.setattr(runner, "prepare", lambda *args, **kwargs: _web_prepared(policy_root))

	verdict = runner.check(tmp_path)

	biome_results = [result for result in verdict.results if ".biome_" in result.check_id]
	assert [result.status for result in biome_results] == [runner.Status.UNCHECKED] * 2


def test_check_does_not_duplicate_biome_results_after_partial_web_failure(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	policy_root = _web_fixture(tmp_path)
	(tmp_path / "quality-gate.toml").write_text(
		_web_manifest()
		+ '\n[[web]]\nname = "second"\nroot = "assets"\n'
		+ 'javascript = ["js/**/*.js"]\ncss = []\nexclude = []\n',
		encoding="utf-8",
	)
	prepared = _web_prepared(policy_root)
	command_calls = 0

	def fail_first_biome_command(_prepared: object) -> list[str]:
		nonlocal command_calls
		command_calls += 1
		if command_calls == 1:
			raise runner.QualityGateError(
				"first Biome component is unavailable",
				exit_code=runner.EXIT_UNCHECKED,
				recovery_action="restore Biome",
			)
		return ["unused"]

	def fail_temporary_directory(_root: Path) -> object:
		raise OSError("temporary directory unavailable")

	monkeypatch.setattr(runner, "candidate_snapshot", _fake_candidate_snapshot)
	monkeypatch.setattr(runner, "prepare", lambda *args, **kwargs: prepared)
	monkeypatch.setattr(runner, "_biome_command", fail_first_biome_command)
	monkeypatch.setattr(runner, "temporary_directory", fail_temporary_directory)

	verdict = runner.check(tmp_path)

	biome_ids = [result.check_id for result in verdict.results if ".biome_" in result.check_id]
	assert biome_ids == [
		"web.component_1.biome_lint",
		"web.component_1.biome_format",
		"web.component_2.biome_lint",
		"web.component_2.biome_format",
	]
	assert all(
		result.status is runner.Status.UNCHECKED
		for result in verdict.results
		if ".biome_" in result.check_id
	)


@pytest.mark.parametrize(
	("relative", "content", "failed_check"),
	(
		("assets/js/app.js", "const unused = 1;\n", "web.component_1.biome_lint"),
		("assets/css/app.css", "a { lint-error: true; }\n", "web.component_1.biome_lint"),
		("assets/css/app.css", "a{bad-format}\n", "web.component_1.biome_format"),
	),
)
def test_check_reports_biome_findings_for_invalid_staged_content(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	relative: str,
	content: str,
	failed_check: str,
) -> None:
	policy_root = _web_fixture(tmp_path)
	prepared = _web_prepared(policy_root)
	_fake_biome_command(tmp_path, monkeypatch)
	(tmp_path / relative).write_text(content, encoding="utf-8")

	monkeypatch.setattr(runner, "candidate_snapshot", _fake_candidate_snapshot)
	monkeypatch.setattr(runner, "prepare", lambda *args, **kwargs: prepared)

	verdict = runner.check(tmp_path)

	biome_results = {
		result.check_id: result.status for result in verdict.results if ".biome_" in result.check_id
	}
	assert biome_results[failed_check] is runner.Status.FAILED
	other_check = next(check_id for check_id in biome_results if check_id != failed_check)
	assert biome_results[other_check] is runner.Status.PASSED
