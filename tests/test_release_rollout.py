from __future__ import annotations

import ast
import base64
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from quality_gate import release_rollout
from quality_gate.publication import parse_decision
from quality_gate.publication_flow import RECEIPT_MARKER
from quality_gate.publication_github import GitHubCLI
from quality_gate.release_rollout import (
	WAIVER_MARKER,
	RolloutError,
	remove_marked_waivers,
	update_policy_release,
)

PROVIDER = "TheTraitor0FJesus/quality-gate"
PROVIDER_SHA = "b" * 40
NEXT_PROVIDER_SHA = "c" * 40


class FakeGitHub:
	"""In-memory Git and pull-request API for the public rollout behavior."""

	def __init__(self, files: Mapping[str, str]) -> None:
		self.repository = "TheTraitor0FJesus/ARGUS_TG"
		self.default_branch = "main"
		self.next_id = 1
		self.blobs: dict[str, str] = {}
		self.trees: dict[str, dict[str, str]] = {}
		self.commits: dict[str, str] = {}
		self.refs: dict[str, str] = {}
		self.pull_requests: list[dict[str, object]] = []
		self.releases: dict[str, dict[str, object]] = {}
		self.mutations: list[tuple[str, str, Mapping[str, object]]] = []
		base_tree: dict[str, str] = {}
		for path, content in files.items():
			blob_sha = self._id()
			self.blobs[blob_sha] = content
			base_tree[path] = blob_sha
		self.trees[self._id()] = base_tree
		base_tree_sha = next(reversed(self.trees))
		base_commit = self._id()
		self.commits[base_commit] = base_tree_sha
		self.refs["main"] = base_commit

	def _id(self) -> str:
		value = f"{self.next_id:040x}"
		self.next_id += 1
		return value

	def _commit_object(self, commit_sha: str) -> dict[str, object]:
		return {"tree": {"sha": self.commits[commit_sha]}}

	def get(self, path: str) -> object:
		if path.startswith("/releases/tags/v"):
			return self.releases.get(path.removeprefix("/releases/tags/v"))
		if path.startswith("/commits/v"):
			return {"sha": "d" * 40}
		if path == "/":
			return {"default_branch": self.default_branch}
		if path.startswith("/git/ref/heads/"):
			branch = unquote(path.removeprefix("/git/ref/heads/"))
			sha = self.refs.get(branch)
			return None if sha is None else {"object": {"sha": sha}}
		if path.startswith("/git/commits/"):
			commit_sha = path.removeprefix("/git/commits/")
			return self._commit_object(commit_sha)
		if path.startswith("/git/trees/"):
			tree_sha = path.split("?", 1)[0].removeprefix("/git/trees/")
			entries = [
				{"path": name, "sha": sha, "type": "blob", "mode": "100644"}
				for name, sha in self.trees[tree_sha].items()
			]
			return {"truncated": False, "tree": entries}
		if path.startswith("/git/blobs/"):
			blob_sha = path.removeprefix("/git/blobs/")
			content = base64.b64encode(self.blobs[blob_sha].encode()).decode()
			return {"encoding": "base64", "content": content}
		if path.startswith("/compare/"):
			return {"behind_by": 0}
		if path.startswith("/pulls?"):
			return self._pulls(path)
		raise AssertionError(f"unexpected GET {path}")

	def _pulls(self, path: str) -> object:
		query = parse_qs(urlsplit(path).query)
		state = query["state"][0]
		base = query["base"][0]
		head_filter = query.get("head", [None])[0]
		pulls = [
			pull for pull in self.pull_requests if pull["state"] == state and pull["base"] == base
		]
		if head_filter is not None:
			pulls = [pull for pull in pulls if pull["head_name"] == head_filter]
		page = int(query.get("page", ["1"])[0])
		page_size = int(query.get("per_page", ["30"])[0])
		return [self._api_pull(pull) for pull in pulls[(page - 1) * page_size : page * page_size]]

	def post(self, path: str, payload: Mapping[str, object]) -> dict[str, object]:
		self.mutations.append(("POST", path, payload))
		if path == "/git/blobs":
			sha = self._id()
			self.blobs[sha] = cast(str, payload["content"])
			return {"sha": sha}
		if path == "/git/trees":
			base_tree = dict(self.trees[cast(str, payload["base_tree"])])
			for raw_entry in cast(list[dict[str, str]], payload["tree"]):
				base_tree[raw_entry["path"]] = raw_entry["sha"]
			sha = self._id()
			self.trees[sha] = base_tree
			return {"sha": sha}
		if path == "/git/commits":
			sha = self._id()
			self.commits[sha] = cast(str, payload["tree"])
			return {"sha": sha}
		if path == "/git/refs":
			ref = cast(str, payload["ref"]).removeprefix("refs/heads/")
			self.refs[ref] = cast(str, payload["sha"])
			return {"ref": ref}
		if path == "/pulls":
			number = len(self.pull_requests) + 1
			branch = cast(str, payload["head"])
			self.pull_requests.append(
				{
					"number": number,
					"state": "open",
					"title": payload["title"],
					"body": payload["body"],
					"head": branch,
					"head_name": f"TheTraitor0FJesus:{branch}",
					"base": payload["base"],
				}
			)
			return {"number": number}
		if path == "/dispatches":
			return {}
		raise AssertionError(f"unexpected POST {path}")

	def patch(self, path: str, payload: Mapping[str, object]) -> dict[str, object]:
		self.mutations.append(("PATCH", path, payload))
		if path.startswith("/git/refs/heads/"):
			branch = unquote(path.removeprefix("/git/refs/heads/"))
			self.refs[branch] = cast(str, payload["sha"])
			return {"ref": branch}
		if path.startswith("/pulls/"):
			number = int(path.removeprefix("/pulls/"))
			pull = self.pull_requests[number - 1]
			pull.update(payload)
			return self._api_pull(pull)
		raise AssertionError(f"unexpected PATCH {path}")

	def _api_pull(self, pull: dict[str, object]) -> dict[str, object]:
		branch = cast(str, pull["head"])
		return {
			"number": pull["number"],
			"state": pull["state"],
			"title": pull["title"],
			"body": pull["body"],
			"head": {
				"ref": branch,
				"sha": self.refs[branch],
				"repo": {"full_name": self.repository},
			},
			"base": {"ref": pull["base"]},
		}


