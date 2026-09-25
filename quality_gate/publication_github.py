"""Standard GitHub CLI transport without product installation or startup."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol
from urllib.parse import quote

from .publication import PublicationError
from .publication_artifacts import MAX_BUNDLE_BYTES, json_record
from .publication_evidence import positive, record

JSON_LIMIT = 16 * 1024 * 1024
MAX_TIMEOUT_SECONDS = 300
MAX_GHCR_TOKEN_BYTES = 64 * 1024
MAX_GHCR_USERNAME_LENGTH = 255
GH_ESCAPE_SEQUENCE_ERROR = (
	b"the response contains terminal escape sequences; pass --allow-escape-sequences"
)
HTTP_STATUS = re.compile(rb"(?:\(\s*HTTP\s+([1-5][0-9]{2})\)|\bHTTP\s+([1-5][0-9]{2})\b)")
GH_API_ARGUMENTS = 3


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


class MissingResourceError(PublicationError):
	"""GitHub positively reported HTTP 404 for an optional resource."""


class EscapeSequenceError(PublicationError):
	"""GitHub CLI refused a raw response containing terminal escape sequences."""


class BoundedCommand:
	"""Read a child stream with a size limit and a watchdog independent of stream progress."""

	def __init__(
		self, timeout_seconds: float, *, environment: Mapping[str, str] | None = None
	) -> None:
		if not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS:
			raise PublicationError("GitHub command timeout must be within 300 seconds")
		self.timeout_seconds = timeout_seconds
		self.environment = dict(environment or {})

	def __call__(
		self, arguments: Sequence[str], *, content: bytes | None = None, limit: int
	) -> bytes:
		with tempfile.TemporaryDirectory(prefix="publisher-") as directory:
			command = list(arguments)
			if content is not None:
				input_file = Path(directory) / "input"
				input_file.write_bytes(content)
				command.extend(["--input", str(input_file)])
			with (Path(directory) / "stderr").open("w+b") as errors:
				return self._execute(command, errors, limit)

	def _execute(self, command: list[str], errors: BinaryIO, limit: int) -> bytes:
		operation = _operation(command)
		environment = os.environ.copy()
		environment.update(self.environment)
		try:
			with subprocess.Popen(
				command,
				stdin=subprocess.DEVNULL,
				stdout=subprocess.PIPE,
				stderr=errors,
				env=environment,
			) as process:
				timed_out = threading.Event()

				def terminate() -> None:
					timed_out.set()
					try:
						process.kill()
					except OSError:
						pass

				watchdog = threading.Timer(self.timeout_seconds, terminate)
				watchdog.start()
				try:
					output = self._read(process, limit)
					code = process.wait(timeout=self.timeout_seconds)
				finally:
					watchdog.cancel()
				if timed_out.is_set():
					raise PublicationError(
						f"{operation}: timed out after {self.timeout_seconds:g} seconds"
					)
				if code:
					errors.seek(0)
					detail = errors.read(4096)
					status = _http_status(detail)
					if status == "404":
						raise MissingResourceError(f"{operation}: resource is absent (HTTP 404)")
					if status is None and GH_ESCAPE_SEQUENCE_ERROR in detail:
						raise EscapeSequenceError(
							f"{operation}: raw response contains terminal escape sequences"
						)
					status_detail = f", HTTP {status}" if status else ""
					raise PublicationError(
						f"{operation}: command failed (exit {code}{status_detail})"
					)
				return output
		except subprocess.TimeoutExpired as error:
			raise PublicationError(
				f"{operation}: timed out after {self.timeout_seconds:g} seconds"
			) from error
		except OSError as error:
			raise PublicationError(f"{operation}: command unavailable") from error

	@staticmethod
	def _read(process: subprocess.Popen[bytes], limit: int) -> bytes:
		if process.stdout is None:
			raise PublicationError("command has no readable output")
		chunks: list[bytes] = []
		total = 0
		while chunk := process.stdout.read(min(1024 * 1024, limit - total + 1)):
			total += len(chunk)
			if total > limit:
				process.kill()
				raise PublicationError("external command response exceeds its size limit")
			chunks.append(chunk)
		return b"".join(chunks)


class GitHubCLI:
	"""GitHub.com repository API plus content-addressed registry readback."""

	def __init__(
		self,
		repository: str,
		*,
		timeout_seconds: float,
		command: Command | None = None,
		release_command: Command | None = None,
	) -> None:
		if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None:
			raise PublicationError("invalid GitHub repository")
		self.repository = repository
		self.command = command or BoundedCommand(timeout_seconds)
		self.release_command = release_command or self.command

	def _arguments(
		self, path: str, method: str, *, allow_escape_sequences: bool = False
	) -> list[str]:
		if not path.startswith("/") or ".." in path or "\\" in path:
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
		*,
		release_operation: bool = False,
	) -> object:
		content = json.dumps(payload).encode("utf-8") if payload is not None else None
		resource_path = path.split("?", 1)[0]
		is_release_resource = resource_path == "/releases" or resource_path.startswith("/releases/")
		command = self.release_command if release_operation or is_release_resource else self.command
		raw = command(self._arguments(path, method), content=content, limit=JSON_LIMIT)
		try:
			value = json.loads(raw)
		except (UnicodeError, json.JSONDecodeError) as error:
			raise PublicationError("GitHub returned invalid JSON") from error
		if isinstance(value, dict) and "message" in value:
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
		return record(self._json(path, "POST", payload, release_operation=True), "created resource")

	def patch(self, path: str, payload: Mapping[str, object]) -> dict[str, object]:
		return record(
			self._json(path, "PATCH", payload, release_operation=True), "updated resource"
		)

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
		raw = self.release_command(
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
		content = self.command(
			["docker", "buildx", "imagetools", "inspect", reference, "--raw"], limit=JSON_LIMIT
		)
		return hashlib.sha256(content).hexdigest()
