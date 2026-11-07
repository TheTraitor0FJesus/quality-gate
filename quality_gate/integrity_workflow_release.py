"""Validate shared release and image-writer workflow routes."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from .contracts import Finding
from .integrity_workflow_common import _workflow_finding
from .integrity_workflow_parser import (
	_JOB_LEVEL,
	_PROPERTY_LEVEL,
	_needs_tokens,
	_unquote_scalar,
	_Workflow,
	_WorkflowJob,
)
from .publication_config import validate_config

_QUALITY_GATE_PROVIDER = "TheTraitor0FJesus/quality-gate"
_RELEASE_READ_PERMISSIONS = {
	"contents": "read",
	"pull-requests": "read",
	"actions": "read",
	"packages": "read",
}


def _permission_map(job: _WorkflowJob) -> dict[str | None, str]:
	return {scope: value.casefold() for scope, value in job.permissions}


def _input_map(job: _WorkflowJob) -> dict[str, str]:
	return dict(job.inputs)


_RELEASE_PR_INPUT = "${{ format('{0}', github.event.pull_request.number || inputs.pr_number) }}"
_PREPARED_CANDIDATE_INPUT = "${{ needs.prepare.outputs.candidate-id }}"
_RELEASE_PR_INPUTS = {"pr-number": _RELEASE_PR_INPUT}
_PREPARED_CANDIDATE_INPUTS = {
	"pr-number": _RELEASE_PR_INPUT,
	"candidate-id": _PREPARED_CANDIDATE_INPUT,
}


def _provider_internal_write_workflow(workflow: _Workflow, is_quality_gate_provider: bool) -> bool:
	return is_quality_gate_provider and workflow.path in {
		".github/workflows/release-publish.yml",
		".github/workflows/image-writer.yml",
	}


def _provider_internal_permission_findings(workflow: _Workflow) -> list[Finding]:
	"""Check the shared reusable jobs that receive the caller's scoped write token."""
	writer = workflow.path.endswith("/image-writer.yml")
	job_id = "push-images" if writer else "publish"
	write_scope = "packages" if writer else "contents"
	expected_permissions = {
		"contents": "read",
		"pull-requests": "read",
		"actions": "read",
		"packages": "read",
	}
	expected_permissions[write_scope] = "write"
	jobs = {job.id: job for job in workflow.jobs}
	job = jobs.get(job_id)
	if (
		workflow.events != {"workflow_call"}
		or set(jobs) != {job_id}
		or job is None
		or (writer and job.name != "Push images")
		or _permission_map(job) != expected_permissions
	):
		return [
			_workflow_finding(
				workflow.path,
				"shared reusable writer permissions or call boundary are invalid",
				"keep one workflow_call job with only its required job-scoped write permission",
			)
		]
	return []


def _full_shared_reference(job: _WorkflowJob, filename: str) -> str | None:
	pattern = re.compile(
		rf"{re.escape(_QUALITY_GATE_PROVIDER)}/\.github/workflows/{re.escape(filename)}@([0-9a-f]{{40}})"
	)
	match = pattern.fullmatch(job.uses or "")
	return match.group(1) if match else None


def _pull_request_section(on_text: str) -> list[str] | None:
	lines = on_text.splitlines()
	start = next(
		(index for index, line in enumerate(lines) if re.fullmatch(r"  pull_request:\s*", line)),
		None,
	)
	if start is None:
		return None
	section: list[str] = []
	for line in lines[start + 1 :]:
		indent = len(line) - len(line.lstrip(" "))
		if line and indent <= _JOB_LEVEL:
			break
		section.append(line)
	return section


def _listed_pull_request_branches(section: list[str], branch_index: int) -> tuple[str, ...] | None:
	branches: list[str] = []
	for line in section[branch_index + 1 :]:
		indent = len(line) - len(line.lstrip(" "))
		if line and indent <= _PROPERTY_LEVEL:
			break
		if not line:
			continue
		match = re.fullmatch(r"\s{6}-\s*['\"]?([^'\"\s]+)['\"]?\s*", line)
		if match is None:
			return None
		branches.append(match.group(1))
	return tuple(branches)


def _pull_request_branches(on_text: str) -> tuple[str, ...] | None:
	section = _pull_request_section(on_text)
	if section is None:
		return None
	if any(re.match(r"^    branches-ignore\s*:", line) for line in section):
		return None
	indices = [index for index, line in enumerate(section) if re.match(r"^    branches\s*:", line)]
	if len(indices) != 1:
		return None
	branch_line = section[indices[0]]
	inline = re.fullmatch(r"\s*branches\s*:\s*\[(.*)\]\s*", branch_line)
	if inline:
		return tuple(_unquote_scalar(value.strip()) for value in inline.group(1).split(","))
	if re.fullmatch(r"\s*branches\s*:\s*", branch_line) is None:
		return None
	return _listed_pull_request_branches(section, indices[0])


