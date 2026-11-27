"""The existing required check includes independent merge-candidate pytest evidence."""

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = "github.event_name == 'pull_request' && github.event.pull_request.base.ref == github.event.repository.default_branch"


def _job(name: str) -> str:
	workflow = (ROOT / ".github/workflows/quality.yml").read_text(encoding="utf-8")
	match = re.search(rf"^  {name}:\n(.*?)(?=^  [a-z][a-z_-]*:|\Z)", workflow, re.MULTILINE | re.DOTALL)
	assert match is not None, f"missing job {name}"
	return match[1]


def test_pytest_has_its_own_setup_and_only_runs_for_default_target_prs() -> None:
	job = _job("pytest")
	assert f"if: {REQUIRED}" in job
	assert "needs:" not in job
	assert "./.quality-gate-ci/.github/actions/prepare" in job
	assert "ref: ${{ github.sha }}" in job
	assert "quality_gate.pytest_ci" in job
	assert "Verify merge parents" in job
	assert "Recheck current PR identity" in job
	assert "./.quality-gate-ci/.github/actions/prepare" in _job("quality-checks")
	assert "pytest_ci" not in _job("quality-checks")


@pytest.mark.parametrize(
	("quality", "tests", "required", "passed"),
	[
		("success", "success", "true", True),
		("success", "failure", "true", False),
		("success", "cancelled", "true", False),
		("success", "skipped", "true", False),
		("failure", "success", "true", False),
		("cancelled", "success", "true", False),
		("skipped", "success", "true", False),
		("success", "skipped", "false", True),
	],
)
def test_required_gate_fails_closed_for_missing_or_failed_tests(
	quality: str, tests: str, required: str, passed: bool,
) -> None:
	job = _job("quality-gate")
	assert "name: Quality Gate" in job
	assert "needs: [quality-checks, pytest]" in job
	assert "always()" in job
	assert REQUIRED in job
	assert "needs.pytest.result" in job
	assert "needs.quality-checks.result" in job
	job = job[job.index("      - name: Require complete quality and applicable test results"):]
	match = re.search(r"run: \|\n(.*)", job, re.DOTALL)
	assert match is not None
	script = textwrap.dedent(match[1])
	result = subprocess.run(
		[sys.executable, "-c", script], capture_output=True, timeout=10, check=False,
		env={**os.environ, "QUALITY_RESULT": quality, "TEST_RESULT": tests, "TESTS_REQUIRED": required},
	)
	assert (result.returncode == 0) is passed, result.stderr


@pytest.mark.parametrize("changed", ["state", "base_ref", "base_sha", "head_sha", "missing"])
def test_stale_or_incomplete_pr_identity_is_rejected(changed: str) -> None:
	from quality_gate.ci_candidate import validate_pull

	pull = {"state": "open", "base": {"ref": "main", "sha": "base"}, "head": {"sha": "head"}}
	validate_pull(pull, "main", "base", "head")
	if changed == "state":
		pull["state"] = "closed"
	elif changed == "missing":
		pull = {}
	else:
		section, key = changed.split("_")
		pull[section][key] = "changed"
	with pytest.raises(ValueError, match="fresh complete check"):
		validate_pull(pull, "main", "base", "head")


@pytest.mark.parametrize("version", ["4.8.0", "4.9.0", "5.0.0"])
def test_parity_expectations_follow_executing_engine_not_the_policy_pin(version: str) -> None:
	from quality_gate.ci_parity import expected_statuses

	checks = expected_statuses(version)
	assert checks["python.component_1.ruff"] == "passed"
	assert checks["secrets.history"] == "failed"
	assert ("python.component_1.pytest" in checks) is (version != "5.0.0")
	assert ("python.component_1.coverage" in checks) is (version != "5.0.0")