def test_policy_pin_changes_only_inside_the_quality_section() -> None:
	manifest = '[quality]\nschema = 2\npolicy_release = "v4.2.0"\n\n[repository]\nname = "demo"\n'

	updated = update_policy_release(manifest, "4.2.1")

	assert 'policy_release = "v4.2.1"' in updated
	assert '[repository]\nname = "demo"' in updated
	with pytest.raises(RolloutError, match="exactly one policy_release"):
		update_policy_release("[quality]\nschema = 2\n", "4.2.1")


def test_only_marked_temporary_waiver_is_removed() -> None:
	manifest = (
		"# quality-gate-rollout:remove-after-release\n"
		'[[waivers]]\nkind = "standard"\ncheck_id = "repository.workflow"\n'
		'target = ".github/workflows/release.yml"\n\n'
		'[[waivers]]\nkind = "standard"\ncheck_id = "repository.workflow"\n'
		'target = ".github/workflows/parity.yml"\n'
	)

	updated = remove_marked_waivers(manifest)

	assert WAIVER_MARKER not in updated
	assert "release.yml" not in updated
	assert "parity.yml" in updated
	with pytest.raises(RolloutError, match="marker must precede"):
		remove_marked_waivers(WAIVER_MARKER + "\n[quality]\n")


@pytest.mark.parametrize("entry_type", ["blob", "tree"])
def test_workflow_symlink_is_rejected_before_entry_type(
	monkeypatch: pytest.MonkeyPatch, entry_type: str
) -> None:
	client = FakeGitHub({".github/workflows/quality.yml": "content"})
	original_get = client.get

	def get(path: str) -> object:
		response = original_get(path)
		if path.startswith("/git/trees/"):
			return {
				"truncated": False,
				"tree": [
					{"path": ".github/workflows/quality.yml", "type": entry_type, "mode": "120000"}
				],
			}
		return response

	monkeypatch.setattr(client, "get", get)
	with pytest.raises(RolloutError, match="symbolic link"):
		release_rollout._tree(client, client.refs["main"])


