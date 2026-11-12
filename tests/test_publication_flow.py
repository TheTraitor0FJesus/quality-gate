"""Shared fixtures and helpers for related tests."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import zipfile
from collections.abc import Mapping
from pathlib import Path

import pytest
from publication_flow_fixture import BASE, CONFIG, PR_HEAD, PROVIDER, SOURCE, GitHub

from quality_gate.publication import PublicationError
from quality_gate.publication_flow import Context, Publisher


def _publisher(
	api: GitHub,
	*,
	attempt: int = 1,
	evidence_wait_seconds: float = 30.0,
	evidence_poll_seconds: float = 2.0,
) -> Publisher:
	return Publisher(
		api,
		Context(
			repository="o/r",
			provider_repository="o/provider",
			provider_sha=PROVIDER,
			helper_sha=PROVIDER,
			run_id=100,
			attempt=attempt,
			run_source_sha=SOURCE,
			run_head_sha=PR_HEAD,
		),
		evidence_wait_seconds=evidence_wait_seconds,
		evidence_poll_seconds=evidence_poll_seconds,
	)


def _event(api: GitHub) -> dict[str, object]:
	return {"action": "closed", "pull_request": copy.deepcopy(api.pull)}


def _run_prepare_cli(
	monkeypatch: pytest.MonkeyPatch,
	publisher: Publisher,
	tmp_path: Path,
	*,
	event: dict[str, object],
	event_name: str = "pull_request",
	validation_only: bool = False,
) -> tuple[int, Path, Path]:
	from quality_gate import publication_cli

	event_path = tmp_path / "event.json"
	output_path = tmp_path / "GITHUB_OUTPUT"
	candidate_directory = tmp_path / "release-candidate"
	event_path.write_text(json.dumps(event), encoding="utf-8")
	monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
	monkeypatch.setenv("GITHUB_EVENT_NAME", event_name)
	monkeypatch.setenv("GITHUB_OUTPUT", str(output_path))
	monkeypatch.setattr(publication_cli, "_publisher", lambda _root: publisher)
	arguments = ["prepare", "--pr", "7", "--output", str(candidate_directory)]
	if validation_only:
		arguments.append("--validation-only")
	return publication_cli._main(arguments), output_path, candidate_directory


def _workflow_outputs(path: Path) -> dict[str, str]:
	return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines())


def _expected_prepare_outputs(
	*,
	status: str,
	version: str,
	source_sha: str = "",
	candidate_id: str = "",
	candidate_run_id: str = "",
	candidate_digest: str = "",
	artifact_name: str = "",
	save_required: str = "false",
) -> dict[str, str]:
	return {
		"status": status,
		"version": version,
		"source-sha": source_sha,
		"candidate-id": candidate_id,
		"provider-sha": PROVIDER,
		"provider-repository": "o/provider",
		"save-required": save_required,
		"candidate-run-id": candidate_run_id,
		"candidate-digest": candidate_digest,
		"artifact-name": artifact_name,
	}


def _persist_candidate(api: GitHub) -> int:
	result = _publisher(api).prepare(7, event=_event(api))
	assert result["status"] == "build-required"
	candidate = result["candidate"]
	assert isinstance(candidate, dict)
	return api.persist(candidate)


def _dependabot_files(api: GitHub, files: list[dict[str, object]]) -> None:
	api.pull["body"] = "Dependabot's generated update description."
	api.pull["user"] = {"login": "dependabot[bot]", "type": "Bot"}
	api.pull["changed_files"] = len(files)
	api.pull_files = files
	api.files[(SOURCE, ".release/version.toml")] = 'version = "2.0.6"\n'


def _open_dependabot_event(api: GitHub, files: list[dict[str, object]]) -> dict[str, object]:
	_dependabot_files(api, files)
	api.pull["merged"] = False
	for (revision, path), content in list(api.files.items()):
		if revision == SOURCE:
			api.files[(PR_HEAD, path)] = content
	return {"action": "opened", "pull_request": copy.deepcopy(api.pull)}


@pytest.fixture
def projected_api(tmp_path: Path) -> GitHub:
	api = GitHub()
	api.files[(SOURCE, ".release/publisher.toml")] = (
		CONFIG
		+ """
