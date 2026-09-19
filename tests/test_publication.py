"""Behavior at the shared preparation/publication interface."""

from __future__ import annotations

import pytest

from quality_gate.publication import PublicationError, prepare
from quality_gate.publication_evidence import verify_producer

PUBLICATION = (
	"owner merge authorizes publication after the merged commit passes the required release checks."
)
BODY = f"## Release\n\n- Version: 2.1.0\n- Changes: Общий выпуск.\n- Publication: {PUBLICATION}\n"


def test_validation_accepts_plain_version_without_notes_impact() -> None:
	result = prepare(
		body=BODY,
		version="2.1.0",
		base_version="2.0.6",
		baseline="2.0.6",
		projections=["2.1.0"],
	)
	assert result == {"status": "validated", "version": "2.1.0", "release": True}


def test_no_release_keeps_previous_version_without_reading_old_notes() -> None:
	assert prepare(
		body="## Release\n\n- No release: Только внутренние инструкции.\n",
		version="2.0.6",
		base_version="2.0.6",
		baseline="2.0.6",
		projections=["2.0.6"],
	) == {"status": "validated", "version": "2.0.6", "release": False}


@pytest.mark.parametrize(
	"body",
	[
		BODY + BODY,
		BODY + "- Version: 2.1.0\n",
		BODY + "- No release: Нет.\n",
		"```markdown\n" + BODY + "```\n",
		"<!--\n" + BODY + "-->\n",
		"<pre>\n" + BODY + "\n## Other\n</pre>\n",
		"<blockquote>\n" + BODY + "\n## Other\n</blockquote>\n",
		BODY + BODY.replace("## Release", "## Release ##"),
		"\n".join("> " + line for line in BODY.splitlines()),
		BODY.replace("2.1.0", "2.0.6 -> 2.1.0 (MINOR; изменение)"),
		BODY.replace("## Release", "## Other"),
		BODY.replace("- Publication:", "### Approval\n- Publication:"),
	],
)
def test_malformed_decisions_cannot_authorize(body: str) -> None:
	with pytest.raises(PublicationError):
		prepare(body=body, version="2.1.0", base_version="2.0.6", baseline="2.0.6", projections=[])


@pytest.mark.parametrize(
	("version", "base", "baseline", "projections", "body"),
	[
		("2.1.1", "2.0.6", "2.0.6", [], BODY.replace("2.1.0", "2.1.1")),
		("2.1.0", "2.0.6", "2.1.0", [], BODY),
		("2.1.0", "2.0.6", "2.0.6", ["2.0.6"], BODY),
		("2.1.0", "2.0.6", "2.0.6", [], "## Release\n- No release: Нет.\n"),
	],
)
def test_version_conflicts_fail(
	version: str, base: str, baseline: str, projections: list[str], body: str
) -> None:
	with pytest.raises(PublicationError):
		prepare(
			body=body,
			version=version,
			base_version=base,
			baseline=baseline,
			projections=projections,
		)


SOURCE = "a" * 40
PROVIDER = "b" * 40


def _provenance() -> dict[str, object]:
	return {
		"run": {
			"id": 100,
			"head_sha": SOURCE,
			"path": ".github/workflows/release.yml",
			"event": "pull_request",
			"repository": {"full_name": "o/r"},
			"head_repository": {"full_name": "o/r"},
			"referenced_workflows": [
				{
					"path": f"o/provider/.github/workflows/release-prepare.yml@{PROVIDER}",
					"sha": PROVIDER,
				}
			],
		},
		"job": {
			"id": 200,
			"run_id": 100,
			"run_attempt": 1,
			"head_sha": SOURCE,
			"name": "Build linux",
			"status": "completed",
			"conclusion": "success",
			"started_at": "2026-09-14T10:00:00Z",
			"completed_at": "2026-09-14T10:10:00Z",
			"steps": [
				{"name": "Verify package", "conclusion": "success"},
				{"name": "Upload release evidence", "conclusion": "success"},
			],
		},
		"artifact": {
			"id": 300,
			"name": "release-evidence-linux-1",
			"expired": False,
			"digest": "sha256:" + "c" * 64,
			"created_at": "2026-09-14T10:09:00Z",
			"workflow_run": {"id": 100, "head_sha": SOURCE},
		},
		"logs": "Artifact ID: 300\nSHA256 digest of uploaded artifact zip is " + "c" * 64,
	}


def test_successful_producing_attempt_survives_failed_jobs_rerun() -> None:
	assert verify_producer(
		_provenance(),
		repository="o/r",
		run_id=100,
		run_head_sha=SOURCE,
		current_attempt=2,
		provider_repository="o/provider",
		provider_sha=PROVIDER,
		workflow=".github/workflows/release.yml",
		job_name="Build linux",
		required_steps=["Verify package"],
		artifact_name="release-evidence-linux-1",
	) == {"artifact_id": 300, "digest": "c" * 64, "job_id": 200, "attempt": 1}


@pytest.mark.parametrize(
	("section", "field", "value"),
	[
		("run", "head_sha", "d" * 40),
		("run", "id", 101),
		("run", "referenced_workflows", []),
		("run", "path", ".github/workflows/untrusted.yml"),
		("job", "conclusion", "failure"),
		("job", "status", "in_progress"),
		("job", "run_id", 101),
		("job", "run_attempt", 3),
		("job", "head_sha", "d" * 40),
		("job", "steps", []),
		("job", "name", "Untrusted build"),
		("artifact", "expired", True),
		("artifact", "workflow_run", {"id": 101, "head_sha": SOURCE}),
		("artifact", "digest", "sha256:" + "d" * 64),
		("artifact", "id", 301),
		("artifact", "created_at", "2026-09-13T10:09:00Z"),
	],
)
def test_substituted_or_unsuccessful_provenance_is_rejected(
	section: str, field: str, value: object
) -> None:
	proof = _provenance()
	record = proof[section]
	assert isinstance(record, dict)
	record[field] = value
	with pytest.raises(PublicationError):
		verify_producer(
			proof,
			repository="o/r",
			run_id=100,
			run_head_sha=SOURCE,
			current_attempt=2,
			provider_repository="o/provider",
			provider_sha=PROVIDER,
			workflow=".github/workflows/release.yml",
			job_name="Build linux",
			required_steps=["Verify package"],
			artifact_name="release-evidence-linux-1",
		)