def test_foreign_rollout_branch_is_ignored_before_marker_validation(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	client = FakeGitHub({})
	pull = {
		"head": {"ref": "quality-gate-rollout/v1", "repo": {"full_name": "other/fork"}},
		"body": None,
	}
	monkeypatch.setattr(release_rollout, "_pull_requests", lambda *args, **kwargs: [pull])
	assert release_rollout._managed_open_pull(client, "owner", "repository", "main") is None

	pull["head"] = {"ref": "quality-gate-rollout/v1", "repo": {"full_name": "OWNER/repository"}}
	with pytest.raises(RolloutError, match="no marker body"):
		release_rollout._managed_open_pull(client, "owner", "repository", "main")


def _provider_client() -> FakeGitHub:
	root = Path(__file__).resolve().parents[1]
	files = {
		path: (root / path).read_text(encoding="utf-8")
		for path in (
			"quality-gate.toml",
			"tests/fixtures/release-parity/quality-gate.toml",
			"tests/test_ci_workflow.py",
			"tests/test_integrity.py",
		)
	}
	for path in ("quality-gate.toml", "tests/fixtures/release-parity/quality-gate.toml"):
		files[path] = update_policy_release(files[path], "4.2.0")
	files["quality-gate.toml"] = files["quality-gate.toml"].replace(
		"\n[[waivers]]",
		"\n" + WAIVER_MARKER + '\n[[waivers]]\nkind = "standard"\n'
		'check_id = "repository.workflow"\ntarget = ".github/workflows/release.yml"\n\n[[waivers]]',
		1,
	)
	files[".release/version.toml"] = 'version = "4.3.0"\n'
	client = FakeGitHub(files)
	client.repository = PROVIDER
	for version, sha in (("4.2.1", PROVIDER_SHA), ("4.2.2", NEXT_PROVIDER_SHA)):
		receipt = {
			"schema": 1,
			"candidate": {
				"repository": PROVIDER,
				"provider_repository": PROVIDER,
				"version": version,
				"source_sha": "d" * 40,
			},
			"verification": {"provider_sha": sha},
		}
		client.releases[version] = {
			"tag_name": f"v{version}",
			"draft": False,
			"prerelease": False,
			"body": "Published notes" + RECEIPT_MARKER + json.dumps(receipt) + "-->\n",
		}
	return client


def _branch_files(client: FakeGitHub) -> dict[str, str]:
	branch = cast(str, client.pull_requests[-1]["head"])
	return {
		path: client.blobs[sha]
		for path, sha in client.trees[client.commits[client.refs[branch]]].items()
	}


def test_published_provider_activates_both_pins_and_valid_assertions_in_no_release_pr() -> None:
	client = _provider_client()
	default_sha = client.refs["main"]

	result = release_rollout.activate_provider(client, PROVIDER, "4.2.1", PROVIDER_SHA)

	assert result == "created pull request #1"
	files = _branch_files(client)
	for path in ("quality-gate.toml", "tests/fixtures/release-parity/quality-gate.toml"):
		assert 'policy_release = "v4.2.1"' in files[path]
	for path in ("tests/test_ci_workflow.py", "tests/test_integrity.py"):
		ast.parse(files[path])
		assert '== "v4.2.1"' in files[path]
	assert WAIVER_MARKER not in files["quality-gate.toml"]
	assert 'target = ".github/workflows/release.yml"' not in files["quality-gate.toml"]
	assert 'target = ".github/workflows/parity.yml"' in files["quality-gate.toml"]
	assert files[".release/version.toml"] == 'version = "4.3.0"\n'
	assert "No release" in parse_decision(cast(str, client.pull_requests[0]["body"]))
	assert client.refs["main"] == default_sha
	assert not any("heads/main" in path for _, path, _ in client.mutations)


def test_delivery_repeats_without_writes_and_newer_release_refreshes_one_provider_pr() -> None:
	client = _provider_client()
	release_rollout.activate_provider(client, PROVIDER, "4.2.1", PROVIDER_SHA)
	client.mutations.clear()

	release_rollout.activate_provider(client, PROVIDER, "4.2.1", PROVIDER_SHA)
	assert not client.mutations
	release_rollout.activate_provider(client, PROVIDER, "4.2.2", NEXT_PROVIDER_SHA)

	assert len(client.pull_requests) == 1
	assert 'policy_release = "v4.2.2"' in _branch_files(client)["quality-gate.toml"]
	assert NEXT_PROVIDER_SHA in cast(str, client.pull_requests[0]["body"])
	client.mutations.clear()
	release_rollout.activate_provider(client, PROVIDER, "4.2.1", PROVIDER_SHA)
	assert not client.mutations


def test_closed_refreshed_provider_pr_stays_closed_for_same_release() -> None:
	client = _provider_client()
	release_rollout.activate_provider(client, PROVIDER, "4.2.1", PROVIDER_SHA)
	release_rollout.activate_provider(client, PROVIDER, "4.2.2", NEXT_PROVIDER_SHA)
	client.pull_requests[0]["state"] = "closed"
	client.mutations.clear()

	result = release_rollout.activate_provider(client, PROVIDER, "4.2.2", NEXT_PROVIDER_SHA)

	assert result == "skipped closed rollout for v4.2.2"
	assert not client.mutations
	assert len(client.pull_requests) == 1
	release_rollout.activate_provider(client, PROVIDER, "4.2.1", PROVIDER_SHA)
	assert not client.mutations
	assert len(client.pull_requests) == 1


@pytest.mark.parametrize("invalid", ["missing", "draft", "prerelease", "sha", "version", "tag"])
def test_unverified_release_cannot_write_provider_branch(invalid: str) -> None:
	client = _provider_client()
	release = client.releases["4.2.1"]
	if invalid == "missing":
		client.releases.clear()
	elif invalid in {"draft", "prerelease"}:
		release[invalid] = True
	elif invalid == "tag":
		release["tag_name"] = "v9.0.0"
	else:
		body = cast(str, release["body"])
		release["body"] = (
			body.replace(PROVIDER_SHA, NEXT_PROVIDER_SHA)
			if invalid == "sha"
			else body.replace("4.2.1", "9.0.0")
		)

	with pytest.raises(RolloutError, match=r"published|receipt"):
		release_rollout.activate_provider(client, PROVIDER, "4.2.1", PROVIDER_SHA)

	assert not client.mutations


def test_next_release_after_a_closed_provider_pr_creates_a_new_pr() -> None:
	client = _provider_client()
	release_rollout.activate_provider(client, PROVIDER, "4.2.1", PROVIDER_SHA)
	client.pull_requests[0]["state"] = "closed"

	release_rollout.activate_provider(client, PROVIDER, "4.2.2", NEXT_PROVIDER_SHA)

	assert [pull["state"] for pull in client.pull_requests] == ["closed", "open"]
	assert 'policy_release = "v4.2.2"' in _branch_files(client)["quality-gate.toml"]


def test_closed_provider_pr_is_found_beyond_one_page_of_unrelated_history() -> None:
	client = _provider_client()
	release_rollout.activate_provider(client, PROVIDER, "4.2.1", PROVIDER_SHA)
	client.pull_requests[0]["state"] = "closed"
	client.refs["unrelated"] = client.refs["main"]
	unrelated = {**client.pull_requests[0], "head": "unrelated", "body": "Unrelated change"}
	client.pull_requests = [dict(unrelated) for _ in range(100)] + client.pull_requests
	client.mutations.clear()

	result = release_rollout.activate_provider(client, PROVIDER, "4.2.1", PROVIDER_SHA)

	assert result == "skipped closed rollout for v4.2.1"
	assert not client.mutations


def test_consecutive_marked_waivers_are_removed_without_losing_permanent_waiver() -> None:
	waiver = '[[waivers]]\ntarget = "temporary"\n'
	manifest = (WAIVER_MARKER + "\n" + waiver) * 2 + '[[waivers]]\ntarget = "permanent"\n'

	assert remove_marked_waivers(manifest) == '[[waivers]]\ntarget = "permanent"\n'


def test_verified_publication_emits_the_stable_version_and_provider_sha_payload() -> None:
	client = _provider_client()

	release_rollout.dispatch_release(client, PROVIDER, "4.2.1", PROVIDER_SHA)

	assert client.mutations == [
		(
			"POST",
			"/dispatches",
			{
				"event_type": "quality-gate-release-published",
				"client_payload": {"version": "4.2.1", "provider_sha": PROVIDER_SHA},
			},
		)
	]


def test_unpublished_release_cannot_dispatch_rollout() -> None:
	client = _provider_client()
	client.releases["4.2.1"]["draft"] = True

	with pytest.raises(RolloutError, match="published"):
		release_rollout.dispatch_release(client, PROVIDER, "4.2.1", PROVIDER_SHA)

	assert not client.mutations


def test_dispatch_accepts_github_no_content_response_after_verifying_publication() -> None:
	service = _provider_client()

	def command(arguments: Sequence[str], *, content: bytes | None = None, limit: int) -> bytes:
		path = arguments[2].removeprefix(f"repos/{PROVIDER}")
		if arguments[arguments.index("--method") + 1] == "GET":
			return json.dumps(service.get(path)).encode()
		assert path == "/dispatches"
		assert content is not None
		service.post(path, json.loads(content))
		return b""

	release_rollout.dispatch_release(
		GitHubCLI(PROVIDER, timeout_seconds=60, command=command),
		PROVIDER,
		"4.2.1",
		PROVIDER_SHA,
	)

	assert service.mutations == [
		(
			"POST",
			"/dispatches",
			{
				"event_type": "quality-gate-release-published",
				"client_payload": {"version": "4.2.1", "provider_sha": PROVIDER_SHA},
			},
		)
	]


def test_workflow_dispatch_requires_successful_publication_and_verified_outputs() -> None:
	root = Path(__file__).resolve().parents[1]
	workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
	dispatch = workflow.split("  dispatch-rollout:\n", 1)[1]
	assert "needs: [prepare, publish]" in dispatch
	assert (
		"if: needs.prepare.outputs.status == 'build-required' && needs.publish.result == 'success'"
		in dispatch
	)
	assert "QUALITY_GATE_RELEASE_VERSION: ${{ needs.prepare.outputs.version }}" in dispatch
	assert "QUALITY_GATE_PROVIDER_SHA: ${{ needs.prepare.outputs.provider-sha }}" in dispatch
	assert "run: python -B -m quality_gate.release_rollout dispatch" in dispatch
	activation = (root / ".github/workflows/release-rollout.yml").read_text(encoding="utf-8")
	assert "types: [quality-gate-release-published]" in activation
	assert "cancel-in-progress: false" in activation
