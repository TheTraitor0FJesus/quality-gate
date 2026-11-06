"""Provider-specific policy activation for published Quality Gate releases."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable, Mapping

from .release_rollout import (
	GitHub,
	RolloutError,
	_mapping_file,
	_sha,
	_text,
	_update_rollout,
	_verify_published_release,
	_version,
	remove_marked_waivers,
	update_policy_release,
)


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


def _provider_updates(client: GitHub, tree: Mapping[str, str], version: str) -> dict[str, str]:
	"""Return changed provider manifest and policy assertion contents for version.

	Read files through client without publishing updates. Prefer the split CI
	contract test when present. Raise RolloutError for missing assertions or a
	policy downgrade; file retrieval and TOML parsing errors propagate."""
	policy_version = f"v{_version(version)}"
	assertion_suffix = version.replace(".", "")
	policy_test_path = next(
		(
			path
			for path in (
				"tests/test_ci_workflow_contract.py",
				"tests/test_ci_workflow.py",
			)
			if path in tree
		),
		None,
	)
	if policy_test_path is None:
		raise RolloutError("provider policy assertion test is missing")
	paths = (
		"quality-gate.toml",
		"tests/fixtures/release-parity/quality-gate.toml",
		"tests/test_integrity.py",
	)
	contents = {path: _mapping_file(client, tree, path) for path in paths}
	contents[policy_test_path] = _mapping_file(client, tree, policy_test_path)
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
	contents[policy_test_path] = _replace_one(
		contents[policy_test_path],
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
