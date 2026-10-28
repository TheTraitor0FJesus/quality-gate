"""Create reviewed provider activation pull requests after verified publication."""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import tomllib
from collections.abc import Callable, Mapping
from typing import Protocol, cast
from urllib.parse import quote, urlencode

from .publication import MAX_VERSION_LENGTH, VERSION, PublicationError
from .publication_flow import RECEIPT_MARKER
from .publication_github import GitHubCLI

LOGGER = logging.getLogger(__name__)
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
WAIVER_MARKER = "# quality-gate-rollout:remove-after-release"
ROLLOUT_PREFIX = "quality-gate-rollout/"
PR_PAGE_SIZE = 100
PR_MAX_PAGES = 10


class RolloutError(ValueError):
	"""A release rollout input or repository state is unsafe to update automatically."""


class GitHub(Protocol):
	"""The API surface already provided by the pinned GitHub CLI transport."""

	def get(self, path: str) -> object: ...
	def post(self, path: str, payload: Mapping[str, object]) -> dict[str, object]: ...
	def patch(self, path: str, payload: Mapping[str, object]) -> dict[str, object]: ...


UpdateFunction = Callable[[GitHub, Mapping[str, str]], dict[str, str]]


def _mapping(value: object, description: str) -> dict[str, object]:
	if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
		raise RolloutError(f"{description} is not a JSON object")
	return cast(dict[str, object], value)


def _sequence(value: object, description: str) -> list[object]:
	if not isinstance(value, list):
		raise RolloutError(f"{description} is not a JSON list")
	return value


def _text(value: object, description: str) -> str:
	if not isinstance(value, str) or not value:
		raise RolloutError(f"{description} is missing or invalid")
	return value


def _sha(value: object) -> str:
	if not isinstance(value, str) or SHA_PATTERN.fullmatch(value) is None:
		raise RolloutError("provider revision must be a full lowercase commit SHA")
	return value


def _version(value: object) -> str:
	if (
		not isinstance(value, str)
		or len(value) > MAX_VERSION_LENGTH
		or VERSION.fullmatch(value) is None
	):
		raise RolloutError("release version must be plain MAJOR.MINOR.PATCH")
	return value


def update_policy_release(content: str, version: str) -> str:
	"""Replace exactly one policy_release value in the TOML quality section."""
	policy_version = f"v{_version(version)}"
	section_pattern = re.compile(r"(?ms)^\[quality\]\r?\n(?P<section>.*?)(?=^\[|\Z)")
	matches = list(section_pattern.finditer(content))
	if len(matches) != 1:
		raise RolloutError("manifest must contain exactly one [quality] section")
	match = matches[0]
	line_pattern = re.compile(r'(?m)^(?P<prefix>[ \t]*policy_release[ \t]*=[ \t]*)"[^"]+"')
	section, count = line_pattern.subn(
		lambda item: f'{item.group("prefix")}"{policy_version}"',
		match.group("section"),
	)
	if count != 1:
		raise RolloutError("[quality] must contain exactly one policy_release")
	return content[: match.start("section")] + section + content[match.end("section") :]


def remove_marked_waivers(content: str) -> str:
	"""Remove only TOML waiver blocks carrying the post-release marker comment."""
	lines = content.splitlines(keepends=True)
	index = 0
	while index < len(lines):
		if lines[index].strip() != WAIVER_MARKER:
			index += 1
			continue
		section = index + 1
		while section < len(lines) and not lines[section].strip():
			section += 1
		if section >= len(lines) or lines[section].strip() != "[[waivers]]":
			raise RolloutError("temporary waiver marker must precede a [[waivers]] block")
		end = section + 1
		while (
			end < len(lines)
			and re.match(r"^[ \t]*\[", lines[end]) is None
			and lines[end].strip() != WAIVER_MARKER
		):
			end += 1
		del lines[index:end]
	return "".join(lines)


def _keep_prefix(value: str) -> Callable[[re.Match[str]], str]:
	return lambda match: f"{match.group('prefix')}{value}{match.groupdict().get('suffix', '')}"


