"""Related behavior tests."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from publication_flow_fixture import (
	BASE,
	BODY,
	NOTES,
	ORIGINAL_RUN,
	PR_HEAD,
	PROVIDER,
	SOURCE,
	GitHub,
)
from test_publication_flow import (
	_event,
	_expected_prepare_outputs,
	_persist_candidate,
	_publisher,
	_run_prepare_cli,
	_workflow_outputs,
)

from quality_gate.publication import PublicationError
from quality_gate.publication_flow import Context, Publisher

pytest_plugins = ("test_publication_flow",)


def test_prepare_accepts_native_projections_from_exact_source_without_execution(
	projected_api: GitHub, tmp_path: Path
) -> None:
	result = _publisher(projected_api).prepare(7, event=_event(projected_api))
	assert result["status"] == "build-required"
	assert result["version"] == "2.1.0"
	assert result["source_sha"] == SOURCE
	assert set(projected_api.source_reads) == {
		(SOURCE, ".release/version.toml"),
		(SOURCE, ".release/publisher.toml"),
		(SOURCE, "pyproject.toml"),
		(SOURCE, "package.json"),
		(SOURCE, "product/version.py"),
		(SOURCE, ".release/notes.md"),
		(BASE, ".release/version.toml"),
	}
	assert not (tmp_path / "python-projection-executed").exists()
	assert projected_api.writes == []


@pytest.mark.parametrize(
	("path", "content"),
	[
		("pyproject.toml", '[project]\nversion = "v2.1.1"\n'),
		("package.json", '{"release": {"version": "2.1.1"}}\n'),
		("product/version.py", '__version__ = "2.1.1"\n'),
	],
	ids=["toml", "json", "python"],
)
def test_prepare_rejects_native_projection_mismatch_before_mutation(
	projected_api: GitHub, path: str, content: str
) -> None:
	projected_api.files[(SOURCE, path)] = content
	with pytest.raises(
		PublicationError, match="native version projections disagree with the authority"
	):
		_publisher(projected_api).prepare(7, event=_event(projected_api))
	assert (SOURCE, path) in projected_api.source_reads
	assert projected_api.writes == []


@pytest.mark.parametrize(
	("path", "content", "message"),
	[
		("package.json", '{"release": {}}\n', "version projection is missing or invalid"),
		(
			"pyproject.toml",
			'[project]\nversion = "2.1.0"\n',
			"version projection is missing or invalid",
		),
		(
			"product/version.py",
			'__version__ = "2.1.0"\n__version__ = "2.1.0"\n',
			"Python projection requires one literal assignment",
		),
	],
	ids=["missing-json-key", "missing-toml-prefix", "duplicate-python-literal"],
)
def test_prepare_rejects_invalid_native_projection_before_mutation(
	projected_api: GitHub, path: str, content: str, message: str
) -> None:
	projected_api.files[(SOURCE, path)] = content
	with pytest.raises(PublicationError, match=message):
		_publisher(projected_api).prepare(7, event=_event(projected_api))
	assert (SOURCE, path) in projected_api.source_reads
	assert projected_api.writes == []


def test_post_merge_preserves_original_candidate_before_product_work() -> None:
	api = GitHub()
	result = _publisher(api).prepare(7, event=_event(api))
	assert result["status"] == "build-required"
	candidate = result["candidate"]
	assert isinstance(candidate, dict)
	assert candidate["source_sha"] == SOURCE
	assert candidate["body"] == BODY
	assert candidate["notes_sha256"] == hashlib.sha256(NOTES.encode()).hexdigest()
	assert candidate["origin_run_id"] == ORIGINAL_RUN
	assert candidate["provider_sha"] == PROVIDER
	assert api.writes == []


def test_prepare_cli_persists_canonical_envelope_and_upload_handoff(
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
	tmp_path: Path,
) -> None:
	api = GitHub()
	exit_code, output_path, candidate_directory = _run_prepare_cli(
		monkeypatch, _publisher(api), tmp_path, event=_event(api)
	)

	assert exit_code == 0
	assert json.loads(capsys.readouterr().out) == {
		"status": "build-required",
		"version": "2.1.0",
		"source_sha": SOURCE,
	}
	expected_candidate = (
		'{"base_sha":"dddddddddddddddddddddddddddddddddddddddd",'
		'"body":"## Release\\n- Version: 2.1.0\\n- Changes: Общая публикация.\\n'
		"- Publication: owner merge authorizes publication after the merged commit "
		'passes the required release checks.\\n",'
		'"config":{"producers":[{"checks":["Verify package"],'
		'"deliverables":["package-linux-{version}.zip"],"job":"Build linux",'
		'"name":"linux"},{"checks":["Verify package"],'
		'"deliverables":["package-windows-{version}.zip"],"job":"Build windows",'
		'"name":"windows"}],"schema":1,"workflow":".github/workflows/release.yml"},'
		'"merged_at":"2026-09-14T10:00:00Z",'
		'"notes":"Общий механизм публикации. Существующие пакеты сохраняются.\\n",'
		'"notes_sha256":"3fc008ab1716887e40a37fa9267a60926e15f52f70fd28e43ea6ef0e5cc4ca6f",'
		'"origin_attempt":1,"origin_run_head_sha":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",'
		'"origin_run_id":100,"pr":7,"provider_repository":"o/provider",'
		'"provider_sha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
		'"repository":"o/r","schema":1,"source_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
		'"version":"2.1.0"}\n'
	).encode()
	expected_digest = "9241958f0e72d4c14f972bdad61fbf042a1871e21b36842ab39a5294ca9a2128"
	assert hashlib.sha256(expected_candidate).hexdigest() == expected_digest
	assert (candidate_directory / "candidate.json").read_bytes() == expected_candidate
	assert [path.name for path in candidate_directory.iterdir()] == ["candidate.json"]
	assert _workflow_outputs(output_path) == _expected_prepare_outputs(
		status="build-required",
		version="2.1.0",
		source_sha=SOURCE,
		candidate_run_id="100",
		candidate_digest=expected_digest,
		artifact_name="release-candidate-pr-7-100-1",
		save_required="true",
	)


def test_prepare_cli_forwards_reused_candidate_without_saving_a_second_envelope(
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
	tmp_path: Path,
) -> None:
	api = GitHub()
	artifact_id = _persist_candidate(api)
	api.run_overrides["run_attempt"] = 2
	before_artifacts = copy.deepcopy(api.artifacts)
	exit_code, output_path, candidate_directory = _run_prepare_cli(
		monkeypatch, _publisher(api, attempt=2), tmp_path, event=_event(api)
	)

	assert exit_code == 0
	assert json.loads(capsys.readouterr().out) == {
		"status": "build-required",
		"version": "2.1.0",
		"source_sha": SOURCE,
		"candidate_artifact_id": artifact_id,
	}
	assert _workflow_outputs(output_path) == _expected_prepare_outputs(
		status="build-required",
		version="2.1.0",
		source_sha=SOURCE,
		candidate_id=str(artifact_id),
		candidate_run_id="100",
		candidate_digest="9241958f0e72d4c14f972bdad61fbf042a1871e21b36842ab39a5294ca9a2128",
		artifact_name="release-candidate-pr-7-100-1",
	)
	assert api.artifacts == before_artifacts
	assert not candidate_directory.exists()


def test_recovery_cli_forwards_original_candidate_without_creating_an_envelope(
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
	tmp_path: Path,
) -> None:
	recovery_sha = "c" * 40

	class RecoveryGitHub(GitHub):
		def get(self, path: str) -> object:
			if path == "/actions/runs/101":
				run = super().get("/actions/runs/100")
				assert isinstance(run, dict)
				return {
					**run,
					"id": 101,
					"head_sha": recovery_sha,
					"event": "workflow_dispatch",
					"head_branch": "main",
				}
			return super().get(path)

	api = RecoveryGitHub()
	artifact_id = _persist_candidate(api)
	before_artifacts = copy.deepcopy(api.artifacts)
	publisher = Publisher(
		api,
		Context(
			repository="o/r",
			provider_repository="o/provider",
			provider_sha=PROVIDER,
			helper_sha=PROVIDER,
			run_id=101,
			attempt=1,
			run_source_sha=recovery_sha,
			run_head_sha=recovery_sha,
		),
	)
	exit_code, output_path, candidate_directory = _run_prepare_cli(
		monkeypatch,
		publisher,
		tmp_path,
		event_name="workflow_dispatch",
		event={"inputs": {"pr-number": "7"}},
	)

	assert exit_code == 0
	assert json.loads(capsys.readouterr().out) == {
		"status": "build-required",
		"version": "2.1.0",
		"source_sha": SOURCE,
		"candidate_artifact_id": artifact_id,
	}
	assert _workflow_outputs(output_path) == _expected_prepare_outputs(
		status="build-required",
		version="2.1.0",
		source_sha=SOURCE,
		candidate_id=str(artifact_id),
		candidate_run_id="100",
		candidate_digest="9241958f0e72d4c14f972bdad61fbf042a1871e21b36842ab39a5294ca9a2128",
		artifact_name="release-candidate-pr-7-100-1",
	)
	assert api.artifacts == before_artifacts
	assert not candidate_directory.exists()


def test_validation_cli_does_not_create_a_candidate_envelope(
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
	tmp_path: Path,
) -> None:
	api = GitHub()
	api.pull["merged"] = False
	for (revision, path), content in list(api.files.items()):
		if revision == SOURCE:
			api.files[(PR_HEAD, path)] = content
	before_artifacts = copy.deepcopy(api.artifacts)
	event = {"action": "edited", "pull_request": copy.deepcopy(api.pull)}
	exit_code, output_path, candidate_directory = _run_prepare_cli(
		monkeypatch,
		_publisher(api),
		tmp_path,
		event=event,
		validation_only=True,
	)

	assert exit_code == 0
	assert json.loads(capsys.readouterr().out) == {
		"status": "validated",
		"version": "2.1.0",
		"release": True,
	}
	assert _workflow_outputs(output_path) == _expected_prepare_outputs(
		status="validated", version="2.1.0"
	)
	assert api.artifacts == before_artifacts
	assert not candidate_directory.exists()


def test_no_release_cli_does_not_request_a_candidate_envelope(
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
	tmp_path: Path,
) -> None:
	api = GitHub()
	api.pull["body"] = "## Release\n- No release: Только инструкции.\n"
	api.files[(SOURCE, ".release/version.toml")] = 'version = "2.0.6"\n'
	api.files[(SOURCE, ".release/notes.md")] = "Impact: PATCH\nVersion: 2.0.6\n"
	before_artifacts = copy.deepcopy(api.artifacts)
	exit_code, output_path, candidate_directory = _run_prepare_cli(
		monkeypatch, _publisher(api), tmp_path, event=_event(api)
	)

	assert exit_code == 0
	assert json.loads(capsys.readouterr().out) == {
		"status": "no-release",
		"version": "2.0.6",
		"release": False,
	}
	assert _workflow_outputs(output_path) == _expected_prepare_outputs(
		status="no-release", version="2.0.6"
	)
	assert api.artifacts == before_artifacts
	assert not candidate_directory.exists()


@pytest.mark.parametrize(
	("field", "value"),
	[
		("merged", False),
		("merged_by", {"login": "intruder"}),
		("merge_commit_sha", "e" * 40),
		("base", {"ref": "feature", "sha": BASE, "repo": {"full_name": "o/r"}}),
	],
)
def test_post_merge_rejects_unauthorized_sources(field: str, value: object) -> None:
	api = GitHub()
	api.pull[field] = value
	with pytest.raises(PublicationError):
		_publisher(api).prepare(7, event=_event(api))
	assert api.writes == []


def test_open_pr_validation_reads_head_and_never_creates_an_envelope() -> None:
	api = GitHub()
	api.pull["merged"] = False
	head = "e" * 40
	for (sha, path), content in list(api.files.items()):
		if sha == SOURCE:
			api.files[(head, path)] = content
	result = _publisher(api).prepare(
		7, event={"action": "edited", "pull_request": copy.deepcopy(api.pull)}, validation_only=True
	)
	assert result == {"status": "validated", "version": "2.1.0", "release": True}
	assert api.writes == []
