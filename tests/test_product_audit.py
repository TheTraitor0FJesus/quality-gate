"""Separate product and release audits keep their distinct obligations."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from quality_gate import runner
from quality_gate.contracts import CheckResult, Status, load_manifest


@pytest.mark.parametrize(("mode", "complete"), [("product", False), ("audit", True)])
def test_product_and_release_audits_scan_full_history_with_distinct_lesson_readiness(
	monkeypatch: pytest.MonkeyPatch, mode: str, complete: bool
) -> None:
	root = Path(__file__).parent / "fixtures" / "valid"
	manifest = load_manifest(root)
	seen: list[object] = []

	def passed(check_id: str) -> CheckResult:
		return CheckResult(check_id, Status.PASSED, "passed")

	monkeypatch.setattr(runner, "required_documents_result", lambda *_: passed("documents"))
	monkeypatch.setattr(runner, "git_integrity_results", lambda *_, **__: [])
	monkeypatch.setattr(runner, "workflow_result", lambda *_, **__: passed("workflow"))
	monkeypatch.setattr(runner, "documentation_results", lambda *_, **__: [])
	monkeypatch.setattr(runner, "web_budget_results", lambda *_, **__: [])
	monkeypatch.setattr(
		runner,
		"lessons_result",
		lambda *_, **kw: seen.append(kw["is_complete_required"]) or passed("lessons"),
	)
	monkeypatch.setattr(runner, "secret_candidate_result", lambda *_, **__: passed("candidate"))
	monkeypatch.setattr(
		runner,
		"secret_audit_result",
		lambda *_, **__: seen.append("full-history") or passed("audit"),
	)
	monkeypatch.setattr(
		runner, "secret_history_result", lambda *_, **__: pytest.fail("range-only history")
	)
	monkeypatch.setattr(runner, "_ci_history_refs", lambda *_, **__: (None, None))
	results = runner._OperationResults(root)
	runner._append_snapshot_baseline_checks(
		root, manifest, results, repository_root=root, index_file=None, mode=mode
	)
	runner._append_snapshot_secret_checks(
		root,
		manifest,
		SimpleNamespace(),
		results,
		repository_root=root,
		base=None,
		head=None,
		mode=mode,
	)
	assert seen == [complete, "full-history"]
