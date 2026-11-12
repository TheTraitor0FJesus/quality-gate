"""Check repository workflow triggers and permissions."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .contracts import Finding
from .integrity_workflow_common import _workflow_finding
from .integrity_workflow_parser import _unquote_scalar, _Workflow
from .integrity_workflow_release import (
	_approved_release_writer_route,
	_image_producer_findings,
	_provider_internal_permission_findings,
	_provider_internal_write_workflow,
	_release_writer_permission_findings,
)

_SHA = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_DEFAULT_BRANCHES = {"main", "master"}


def _workflow_paths(root: Path) -> list[Path]:
	return sorted(
		path
		for path in root.glob(".github/workflows/*")
		if not path.is_symlink() and path.is_file() and path.suffix.casefold() in {".yml", ".yaml"}
	)


def _dependabot_app_findings(workflow: _Workflow, is_quality_gate_provider: bool) -> list[Finding]:
	if workflow.path == _DEPENDABOT_DECISION_PATH or (
		"pull_request_target" in workflow.events
		and "QUALITY_GATE_ROLLOUT_APP_PRIVATE_KEY" in workflow.source
	):
		fingerprint = hashlib.sha256(workflow.source.rstrip("\r\n").encode("utf-8")).hexdigest()
		if (
			not is_quality_gate_provider
			or workflow.path != _DEPENDABOT_DECISION_PATH
			or fingerprint != _DEPENDABOT_DECISION_FINGERPRINT
		):
			return [
				_workflow_finding(
					workflow.path,
					"Dependabot decision App workflow differs from the approved template",
					"restore the exact trusted metadata-only workflow",
				)
			]
	return []


def _single_workflow_findings(workflow: _Workflow, is_quality_gate_provider: bool) -> list[Finding]:
	findings = _workflow_reference_findings(workflow)
	findings.extend(_dependabot_app_findings(workflow, is_quality_gate_provider))
	is_pull_request = bool({"pull_request", "pull_request_target"} & workflow.events)
	findings.extend(
		_workflow_permission_findings(workflow, is_pull_request, is_quality_gate_provider)
	)
	if workflow.concurrency_group is None or workflow.concurrency_cancel is None:
		findings.append(
			_workflow_finding(
				workflow.path,
				"workflow does not declare explicit concurrency cancellation behavior",
				"declare concurrency.cancel-in-progress explicitly",
			)
		)
	elif is_pull_request and workflow.concurrency_cancel != "true":
		findings.append(
			_workflow_finding(
				workflow.path,
				"pull-request concurrency does not cancel superseded runs",
				"set concurrency.cancel-in-progress to true",
			)
		)
	for job in workflow.jobs:
		if not job.is_reusable and (
			job.timeout is None or not job.timeout.isdigit() or int(job.timeout) <= 0
		):
			findings.append(
				_workflow_finding(
					workflow.path,
					f"job {job.id!r} has no positive timeout-minutes",
					"declare a bounded job timeout",
				)
			)
	if workflow.path == ".github/workflows/release.yml" and any(
		job.id == "write-image" and job.has_matrix for job in workflow.jobs
	):
		findings.append(
			_workflow_finding(
				workflow.path,
				"release image writer job declares a matrix",
				"remove the matrix so one approved writer job makes one package write call",
			)
		)
	return findings


_DEPENDABOT_DECISION_PATH = ".github/workflows/dependabot-release-decision.yml"
_DEPENDABOT_DECISION_FINGERPRINT = (
	"b004e8341772415f2c0b225ff70e06cb35eaa16ac8d25d159669d88f9ae22499"
)


def _workflow_permission_findings(
	workflow: _Workflow, is_pull_request: bool, is_quality_gate_provider: bool
) -> list[Finding]:
	findings: list[Finding] = []
	metadata_review = _approved_dependabot_review_workflow(workflow)
	dependabot_auto_merge = _approved_dependabot_auto_merge_workflow(workflow)
	if not workflow.permissions:
		findings.append(
			_workflow_finding(
				workflow.path,
				"workflow does not declare top-level permissions",
				"declare the minimum required read permissions",
			)
		)
	elif any(
		value.casefold() not in {"", "read", "none"} for _scope, value in workflow.permissions
	):
		findings.append(
			_workflow_finding(
				workflow.path,
				"workflow permissions are broader than read-only",
				"reduce permissions to the minimum required values",
			)
		)
	write_permissions = [
		(scope, job.id)
		for job in workflow.jobs
		for scope, value in job.permissions
		if value.casefold() not in {"", "read", "none"}
	]
	provider_internal = _provider_internal_write_workflow(workflow, is_quality_gate_provider)
	if (
		write_permissions
		and is_pull_request
		and not _approved_release_writer_route(workflow, is_quality_gate_provider)
		and not provider_internal
		and not metadata_review
		and not dependabot_auto_merge
	):
		findings.append(
			_workflow_finding(
				workflow.path,
				"write-capable jobs are reachable from pull requests",
				"remove write permissions or isolate the jobs in a deployment-only workflow",
			)
		)
	elif (
		write_permissions
		and not _approved_release_writer_route(workflow, is_quality_gate_provider)
		and not provider_internal
		and not metadata_review
		and not dependabot_auto_merge
	):
		unrelated_scopes = sorted(
			{scope or "write-all" for scope, _job_id in write_permissions}
			- {"contents", "packages"}
		)
		if unrelated_scopes:
			findings.append(
				_workflow_finding(
					workflow.path,
					"deployment job uses an unrelated write permission",
					"limit job-level writes to contents and packages",
				)
			)
		if not _is_deployment_only(workflow):
			findings.append(
				_workflow_finding(
					workflow.path,
					"write-capable workflow is not restricted to deployment triggers",
					"limit it to default-branch pushes and/or workflow_dispatch",
				)
			)
	if write_permissions and _approved_release_writer_route(workflow, is_quality_gate_provider):
		findings.extend(
			_release_writer_permission_findings(
				workflow, write_permissions, is_quality_gate_provider
			)
		)
	if write_permissions and provider_internal:
		findings.extend(_provider_internal_permission_findings(workflow))
	return findings


_DEPENDABOT_REVIEW_WORKFLOW = """name: Dependabot review

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


