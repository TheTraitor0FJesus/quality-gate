"""Feature test partition."""

from __future__ import annotations

from publication_flow_feature_split72_support import (
	BASE as BASE,
)
from publication_flow_feature_split72_support import (
	BODY as BODY,
)
from publication_flow_feature_split72_support import (
	CONFIG as CONFIG,
)
from publication_flow_feature_split72_support import (
	NOTES as NOTES,
)
from publication_flow_feature_split72_support import (
	ORIGINAL_RUN as ORIGINAL_RUN,
)
from publication_flow_feature_split72_support import (
	PR_HEAD as PR_HEAD,
)
from publication_flow_feature_split72_support import (
	PROVIDER as PROVIDER,
)
from publication_flow_feature_split72_support import (
	RECEIPT_MARKER as RECEIPT_MARKER,
)
from publication_flow_feature_split72_support import (
	REPAIRED_PROVIDER as REPAIRED_PROVIDER,
)
from publication_flow_feature_split72_support import (
	REPAIRED_REFERENCE as REPAIRED_REFERENCE,
)
from publication_flow_feature_split72_support import (
	REPAIRED_RUN as REPAIRED_RUN,
)
from publication_flow_feature_split72_support import (
	REPAIRED_SOURCE as REPAIRED_SOURCE,
)
from publication_flow_feature_split72_support import (
	SOURCE as SOURCE,
)
from publication_flow_feature_split72_support import (
	BaselineGitHub as BaselineGitHub,
)
from publication_flow_feature_split72_support import (
	Context as Context,
)
from publication_flow_feature_split72_support import (
	GitHub as GitHub,
)
from publication_flow_feature_split72_support import (
	Mapping as Mapping,
)
from publication_flow_feature_split72_support import (
	MultiImageRegistryGitHub as MultiImageRegistryGitHub,
)
from publication_flow_feature_split72_support import (
	Path as Path,
)
from publication_flow_feature_split72_support import (
	PublicationError as PublicationError,
)
from publication_flow_feature_split72_support import (
	PublicationFailureGitHub as PublicationFailureGitHub,
)
from publication_flow_feature_split72_support import (
	Publisher as Publisher,
)
from publication_flow_feature_split72_support import (
	RegistryGitHub as RegistryGitHub,
)
from publication_flow_feature_split72_support import (
	RepairedGitHub as RepairedGitHub,
)
from publication_flow_feature_split72_support import (
	TagForbiddenRegistryGitHub as TagForbiddenRegistryGitHub,
)
from publication_flow_feature_split72_support import (
	_candidate_from_artifact as _candidate_from_artifact,
)
from publication_flow_feature_split72_support import (
	_dependabot_files as _dependabot_files,
)
from publication_flow_feature_split72_support import (
	_event as _event,
)
from publication_flow_feature_split72_support import (
	_expected_prepare_outputs as _expected_prepare_outputs,
)
from publication_flow_feature_split72_support import (
	_open_dependabot_event as _open_dependabot_event,
)
from publication_flow_feature_split72_support import (
	_persist_candidate as _persist_candidate,
)
from publication_flow_feature_split72_support import (
	_publisher as _publisher,
)
from publication_flow_feature_split72_support import (
	_ready as _ready,
)
from publication_flow_feature_split72_support import (
	_run_prepare_cli as _run_prepare_cli,
)
from publication_flow_feature_split72_support import (
	_workflow_outputs as _workflow_outputs,
)
from publication_flow_feature_split72_support import (
	cast as cast,
)
from publication_flow_feature_split72_support import (
	copy as copy,
)
from publication_flow_feature_split72_support import (
	hashlib as hashlib,
)
from publication_flow_feature_split72_support import (
	io as io,
)
from publication_flow_feature_split72_support import (
	json as json,
)
from publication_flow_feature_split72_support import (
	projected_api as projected_api,
)
from publication_flow_feature_split72_support import (
	pytest as pytest,
)
from publication_flow_feature_split72_support import (
	zipfile as zipfile,
)


