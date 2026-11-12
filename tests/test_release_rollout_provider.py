from __future__ import annotations

import ast
from typing import cast

import pytest
from test_release_rollout import (
	FakeGitHub,
	NEXT_PROVIDER_SHA,
	PROVIDER,
	PROVIDER_SHA,
	_branch_files,
	_provider_client,
)

from quality_gate import release_rollout
from quality_gate.publication import parse_decision
from quality_gate.release_rollout import (
	RolloutError,
	WAIVER_MARKER,
)


def test_published_provider_activates_both_pins_and_valid_assertions_in_no_release_pr() -> None:
	client = _provider_client()
	default_sha = client.refs["main"]

	result = release_rollout.activate_provider(client, PROVIDER, "4.2.1", PROVIDER_SHA)

	assert result == "created pull request #1"
	files = _branch_files(client)
	for path in ("quality-gate.toml", "tests/fixtures/release-parity/quality-gate.toml"):
		assert 'policy_release = "v4.2.1"' in files[path]
	for path in ("tests/test_ci_workflow_contract.py", "tests/test_integrity.py"):
		ast.parse(files[path])
		assert '== "v4.2.1"' in files[path]
	assert WAIVER_MARKER not in files["quality-gate.toml"]
	assert 'target = ".github/workflows/release.yml"' not in files["quality-gate.toml"]
	assert 'target = ".github/workflows/parity.yml"' in files["quality-gate.toml"]
	assert files[".release/version.toml"] == 'version = "4.3.0"\n'
	assert "No release" in parse_decision(cast(str, client.pull_requests[0]["body"]))
	assert client.refs["main"] == default_sha
	assert not any("heads/main" in path for _, path, _ in client.mutations)


def test_existing_rollout_branch_with_legacy_workflow_path_is_validated() -> None:
	client = _provider_client()
	release_rollout.activate_provider(client, PROVIDER, "4.2.1", PROVIDER_SHA)
	branch = str(client.pull_requests[0]["head"])
	branch_tree = client.trees[client.commits[client.refs[branch]]]
	branch_tree.pop("tests/test_ci_workflow_contract.py")
	legacy_path = "tests/test_ci_workflow.py"
	legacy_blob = client._id()
	client.blobs[legacy_blob] = 'assert manifest["quality"]["policy_release"] == "v4.2.1"\n'
	branch_tree[legacy_path] = legacy_blob

	result = release_rollout.activate_provider(client, PROVIDER, "4.2.2", NEXT_PROVIDER_SHA)

	assert result == "updated pull request #1"
	files = _branch_files(client)
	assert "tests/test_ci_workflow_contract.py" not in files
	assert 'assert manifest["quality"]["policy_release"] == "v4.2.2"' in files[legacy_path]


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