def _approved_dependabot_review_workflow(workflow: _Workflow) -> bool:
	"""Allow only the exact metadata-only Dependabot owner-review workflow template."""
	return workflow.path == ".github/workflows/dependabot-review.yml" and (
		workflow.source.rstrip("\r\n") == _DEPENDABOT_REVIEW_WORKFLOW.rstrip("\r\n")
	)


_DEPENDABOT_AUTO_MERGE_WORKFLOW_PATH = ".github/workflows/dependabot-auto-merge.yml"

_DEPENDABOT_AUTO_MERGE_WORKFLOW = """name: Dependabot patch auto-merge

on:
  pull_request_target:
    types: [opened, synchronize, reopened, ready_for_review]

permissions:
  contents: read

concurrency:
  group: dependabot-auto-merge-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  enable-patch-auto-merge:
    if: >-
      github.event.pull_request.user.login == 'dependabot[bot]' &&
      github.event.pull_request.base.ref == 'main' &&
      github.event.pull_request.draft == false
    runs-on: ubuntu-latest
    timeout-minutes: 10
    permissions:
      contents: write
      pull-requests: write
    steps:
      - name: Read Dependabot update metadata
        id: metadata
        uses: dependabot/fetch-metadata@25dd0e34f4fe68f24cc83900b1fe3fe149efef98 # v3.1.0
        with:
          alert-lookup: true
          github-token: ${{ secrets.DEPENDABOT_ALERTS_TOKEN }}

      - name: Check every grouped update for security alerts
        id: security
        env:
          UPDATED_DEPENDENCIES: ${{ steps.metadata.outputs.updated-dependencies-json }}
        run: |
          if jq --exit-status '
            type == "array"
            and length > 0
            and all(.[]; type == "object" and has("ghsaId") and .ghsaId == "")
          ' <<< "$UPDATED_DEPENDENCIES" > /dev/null; then
            echo "security-free=true" >> "$GITHUB_OUTPUT"
          else
            echo "security-free=false" >> "$GITHUB_OUTPUT"
          fi

      - name: Enable auto-merge for patch version updates
        if: >-
          steps.metadata.outputs.update-type == 'version-update:semver-patch' &&
          steps.security.outputs.security-free == 'true'
        env:
          GH_TOKEN: ${{ github.token }}
          GH_REPO: ${{ github.repository }}
          PULL_REQUEST_URL: ${{ github.event.pull_request.html_url }}
        run: gh pr merge --auto --squash "$PULL_REQUEST_URL"
"""


