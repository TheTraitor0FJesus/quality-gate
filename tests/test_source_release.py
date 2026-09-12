from __future__ import annotations

import hashlib
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from urllib.request import Request

import pytest
from scripts import source_release
from scripts.source_release import (
	ArtifactIdentity,
	GitHubNotFound,
	ReleaseController,
	ReleaseError,
	ReleaseIntent,
	load_release_intent,
	load_release_timeout,
	validate_latest_release,
	validate_owner_merge,
	validate_version_projections,
)

SOURCE_SHA = "a" * 40
OTHER_SHA = "b" * 40
BASELINE_SHA = "c" * 40
REPOSITORY = Path(__file__).resolve().parents[1]


class FakeGitHub:
	def __init__(self, values: dict[str, object]) -> None:
		self.values = deepcopy(values)
		self.uploaded: list[str] = []
		self.patched: list[dict[str, object]] = []

	def _stored_releases(self) -> list[dict[str, object]]:
		result: list[dict[str, object]] = []
		for value in self.values.values():
			if isinstance(value, dict) and value.get("id") == 7:
				result.append(value)
			elif isinstance(value, list):
				result.extend(
					item
					for item in value
					if isinstance(item, dict) and item.get("id") == 7
				)
		return result

	def get(self, path: str) -> object:
		if path not in self.values:
			if path.endswith("/immutable-releases"):
				return {"enabled": True, "enforced_by_owner": False}
			if path.endswith("/git/ref/tags/v2.0.5"):
				return {"object": {"sha": BASELINE_SHA, "type": "commit"}}
			if path.endswith(f"/git/tags/{BASELINE_SHA}"):
				return {"object": {"sha": BASELINE_SHA, "type": "commit"}}
			raise GitHubNotFound(f"missing fake response: {path}")
		return self.values[path]

	def post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
		prefix = path.removesuffix("/releases")
		release = {
			"id": 7,
			"upload_url": "https://uploads.example/releases/7/assets{?name,label}",
			"assets": [],
			"immutable": False,
			**payload,
		}
		if payload.get("draft") is True:
			releases = self.values.setdefault(f"{prefix}/releases?per_page=100", [])
			assert isinstance(releases, list)
			releases.insert(0, release)
		else:
			self.values[f"{prefix}/releases/tags/{payload['tag_name']}"] = release
			self.values[f"{prefix}/git/ref/tags/{payload['tag_name']}"] = {
				"object": {"sha": payload["target_commitish"], "type": "commit"}
			}
		return release

	def patch(self, path: str, payload: dict[str, object]) -> dict[str, object]:
		self.patched.append(payload)
		release = next(iter(self._stored_releases()), None)
		if release is not None:
			release.update(payload)
			if payload.get("draft") is False:
				release["immutable"] = True
			prefix = path.rsplit("/releases/", 1)[0]
			tag = release.get("tag_name")
			if isinstance(tag, str):
				self.values[f"{prefix}/releases/tags/{tag}"] = release
				self.values[f"{prefix}/git/ref/tags/{tag}"] = {
					"object": {"sha": release["target_commitish"], "type": "commit"}
				}
		return {"id": 7, **payload}

	def upload(self, upload_url: str, name: str, content: bytes) -> dict[str, object]:
		self.uploaded.append(name)
		asset = {"name": name, "digest": f"sha256:{hashlib.sha256(content).hexdigest()}"}
		release = next(iter(self._stored_releases()), None)
		if release is not None:
			assets = release.setdefault("assets", [])
			assert isinstance(assets, list)
			assets.append(asset)
		return asset


class MismatchingUploadGitHub(FakeGitHub):
	def upload(self, upload_url: str, name: str, content: bytes) -> dict[str, object]:
		asset = super().upload(upload_url, name, content)
		asset["digest"] = "sha256:" + ("0" * 64)
		return asset


class MismatchingDraftSourceGitHub(FakeGitHub):
	def post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
		release = super().post(path, payload)
		release["target_commitish"] = OTHER_SHA
		return release


