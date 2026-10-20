"""Bounded read-only working-tree secret checking."""

from __future__ import annotations

import json
import os
import re
import time
import tomllib
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import __version__
from .contracts import Manifest, ValidationError, parse_manifest
from .distribution import PolicyCache
from .edit_inputs import Configuration, SourceFile, read_bounded, regular_path, sources
from .edit_policy import select_release, sha256
from .edit_process import run_process
from .edit_protocol import MAX_FILE_BYTES, MAX_FINDINGS, RECOVERY, EditError, encode
from .secrets import parse_secret_report, scanner_command

CHECK_ID = "secrets.candidate"
SECRET_RECOVERY = RECOVERY | {
	"release": "Restore the exact immutable policy release through quality-gate sync.",
	"tool": "Restore the selected release's verified Gitleaks executable.",
	"configuration": "Restore bounded readable local Gitleaks configuration and retry.",
	"coverage": "Use supported UTF-8 text without NUL bytes; verify other content separately.",
	"tool_output": "Restore the selected Gitleaks tool and retry; raw output is omitted.",
}


def secret_unchecked(reason: str) -> dict[str, Any]:
	"""Return source-free read-only failure semantics."""
	return {
		"schema": 1,
		"engine_version": __version__,
		"check_id": CHECK_ID,
		"status": "unchecked",
		"reason": reason,
		"action": SECRET_RECOVERY[reason],
		"files": [],
		"writes_may_have_occurred": False,
	}


ACTION = "Rotate or revoke the credential and remove it from the touched file."


def _manifest(configuration: Configuration) -> Manifest | None:
	content = configuration.capture("quality-gate.toml")
	if content is None:
		return None
	try:
		return parse_manifest(content.decode("utf-8"))
	except (ValidationError, UnicodeError) as error:
		raise EditError("manifest") from error


def _materialize(
	configuration: Configuration, files: list[SourceFile], candidate: Path
) -> set[str]:
	for source in files:
		try:
			source.content.decode("utf-8-sig")
		except UnicodeError as error:
			raise EditError("coverage") from error
		if b"\x00" in source.content:
			raise EditError("coverage")
		path = candidate / source.path
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_bytes(source.content)
	paths = {source.path for source in files}
	for name in (".gitleaks.toml", ".gitleaksignore"):
		content = configuration.capture(name)
		if content is None:
			continue
		if name == ".gitleaks.toml":
			try:
				parsed = tomllib.loads(content.decode("utf-8"))
				# Gitleaks' Viper keys are case-insensitive. External extension can
				# read outside the captured tree or use the network, so fail closed.
				extensions = [value for key, value in parsed.items() if key.casefold() == "extend"]
				if any(
					not isinstance(extension, dict)
					or any(
						key.casefold() in {"path", "url"} and value
						for key, value in extension.items()
					)
					for extension in extensions
				):
					raise EditError("configuration")
			except (ValueError, UnicodeError) as error:
				raise EditError("configuration") from error
		(candidate / name).write_bytes(content)
		paths.add(name)
	return paths


def _selection(
	cache: PolicyCache, version: str, manifest: Manifest | None, configuration: Configuration
) -> tuple[Path, dict[str, Any]]:
	root, release = select_release(cache, version)
	tool = next((tool for tool in release.tools if tool.name == "gitleaks"), None)
	if tool is None or tool.version != "8.30.1" or not os.access(root / tool.path, os.X_OK):
		raise EditError("tool")
	config_digest = configuration.identities.get(".gitleaks.toml")
	waivers = [asdict(waiver) for waiver in manifest.waivers] if manifest is not None else []
	return root / tool.path, {
		"policy_release": version,
		"release_sha256": sha256((root / "release.toml").read_bytes()),
		"policy_source": "project" if config_digest is not None else "gitleaks-built-in",
		"policy_sha256": config_digest if config_digest is not None else tool.sha256,
		"scanner_version": tool.version,
		"scanner_sha256": tool.sha256,
		"waiver_sha256": sha256(json.dumps(waivers, sort_keys=True, default=str).encode()),
	}


