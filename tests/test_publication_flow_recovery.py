"""Related behavior tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import cast

import pytest
from publication_flow_fixture import BODY, PR_HEAD, PROVIDER, SOURCE, GitHub
from test_publication_flow import (
	RegistryGitHub,
	_event,
	_publisher,
	_ready,
)

from quality_gate.publication import PublicationError
from quality_gate.publication_flow import Context, Publisher


def test_recovery_requires_original_envelope_and_preserves_its_identity() -> None:
	api = GitHub()
	publisher, artifact_id = _ready(api)
	result = publisher.recover(7)
	assert result["candidate_artifact_id"] == artifact_id
	assert result["source_sha"] == SOURCE
	assert result["status"] == "build-required"
	assert api.writes == []
	api.pull["body"] = BODY.replace("Общая публикация", "Новая декларация")
	with pytest.raises(PublicationError, match="original"):
		publisher.recover(7)
	assert api.writes == []


def test_recovery_refuses_missing_or_ambiguous_historical_authorization() -> None:
	api = GitHub()
	with pytest.raises(PublicationError, match="original"):
		_publisher(api).recover(7)
	assert api.writes == []
	publisher, artifact_id = _ready(api)
	api.artifacts[artifact_id]["expired"] = True
	with pytest.raises(PublicationError, match=r"expired|original"):
		publisher.recover(7)
	assert api.writes == []


def test_completed_readback_survives_expired_actions_artifacts() -> None:
	api = GitHub()
	publisher, artifact_id = _ready(api)
	publisher.publish(7, artifact_id)
	api.artifacts.clear()
	api.archives.clear()
	before = copy.deepcopy((api.writes, api.releases, api.tags))
	assert publisher.recover(7)["status"] == "already-complete"
	assert before == (api.writes, api.releases, api.tags)


def test_original_event_rerun_reuses_the_original_candidate_envelope() -> None:
	api = GitHub()
	_, artifact_id = _ready(api)
	api.run_overrides["run_attempt"] = 2
	publisher = Publisher(
		api,
		Context(
			repository="o/r",
			provider_repository="o/provider",
			provider_sha=PROVIDER,
			helper_sha=PROVIDER,
			run_id=100,
			attempt=2,
			run_source_sha=SOURCE,
			run_head_sha=PR_HEAD,
		),
	)
	result = publisher.prepare(7, event=_event(api))
	assert result["candidate_artifact_id"] == artifact_id
	candidate = result["candidate"]
	assert isinstance(candidate, dict)
	assert candidate["origin_attempt"] == 1


def test_completed_receipt_cannot_remove_a_required_platform() -> None:
	api = GitHub()
	publisher, artifact_id = _ready(api)
	publisher.publish(7, artifact_id)
	release = api.releases[-1]
	body = str(release["body"])
	prefix, encoded = body.split("\n<!-- shared-publisher-receipt-v1\n")
	receipt = json.loads(encoded.removesuffix("-->\n"))
	receipt["deliverables"] = receipt["deliverables"][:1]
	release["body"] = (
		prefix
		+ "\n<!-- shared-publisher-receipt-v1\n"
		+ json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
		+ "\n-->\n"
	)
	assets = release["assets"]
	assert isinstance(assets, list)
	release["assets"] = assets[:1]
	before = copy.deepcopy(api.writes)
	with pytest.raises(PublicationError):
		publisher.prepare(7, event=_event(api))
	assert api.writes == before


def test_recovery_dispatch_refuses_a_nondefault_tooling_branch() -> None:
	api = GitHub()
	publisher, _ = _ready(api)
	api.run_overrides = {"event": "workflow_dispatch", "head_branch": "unreviewed"}
	with pytest.raises(PublicationError, match="default"):
		publisher.recover(7)
	assert api.writes == []


def test_matching_draft_without_tag_resumes_after_tag_creation() -> None:
	api = GitHub()
	publisher, artifact_id = _ready(api)
	api.fail_upload = "package-windows-2.1.0.zip"
	with pytest.raises(PublicationError, match="interrupted"):
		publisher.publish(7, artifact_id)
	del api.tags["v2.1.0"]
	api.fail_upload = ""
	assert publisher.publish(7, artifact_id)["status"] == "published"
	assert api.tags["v2.1.0"] == SOURCE
	assert api.writes.count("package-linux-2.1.0.zip") == 1


@pytest.mark.parametrize("latest_failure", [False, True])
def test_failed_jobs_rerun_uses_only_latest_successful_platform(latest_failure: bool) -> None:
	api = GitHub()
	_, artifact_id = _ready(api)
	api.run_overrides["run_attempt"] = 2
	if latest_failure:
		failed = {**api.jobs[-1], "id": 999, "run_attempt": 2, "conclusion": "failure"}
		api.jobs.append(failed)
	publisher = Publisher(
		api,
		Context(
			repository="o/r",
			provider_repository="o/provider",
			provider_sha=PROVIDER,
			helper_sha=PROVIDER,
			run_id=100,
			attempt=2,
			run_source_sha=SOURCE,
			run_head_sha=PR_HEAD,
		),
	)
	if latest_failure:
		with pytest.raises(PublicationError):
			publisher.publish(7, artifact_id)
		assert api.writes == []
	else:
		assert publisher.publish(7, artifact_id)["status"] == "published"


def test_rerun_accepts_reused_successful_jobs_with_their_attempt_one_evidence() -> None:
	api = GitHub()
	_, candidate_artifact_id = _ready(api, evidence_wait_seconds=0.05, evidence_poll_seconds=0.001)
	api.run_overrides["run_attempt"] = 2
	for job in api.jobs:
		if job.get("name") in {"Build linux", "Build windows"}:
			job["run_attempt"] = 2
	publisher = _publisher(api, attempt=2, evidence_wait_seconds=0.05, evidence_poll_seconds=0.001)

	assert publisher.publish(7, candidate_artifact_id)["status"] == "published"
	assert '"artifact_attempt":1' in str(api.releases[-1]["body"])
	assert '"attempt":1' in str(api.releases[-1]["body"])
	assert '"latest_attempt":2' in str(api.releases[-1]["body"])


def test_rerun_accepts_reused_job_with_a_new_github_job_id() -> None:
	api = GitHub()
	_, candidate_artifact_id = _ready(api, evidence_wait_seconds=0.05, evidence_poll_seconds=0.001)
	api.run_overrides["run_attempt"] = 2
	for job in api.jobs:
		if job.get("name") in {"Build linux", "Build windows"}:
			job["id"] = cast(int, job["id"]) + 1000
			job["run_attempt"] = 2
	publisher = _publisher(api, attempt=2, evidence_wait_seconds=0.05, evidence_poll_seconds=0.001)

	assert publisher.publish(7, candidate_artifact_id)["status"] == "published"
	assert '"artifact_attempt":1' in str(api.releases[-1]["body"])


@pytest.mark.parametrize("runner_id", [None, 4321])
def test_rerun_rejects_new_job_id_without_matching_runner(runner_id: int | None) -> None:
	api = GitHub()
	_, candidate_artifact_id = _ready(api, evidence_wait_seconds=0.05, evidence_poll_seconds=0.001)
	api.run_overrides["run_attempt"] = 2
	for job in api.jobs:
		if job.get("name") == "Build linux":
			job.update({"id": 999, "run_attempt": 2, "runner_id": runner_id})
	publisher = _publisher(api, attempt=2, evidence_wait_seconds=0.05, evidence_poll_seconds=0.001)

	with pytest.raises(PublicationError, match="evidence artifact for producer linux attempt 2"):
		publisher.publish(7, candidate_artifact_id)
	assert api.writes == []


@pytest.mark.parametrize(
	"incomplete_field",
	[
		"jobs",
		"job_steps",
		"job_conclusion",
		"artifacts",
		"artifact_metadata",
		"run_metadata",
	],
)
def test_temporary_incomplete_actions_data_is_retried_before_provenance(
	incomplete_field: str,
) -> None:
	api = GitHub()
	publisher, candidate_artifact_id = _ready(
		api, evidence_wait_seconds=0.05, evidence_poll_seconds=0.001
	)
	if incomplete_field in {"jobs", "artifacts"}:
		api.incomplete_lists[incomplete_field] = 1
	elif incomplete_field == "job_steps":
		api.incomplete_job_steps = 1
	elif incomplete_field == "job_conclusion":
		api.incomplete_job_conclusion = 1
	elif incomplete_field == "artifact_metadata":
		api.incomplete_artifact_metadata[candidate_artifact_id] = 1
	else:
		api.incomplete_run_metadata = 1

	assert publisher.publish(7, candidate_artifact_id)["status"] == "published"
	assert api.writes


def test_missing_producer_evidence_fails_clearly_without_publication_mutations() -> None:
	api = GitHub()
	publisher, candidate_artifact_id = _ready(
		api, evidence_wait_seconds=0.05, evidence_poll_seconds=0.001
	)
	linux_id = next(
		artifact_id
		for artifact_id, artifact in api.artifacts.items()
		if artifact["name"] == "release-evidence-linux-1"
	)
	del api.artifacts[linux_id]
	del api.archives[linux_id]
	before = copy.deepcopy((api.writes, api.releases, api.tags))

	with pytest.raises(PublicationError, match="required evidence artifact for producer linux"):
		publisher.publish(7, candidate_artifact_id)
	assert before == (api.writes, api.releases, api.tags)


def test_attempt_one_evidence_does_not_match_a_new_attempt_two_job() -> None:
	api = GitHub()
	_, candidate_artifact_id = _ready(api, evidence_wait_seconds=0.05, evidence_poll_seconds=0.001)
	api.run_overrides["run_attempt"] = 2
	for job in api.jobs:
		if job.get("name") in {"Build linux", "Build windows"}:
			job.update(
				{
					"run_attempt": 2,
					"started_at": "2026-09-14T10:11:00Z",
					"completed_at": "2026-09-14T10:20:00Z",
				}
			)
	publisher = _publisher(api, attempt=2, evidence_wait_seconds=0.05, evidence_poll_seconds=0.001)
	before = copy.deepcopy((api.writes, api.releases, api.tags))

	with pytest.raises(PublicationError, match="evidence artifact for producer linux attempt 2"):
		publisher.publish(7, candidate_artifact_id)
	assert before == (api.writes, api.releases, api.tags)


def test_shared_protocol_publishes_and_reads_back_image_delivery(tmp_path: Path) -> None:
	api = RegistryGitHub("image")
	publisher, artifact_id = _ready(api)
	assert publisher.write_images(7, artifact_id, tmp_path)["status"] == "images-written"
	api.artifact(
		"release-image-receipt-1",
		{"image-receipt.json": (tmp_path / "image-receipt.json").read_bytes()},
		"Write image / Push images",
		["Write images"],
	)
	assert publisher.publish(7, artifact_id)["status"] == "published"
	assert api.uploads == {}
	before = copy.deepcopy(api.writes)
	assert publisher.prepare(7, event=_event(api))["status"] == "already-complete"
	assert api.writes == before
	api.available = False
	with pytest.raises(PublicationError, match="digest"):
		publisher.prepare(7, event=_event(api))
	assert api.writes == before
