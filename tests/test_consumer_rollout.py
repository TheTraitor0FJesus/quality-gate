"""Consumer activation through the rollout CLI and its GitHub API boundary."""

import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

import pytest
from test_release_rollout import (
	NEXT_PROVIDER_SHA,
	PROVIDER,
	PROVIDER_SHA,
	FakeGitHub,
	_provider_client,
)

from quality_gate import consumer_rollout
from quality_gate.consumer_rollout import load_consumers, update_workflow_references
from quality_gate.publication import PublicationError
from quality_gate.publication_github import GitHubCLI
from quality_gate.release_rollout import RolloutError, _update_rollout


def _workflow_update(
	client: FakeGitHub,
	tree: Mapping[str, str],
	version: str,
	provider_sha: str,
) -> dict[str, str]:
	workflow_sha = next(sha for path, sha in tree.items() if path.endswith("quality.yml"))
	content = client.blobs[workflow_sha]
	updated = update_workflow_references(content, PROVIDER, provider_sha, version)
	return {".github/workflows/quality.yml": updated} if updated != content else {}


def _fake_client() -> FakeGitHub:
	return FakeGitHub(
		{
			".github/workflows/quality.yml": (
				"jobs:\n  quality:\n    uses: " + PROVIDER + "/.github/workflows/quality.yml@main\n"
			),
			"quality-gate.toml": '[quality]\nschema = 2\npolicy_release = "v4.2.0"\n',
		}
	)


def test_workflow_pin_updates_gate_helpers_and_preserves_other_workflows() -> None:
	old_sha = "a" * 40
	workflow = (
		f"jobs:\n  quality:\n    uses: {PROVIDER}/.github/workflows/quality.yml@main\n"
		f"  release:\n    uses: {PROVIDER}/.github/workflows/"
		f"release-publish.yml@{old_sha} # v4.2.0\n"
		"  checkout:\n    uses: actions/checkout@v7\n"
	)

	updated = update_workflow_references(workflow, PROVIDER, PROVIDER_SHA, "4.2.1")

	expected_gate_references = 2
	assert updated.count(f"@{PROVIDER_SHA} # v4.2.1") == expected_gate_references
	assert "actions/checkout@v7" in updated
	with pytest.raises(RolloutError, match="unsupported format"):
		update_workflow_references(
			f"uses: {PROVIDER}/.github/workflows/quality.yml@${{{{ vars.SHA }}}}",
			PROVIDER,
			PROVIDER_SHA,
			"4.2.1",
		)


def test_newer_release_refreshes_same_open_pull_request_without_touching_main() -> None:
	client = _fake_client()
	main_sha = client.refs["main"]

	def command(arguments: Sequence[str], *, content: bytes | None = None, limit: int) -> bytes:
		path = arguments[2].removeprefix(f"repos/{client.repository}") or "/"
		method = arguments[arguments.index("--method") + 1]
		if method == "GET":
			return json.dumps(client.get(path)).encode()
		assert content is not None
		payload = json.loads(content)
		if method == "POST":
			return json.dumps(client.post(path, payload)).encode()
		assert method == "PATCH"
		return json.dumps(client.patch(path, payload)).encode()

	github = GitHubCLI(client.repository, timeout_seconds=60, command=command)
	first = _update_rollout(
		github,
		"TheTraitor0FJesus",
		"ARGUS_TG",
		"4.2.1",
		PROVIDER_SHA,
		lambda api, tree: _workflow_update(client, tree, "4.2.1", PROVIDER_SHA),
	)
	second = _update_rollout(
		github,
		"TheTraitor0FJesus",
		"ARGUS_TG",
		"4.2.2",
		NEXT_PROVIDER_SHA,
		lambda api, tree: _workflow_update(client, tree, "4.2.2", NEXT_PROVIDER_SHA),
	)

	assert first == "created pull request #1"
	assert second == "updated pull request #1"
	assert len(client.pull_requests) == 1
	assert client.refs["main"] == main_sha
	branch_sha = client.refs[cast(str, client.pull_requests[0]["head"])]
	branch_tree = client.trees[client.commits[branch_sha]]
	workflow_sha = branch_tree[".github/workflows/quality.yml"]
	workflow = client.blobs[workflow_sha]
	assert f"@{NEXT_PROVIDER_SHA} # v4.2.2" in workflow
	assert "@main" not in workflow
	assert "v4.2.2" in cast(str, client.pull_requests[0]["body"])