[[projections]]
path = "pyproject.toml"
format = "toml"
key = "project.version"
prefix = "v"
[[projections]]
path = "package.json"
format = "json"
key = "release.version"
[[projections]]
path = "product/version.py"
format = "python"
key = "__version__"
"""
	)
	api.files[(SOURCE, "pyproject.toml")] = '[project]\nversion = "v2.1.0"\n'
	api.files[(SOURCE, "package.json")] = '{"release": {"version": "2.1.0"}}\n'
	marker = tmp_path / "python-projection-executed"
	api.files[(SOURCE, "product/version.py")] = (
		"from pathlib import Path\n"
		f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n"
		'__version__ = "2.1.0"\n'
	)
	# Valid decoys let the read assertions detect a wrong revision directly.
	for (revision, path), content in list(api.files.items()):
		if revision == SOURCE:
			for decoy in (PR_HEAD, "main"):
				api.files[(decoy, path)] = content
	return api


def _ready(
	api: GitHub, *, evidence_wait_seconds: float = 30.0, evidence_poll_seconds: float = 2.0
) -> tuple[Publisher, int]:
	publisher = _publisher(
		api,
		evidence_wait_seconds=evidence_wait_seconds,
		evidence_poll_seconds=evidence_poll_seconds,
	)
	candidate = publisher.prepare(7, event=_event(api))["candidate"]
	assert isinstance(candidate, dict)
	artifact_id = api.persist(candidate)
	api.build(candidate)
	return publisher, artifact_id


def _candidate_from_artifact(api: GitHub, artifact_id: int) -> dict[str, object]:
	with zipfile.ZipFile(io.BytesIO(api.archives[artifact_id])) as archive:
		return json.loads(archive.read("candidate.json"))


class RegistryGitHub(GitHub):
	def __init__(self, kind: str) -> None:
		super().__init__()
		self.kind = kind
		self.available = True
		self.image_pushes: list[dict[str, object]] = []
		self.image_preflights: list[str] = []
		self.conflicting_image_repository: str | None = None
		self.files[(SOURCE, ".release/publisher.toml")] = (
			'''schema = 1
workflow = ".github/workflows/release.yml"
[[producers]]
name = "delivery"
job = "Verify delivery"
checks = ["Check runtime"]
deliverables = ["product-{version}"]
kind = "'''
			+ kind
			+ """"