def _replace_one(
	content: str,
	pattern: str,
	replacement: str | Callable[[re.Match[str]], str],
	description: str,
) -> str:
	updated, count = re.subn(pattern, replacement, content, flags=re.MULTILINE)
	if count != 1:
		raise RolloutError(f"expected exactly one active {description}")
	return updated


def _mapping_file(client: GitHub, tree: Mapping[str, str], path: str) -> str:
	blob_sha = tree.get(path)
	if blob_sha is None:
		raise RolloutError(f"required file is missing: {path}")
	blob = _mapping(client.get(f"/git/blobs/{_sha(blob_sha)}"), f"Git blob for {path}")
	if blob.get("encoding") != "base64":
		raise RolloutError(f"required file is not a base64 Git blob: {path}")
	encoded = _text(blob.get("content"), f"Git blob content for {path}")
	try:
		return base64.b64decode(encoded.replace("\n", ""), validate=True).decode("utf-8")
	except (ValueError, UnicodeError) as error:
		raise RolloutError(f"Git blob is not valid UTF-8 text: {path}") from error


def _provider_updates(client: GitHub, tree: Mapping[str, str], version: str) -> dict[str, str]:
	policy_version = f"v{_version(version)}"
	assertion_suffix = version.replace(".", "")
	paths = (
		"quality-gate.toml",
		"tests/fixtures/release-parity/quality-gate.toml",
		"tests/test_ci_workflow.py",
		"tests/test_integrity.py",
	)
	contents = {path: _mapping_file(client, tree, path) for path in paths}
	for path in paths[:2]:
		current = tomllib.loads(contents[path])["quality"]["policy_release"]
		current_version = _version(_text(current, "current policy release").removeprefix("v"))
		if tuple(map(int, current_version.split("."))) > tuple(map(int, version.split("."))):
			raise RolloutError("older event cannot downgrade the active provider policy")
	contents["quality-gate.toml"] = remove_marked_waivers(
		update_policy_release(contents["quality-gate.toml"], version)
	)
	contents["tests/fixtures/release-parity/quality-gate.toml"] = update_policy_release(
		contents["tests/fixtures/release-parity/quality-gate.toml"], version
	)
	contents["tests/test_ci_workflow.py"] = _replace_one(
		contents["tests/test_ci_workflow.py"],
		r'^(?P<prefix>[ \t]*assert manifest\["quality"\]\["policy_release"\] == )'
		r'"v[0-9]+\.[0-9]+\.[0-9]+"$',
		_keep_prefix(f'"{policy_version}"'),
		"provider policy assertion",
	)
	contents["tests/test_integrity.py"] = _replace_one(
		contents["tests/test_integrity.py"],
		r"^(?P<prefix>[ \t]*def test_provider_self_policy_v)[0-9]+"
		r"(?P<suffix>_uses_only_the_parity_waiver\(\) -> None:)$",
		_keep_prefix(assertion_suffix),
		"provider waiver test name",
	)
	contents["tests/test_integrity.py"] = _replace_one(
		contents["tests/test_integrity.py"],
		r'^(?P<prefix>[ \t]*assert manifest\.policy_release == )"v[0-9]+\.[0-9]+\.[0-9]+"$',
		_keep_prefix(f'"{policy_version}"'),
		"provider policy integrity assertion",
	)
	return {
		path: value
		for path, value in contents.items()
		if _mapping_file(client, tree, path) != value
	}


def _tree(client: GitHub, commit_sha: str) -> dict[str, str]:
	commit = _mapping(client.get(f"/git/commits/{_sha(commit_sha)}"), "Git commit")
	tree_sha = _sha(_mapping(commit.get("tree"), "commit tree").get("sha"))
	response = _mapping(client.get(f"/git/trees/{tree_sha}?recursive=1"), "Git tree")
	if response.get("truncated") is True:
		raise RolloutError("repository tree is truncated; refusing to miss active workflow pins")
	result: dict[str, str] = {}
	for index, raw_entry in enumerate(_sequence(response.get("tree"), "Git tree entries"), 1):
		entry = _mapping(raw_entry, f"tree entry {index}")
		path = _text(entry.get("path"), f"tree entry {index} path")
		if entry.get("type") == "blob":
			result[path] = _sha(entry.get("sha"))
		elif path.startswith(".github/workflows/") and entry.get("mode") == "120000":
			raise RolloutError(f"workflow file is a symbolic link: {path}")
	return result


