"""Working-tree Ruff batch orchestration."""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, cast

from . import __version__
from .contracts import Manifest, ValidationError, parse_manifest
from .edit_inputs import (
	Configuration,
	ImportContext,
	SourceFile,
	read_bounded,
	sources,
)
from .edit_invocation import Invocation as Invocation
from .edit_policy import sha256
from .edit_protocol import (
	MAX_FILE_BYTES,
	MAX_FINDINGS,
	EditError,
	encode,
)
from .edit_ruff_support import Selection, _assert_inputs_current, _component, _publish
from .verification_evidence import (
	repository_digest,
)


def execute(request: dict[str, Any], deadline: float, workspace: Path) -> dict[str, Any]:
	"""Execute one isolated writer or read-only validation batch; fail closed when incomplete."""
	invocation = Invocation(request, deadline, workspace)
	try:
		return _execute(invocation, workspace)
	except EditError as error:
		error.stage = invocation.stage
		raise


def _reuse_identity(
	invocation: Invocation,
	files: list[SourceFile],
	records: list[dict[str, Any]],
	selections: list[Selection],
) -> dict[str, Any] | None:
	if (
		invocation.imports is None
		or invocation.imports.identity is None
		or not selections
		or invocation.engine_identity is None
	):
		return None
	repository = repository_digest(invocation.root)
	if repository is None:
		return None
	file_by_path = {file.path: file for file in files}
	phase = (
		"validate" if invocation.read_only else "write-only" if invocation.write_only else "legacy"
	)
	return {
		"kind": "edit.batch.v1",
		"engine": invocation.engine_identity.as_dict(),
		"repository": repository,
		"scope": {
			"check": "edit.batch.v1",
			"phase": phase,
			"files": [
				{
					key: record[key]
					for key in ("path", "sha256", "coverage", "component", "selection")
				}
				| {"mode": file_by_path[record["path"]].mode}
				for record in records
				if record["path"] in file_by_path
			],
		},
		"selections": [selection.identity for selection in selections],
		"inputs": invocation.configuration.as_list(),
		"imports": invocation.imports.identity,
		"artifacts": sorted((str(path), digest) for path, digest in invocation.artifacts.items()),
	}


def _reused_result(
	invocation: Invocation,
	files: list[SourceFile],
	records: list[dict[str, Any]],
	selections: list[Selection],
	workspace: Path,
) -> dict[str, Any] | None:
	# Validate always reruns import-dependent checks over its current complete scope.
	if invocation.read_only or not invocation.reuse_enabled:
		return None
	identity = _reuse_identity(invocation, files, records, selections)
	cached = invocation.result_reuse.lookup(identity, expected_engine=invocation.engine_identity)
	if cached is None or not _valid_batch_result(cached, records):
		return None
	cached["root"] = str(invocation.root)
	for record in cached["files"]:
		record.update(
			input_sha256=record["sha256"],
			changed=False,
			evidence="not_applicable" if record["status"] == "not_applicable" else "reused",
		)
	cached.update(process_count=invocation.process_count, writes_may_have_occurred=False)
	if invocation.read_only:
		_assert_inputs_current(invocation, files)
	else:
		_publish(invocation, files, workspace, cached["files"])
	return cached


def _valid_batch_result(result: object, expected_records: list[dict[str, Any]]) -> bool:
	if not isinstance(result, dict) or set(result) != {
		"schema",
		"engine_version",
		"root",
		"status",
		"reason",
		"files",
		"selections",
		"inputs",
		"process_count",
		"writes_may_have_occurred",
	}:
		return False
	if (
		result.get("schema") != 1
		or not isinstance(result.get("engine_version"), str)
		or not isinstance(result.get("root"), str)
		or result.get("status") not in {"passed", "failed", "not_applicable"}
		or result.get("reason") is not None
		or not isinstance(result.get("files"), list)
		or len(result["files"]) != len(expected_records)
		or not isinstance(result.get("selections"), list)
		or not isinstance(result.get("inputs"), list)
		or type(result.get("process_count")) is not int
		or result["process_count"] < 1
		or result.get("writes_may_have_occurred") is not False
	):
		return False
	return all(
		_valid_batch_record(actual, expected)
		for actual, expected in zip(result["files"], expected_records, strict=True)
	) and result["status"] == _result_status(result["files"])


def _valid_batch_record(actual: object, expected: dict[str, Any]) -> bool:
	if not isinstance(actual, dict) or set(actual) != {
		"path",
		"status",
		"coverage",
		"component",
		"input_sha256",
		"sha256",
		"changed",
		"evidence",
		"selection",
		"findings",
		"format",
	}:
		return False
	if (
		actual["path"] != expected["path"]
		or actual["coverage"] != expected["coverage"]
		or actual["component"] != expected["component"]
		or actual["selection"] != expected["selection"]
		or actual["input_sha256"] != expected["input_sha256"]
		or actual["sha256"] != actual["input_sha256"]
		or actual["changed"] is not False
		or actual["evidence"] not in {"executed", "reused", "not_applicable"}
		or actual["status"] not in {"passed", "failed", "not_applicable"}
		or actual["format"] not in {"passed", "failed", "not_applicable"}
		or not isinstance(actual["findings"], list)
		or len(actual["findings"]) > MAX_FINDINGS
	):
		return False
	for finding in actual["findings"]:
		if (
			not isinstance(finding, dict)
			or set(finding) != {"check_id", "rule", "line", "column", "action"}
			or finding.get("check_id") != "ruff"
			or not isinstance(finding.get("rule"), str)
			or not re.fullmatch(r"(?:[A-Z]+[0-9]+|invalid-syntax)", finding["rule"])
			or type(finding.get("line")) is not int
			or type(finding.get("column")) is not int
			or min(finding["line"], finding["column"]) < 1
			or finding.get("action") != f"Resolve Ruff {finding['rule']} in the final file."
		):
			return False
	if actual["status"] == "not_applicable":
		return not actual["findings"] and actual["format"] == "not_applicable"
	if actual["status"] == "passed":
		return not actual["findings"] and actual["format"] != "failed"
	return bool(actual["findings"]) or actual["format"] == "failed"


