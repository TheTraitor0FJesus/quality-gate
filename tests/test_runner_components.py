"""Related behavior tests."""

from __future__ import annotations

import sys
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_runner import (
	FIXTURES,
	_fake_candidate_snapshot,
)

from quality_gate import runner
from quality_gate.distribution import ExternalTool, ReleaseFile, ReleaseManifest


def test_check_skips_local_admission_in_ci(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setattr(runner, "running_in_ci", lambda: True)
	monkeypatch.setattr(
		runner,
		"gate_admission",
		lambda: pytest.fail("CI must not wait on the local queue"),
	)
	monkeypatch.setattr(runner, "temporary_workspace", lambda _root: nullcontext())
	monkeypatch.setattr(runner, "candidate_snapshot", _fake_candidate_snapshot)
	passed = runner.Verdict(
		(runner.CheckResult("ci.fixture", runner.Status.PASSED, "CI check completed"),)
	)
	monkeypatch.setattr(runner, "_check_snapshot", lambda *_args, **_kwargs: passed)

	assert runner._run_snapshot(tmp_path) is passed


def test_ruff_children_receive_a_bounded_worker_environment(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	monkeypatch.delenv(runner.RUFF_THREADS_ENV, raising=False)
	actual_run = runner.run
	observed: list[str] = []

	def inspect_child_environment(
		_command: list[str],
		root: Path,
		environment: dict[str, str],
		*,
		timeout: float | None = None,
	) -> str:
		assert timeout is not None and timeout > 0
		output = actual_run(
			[
				sys.executable,
				"-c",
				"import os; print(os.environ.get('RAYON_NUM_THREADS', 'unset'))",
			],
			root,
			environment,
			timeout=timeout,
		)
		observed.append(output.strip())
		return output

	monkeypatch.setattr(runner, "run", inspect_child_environment)
	base_environment = runner._safe_environment("quality-gate-temporary")
	deadline = time.monotonic() + 30
	executed, errors, _outputs = runner._execute_commands(
		tmp_path,
		base_environment,
		[
			("python.component_1.ruff", ["ruff", "check"], 10),
			("python.component_1.format", ["ruff", "format"], 10),
			("python.component_1.mypy", ["mypy"], 10),
		],
		10,
		deadline,
	)

	assert executed == [
		"python.component_1.ruff",
		"python.component_1.format",
		"python.component_1.mypy",
	]
	assert errors == []
	assert observed == ["4", "4", "unset"]


def test_ruff_worker_override_is_validated_and_applied_only_to_ruff(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	monkeypatch.setenv(runner.RUFF_THREADS_ENV, "8")
	assert runner._ruff_environment({}) == {"RAYON_NUM_THREADS": "8"}
	for invalid in ("0", "65", "-1", "many"):
		monkeypatch.setenv(runner.RUFF_THREADS_ENV, invalid)
		with pytest.raises(runner.QualityGateError, match=runner.RUFF_THREADS_ENV):
			runner._ruff_environment({})


def test_missing_external_tool_is_unchecked(monkeypatch: pytest.MonkeyPatch) -> None:
	root = FIXTURES / "valid"
	missing_tool = root / "missing-ruff"
	prepared = SimpleNamespace(
		policy_root=runner.POLICY_DIR.parent.parent,
		release_manifest=ReleaseManifest(
			"v2.0.0",
			(ReleaseFile("quality_gate.whl", "a" * 64),),
			(ExternalTool("ruff", "0.15.12", missing_tool.name, "b" * 64),),
		),
		runtimes=(SimpleNamespace(python=Path(sys.executable), current=True),),
	)

	monkeypatch.setattr(runner, "prepare", lambda *args, **kwargs: prepared)
	monkeypatch.setattr(runner, "candidate_snapshot", _fake_candidate_snapshot)

	verdict = runner.check(root)

	ruff = next(
		result for result in verdict.results if result.check_id == "python.component_1.ruff"
	)
	assert ruff.status is runner.Status.UNCHECKED
	assert verdict.exit_code == runner.EXIT_UNCHECKED


def test_format_uses_only_explicit_paths(monkeypatch: pytest.MonkeyPatch) -> None:
	root = FIXTURES / "valid"
	calls: list[list[str]] = []
	environments: list[dict[str, str]] = []
	monkeypatch.setenv(runner.RUFF_THREADS_ENV, "8")
	monkeypatch.setattr(
		runner,
		"prepare",
		lambda *args, **kwargs: SimpleNamespace(
			policy_root=runner.POLICY_DIR.parent.parent,
			runtimes=(SimpleNamespace(python=Path(sys.executable), current=True),),
		),
	)
	monkeypatch.setattr(
		runner, "temporary_directory", lambda root: nullcontext("test-temporary-directory")
	)

	def record_run(
		command: list[str], root: Path, environment: dict[str, str], *, timeout: float
	) -> None:
		calls.append(command)
		environments.append(environment)

	monkeypatch.setattr(runner, "run", record_run)

	runner.format_paths(root, ("app",))

	assert len(calls) == 1
	assert calls[0][calls[0].index("format") + 1] == "--config"
	assert calls[0][-1] == "app"
	assert "--check" not in calls[0]
	assert "RAYON_NUM_THREADS" not in environments[0]


def test_policy_access_error_is_unchecked(monkeypatch: pytest.MonkeyPatch) -> None:
	root = FIXTURES / "valid"
	monkeypatch.setattr(runner, "candidate_snapshot", _fake_candidate_snapshot)

	def fail_prepare(*args: object, **kwargs: object) -> None:
		raise PermissionError("cache is inaccessible")

	monkeypatch.setattr(
		runner,
		"prepare",
		fail_prepare,
	)

	verdict = runner.check(root)

	policy = next(result for result in verdict.results if result.check_id == "runtime.policy")
	assert policy.status is runner.Status.UNCHECKED
	documents = next(
		result for result in verdict.results if result.check_id == "manifest.documents"
	)
	assert documents.status is runner.Status.PASSED
	assert not any(result.check_id.startswith("python.component_") for result in verdict.results)
	assert verdict.exit_code == runner.EXIT_UNCHECKED


def test_workspace_failure_reports_only_the_unavailable_workspace(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	root = FIXTURES / "valid"

	def fail_workspace(_root: Path) -> object:
		raise runner.TemporaryWorkspaceError("repository temp directory is unavailable")

	monkeypatch.setattr(runner, "temporary_workspace", fail_workspace)

	verdict = runner._run_snapshot(root)

	assert verdict.exit_code == runner.EXIT_UNCHECKED
	assert [result.check_id for result in verdict.results] == ["temporary.workspace"]
	assert verdict.results[0].status is runner.Status.UNCHECKED


def test_not_applicable_component_tests_are_reported(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	root = FIXTURES / "valid"
	monkeypatch.setattr(runner, "candidate_snapshot", _fake_candidate_snapshot)
	monkeypatch.setattr(
		runner,
		"prepare",
		lambda *args, **kwargs: SimpleNamespace(
			policy_root=runner.POLICY_DIR.parent.parent,
			runtimes=(SimpleNamespace(python=Path(sys.executable), current=True),),
		),
	)
	monkeypatch.setattr(runner, "run", lambda *args, **kwargs: None)
	monkeypatch.setattr(
		runner, "temporary_directory", lambda root: nullcontext("test-temporary-directory")
	)

	verdict = runner.check(root)

	tests = next(
		result for result in verdict.results if result.check_id == "python.component_1.pytest"
	)
	assert tests.status is runner.Status.NOT_APPLICABLE


def test_pytest_collection_failure_is_failed(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	root = FIXTURES / "python-tests"
	monkeypatch.setattr(runner, "candidate_snapshot", _fake_candidate_snapshot)
	monkeypatch.setattr(
		runner,
		"prepare",
		lambda *args, **kwargs: SimpleNamespace(
			policy_root=runner.POLICY_DIR.parent.parent,
			runtimes=(SimpleNamespace(python=Path(sys.executable), current=True),),
		),
	)
	monkeypatch.setattr(
		runner,
		"temporary_directory",
		lambda root: nullcontext("test-temporary-directory"),
	)
	seen_commands: list[list[str]] = []

	def fail_collection(
		command: list[str], root: Path, environment: dict[str, str], *, timeout: float
	) -> None:
		seen_commands.append(command)
		if "pytest" in command:
			raise runner.QualityGateError(
				"ERROR collecting tests/test_app.py",
				exit_code=runner.EXIT_UNCHECKED,
				recovery_action="fix collection",
			)

	monkeypatch.setattr(runner, "run", fail_collection)

	verdict = runner.check(root)

	pytest_result = next(
		result for result in verdict.results if result.check_id == "python.component_1.pytest"
	)
	assert pytest_result.status is runner.Status.UNCHECKED
	assert [
		result.check_id
		for result in verdict.results
		if result.check_id == "python.component_1.pytest"
	] == ["python.component_1.pytest"]
	pytest_command = next(command for command in seen_commands if "pytest" in command)
	assert "tests" in pytest_command
	assert any(path.replace("\\", "/") == "tests/extra" for path in pytest_command)


def test_missing_test_path_is_unchecked_without_hiding_other_checks(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	root = FIXTURES / "missing-test-path"
	monkeypatch.setattr(runner, "candidate_snapshot", _fake_candidate_snapshot)
	monkeypatch.setattr(
		runner,
		"prepare",
		lambda *args, **kwargs: SimpleNamespace(
			policy_root=runner.POLICY_DIR.parent.parent,
			runtimes=(SimpleNamespace(python=Path(sys.executable), current=True),),
		),
	)
	monkeypatch.setattr(runner, "run", lambda *args, **kwargs: None)
	monkeypatch.setattr(
		runner, "temporary_directory", lambda root: nullcontext("test-temporary-directory")
	)

	verdict = runner.check(root)

	missing = next(
		result for result in verdict.results if result.check_id == "python.component_1.test_path_1"
	)
	assert missing.status is runner.Status.UNCHECKED
	assert any(result.check_id == "python.component_1.ruff" for result in verdict.results)


def test_component_runtime_failure_does_not_hide_other_components(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	root = FIXTURES / "multi-component"
	monkeypatch.setattr(runner, "candidate_snapshot", _fake_candidate_snapshot)
	monkeypatch.setattr(
		runner,
		"prepare",
		lambda *args, **kwargs: SimpleNamespace(
			policy_root=runner.POLICY_DIR.parent.parent,
			runtimes=(
				SimpleNamespace(python=None, current=False, reason="runtime is missing"),
				SimpleNamespace(python=Path(sys.executable), current=True),
			),
		),
	)
	monkeypatch.setattr(runner, "run", lambda *args, **kwargs: None)
	monkeypatch.setattr(
		runner, "temporary_directory", lambda root: nullcontext("test-temporary-directory")
	)

	verdict = runner.check(root)

	assert any(
		result.check_id == "python.component_1.runtime" and result.status is runner.Status.UNCHECKED
		for result in verdict.results
	)
	assert any(
		result.check_id == "python.component_2.ruff" and result.status is runner.Status.PASSED
		for result in verdict.results
	)