def _branch_sha(client: GitHub, branch: str) -> str | None:
	response = client.get(f"/git/ref/heads/{quote(branch, safe='')}")
	if response is None:
		return None
	ref = _mapping(response, f"branch ref {branch}")
	return _sha(_mapping(ref.get("object"), "branch object").get("sha"))


def _pull_requests(
	client: GitHub, state: str, base: str, *, head: str | None = None
) -> list[dict[str, object]]:
	query = {"state": state, "base": base, "per_page": str(PR_PAGE_SIZE)}
	if head is not None:
		query["head"] = head
	pulls: list[dict[str, object]] = []
	for page in range(1, PR_MAX_PAGES + 1):
		query["page"] = str(page)
		value = _sequence(client.get(f"/pulls?{urlencode(query)}"), "pull request list")
		pulls.extend(_mapping(item, "pull request") for item in value)
		if len(value) < PR_PAGE_SIZE:
			return pulls
	raise RolloutError("pull request history exceeds the bounded rollout lookup")


def _new_branch(version: str, repository: str) -> str:
	slug = re.sub(r"[^a-z0-9-]+", "-", repository.casefold()).strip("-")
	return f"{ROLLOUT_PREFIX}v{version}-{slug}"


def _target_marker(repository: str) -> str:
	return f"<!-- quality-gate-rollout:target={repository} -->"


def _release_marker(version: str) -> str:
	return f"<!-- quality-gate-rollout:release=v{version} -->"


def _pull_version(pull: Mapping[str, object]) -> tuple[int, ...]:
	body = _text(pull.get("body"), "rollout PR body")
	versions = re.findall(r"<!-- quality-gate-rollout:release=v([0-9.]+) -->", body)
	if len(versions) != 1:
		raise RolloutError("managed rollout PR must identify exactly one release")
	return tuple(map(int, _version(versions[0]).split(".")))


def _rollout_body(repository: str, version: str, provider_sha: str) -> str:
	return (
		f"Активация опубликованной policy v{version}. Проверьте изменения перед слиянием.\n\n"
		f"Provider revision: {provider_sha}\n\n"
		f"{_target_marker(repository)}\n{_release_marker(version)}\n\n"
		"## Release\n\n- No release: Активация уже опубликованной policy без изменения продукта.\n"
	)


def _sync_branch_with_base(
	client: GitHub, branch: str, base_branch: str, base_sha: str, head_sha: str
) -> str:
	comparison = _mapping(client.get(f"/compare/{base_sha}...{head_sha}"), "branch comparison")
	behind = comparison.get("behind_by", 0)
	if not isinstance(behind, int) or isinstance(behind, bool) or behind < 0:
		raise RolloutError("GitHub returned an invalid branch comparison")
	if behind:
		client.post(
			"/merges",
			{
				"base": branch,
				"head": base_branch,
				"commit_message": "chore: sync default branch into Quality Gate rollout",
			},
		)
		updated_sha = _branch_sha(client, branch)
		if updated_sha is None:
			raise RolloutError("rollout branch disappeared while syncing the default branch")
		return updated_sha
	return head_sha


def _managed_open_pull(
	client: GitHub, owner: str, repository: str, base_branch: str
) -> dict[str, object] | None:
	target = f"{owner}/{repository}"
	managed: list[dict[str, object]] = []
	for pull in _pull_requests(client, "open", base_branch):
		head = _mapping(pull.get("head"), "pull request head")
		head_repo = head.get("repo")
		if not isinstance(head_repo, dict):
			continue
		branch = head.get("ref")
		body = pull.get("body")
		if not isinstance(branch, str) or not branch.startswith(ROLLOUT_PREFIX):
			continue
		if not isinstance(body, str):
			raise RolloutError("managed rollout pull request has no marker body")
		if _target_marker(target) not in body:
			raise RolloutError("managed rollout branch has an unexpected target marker")
		head_name = _mapping(head_repo, "pull request head repository").get("full_name")
		if not isinstance(head_name, str) or head_name.casefold() != target.casefold():
			raise RolloutError("managed rollout pull request head is outside its target repository")
		managed.append(pull)
	if len(managed) > 1:
		raise RolloutError("multiple open rollout pull requests exist for this repository")
	return managed[0] if managed else None


