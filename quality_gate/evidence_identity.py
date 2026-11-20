"""Identity and validation for file-local Ruff evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_FILE_MODE = 0o777
_MAX_FINDINGS = 4096
# Reviewed policy bytes in LF and CRLF form, paired with the reviewed Ruff release.
_REVIEWED_RUFF_POLICIES = frozenset(
	{
		"f49ac3e4baa6fdfdd66bb13f07d3ccaebe9174cd23af269fd7b55db30b0809af",
		"adbf2fd724b5805ac1356493d73136b6be0cfbfac27be3b9aa3bc3ee4b2e48e8",
	}
)


@dataclass(frozen=True, slots=True)
class EngineIdentity:
	"""The exact provider engine bytes and whether they came from source or a wheel."""

	form: str
	version: str
	digest: str

	def __post_init__(self) -> None:
		if not isinstance(self.form, str) or self.form not in {"source", "wheel"}:
			raise ValueError("engine form must be source or wheel")
		if not isinstance(self.version, str) or not self.version.strip():
			raise ValueError("engine version must be non-empty")
		if not isinstance(self.digest, str) or not _SHA256.fullmatch(self.digest):
			raise ValueError("engine digest must be SHA-256")

	def as_dict(self) -> dict[str, str]:
		return {"form": self.form, "version": self.version, "digest": self.digest}


RUFF_LOCAL_COMMON = ("--no-cache", "--force-exclude")
RUFF_LOCAL_LINT = ("--no-fix", "--extend-ignore", "I", "--output-format", "json")
RUFF_LOCAL_FORMAT = ("--check", "--output-format", "json")


def ruff_file_identity(
	*,
	engine: EngineIdentity,
	repository: str,
	path: str,
	mode: int | None,
	content_sha256: str,
	tool_version: str,
	tool_sha256: str,
	policy_release: str,
	release_sha256: str,
	policy_sha256: str,
	python_version: str,
	python_sha256: str,
	runtime_fingerprint: str,
	platform: str,
	configuration: list[dict[str, str | None]],
) -> dict[str, Any] | None:
	"""Build the shared exact identity for cached Ruff lint and format results."""
	if (
		not isinstance(engine, EngineIdentity)
		or tool_version != "0.16.4"
		or policy_sha256 not in _REVIEWED_RUFF_POLICIES
		or not all(
			isinstance(value, str) and value.strip()
			for value in (
				repository,
				path,
				tool_version,
				policy_release,
				python_version,
				runtime_fingerprint,
				platform,
			)
		)
		or type(mode) is not int
		or not 0 <= mode <= _MAX_FILE_MODE
		or not all(
			isinstance(value, str) and _SHA256.fullmatch(value)
			for value in (
				content_sha256,
				tool_sha256,
				release_sha256,
				policy_sha256,
				python_sha256,
				runtime_fingerprint,
			)
		)
		or not _SHA256.fullmatch(repository)
		or not _repository_relative(path)
		or not isinstance(configuration, list)
	):
		return None
	configurations: list[dict[str, str | None]] = []
	for item in configuration:
		if (
			not isinstance(item, dict)
			or set(item) != {"path", "sha256"}
			or not isinstance(item["path"], str)
			or not _repository_relative(item["path"])
			or item["sha256"] is not None
			and (not isinstance(item["sha256"], str) or not _SHA256.fullmatch(item["sha256"]))
		):
			return None
		configurations.append({"path": item["path"], "sha256": item["sha256"]})
	if len({item["path"] for item in configurations}) != len(configurations):
		return None
	scope = {
		"check": "ruff.file-local.v1",
		"files": [{"path": path, "mode": mode, "sha256": content_sha256}],
	}
	return {
		"kind": "ruff.file-local.v1",
		"engine": engine.as_dict(),
		"repository": repository,
		"scope": scope,
		"tool": {"name": "ruff", "version": tool_version, "sha256": tool_sha256},
		"policy": {
			"release": policy_release,
			"release_sha256": release_sha256,
			"sha256": policy_sha256,
		},
		"runtime": {
			"python_version": python_version,
			"python_sha256": python_sha256,
			"fingerprint": runtime_fingerprint,
		},
		"platform": platform,
		"configuration": sorted(configurations, key=lambda item: item["path"]),
		"options": {
			"lint": ["check", "--config", "<pinned-policy>", *RUFF_LOCAL_COMMON, *RUFF_LOCAL_LINT],
			"format": [
				"format",
				"--config",
				"<pinned-policy>",
				*RUFF_LOCAL_COMMON,
				*RUFF_LOCAL_FORMAT,
			],
		},
	}


def _repository_relative(value: str) -> bool:
	path = Path(value.replace("\\", "/"))
	return (
		bool(value)
		and not path.is_absolute()
		and not path.drive
		and all(part not in {"", ".", ".."} for part in path.parts)
	)


def valid_ruff_file_outcome(value: object, *, path: str) -> bool:
	"""Reject partial, malformed, or non-local diagnostic payloads before reuse."""
	if (
		not isinstance(value, dict)
		or set(value) != {"status", "lint_status", "format_status", "findings"}
		or value.get("status") not in {"passed", "failed"}
		or value.get("lint_status") not in {"passed", "failed"}
		or value.get("format_status") not in {"passed", "failed"}
		or not isinstance(value.get("findings"), list)
		or len(value["findings"]) > _MAX_FINDINGS
	):
		return False
	for finding in value["findings"]:
		if (
			not isinstance(finding, dict)
			or set(finding) != {"path", "rule", "line", "column"}
			or finding.get("path") != path
			or not isinstance(finding.get("rule"), str)
			or not re.fullmatch(r"(?:[A-Z]+[0-9]+|invalid-syntax)", finding["rule"])
			or type(finding.get("line")) is not int
			or type(finding.get("column")) is not int
			or min(finding["line"], finding["column"]) < 1
		):
			return False
	expected_lint = "failed" if value["findings"] else "passed"
	failed = expected_lint == "failed" or value["format_status"] == "failed"
	return value["lint_status"] == expected_lint and value["status"] == (
		"failed" if failed else "passed"
	)


def valid_evidence_outcome(identity: dict[str, Any], outcome: object) -> bool:
	"""Require complete per-file results; edit batch validation remains with its consumer."""
	if identity.get("kind") != "ruff.file-local.v1":
		return isinstance(outcome, dict) and outcome.get("status") in {"passed", "failed"}
	scope = identity.get("scope")
	files = scope.get("files") if isinstance(scope, dict) else None
	if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict):
		return False
	path = files[0].get("path")
	return isinstance(path, str) and valid_ruff_file_outcome(outcome, path=path)