def test_closed_rollout_is_not_reopened_but_next_release_gets_a_new_pr() -> None:
	client = _fake_client()

	def update(api: FakeGitHub, tree: Mapping[str, str]) -> dict[str, str]:
		return _workflow_update(api, tree, "4.2.1", PROVIDER_SHA)

	_update_rollout(client, "TheTraitor0FJesus", "ARGUS_TG", "4.2.1", PROVIDER_SHA, update)
	client.pull_requests[0]["state"] = "closed"

	closed_repeat = _update_rollout(
		client,
		"TheTraitor0FJesus",
		"ARGUS_TG",
		"4.2.1",
		PROVIDER_SHA,
		update,
	)
	new_release = _update_rollout(
		client,
		"TheTraitor0FJesus",
		"ARGUS_TG",
		"4.2.2",
		NEXT_PROVIDER_SHA,
		lambda api, tree: _workflow_update(api, tree, "4.2.2", NEXT_PROVIDER_SHA),
	)

	assert closed_repeat == "skipped closed rollout for v4.2.1"
	assert new_release == "created pull request #2"
	assert [pull["state"] for pull in client.pull_requests] == ["closed", "open"]


def test_consumer_manifest_includes_the_skills_template() -> None:
	owner, consumers = load_consumers(Path(".github/quality-gate-consumers.json"))

	assert owner == "TheTraitor0FJesus"
	assert {consumer.name for consumer in consumers} == {
		"ARGUS_TG",
		"ARGUS_tracker_multiuser",
		"ARGUS_WEB",
		"ARGUS_BRAIN",
		"Obsidian_English",
		"skills",
		"quality-gate",
	}
	skills = next(consumer for consumer in consumers if consumer.name == "skills")
	assert skills.policy_manifest == "quality-gate.toml"
	assert skills.additional_workflow_files == ("skills/setup-repo/assets/quality-workflow.yml",)
	provider = next(consumer for consumer in consumers if consumer.name == "quality-gate")
	assert provider.activation == "provider"


def _consumer_files() -> dict[str, str]:
	return {
		"quality-gate.toml": '[quality]\nschema = 2\npolicy_release = "v4.2.0"\n',
		".github/workflows/quality.yml": (
			f"jobs:\n  quality:\n    uses: {PROVIDER}/.github/workflows/quality.yml@main\n"
		),
		".github/workflows/release.yml": "jobs:\n"
		+ "".join(
			f"  job{index}:\n    uses: {PROVIDER}/.github/workflows/{name}@main\n"
			for index, name in enumerate(
				("release-prepare.yml", "release-publish.yml", "image-writer.yml")
			)
		),
		"README.md": "Keep unrelated work.\n",
	}


def _cli_services(monkeypatch: pytest.MonkeyPatch) -> dict[str, FakeGitHub]:
	_, targets = load_consumers(Path(".github/quality-gate-consumers.json"))
	services = {PROVIDER: _provider_client()}
	for target in targets:
		if target.full_name == PROVIDER:
			continue
		files = _consumer_files()
		if target.name == "skills":
			files["skills/setup-repo/assets/quality-workflow.yml"] = (
				f"jobs:\n  quality:\n    uses: {PROVIDER}/.github/workflows/"
				"quality.yml@<40-character-commit-sha> # <release-tag>\n"
			)
		service = FakeGitHub(files)
		service.repository = target.full_name
		services[target.full_name] = service
	monkeypatch.setattr(sys, "argv", ["consumer-rollout", "consumers"])
	monkeypatch.setenv("GITHUB_REPOSITORY", PROVIDER)
	monkeypatch.setenv("QUALITY_GATE_RELEASE_VERSION", "4.2.1")
	monkeypatch.setenv("QUALITY_GATE_PROVIDER_SHA", PROVIDER_SHA)
	# The installation-token API transport is the only replaced production boundary.
	monkeypatch.setattr(
		consumer_rollout,
		"GitHubCLI",
		lambda repository, **kwargs: services[repository],
	)
	return services


