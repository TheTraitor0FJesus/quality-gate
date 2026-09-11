"""Common source-authorized release controller for the repository migrations."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
	sys.path.insert(0, str(_REPOSITORY_ROOT))

SEMANTIC_VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
TAG_VERSION = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_API_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
# ponytail: cap history at 10,000 releases; switch to an indexed baseline endpoint if growth reaches this ceiling.
MAX_RELEASE_PAGES = 100
REQUIRED_NOTE_HEADINGS = (
	"Interface",
	"Integrations",
	"Configuration",
	"Persisted data",
	"Delivery/runtime",
)
PUBLICATION_DECLARATION = (
	"Publication: owner merge authorizes publication after the merged commit passes the required release checks."
)
DEFAULT_REQUIRED_CHECKS = frozenset({"Quality Gate"})
RELEASE_TIMEOUT_KEYS = frozenset(
	{
		"github_api_seconds",
		"external_download_seconds",
		"subprocess_seconds",
		"check_wait_seconds",
		"check_poll_seconds",
	}
)
RELEASE_TIMEOUT_LIMITS = {
	"github_api_seconds": 300.0,
	"external_download_seconds": 1800.0,
	"subprocess_seconds": 3600.0,
	"check_wait_seconds": 3600.0,
	"check_poll_seconds": 60.0,
}


class ReleaseError(RuntimeError):
	"""The reviewed source candidate cannot be published safely."""


class GitHubNotFound(ReleaseError):
	"""A requested GitHub release or tag does not exist."""


class ChecksPending(ReleaseError):
	"""The required source checks have not reached a terminal state."""


@dataclass(frozen=True, slots=True)
class ReleaseIntent:
	"""Versioned release notes reviewed in the source tree."""

	version: str
	impact: str
	notes: str

	@property
	def tag(self) -> str:
		return f"v{self.version}"


@dataclass(frozen=True, slots=True)
class VersionProjection:
	"""The projections that must agree with the repository version authority."""

	version: str
	package: str
	policy_release: str


@dataclass(frozen=True, slots=True)
class ArtifactIdentity:
	"""One verified platform artifact and the source that produced it."""

	version: str
	source_sha: str
	platform: str
	name: str
	sha256: str
	path: str | None = None

	def as_dict(self) -> dict[str, str]:
		value = asdict(self)
		return {key: item for key, item in value.items() if item is not None}

	@classmethod
	def from_mapping(cls, value: Mapping[str, object]) -> ArtifactIdentity:
		fields = {"version", "source_sha", "platform", "name", "sha256", "path"}
		unknown = set(value) - fields
		if unknown:
			raise ReleaseError(f"artifact identity has unknown fields: {sorted(unknown)}")
		required = {name: value.get(name) for name in ("version", "source_sha", "platform", "name", "sha256")}
		if not all(isinstance(item, str) and item.strip() for item in required.values()):
			raise ReleaseError("artifact identity is incomplete")
		if not isinstance(required["name"], str) or ARTIFACT_NAME.fullmatch(required["name"]) is None:
			raise ReleaseError("artifact identity name is invalid")
		path = value.get("path")
		if path is not None and (not isinstance(path, str) or not path.strip()):
			raise ReleaseError("artifact identity path is invalid")
		return cls(**cast(dict[str, str], required), path=cast(str | None, path))


@dataclass(frozen=True, slots=True)
class ReleaseResult:
	"""Observable result of a verification or publication attempt."""

	status: str
	version: str
	source_sha: str
	artifacts: tuple[ArtifactIdentity, ...] = ()


def load_release_timeout(root: Path | str, name: str) -> float:
	"""Load one positive release-operation timeout from tracked configuration."""
	if name not in RELEASE_TIMEOUT_KEYS:
		raise ReleaseError(f"unknown release timeout: {name}")
	path = Path(root).resolve() / ".release" / "release-tools.toml"
	try:
		config = tomllib.loads(path.read_text(encoding="utf-8"))
	except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
		raise ReleaseError("release tools configuration is unreadable") from error
	timeouts = config.get("timeouts")
	value = timeouts.get(name) if isinstance(timeouts, dict) else None
	if (
		isinstance(value, bool)
		or not isinstance(value, (int, float))
		or not math.isfinite(float(value))
		or value <= 0
		or value > RELEASE_TIMEOUT_LIMITS[name]
	):
		raise ReleaseError(f"release timeout {name} must be finite and within its configured bound")
	return float(value)


class GitHubApi(Protocol):
	"""Injected GitHub boundary used by the controller and its tests."""

	def get(self, path: str) -> object:
		...

	def post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
		...

	def patch(self, path: str, payload: dict[str, object]) -> dict[str, object]:
		...

	def upload(self, upload_url: str, name: str, content: bytes) -> dict[str, object]:
		...


class HttpGitHubApi:
	"""Small stdlib GitHub API client used only by the post-merge workflow."""

	def __init__(
		self,
		repository: str,
		token: str,
		*,
		api_url: str | None = None,
		timeout_seconds: float | None = None,
	) -> None:
		if not token:
			raise ReleaseError("GITHUB_TOKEN is required for release publication")
		self.repository = repository
		self.token = token
		self.api_url = (api_url or "https://api.github.com").rstrip("/")
		self.timeout_seconds = timeout_seconds or load_release_timeout(
			_REPOSITORY_ROOT, "github_api_seconds"
		)

	def _request(
		self,
		method: str,
		url: str,
		*,
		payload: Mapping[str, object] | None = None,
		content: bytes | None = None,
		content_type: str = "application/vnd.github+json",
	) -> object:
		body = None
		if payload is not None:
			body = json.dumps(payload).encode("utf-8")
		elif content is not None:
			body = content
		request = Request(url, data=body, method=method)
		request.add_header("Accept", "application/vnd.github+json")
		request.add_header("X-GitHub-Api-Version", "2026-03-10")
		request.add_header("Authorization", f"Bearer {self.token}")
		request.add_header("Content-Type", content_type)
		try:
			with urlopen(request, timeout=self.timeout_seconds) as response:
				data = read_bounded(response, MAX_API_RESPONSE_BYTES, "GitHub API response")
		except HTTPError as error:
			if error.code == 404:
				raise GitHubNotFound(f"GitHub resource not found: {url}") from error
			raise ReleaseError(f"GitHub API request failed with HTTP {error.code}") from error
		except (OSError, URLError) as error:
			raise ReleaseError("GitHub API request is unavailable") from error
		if not data:
			return {}
		try:
			return json.loads(data.decode("utf-8"))
		except (UnicodeError, json.JSONDecodeError) as error:
			raise ReleaseError("GitHub API returned invalid JSON") from error

	def _repo_url(self, path: str) -> str:
		if path.startswith("http://") or path.startswith("https://"):
			return path
		if path.startswith("/repos/"):
			return f"{self.api_url}{path}"
		return f"{self.api_url}/repos/{self.repository}{path}"

	def get(self, path: str) -> object:
		return self._request("GET", self._repo_url(path))

	def post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
		value = self._request("POST", self._repo_url(path), payload=payload)
		return _object(value, "GitHub POST response")

	def patch(self, path: str, payload: dict[str, object]) -> dict[str, object]:
		value = self._request("PATCH", self._repo_url(path), payload=payload)
		return _object(value, "GitHub PATCH response")

	def upload(self, upload_url: str, name: str, content: bytes) -> dict[str, object]:
		base = upload_url.split("{", 1)[0]
		separator = "&" if "?" in base else "?"
		value = self._request(
			"POST",
			f"{base}{separator}name={quote(name, safe='')}",
			content=content,
			content_type="application/zip",
		)
		return _object(value, "GitHub asset upload response")


def _object(value: object, label: str) -> dict[str, object]:
	if not isinstance(value, dict):
		raise ReleaseError(f"{label} must be an object")
	return cast(dict[str, object], value)


def _list(value: object, label: str) -> list[dict[str, object]]:
	if not isinstance(value, list):
		raise ReleaseError(f"{label} must be a list")
	return [_object(item, f"{label} item") for item in value]


def read_bounded(stream: object, maximum: int, label: str) -> bytes:
	"""Read an external stream while enforcing its declared and actual size."""
	read = getattr(stream, "read", None)
	if not callable(read):
		raise ReleaseError(f"{label} is not readable")
	headers = getattr(stream, "headers", None)
	declared = headers.get("Content-Length") if headers is not None else None
	if declared is not None:
		try:
			if int(declared) > maximum:
				raise ReleaseError(f"{label} exceeds the {maximum}-byte limit")
		except (TypeError, ValueError) as error:
			raise ReleaseError(f"{label} has an invalid Content-Length") from error
	chunks: list[bytes] = []
	total = 0
	while True:
		chunk = cast(bytes, read(min(1024 * 1024, maximum - total + 1)))
		if not chunk:
			break
		total += len(chunk)
		if total > maximum:
			raise ReleaseError(f"{label} exceeds the {maximum}-byte limit")
		chunks.append(chunk)
	return b"".join(chunks)


def validate_source_sha(value: str) -> str:
	"""Require a full immutable lowercase Git commit SHA."""
	if SOURCE_SHA.fullmatch(value) is None:
		raise ReleaseError("source SHA must be exactly 40 lowercase hexadecimal characters")
	return value


def resolve_local_source_sha(root: Path | str) -> str:
	"""Resolve the checked-out commit that supplies the release source files."""
	actual_root = Path(root).resolve()
	try:
		result = subprocess.run(
			["git", "rev-parse", "HEAD"],
			cwd=actual_root,
			capture_output=True,
			text=True,
			encoding="utf-8",
			check=False,
			timeout=load_release_timeout(actual_root, "subprocess_seconds"),
		)
	except subprocess.TimeoutExpired as error:
		raise ReleaseError("local source revision lookup timed out") from error
	except (OSError, UnicodeError) as error:
		raise ReleaseError("local source revision lookup is unavailable") from error
	if result.returncode != 0:
		raise ReleaseError("local source revision lookup failed")
	try:
		return validate_source_sha(result.stdout.strip())
	except ReleaseError as error:
		raise ReleaseError("local source revision is invalid") from error


def _validate_artifact_name(name: str) -> None:
	if ARTIFACT_NAME.fullmatch(name) is None:
		raise ReleaseError("artifact identity name is invalid")


def _artifact_content(artifact: ArtifactIdentity) -> bytes:
	if artifact.path is None:
		raise ReleaseError(f"artifact path is missing for {artifact.name}")
	path = Path(artifact.path)
	try:
		if path.stat().st_size > MAX_ARTIFACT_BYTES:
			raise ReleaseError(f"artifact exceeds the {MAX_ARTIFACT_BYTES}-byte limit: {artifact.name}")
		with path.open("rb") as stream:
			content = read_bounded(stream, MAX_ARTIFACT_BYTES, f"artifact {artifact.name}")
	except OSError as error:
		raise ReleaseError(f"artifact is unreadable: {artifact.name}") from error
	if hashlib.sha256(content).hexdigest() != artifact.sha256:
		raise ReleaseError(f"artifact content does not match its declared digest: {artifact.name}")
	return content


def _version_tuple(value: str) -> tuple[int, int, int]:
	match = TAG_VERSION.fullmatch(value)
	if match is None:
		raise ReleaseError(f"invalid release tag: {value}")
	return tuple(int(part) for part in match.groups())


def _next_version(value: str, impact: str) -> str:
	major, minor, patch = _version_tuple(value)
	if impact == "MAJOR":
		return f"v{major + 1}.0.0"
	if impact == "MINOR":
		return f"v{major}.{minor + 1}.0"
	if impact == "PATCH":
		return f"v{major}.{minor}.{patch + 1}"
	raise ReleaseError(f"unsupported release impact: {impact}")


def _read_text(path: Path, label: str) -> str:
	try:
		value = path.read_text(encoding="utf-8")
	except (OSError, UnicodeError) as error:
		raise ReleaseError(f"{label} is unreadable") from error
	if not value.strip():
		raise ReleaseError(f"{label} is empty")
	return value


def load_version_authority(root: Path | str) -> str:
	"""Read the only product version authority."""
	actual_root = Path(root).resolve()
	path = actual_root / ".release" / "version.toml"
	try:
		raw = tomllib.loads(path.read_text(encoding="utf-8"))
	except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
		raise ReleaseError(".release/version.toml is unreadable") from error
	if set(raw) != {"version"}:
		raise ReleaseError(".release/version.toml must contain exactly one top-level version")
	version = raw.get("version")
	if not isinstance(version, str) or SEMANTIC_VERSION.fullmatch(version) is None:
		raise ReleaseError(".release/version.toml version must be MAJOR.MINOR.PATCH")
	return version


def load_release_intent(root: Path | str) -> ReleaseIntent:
	"""Read and validate the common release notes contract."""
	actual_root = Path(root).resolve()
	version = load_version_authority(actual_root)
	notes = _read_text(actual_root / ".release" / "notes.md", ".release/notes.md")
	version_match = re.search(rf"(?m)^Version:\s*{re.escape(version)}\s*$", notes)
	if version_match is None:
		raise ReleaseError("release notes do not declare the authoritative version")
	impact_match = re.search(r"(?m)^Impact:\s*(MAJOR|MINOR|PATCH|NONE)\s*$", notes)
	if impact_match is None:
		raise ReleaseError("release notes do not declare MAJOR, MINOR, PATCH, or NONE impact")
	for label in ("Changes", "Required adaptation"):
		if re.search(rf"(?m)^{re.escape(label)}:\s*\S+", notes) is None:
			raise ReleaseError(f"release notes do not declare {label.casefold()}")
	headings = re.findall(r"(?m)^##\s+(.+?)\s*$", notes)
	if tuple(headings) != REQUIRED_NOTE_HEADINGS:
		raise ReleaseError(
			"release notes headings must be exactly " + ", ".join(REQUIRED_NOTE_HEADINGS)
		)
	return ReleaseIntent(version, impact_match.group(1), notes)


def _literal_version(path: Path, label: str) -> str:
	try:
		tree = ast.parse(_read_text(path, label), filename=str(path))
	except SyntaxError as error:
		raise ReleaseError(f"{label} is unreadable") from error
	for node in ast.walk(tree):
		if isinstance(node, ast.Assign) and any(
			isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
		):
			if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
				return node.value.value
			break
	raise ReleaseError(f"{label} does not declare a literal __version__")


def _toml(path: Path, label: str) -> dict[str, object]:
	try:
		return _object(tomllib.loads(path.read_text(encoding="utf-8")), label)
	except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
		raise ReleaseError(f"{label} is unreadable") from error


def validate_version_projections(root: Path | str) -> VersionProjection:
	"""Require package, runtime, manifest, template, and workflow projections to agree."""
	actual_root = Path(root).resolve()
	version = load_version_authority(actual_root)
	project = _toml(actual_root / "pyproject.toml", "pyproject.toml").get("project")
	project_table = _object(project, "pyproject.toml project")
	if project_table.get("version") != version:
		raise ReleaseError("native package version does not match the version authority")
	if _literal_version(actual_root / "quality_gate" / "__init__.py", "runtime version") != version:
		raise ReleaseError("runtime version does not match the version authority")
	manifest = _toml(actual_root / "quality-gate.toml", "quality-gate.toml")
	quality = _object(manifest.get("quality"), "quality-gate.toml quality")
	if quality.get("policy_release") != f"v{version}":
		raise ReleaseError("policy manifest version does not match the version authority")
	template = _toml(actual_root / "templates" / "quality-gate.toml", "template manifest")
	template_release = template.get("policy_release")
	if template_release is None:
		template_release = _object(template.get("quality"), "template quality").get("policy_release")
	if template_release != f"v{version}":
		raise ReleaseError("template policy version does not match the version authority")
	workflow = _read_text(
		actual_root / ".github" / "workflows" / "quality.yml", "reusable workflow"
	)
	if "QUALITY_GATE_RELEASE" not in workflow or "quality-gate sync" not in workflow:
		raise ReleaseError("reusable workflow does not project the policy release dynamically")
	from quality_gate.release_contract import supports_unified_inventory

	if not supports_unified_inventory(f"v{version}"):
		raise ReleaseError("distribution inventory does not support the authoritative version")
	return VersionProjection(version, version, f"v{version}")


def _release_section(body: str) -> str:
	match = re.search(r"(?ms)^##\s+Release\s*$([\s\S]*?)(?=^##\s+|\Z)", body)
	if match is None:
		raise ReleaseError("merged PR has no Release section")
	return match.group(1)


def validate_owner_merge(
	pull_requests: Iterable[Mapping[str, object]],
	intent: ReleaseIntent | object,
	*,
	source_sha: str,
	owner: str,
	default_branch: str,
) -> Mapping[str, object]:
	"""Require an owner-merged PR that declares this exact release source."""
	version = getattr(intent, "version", "")
	impact = getattr(intent, "impact", "")
	if not isinstance(version, str) or not isinstance(impact, str):
		raise ReleaseError("release intent is invalid")
	for pull_request in pull_requests:
		base = _object(pull_request.get("base"), "pull request base")
		if base.get("ref") != default_branch:
			continue
		if pull_request.get("merge_commit_sha") != source_sha:
			continue
		if not pull_request.get("merged_at"):
			raise ReleaseError("source PR is not merged")
		merged_by = _object(pull_request.get("merged_by"), "pull request merger")
		if str(merged_by.get("login", "")).casefold() != owner.casefold():
			raise ReleaseError("source PR was not merged by the repository owner")
		section = _release_section(str(pull_request.get("body") or ""))
		if impact == "NONE":
			lines = [line.strip() for line in section.splitlines() if line.strip()]
			if len(lines) != 1 or re.fullmatch(r"-\s+No release:\s+\S.*", lines[0]) is None:
				raise ReleaseError("merged PR does not declare the no-release decision")
			return pull_request
		version_line = re.search(
			rf"(?m)^-\s+Version:\s+v?\d+\.\d+\.\d+\s+->\s+v?{re.escape(version)}\s+\(({re.escape(impact)});[^\n]+\)\.?$",
			section,
		)
		if version_line is None:
			raise ReleaseError("merged PR does not declare the authoritative version and impact")
		changes_match = re.search(r"(?m)^-\s+Changes:\s+(\S.*)$", section)
		if changes_match is None:
			raise ReleaseError("merged PR does not summarize user-visible changes")
		notes_adaptation = re.search(r"(?m)^Required adaptation:\s*(\S.*)$", str(getattr(intent, "notes", "")))
		if notes_adaptation is not None and notes_adaptation.group(1).casefold() not in changes_match.group(1).casefold():
			raise ReleaseError("merged PR does not state required adaptation in its changes summary")
		if re.search(
			rf"(?m)^-\s+{re.escape(PUBLICATION_DECLARATION)}$",
			section,
		) is None:
			raise ReleaseError("merged PR does not declare owner-merge publication authorization")
		return pull_request
	raise ReleaseError("no owner-merged PR authorizes the exact source commit")


def _latest_stable_tag(releases: Iterable[Mapping[str, object]]) -> str:
	stable: list[str] = []
	for release in releases:
		tag = release.get("tag_name")
		if not isinstance(tag, str) or TAG_VERSION.fullmatch(tag) is None:
			continue
		if release.get("draft") is True or release.get("prerelease") is True:
			continue
		stable.append(tag)
	if not stable:
		raise ReleaseError("published release baseline is unavailable")
	return max(stable, key=_version_tuple)


def validate_latest_release(
	releases: Iterable[Mapping[str, object]], intent: ReleaseIntent | object
) -> str:
	"""Require the intent to be the next release after the latest stable publication."""
	version = getattr(intent, "version", "")
	impact = getattr(intent, "impact", "")
	latest = _latest_stable_tag(releases)
	if impact == "NONE":
		return latest
	tag = f"v{version}"
	if tag == latest:
		raise ReleaseError("conflicting existing release uses the reviewed version")
	if _next_version(latest, impact) != tag:
		raise ReleaseError(f"stale release intent: expected {_next_version(latest, impact)}")
	return latest


def _check_runs_ready(check_runs: object, required: frozenset[str]) -> None:
	runs = _object(check_runs, "check-runs response").get("check_runs")
	items = _list(runs, "check-runs")
	by_name: dict[str, list[dict[str, object]]] = {}
	for item in items:
		by_name.setdefault(str(item.get("name")), []).append(item)
	missing = required - set(by_name)
	if missing:
		raise ChecksPending(f"required checks are missing: {sorted(missing)}")
	for name in required:
		check_items = by_name[name]
		if any(item.get("status") == "completed" and item.get("conclusion") != "success" for item in check_items):
			raise ReleaseError(f"required check is not successful: {name}")
		if any(item.get("status") != "completed" for item in check_items):
			raise ChecksPending(f"required check is still running: {name}")
		if any(item.get("conclusion") != "success" for item in check_items):
			raise ReleaseError(f"required check is not successful: {name}")


def _validate_checks(api: GitHubApi, prefix: str, source_sha: str, required: frozenset[str]) -> None:
	_check_runs_ready(api.get(f"{prefix}/commits/{source_sha}/check-runs?per_page=100"), required)


def _wait_for_checks(
	api: GitHubApi,
	prefix: str,
	source_sha: str,
	required: frozenset[str],
	*,
	timeout_seconds: float,
	poll_seconds: float,
) -> None:
	deadline = time.monotonic() + timeout_seconds
	while True:
		try:
			_validate_checks(api, prefix, source_sha, required)
			return
		except ChecksPending as error:
			if time.monotonic() >= deadline:
				raise ReleaseError(f"required checks did not complete before timeout: {error}") from error
			time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))


def _artifact_map(
	artifacts: Sequence[ArtifactIdentity],
	intent: ReleaseIntent,
	source_sha: str | None = None,
	*,
	validate_content: bool = False,
) -> dict[str, ArtifactIdentity]:
	if not artifacts:
		raise ReleaseError("no verified platform artifacts were supplied")
	result: dict[str, ArtifactIdentity] = {}
	for artifact in artifacts:
		_validate_artifact_name(artifact.name)
		if (
			artifact.version != intent.version
			or (source_sha is not None and artifact.source_sha != source_sha)
			or not SHA256.fullmatch(artifact.sha256)
		):
			raise ReleaseError("artifact version or digest does not match the release intent")
		if artifact.source_sha == "" or artifact.platform not in {"linux", "windows"}:
			raise ReleaseError("artifact source or platform identity is invalid")
		if artifact.name in result:
			raise ReleaseError(f"duplicate release artifact: {artifact.name}")
		if validate_content:
			_artifact_content(artifact)
		result[artifact.name] = artifact
	if len(artifacts) != 2 or {item.platform for item in artifacts} != {"linux", "windows"}:
		raise ReleaseError("required Windows and Linux artifacts are incomplete")
	if len({item.platform for item in artifacts}) != len(artifacts):
		raise ReleaseError("release artifacts contain duplicate platforms")
	return result


def _uploaded_digest(value: Mapping[str, object], name: str) -> str:
	digest = value.get("digest")
	if not isinstance(digest, str) or not digest.startswith("sha256:"):
		raise ReleaseError(f"GitHub did not return a verified digest for {name}")
	result = digest.removeprefix("sha256:").lower()
	if SHA256.fullmatch(result) is None:
		raise ReleaseError(f"GitHub returned an invalid digest for {name}")
	return result


def _asset_map(release: Mapping[str, object]) -> dict[str, str]:
	assets = _list(release.get("assets"), "release assets")
	result: dict[str, str] = {}
	for asset in assets:
		name = asset.get("name")
		digest = asset.get("digest")
		if not isinstance(name, str) or not isinstance(digest, str) or not digest.startswith("sha256:"):
			raise ReleaseError("release asset has no verified SHA-256 digest")
		value = digest.removeprefix("sha256:").lower()
		if not SHA256.fullmatch(value) or name in result:
			raise ReleaseError("release asset identity is invalid or duplicated")
		result[name] = value
	return result


def _expected_body(intent: ReleaseIntent, source_sha: str) -> str:
	return f"{intent.notes.rstrip()}\n\nCandidate source revision: `{source_sha}`.\n"


def verify_published_release(
	release: Mapping[str, object],
	*,
	tag_sha: str,
	source_sha: str,
	intent: ReleaseIntent,
	artifacts: Sequence[ArtifactIdentity],
) -> ReleaseResult:
	"""Verify one completed immutable publication without modifying it."""
	if (
		release.get("tag_name") != intent.tag
		or release.get("draft") is True
		or release.get("prerelease") is True
		or release.get("immutable") is not True
		or tag_sha != source_sha
	):
		raise ReleaseError("conflicting publication is not immutable or does not identify the source")
	if release.get("body") != _expected_body(intent, source_sha):
		raise ReleaseError("published release notes do not identify the reviewed source")
	expected = {
		name: artifact.sha256
		for name, artifact in _artifact_map(artifacts, intent, source_sha).items()
	}
	if _asset_map(release) != expected:
		raise ReleaseError("published release asset identity does not match verified artifacts")
	return ReleaseResult("already-complete", intent.version, source_sha, tuple(artifacts))


def verify_draft_release(
	release: Mapping[str, object],
	*,
	tag_sha: str,
	source_sha: str,
	intent: ReleaseIntent,
	artifacts: Sequence[ArtifactIdentity],
) -> tuple[str, tuple[str, ...]]:
	"""Check a resumable unpublished release without replacing existing assets."""
	if (
		release.get("tag_name") != intent.tag
		or release.get("draft") is not True
		or release.get("prerelease") is True
		or tag_sha != source_sha
		or release.get("body") != _expected_body(intent, source_sha)
	):
		raise ReleaseError("conflicting unpublished release cannot be resumed")
	expected = {
		name: artifact.sha256
		for name, artifact in _artifact_map(artifacts, intent, source_sha).items()
	}
	actual = _asset_map(release)
	for name, digest in actual.items():
		if name not in expected or expected[name] != digest:
			raise ReleaseError("unpublished release contains a conflicting asset")
	missing = tuple(sorted(set(expected) - set(actual)))
	return "needs-assets", missing


class ReleaseController:
	"""Authorize, verify, and publish one exact merged source candidate."""

	def __init__(
		self,
		root: Path | str,
		api: GitHubApi,
		*,
		repository: str,
		owner: str,
		default_branch: str = "main",
		required_checks: frozenset[str] = DEFAULT_REQUIRED_CHECKS,
		check_wait_seconds: float | None = None,
		check_poll_seconds: float | None = None,
		source_sha_resolver: Callable[[Path], str] | None = None,
	) -> None:
		self.root = Path(root).resolve()
		self.api = api
		self.repository = repository
		self.owner = owner
		self.default_branch = default_branch
		self.required_checks = required_checks
		self.check_wait_seconds = (
			load_release_timeout(self.root, "check_wait_seconds")
			if check_wait_seconds is None
			else check_wait_seconds
		)
		self.check_poll_seconds = (
			load_release_timeout(self.root, "check_poll_seconds")
			if check_poll_seconds is None
			else check_poll_seconds
		)
		self.source_sha_resolver = source_sha_resolver or resolve_local_source_sha
		self.prefix = f"/repos/{repository}"

	def _get(self, path: str) -> object:
		return self.api.get(path)

	def _optional_release(self, tag: str) -> Mapping[str, object] | None:
		try:
			return _object(self._get(f"{self.prefix}/releases/tags/{tag}"), "release")
		except GitHubNotFound:
			return None

	def _require_immutable_releases(self) -> None:
		try:
			settings = _object(self._get(f"{self.prefix}/immutable-releases"), "immutable release settings")
		except GitHubNotFound as error:
			raise ReleaseError("immutable releases are not enabled for this repository") from error
		if settings.get("enabled") is not True:
			raise ReleaseError("immutable releases are not enabled for this repository")

	def _releases(self) -> list[dict[str, object]]:
		"""Read the complete bounded stable-release history used for baselining."""
		result: list[dict[str, object]] = []
		for page in range(1, MAX_RELEASE_PAGES + 1):
			query = "?per_page=100" if page == 1 else f"?per_page=100&page={page}"
			items = _list(self._get(f"{self.prefix}/releases{query}"), "releases")
			result.extend(items)
			if len(items) < 100:
				return result
		raise ReleaseError("published release history exceeds the configured page limit")

	def _tag(self, tag: str) -> str:
		current = _object(self._get(f"{self.prefix}/git/ref/tags/{tag}"), "tag reference")
		for _ in range(4):
			tag_object = _object(current.get("object"), "tag object")
			sha = tag_object.get("sha")
			kind = tag_object.get("type")
			if not isinstance(sha, str) or not isinstance(kind, str):
				raise ReleaseError(f"tag {tag} has an invalid object identity")
			if kind == "commit":
				validate_source_sha(sha)
				return sha
			if kind != "tag":
				raise ReleaseError(f"tag {tag} does not resolve to a commit")
			current = _object(self._get(f"{self.prefix}/git/tags/{sha}"), "annotated tag object")
		raise ReleaseError(f"tag {tag} has too many annotated tag indirections")

	def _require_tag_source(self, tag: str, source_sha: str) -> str:
		"""Require a release tag to resolve to the exact reviewed source."""
		tag_sha = self._tag(tag)
		if tag_sha != source_sha:
			raise ReleaseError("release tag does not identify the reviewed source")
		return tag_sha

	def _require_local_source(self, source_sha: str) -> None:
		"""Require the source files being read to come from the reviewed commit."""
		actual_sha = validate_source_sha(self.source_sha_resolver(self.root))
		if actual_sha != source_sha:
			raise ReleaseError("local source tree does not identify the reviewed source")

	def _source_pull_requests(self, source_sha: str) -> list[dict[str, object]]:
		"""Hydrate commit-associated pull requests with merge authorization details."""
		pulls = _list(
			self._get(f"{self.prefix}/commits/{source_sha}/pulls"),
			"pull requests",
		)
		hydrated: list[dict[str, object]] = []
		for pull_request in pulls:
			if pull_request.get("merged_by") is not None:
				hydrated.append(pull_request)
				continue
			number = pull_request.get("number")
			if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
				hydrated.append(pull_request)
				continue
			detail = _object(
				self._get(f"{self.prefix}/pulls/{number}"),
				"pull request",
			)
			hydrated.append(detail)
		return hydrated

	def _upload_artifact(self, release: Mapping[str, object], artifact: ArtifactIdentity) -> None:
		uploaded = self.api.upload(
			str(release.get("upload_url") or ""),
			artifact.name,
			_artifact_content(artifact),
		)
		if _uploaded_digest(uploaded, artifact.name) != artifact.sha256:
			raise ReleaseError(f"GitHub returned a digest mismatch for {artifact.name}")

	def _resume_draft(
		self,
		release: Mapping[str, object],
		*,
		tag_sha: str,
		source_sha: str,
		intent: ReleaseIntent,
		artifacts: Sequence[ArtifactIdentity],
	) -> None:
		_, missing = verify_draft_release(
			release,
			tag_sha=tag_sha,
			source_sha=source_sha,
			intent=intent,
			artifacts=artifacts,
		)
		by_name = {artifact.name: artifact for artifact in artifacts}
		for name in missing:
			self._upload_artifact(release, by_name[name])
		self.api.patch(f"{self.prefix}/releases/{release.get('id')}", {"draft": False})

	def _create_draft(
		self,
		*,
		intent: ReleaseIntent,
		source_sha: str,
		body: str,
		artifacts: Sequence[ArtifactIdentity],
	) -> None:
		self.api.post(
			f"{self.prefix}/releases",
			{
				"tag_name": intent.tag,
				"target_commitish": source_sha,
				"name": f"Quality Gate {intent.tag}",
				"body": body,
				"draft": True,
				"prerelease": False,
			},
		)
		release = self._optional_release(intent.tag)
		if release is None:
			raise ReleaseError("GitHub did not return the created draft release")
		self._require_tag_source(intent.tag, source_sha)
		for artifact in artifacts:
			self._upload_artifact(release, artifact)
		self.api.patch(f"{self.prefix}/releases/{release.get('id')}", {"draft": False})

	def _verify_completed(
		self,
		*,
		intent: ReleaseIntent,
		source_sha: str,
		artifacts: Sequence[ArtifactIdentity],
	) -> ReleaseResult:
		release = self._optional_release(intent.tag)
		if release is None:
			raise ReleaseError("published release is unreadable after publication")
		return verify_published_release(
			release,
			tag_sha=self._require_tag_source(intent.tag, source_sha),
			source_sha=source_sha,
			intent=intent,
			artifacts=artifacts,
		)

	def verify(
		self,
		source_sha: str,
		artifacts: Sequence[ArtifactIdentity] = (),
		*,
		wait_for_checks: bool = False,
		authorization_only: bool = False,
	) -> ReleaseResult:
		validate_source_sha(source_sha)
		self._require_local_source(source_sha)
		intent = load_release_intent(self.root)
		validate_version_projections(self.root)
		validate_owner_merge(
			self._source_pull_requests(source_sha),
			intent,
			source_sha=source_sha,
			owner=self.owner,
			default_branch=self.default_branch,
		)
		releases = self._releases()
		baseline_tag = _latest_stable_tag(releases)
		validate_source_sha(self._tag(baseline_tag))
		if wait_for_checks:
			_wait_for_checks(
				self.api,
				self.prefix,
				source_sha,
				self.required_checks,
				timeout_seconds=self.check_wait_seconds,
				poll_seconds=self.check_poll_seconds,
			)
		else:
			_validate_checks(self.api, self.prefix, source_sha, self.required_checks)
		if intent.impact == "NONE":
			return ReleaseResult("no-release", intent.version, source_sha)
		if authorization_only:
			return ReleaseResult("authorized", intent.version, source_sha)
		existing = self._optional_release(intent.tag)
		if existing is not None and artifacts and existing.get("draft") is not True:
			_artifact_map(artifacts, intent, source_sha, validate_content=True)
			return verify_published_release(
				existing,
				tag_sha=self._tag(intent.tag),
				source_sha=source_sha,
				intent=intent,
				artifacts=artifacts,
			)
		validate_latest_release(releases, intent)
		_artifact_map(artifacts, intent, source_sha, validate_content=True)
		return ReleaseResult("ready", intent.version, source_sha, tuple(artifacts))

	def publish(self, source_sha: str, artifacts: Sequence[ArtifactIdentity]) -> ReleaseResult:
		validate_source_sha(source_sha)
		result = self.verify(source_sha, artifacts, wait_for_checks=True)
		if result.status in {"no-release", "already-complete"}:
			return result
		intent = load_release_intent(self.root)
		if not artifacts:
			raise ReleaseError("publish requires verified Linux and Windows artifacts")
		existing = self._optional_release(intent.tag)
		if existing is not None:
			if existing.get("draft") is not True:
				return self._verify_completed(intent=intent, source_sha=source_sha, artifacts=artifacts)
		self._require_immutable_releases()
		if existing is not None:
			self._resume_draft(
				existing,
				tag_sha=self._require_tag_source(intent.tag, source_sha),
				source_sha=source_sha,
				intent=intent,
				artifacts=artifacts,
			)
		else:
			self._create_draft(
				intent=intent,
				source_sha=source_sha,
				body=_expected_body(intent, source_sha),
				artifacts=artifacts,
			)
		return self._verify_completed(intent=intent, source_sha=source_sha, artifacts=artifacts)


def _confine_artifact_path(artifact: ArtifactIdentity, root: Path) -> ArtifactIdentity:
	if artifact.path is None:
		raise ReleaseError(f"artifact path is missing for {artifact.name}")
	raw_path = Path(artifact.path)
	resolved = (raw_path if raw_path.is_absolute() else root / raw_path).resolve()
	try:
		resolved.relative_to(root.resolve())
	except ValueError as error:
		raise ReleaseError(f"artifact path escapes its manifest directory: {artifact.name}") from error
	return ArtifactIdentity(
		artifact.version,
		artifact.source_sha,
		artifact.platform,
		artifact.name,
		artifact.sha256,
		str(resolved),
	)


def _load_artifacts(path: Path) -> tuple[ArtifactIdentity, ...]:
	try:
		with path.open("rb") as stream:
			value = json.loads(read_bounded(stream, MAX_MANIFEST_BYTES, "artifact manifest").decode("utf-8"))
	except (OSError, UnicodeError, json.JSONDecodeError) as error:
		raise ReleaseError("artifact manifest is unreadable") from error
	items = _list(value, "artifact manifest")
	manifest_root = path.resolve().parent
	return tuple(_confine_artifact_path(ArtifactIdentity.from_mapping(item), manifest_root) for item in items)


def _parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description=__doc__)
	subcommands = parser.add_subparsers(dest="command", required=True)
	for name in ("verify", "publish"):
		command = subcommands.add_parser(name)
		command.add_argument("--source-sha", required=True)
		command.add_argument("--artifact-manifest", type=Path)
		command.add_argument("--wait-for-checks", action="store_true")
		command.add_argument("--authorization-only", action="store_true")
	return parser


def main(arguments: Sequence[str] | None = None) -> int:
	options = _parser().parse_args(arguments)
	try:
		repository = os.environ["GITHUB_REPOSITORY"]
		owner = os.environ.get("GITHUB_REPOSITORY_OWNER", repository.split("/", 1)[0])
		root = Path(__file__).resolve().parents[1]
		api = HttpGitHubApi(
			repository,
			os.environ.get("GITHUB_TOKEN", ""),
			api_url=os.environ.get("GITHUB_API_URL"),
			timeout_seconds=load_release_timeout(root, "github_api_seconds"),
		)
		controller = ReleaseController(
			root,
			api,
			repository=repository,
			owner=owner,
			default_branch=os.environ.get("GITHUB_DEFAULT_BRANCH", "main"),
		)
		artifacts = _load_artifacts(options.artifact_manifest) if options.artifact_manifest is not None else ()
		result = (
			controller.publish(options.source_sha, artifacts)
			if options.command == "publish"
			else controller.verify(
				options.source_sha,
				artifacts,
				wait_for_checks=options.wait_for_checks,
				authorization_only=options.authorization_only,
			)
		)
	except (KeyError, OSError, ReleaseError) as error:
		print(f"release: unchecked - {error}")
		return 2
	print(json.dumps({"status": result.status, "version": result.version, "source_sha": result.source_sha}))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
