"""Controlled GitHub service used by publisher tests."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import zipfile
from collections.abc import Mapping

from quality_gate.publication import PublicationError

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
		self.pull_files: list[dict[str, object]] = []
		self.pull_file_pages: list[int] = []
		self.pull_after_file_read: dict[str, object] | None = None
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
		self.source_reads: list[tuple[str, str]] = []
		self.uploads: dict[str, bytes] = {}
		self.artifacts: dict[int, dict[str, object]] = {}
		self.archives: dict[int, bytes] = {}
		self.jobs: list[dict[str, object]] = []
		self.job_attempts: dict[tuple[int, int], dict[str, object]] = {}
		self.logs: dict[int, bytes] = {}
		self.fail_upload = ""
		self.fail_publish = False
		self.run_overrides: dict[str, object] = {}
		self.incomplete_lists: dict[str, int] = {}
		self.incomplete_artifact_metadata: dict[int, int] = {}
		self.incomplete_run_metadata = 0
		self.incomplete_job_steps = 0
		self.incomplete_job_conclusion = 0

	def artifact(
		self,
		name: str,
		files: dict[str, bytes],
		job_name: str,
		checks: list[str],
		*,
		attempt: int = 1,
	) -> int:
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
		job = {
			"id": job_id,
			"runner_id": 1234,
			"run_id": 100,
			"run_attempt": attempt,
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
		self.jobs.append(job)
		self.job_attempts[(job_id, attempt)] = copy.deepcopy(job)
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

	def build(self, candidate: dict[str, object], *, attempt: int = 1) -> None:
		for platform in ["linux", "windows"]:
			name = f"package-{platform}-2.1.0.zip"
			content = platform.encode() + (b"" if attempt == 1 else f"-attempt-{attempt}".encode())
			evidence = {
				"schema": 1,
				"repository": "o/r",
				"pr": 7,
				"source_sha": SOURCE,
				"version": "2.1.0",
				"provider_sha": PROVIDER,
				"run_id": 100,
				"attempt": attempt,
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
				f"release-evidence-{platform}-{attempt}",
				{"evidence.json": json.dumps(evidence).encode(), name: content},
				f"Build {platform}",
				["Verify package"],
				attempt=attempt,
			)

	def _get_jobs(self) -> dict[str, object]:
		jobs = copy.deepcopy(self.jobs)
		if self.incomplete_lists.get("jobs", 0):
			self.incomplete_lists["jobs"] -= 1
			jobs = jobs[:-1]
		if self.incomplete_job_steps:
			self.incomplete_job_steps -= 1
			linux = next(job for job in jobs if job.get("name") == "Build linux")
			linux.pop("steps")
		if self.incomplete_job_conclusion:
			self.incomplete_job_conclusion -= 1
			linux = next(job for job in jobs if job.get("name") == "Build linux")
			linux["conclusion"] = None
		return {"jobs": jobs, "total_count": len(self.jobs)}

	def _get_actions(self, path: str) -> object:
		if (
			path.startswith("/actions/artifacts?")
			or path == "/actions/runs/100/artifacts?per_page=100&page=1"
		):
			artifacts = copy.deepcopy(list(self.artifacts.values()))
			if self.incomplete_lists.get("artifacts", 0):
				self.incomplete_lists["artifacts"] -= 1
				artifacts = artifacts[:-1]
			return {
				"artifacts": artifacts,
				"total_count": len(self.artifacts),
			}
		if path.startswith("/actions/artifacts/"):
			artifact_id = int(path.rsplit("/", 1)[1])
			artifact = copy.deepcopy(self.artifacts[artifact_id])
			if self.incomplete_artifact_metadata.get(artifact_id, 0):
				self.incomplete_artifact_metadata[artifact_id] -= 1
				artifact.pop("workflow_run")
			return artifact
		if path.startswith("/actions/runs/100/jobs?"):
			return self._get_jobs()
		if path.startswith("/actions/runs/100/attempts/"):
			attempt = int(path.split("/attempts/", 1)[1].split("/", 1)[0])
			jobs = [
				copy.deepcopy(job)
				for (job_id, job_attempt), job in self.job_attempts.items()
				if job_attempt == attempt
			]
			return {"jobs": jobs, "total_count": len(jobs)}
		if path == "/actions/runs/100":
			run = {
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
			if self.incomplete_run_metadata:
				self.incomplete_run_metadata -= 1
				run.pop("run_attempt")
			return run
		raise AssertionError(f"unexpected API read: {path}")

	def get(self, path: str) -> object:
		if path.startswith("/actions/"):
			return self._get_actions(path)
		if path == "/":
			return {"full_name": "o/r", "default_branch": "main", "owner": {"login": "o"}}
		if path.startswith("/pulls/7/files?"):
			page = int(path.rsplit("page=", 1)[1])
			self.pull_file_pages.append(page)
			start = (page - 1) * 100
			files = copy.deepcopy(self.pull_files[start : start + 100])
			if self.pull_after_file_read is not None:
				self.pull.update(self.pull_after_file_read)
				self.pull_after_file_read = None
			return files
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
		self.source_reads.append((sha, path))
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
		if payload.get("draft") is False and self.fail_publish:
			raise PublicationError("injected interrupted publication")
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