def _approved_dependabot_auto_merge_workflow(workflow: _Workflow) -> bool:
	"""Allow only the exact Dependabot patch auto-merge workflow template."""
	normalized_source = workflow.source.replace("\r\n", "\n")
	return workflow.path == _DEPENDABOT_AUTO_MERGE_WORKFLOW_PATH and (
		normalized_source == _DEPENDABOT_AUTO_MERGE_WORKFLOW
	)


def _workflow_findings(
	root: Path, workflows: list[_Workflow], is_quality_gate_provider: bool
) -> list[Finding]:
	findings = [
		finding
		for workflow in workflows
		for finding in _single_workflow_findings(workflow, is_quality_gate_provider)
	]
	findings.extend(
		finding for workflow in workflows for finding in _image_producer_findings(root, workflow)
	)
	quality_jobs = [
		(job, workflow)
		for workflow in workflows
		for job in workflow.jobs
		if job.id.casefold() == "quality-gate" or (job.name or "").casefold() == "quality gate"
	]
	if len(quality_jobs) != 1:
		findings.append(
			_workflow_finding(
				".github/workflows",
				"workflow must expose exactly one stable Quality Gate job identity",
				"keep one job named quality-gate",
			)
		)
	elif "pull_request" not in quality_jobs[0][1].events:
		findings.append(
			_workflow_finding(
				quality_jobs[0][1].path,
				"Quality Gate job is not reachable from pull requests",
				"declare the pull_request trigger on the Quality Gate workflow",
			)
		)
	if len(quality_jobs) == 1 and not _has_default_push_trigger(quality_jobs[0][1].on_text):
		findings.append(
			_workflow_finding(
				quality_jobs[0][1].path,
				"Quality Gate job is not reachable from the default branch",
				"declare the default-branch push trigger on the Quality Gate workflow",
			)
		)
	return findings


def _workflow_reference_findings(workflow: _Workflow) -> list[Finding]:
	return [
		_workflow_finding(
			workflow.path,
			"workflow reference is not pinned to a full commit SHA",
			"pin the reference to a 40-character commit SHA",
		)
		for reference in workflow.uses
		if not reference.startswith("./")
		and ("@" not in reference or not _SHA.fullmatch(reference.rsplit("@", 1)[-1]))
	]


def _has_default_push_trigger(on_text: str) -> bool:
	push_text = _push_trigger_text(on_text)
	if push_text is None:
		return False
	branches = _push_branches(push_text)
	if branches is None:
		if re.search(r"(?m)^[ ]{4}(?:branches-ignore|tags|tags-ignore)[ ]*:", push_text):
			return False
		return True
	return bool(set(branches) & _DEFAULT_BRANCHES)


def _push_trigger_text(on_text: str) -> str | None:
	match = re.search(
		r"(?ms)^[ ]{2}push[ ]*:[ ]*$.*?(?=^[ ]{2}[a-z_][a-z0-9_-]*[ ]*:|\Z)",
		on_text,
	)
	return match.group(0) if match else None


def _push_branches(push_text: str) -> tuple[str, ...] | None:
	match = re.search(r"(?m)^[ ]{4}branches[ ]*:[ ]*(?P<inline>[^\n]*)$", push_text)
	if match is None:
		return None
	inline = match.group("inline").strip()
	if inline:
		if not (inline.startswith("[") and inline.endswith("]")):
			return ()
		return tuple(
			_unquote_scalar(branch.strip()).casefold()
			for branch in inline[1:-1].split(",")
			if branch.strip()
		)
	return tuple(
		_unquote_scalar(item.group(1)).casefold()
		for item in re.finditer(r"(?m)^[ ]{6}-[ ]*([^\s#]+)", push_text[match.end() :])
	)


def _is_deployment_only(workflow: _Workflow) -> bool:
	if not workflow.events or not workflow.events <= {"push", "workflow_dispatch"}:
		return False
	if workflow.events == {"workflow_dispatch"}:
		return True
	push_text = _push_trigger_text(workflow.on_text)
	if push_text is None:
		return False
	branches = _push_branches(push_text)
	return branches is not None and bool(branches) and set(branches) <= _DEFAULT_BRANCHES
