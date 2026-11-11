"""Identify the Quality Gate provider repository by its Git origin."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from .integrity_workflow_release import _QUALITY_GATE_PROVIDER


def _is_repository_root(path: Path) -> bool:
	try:
		requested_root = path.resolve()
		result = subprocess.run(
			[
				"git",
				"-C",
				str(requested_root),
				"rev-parse",
				"--show-toplevel",
			],
			capture_output=True,
			check=False,
			timeout=5,
		)
		if result.returncode:
			return False
		git_root = Path(result.stdout.decode("utf-8").strip()).resolve()
	except (OSError, RuntimeError, UnicodeError, subprocess.TimeoutExpired):
		return False
	return os.path.normcase(str(git_root)) == os.path.normcase(str(requested_root))


def _is_quality_gate_origin(repository_root: Path) -> bool:
	"""Bind provider-only workflow exceptions to the checkout's actual origin."""
	github_repository = os.environ.get("GITHUB_REPOSITORY")
	if github_repository and github_repository.casefold() != _QUALITY_GATE_PROVIDER.casefold():
		return False
	if not _is_repository_root(repository_root):
		return False
	try:
		result = subprocess.run(
			["git", "-C", str(repository_root), "remote", "get-url", "origin"],
			capture_output=True,
			check=False,
			timeout=5,
		)
	except (OSError, subprocess.TimeoutExpired):
		return False
	if result.returncode:
		return False
	try:
		remote = result.stdout.decode("utf-8").strip()
	except UnicodeError:
		return False
	if remote.startswith("git@github.com:"):
		path = remote.removeprefix("git@github.com:")
	else:
		try:
			parsed = urlsplit(remote)
			port = parsed.port
		except ValueError:
			return False
		if (
			parsed.scheme.casefold() not in {"https", "ssh"}
			or (parsed.hostname or "").casefold() != "github.com"
			or port not in {None, 22 if parsed.scheme.casefold() == "ssh" else 443}
			or parsed.query
			or parsed.fragment
		):
			return False
		if parsed.password or (
			parsed.username and not (parsed.scheme.casefold() == "ssh" and parsed.username == "git")
		):
			return False
		path = parsed.path.lstrip("/")
	path = path.removesuffix(".git").rstrip("/")
	return path.casefold() == _QUALITY_GATE_PROVIDER.casefold()