def _root(root: Path) -> None:
	(root / ".release").mkdir()
	(root / ".release" / "version.toml").write_text('version = "2.0.6"\n', encoding="utf-8")
	(root / ".release" / "release-tools.toml").write_text(
		"""[timeouts]
github_api_seconds = 60
external_download_seconds = 120
subprocess_seconds = 900
check_wait_seconds = 900
check_poll_seconds = 10
""",
		encoding="utf-8",
	)
	(root / ".release" / "notes.md").write_text(
		"""# Quality Gate v2.0.6

Version: 2.0.6
Impact: PATCH
Changes: owner-merged immutable package publication.
Required adaptation: no consumer pin changes are required.

## Interface
The CLI remains compatible with the v2 contract.

## Integrations
The reusable workflow remains SHA-pinned.

## Configuration
The policy manifest projection follows the version authority.

## Persisted data
N/A — the package has no persisted runtime data.

## Delivery/runtime
Linux and Windows package assets remain the delivery format.
""",
		encoding="utf-8",
	)
	(root / "pyproject.toml").write_text('[project]\nversion = "2.0.6"\n', encoding="utf-8")
	(root / "quality-gate.toml").write_text(
		'[quality]\npolicy_release = "v2.0.6"\n', encoding="utf-8"
	)
	(root / "quality_gate").mkdir()
	(root / "quality_gate" / "__init__.py").write_text('__version__ = "2.0.6"\n', encoding="utf-8")
	(root / ".github" / "workflows").mkdir(parents=True)
	(root / ".github" / "workflows" / "quality.yml").write_text(
		"quality-gate sync --source release --version $QUALITY_GATE_RELEASE\n",
		encoding="utf-8",
	)
	(root / "templates").mkdir()
	(root / "templates" / "quality-gate.toml").write_text(
		'policy_release = "v2.0.6"\n', encoding="utf-8"
	)


def _pull_request(*, source_sha: str = SOURCE_SHA) -> dict[str, object]:
	return {
		"number": 12,
		"base": {"ref": "main"},
		"merged_at": "2026-09-10T10:00:00Z",
		"merge_commit_sha": source_sha,
		"merged_by": {"login": "TheTraitor0FJesus"},
		"body": """## Release

- Version: v2.0.5 -> v2.0.6 (PATCH; release automation).
- Changes: owner-merged immutable package publication. Required adaptation: no consumer pin changes are required.
- Publication: owner merge authorizes publication after the merged commit passes the required release checks.
""",
	}


def _artifact(
	platform: str,
	name: str = "asset.zip",
	*,
	source_sha: str = SOURCE_SHA,
	version: str = "2.0.6",
	path: str | None = None,
	root: Path | None = None,
) -> ArtifactIdentity:
	if path is None and root is not None:
		path = str(root / name)
		Path(path).write_bytes(platform.encode())
	return ArtifactIdentity(
		version=version,
		source_sha=source_sha,
		platform=platform,
		name=name,
		sha256=hashlib.sha256(platform.encode()).hexdigest(),
		path=path,
	)


def _controller(root: Path, api: FakeGitHub, *, repository: str = "o/r") -> ReleaseController:
	return ReleaseController(
		root,
		api,
		repository=repository,
		owner="TheTraitor0FJesus",
		source_sha_resolver=lambda _root: SOURCE_SHA,
	)


def test_version_authority_and_all_projections_are_validated(tmp_path: Path) -> None:
	_root(tmp_path)

	intent = load_release_intent(tmp_path)
	assert intent.version == "2.0.6"
	assert validate_version_projections(tmp_path).version == "2.0.6"

	(tmp_path / "quality_gate" / "__init__.py").write_text(
		'__version__ = "2.0.5"\n', encoding="utf-8"
	)
	with pytest.raises(ReleaseError, match="runtime version"):
		validate_version_projections(tmp_path)


def test_owner_merge_requires_authorization_for_the_exact_source() -> None:
	intent = type("Intent", (), {"version": "2.0.6", "impact": "PATCH"})()
	assert validate_owner_merge(
		[_pull_request()],
		intent,
		source_sha=SOURCE_SHA,
		owner="TheTraitor0FJesus",
		default_branch="main",
	)["number"] == 12

	for mutation in (
		lambda pr: pr.update(merged_at=None),
		lambda pr: pr.update(merged_by={"login": "another-user"}),
		lambda pr: pr.update(merge_commit_sha=OTHER_SHA),
		lambda pr: pr.update(body="approved but no release declaration"),
		lambda pr: pr.update(
			body=str(pr["body"]).replace(
				"Publication: owner merge authorizes publication after the merged commit passes the required release checks.",
				"Publication: owner merge authorizes publication after the merged commit passes the required release checks",
			)
		),
	):
		candidate = _pull_request()
		mutation(candidate)
		with pytest.raises(ReleaseError):
			validate_owner_merge(
				[candidate],
				intent,
				source_sha=SOURCE_SHA,
				owner="TheTraitor0FJesus",
				default_branch="main",
			)


