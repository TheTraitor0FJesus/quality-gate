"""Standard GitHub CLI transport without product installation or startup."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Protocol
from urllib.parse import quote

from .publication import (
	MissingResourceError,
	PublicationError,
	image_repository,
)
from .publication_artifacts import MAX_BUNDLE_BYTES, json_record
from .publication_command import (
	MAX_IMAGE_WRITER_TIMEOUT_SECONDS as MAX_IMAGE_WRITER_TIMEOUT_SECONDS,
)
from .publication_command import (
	BoundedCommand as BoundedCommand,
)
from .publication_command import (
	EscapeSequenceError as EscapeSequenceError,
)
from .publication_evidence import positive, record
from .publication_writer import preflight_image_archive, push_image_archive

JSON_LIMIT = 16 * 1024 * 1024
MAX_GHCR_TOKEN_BYTES = 64 * 1024
MAX_GHCR_USERNAME_LENGTH = 255


@contextmanager
def registry_configuration(token: str, username: str) -> Iterator[None]:
	"""Expose the caller's short-lived GHCR token through a temporary Docker config."""
	if not token or not username:
		raise PublicationError("caller GITHUB_TOKEN and actor are required for GHCR access")
	if len(token.encode("utf-8")) > MAX_GHCR_TOKEN_BYTES:
		raise PublicationError("GHCR token exceeds 64 KiB")
	if any(character.isspace() or character == "\x00" for character in token):
		raise PublicationError("GHCR token contains invalid characters")
	if len(username) > MAX_GHCR_USERNAME_LENGTH or any(
		character.isspace() or character in ":\x00" for character in username
	):
		raise PublicationError("GitHub actor is invalid for GHCR authentication")
	auth = base64.b64encode(f"{username}:{token}".encode()).decode("ascii")
	content = json.dumps({"auths": {"ghcr.io": {"auth": auth}}}, separators=(",", ":"))
	previous = os.environ.get("DOCKER_CONFIG")
	with tempfile.TemporaryDirectory(prefix="publisher-registry-") as directory:
		path = Path(directory) / "config.json"
		path.write_text(content, encoding="utf-8")
		path.chmod(0o600)
		os.environ["DOCKER_CONFIG"] = directory
		try:
			yield
		finally:
			if previous is None:
				os.environ.pop("DOCKER_CONFIG", None)
			else:
				os.environ["DOCKER_CONFIG"] = previous


class Command(Protocol):
	def __call__(
		self, arguments: Sequence[str], *, content: bytes | None = None, limit: int
	) -> bytes: ...


