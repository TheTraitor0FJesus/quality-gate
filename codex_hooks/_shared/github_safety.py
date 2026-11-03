"""Safety policy for GitHub MCP operations that can modify branch refs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GithubViolation:
	"""A blocked GitHub MCP operation."""

	rule_id: str
	label: str


_CONTENT_WRITE_TOOLS = {"create_file", "delete_file", "update_file"}
_UNVERIFIABLE_PR_WRITES = {"enable_auto_merge", "merge_pull_request"}
_KNOWN_TOOLS = (
	_CONTENT_WRITE_TOOLS
	| _UNVERIFIABLE_PR_WRITES
	| {
		"create_branch",
		"update_ref",
	}
)


def _github_tool_name(tool_name: str) -> str | None:
	"""Return the GitHub operation name from a qualified MCP tool name."""
	lowered = tool_name.casefold()
	for operation in _KNOWN_TOOLS:
		if lowered == f"github_{operation}" or lowered.endswith(f"__github_{operation}"):
			return operation
	return None


def _is_main_ref(value: object) -> bool:
	if not isinstance(value, str):
		return False
	normalized = value.strip().casefold().lstrip("+")
	for prefix in (
		"refs/remotes/origin/",
		"refs/heads/",
		"origin/",
		"heads/",
	):
		if normalized.startswith(prefix):
			normalized = normalized[len(prefix) :]
			break
	return normalized == "main"


def evaluate_github_tool(tool_name: str, tool_input: dict[str, Any]) -> GithubViolation | None:
	"""Return a main-protection violation, or ``None`` when the call is allowed."""
	operation = _github_tool_name(tool_name)
	if operation is None:
		return None

	protected = GithubViolation(
		"github-main-protected",
		"modify the protected main branch through GitHub MCP",
	)
	if operation in _CONTENT_WRITE_TOOLS:
		branch = tool_input.get("branch")
		if not isinstance(branch, str) or not branch.strip():
			return GithubViolation(
				"github-default-branch-write",
				"write to the repository default branch, which may be main",
			)
		return protected if _is_main_ref(branch) else None
	if operation in {"create_branch", "update_ref"}:
		return protected if _is_main_ref(tool_input.get("branch_name")) else None
	if operation in _UNVERIFIABLE_PR_WRITES:
		return GithubViolation(
			"github-pr-target-unverified",
			"merge a pull request whose target branch the hook cannot verify",
		)
	return None