"""
		)
		if kind == "image":
			self.files[(SOURCE, ".release/publisher.toml")] += (
				'image_repository = "ghcr.io/o/product"\n'
			)

	def build(self, candidate: dict[str, object]) -> None:
		content = b"verified delivery"
		identity: dict[str, object] = {
			"name": "product-2.1.0",
			"kind": self.kind,
		}
		if self.kind == "image":
			identity.update(
				{
					"archive": "image.tar",
					"archive_sha256": hashlib.sha256(content).hexdigest(),
				}
			)
		else:
			identity["sha256"] = hashlib.sha256(content).hexdigest()
		evidence = {
			"schema": 1,
			"repository": "o/r",
			"pr": 7,
			"source_sha": SOURCE,
			"version": "2.1.0",
			"provider_sha": PROVIDER,
			"run_id": 100,
			"attempt": 1,
			"candidate_sha256": hashlib.sha256(
				(
					json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
					+ "\n"
				).encode()
			).hexdigest(),
			"deliverables": [identity],
		}
		files = {"evidence.json": json.dumps(evidence).encode()}
		if self.kind == "image":
			files["image.tar"] = content
		else:
			files["product-2.1.0"] = content
		self.artifact("release-evidence-delivery-1", files, "Verify delivery", ["Check runtime"])

	def get(self, path: str) -> object:
		result = super().get(path)
		if path == "/actions/runs/100" and self.kind == "image":
			assert isinstance(result, dict)
			return {
				**result,
				"referenced_workflows": [
					*result["referenced_workflows"],
					{
						"path": f"o/provider/.github/workflows/image-writer.yml@{PROVIDER}",
						"sha": PROVIDER,
					},
				],
			}
		return result

	def publish_image_archive(
		self,
		content: bytes,
		source: str,
		repository: str,
		version: str,
		producer: str,
		*,
		expected_digest: str | None = None,
	) -> dict[str, str]:
		self.image_pushes.append({"source": source, "repository": repository, "version": version})
		assert content == b"verified delivery"
		assert source == SOURCE
		assert repository == "ghcr.io/o/product"
		assert version == "2.1.0"
		assert producer == "delivery"
		digest_value = hashlib.sha256(b"registry-manifest").hexdigest()
		return {
			"reference": f"{repository}@sha256:{digest_value}",
			"sha256": digest_value,
			"tag": f"{repository}:v{version}",
		}

	def preflight_image_archive(
		self,
		content: bytes,
		source: str,
		repository: str,
		version: str,
		producer: str,
		*,
		expected_digest: str | None = None,
	) -> None:
		self.image_preflights.append(repository)
		assert content == b"verified delivery"
		assert source == SOURCE
		assert version == "2.1.0"
		assert producer
		if repository == self.conflicting_image_repository:
			raise PublicationError("existing image tag points to different image bytes")

	def image_digest(self, reference: str) -> str:
		assert reference.startswith("ghcr.io/o/product@sha256:")
		return hashlib.sha256(
			b"registry-manifest" if self.available else b"substituted"
		).hexdigest()


class TagForbiddenRegistryGitHub(RegistryGitHub):
	def __init__(self) -> None:
		super().__init__("image")
		self.deny_tag_creation = True

	def post(self, path: str, payload: Mapping[str, object]) -> dict[str, object]:
		if path == "/git/refs" and self.deny_tag_creation:
			self.writes.append(path)
			raise PublicationError("GitHub denied tag creation (HTTP 403)")
		return super().post(path, payload)


class MultiImageRegistryGitHub(RegistryGitHub):
	def __init__(self) -> None:
		super().__init__("image")
		self.files[(SOURCE, ".release/publisher.toml")] = """schema = 1