def _has_default_pull_request_filter(on_text: str) -> bool:
	return _pull_request_branches(on_text) in {("main",), ("master",)}


def _release_route_trigger_is_approved(workflow: _Workflow) -> bool:
	return (
		workflow.path == ".github/workflows/release.yml"
		and workflow.events == {"pull_request", "workflow_dispatch"}
		and re.search(r"(?m)^    types: \[closed\]$", workflow.on_text) is not None
		and _has_default_pull_request_filter(workflow.on_text)
		and not any(event in workflow.events for event in {"pull_request_target", "workflow_run"})
	)


def _release_route_jobs(
	workflow: _Workflow,
) -> tuple[_WorkflowJob, _WorkflowJob, _WorkflowJob | None] | None:
	jobs = {job.id: job for job in workflow.jobs}
	prepare = jobs.get("prepare")
	publish = jobs.get("publish")
	writer = jobs.get("write-image")
	publish_needs = _needs_tokens(publish) if publish is not None else None
	writer_needs = _needs_tokens(writer) if writer is not None else None
	if (
		prepare is None
		or publish is None
		or not prepare.is_reusable
		or prepare.has_steps
		or prepare.condition
		!= "github.event_name == 'workflow_dispatch' || github.event.pull_request.merged == true"
		or not publish.is_reusable
		or publish.has_steps
		or publish.condition != "needs.prepare.outputs.status == 'build-required'"
		or publish_needs is None
		or "prepare" not in publish_needs
		or "build-and-verify" not in publish_needs
		or (
			writer is not None
			and (
				not writer.is_reusable
				or writer.name != "Write image"
				or writer.has_steps
				or writer.condition != "needs.prepare.outputs.status == 'build-required'"
				or writer.has_matrix
				or writer_needs is None
				or "prepare" not in writer_needs
				or "build-and-verify" not in writer_needs
				or "write-image" not in publish_needs
			)
		)
	):
		return None
	return prepare, publish, writer


def _configured_image_producers(root: Path, workflow: _Workflow) -> list[dict[str, object]] | None:
	config_path = root / ".release" / "publisher.toml"
	try:
		if config_path.parent.is_symlink() or config_path.is_symlink() or not config_path.is_file():
			raise ValueError("publisher configuration is missing or is not a regular file")
		if config_path.stat().st_size > 1024 * 1024:
			raise ValueError("publisher configuration exceeds 1 MiB")
		content = config_path.read_bytes()
		if len(content) > 1024 * 1024:
			raise ValueError("publisher configuration exceeds 1 MiB")
		config = tomllib.loads(content.decode("utf-8"))
		validate_config(config)
		if config.get("workflow") != workflow.path:
			raise ValueError("publisher configuration names a different workflow")
		producers = config["producers"]
		image_producers = [
			producer for producer in producers if producer.get("kind", "archive") == "image"
		]
		if not image_producers:
			raise ValueError("image writer route has no configured image producers")
	except (OSError, UnicodeError, TypeError, ValueError):
		return None
	return image_producers


def _image_producer_configuration_finding(workflow: _Workflow) -> Finding:
	return _workflow_finding(
		workflow.path,
		"image writer route has no valid matching publisher configuration",
		"declare the release workflow and every image producer in .release/publisher.toml",
	)


def _resolve_image_producer_job(
	workflow: _Workflow, producer: dict[str, object]
) -> _WorkflowJob | None:
	producer_name = producer.get("job")
	if not isinstance(producer_name, str) or not producer_name.strip() or "${{" in producer_name:
		return None
	matches = [job for job in workflow.jobs if job.name == producer_name]
	if len(matches) != 1:
		return None
	job = matches[0]
	if job.name is None or "${{" in job.name or job.is_reusable or job.has_matrix:
		return None
	return job


def _image_producer_job_findings(
	workflow: _Workflow,
	writer_needs: frozenset[str] | None,
	producer: dict[str, object],
) -> list[Finding]:
	job = _resolve_image_producer_job(workflow, producer)
	if job is None:
		return [
			_workflow_finding(
				workflow.path,
				"image producer does not map to one local job with a unique literal name",
				"set each configured producer job to one unique literal local job name",
			)
		]
	findings: list[Finding] = []
	permissions = _permission_map(job)
	if (
		permissions.get("contents") != "read"
		or permissions.get("packages") != "read"
		or any(value not in {"read", "none"} for value in permissions.values())
	):
		findings.append(
			_workflow_finding(
				workflow.path,
				"image producer job lacks explicit read-only contents and packages permissions",
				"grant the producer job contents: read and packages: read "
				"without write permissions",
			)
		)
	if writer_needs is None or job.id not in writer_needs:
		findings.append(
			_workflow_finding(
				workflow.path,
				"image writer does not directly depend on every configured image producer",
				"add each producer job ID as a direct write-image needs entry",
			)
		)
	return findings


