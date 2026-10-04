from __future__ import annotations

from pathlib import Path

import pytest
from quality_gate.contracts import CheckResult, load_manifest
from quality_gate.integrity import workflow_result

FIXTURES = Path(__file__).parent / "fixtures"

APPROVED_WORKFLOW = """name: Dependabot review

on:
  pull_request_target:
    branches: [main]
    types: [opened, reopened, ready_for_review]

permissions:
  contents: read

concurrency:
  group: dependabot-review-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  request-review:
    if: >-
      github.event.pull_request.user.login == 'dependabot[bot]' &&
      github.event.pull_request.user.type == 'Bot' &&
      !github.event.pull_request.draft
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    permissions:
      pull-requests: write
    steps:
      - name: Request review from the repository owner
        shell: bash
        env:
          GH_TOKEN: ${{ github.token }}
          GH_REPO: ${{ github.repository }}
          PR_NUMBER: ${{ github.event.pull_request.number }}
          REVIEWER: ${{ github.repository_owner }}
        run: |
          reviewers=$(gh pr view "$PR_NUMBER" --repo "$GH_REPO" \\
            --json reviewRequests,reviews \\
            --jq '[.reviewRequests[].login, .reviews[].author.login] | unique | .[]')
          if grep -Fx "$REVIEWER" <<< "$reviewers" >/dev/null; then
            exit 0
          fi
          gh api --method POST "repos/$GH_REPO/pulls/$PR_NUMBER/requested_reviewers" \\
            -f "reviewers[]=$REVIEWER" --silent
"""


def _workflow_result(
	tmp_path: Path, workflow_text: str, filename: str = "dependabot-review.yml"
) -> CheckResult:
	workflows = tmp_path / ".github" / "workflows"
	workflows.mkdir(parents=True)
	(workflows / "quality.yml").write_text(
		"""name: Quality gate
on:
  pull_request:
  push:
permissions: read
concurrency:
  group: quality-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true
jobs:
  quality-gate:
    name: quality-gate
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@0123456789abcdef0123456789abcdef01234567
""",
		encoding="utf-8",
	)
	(workflows / filename).write_text(workflow_text, encoding="utf-8", newline="")
	return workflow_result(tmp_path, load_manifest(FIXTURES / "no-python"))


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_workflow_hygiene_accepts_exact_dependabot_review_route(
	tmp_path: Path, newline: str
) -> None:
	result = _workflow_result(tmp_path, APPROVED_WORKFLOW.replace("\n", newline))

	assert result.status.value == "passed"


@pytest.mark.parametrize("filename", ["other-review.yml", "dependabot-review.yaml"])
def test_workflow_hygiene_rejects_review_template_at_another_path(
	tmp_path: Path, filename: str
) -> None:
	result = _workflow_result(tmp_path, APPROVED_WORKFLOW, filename)

	assert result.status.value == "failed"
	assert any(
		finding.path == f".github/workflows/{filename}"
		and finding.message == "write-capable jobs are reachable from pull requests"
		for finding in result.findings
	)


@pytest.mark.parametrize(
	"workflow_text",
	[
		APPROVED_WORKFLOW.replace(
			"          GH_TOKEN: ${{ github.token }}\n",
			"          GH_TOKEN: ${{ github.token }}\n"
			"          UNTRUSTED: ${{ github.event.pull_request.title }}\n",
		),
		APPROVED_WORKFLOW.replace(
			"          REVIEWER: ${{ github.repository_owner }}",
			"          REVIEWER: attacker",
		),
		APPROVED_WORKFLOW.replace("  contents: read", "  contents: write"),
		APPROVED_WORKFLOW.replace("      pull-requests: write", "      contents: write"),
		APPROVED_WORKFLOW.replace(
			"      pull-requests: write", "      pull-requests: write\n      packages: write"
		),
		APPROVED_WORKFLOW.replace("branches: [main]", "branches: [other]"),
		APPROVED_WORKFLOW.replace("timeout-minutes: 5", "timeout-minutes: 60"),
		APPROVED_WORKFLOW.replace("cancel-in-progress: true", "cancel-in-progress: false"),
		APPROVED_WORKFLOW.replace(
			"    runs-on: ubuntu-24.04", "    environment: privileged\n    runs-on: ubuntu-24.04"
		),
		APPROVED_WORKFLOW.replace(
			"GH_TOKEN: ${{ github.token }}", "GH_TOKEN: ${{ secrets.REVIEW_TOKEN }}"
		),
		APPROVED_WORKFLOW.replace(
			'          if grep -Fx "$REVIEWER" <<< "$reviewers" >/dev/null; then',
			"          if false; then",
		),
		APPROVED_WORKFLOW.replace("!github.event.pull_request.draft", "true"),
		APPROVED_WORKFLOW.replace("pull_request_target:", "pull_request:"),
		APPROVED_WORKFLOW.replace(
			"types: [opened, reopened, ready_for_review]", "types: [opened, synchronize]"
		),
		APPROVED_WORKFLOW.replace("'dependabot[bot]'", "'attacker'"),
		APPROVED_WORKFLOW.replace("user.type == 'Bot'", "user.type == 'User'"),
		APPROVED_WORKFLOW.replace(
			"reviewers=$(gh pr view",
			"reviewers=$(gh api --method POST",
		),
		APPROVED_WORKFLOW.replace(
			"      - name: Request review from the repository owner\n",
			"      - uses: actions/checkout@0123456789abcdef0123456789abcdef01234567\n"
			"      - name: Request review from the repository owner\n",
		),
		APPROVED_WORKFLOW.replace(
			"          gh api --method POST",
			'          echo "${{ github.event.pull_request.title }}"\n'
			"          gh api --method POST",
		),
	],
)
def test_workflow_hygiene_rejects_changes_to_dependabot_review_route(
	tmp_path: Path, workflow_text: str
) -> None:
	result = _workflow_result(tmp_path, workflow_text)

	assert result.status.value == "failed"
	assert any(
		finding.path == ".github/workflows/dependabot-review.yml"
		and finding.message == "write-capable jobs are reachable from pull requests"
		for finding in result.findings
	)