workflow = ".github/workflows/release.yml"
[[producers]]
name = "first"
job = "Verify first image"
checks = ["Check runtime"]
deliverables = ["first-{version}"]
kind = "image"
image_repository = "ghcr.io/o/first"
[[producers]]
name = "second"
job = "Verify second image"
checks = ["Check runtime"]
deliverables = ["second-{version}"]
kind = "image"
image_repository = "ghcr.io/o/second"
"""

	def build(self, candidate: dict[str, object]) -> None:
		content = b"verified delivery"
		candidate_digest = hashlib.sha256(
			(
				json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
				+ "\n"
			).encode()
		).hexdigest()
		for producer, job in (("first", "Verify first image"), ("second", "Verify second image")):
			name = f"{producer}-2.1.0"
			identity = {
				"name": name,
				"kind": "image",
				"archive": f"{producer}.tar",
				"archive_sha256": hashlib.sha256(content).hexdigest(),
			}
			evidence = {
				"schema": 1,
				"repository": "o/r",
				"pr": 7,
				"source_sha": SOURCE,
				"version": "2.1.0",
				"provider_sha": PROVIDER,
				"run_id": 100,
				"attempt": 1,
				"candidate_sha256": candidate_digest,
				"deliverables": [identity],
			}
			self.artifact(
				f"release-evidence-{producer}-1",
				{
					"evidence.json": json.dumps(evidence).encode(),
					f"{producer}.tar": content,
				},
				job,
				["Check runtime"],
			)


REPAIRED_SOURCE = "f" * 40
REPAIRED_PROVIDER = "c" * 40
REPAIRED_RUN = 101
REPAIRED_REFERENCE = "o/provider/.github/workflows/release-prepare.yml@" + REPAIRED_PROVIDER


class RepairedGitHub(GitHub):
	def artifact(
		self,
		name: str,
		files: dict[str, bytes],
		job_name: str,
		checks: list[str],
		*,
		attempt: int = 1,
	) -> int:
		if "evidence.json" in files:
			evidence = json.loads(files["evidence.json"])
			evidence.update({"run_id": 101, "provider_sha": REPAIRED_PROVIDER})
			files = {**files, "evidence.json": json.dumps(evidence).encode()}
		artifact_id = super().artifact(name, files, job_name, checks, attempt=attempt)
		if "evidence.json" in files:
			self.artifacts[artifact_id]["workflow_run"] = {"id": 101, "head_sha": REPAIRED_SOURCE}
			self.jobs[-1].update({"run_id": 101, "head_sha": REPAIRED_SOURCE})
			job_id = int(self.jobs[-1]["id"])
			self.job_attempts[(job_id, attempt)] = copy.deepcopy(self.jobs[-1])
		return artifact_id

	def get(self, path: str) -> object:
		if path == "/actions/runs/101":
			run = super().get("/actions/runs/100")
			assert isinstance(run, dict)
			return {
				**run,
				"id": 101,
				"event": "workflow_dispatch",
				"head_branch": "main",
				"head_sha": REPAIRED_SOURCE,
				"referenced_workflows": [
					{
						"path": REPAIRED_REFERENCE,
						"sha": REPAIRED_PROVIDER,
					}
				],
			}
		if path.startswith("/actions/runs/101/jobs?"):
			jobs = copy.deepcopy([job for job in self.jobs if job["run_id"] == REPAIRED_RUN])
			return {"jobs": jobs, "total_count": len(jobs)}
		if path.startswith("/actions/runs/101/attempts/"):
			attempt = int(path.split("/attempts/", 1)[1].split("/", 1)[0])
			jobs = [
				copy.deepcopy(job)
				for (_job_id, job_attempt), job in self.job_attempts.items()
				if job_attempt == attempt and job.get("run_id") == REPAIRED_RUN
			]
			return {"jobs": jobs, "total_count": len(jobs)}
		if path.startswith("/actions/runs/101/artifacts?"):
			artifacts = copy.deepcopy(
				[
					artifact
					for artifact in self.artifacts.values()
					if artifact["workflow_run"] == {"id": 101, "head_sha": REPAIRED_SOURCE}
				]
			)
			return {"artifacts": artifacts, "total_count": len(artifacts)}
		return super().get(path)


class PublicationFailureGitHub(GitHub):
	def __init__(self, failure: str) -> None:
		super().__init__()
		self.failure = failure

	def post(self, path: str, payload: Mapping[str, object]) -> dict[str, object]:
		release = super().post(path, payload)
		if path == "/releases" and self.failure == "draft-source":
			release["target_commitish"] = BASE
		return release

	def upload(self, release_id: int, name: str, content: bytes) -> dict[str, object]:
		asset = super().upload(release_id, name, content)
		if self.failure == "upload-digest":
			asset["digest"] = "sha256:" + "0" * 64
		elif self.failure == "missing-ready-asset":
			self.releases[-1]["assets"] = []
		return asset


class BaselineGitHub(GitHub):
	def __init__(self, scenario: str) -> None:
		super().__init__()
		self.scenario = scenario

	def get(self, path: str) -> object:
		if path == "/git/ref/tags/v2.0.6":
			return {"object": {"type": "tag", "sha": "c" * 40}}
		if path == "/git/tags/" + "c" * 40:
			return {"object": {"type": "commit", "sha": BASE}}
		if self.scenario == "later-page":
			if path == "/releases?per_page=100&page=1":
				return self.releases * 100
			if path == "/releases?per_page=100&page=2":
				return [{"tag_name": "v2.2.0", "draft": False, "prerelease": False}]
		return super().get(path)