def test_matching_completed_publication_is_idempotent(tmp_path: Path) -> None:
	_root(tmp_path)
	api = FakeGitHub(
		{
			f"/repos/TheTraitor0FJesus/quality-gate/commits/{SOURCE_SHA}/pulls": [_pull_request()],
			"/repos/TheTraitor0FJesus/quality-gate/releases?per_page=100": [
				{"tag_name": "v2.0.6", "draft": False, "prerelease": False},
				{"tag_name": "v2.0.5", "draft": False, "prerelease": False},
			],
			f"/repos/TheTraitor0FJesus/quality-gate/commits/{SOURCE_SHA}/check-runs?per_page=100": {
				"check_runs": [{"name": "Quality Gate", "status": "completed", "conclusion": "success"}]
			},
			f"/repos/TheTraitor0FJesus/quality-gate/commits/{SOURCE_SHA}/status": {
				"state": "success"
			},
			"/repos/TheTraitor0FJesus/quality-gate/releases/tags/v2.0.6": {
				"tag_name": "v2.0.6",
				"draft": False,
				"prerelease": False,
				"immutable": True,
				"body": (
					(tmp_path / ".release" / "notes.md").read_text(encoding="utf-8").rstrip()
					+ f"\n\nCandidate source revision: `{SOURCE_SHA}`.\n"
				),
				"assets": [
					{"name": "linux.zip", "digest": f"sha256:{_artifact('linux').sha256}"},
					{"name": "windows.zip", "digest": f"sha256:{_artifact('windows').sha256}"},
				],
			},
			"/repos/TheTraitor0FJesus/quality-gate/git/ref/tags/v2.0.6": {
				"object": {"sha": SOURCE_SHA, "type": "commit"}
			},
		}
	)
	controller = _controller(tmp_path, api, repository="TheTraitor0FJesus/quality-gate")

	result = controller.publish(
		SOURCE_SHA,
		[_artifact("linux", "linux.zip", root=tmp_path), _artifact("windows", "windows.zip", root=tmp_path)],
	)

	assert result.status == "already-complete"
	assert api.uploaded == []


def test_failed_check_and_conflicting_publication_are_rejected(tmp_path: Path) -> None:
	_root(tmp_path)
	api = FakeGitHub(
		{
			f"/repos/o/r/commits/{SOURCE_SHA}/pulls": [_pull_request(source_sha=SOURCE_SHA)],
			"/repos/o/r/releases?per_page=100": [
				{"tag_name": "v2.0.5", "draft": False, "prerelease": False}
			],
			f"/repos/o/r/commits/{SOURCE_SHA}/check-runs?per_page=100": {
				"check_runs": [{"name": "Quality Gate", "status": "completed", "conclusion": "failure"}]
			},
			f"/repos/o/r/commits/{SOURCE_SHA}/status": {"state": "failure"},
			"/repos/o/r/releases/tags/v2.0.6": {
				"tag_name": "v2.0.6",
				"draft": False,
				"prerelease": False,
				"immutable": True,
				"assets": [],
			},
			"/repos/o/r/git/ref/tags/v2.0.6": {
				"object": {"sha": OTHER_SHA, "type": "commit"}
			},
		}
	)
	controller = _controller(tmp_path, api)

	with pytest.raises(ReleaseError, match="check"):
		controller.verify(SOURCE_SHA)

	api.values[f"/repos/o/r/commits/{SOURCE_SHA}/check-runs?per_page=100"] = {
		"check_runs": [{"name": "Quality Gate", "status": "completed", "conclusion": "success"}]
	}
	api.values[f"/repos/o/r/commits/{SOURCE_SHA}/status"] = {"state": "success"}
	with pytest.raises(ReleaseError, match="conflicting"):
		controller.publish(
			SOURCE_SHA,
			[_artifact("linux", "linux.zip", root=tmp_path), _artifact("windows", "windows.zip", root=tmp_path)],
		)


def test_no_release_and_stale_intents_have_no_publishable_baseline() -> None:
	intent = ReleaseIntent("2.0.6", "PATCH", "reviewed notes")

	with pytest.raises(ReleaseError, match="baseline"):
		validate_latest_release([], intent)
	with pytest.raises(ReleaseError, match="stale"):
		validate_latest_release([{"tag_name": "v2.0.4", "draft": False, "prerelease": False}], intent)
	with pytest.raises(ReleaseError, match="conflicting"):
		validate_latest_release([{"tag_name": "v2.0.6", "draft": False, "prerelease": False}], intent)
	assert validate_latest_release(
		[{"tag_name": "v2.0.5", "draft": False, "prerelease": False}],
		ReleaseIntent("2.0.5", "NONE", "internal-only notes"),
	) == "v2.0.5"


