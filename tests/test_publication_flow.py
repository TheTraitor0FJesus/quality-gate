"""Shared publisher behavior with a controlled GitHub service and real collaborators."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import zipfile
from collections.abc import Mapping

import pytest

from quality_gate.publication import PublicationError
from quality_gate.publication_flow import Context, Publisher

SOURCE = "a" * 40
PR_HEAD = "e" * 40
BASE = "d" * 40
PROVIDER = "b" * 40
ORIGINAL_RUN = 100
BODY = (
	"## Release\n- Version: 2.1.0\n- Changes: Общая публикация.\n"
	"- Publication: owner merge authorizes publication after the merged commit "
	"passes the required release checks.\n"
)
NOTES = "Общий механизм публикации. Существующие пакеты сохраняются.\n"
CONFIG = """schema = 1
workflow = ".github/workflows/release.yml"
[[producers]]
name = "linux"
job = "Build linux"
checks = ["Verify package"]
deliverables = ["package-linux-{version}.zip"]
[[producers]]
name = "windows"
job = "Build windows"
checks = ["Verify package"]
deliverables = ["package-windows-{version}.zip"]
"""


class GitHub:
	"""In-memory external service; publication mutations remain directly observable."""

	def __init__(self) -> None:
		self.pull: dict[str, object] = {
			"number": 7,
			"merged": True,
			"merged_at": "2026-09-14T10:00:00Z",
			"merge_commit_sha": SOURCE,
			"merged_by": {"login": "o"},
			"body": BODY,
			"base": {"ref": "main", "sha": BASE, "repo": {"full_name": "o/r"}},
			"head": {"sha": PR_HEAD},
		}
		self.files = {
			(SOURCE, ".release/version.toml"): 'version = "2.1.0"\n',
			(BASE, ".release/version.toml"): 'version = "2.0.6"\n',
			(SOURCE, ".release/notes.md"): NOTES,
			(SOURCE, ".release/publisher.toml"): CONFIG,
		}
		self.releases: list[dict[str, object]] = [
			{"id": 1, "tag_name": "v2.0.6", "draft": False, "prerelease": False}
		]
		self.tags = {"v2.0.6": BASE}
		self.writes: list[str] = []
		self.uploads: dict[str, bytes] = {}
		self.artifacts: dict[int, dict[str, object]] = {}
		self.archives: dict[int, bytes] = {}
		self.jobs: list[dict[str, object]] = []
		self.logs: dict[int, bytes] = {}
		self.fail_upload = ""
		self.run_overrides: dict[str, object] = {}

	def artifact(self, name: str, files: dict[str, bytes], job_name: str, checks: list[str]) -> int:
		artifact_id = 300 + len(self.artifacts)
		job_id = 200 + len(self.jobs)
		output = io.BytesIO()
		with zipfile.ZipFile(output, "w") as archive:
			for filename, content in files.items():
				archive.writestr(filename, content)
		content = output.getvalue()
		digest = hashlib.sha256(content).hexdigest()
		self.archives[artifact_id] = content
		self.artifacts[artifact_id] = {
			"id": artifact_id,
			"name": name,
			"expired": False,
			"digest": "sha256:" + digest,
			"created_at": "2026-09-14T10:09:00Z",
			"workflow_run": {"id": 100, "head_sha": PR_HEAD},
		}
		self.jobs.append(
			{
				"id": job_id,
				"run_id": 100,
				"run_attempt": 1,
				"head_sha": PR_HEAD,
				"name": job_name,
				"status": "completed",
				"conclusion": "success",
				"started_at": "2026-09-14T10:00:00Z",
				"completed_at": "2026-09-14T10:10:00Z",
				"steps": [
					{"name": step, "conclusion": "success"}
					for step in [*checks, "Upload release evidence"]
				],
			}
		)
		self.logs[job_id] = (
			f"Artifact ID: {artifact_id}\nSHA256 digest of uploaded artifact zip is {digest}\n"
		).encode()
		return artifact_id

	def persist(self, candidate: dict[str, object]) -> int:
		return self.artifact(
			"release-candidate-pr-7-100-1",
			{"candidate.json": json.dumps(candidate).encode()},
			"Prepare release / Prepare candidate",
			["Prepare candidate"],
		)

	def build(self, candidate: dict[str, object]) -> None:
		for platform in ["linux", "windows"]:
			name = f"package-{platform}-2.1.0.zip"
			content = platform.encode()
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
						json.dumps(
							candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":")
						)
						+ "\n"
					).encode()
				).hexdigest(),
				"deliverables": [
					{"kind": "archive", "name": name, "sha256": hashlib.sha256(content).hexdigest()}
				],
			}
			self.artifact(
				f"release-evidence-{platform}-1",
				{"evidence.json": json.dumps(evidence).encode(), name: content},
				f"Build {platform}",
				["Verify package"],
			)

	def get(self, path: str) -> object:
		if (
			path.startswith("/actions/artifacts?")
			or path == "/actions/runs/100/artifacts?per_page=100&page=1"
		):
			return {
				"artifacts": copy.deepcopy(list(self.artifacts.values())),
				"total_count": len(self.artifacts),
			}
		if path.startswith("/actions/artifacts/"):
			return copy.deepcopy(self.artifacts[int(path.rsplit("/", 1)[1])])
		if path.startswith("/actions/runs/100/jobs?"):
			return {"jobs": copy.deepcopy(self.jobs), "total_count": len(self.jobs)}
		if path == "/actions/runs/100":
			return {
				"id": 100,
				"head_sha": PR_HEAD,
				"path": ".github/workflows/release.yml",
				"event": "pull_request",
				"run_attempt": 1,
				"repository": {"full_name": "o/r"},
				"head_repository": {"full_name": "o/r"},
				"referenced_workflows": [
					{
						"path": f"o/provider/.github/workflows/release-prepare.yml@{PROVIDER}",
						"sha": PROVIDER,
					}
				],
				**self.run_overrides,
			}
		if path == "/":
			return {"full_name": "o/r", "default_branch": "main", "owner": {"login": "o"}}
		if path == "/pulls/7":
			return copy.deepcopy(self.pull)
		if path == "/releases?per_page=100&page=1":
			return copy.deepcopy(self.releases)
		if path.startswith("/git/ref/tags/"):
			tag = path.removeprefix("/git/ref/tags/")
			return (
				{"object": {"type": "commit", "sha": self.tags[tag]}} if tag in self.tags else None
			)
		if path.startswith("/releases/"):
			return copy.deepcopy(
				next(item for item in self.releases if item["id"] == int(path.rsplit("/", 1)[1]))
			)
		raise AssertionError(f"unexpected API read: {path}")

	def source(self, sha: str, path: str) -> str:
		return self.files[(sha, path)]

	def post(self, path: str, payload: Mapping[str, object]) -> dict[str, object]:
		self.writes.append(path)
		if path == "/git/refs":
			self.tags[str(payload["ref"]).removeprefix("refs/tags/")] = str(payload["sha"])
			return dict(payload)
		assert path == "/releases"
		release = {**payload, "id": 2, "assets": [], "immutable": False}
		self.releases.append(release)
		return copy.deepcopy(release)

	def patch(self, path: str, payload: Mapping[str, object]) -> dict[str, object]:
		self.writes.append(path)
		release = next(item for item in self.releases if item["id"] == int(path.rsplit("/", 1)[1]))
		release.update(payload)
		return copy.deepcopy(release)

	def upload(self, release_id: int, name: str, content: bytes) -> dict[str, object]:
		if name == self.fail_upload:
			raise PublicationError("injected interrupted upload")
		self.writes.append(name)
		self.uploads[name] = content
		asset: dict[str, object] = {
			"name": name,
			"digest": "sha256:" + hashlib.sha256(content).hexdigest(),
		}
		release = next(item for item in self.releases if item["id"] == release_id)
		assets = release["assets"]
		assert isinstance(assets, list)
		assets.append(asset)
		return asset

	def download(self, path: str) -> bytes:
		if path.startswith("/actions/artifacts/"):
			return self.archives[int(path.split("/")[3])]
		if path.startswith("/actions/jobs/"):
			return self.logs[int(path.split("/")[3])]
		raise AssertionError(f"unexpected download: {path}")

	def image_digest(self, reference: str) -> str:
		raise AssertionError(f"unexpected image: {reference}")


def _publisher(api: GitHub) -> Publisher:
	return Publisher(
		api,
		Context(
			repository="o/r",
			provider_repository="o/provider",
			provider_sha=PROVIDER,
			helper_sha=PROVIDER,
			run_id=100,
			attempt=1,
			run_source_sha=SOURCE,
			run_head_sha=PR_HEAD,
		),
	)


def _event(api: GitHub) -> dict[str, object]:
	return {"action": "closed", "pull_request": copy.deepcopy(api.pull)}


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


def test_post_merge_no_release_ignores_old_patch_notes() -> None:
	api = GitHub()
	api.pull["body"] = "## Release\n- No release: Только инструкции.\n"
	api.files[(SOURCE, ".release/version.toml")] = 'version = "2.0.6"\n'
	api.files[(SOURCE, ".release/notes.md")] = "Impact: PATCH\nVersion: 2.0.6\n"
	result = _publisher(api).prepare(7, event=_event(api))
	assert result["status"] == "no-release"
	assert "candidate" not in result
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


def _ready(api: GitHub) -> tuple[Publisher, int]:
	publisher = _publisher(api)
	candidate = publisher.prepare(7, event=_event(api))["candidate"]
	assert isinstance(candidate, dict)
	artifact_id = api.persist(candidate)
	api.build(candidate)
	return publisher, artifact_id


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


class RegistryGitHub(GitHub):
	def __init__(self, kind: str) -> None:
		super().__init__()
		self.kind = kind
		self.available = True
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

	def build(self, candidate: dict[str, object]) -> None:
		content = b"verified delivery"
		identity = {
			"name": "product-2.1.0",
			"kind": self.kind,
			"sha256": hashlib.sha256(content).hexdigest(),
		}
		if self.kind == "image":
			identity["reference"] = "ghcr.io/o/product@sha256:" + identity["sha256"]
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
		if self.kind != "image":
			files["product-2.1.0"] = content
		self.artifact("release-evidence-delivery-1", files, "Verify delivery", ["Check runtime"])

	def image_digest(self, reference: str) -> str:
		assert reference.startswith("ghcr.io/o/product@sha256:")
		return hashlib.sha256(
			b"verified delivery" if self.available else b"substituted"
		).hexdigest()


@pytest.mark.parametrize("kind", ["image", "package"])
def test_shared_protocol_publishes_and_reads_back_other_delivery_kinds(kind: str) -> None:
	api = RegistryGitHub(kind)
	publisher, artifact_id = _ready(api)
	assert publisher.publish(7, artifact_id)["status"] == "published"
	assert len(api.uploads) == (0 if kind == "image" else 1)
	before = copy.deepcopy(api.writes)
	assert publisher.prepare(7, event=_event(api))["status"] == "already-complete"
	assert api.writes == before
	if kind == "image":
		api.available = False
		with pytest.raises(PublicationError, match="digest"):
			publisher.prepare(7, event=_event(api))
		assert api.writes == before


def test_source_contract_prevents_replacing_archive_with_image() -> None:
	api = RegistryGitHub("image")
	api.files[(SOURCE, ".release/publisher.toml")] = api.files[
		(SOURCE, ".release/publisher.toml")
	].replace('kind = "image"', 'kind = "archive"')
	publisher, artifact_id = _ready(api)
	with pytest.raises(PublicationError, match="kind"):
		publisher.publish(7, artifact_id)
	assert api.writes == []


REPAIRED_SOURCE = "f" * 40
REPAIRED_PROVIDER = "c" * 40
REPAIRED_RUN = 101
REPAIRED_REFERENCE = "o/provider/.github/workflows/release-prepare.yml@" + REPAIRED_PROVIDER


class RepairedGitHub(GitHub):
	def artifact(self, name: str, files: dict[str, bytes], job_name: str, checks: list[str]) -> int:
		if "evidence.json" in files:
			evidence = json.loads(files["evidence.json"])
			evidence.update({"run_id": 101, "provider_sha": REPAIRED_PROVIDER})
			files = {**files, "evidence.json": json.dumps(evidence).encode()}
		artifact_id = super().artifact(name, files, job_name, checks)
		if "evidence.json" in files:
			self.artifacts[artifact_id]["workflow_run"] = {"id": 101, "head_sha": REPAIRED_SOURCE}
			self.jobs[-1].update({"run_id": 101, "head_sha": REPAIRED_SOURCE})
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
			return {
				"jobs": copy.deepcopy([job for job in self.jobs if job["run_id"] == REPAIRED_RUN])
			}
		if path.startswith("/actions/runs/101/artifacts?"):
			return {
				"artifacts": copy.deepcopy(
					[
						artifact
						for artifact in self.artifacts.values()
						if artifact["workflow_run"] == {"id": 101, "head_sha": REPAIRED_SOURCE}
					]
				)
			}
		return super().get(path)


def test_repaired_provider_verifies_original_source_in_a_new_run() -> None:
	api = RepairedGitHub()
	original = _publisher(api).prepare(7, event=_event(api))["candidate"]
	assert isinstance(original, dict)
	artifact_id = api.persist(original)
	publisher = Publisher(
		api,
		Context(
			repository="o/r",
			provider_repository="o/provider",
			provider_sha=REPAIRED_PROVIDER,
			helper_sha=REPAIRED_PROVIDER,
			run_id=101,
			attempt=1,
			run_source_sha=REPAIRED_SOURCE,
			run_head_sha=REPAIRED_SOURCE,
		),
	)
	assert publisher.recover(7)["candidate"] == original
	api.build(original)
	assert publisher.publish(7, artifact_id)["source_sha"] == SOURCE
	assert api.tags["v2.1.0"] == SOURCE
	assert original["provider_sha"] == PROVIDER
	assert REPAIRED_PROVIDER in str(api.releases[-1]["body"])


def test_unrelated_pull_request_run_cannot_publish_an_old_candidate() -> None:
	api = GitHub()
	_, artifact_id = _ready(api)
	publisher = Publisher(
		api,
		Context(
			repository="o/r",
			provider_repository="o/provider",
			provider_sha=PROVIDER,
			helper_sha=PROVIDER,
			run_id=100,
			attempt=1,
			run_source_sha=REPAIRED_SOURCE,
			run_head_sha=PR_HEAD,
		),
	)
	with pytest.raises(PublicationError):
		publisher.publish(7, artifact_id)
	assert api.writes == []


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


@pytest.mark.parametrize("failure", ["draft-source", "upload-digest", "missing-ready-asset"])
def test_failed_draft_readiness_never_promotes_a_public_release(failure: str) -> None:
	api = PublicationFailureGitHub(failure)
	publisher, artifact_id = _ready(api)
	with pytest.raises(PublicationError):
		publisher.publish(7, artifact_id)
	assert api.releases[-1]["draft"] is True
	assert "/releases/2" not in api.writes
	if failure == "draft-source":
		assert api.uploads == {}


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


def test_annotated_baseline_is_accepted_through_preparation() -> None:
	api = BaselineGitHub("annotated")
	assert _publisher(api).prepare(7, event=_event(api))["status"] == "build-required"
	assert api.writes == []


def test_stable_baseline_beyond_first_page_rejects_stale_candidate() -> None:
	api = BaselineGitHub("later-page")
	api.tags["v2.2.0"] = BASE
	with pytest.raises(PublicationError, match="stale"):
		_publisher(api).prepare(7, event=_event(api))
	assert api.writes == []