def _image_producer_findings(root: Path, workflow: _Workflow) -> list[Finding]:
	"""Bind every configured image producer to a static local job before writer dispatch."""
	jobs = {job.id: job for job in workflow.jobs}
	writer = jobs.get("write-image")
	if (
		workflow.path != ".github/workflows/release.yml"
		or writer is None
		or _full_shared_reference(writer, "image-writer.yml") is None
	):
		return []
	image_producers = _configured_image_producers(root, workflow)
	if image_producers is None:
		return [_image_producer_configuration_finding(workflow)]
	writer_needs = _needs_tokens(writer)
	return [
		finding
		for producer in image_producers
		for finding in _image_producer_job_findings(workflow, writer_needs, producer)
	]


def _provider_release_route_is_approved(
	prepare: _WorkflowJob, publish: _WorkflowJob, writer: _WorkflowJob | None
) -> bool:
	return (
		prepare.uses == "./.github/workflows/release-prepare.yml"
		and _input_map(prepare) == _RELEASE_PR_INPUTS
		and publish.uses == "./.github/workflows/release-publish.yml"
		and _input_map(publish) == _PREPARED_CANDIDATE_INPUTS
		and writer is None
	)


def _consumer_release_route_is_approved(
	prepare: _WorkflowJob, publish: _WorkflowJob, writer: _WorkflowJob | None
) -> bool:
	if _input_map(prepare) != _RELEASE_PR_INPUTS:
		return False
	if _input_map(publish) != _PREPARED_CANDIDATE_INPUTS:
		return False
	prepare_sha = _full_shared_reference(prepare, "release-prepare.yml")
	publish_sha = _full_shared_reference(publish, "release-publish.yml")
	if prepare_sha is None or publish_sha != prepare_sha:
		return False
	if writer is not None and (
		_full_shared_reference(writer, "image-writer.yml") != prepare_sha
		or _input_map(writer) != _PREPARED_CANDIDATE_INPUTS
	):
		return False
	return True


def _approved_release_writer_route(workflow: _Workflow, is_quality_gate_provider: bool) -> bool:
	"""Allow only the merged/recovery dispatcher to call the shared publisher and image writer."""
	if not _release_route_trigger_is_approved(workflow):
		return False
	route_jobs = _release_route_jobs(workflow)
	if route_jobs is None:
		return False
	prepare, publish, writer = route_jobs
	if is_quality_gate_provider:
		return _provider_release_route_is_approved(prepare, publish, writer)
	return _consumer_release_route_is_approved(prepare, publish, writer)


def _release_writer_permission_findings(
	workflow: _Workflow,
	write_permissions: list[tuple[str | None, str]],
	is_quality_gate_provider: bool,
) -> list[Finding]:
	"""Enforce the exact reusable jobs and their complete job-scoped token permissions."""
	findings: list[Finding] = []
	jobs = {job.id: job for job in workflow.jobs}
	allowed = {
		("contents", "publish"),
		*([("packages", "write-image")] if "write-image" in jobs else []),
	}
	actual = {(scope, job_id) for scope, job_id in write_permissions}
	if actual != allowed:
		findings.append(
			_workflow_finding(
				workflow.path,
				"release writes are not limited to the shared publisher and dedicated image "
				"writer calls",
				"grant contents: write only to publish and packages: write only to write-image",
			)
		)
	for scope, job_id, provider_workflow in (
		("contents", "publish", "release-publish.yml"),
		*([("packages", "write-image", "image-writer.yml")] if "write-image" in jobs else []),
	):
		job = jobs.get(job_id)
		valid_reference = (
			job is not None
			and job.is_reusable
			and not job.has_steps
			and (
				job.uses == f"./.github/workflows/{provider_workflow}"
				if (
					is_quality_gate_provider
					and workflow.path == ".github/workflows/release.yml"
					and provider_workflow == "release-publish.yml"
				)
				else _full_shared_reference(job, provider_workflow) is not None
			)
		)
		permissions = _permission_map(job) if job is not None else {}
		needs = _needs_tokens(job) if job is not None else None
		expected = {**_RELEASE_READ_PERMISSIONS, scope: "write"}
		if (
			not valid_reference
			or permissions != expected
			or job is None
			or job.condition != "needs.prepare.outputs.status == 'build-required'"
			or needs is None
			or "prepare" not in needs
			or "build-and-verify" not in needs
		):
			findings.append(
				_workflow_finding(
					workflow.path,
					f"{scope}: write is not scoped to the exact shared {provider_workflow} call",
					"use the full pinned shared workflow call with only the required "
					"job permissions",
				)
			)
	return findings
