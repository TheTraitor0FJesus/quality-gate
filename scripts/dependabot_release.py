"""Add the repository's no-release decision to Dependabot pull requests."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import Protocol

from quality_gate.publication import PublicationError, parse_decision
from quality_gate.publication_evidence import record
from quality_gate.publication_github import GitHubCLI

DECISION = "## Release\n\n- No release: Обновление зависимостей Dependabot.\n"


class PullAPI(Protocol):
	def get(self, path: str) -> object: ...
	def patch(self, path: str, payload: Mapping[str, object]) -> dict[str, object]: ...


def _body(pull: dict[str, object], number: int) -> str:
	author = record(pull.get("user"), "pull request author")
	if (
		pull.get("number") != number
		or pull.get("state") != "open"
		or author.get("login") != "dependabot[bot]"
		or author.get("type") != "Bot"
	):
		raise PublicationError("pull request is not an open Dependabot PR")
	body = pull.get("body")
	if body is None:
		body = ""
	if not isinstance(body, str):
		raise PublicationError("pull request body is invalid")
	return body


def _updated_body(body: str) -> str | None:
	try:
		decision = parse_decision(body)
	except PublicationError as error:
		if str(error) != "exactly one second-level Release section is required":
			raise
	else:
		if "No release" not in decision:
			raise PublicationError("pull request already has an explicit release decision")
		return None
	updated = body + ("\n\n" if body else "") + DECISION
	if "No release" not in parse_decision(updated):
		raise PublicationError("could not construct a no-release decision")
	return updated


def update_decision(api: PullAPI, number: int) -> bool:
	"""Preserve the current body and reject ambiguous or changed PR metadata."""
	if type(number) is not int or number <= 0:
		raise PublicationError("invalid pull request number")
	path = f"/pulls/{number}"
	pull = record(api.get(path), "pull request")
	updated = _updated_body(_body(pull, number))
	if updated is None:
		return False
	latest = record(api.get(path), "current pull request")
	head = record(pull.get("head"), "pull request head").get("sha")
	base = record(pull.get("base"), "pull request base").get("ref")
	if (
		not isinstance(head, str)
		or not isinstance(base, str)
		or _body(latest, number) != _body(pull, number)
		or record(latest.get("head"), "current pull request head").get("sha") != head
		or record(latest.get("base"), "current pull request base").get("ref") != base
	):
		raise PublicationError("pull request changed while its decision was prepared")
	if api.patch(path, {"body": updated}).get("body") != updated:
		raise PublicationError("GitHub did not retain the updated decision")
	return True


def main() -> None:
	api = GitHubCLI(os.environ["GITHUB_REPOSITORY"], timeout_seconds=60)
	updated = update_decision(api, int(os.environ["PR_NUMBER"]))
	sys.stdout.write(
		"Dependabot release decision added\n" if updated else "Decision already exists\n"
	)


if __name__ == "__main__":
	main()
