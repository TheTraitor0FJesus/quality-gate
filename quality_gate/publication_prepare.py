"""Publisher preparation operations."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping

from .publication import PublicationError, parse_decision, prepare
from .publication_artifacts import json_record
from .publication_config import validate_config
from .publication_evidence import positive, record, records
from .publication_flow_context import _PublisherContext
from .publication_flow_support import (
	DEPENDABOT_LOGIN,
	MAX_NOTES_BYTES,
	MAX_PULL_FILES,
	PAGE_SIZE,
	RECEIPT_MARKER,
	WORKFLOW_YAML_PATH,
	_authority,
	_projection,
	_toml,
	canonical,
	sha,
)


class _Preparation(_PublisherContext):
	def _releases(self) -> list[dict[str, object]]:
		result: list[dict[str, object]] = []
		for page in range(1, 101):
			items = records(self.api.get(f"/releases?per_page=100&page={page}"), "releases")
			result.extend(items)
			if len(items) < PAGE_SIZE:
				return result
		raise PublicationError("release history exceeds 10,000 entries")

	def _baseline(self) -> str:
		versions = [
			str(item["tag_name"])[1:]
			for item in self._releases()
			if not item.get("draft")
			and not item.get("prerelease")
			and re.fullmatch(
				r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", str(item.get("tag_name"))
			)
		]
		if not versions:
			raise PublicationError(
				"published stable baseline is unavailable; explicit bootstrap is required"
			)
		version = max(versions, key=lambda item: tuple(int(part) for part in item.split(".")))
		if self._tag(f"v{version}") is None:
			raise PublicationError("published baseline tag is missing")
		return version

	def _tag(self, tag: str) -> str | None:
		value = self.api.get(f"/git/ref/tags/{tag}")
		if value is None:
			return None
		for _ in range(4):
			identity = record(record(value, "tag").get("object"), "tag object")
			commit = sha(identity.get("sha"))
			if identity.get("type") == "commit":
				return commit
			if identity.get("type") != "tag":
				break
			value = self.api.get(f"/git/tags/{commit}")
		raise PublicationError("tag does not resolve to a commit within four indirections")

	def _pull(self, number: int, *, merged: bool) -> dict[str, object]:
		positive(number, "PR number")
		repository = record(self.api.get("/"), "repository")
		pull = record(self.api.get(f"/pulls/{number}"), "pull request")
		base = record(pull.get("base"), "PR base")
		if (
			pull.get("number") != number
			or repository.get("full_name") != self.context.repository
			or base.get("ref") != repository.get("default_branch")
			or record(base.get("repo"), "base repository").get("full_name")
			!= self.context.repository
		):
			raise PublicationError("PR does not target this repository's default branch")
		if merged and (
			pull.get("merged") is not True
			or not pull.get("merged_at")
			or record(pull.get("merged_by"), "PR merger").get("login")
			!= record(repository.get("owner"), "owner").get("login")
		):
			raise PublicationError("publication requires an owner-merged PR")
		return pull

	def _dependabot_workflow_only(
		self,
		number: int,
		event_pull: Mapping[str, object],
		pull: Mapping[str, object],
	) -> bool:
		"""Recognize only a complete, stable Dependabot workflow-YAML-only file list."""
		event_user = record(event_pull.get("user"), "original PR author")
		current_user = record(pull.get("user"), "current PR author")
		event_head = record(event_pull.get("head"), "original PR head")
		current_head = record(pull.get("head"), "current PR head")
		event_base = record(event_pull.get("base"), "original PR base")
		current_base = record(pull.get("base"), "current PR base")
		if (
			event_user.get("login") != DEPENDABOT_LOGIN
			or event_user.get("type") != "Bot"
			or current_user.get("login") != DEPENDABOT_LOGIN
			or current_user.get("type") != "Bot"
			or event_head.get("sha") != current_head.get("sha")
			or event_base.get("sha") != current_base.get("sha")
			or event_base.get("ref") != current_base.get("ref")
		):
			return False

		changed_file_count = pull.get("changed_files")
		if (
			type(changed_file_count) is not int
			or changed_file_count <= 0
			or changed_file_count > MAX_PULL_FILES
		):
			return False
		page_count = (changed_file_count + PAGE_SIZE - 1) // PAGE_SIZE
		found = 0
		for page in range(1, page_count + 1):
			files = records(
				self.api.get(f"/pulls/{number}/files?per_page={PAGE_SIZE}&page={page}"),
				"PR changed files",
			)
			if not files or len(files) > PAGE_SIZE:
				return False
			for changed_file in files:
				filename = changed_file.get("filename")
				previous_filename = changed_file.get("previous_filename")
				if not isinstance(filename, str) or WORKFLOW_YAML_PATH.fullmatch(filename) is None:
					return False
				if previous_filename is not None and (
					not isinstance(previous_filename, str)
					or WORKFLOW_YAML_PATH.fullmatch(previous_filename) is None
				):
					return False
			found += len(files)
		if found != changed_file_count:
			return False

		latest = record(self.api.get(f"/pulls/{number}"), "current PR")
		latest_head = record(latest.get("head"), "latest PR head")
		latest_base = record(latest.get("base"), "latest PR base")
		return (
			latest_head.get("sha") == current_head.get("sha")
			and latest_base.get("sha") == current_base.get("sha")
			and latest_base.get("ref") == current_base.get("ref")
			and latest.get("changed_files") == changed_file_count
			and latest.get("body") == pull.get("body")
		)

	def _metadata(self, source: str) -> tuple[str, dict[str, object], list[str]]:
		version = _authority(self.api.source(source, ".release/version.toml"))
		config = _toml(self.api.source(source, ".release/publisher.toml"))
		validate_config(config)
		projections = [
			_projection(self.api.source(source, str(item.get("path"))), item)
			for item in records(config.get("projections", []), "projections")
		]
		return version, config, projections

	def prepare(
		self,
		number: int,
		*,
		event: Mapping[str, object],
		validation_only: bool = False,
	) -> dict[str, object]:
		"""Resolve exact PR source and return validated, no-release, or a candidate envelope."""
		pull = self._pull(number, merged=not validation_only)
		original = record(event.get("pull_request"), "original PR event")
		if original.get("number") != number or original.get("body") != pull.get("body"):
			raise PublicationError(
				"current PR decision differs from the original event; restore its body"
			)
		source = (
			sha(record(pull.get("head"), "PR head").get("sha"))
			if validation_only
			else sha(pull.get("merge_commit_sha"))
		)
		if not validation_only and (
			event.get("action") != "closed"
			or original.get("merged") is not True
			or original.get("merge_commit_sha") != source
			or source != self.context.run_source_sha
			or record(original.get("head"), "original PR head").get("sha")
			!= self.context.run_head_sha
		):
			raise PublicationError("event does not authorize the exact final merged source")
		version, config, projections = self._metadata(source)
		base = sha(record(pull.get("base"), "PR base").get("sha"))
		completed = (
			self._completed(pull, source, version, config, projections)
			if not validation_only
			else None
		)
		if completed is not None:
			return completed
		if not validation_only and self._original_artifacts(number):
			return self.recover(number)
		base_version = _authority(self.api.source(base, ".release/version.toml"))
		baseline = self._baseline()
		body = str(original.get("body") or "")
		try:
			result = prepare(
				body=body,
				version=version,
				base_version=base_version,
				baseline=baseline,
				projections=projections,
			)
		except PublicationError as error:
			if (
				str(error) != "exactly one second-level Release section is required"
				or not self._dependabot_workflow_only(number, original, pull)
			):
				raise
			result = prepare(
				body=(
					"## Release\n\n"
					"- No release: Dependabot updated GitHub Actions workflow YAML only."
				),
				version=version,
				base_version=base_version,
				baseline=baseline,
				projections=projections,
			)
		if validation_only:
			return result
		if not result["release"]:
			return {**result, "status": "no-release"}
		notes = self.api.source(source, ".release/notes.md")
		if not notes.strip() or len(notes.encode("utf-8")) > MAX_NOTES_BYTES:
			raise PublicationError("source release notes must be nonempty and at most 64 KiB")
		candidate = {
			"schema": 1,
			"repository": self.context.repository,
			"pr": number,
			"source_sha": source,
			"base_sha": base,
			"merged_at": original.get("merged_at"),
			"body": original.get("body"),
			"version": version,
			"notes": notes,
			"notes_sha256": hashlib.sha256(notes.encode("utf-8")).hexdigest(),
			"provider_repository": self.context.provider_repository,
			"provider_sha": self.context.provider_sha,
			"origin_run_id": self.context.run_id,
			"origin_attempt": self.context.attempt,
			"origin_run_head_sha": self.context.run_head_sha,
			"config": config,
		}
		return {
			"status": "build-required",
			"version": version,
			"source_sha": source,
			"candidate": candidate,
		}

	def _existing(self, version: str) -> dict[str, object] | None:
		matches = [item for item in self._releases() if item.get("tag_name") == f"v{version}"]
		if len(matches) > 1:
			raise PublicationError("duplicate conflicting releases or drafts")
		return matches[0] if matches else None

	def _completed(
		self,
		pull: Mapping[str, object],
		source: str,
		version: str,
		config: Mapping[str, object],
		projections: list[str],
	) -> dict[str, object] | None:
		# An absent section must reach normal preparation; explicit no-release decisions
		# must not inspect an older publication's legacy receipt.
		try:
			decision = parse_decision(str(pull.get("body") or ""))
		except PublicationError as error:
			if str(error) != "exactly one second-level Release section is required":
				raise
			return None
		if "No release" in decision:
			return None
		release = self._existing(version)
		if release is None or release.get("draft") is True:
			return None
		body = str(release.get("body", ""))
		if body.count(RECEIPT_MARKER) != 1 or not body.endswith("-->\n"):
			raise PublicationError("existing publication lacks the original candidate receipt")
		receipt = json_record(body.split(RECEIPT_MARKER)[1].removesuffix("-->\n").encode("utf-8"))
		candidate = record(receipt.get("candidate"), "completed candidate")
		self._validate_candidate(candidate, pull)
		if candidate.get("config") != config or any(item != version for item in projections):
			raise PublicationError("completed release source projections or contract conflict")
		self._readback(release, candidate, receipt)
		return {"status": "already-complete", "version": version, "source_sha": source}

	def _validate_candidate(
		self, candidate: Mapping[str, object], pull: Mapping[str, object]
	) -> None:
		source = sha(candidate.get("source_sha"))
		version, config, projections = self._metadata(source)
		notes = self.api.source(source, ".release/notes.md")
		if parse_decision(str(candidate.get("body") or "")).get("Version") != version:
			raise PublicationError("original candidate decision does not authorize its version")
		if (
			candidate.get("schema") != 1
			or candidate.get("repository") != self.context.repository
			or candidate.get("provider_repository") != self.context.provider_repository
			or candidate.get("pr") != pull.get("number")
			or source != pull.get("merge_commit_sha")
			or candidate.get("base_sha") != record(pull.get("base"), "PR base").get("sha")
			or candidate.get("origin_run_head_sha")
			!= record(pull.get("head"), "PR head").get("sha")
			or candidate.get("body") != pull.get("body")
			or candidate.get("merged_at") != pull.get("merged_at")
			or candidate.get("version") != version
			or candidate.get("config") != config
			or candidate.get("notes") != notes
			or candidate.get("notes_sha256") != hashlib.sha256(notes.encode("utf-8")).hexdigest()
			or any(item != version for item in projections)
		):
			raise PublicationError(
				"retained original candidate conflicts with PR decision or exact source; "
				"restore original body"
			)

	def _candidate(self, number: int, artifact_id: int) -> dict[str, object]:
		artifact, files = self.artifacts.read(artifact_id)
		if set(files) != {"candidate.json"}:
			raise PublicationError("candidate envelope artifact must contain only candidate.json")
		candidate = json_record(files["candidate.json"])
		self._validate_candidate(candidate, self._pull(number, merged=True))
		run_id = positive(candidate.get("origin_run_id"), "original run ID")
		attempt = positive(candidate.get("origin_attempt"), "original attempt")
		job = self.artifacts.jobs_for_attempt(
			run_id, "Prepare release / Prepare candidate", attempt, ["Prepare candidate"]
		)
		config = record(candidate.get("config"), "candidate configuration")
		self.artifacts.prove(
			artifact,
			job=job,
			repository=self.context.repository,
			run_id=run_id,
			run_head_sha=sha(candidate.get("origin_run_head_sha")),
			current_attempt=attempt,
			provider_repository=str(candidate.get("provider_repository")),
			provider_sha=sha(candidate.get("provider_sha")),
			workflow=str(config.get("workflow")),
			name=f"release-candidate-pr-{number}-{run_id}-{attempt}",
			checks=["Prepare candidate"],
		)
		return candidate

	def _original_artifacts(self, number: int) -> list[dict[str, object]]:
		artifacts = self.artifacts.pages("/actions/artifacts", "artifacts")
		return [
			item
			for item in artifacts
			if re.fullmatch(rf"release-candidate-pr-{number}-[0-9]+-[0-9]+", str(item.get("name")))
			and item.get("expired") is False
		]

	def recover(self, number: int) -> dict[str, object]:
		"""Recover by PR identity using proven original envelopes, never an editable body alone."""
		self._recovery_context()
		pull = self._pull(number, merged=True)
		source = sha(pull.get("merge_commit_sha"))
		version, config, projections = self._metadata(source)
		completed = self._completed(pull, source, version, config, projections)
		if completed is not None:
			return completed
		matches = self._original_artifacts(number)
		if not matches:
			raise PublicationError(
				"original candidate envelope is missing or expired; "
				"rerun its original merge event or prepare a new reviewed PR"
			)
		candidates = [
			(
				positive(item.get("id"), "candidate artifact ID"),
				self._candidate(number, positive(item.get("id"), "candidate artifact ID")),
			)
			for item in matches
		]
		if len({canonical(candidate) for _, candidate in candidates}) != 1:
			raise PublicationError("original candidate envelope lookup is ambiguous")
		artifact_id, candidate = min(candidates, key=lambda item: item[0])
		prepare(
			body=str(candidate["body"]),
			version=version,
			base_version=_authority(
				self.api.source(sha(candidate["base_sha"]), ".release/version.toml")
			),
			baseline=self._baseline(),
			projections=projections,
		)
		return {
			"status": "build-required",
			"version": version,
			"source_sha": source,
			"candidate": candidate,
			"candidate_artifact_id": artifact_id,
		}

	def _recovery_context(self) -> None:
		run = self.artifacts.run(self.context.run_id, expected_attempt=self.context.attempt)
		if run.get("event") == "workflow_dispatch":
			repository = record(self.api.get("/"), "repository")
			if run.get("head_branch") != repository.get("default_branch"):
				raise PublicationError("recovery tooling must run from the default branch")
			if run.get("head_sha") != self.context.run_source_sha:
				raise PublicationError("recovery tooling source differs from its Actions run")
