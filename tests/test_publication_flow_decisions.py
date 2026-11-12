"""Related behavior tests."""

from __future__ import annotations

import copy

import pytest
from publication_flow_fixture import BASE, NOTES, PR_HEAD, PROVIDER, SOURCE, GitHub
from test_publication_flow import (
	_candidate_from_artifact,
	_dependabot_files,
	_event,
	_open_dependabot_event,
	_publisher,
	_ready,
)

from quality_gate.publication import PublicationError
from quality_gate.publication_flow import Context, Publisher


def test_post_merge_no_release_ignores_old_patch_notes() -> None:
	api = GitHub()
	api.pull["body"] = "## Release\n- No release: Только инструкции.\n"
	api.files[(SOURCE, ".release/version.toml")] = 'version = "2.0.6"\n'
	api.files[(SOURCE, ".release/notes.md")] = "Impact: PATCH\nVersion: 2.0.6\n"
	result = _publisher(api).prepare(7, event=_event(api))
	assert result["status"] == "no-release"
	assert "candidate" not in result
	assert api.writes == []


def test_dependabot_workflow_only_pr_without_a_release_section_validates_as_no_release() -> None:
	api = GitHub()
	event = _open_dependabot_event(
		api,
		[
			{"filename": ".github/workflows/quality.yml"},
			{"filename": ".github/workflows/release.yaml"},
		],
	)

	result = _publisher(api).prepare(7, event=event, validation_only=True)

	assert result == {"status": "validated", "version": "2.0.6", "release": False}
	assert api.pull_file_pages == [1]
	assert api.writes == []
	assert (PR_HEAD, ".release/notes.md") not in api.source_reads


@pytest.mark.parametrize(
	"files",
	[
		[
			{"filename": ".github/workflows/quality.yml"},
			{"filename": "src/app.py"},
		],
		[
			{
				"filename": ".github/workflows/quality.yml",
				"previous_filename": "src/quality.py",
			}
		],
		[{"filename": ".github/workflows/nested/quality.yml"}],
	],
	ids=["mixed-files", "rename-from-code", "nested-path"],
)
def test_dependabot_with_non_workflow_changes_still_requires_release_decision(
	files: list[dict[str, object]],
) -> None:
	api = GitHub()
	event = _open_dependabot_event(api, files)

	with pytest.raises(
		PublicationError, match="exactly one second-level Release section is required"
	):
		_publisher(api).prepare(7, event=event, validation_only=True)

	assert api.writes == []


def test_dependabot_workflow_only_detection_reads_every_file_page() -> None:
	api = GitHub()
	event = _open_dependabot_event(
		api,
		[{"filename": f".github/workflows/action-{index}.yml"} for index in range(100)]
		+ [{"filename": ".github/workflows/release.yaml"}],
	)

	result = _publisher(api).prepare(7, event=event, validation_only=True)

	assert result["release"] is False
	assert api.pull_file_pages == [1, 2]


def test_incomplete_dependabot_file_list_does_not_allow_implicit_no_release() -> None:
	api = GitHub()
	event = _open_dependabot_event(api, [{"filename": ".github/workflows/quality.yml"}])
	api.pull["changed_files"] = 2

	with pytest.raises(
		PublicationError, match="exactly one second-level Release section is required"
	):
		_publisher(api).prepare(7, event=event, validation_only=True)

	assert api.pull_file_pages == [1]


def test_non_dependabot_pr_cannot_use_implicit_workflow_no_release() -> None:
	api = GitHub()
	event = _open_dependabot_event(api, [{"filename": ".github/workflows/quality.yml"}])
	pull = event["pull_request"]
	assert isinstance(pull, dict)
	pull["user"] = {"login": "human", "type": "User"}
	api.pull["user"] = {"login": "human", "type": "User"}

	with pytest.raises(
		PublicationError, match="exactly one second-level Release section is required"
	):
		_publisher(api).prepare(7, event=event, validation_only=True)

	assert api.pull_file_pages == []


def test_dependabot_stale_event_head_cannot_use_implicit_workflow_no_release() -> None:
	api = GitHub()
	event = _open_dependabot_event(api, [{"filename": ".github/workflows/quality.yml"}])
	pull = event["pull_request"]
	assert isinstance(pull, dict)
	head = pull["head"]
	assert isinstance(head, dict)
	head["sha"] = "f" * 40

	with pytest.raises(
		PublicationError, match="exactly one second-level Release section is required"
	):
		_publisher(api).prepare(7, event=event, validation_only=True)

	assert api.pull_file_pages == []


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