def test_no_release_decision_is_verified_without_publication(tmp_path: Path) -> None:
	_root(tmp_path)
	notes = (tmp_path / ".release" / "notes.md").read_text(encoding="utf-8").replace("Impact: PATCH", "Impact: NONE")
	(tmp_path / ".release" / "notes.md").write_text(notes, encoding="utf-8")
	no_release = _pull_request()
	no_release["body"] = "## Release\n\n- No release: internal-only documentation maintenance.\n"
	api = FakeGitHub(
		{
			f"/repos/o/r/commits/{SOURCE_SHA}/pulls": [no_release],
			"/repos/o/r/releases?per_page=100": [
				{"tag_name": "v2.0.5", "draft": False, "prerelease": False}
			],
			f"/repos/o/r/commits/{SOURCE_SHA}/check-runs?per_page=100": {
				"check_runs": [{"name": "Quality Gate", "status": "completed", "conclusion": "success"}]
			},
			f"/repos/o/r/commits/{SOURCE_SHA}/status": {"state": "success"},
		}
	)
	controller = _controller(tmp_path, api)

	assert controller.verify(SOURCE_SHA).status == "no-release"
	assert controller.publish(SOURCE_SHA, []).status == "no-release"
	assert "/repos/o/r/releases/tags/v2.0.6" not in api.values


def test_published_baseline_tag_must_resolve_to_a_full_source_sha(tmp_path: Path) -> None:
	_root(tmp_path)
	api = FakeGitHub(
		{
			f"/repos/o/r/commits/{SOURCE_SHA}/pulls": [_pull_request(source_sha=SOURCE_SHA)],
			"/repos/o/r/releases?per_page=100": [
				{"tag_name": "v2.0.5", "draft": False, "prerelease": False}
			],
			f"/repos/o/r/commits/{SOURCE_SHA}/check-runs?per_page=100": {
				"check_runs": [{"name": "Quality Gate", "status": "completed", "conclusion": "success"}]
			},
			f"/repos/o/r/commits/{SOURCE_SHA}/status": {"state": "success"},
			"/repos/o/r/git/ref/tags/v2.0.5": {"object": {"sha": "short", "type": "commit"}},
		}
	)
	controller = _controller(tmp_path, api)

	with pytest.raises(ReleaseError, match="40 lowercase"):
		controller.verify(SOURCE_SHA)


def test_annotated_baseline_tag_is_resolved_to_a_commit(tmp_path: Path) -> None:
	_root(tmp_path)
	api = FakeGitHub({"/repos/o/r/git/ref/tags/v2.0.5": {"object": {"sha": BASELINE_SHA, "type": "tag"}}})
	controller = _controller(tmp_path, api)

	assert controller._tag("v2.0.5") == BASELINE_SHA


def test_release_baseline_reads_beyond_the_first_page(tmp_path: Path) -> None:
	_root(tmp_path)
	first_page = [
		{"tag_name": "v1.0.0", "draft": False, "prerelease": False}
		for _ in range(99)
	] + [{"tag_name": "v2.0.5", "draft": False, "prerelease": False}]
	api = FakeGitHub(
		{
			f"/repos/o/r/commits/{SOURCE_SHA}/pulls": [_pull_request(source_sha=SOURCE_SHA)],
			"/repos/o/r/releases?per_page=100": first_page,
			"/repos/o/r/releases?per_page=100&page=2": [
				{"tag_name": "v2.1.0", "draft": False, "prerelease": False}
			],
			"/repos/o/r/git/ref/tags/v2.1.0": {"object": {"sha": BASELINE_SHA, "type": "commit"}},
			f"/repos/o/r/commits/{SOURCE_SHA}/check-runs?per_page=100": {
				"check_runs": [{"name": "Quality Gate", "status": "completed", "conclusion": "success"}]
			},
			f"/repos/o/r/commits/{SOURCE_SHA}/status": {"state": "success"},
		}
	)
	controller = _controller(tmp_path, api)

	with pytest.raises(ReleaseError, match="stale"):
		controller.verify(SOURCE_SHA)