def test_dependabot_mixed_head_snapshot_cannot_use_implicit_workflow_no_release() -> None:
	api = GitHub()
	event = _open_dependabot_event(api, [{"filename": ".github/workflows/quality.yml"}])
	api.pull_after_file_read = {"body": "updated during file listing"}

	with pytest.raises(
		PublicationError, match="exactly one second-level Release section is required"
	):
		_publisher(api).prepare(7, event=event, validation_only=True)

	assert api.pull_file_pages == [1]


def test_malformed_explicit_dependabot_decision_does_not_use_implicit_no_release() -> None:
	api = GitHub()
	event = _open_dependabot_event(api, [{"filename": ".github/workflows/quality.yml"}])
	pull = event["pull_request"]
	assert isinstance(pull, dict)
	pull["body"] = "## Release\n\n- Version: 2.0.6"
	api.pull["body"] = pull["body"]

	with pytest.raises(PublicationError, match="Release requires Version"):
		_publisher(api).prepare(7, event=event, validation_only=True)

	assert api.pull_file_pages == []


def test_duplicate_release_sections_do_not_use_implicit_no_release() -> None:
	api = GitHub()
	event = _open_dependabot_event(api, [{"filename": ".github/workflows/quality.yml"}])
	duplicate_body = "## Release\n\n- No release: first.\n\n## Release\n\n- No release: second.\n"
	pull = event["pull_request"]
	assert isinstance(pull, dict)
	pull["body"] = duplicate_body
	api.pull["body"] = duplicate_body

	with pytest.raises(PublicationError, match="multiple second-level Release sections"):
		_publisher(api).prepare(7, event=event, validation_only=True)

	assert api.pull_file_pages == []
	assert api.writes == []


def test_post_merge_dependabot_workflow_only_pr_skips_product_publication() -> None:
	api = GitHub()
	_dependabot_files(api, [{"filename": ".github/workflows/quality.yml"}])

	result = _publisher(api).prepare(7, event=_event(api))

	assert result["status"] == "no-release"
	assert "candidate" not in result
	assert (SOURCE, ".release/notes.md") not in api.source_reads
	assert api.writes == []


def test_post_merge_duplicate_release_sections_are_not_treated_as_absent() -> None:
	api = GitHub()
	_dependabot_files(api, [{"filename": ".github/workflows/quality.yml"}])
	duplicate_body = "## Release\n\n- No release: first.\n\n## Release\n\n- No release: second.\n"
	api.pull["body"] = duplicate_body

	with pytest.raises(PublicationError, match="multiple second-level Release sections"):
		_publisher(api).prepare(7, event=_event(api))

	assert api.pull_file_pages == []
	assert api.writes == []


def test_helper_mismatch_is_rejected_before_accessing_product() -> None:
	api = GitHub()
	with pytest.raises(PublicationError, match="helper"):
		Publisher(
			api,
			Context(
				repository="o/r",
				provider_repository="o/provider",
				provider_sha=PROVIDER,
				helper_sha=SOURCE,
				run_id=100,
				attempt=1,
				run_source_sha=SOURCE,
				run_head_sha=PR_HEAD,
			),
		)
	assert api.writes == []


def test_complete_release_uses_both_verified_packages_without_admin_api() -> None:
	api = GitHub()
	publisher, artifact_id = _ready(api)
	result = publisher.publish(7, artifact_id)
	assert result["status"] == "published"
	assert api.tags["v2.1.0"] == SOURCE
	assert api.releases[-1]["draft"] is False
	assert api.releases[-1]["immutable"] is False
	assert api.uploads == {
		"package-linux-2.1.0.zip": b"linux",
		"package-windows-2.1.0.zip": b"windows",
	}
	assert str(api.releases[-1]["body"]).startswith(NOTES)
	before = copy.deepcopy((api.releases, api.tags, api.writes))
	assert publisher.prepare(7, event=_event(api))["status"] == "already-complete"
	assert before == (api.releases, api.tags, api.writes)