class GitHubCLI:
	"""GitHub.com repository API plus content-addressed registry readback."""

	def __init__(
		self,
		repository: str,
		*,
		timeout_seconds: float,
		command: Command | None = None,
		image_command: Command | None = None,
	) -> None:
		if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None:
			raise PublicationError("invalid GitHub repository")
		self.repository = repository
		self.command = command or BoundedCommand(timeout_seconds)
		self.image_command = image_command or self.command

	def _arguments(
		self, path: str, method: str, *, allow_escape_sequences: bool = False
	) -> list[str]:
		is_commit_comparison = (
			method == "GET"
			and re.fullmatch(r"/compare/[0-9a-f]{40}\.\.\.[0-9a-f]{40}", path) is not None
		)
		if not path.startswith("/") or (".." in path and not is_commit_comparison) or "\\" in path:
			raise PublicationError("invalid repository API path")
		endpoint = f"repos/{self.repository}" + ("" if path == "/" else path)
		arguments = [
			"gh",
			"api",
			endpoint,
			"--method",
			method,
			"--header",
			"Accept: application/vnd.github+json",
			"--header",
			"X-GitHub-Api-Version: 2022-11-28",
		]
		if allow_escape_sequences:
			arguments.append("--allow-escape-sequences")
		return arguments

	def _json(
		self,
		path: str,
		method: str,
		payload: Mapping[str, object] | None = None,
	) -> object:
		content = json.dumps(payload).encode("utf-8") if payload is not None else None
		raw = self.command(self._arguments(path, method), content=content, limit=JSON_LIMIT)
		if path == "/dispatches" and method == "POST" and raw == b"":
			return {}
		try:
			value = json.loads(raw)
		except (UnicodeError, json.JSONDecodeError) as error:
			raise PublicationError("GitHub returned invalid JSON") from error
		if isinstance(value, dict) and "message" in value:
			commit_sha = value.get("sha")
			commit_tree = value.get("tree")
			commit_tree_sha = commit_tree.get("sha") if isinstance(commit_tree, dict) else None
			commit_path = (method == "POST" and path == "/git/commits") or (
				method == "GET" and re.fullmatch(r"/git/commits/[0-9a-f]{40}", path) is not None
			)
			is_commit = (
				commit_path
				and isinstance(value.get("message"), str)
				and isinstance(commit_sha, str)
				and re.fullmatch(r"[0-9a-f]{40}", commit_sha) is not None
				and isinstance(commit_tree_sha, str)
				and re.fullmatch(r"[0-9a-f]{40}", commit_tree_sha) is not None
				and isinstance(value.get("parents"), list)
				and not any(key in value for key in ("documentation_url", "errors", "status"))
			)
			# Git Data commit resources have a top-level commit message; BoundedCommand
			# already rejects unsuccessful GitHub CLI invocations before parsing stdout.
			if not is_commit:
				raise PublicationError("GitHub returned an API error object")
		return value

	def get(self, path: str) -> object:
		try:
			return self._json(path, "GET")
		except MissingResourceError:
			return None

	def source(self, sha: str, path: str) -> str:
		if (
			re.fullmatch(r"[0-9a-f]{40}", sha) is None
			or PurePosixPath(path).is_absolute()
			or ".." in PurePosixPath(path).parts
			or re.fullmatch(r"[A-Za-z0-9_./-]+", path) is None
		):
			raise PublicationError("invalid exact-source path or revision")
		response = record(self.get(f"/contents/{path}?ref={sha}"), "source content")
		if response.get("encoding") != "base64" or not isinstance(response.get("content"), str):
			raise PublicationError("source content is missing or too large for the contents API")
		try:
			content = base64.b64decode(str(response["content"]).replace("\n", ""), validate=True)
			if len(content) > 1024 * 1024:
				raise PublicationError("source content exceeds 1 MiB")
			return content.decode("utf-8")
		except (ValueError, UnicodeError) as error:
			raise PublicationError("source content encoding is invalid") from error

	def post(self, path: str, payload: Mapping[str, object]) -> dict[str, object]:
		try:
			return record(self._json(path, "POST", payload), "created resource")
		except PublicationError as error:
			if path == "/git/refs" and "HTTP 403" in str(error):
				raise PublicationError(
					"GitHub denied creation of the release tag (HTTP 403). "
					"If this is the historical Workflows: write restriction, "
					"keep the original candidate and its source SHA. "
					"If v<version> is absent, an authorized maintainer may "
					"create and push only that exact tag with an already-"
					"authenticated Git client, then rerun only the failed "
					"Publish release job in this original workflow run. "
					"This reuses the successful producer and image-writer "
					"results; do not dispatch a new run, rerun all jobs, or "
					"repush the image. Stop and ask the owner for manual "
					"recovery if any other job failed, a required producer "
					"or writer artifact or upload log is missing or expired, "
					"the publisher-only rerun is unavailable, or the tag "
					"points elsewhere. Do not change "
					"the source or expand GITHUB_TOKEN permissions."
				) from error
			raise

	def patch(self, path: str, payload: Mapping[str, object]) -> dict[str, object]:
		return record(self._json(path, "PATCH", payload), "updated resource")

	def download(self, path: str) -> bytes:
		limit = JSON_LIMIT if path.endswith("/logs") else MAX_BUNDLE_BYTES
		arguments = self._arguments(path, "GET")
		try:
			return self.command(arguments, limit=limit)
		except EscapeSequenceError:
			if re.fullmatch(r"/actions/jobs/[0-9]+/logs", path) is None:
				raise
			return self.command(
				self._arguments(path, "GET", allow_escape_sequences=True), limit=limit
			)

	def upload(self, release_id: int, name: str, content: bytes) -> dict[str, object]:
		positive(release_id, "release ID")
		endpoint = (
			f"https://uploads.github.com/repos/{self.repository}/releases/{release_id}"
			f"/assets?name={quote(name, safe='')}"
		)
		raw = self.command(
			[
				"gh",
				"api",
				endpoint,
				"--method",
				"POST",
				"--header",
				"Content-Type: application/octet-stream",
			],
			content=content,
			limit=JSON_LIMIT,
		)
		return json_record(raw)

	def image_digest(self, reference: str) -> str:
		if re.fullmatch(r"[a-z0-9][a-z0-9._:/-]+@sha256:[0-9a-f]{64}", reference) is None:
			raise PublicationError("image reference must be a registry path pinned by SHA-256")
		content = self.image_command(
			["docker", "buildx", "imagetools", "inspect", reference, "--raw"], limit=JSON_LIMIT
		)
		return hashlib.sha256(content).hexdigest()

	def preflight_image_archive(
		self,
		content: bytes,
		source: str,
		repository: str,
		version: str,
		producer: str,
		*,
		expected_digest: str | None = None,
	) -> None:
		"""Check one configured image tag before any candidate image tag is pushed."""
		if re.fullmatch(r"[a-z][a-z0-9-]{0,31}", producer) is None:
			raise PublicationError("invalid image producer name")
		package = image_repository(repository)
		preflight_image_archive(
			content,
			source,
			f"{package}:v{version}",
			self.image_command,
			expected_digest=expected_digest,
		)

	def publish_image_archive(
		self,
		content: bytes,
		source: str,
		repository: str,
		version: str,
		producer: str,
		*,
		expected_digest: str | None = None,
	) -> dict[str, str]:
		"""Push one source-verified image archive and return its immutable GHCR identity."""
		if re.fullmatch(r"[a-z][a-z0-9-]{0,31}", producer) is None:
			raise PublicationError("invalid image producer name")
		package = image_repository(repository)
		target = f"{package}:v{version}"
		registry_digest = push_image_archive(
			content,
			source,
			target,
			self.image_command,
			expected_digest=expected_digest,
		)
		return {
			"reference": f"{package}@sha256:{registry_digest}",
			"sha256": registry_digest,
			"tag": target,
		}
