"""Keep Gate deadline ownership and retained failures across evidence checks."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from quality_gate import operation, runner, runner_python_checks, runner_ruff
from quality_gate.contracts import CheckResult, Status


def test_format_timeout_keeps_completed_ruff_failure(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	class EvidencePlan:
		lint_complete = True
		cached: dict[str, object] = {}

		def lint_checked(self, _fallback: object) -> CheckResult:
			return CheckResult(
				"python.component_1.ruff",
				Status.FAILED,
				"observed failure",
				recovery_action="resolve the finding",
			)

		def format_checked(self, _fallback: object) -> CheckResult:
			raise operation.OperationTimeoutError("deadline during format")

	monkeypatch.setattr(runner_ruff, "prepare_ruff_evidence", lambda *args: EvidencePlan())
	component = SimpleNamespace(timeout_seconds=30)
	manifest = SimpleNamespace(repository=SimpleNamespace(command_timeout_seconds=30))
	context = SimpleNamespace(
		root=tmp_path,
		component=component,
		prepared=object(),
		component_index=1,
		manifest=manifest,
		deadline=time.monotonic() + 30,
	)
	results = runner._OperationResults(tmp_path)
	gate = operation.GateOperation(tmp_path, "check", timeout_seconds=30)
	token = operation._active.set(gate)
	commands = [
		("python.component_1.ruff", ["ruff"], 30),
		("python.component_1.format", ["ruff"], 30),
	]
	try:
		with pytest.raises(operation.OperationTimeoutError):
			runner_python_checks._run_ruff_with_evidence(
				results, context, tmp_path / "python", {}, commands
			)
	finally:
		operation._active.reset(token)
	retained = {result.check_id: result for result in gate.results}
	assert retained["python.component_1.ruff"].status is Status.FAILED
	assert retained["python.component_1.format"].status is Status.UNCHECKED


def test_unavailable_evidence_keeps_full_feature_commands(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	monkeypatch.setattr(runner_ruff, "prepare_ruff_evidence", lambda *args: None)
	context = SimpleNamespace(
		root=tmp_path,
		component=SimpleNamespace(timeout_seconds=30),
		prepared=object(),
		component_index=1,
		manifest=SimpleNamespace(repository=SimpleNamespace(command_timeout_seconds=30)),
		deadline=time.monotonic() + 30,
	)
	results = runner._OperationResults(tmp_path)
	gate = operation.GateOperation(tmp_path, "check", timeout_seconds=30)
	token = operation._active.set(gate)
	commands = [
		("python.component_1.ruff", ["ruff"], 30),
		("python.component_1.format", ["ruff"], 30),
	]
	try:
		remaining = runner_python_checks._run_ruff_with_evidence(
			results, context, tmp_path / "python", {}, commands
		)
	finally:
		operation._active.reset(token)
	assert remaining == commands and not results