def test_interrupted_upload_resumes_only_missing_assets() -> None:
	api = GitHub()
	publisher, artifact_id = _ready(api)
	api.fail_upload = "package-windows-2.1.0.zip"
	with pytest.raises(PublicationError, match="interrupted"):
		publisher.publish(7, artifact_id)
	assert api.releases[-1]["draft"] is True
	assert api.writes.count("package-linux-2.1.0.zip") == 1
	api.fail_upload = ""
	assert publisher.publish(7, artifact_id)["status"] == "published"
	assert api.writes.count("package-linux-2.1.0.zip") == 1


def test_recovery_preserves_matching_draft_receipt_when_rebuilt_assets_change() -> None:
	api = GitHub()
	publisher, candidate_artifact_id = _ready(api)
	api.fail_publish = True
	with pytest.raises(PublicationError, match="interrupted publication"):
		publisher.publish(7, candidate_artifact_id)
	release = api.releases[-1]
	assert release["draft"] is True
	original_body = release["body"]
	original_uploads = copy.deepcopy(api.uploads)

	candidate = _candidate_from_artifact(api, candidate_artifact_id)
	api.fail_publish = False
	api.run_overrides["run_attempt"] = 2
	api.build(candidate, attempt=2)
	recovery = _publisher(api, attempt=2)

	assert recovery.publish(7, candidate_artifact_id)["status"] == "published"
	assert release["body"] == original_body
	assert api.uploads == original_uploads
	assert api.writes.count("package-linux-2.1.0.zip") == 1
	assert api.writes.count("package-windows-2.1.0.zip") == 1


def test_recovery_does_not_fill_missing_draft_asset_with_different_bytes() -> None:
	api = GitHub()
	publisher, candidate_artifact_id = _ready(api)
	api.fail_upload = "package-windows-2.1.0.zip"
	with pytest.raises(PublicationError, match="interrupted"):
		publisher.publish(7, candidate_artifact_id)
	release = api.releases[-1]
	original_body = release["body"]
	before_writes = copy.deepcopy(api.writes)

	candidate = _candidate_from_artifact(api, candidate_artifact_id)
	api.fail_upload = ""
	api.run_overrides["run_attempt"] = 2
	api.build(candidate, attempt=2)
	recovery = _publisher(api, attempt=2)
	with pytest.raises(
		PublicationError, match="current verified output does not match its recorded digest"
	):
		recovery.publish(7, candidate_artifact_id)

	assert release["draft"] is True
	assert release["body"] == original_body
	assert api.writes == before_writes
	assert "package-windows-2.1.0.zip" not in api.uploads


@pytest.mark.parametrize(
	"conflict", ["tag", "notes", "asset", "duplicate-draft", "evidence", "producer"]
)
def test_conflicts_fail_without_forbidden_release_writes(conflict: str) -> None:
	api = GitHub()
	publisher, artifact_id = _ready(api)
	if conflict == "tag":
		api.tags["v2.1.0"] = BASE
	elif conflict == "evidence":
		api.archives[301] = b"substituted artifact"
	elif conflict == "producer":
		api.jobs[-1]["conclusion"] = "failure"
	else:
		publisher.publish(7, artifact_id)
		if conflict == "notes":
			api.releases[-1]["body"] = "edited notes"
		elif conflict == "asset":
			api.releases[-1]["assets"] = []
		else:
			api.releases[-1]["draft"] = True
			api.releases.append(copy.deepcopy(api.releases[-1]))
	before = copy.deepcopy((api.writes, api.releases, api.tags))
	with pytest.raises(PublicationError):
		publisher.publish(7, artifact_id)
	assert before == (api.writes, api.releases, api.tags)


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