def _commit_updates(
	client: GitHub,
	parent_sha: str,
	updates: Mapping[str, str],
	version: str,
) -> str:
	entries: list[dict[str, str]] = []
	for path, content in sorted(updates.items()):
		blob = client.post("/git/blobs", {"content": content, "encoding": "utf-8"})
		entries.append(
			{"path": path, "mode": "100644", "type": "blob", "sha": _sha(blob.get("sha"))}
		)
	commit = _mapping(client.get(f"/git/commits/{_sha(parent_sha)}"), "base commit")
	base_tree_sha = _sha(_mapping(commit.get("tree"), "base commit tree").get("sha"))
	tree = client.post("/git/trees", {"base_tree": base_tree_sha, "tree": entries})
	created = client.post(
		"/git/commits",
		{
			"message": f"chore: update Quality Gate to v{version}",
			"tree": _sha(tree.get("sha")),
			"parents": [_sha(parent_sha)],
		},
	)
	return _sha(created.get("sha"))


def _publish_branch(
	client: GitHub,
	branch: str,
	commit_sha: str,
	*,
	existing_branch: bool,
) -> None:
	if existing_branch:
		client.patch(
			f"/git/refs/heads/{quote(branch, safe='')}",
			{"sha": commit_sha, "force": False},
		)
	else:
		client.post("/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit_sha})


def _ensure_pull_request(
	client: GitHub,
	open_pull: dict[str, object] | None,
	branch: str,
	base_branch: str,
	repository: str,
	version: str,
	provider_sha: str,
) -> str:
	title = f"chore: update Quality Gate to v{version}"
	body = _rollout_body(repository, version, provider_sha)
	if open_pull is not None:
		number = open_pull.get("number")
		if not isinstance(number, int):
			raise RolloutError("rollout pull request number is invalid")
		if open_pull.get("body") != body or open_pull.get("title") != title:
			client.patch(f"/pulls/{number}", {"title": title, "body": body})
		return f"updated pull request #{number}"
	created = client.post(
		"/pulls",
		{
			"title": title,
			"head": branch,
			"base": base_branch,
			"body": body,
			"maintainer_can_modify": True,
		},
	)
	return f"created pull request #{created.get('number')}"


def _update_rollout(
	client: GitHub,
	owner: str,
	repository: str,
	version: str,
	provider_sha: str,
	updates_for_tree: UpdateFunction,
) -> str:
	info = _mapping(client.get("/"), "repository metadata")
	base_branch = _text(info.get("default_branch"), "default branch")
	base_sha = _branch_sha(client, base_branch)
	if base_sha is None:
		raise RolloutError("default branch ref is missing")
	open_pull = _managed_open_pull(client, owner, repository, base_branch)
	if open_pull is not None and _pull_version(open_pull) > tuple(map(int, version.split("."))):
		return "skipped older rollout event"
	if open_pull is None:
		branch = _new_branch(version, repository)
		closed = _pull_requests(client, "closed", base_branch)
		if any(
			_target_marker(f"{owner}/{repository}") in str(pull.get("body", ""))
			and _pull_version(pull) >= tuple(map(int, version.split(".")))
			for pull in closed
		):
			return f"skipped closed rollout for v{version}"
		parent_sha = base_sha
		working_tree = _tree(client, parent_sha)
		existing_branch = _branch_sha(client, branch) is not None
		if existing_branch:
			raise RolloutError("existing rollout branch has no managed open pull request")
	else:
		head = _mapping(open_pull.get("head"), "rollout PR head")
		branch = _text(head.get("ref"), "rollout PR branch")
		if branch == base_branch:
			raise RolloutError("rollout branch must differ from the default branch")
		parent_sha = _sha(head.get("sha"))
		# Validate both inputs before a merge can mutate the managed PR branch.
		updates_for_tree(client, _tree(client, base_sha))
		updates_for_tree(client, _tree(client, parent_sha))
		parent_sha = _sync_branch_with_base(client, branch, base_branch, base_sha, parent_sha)
		working_tree = _tree(client, parent_sha)
		existing_branch = True
	if branch == base_branch:
		raise RolloutError("rollout branch must differ from the default branch")
	updates = updates_for_tree(client, working_tree)
	if not updates:
		if open_pull is None:
			return "no Quality Gate pin changes"
		return _ensure_pull_request(
			client,
			open_pull,
			branch,
			base_branch,
			f"{owner}/{repository}",
			version,
			provider_sha,
		)
	commit_sha = _commit_updates(client, parent_sha, updates, version)
	_publish_branch(
		client,
		branch,
		commit_sha,
		existing_branch=existing_branch,
	)
	return _ensure_pull_request(
		client,
		open_pull,
		branch,
		base_branch,
		f"{owner}/{repository}",
		version,
		provider_sha,
	)


def _verify_published_release(
	client: GitHub, repository: str, version: str, provider_sha: str
) -> None:
	release = _mapping(client.get(f"/releases/tags/v{version}"), "published release")
	if (
		release.get("draft") is not False
		or release.get("prerelease") is not False
		or release.get("tag_name") != f"v{version}"
	):
		raise RolloutError("release must be published with the exact stable version tag")
	body = _text(release.get("body"), "published release body")
	if body.count(RECEIPT_MARKER) != 1 or not body.endswith("-->\n"):
		raise RolloutError("published release lacks its retained receipt")
	receipt = _mapping(json.loads(body.split(RECEIPT_MARKER)[1][:-4]), "publication receipt")
	candidate = _mapping(receipt.get("candidate"), "receipt candidate")
	verification = _mapping(receipt.get("verification"), "receipt verification")
	tag = _mapping(client.get(f"/commits/v{version}"), "published tag commit")
	if (
		receipt.get("schema") != 1
		or candidate.get("repository") != repository
		or candidate.get("provider_repository") != repository
		or candidate.get("version") != version
		or _sha(candidate.get("source_sha")) != tag.get("sha")
		or verification.get("provider_sha") != provider_sha
	):
		raise RolloutError("event identity conflicts with the published release receipt")


def activate_provider(client: GitHub, repository: str, version: str, provider_sha: str) -> str:
	"""Validate the published event and update only a provider activation PR branch."""
	version = _version(version)
	provider_sha = _sha(provider_sha)
	if re.fullmatch(r"[A-Za-z0-9_.-]+/quality-gate", repository) is None:
		raise RolloutError("provider activation must target the Quality Gate repository")
	_verify_published_release(client, repository, version, provider_sha)
	owner, name = repository.split("/")
	return _update_rollout(
		client,
		owner,
		name,
		version,
		provider_sha,
		lambda api, tree: _provider_updates(api, tree, version),
	)


def dispatch_release(client: GitHub, repository: str, version: str, provider_sha: str) -> None:
	"""Emit the stable rollout event only when its identity matches published readback."""
	version = _version(version)
	provider_sha = _sha(provider_sha)
	_verify_published_release(client, repository, version, provider_sha)
	client.post(
		"/dispatches",
		{
			"event_type": "quality-gate-release-published",
			"client_payload": {"version": version, "provider_sha": provider_sha},
		},
	)


def main() -> int:
	"""Activate a provider from validated repository-dispatch or recovery inputs."""
	logging.basicConfig(level=logging.INFO, format="%(message)s")
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("mode", choices=("provider", "dispatch"), nargs="?", default="provider")
	arguments = parser.parse_args()
	try:
		repository = _text(os.environ.get("GITHUB_REPOSITORY"), "GITHUB_REPOSITORY")
		version = _version(os.environ.get("QUALITY_GATE_RELEASE_VERSION"))
		provider_sha = _sha(os.environ.get("QUALITY_GATE_PROVIDER_SHA"))
		client = GitHubCLI(repository, timeout_seconds=120)
		if arguments.mode == "dispatch":
			dispatch_release(client, repository, version, provider_sha)
			return 0
		LOGGER.info(
			"%s: %s", repository, activate_provider(client, repository, version, provider_sha)
		)
		return 0
	except (PublicationError, RolloutError, OSError, ValueError) as error:
		LOGGER.error("provider activation failed: %s", error)
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