def _branch_contents(client: FakeGitHub) -> dict[str, str]:
	branch = cast(str, client.pull_requests[-1]["head"])
	tree = client.trees[client.commits[client.refs[branch]]]
	return {path: client.blobs[sha] for path, sha in tree.items()}


def test_verified_event_updates_all_consumers_and_skills_without_duplicate_provider_pr(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	services = _cli_services(monkeypatch)
	defaults = {name: client.refs["main"] for name, client in services.items()}

	assert consumer_rollout.main() == 0

	assert not services[PROVIDER].mutations
	for name, client in services.items():
		assert client.refs["main"] == defaults[name]
		if name == PROVIDER:
			continue
		assert len(client.pull_requests) == 1
		files = _branch_contents(client)
		assert 'policy_release = "v4.2.1"' in files["quality-gate.toml"]
		assert files["README.md"] == "Keep unrelated work.\n"
		for path, content in files.items():
			if path.endswith(".yml"):
				assert "@main" not in content
				assert f"@{PROVIDER_SHA}" in content
		assert not any(payload.get("force") for _, _, payload in client.mutations)
		assert not any("merge" in path for _, path, _ in client.mutations)


def test_unverified_event_makes_no_consumer_mutations(monkeypatch: pytest.MonkeyPatch) -> None:
	services = _cli_services(monkeypatch)
	services[PROVIDER].releases["4.2.1"]["draft"] = True

	assert consumer_rollout.main() == 1

	assert all(not client.mutations for client in services.values())


def test_one_inaccessible_consumer_does_not_suppress_remaining_targets(
	monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
	services = _cli_services(monkeypatch)
	failed = "TheTraitor0FJesus/ARGUS_WEB"

	def client_for(repository: str, **kwargs: object) -> FakeGitHub:
		if repository == failed:
			raise PublicationError("installation lacks workflow write permission")
		return services[repository]

	monkeypatch.setattr(consumer_rollout, "GitHubCLI", client_for)

	assert consumer_rollout.main() == 1

	assert not services[failed].mutations
	assert "ARGUS_WEB rollout failed" in caplog.text
	for name, client in services.items():
		if name not in {PROVIDER, failed}:
			assert len(client.pull_requests) == 1


def test_matrix_targets_come_from_registry_without_provider_or_api_writes(
	monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
	services = _cli_services(monkeypatch)
	monkeypatch.setattr(sys, "argv", ["consumer-rollout", "targets"])
	monkeypatch.delenv("QUALITY_GATE_RELEASE_VERSION")
	monkeypatch.delenv("QUALITY_GATE_PROVIDER_SHA")

	assert consumer_rollout.main() == 0

	assert set(json.loads(capsys.readouterr().out)) == {
		"ARGUS_TG",
		"ARGUS_tracker_multiuser",
		"ARGUS_WEB",
		"ARGUS_BRAIN",
		"Obsidian_English",
		"skills",
	}
	assert all(not client.mutations for client in services.values())


@pytest.mark.parametrize("selected", ["ARGUS_TG", "quality-gate", "unregistered"])
def test_matrix_job_can_only_update_one_registered_consumer(
	monkeypatch: pytest.MonkeyPatch, selected: str
) -> None:
	services = _cli_services(monkeypatch)
	monkeypatch.setattr(sys, "argv", ["consumer-rollout", "consumers", "--repository", selected])

	assert consumer_rollout.main() == (0 if selected == "ARGUS_TG" else 1)

	for name, client in services.items():
		if selected == "ARGUS_TG" and name == f"TheTraitor0FJesus/{selected}":
			assert len(client.pull_requests) == 1
		else:
			assert not client.mutations


@pytest.mark.parametrize("invalid_ref", ["${{ vars.SHA }}", "${{vars.SHA}}", "main extra"])
def test_invalid_workflow_reference_fails_before_branch_or_pr_writes(
	monkeypatch: pytest.MonkeyPatch, invalid_ref: str
) -> None:
	services = _cli_services(monkeypatch)
	client = services["TheTraitor0FJesus/ARGUS_TG"]
	tree = client.trees[client.commits[client.refs["main"]]]
	client.blobs[tree[".github/workflows/quality.yml"]] = (
		f"jobs:\n  quality:\n    uses: {PROVIDER}/.github/workflows/quality.yml@{invalid_ref}\n"
	)

	assert consumer_rollout.main() == 1

	assert not client.mutations


def test_newer_verified_release_refreshes_policy_workflows_and_template_in_same_pr(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	services = _cli_services(monkeypatch)
	assert consumer_rollout.main() == 0
	for client in services.values():
		client.mutations.clear()
	assert consumer_rollout.main() == 0
	assert all(not client.mutations for client in services.values())
	monkeypatch.setenv("QUALITY_GATE_RELEASE_VERSION", "4.2.2")
	monkeypatch.setenv("QUALITY_GATE_PROVIDER_SHA", NEXT_PROVIDER_SHA)

	assert consumer_rollout.main() == 0

	for name, client in services.items():
		if name == PROVIDER:
			continue
		assert len(client.pull_requests) == 1
		files = _branch_contents(client)
		assert 'policy_release = "v4.2.2"' in files["quality-gate.toml"]
		assert all(
			f"@{NEXT_PROVIDER_SHA}" in content
			for path, content in files.items()
			if path.endswith(".yml")
		)
		assert files["README.md"] == "Keep unrelated work.\n"


def test_invalid_open_pr_is_rejected_before_syncing_its_branch(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	services = _cli_services(monkeypatch)
	assert consumer_rollout.main() == 0
	client = services["TheTraitor0FJesus/ARGUS_TG"]
	branch = cast(str, client.pull_requests[0]["head"])
	tree = client.trees[client.commits[client.refs[branch]]]
	client.blobs[tree[".github/workflows/quality.yml"]] = (
		f"jobs:\n  quality:\n    uses: {PROVIDER}/.github/workflows/"
		"quality.yml@${{ vars.SHA }}\n"
	)
	original_get = client.get

	def get(path: str) -> object:
		if path.startswith("/compare/"):
			return {"behind_by": 1}
		return original_get(path)

	monkeypatch.setattr(client, "get", get)
	monkeypatch.setenv("QUALITY_GATE_RELEASE_VERSION", "4.2.2")
	monkeypatch.setenv("QUALITY_GATE_PROVIDER_SHA", NEXT_PROVIDER_SHA)
	client.mutations.clear()
	branch_sha = client.refs[branch]

	assert consumer_rollout.main() == 1

	assert not client.mutations
	assert client.refs[branch] == branch_sha


def test_existing_branch_without_managed_pr_is_preserved(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	services = _cli_services(monkeypatch)
	client = services["TheTraitor0FJesus/ARGUS_TG"]
	branch = "quality-gate-rollout/v4.2.1-argus-tg"
	client.refs[branch] = client.refs["main"]
	branch_sha = client.refs[branch]

	assert consumer_rollout.main() == 1

	assert not client.mutations
	assert client.refs[branch] == branch_sha
	assert not client.pull_requests


@pytest.mark.parametrize("quote", ["'", '"', ""])
@pytest.mark.parametrize("comment_prefix", ["#", "    #"])
def test_static_refs_preserve_quotes_comments_and_commented_examples(
	quote: str, comment_prefix: str
) -> None:
	content = (
		f"    uses: {quote}{PROVIDER}/.github/workflows/quality.yml@main{quote} # keep detail\r\n"
		f"{comment_prefix} uses: {PROVIDER}/.github/workflows/quality.yml@main\r\n"
	)

	updated = update_workflow_references(content, PROVIDER, PROVIDER_SHA, "4.2.1")

	assert updated == (
		f"    uses: {quote}{PROVIDER}/.github/workflows/quality.yml@{PROVIDER_SHA}{quote} "
		"# keep detail\r\n"
		f"{comment_prefix} uses: {PROVIDER}/.github/workflows/quality.yml@main\r\n"
	)


@pytest.mark.parametrize("missing", ["quality-gate.toml", "template-reference"])
def test_missing_configured_pin_is_an_error_before_writes(
	monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
	services = _cli_services(monkeypatch)
	client = services["TheTraitor0FJesus/skills"]
	tree = client.trees[client.commits[client.refs["main"]]]
	if missing == "quality-gate.toml":
		del tree[missing]
	else:
		client.blobs[tree["skills/setup-repo/assets/quality-workflow.yml"]] = "jobs: {}\n"

	assert consumer_rollout.main() == 1

	assert not client.mutations
