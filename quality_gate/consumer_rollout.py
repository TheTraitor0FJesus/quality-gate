"""Open consumer activation pull requests after verified Quality Gate publication."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .publication import PublicationError
from .publication_github import GitHubCLI
from .release_rollout import (
	LOGGER,
	GitHub,
	RolloutError,
	_mapping,
	_mapping_file,
	_sequence,
	_sha,
	_text,
	_update_rollout,
	_verify_published_release,
	_version,
	update_policy_release,
)


@dataclass(frozen=True)
class Consumer:
	"""One registered repository and its optional Quality Gate paths."""

	owner: str
	name: str
	policy_manifest: str | None
	additional_workflow_files: tuple[str, ...]
	activation: str = "consumer"

	@property
	def full_name(self) -> str:
		return f"{self.owner}/{self.name}"


def _safe_path(value: object, description: str) -> str:
	path = _text(value, description)
	pure_path = PurePosixPath(path)
	if (
		pure_path.is_absolute()
		or ".." in pure_path.parts
		or "\\" in path
		or not path.endswith((".yml", ".yaml", ".toml", ".py"))
	):
		raise RolloutError(f"{description} must be a safe repository-relative text path")
	return path


def _consumer(owner: str, raw_target: object, index: int) -> Consumer:
	target = _mapping(raw_target, f"consumer {index}")
	name = _text(target.get("repository"), f"consumer {index} repository")
	if re.fullmatch(r"[A-Za-z0-9_.-]+", name) is None:
		raise RolloutError(f"consumer {index} repository name is invalid")
	manifest_path = target.get("policy_manifest")
	if manifest_path is not None:
		manifest_path = _safe_path(manifest_path, f"consumer {index} policy_manifest")
	workflow_files = tuple(
		_safe_path(item, f"consumer {index} additional workflow file")
		for item in _sequence(
			target.get("additional_workflow_files", []),
			f"consumer {index} additional_workflow_files",
		)
	)
	activation = _text(target.get("activation", "consumer"), f"consumer {index} activation")
	if activation not in {"consumer", "provider"}:
		raise RolloutError(f"consumer {index} activation must be consumer or provider")
	if (name.casefold() == "quality-gate") != (activation == "provider"):
		raise RolloutError("quality-gate must use its separate provider activation route")
	return Consumer(owner, name, manifest_path, workflow_files, activation)


def load_consumers(path: Path) -> tuple[str, tuple[Consumer, ...]]:
	"""Load the owner and consumer repositories from the public JSON manifest."""
	try:
		manifest = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as error:
		raise RolloutError("consumer manifest is unreadable or invalid JSON") from error
	data = _mapping(manifest, "consumer manifest")
	if data.get("schema_version") != 1:
		raise RolloutError("consumer manifest schema_version must be 1")
	owner = _text(data.get("owner"), "consumer manifest owner")
	if re.fullmatch(r"[A-Za-z0-9_.-]+", owner) is None:
		raise RolloutError("consumer manifest owner is invalid")
	consumers: list[Consumer] = []
	seen: set[str] = set()
	for index, raw_target in enumerate(_sequence(data.get("consumers"), "consumers"), 1):
		target = _consumer(owner, raw_target, index)
		if target.full_name.casefold() in seen:
			raise RolloutError(f"duplicate consumer repository: {target.full_name}")
		seen.add(target.full_name.casefold())
		consumers.append(target)
	if not consumers:
		raise RolloutError("consumer manifest must contain at least one repository")
	return owner, tuple(consumers)


def update_workflow_references(content: str, provider: str, provider_sha: str, version: str) -> str:
	"""Pin every reusable Quality Gate workflow reference in one configured file."""
	sha = _sha(provider_sha)
	policy_version = f"v{_version(version)}"
	pattern = re.compile(
		r"^(?P<prefix>[ \t]*uses:[ \t]*)(?P<quote>['\"]?)(?P<workflow>"
		+ re.escape(provider)
		+ r"/\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml@)"
		r"(?:[A-Za-z0-9_./-]+|<40-character-commit-sha>)(?P=quote)"
		r"[ \t]*(?P<comment>#[^\r\n]*)?(?P<cr>\r?)$",
		re.IGNORECASE | re.MULTILINE,
	)
	all_references = len(_workflow_reference_pattern(provider).findall(content))

	def replace_reference(item: re.Match[str]) -> str:
		comment = item.group("comment") or ""
		if not comment or re.fullmatch(
			r"#[ \t]*(?:(?:quality-gate[ \t]+)?v[0-9]+\.[0-9]+\.[0-9]+|<release-tag>)",
			comment,
		):
			comment = f"# {policy_version}"
		return (
			f"{item.group('prefix')}{item.group('quote')}{item.group('workflow')}"
			f"{sha}{item.group('quote')} {comment}{item.group('cr')}"
		)

	updated, count = pattern.subn(replace_reference, content)
	if count != all_references:
		raise RolloutError("a Quality Gate workflow reference has an unsupported format")
	return updated


def _workflow_reference_pattern(provider: str) -> re.Pattern[str]:
	return re.compile(
		r"^[^\r\n#]*\buses:[ \t]*['\"]?" + re.escape(provider) + r"/\.github/workflows/",
		re.IGNORECASE | re.MULTILINE,
	)


def _consumer_updates(
	client: GitHub,
	tree: Mapping[str, str],
	target: Consumer,
	provider: str,
	version: str,
	provider_sha: str,
) -> dict[str, str]:
	paths = {
		path
		for path in tree
		if path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml"))
	}
	paths.update(target.additional_workflow_files)
	missing = sorted(path for path in paths if path not in tree)
	if missing:
		raise RolloutError(f"configured workflow file is missing: {missing[0]}")
	updates: dict[str, str] = {}
	reference_count = 0
	for path in sorted(paths):
		content = _mapping_file(client, tree, path)
		reference_pattern = _workflow_reference_pattern(provider)
		found = len(reference_pattern.findall(content))
		if path in target.additional_workflow_files and not found:
			raise RolloutError(f"configured workflow reference is missing: {path}")
		if found:
			reference_count += found
			updated = update_workflow_references(content, provider, provider_sha, version)
			if updated != content:
				updates[path] = updated
	if reference_count == 0:
		raise RolloutError("no active Quality Gate reusable-workflow references were found")
	if target.policy_manifest is not None:
		manifest = _mapping_file(client, tree, target.policy_manifest)
		updated = update_policy_release(manifest, version)
		if updated != manifest:
			updates[target.policy_manifest] = updated
	return updates


def _run_target(
	owner: str,
	target: Consumer,
	version: str,
	provider_sha: str,
	provider_repository: str,
) -> str:
	client = GitHubCLI(target.full_name, timeout_seconds=120)
	return _update_rollout(
		client,
		owner,
		target.name,
		version,
		provider_sha,
		lambda api, tree: _consumer_updates(
			api, tree, target, provider_repository, version, provider_sha
		),
	)


def _registered_consumers(repository: str) -> tuple[str, tuple[Consumer, ...]]:
	match = re.fullmatch(r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<name>[A-Za-z0-9_.-]+)", repository)
	if match is None:
		raise RolloutError("GITHUB_REPOSITORY is invalid")
	owner = match.group("owner")
	if match.group("name").casefold() != "quality-gate":
		raise RolloutError("consumer rollout must run from the Quality Gate repository")
	manifest_owner, consumers = load_consumers(Path(".github/quality-gate-consumers.json"))
	if manifest_owner.casefold() != owner.casefold():
		raise RolloutError("consumer manifest owner must match GITHUB_REPOSITORY")
	return owner, tuple(target for target in consumers if target.activation == "consumer")


def _run(argv: Sequence[str] | None = None) -> int:
	parser = argparse.ArgumentParser(
		description="Create Quality Gate release rollout pull requests."
	)
	parser.add_argument("target", choices=("consumers", "targets"))
	parser.add_argument("--repository", help="Process one registered consumer repository.")
	arguments = parser.parse_args(argv)
	repository = _text(os.environ.get("GITHUB_REPOSITORY"), "GITHUB_REPOSITORY")
	owner, consumers = _registered_consumers(repository)
	if arguments.target == "targets":
		sys.stdout.write(json.dumps([target.name for target in consumers]) + "\n")
		return 0
	if arguments.repository is not None:
		consumers = tuple(target for target in consumers if target.name == arguments.repository)
		if not consumers:
			raise RolloutError("selected repository must be a registered consumer target")
	version = _version(os.environ.get("QUALITY_GATE_RELEASE_VERSION"))
	provider_sha = _sha(os.environ.get("QUALITY_GATE_PROVIDER_SHA"))
	_verify_published_release(
		GitHubCLI(repository, timeout_seconds=120), repository, version, provider_sha
	)
	failures = 0
	for target in consumers:
		try:
			result = _run_target(owner, target, version, provider_sha, repository)
			LOGGER.info("%s: %s", target.full_name, result)
		except (PublicationError, RolloutError, OSError, ValueError) as error:
			failures += 1
			LOGGER.error("%s rollout failed: %s", target.full_name, error)
	if failures:
		raise RolloutError(f"{failures} consumer rollout target(s) failed")
	return 0


def main() -> int:
	"""Run one rollout after validating event inputs."""
	logging.basicConfig(level=logging.INFO, format="%(message)s")
	try:
		return _run()
	except (PublicationError, RolloutError, OSError, ValueError) as error:
		LOGGER.error("rollout failed: %s", error)
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
