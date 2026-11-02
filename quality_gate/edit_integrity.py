"""Read-only touched-file integrity checking through the released Gate semantics."""

from __future__ import annotations

import json
import stat
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import __version__
from .contracts import CheckResult, Manifest, Status, ValidationError, parse_manifest
from .distribution import PolicyCache
from .edit_inputs import Configuration, SourceFile, read_bounded, regular_path, sources
from .edit_policy import select_release, sha256
from .edit_protocol import MAX_FILE_BYTES, MAX_FINDINGS, RECOVERY, EditError, encode
from .integrity import git_conflict_marker_results

CHECK_ID = "repository.git.conflict_markers"
ACTION = "Resolve the merge conflict markers in this touched file."
INTEGRITY_RECOVERY = RECOVERY | {
	"tool_output": "Restore the selected Gate release and retry; raw output is omitted.",
	"io": "Restore readable regular touched files and retry with fresh identities.",
}


def integrity_unchecked(reason: str) -> dict[str, Any]:
	"""Return a source-free incomplete result for the public integrity boundary."""
	return {
		"schema": 1,
		"engine_version": __version__,
		"check_id": CHECK_ID,
		"status": "unchecked",
		"reason": reason,
		"action": INTEGRITY_RECOVERY[reason],
		"files": [],
		"selections": [],
		"inputs": [],
		"writes_may_have_occurred": False,
	}


def _manifest(configuration: Configuration) -> Manifest | None:
	content = configuration.capture("quality-gate.toml")
	if content is None:
		return None
	try:
		return parse_manifest(content.decode("utf-8"))
	except (ValidationError, UnicodeError) as error:
		raise EditError("manifest") from error


def _selection(cache: PolicyCache, version: str, manifest: Manifest | None) -> dict[str, Any]:
	root, _release = select_release(cache, version)
	release_digest = sha256((root / "release.toml").read_bytes())
	waivers = [asdict(waiver) for waiver in manifest.waivers] if manifest is not None else []
	return {
		"policy_release": version,
		"release_sha256": release_digest,
		"waiver_sha256": sha256(json.dumps(waivers, sort_keys=True, default=str).encode()),
	}


def _record(
	source: SourceFile,
	manifest: Manifest | None,
	status: Status,
	findings: list[dict[str, Any]],
) -> dict[str, Any]:
	if status is Status.UNCHECKED:
		raise EditError("limits")
	return {
		"path": source.path,
		"status": status.value,
		"coverage": "managed" if manifest is not None else "baseline",
		"input_sha256": source.sha256,
		"sha256": source.sha256,
		"changed": False,
		"selection": 0,
		"findings": findings,
	}


def _root(request: dict[str, Any]) -> Path:
	root = Path(request["root"])
	if (
		not root.is_absolute()
		or root.is_symlink()
		or not root.is_dir()
		or getattr(root.lstat(), "st_file_attributes", 0)
		& getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
	):
		raise EditError("unsafe_path")
	return root.resolve(strict=True)


def _policy_version(request: dict[str, Any], manifest: Manifest | None) -> str:
	baseline = request.get("baseline")
	version = (
		manifest.policy_release
		if manifest is not None
		else (baseline.get("policy_release") if isinstance(baseline, dict) else None)
	)
	if not isinstance(version, str):
		raise EditError("invalid_request")
	return version


def _findings(
	files: list[SourceFile], manifest: Manifest | None
) -> tuple[CheckResult, dict[str, list[dict[str, Any]]], dict[str, Status]]:
	gate_result, file_results = git_conflict_marker_results(
		manifest,
		((source.path, source.content) for source in files),
		max_findings=MAX_FINDINGS,
	)
	if gate_result.status is Status.UNCHECKED:
		raise EditError("limits")
	if len(gate_result.findings) > MAX_FINDINGS:
		raise EditError("limits")
	if len(file_results) != len(files):
		raise EditError("tool_output")
	by_path: dict[str, list[dict[str, Any]]] = {source.path: [] for source in files}
	for finding in gate_result.findings:
		if finding.path not in by_path or type(finding.line) is not int or finding.line < 1:
			raise EditError("tool_output")
		by_path[finding.path].append(
			{
				"check_id": CHECK_ID,
				"path": finding.path,
				"line": finding.line,
				"status": "failed",
				"action": ACTION,
			}
		)
	statuses = {
		source.path: result.status for source, result in zip(files, file_results, strict=True)
	}
	return gate_result, by_path, statuses


def _assert_current(
	configuration: Configuration,
	cache: PolicyCache,
	version: str,
	manifest: Manifest | None,
	selection: dict[str, Any],
	root: Path,
	files: list[SourceFile],
) -> None:
	configuration.assert_current()
	if _selection(cache, version, manifest) != selection:
		raise EditError("stale_input")
	for source in files:
		if sha256(read_bounded(regular_path(root, source.path), MAX_FILE_BYTES)) != source.sha256:
			raise EditError("stale_input")


def execute(request: dict[str, Any], deadline: float, workspace: Path) -> dict[str, Any]:
	"""Check complete explicit inputs and publish only if all identities stay current."""
	root = _root(request)
	configuration = Configuration(root)
	manifest = _manifest(configuration)
	files = sources(root, request["files"])
	result: dict[str, Any] = {
		"schema": 1,
		"engine_version": __version__,
		"root": str(root),
		"check_id": CHECK_ID,
		"status": "not_applicable",
		"reason": None,
		"findings": [],
		"files": [],
		"selections": [],
		"inputs": configuration.as_list(),
		"process_count": 2,
		"writes_may_have_occurred": False,
	}
	if not files:
		configuration.assert_current()
		result["inputs"] = configuration.as_list()
		return result
	if time.monotonic() >= deadline:
		raise EditError("deadline")
	version = _policy_version(request, manifest)
	cache = PolicyCache(request.get("cache_dir"))
	selection = _selection(cache, version, manifest)
	gate_result, by_path, statuses = _findings(files, manifest)
	records = [
		_record(source, manifest, statuses[source.path], by_path[source.path]) for source in files
	]
	result.update(
		status=gate_result.status.value,
		findings=[item for record in records for item in record["findings"]],
		files=records,
		selections=[selection],
		inputs=configuration.as_list(),
	)
	if (result["status"] == "failed") != bool(result["findings"]):
		raise EditError("tool_output")
	encode(result)
	_assert_current(configuration, cache, version, manifest, selection, root, files)
	if time.monotonic() >= deadline:
		raise EditError("deadline")
	return result