def _working_manifest(configuration: Configuration) -> Manifest | None:
	manifest_content = configuration.capture("quality-gate.toml")
	try:
		return (
			parse_manifest(manifest_content.decode("utf-8"))
			if manifest_content is not None
			else None
		)
	except (ValidationError, UnicodeError) as error:
		raise EditError("manifest") from error


def _source_batches(
	invocation: Invocation, files: list[SourceFile], manifest: Manifest | None
) -> tuple[list[dict[str, Any]], list[Selection], dict[int, list[SourceFile]]]:
	"""Build result records and the exact policy batches for these source files."""
	batches: dict[int, list[SourceFile]] = {}
	selections: list[Selection] = []
	records: list[dict[str, Any]] = []
	for file in files:
		component = _component(manifest, file.path)
		record: dict[str, Any] = {
			"path": file.path,
			"status": "not_applicable",
			"coverage": "managed" if component else "baseline",
			"component": component.name if component else None,
			"input_sha256": file.sha256,
			"sha256": file.sha256,
			"changed": False,
			"evidence": "not_applicable",
			"selection": None,
			"findings": [],
			"format": "not_applicable",
		}
		if Path(file.path).suffix in {".py", ".pyi"}:
			selection = invocation.select(manifest, component)
			if selection not in selections:
				selections.append(selection)
			index = selections.index(selection)
			record["selection"] = index
			batches.setdefault(index, []).append(file)
		records.append(record)
	return records, selections, batches


def _apply_batches(
	invocation: Invocation,
	workspace: Path,
	files: list[SourceFile],
	selections: list[Selection],
	batches: dict[int, list[SourceFile]],
	records: list[dict[str, Any]],
) -> None:
	for index, batch in batches.items():
		results = invocation.check_batch(selections[index], batch, workspace)
		for record in records:
			if record["path"] not in results:
				continue
			record.update(results[record["path"]])
			if record["status"] == "not_applicable":
				continue
			record["evidence"] = results[record["path"]].get("evidence", "executed")
			staged = workspace / record["path"]
			record["sha256"] = sha256(read_bounded(staged, MAX_FILE_BYTES))
			record["changed"] = record["sha256"] != record["input_sha256"]


def _result_status(records: list[dict[str, Any]]) -> str:
	statuses = {record["status"] for record in records}
	if "failed" in statuses:
		return "failed"
	if "passed" in statuses:
		return "passed"
	return "not_applicable"


def _execute(invocation: Invocation, workspace: Path) -> dict[str, Any]:
	request = invocation.request
	files = sources(invocation.root, request["files"])
	imports = ImportContext(invocation.root, files)
	invocation.imports = imports
	configuration = invocation.configuration
	manifest = _working_manifest(configuration)
	configuration.for_sources(files)
	records, selections, batches = _source_batches(invocation, files, manifest)
	_assert_inputs_current(invocation, files)
	cached = _reused_result(invocation, files, records, selections, workspace)
	if cached is not None:
		return cached
	imports.materialize_packages(workspace)
	_apply_batches(invocation, workspace, files, selections, batches, records)
	if sum(len(record["findings"]) for record in records) > MAX_FINDINGS:
		raise EditError("limits")
	result = {
		"schema": 1,
		"engine_version": __version__,
		"root": str(invocation.root),
		"status": _result_status(records),
		"reason": None,
		"files": records,
		"selections": [selection.identity for selection in selections],
		"inputs": configuration.as_list(),
		"process_count": invocation.process_count,
		"writes_may_have_occurred": any(record["changed"] for record in records),
	}
	if invocation.read_only:
		result["phase"] = "validate"
	encode(result)
	if invocation.read_only:
		_assert_inputs_current(invocation, files)
	else:
		_publish(invocation, files, workspace, records)
	phase = "validate" if invocation.read_only else "write" if invocation.write_only else "legacy"
	operation = {
		"validate": "edit-check.validate",
		"write": "edit-check.write",
		"legacy": "edit-check.legacy",
	}[phase]
	if invocation.reuse_enabled and not invocation.read_only:
		stored = copy.deepcopy(result)
		stored["writes_may_have_occurred"] = False
		for record in cast(list[dict[str, Any]], stored["files"]):
			record["input_sha256"] = record["sha256"]
			record["changed"] = False
		invocation.result_reuse.remember(
			_reuse_identity(invocation, files, records, selections),
			stored,
			producer=invocation.evidence_producer(operation),
		)
	return result
