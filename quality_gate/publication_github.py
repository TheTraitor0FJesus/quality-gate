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
MAX_REGISTRY_AUTH_BYTES = 64 * 1024


@contextmanager
def registry_configuration(content: str) -> Iterator[None]:
	"""Expose caller-owned registry auth temporarily, without executable credential helpers."""
	if not content:
		yield
		return
	if len(content.encode("utf-8")) > MAX_REGISTRY_AUTH_BYTES:
		raise PublicationError("registry auth exceeds 64 KiB")
	configuration = json_record(content.encode("utf-8"))
	if set(configuration) != {"auths"}:
		raise PublicationError("registry configuration supports only inline auths")
	for credentials in record(configuration["auths"], "registry auths").values():
		entry = record(credentials, "registry credentials")
		if set(entry) != {"auth"} or not isinstance(entry["auth"], str):
			raise PublicationError("registry credentials require only an inline auth string")
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


class BoundedCommand:
	"""Read a child stream with a size limit and a watchdog independent of stream progress."""

	def __init__(self, timeout_seconds: float) -> None:
		if not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS:
			raise PublicationError("GitHub command timeout must be within 300 seconds")
		self.timeout_seconds = timeout_seconds

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
		try:
			with subprocess.Popen(
				command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors
			) as process:
				watchdog = threading.Timer(self.timeout_seconds, process.kill)
				watchdog.start()
				try:
					output = self._read(process, limit)
					code = process.wait(timeout=self.timeout_seconds)
				finally:
					watchdog.cancel()
				if code:
					errors.seek(0)
					detail = errors.read(4096)
					if b"(HTTP 404)" in detail:
						raise MissingResourceError("GitHub resource is absent (HTTP 404)")
					raise PublicationError(
						f"GitHub/registry command failed or timed out (exit {code}); "
						"inspect runner diagnostics"
					)
				return output
		except (OSError, subprocess.TimeoutExpired) as error:
			raise PublicationError("GitHub/registry command is unavailable or timed out") from error

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
		self, repository: str, *, timeout_seconds: float, command: Command | None = None
	) -> None:
		if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None:
			raise PublicationError("invalid GitHub repository")
		self.repository = repository
		self.command = command or BoundedCommand(timeout_seconds)

	def _arguments(self, path: str, method: str) -> list[str]:
		if not path.startswith("/") or ".." in path or "\\" in path:
			raise PublicationError("invalid repository API path")
		endpoint = f"repos/{self.repository}" + ("" if path == "/" else path)
		return [
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

	def _json(self, path: str, method: str, payload: Mapping[str, object] | None = None) -> object:
		content = json.dumps(payload).encode("utf-8") if payload is not None else None
		raw = self.command(self._arguments(path, method), content=content, limit=JSON_LIMIT)
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
		return record(self._json(path, "POST", payload), "created resource")

	def patch(self, path: str, payload: Mapping[str, object]) -> dict[str, object]:
		return record(self._json(path, "PATCH", payload), "updated resource")

	def download(self, path: str) -> bytes:
		limit = JSON_LIMIT if path.endswith("/logs") else MAX_BUNDLE_BYTES
		return self.command(self._arguments(path, "GET"), limit=limit)

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
		content = self.command(
			["docker", "buildx", "imagetools", "inspect", reference, "--raw"], limit=JSON_LIMIT
		)
		return hashlib.sha256(content).hexdigest()
