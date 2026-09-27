"""Workflow entry point for the shared publisher; no product code is imported."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path

from .publication import PublicationError
from .publication_artifacts import ArtifactReader, load_json_record
from .publication_evidence import record
from .publication_flow import Context, Publisher, canonical, sha
from .publication_github import (
	JSON_LIMIT,
	MAX_IMAGE_WRITER_TIMEOUT_SECONDS,
	BoundedCommand,
	GitHubCLI,
	registry_configuration,
)
from .temp_workspace import TemporaryWorkspaceError, temporary_workspace


def _configured_timeout(timeouts: Mapping[str, object], name: str) -> float:
	value = timeouts.get(name)
	if isinstance(value, bool) or not isinstance(value, (int, float)):
		raise PublicationError(f"release timeout {name} must be numeric")
	return float(value)


def _helper(root: Path, expected: str, command: BoundedCommand) -> str:
	identity = (
		command(
			["git", "-C", str(root), "rev-parse", "HEAD"],
			limit=JSON_LIMIT,
		)
		.decode("utf-8")
		.strip()
	)
	if sha(identity) != sha(expected):
		raise PublicationError("helper checkout differs from the actual reusable workflow revision")
	result = command(
		["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
		limit=JSON_LIMIT,
	)
	if result.strip():
		raise PublicationError("helper checkout contains modified or untracked code")
	return identity


def _publisher(root: Path) -> Publisher:
	provider_sha = sha(os.environ.get("PUBLISHER_SHA"))
	settings = tomllib.loads((root / ".release/release-tools.toml").read_text(encoding="utf-8"))
	timeouts = record(settings.get("timeouts"), "release timeouts")
	github_api_timeout = _configured_timeout(timeouts, "github_api_seconds")
	image_writer_timeout = _configured_timeout(timeouts, "image_writer_seconds")
	evidence_wait = _configured_timeout(timeouts, "actions_evidence_wait_seconds")
	evidence_poll = _configured_timeout(timeouts, "actions_evidence_poll_seconds")
	command = BoundedCommand(github_api_timeout)
	image_command = BoundedCommand(
		image_writer_timeout,
		maximum_timeout_seconds=MAX_IMAGE_WRITER_TIMEOUT_SECONDS,
		aggregate_timeout_seconds=image_writer_timeout,
	)
	helper_sha = _helper(root, provider_sha, command)
	api = GitHubCLI(
		os.environ["GITHUB_REPOSITORY"],
		timeout_seconds=command.timeout_seconds,
		command=command,
		image_command=image_command,
	)
	run_id = int(os.environ["GITHUB_RUN_ID"])
	attempt = int(os.environ["GITHUB_RUN_ATTEMPT"])
	run = ArtifactReader(
		api,
		wait_seconds=float(evidence_wait),
		poll_seconds=float(evidence_poll),
	).run(run_id, expected_attempt=attempt)
	context = Context(
		repository=os.environ["GITHUB_REPOSITORY"],
		provider_repository=os.environ["PUBLISHER_REPOSITORY"],
		provider_sha=provider_sha,
		helper_sha=helper_sha,
		run_id=run_id,
		attempt=attempt,
		run_source_sha=sha(os.environ.get("GITHUB_SHA")),
		run_head_sha=sha(run.get("head_sha")),
	)
	return Publisher(
		api,
		context,
		evidence_wait_seconds=float(evidence_wait),
		evidence_poll_seconds=float(evidence_poll),
	)


def _outputs(result: Mapping[str, object], publisher: Publisher, directory: Path) -> None:
	outputs = {
		"status": result["status"],
		"version": result["version"],
		"source-sha": result.get("source_sha", ""),
		"candidate-id": result.get("candidate_artifact_id", ""),
		"provider-sha": publisher.context.provider_sha,
		"provider-repository": publisher.context.provider_repository,
		"save-required": "false",
		"candidate-run-id": "",
		"candidate-digest": "",
		"artifact-name": "",
	}
	candidate = result.get("candidate")
	if isinstance(candidate, dict):
		content = canonical(candidate)
		outputs.update(
			{
				"candidate-run-id": candidate["origin_run_id"],
				"candidate-digest": hashlib.sha256(content).hexdigest(),
				"artifact-name": (
					f"release-candidate-pr-{candidate['pr']}-"
					f"{candidate['origin_run_id']}-{candidate['origin_attempt']}"
				),
			}
		)
		if not result.get("candidate_artifact_id"):
			directory.mkdir(parents=True, exist_ok=True)
			temporary = directory / "candidate.json.tmp"
			temporary.write_bytes(content)
			temporary.replace(directory / "candidate.json")
			outputs["save-required"] = "true"
	with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
		for key, value in outputs.items():
			if "\n" in str(value) or "\r" in str(value):
				raise PublicationError("invalid multiline workflow output")
			output.write(f"{key}={value}\n")


def _parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description=__doc__)
	commands = parser.add_subparsers(dest="command", required=True)
	prepare = commands.add_parser("prepare")
	prepare.add_argument("--pr", type=int, required=True)
	prepare.add_argument("--output", type=Path, required=True)
	prepare.add_argument("--validation-only", action="store_true")
	publish = commands.add_parser("publish")
	publish.add_argument("--pr", type=int, required=True)
	publish.add_argument("--candidate-id", type=int, required=True)
	write_images = commands.add_parser("write-images")
	write_images.add_argument("--pr", type=int, required=True)
	write_images.add_argument("--candidate-id", type=int, required=True)
	write_images.add_argument("--output", type=Path, required=True)
	return parser


def _main(
	arguments: Sequence[str] | None = None, *, options: argparse.Namespace | None = None
) -> int:
	options = options if options is not None else _parser().parse_args(arguments)
	try:
		publisher = _publisher(Path(__file__).resolve().parents[1])
		if options.command == "publish":
			result = publisher.publish(options.pr, options.candidate_id)
		elif options.command == "write-images":
			result = publisher.write_images(options.pr, options.candidate_id, options.output)
		else:
			with Path(os.environ["GITHUB_EVENT_PATH"]).open("rb") as event_file:
				event = load_json_record(event_file)
			if os.environ["GITHUB_EVENT_NAME"] == "workflow_dispatch":
				if options.validation_only:
					raise PublicationError("dispatch is recovery, not open-PR validation")
				result = publisher.recover(options.pr)
			else:
				if os.environ["GITHUB_EVENT_NAME"] != "pull_request":
					raise PublicationError(
						"only pull_request and recovery dispatch events are supported"
					)
				if options.validation_only and event.get("action") not in {
					"opened",
					"synchronize",
					"reopened",
					"edited",
				}:
					raise PublicationError("unsupported validation event")
				result = publisher.prepare(
					options.pr, event=event, validation_only=options.validation_only
				)
			_outputs(result, publisher, options.output)
		sys.stdout.write(
			json.dumps(
				{key: value for key, value in result.items() if key != "candidate"},
				ensure_ascii=False,
			)
			+ "\n"
		)
		return 0
	except (PublicationError, OSError) as error:
		sys.stderr.write(f"publication: refused - {error}\n")
		return 1


def main(arguments: Sequence[str] | None = None) -> int:
	"""Run shared publication with temporary GHCR auth only for image operations."""
	try:
		with temporary_workspace(Path(__file__).resolve().parents[1]):
			options = _parser().parse_args(arguments)
			if options.command == "prepare":
				return _main(arguments, options=options)
			with registry_configuration(
				os.environ.get("GH_TOKEN", ""),
				os.environ.get("GITHUB_ACTOR", ""),
			):
				return _main(arguments, options=options)
	except TemporaryWorkspaceError as error:
		sys.stderr.write(f"publication: temporary workspace refused - {error}\n")
		return 1
	except (PublicationError, OSError) as error:
		sys.stderr.write(f"publication: GHCR configuration refused - {error}\n")
		return 1