def _records(
	files: list[SourceFile],
	report: bytes,
	candidate: Path,
	materialized: set[str],
	manifest: Manifest | None,
	*,
	exit_code: int,
) -> list[dict[str, Any]]:
	try:
		matches = parse_secret_report(report, candidate)
	except ValueError as error:
		raise EditError("tool_output") from error
	if len(matches) > MAX_FINDINGS:
		raise EditError("limits")
	by_path: dict[str, list[dict[str, Any]]] = {source.path: [] for source in files}
	for match in matches:
		path, raw_line = match.finding.path.rsplit(":", 1)
		line = int(raw_line)
		if path not in materialized:
			raise EditError("tool_output")
		if path not in by_path:
			continue  # Configuration is materialized, but is not implicitly touched.
		waived = (
			manifest is not None
			and manifest.resolve_waiver(CHECK_ID, match.finding.path, match.fingerprint) is not None
		)
		by_path[path].append(
			{
				"check_id": CHECK_ID,
				"path": path,
				"target": match.finding.path,
				"line": line,
				"fingerprint": match.fingerprint,
				"status": "waived" if waived else "failed",
				"action": ACTION,
			}
		)
	records = []
	for source in files:
		findings = sorted(
			by_path[source.path], key=lambda item: (item["line"], item["fingerprint"])
		)
		if any(item["line"] > source.content.count(b"\n") + 1 for item in findings):
			raise EditError("tool_output")
		records.append(
			{
				"path": source.path,
				"status": "failed"
				if any(item["status"] == "failed" for item in findings)
				else "waived"
				if findings
				else "passed"
				if source.content
				else "not_applicable",
				"coverage": "managed" if manifest is not None else "baseline",
				"input_sha256": source.sha256,
				"sha256": source.sha256,
				"changed": False,
				"selection": 0,
				"findings": findings,
			}
		)
	# Config-only matches are legitimate but outside the requested path projection.
	if (exit_code == 1) != bool(matches):
		raise EditError("tool_output")
	return records


def _coverage(diagnostics: bytes) -> None:
	"""Interpret only pinned scanner lifecycle markers; never expose its text."""
	complete = False
	for line in diagnostics.splitlines():
		entry = re.fullmatch(rb"\S+ (TRC|DBG|INF|WRN|ERR|FTL|PNC) (.*)", line)
		if entry is None:
			raise EditError("tool_output")
		level, message = entry.groups()
		if message.startswith(
			(b"skipping binary file", b"skipping archive:", b"skipping unknown archive type")
		):
			raise EditError("coverage")
		if level in {b"ERR", b"FTL", b"PNC"}:
			raise EditError("tool_output")
		if level == b"WRN" and not re.fullmatch(rb"leaks found: [0-9]+\s*", message):
			raise EditError("tool_output")
		complete |= level == b"INF" and re.match(rb"scanned ~[0-9]+ bytes", message) is not None
	if not complete:
		raise EditError("tool_output")


def execute(request: dict[str, Any], deadline: float, workspace: Path) -> dict[str, Any]:
	"""Execute a complete bounded batch or fail without claiming partial coverage."""
	root = Path(request["root"]).resolve(strict=True)
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
		"files": [],
		"selections": [],
		"inputs": [],
		"process_count": 2,
		"writes_may_have_occurred": False,
	}
	if not files:
		configuration.capture(".gitleaks.toml")
		configuration.capture(".gitleaksignore")
		configuration.assert_current()
		result["inputs"] = configuration.as_list()
		return result
	candidate = workspace / "candidate"
	candidate.mkdir()
	materialized = _materialize(configuration, files, candidate)
	baseline: dict[str, str] = request.get("baseline") or {}
	version = manifest.policy_release if manifest is not None else baseline.get("policy_release")
	if version is None:
		raise EditError("invalid_request")
	cache = PolicyCache(request.get("cache_dir"))
	scanner, selection = _selection(cache, version, manifest, configuration)
	if time.monotonic() >= deadline:
		raise EditError("deadline")
	code, report, diagnostics = run_process(
		scanner_command(scanner, candidate, coverage=True),
		root=candidate,
		environment={
			key: value
			for key, value in os.environ.items()
			if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "LANG", "LC_ALL"}
		}
		| {
			"HOME": str(workspace),
			"USERPROFILE": str(workspace),
			"TEMP": str(workspace),
			"TMP": str(workspace),
		},
		deadline=deadline,
	)
	_coverage(diagnostics)
	if code not in {0, 1}:
		raise EditError("tool_output")
	records = _records(files, report, candidate, materialized, manifest, exit_code=code)
	statuses = {record["status"] for record in records}
	result.update(
		status=next(
			(status for status in ("failed", "passed", "waived") if status in statuses),
			"not_applicable",
		),
		files=records,
		selections=[selection],
		inputs=configuration.as_list(),
		process_count=3,
	)
	if _selection(cache, version, manifest, configuration) != (scanner, selection):
		raise EditError("stale_input")
	configuration.assert_current()
	for source in files:
		if sha256(read_bounded(regular_path(root, source.path), MAX_FILE_BYTES)) != source.sha256:
			raise EditError("stale_input")
	if time.monotonic() >= deadline:
		raise EditError("deadline")
	encode(result)
	return result