def test_verify_rejects_missing_or_tampered_artifact_content(tmp_path: Path) -> None:
	_root(tmp_path)
	api = FakeGitHub(
		{
			f"/repos/o/r/commits/{SOURCE_SHA}/pulls": [_pull_request(source_sha=SOURCE_SHA)],
			"/repos/o/r/releases?per_page=100": [
				{"tag_name": "v2.0.5", "draft": False, "prerelease": False}
			],
			f"/repos/o/r/commits/{SOURCE_SHA}/check-runs?per_page=100": {
				"check_runs": [{"name": "Quality Gate", "status": "completed", "conclusion": "success"}]
			},
			f"/repos/o/r/commits/{SOURCE_SHA}/status": {"state": "success"},
		}
	)
	artifacts = [_artifact("linux", "linux.zip", root=tmp_path), _artifact("windows", "windows.zip", root=tmp_path)]
	controller = _controller(tmp_path, api)

	assert controller.verify(SOURCE_SHA, artifacts).status == "ready"
	(tmp_path / "linux.zip").write_bytes(b"tampered")
	with pytest.raises(ReleaseError, match="does not match"):
		controller.verify(SOURCE_SHA, artifacts)
	(tmp_path / "linux.zip").unlink()
	with pytest.raises(ReleaseError, match="unreadable"):
		controller.verify(SOURCE_SHA, artifacts)


def test_release_operation_timeouts_are_loaded_from_tracked_configuration(tmp_path: Path) -> None:
	(tmp_path / ".release").mkdir()
	(tmp_path / ".release" / "release-tools.toml").write_text(
		"[timeouts]\ngithub_api_seconds = 7\nexternal_download_seconds = 8\nsubprocess_seconds = 9\ncheck_wait_seconds = 10\ncheck_poll_seconds = 11\n",
		encoding="utf-8",
	)

	assert load_release_timeout(tmp_path, "github_api_seconds") == 7.0
	(tmp_path / ".release" / "release-tools.toml").write_text(
		"[timeouts]\ngithub_api_seconds = nan\nexternal_download_seconds = 8\nsubprocess_seconds = 9\n",
		encoding="utf-8",
	)
	with pytest.raises(ReleaseError, match="finite"):
		load_release_timeout(tmp_path, "github_api_seconds")


def test_artifact_names_are_safe_upload_identifiers() -> None:
	with pytest.raises(ReleaseError, match="name is invalid"):
		ArtifactIdentity.from_mapping(
			{
				"version": "2.0.6",
				"source_sha": SOURCE_SHA,
				"platform": "linux",
				"name": "../release.zip",
				"sha256": "0" * 64,
			}
		)


@pytest.mark.parametrize(
	("impact", "version"),
	[("PATCH", "2.0.6"), ("MINOR", "2.1.0"), ("MAJOR", "3.0.0")],
)
def test_release_impact_selects_the_next_semantic_version(impact: str, version: str) -> None:
	assert validate_latest_release(
		[{"tag_name": "v2.0.5", "draft": False, "prerelease": False}],
		ReleaseIntent(version, impact, "reviewed notes"),
	) == "v2.0.5"


def test_updated_pr_is_selected_only_when_it_authorizes_the_exact_merged_source() -> None:
	old = _pull_request(source_sha=OTHER_SHA)
	old["number"] = 11
	current = _pull_request(source_sha=SOURCE_SHA)

	assert validate_owner_merge(
		[old, current],
		ReleaseIntent("2.0.6", "PATCH", "reviewed notes"),
		source_sha=SOURCE_SHA,
		owner="TheTraitor0FJesus",
		default_branch="main",
	)["merge_commit_sha"] == SOURCE_SHA


