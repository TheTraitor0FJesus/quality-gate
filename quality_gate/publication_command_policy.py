"""Redact command identities and classify bounded diagnostics."""

from __future__ import annotations

import re
from collections.abc import Sequence

GH_API_ARGUMENTS = 3
HTTP_STATUS = re.compile(
	rb"""(?:\(\s*HTTP\s+([1-5][0-9]{2})\)|\bHTTP\s+([1-5][0-9]{2})\b|
		(?:response\s+)?status\s+code:?\s*([1-5][0-9]{2})\b|\b(404)\s+Not Found\b)""",
	re.IGNORECASE | re.VERBOSE,
)


def _operation(command: Sequence[str]) -> str:
	"""Describe one external operation without echoing arguments or query data."""
	if len(command) >= GH_API_ARGUMENTS and command[0] == "gh" and command[1] == "api":
		method = "GET"
		if "--method" in command:
			index = command.index("--method") + 1
			candidate = command[index] if index < len(command) else ""
			if candidate in {"GET", "POST", "PATCH", "DELETE"}:
				method = candidate
		endpoint = command[2].split("?", 1)[0]
		if endpoint.startswith("https://uploads.github.com/"):
			endpoint = endpoint.removeprefix("https://uploads.github.com/")
		if re.fullmatch(
			r"repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.@-]+)*",
			endpoint,
		):
			return f"gh api {method} {endpoint}"
		return f"gh api {method} <endpoint>"
	if command and command[0] == "docker":
		docker_subcommands = tuple(command[1:])
		if docker_subcommands[:2] in {
			("buildx", "imagetools"),
			("image", "inspect"),
			("image", "tag"),
		}:
			return f"docker {docker_subcommands[0]} {docker_subcommands[1]}"
		if docker_subcommands and docker_subcommands[0] in {"load", "pull", "push"}:
			return f"docker {docker_subcommands[0]}"
		return "docker command"
	if command and command[0] == "git":
		return "git command"
	return "external command"


def _http_status(detail: bytes) -> str | None:
	"""Extract only a bounded HTTP status from a child diagnostic."""
	match = HTTP_STATUS.search(detail)
	if match is None:
		return None
	return next((group.decode("ascii") for group in match.groups() if group), None)