def test_release_workflow_keeps_manual_retry_and_exact_source_boundaries() -> None:
	workflow = (REPOSITORY / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

	assert "workflow_dispatch:" in workflow
	assert "source_sha:" in workflow
	assert "[[ \"$source_sha\" =~ ^[0-9a-f]{40}$ ]]" in workflow
	assert "cancel-in-progress: false" in workflow
	assert "ref: ${{ needs.resolve-source.outputs.source_sha }}" in workflow
	assert "--artifact-manifest" in workflow
	assert "--authorization-only" in workflow
	assert "scripts/source_release.py\", \"publish\"" in workflow
	assert "authorize-no-release:" in workflow
	assert "authorize-release:" in workflow
	assert "release_required" in workflow
	assert "statuses: read" not in workflow
	assert "/status" not in workflow
	assert "ref: main" not in workflow
	assert "github.head_ref" not in workflow


def test_source_sha_must_be_a_full_lowercase_commit_identity(tmp_path: Path) -> None:
	_root(tmp_path)
	controller = _controller(tmp_path, FakeGitHub({}))

	with pytest.raises(ReleaseError, match="40 lowercase"):
		controller.verify("not-a-commit-sha", [])


def test_controller_rejects_a_checkout_bound_to_another_source(tmp_path: Path) -> None:
	_root(tmp_path)
	controller = ReleaseController(
		tmp_path,
		FakeGitHub({}),
		repository="o/r",
		owner="TheTraitor0FJesus",
		source_sha_resolver=lambda _root: OTHER_SHA,
	)

	with pytest.raises(ReleaseError, match="local source tree"):
		controller.verify(SOURCE_SHA)


def test_controller_hydrates_merge_authorization_from_full_pull_request(tmp_path: Path) -> None:
	_root(tmp_path)
	commit_pull = _pull_request(source_sha=SOURCE_SHA)
	commit_pull["merged_by"] = None
	full_pull = _pull_request(source_sha=SOURCE_SHA)
	full_pull["body"] = str(full_pull["body"]).replace("\n", "\r\n")
	api = FakeGitHub(
		{
			f"/repos/o/r/commits/{SOURCE_SHA}/pulls": [commit_pull],
			"/repos/o/r/pulls/12": full_pull,
			"/repos/o/r/releases?per_page=100": [
				{"tag_name": "v2.0.5", "draft": False, "prerelease": False}
			],
			f"/repos/o/r/commits/{SOURCE_SHA}/check-runs?per_page=100": {
				"check_runs": [
					{"name": "Quality Gate", "status": "completed", "conclusion": "success"}
				]
			},
		}
	)

	assert _controller(tmp_path, api).verify(SOURCE_SHA, authorization_only=True).status == "authorized"


def test_github_api_uses_merge_compatible_version(monkeypatch: pytest.MonkeyPatch) -> None:
	class Response:
		headers: dict[str, str] = {}

		def __init__(self) -> None:
			self._body = BytesIO(b"{}")

		def __enter__(self) -> Response:
			return self

		def __exit__(self, *args: object) -> None:
			return None

		def read(self, _size: int) -> bytes:
			return self._body.read(_size)

	requests: list[Request] = []

	def fake_urlopen(request: Request, timeout: float) -> Response:
		assert timeout == 1
		requests.append(request)
		return Response()

	monkeypatch.setattr(source_release, "urlopen", fake_urlopen)
	api = source_release.HttpGitHubApi(
		"o/r",
		"token",
		api_url="https://api.example",
		timeout_seconds=1,
	)

	assert api.get("/pulls/12") == {}
	assert requests[0].headers["X-github-api-version"] == "2022-11-28"


def test_missing_checks_and_artifact_source_mismatch_fail_closed(tmp_path: Path) -> None:
	_root(tmp_path)
	api = FakeGitHub(
		{
			f"/repos/o/r/commits/{SOURCE_SHA}/pulls": [_pull_request(source_sha=SOURCE_SHA)],
			"/repos/o/r/releases?per_page=100": [
				{"tag_name": "v2.0.5", "draft": False, "prerelease": False}
			],
			f"/repos/o/r/commits/{SOURCE_SHA}/check-runs?per_page=100": {"check_runs": []},
			f"/repos/o/r/commits/{SOURCE_SHA}/status": {"state": "pending"},
		}
	)
	controller = _controller(tmp_path, api)

	with pytest.raises(ReleaseError, match="missing"):
		controller.verify(SOURCE_SHA)

	api.values[f"/repos/o/r/commits/{SOURCE_SHA}/check-runs?per_page=100"] = {
		"check_runs": [{"name": "Quality Gate", "status": "completed", "conclusion": "success"}]
	}
	api.values[f"/repos/o/r/commits/{SOURCE_SHA}/status"] = {"state": "success"}
	assert controller.verify(SOURCE_SHA, [], authorization_only=True).status == "authorized"
	with pytest.raises(ReleaseError, match="no verified platform artifacts"):
		controller.verify(SOURCE_SHA, [])
	with pytest.raises(ReleaseError, match="artifact"):
		controller.verify(
			SOURCE_SHA,
			[_artifact("linux", "linux.zip", source_sha=OTHER_SHA), _artifact("windows", "windows.zip")],
		)


def test_failed_duplicate_check_run_cannot_be_masked_by_success(tmp_path: Path) -> None:
	_root(tmp_path)
	api = FakeGitHub(
		{
			f"/repos/o/r/commits/{SOURCE_SHA}/pulls": [_pull_request(source_sha=SOURCE_SHA)],
			"/repos/o/r/releases?per_page=100": [
				{"tag_name": "v2.0.5", "draft": False, "prerelease": False}
			],
			f"/repos/o/r/commits/{SOURCE_SHA}/check-runs?per_page=100": {
				"check_runs": [
					{"name": "Quality Gate", "status": "completed", "conclusion": "success"},
					{"name": "Quality Gate", "status": "completed", "conclusion": "failure"},
				]
			},
			f"/repos/o/r/commits/{SOURCE_SHA}/status": {"state": "success"},
		}
	)
	controller = _controller(tmp_path, api)

	with pytest.raises(ReleaseError, match="not successful"):
		controller.verify(SOURCE_SHA, authorization_only=True)


def test_new_publication_and_recoverable_draft_use_only_missing_assets(tmp_path: Path) -> None:
	_root(tmp_path)
	linux = tmp_path / "linux.zip"
	windows = tmp_path / "windows.zip"
	linux.write_bytes(b"linux")
	windows.write_bytes(b"windows")
	artifacts = [
		ArtifactIdentity("2.0.6", SOURCE_SHA, "linux", linux.name, hashlib.sha256(linux.read_bytes()).hexdigest(), str(linux)),
		ArtifactIdentity(
			"2.0.6", SOURCE_SHA, "windows", windows.name, hashlib.sha256(windows.read_bytes()).hexdigest(), str(windows)
		),
	]
	base = {
		f"/repos/o/r/commits/{SOURCE_SHA}/pulls": [_pull_request(source_sha=SOURCE_SHA)],
		"/repos/o/r/releases?per_page=100": [
			{"tag_name": "v2.0.5", "draft": False, "prerelease": False}
		],
		f"/repos/o/r/commits/{SOURCE_SHA}/check-runs?per_page=100": {
			"check_runs": [{"name": "Quality Gate", "status": "completed", "conclusion": "success"}]
		},
		f"/repos/o/r/commits/{SOURCE_SHA}/status": {"state": "success"},
	}
	new_api = FakeGitHub(base.copy())
	new_controller = _controller(tmp_path, new_api)
	new_result = new_controller.publish(SOURCE_SHA, artifacts)

	assert new_result.status == "already-complete"
	assert new_api.uploaded == ["linux.zip", "windows.zip"]

	draft_body = (
		(tmp_path / ".release" / "notes.md").read_text(encoding="utf-8").rstrip()
		+ f"\n\nCandidate source revision: `{SOURCE_SHA}`.\n"
	)
	draft = {
		"id": 7,
		"tag_name": "v2.0.6",
		"target_commitish": SOURCE_SHA,
		"draft": True,
		"prerelease": False,
		"immutable": False,
		"body": draft_body,
		"upload_url": "https://uploads.example/releases/7/assets{?name,label}",
		"assets": [{"name": "linux.zip", "digest": f"sha256:{artifacts[0].sha256}"}],
	}
	draft_values = {
		**base,
		"/repos/o/r/releases?per_page=100": [draft, {"tag_name": "v2.0.5", "draft": False, "prerelease": False}],
	}
	draft_api = FakeGitHub(draft_values)
	draft_controller = _controller(tmp_path, draft_api)
	draft_result = draft_controller.publish(SOURCE_SHA, artifacts)

	assert draft_result.status == "already-complete"
	assert draft_api.uploaded == ["windows.zip"]
	assert draft_api.patched == [{"draft": False}]


def test_upload_digest_mismatch_keeps_new_release_draft(tmp_path: Path) -> None:
	_root(tmp_path)
	linux = tmp_path / "linux.zip"
	windows = tmp_path / "windows.zip"
	linux.write_bytes(b"linux")
	windows.write_bytes(b"windows")
	artifacts = [
		ArtifactIdentity(
			"2.0.6",
			SOURCE_SHA,
			"linux",
			linux.name,
			hashlib.sha256(linux.read_bytes()).hexdigest(),
			str(linux),
		),
		ArtifactIdentity(
			"2.0.6",
			SOURCE_SHA,
			"windows",
			windows.name,
			hashlib.sha256(windows.read_bytes()).hexdigest(),
			str(windows),
		),
	]
	base = {
		f"/repos/o/r/commits/{SOURCE_SHA}/pulls": [_pull_request(source_sha=SOURCE_SHA)],
		"/repos/o/r/releases?per_page=100": [
			{"tag_name": "v2.0.5", "draft": False, "prerelease": False}
		],
		f"/repos/o/r/commits/{SOURCE_SHA}/check-runs?per_page=100": {
			"check_runs": [{"name": "Quality Gate", "status": "completed", "conclusion": "success"}]
		},
		f"/repos/o/r/commits/{SOURCE_SHA}/status": {"state": "success"},
	}
	api = MismatchingUploadGitHub(base)
	controller = _controller(tmp_path, api)

	with pytest.raises(ReleaseError, match="digest mismatch"):
		controller.publish(SOURCE_SHA, artifacts)

	releases = api.values["/repos/o/r/releases?per_page=100"]
	assert isinstance(releases, list)
	draft = next(release for release in releases if isinstance(release, dict) and release.get("tag_name") == "v2.0.6")
	assert draft["draft"] is True
	assert api.patched == []


def test_new_publication_tag_mismatch_stops_before_upload_or_promotion(tmp_path: Path) -> None:
	_root(tmp_path)
	linux = tmp_path / "linux.zip"
	windows = tmp_path / "windows.zip"
	linux.write_bytes(b"linux")
	windows.write_bytes(b"windows")
	artifacts = [
		ArtifactIdentity("2.0.6", SOURCE_SHA, "linux", linux.name, hashlib.sha256(linux.read_bytes()).hexdigest(), str(linux)),
		ArtifactIdentity("2.0.6", SOURCE_SHA, "windows", windows.name, hashlib.sha256(windows.read_bytes()).hexdigest(), str(windows)),
	]
	api = MismatchingDraftSourceGitHub(
		{
			f"/repos/o/r/commits/{SOURCE_SHA}/pulls": [_pull_request(source_sha=SOURCE_SHA)],
			"/repos/o/r/releases?per_page=100": [
				{"tag_name": "v2.0.5", "draft": False, "prerelease": False}
			],
			f"/repos/o/r/commits/{SOURCE_SHA}/check-runs?per_page=100": {
				"check_runs": [{"name": "Quality Gate", "status": "completed", "conclusion": "success"}]
			},
			f"/repos/o/r/commits/{SOURCE_SHA}/status": {"state": "success"},
		}
	)
	controller = _controller(tmp_path, api)

	with pytest.raises(ReleaseError, match="conflicting unpublished"):
		controller.publish(SOURCE_SHA, artifacts)

	releases = api.values["/repos/o/r/releases?per_page=100"]
	assert isinstance(releases, list)
	draft = next(release for release in releases if isinstance(release, dict) and release.get("tag_name") == "v2.0.6")
	assert draft["draft"] is True
	assert api.uploaded == []
	assert api.patched == []


def test_disabled_immutable_releases_leave_new_publication_uncreated(tmp_path: Path) -> None:
	_root(tmp_path)
	linux = tmp_path / "linux.zip"
	windows = tmp_path / "windows.zip"
	linux.write_bytes(b"linux")
	windows.write_bytes(b"windows")
	artifacts = [
		ArtifactIdentity("2.0.6", SOURCE_SHA, "linux", linux.name, hashlib.sha256(linux.read_bytes()).hexdigest(), str(linux)),
		ArtifactIdentity("2.0.6", SOURCE_SHA, "windows", windows.name, hashlib.sha256(windows.read_bytes()).hexdigest(), str(windows)),
	]
	api = FakeGitHub(
		{
			f"/repos/o/r/commits/{SOURCE_SHA}/pulls": [_pull_request(source_sha=SOURCE_SHA)],
			"/repos/o/r/releases?per_page=100": [
				{"tag_name": "v2.0.5", "draft": False, "prerelease": False}
			],
			f"/repos/o/r/commits/{SOURCE_SHA}/check-runs?per_page=100": {
				"check_runs": [{"name": "Quality Gate", "status": "completed", "conclusion": "success"}]
			},
			f"/repos/o/r/commits/{SOURCE_SHA}/status": {"state": "success"},
			"/repos/o/r/immutable-releases": {"enabled": False},
		}
	)
	controller = _controller(tmp_path, api)

	with pytest.raises(ReleaseError, match="immutable releases"):
		controller.publish(SOURCE_SHA, artifacts)

	assert "/repos/o/r/releases/tags/v2.0.6" not in api.values
	assert api.uploaded == []
