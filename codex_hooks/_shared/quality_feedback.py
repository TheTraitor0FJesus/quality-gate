"""Generate bounded model context from validated, source-free hook results."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any

from _shared.quality_diagnostics import KNOWN_REASONS, record, retain
from _shared.quality_events import MAX_EVENT_ID_CHARS, MAX_FILES
from _shared.quality_legacy_security import MAX_BUILTIN_FINDINGS
from _shared.quality_settings import MANAGED_BY
from _shared.quality_storage import atomic_json, directory_transaction

MAX_CONTEXT_CHARS = 6000
MAX_PATH_CHARS = 4096
FIRST_CONTROL_CODE = 32
CHECKS = frozenset(
	{
		"ruff",
		"attribution",
		"settings",
		"legacy_security",
		"builtin_secrets",
		"integrity",
		"types",
		"secrets",
		"background",
	}
)
RULE = re.compile(r"^(?:[A-Z][A-Z0-9]{0,15}|invalid-syntax)$")
FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
MAX_SECRET_FINDINGS = 200
MAX_INTEGRITY_FINDINGS = 200
MAX_TYPE_FINDINGS = 200
MYPY_RULE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
MAX_DELIVERY_STATE_BYTES = 1024
RECOVERY = {
	"abandoned_shell_call": (
		"A previous turn ended without command completion evidence. Its capture was retained "
		"under events/unconfirmed; earlier edits remain unchecked. A new baseline is captured."
	),
	"mutation_not_confirmed": (
		"Repeat the edit through apply_patch or restore trusted tool completion metadata; "
		"verification remains incomplete."
	),
	"mutation_failed": (
		"The shell command failed; inspect partial changes "
		"and repeat a successful attributable edit."
	),
	"mechanism_unavailable": "Disable this mechanism until its implementation is installed.",
	"release": "Run explicit Gate sync and edit-setup for the selected release, then retry.",
	"unsupported_policy": (
		"Use the explicit consumer upgrade route; preserve the current pin until reviewed."
	),
	"runtime": (
		"Run explicit Gate setup for the component or restore the configured baseline Python."
	),
	"tool": "Run explicit edit-setup for the selected policy and retry offline.",
	"deadline": (
		"Restore timely verification or explicitly adjust the measured edit budget, then retry."
	),
	"stale_result": "Capture the final content again and repeat verification.",
	"stale_input": "Capture current content again and repeat verification.",
	"overlapping_shell_calls": (
		"Finish overlapping commands, then retry with one shell mutation at a time."
	),
	"snapshot_limit": (
		"Inspect the selected project root and the reported snapshot limit; "
		"a nested working directory may select the same root. Use an attributable patch to retry."
	),
	"snapshot_missing": (
		"Stored Pre evidence is unavailable; inspect capture and pairing evidence before "
		"choosing recovery. Registration failure is not established. Retry an attributable patch."
	),
	"settings_missing": (
		"Initialize managed hook settings and explicitly prepare the required releases."
	),
	"manifest": "Correct the working-tree Gate manifest; do not replace its policy with baseline.",
	"invalid_request": (
		"Repeat the edit through a supported tool and retry integrity verification."
	),
	"invalid_settings": (
		"Restore valid managed settings and the selected release identities, then retry."
	),
	"unsafe_path": (
		"Restore touched files as regular files inside the project root, then retry verification."
	),
	"limits": "Reduce touched-file count or size and retry; coverage remains incomplete.",
	"io": "Check input readability and temporary-workspace creation/cleanup, then retry.",
	"dependencies": "Restore the component's required dependencies and typing stubs, then retry.",
	"tool_output": "Restore the selected Gate release and retry; raw output is omitted.",
	"background": "Restore valid hook settings and retry; verification remains incomplete.",
}
for _reason in KNOWN_REASONS:
	RECOVERY.setdefault(
		_reason,
		(
			"Inspect retained evidence for the failed operation and restore "
			"that boundary before retrying; verification remains incomplete."
		),
	)
RECOVERY.update(
	{
		"cancelled": (
			"Verification was cancelled; retry current final bytes when the owning task is active."
		),
		"configuration": (
			"Restore the selected check configuration, then retry current final bytes."
		),
		"invalid_provider_result": (
			"Inspect provider identity and its supported result contract, then"
			" retry; do not trust incomplete output."
		),
		"background_unavailable": (
			"Inspect the background state or launch operation and retained OS "
			"error; retry after restoring that boundary."
		),
	}
)


def runtime_directory() -> Path:
	"""Keep retained reports and snapshots outside repositories and active sources."""
	if os.name == "nt":
		base = os.environ.get("LOCALAPPDATA")
	else:
		base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
	if not base:
		raise OSError("runtime_directory_unavailable")
	return Path(base) / "codex-python-quality"


def safe_relative(value: object) -> str | None:
	if not isinstance(value, str) or not value or len(value) > MAX_PATH_CHARS:
		return None
	if any(ord(character) < FIRST_CONTROL_CODE for character in value):
		return None
	path = PurePosixPath(value.replace("\\", "/"))
	if path.is_absolute() or ".." in path.parts or ":" in value:
		return None
	return path.as_posix()


def ruff_lines(files: object) -> list[str]:
	if not isinstance(files, list):
		return []
	lines = []
	for item in files[:10]:
		if not isinstance(item, dict) or (path := safe_relative(item.get("path"))) is None:
			continue
		findings = item.get("findings", [])
		if not isinstance(findings, list):
			continue
		for finding in findings[:200]:
			if not isinstance(finding, dict):
				continue
			rule, line, column = finding.get("rule"), finding.get("line"), finding.get("column")
			if not isinstance(rule, str) or not RULE.fullmatch(rule):
				continue
			if type(line) is not int or type(column) is not int or min(line, column) <= 0:
				continue
			lines.append(
				f"{path}:{line}:{column} Ruff {rule}: violation remains; "
				"resolve this rule in the final file."
			)
		if item.get("format") == "failed":
			lines.append(f"{path}: Ruff format check failed; correct formatting in the final file.")
	return lines


def integrity_lines(findings: object) -> list[str]:
	"""Render only Gate locations; never echo marker text or provider messages."""
	if not isinstance(findings, list):
		return []
	lines = []
	for finding in findings[:MAX_INTEGRITY_FINDINGS]:
		if not isinstance(finding, dict):
			continue
		path = safe_relative(finding.get("path"))
		line = finding.get("line")
		if path is None or type(line) is not int or not 0 < line <= 2**31 - 1:
			continue
		lines.append(
			f"{path}:{line} repository.git.conflict_markers: "
			"resolve the merge conflict in the final file."
		)
	return lines


def typing_lines(findings: object) -> list[str]:
	"""Keep mypy locations and rule identities without echoing source or messages."""
	if not isinstance(findings, list) or len(findings) > MAX_TYPE_FINDINGS:
		return []
	lines = []
	for finding in findings:
		if not isinstance(finding, dict):
			return []
		path = safe_relative(finding.get("path"))
		line, column, rule = finding.get("line"), finding.get("column"), finding.get("rule")
		if (
			path in {None, "."}
			or type(line) is not int
			or type(column) is not int
			or not 0 < line <= 2**31 - 1
			or not 0 < column <= 2**31 - 1
			or not isinstance(rule, str)
			or MYPY_RULE.fullmatch(rule) is None
		):
			return []
		lines.append(
			f"{path}:{line}:{column} mypy {rule}: type error remains; "
			"resolve this rule in the analyzed component."
		)
	return list(dict.fromkeys(lines))


def builtin_lines(files: list[dict[str, Any]]) -> list[str]:
	lines = []
	for item in files[:MAX_BUILTIN_FINDINGS]:
		path = safe_relative(item.get("path")) if isinstance(item, dict) else None
		if path is None:
			continue
		line = item.get("line")
		if type(line) is int and 0 < line <= 2**31 - 1:
			lines.append(
				f"{path}:{line} secrets.builtin: preserved built-in protection found "
				"a possible secret; remove it. "
				"Gate waivers apply only to secrets.candidate."
			)
	return lines


def security_lines(report: dict[str, Any]) -> list[str]:
	if report["check"] == "builtin_secrets":
		return builtin_lines(report.get("files", [])) or [
			"Built-in protection FAILED: restore valid finding locations and repeat verification."
		]
	lines = [
		"Existing security protection found a possible secret; remove it from the touched content."
	]
	for item in report.get("files", [])[:MAX_FILES]:
		if isinstance(item, dict) and (path := safe_relative(item.get("path"))) is not None:
			lines.append(
				f"{path}: legacy security finding; inspect locally without echoing the value."
			)
	return lines


def unchecked_scope(report: dict[str, Any]) -> list[str]:
	"""Show host-owned input scope without treating it as the exact failing path."""
	lines = []
	root = report.get("root")
	if (
		isinstance(root, str)
		and len(root) <= MAX_PATH_CHARS
		and not any(ord(character) < FIRST_CONTROL_CODE for character in root)
		and Path(root).is_absolute()
	):
		lines.append(f"Repository: {root}")
	paths = report.get("input_paths")
	if isinstance(paths, list):
		for value in paths[:MAX_FILES]:
			if (path := safe_relative(value)) is not None:
				lines.append(f"Input path: {path}")
	return lines


def unchecked_lines(report: dict[str, Any], check: str) -> list[str]:
	lines: list[str] = []
	clean = record(report)
	reason = report.get("reason")
	action = (
		RECOVERY.get(
			reason,
			"Restore hook prerequisites and repeat the edit; verification remains incomplete.",
		)
		if isinstance(reason, str)
		else ("Restore hook prerequisites and repeat the edit; verification remains incomplete.")
	)
	code = reason if isinstance(reason, str) and reason in RECOVERY else "unknown"
	if reason == "deadline" and (
		check == "background" or clean["diagnostic"].get("phase") == "background"
	):
		action = (
			"Inspect the background work, drain and cleanup deadline; "
			"the foreground edit budget does not establish the cause. Retry current bytes."
		)
	lines.append(f"{check} UNCHECKED: {action} [reason={code}]")
	details = clean["diagnostic"]
	lines.append(
		"Diagnostic: "
		+ " ".join(
			f"{name}={details.get(name, 'unavailable')}"
			for name in (
				"operation",
				"phase",
				"category",
				"errno",
				"winerror",
				"limit_dimension",
				"observed",
				"allowed",
			)
		)
	)
	lines.append(
		"Call: "
		+ " ".join(
			f"{name}={clean.get('event', {}).get(name, 'unavailable')}"
			for name in ("session_id", "turn_id", "tool_use_id")
		)
	)
	lines.append(
		"Missing evidence: complete current verification; "
		"unavailable diagnostic fields are not established causes."
	)
	if clean.get("writes_may_have_occurred") is True:
		lines.append(
			"Partial writes may have occurred; inspect final bytes before retrying verification."
		)
	elif clean.get("writes_may_have_occurred") is False:
		lines.append("Writes from this check: none.")
	else:
		lines.append("Writes from this check: unavailable.")
	lines.extend(unchecked_scope(clean))
	if "root" not in clean:
		lines.append("Repository: unavailable")
	if not clean.get("input_paths"):
		lines.append("Input scope: unavailable")
	lines.append(f"Background event: {clean.get('background_event_id', 'unavailable')}")
	return lines


def report_lines(reports: list[dict[str, Any]]) -> tuple[list[str], bool]:
	lines: list[str] = []
	security_block = False
	for report in reports:
		raw_check = report.get("check")
		check = raw_check if isinstance(raw_check, str) and raw_check in CHECKS else "quality"
		status = report.get("status")
		if status in {"passed", "not_applicable", "pending"}:
			continue
		if check in {"integrity", "secrets"} and status == "waived":
			continue
		if status == "failed" and check == "ruff":
			lines.extend(
				ruff_lines(report.get("files"))
				or ["Ruff FAILED: resolve final lint/format findings."]
			)
		elif status == "failed" and check == "secrets":
			lines.extend(secret_lines(report.get("files")))
		elif status == "failed" and check == "integrity":
			lines.extend(
				integrity_lines(report.get("findings"))
				or ["Integrity FAILED: resolve the merge conflict."]
			)
		elif status == "failed" and check == "types":
			if report.get("reason") == "repeated_findings":
				lines.append("types FAILED: unchanged findings remain; resolve them.")
			else:
				lines.extend(
					typing_lines(report.get("findings"))
					or ["types UNCHECKED: restore valid mypy locations and repeat verification."]
				)
		elif status == "failed" and check in {"legacy_security", "builtin_secrets"}:
			security_block = True
			lines.extend(security_lines(report))
		else:
			lines.extend(unchecked_lines(report, check))
	return lines, security_block


def secret_finding_line(finding: object, path: str) -> str | None:
	if not isinstance(finding, dict) or finding.get("check_id") != "secrets.candidate":
		raise ValueError("invalid_secret_finding")
	line, fingerprint = finding.get("line"), finding.get("fingerprint")
	if (
		type(line) is not int
		or not 0 < line <= 2**31 - 1
		or not isinstance(fingerprint, str)
		or not FINGERPRINT.fullmatch(fingerprint)
	):
		raise ValueError("invalid_secret_finding")
	status = finding.get("status", "failed")
	if not isinstance(status, str) or status not in {"failed", "waived"}:
		raise ValueError("invalid_secret_finding")
	if status == "waived":
		return None
	return (
		f"{path}:{line} secrets.candidate: possible secret "
		f"(fingerprint: {fingerprint}); remove the value from touched content "
		"and rotate or revoke it if it is a real credential."
	)


def secret_lines(files: object) -> list[str]:
	"""Validate the complete presentation before emitting deduplicated diagnostics."""
	invalid = [
		"secrets UNCHECKED: Restore a valid source-free scanner report and repeat verification."
	]
	if not isinstance(files, list) or not 0 < len(files) <= MAX_FILES:
		return invalid
	lines = []
	total = 0
	try:
		for item in files:
			if not isinstance(item, dict) or (path := safe_relative(item.get("path"))) is None:
				return invalid
			findings = item.get("findings")
			if not isinstance(findings, list):
				return invalid
			total += len(findings)
			if total > MAX_SECRET_FINDINGS:
				return invalid
			for finding in findings:
				if (text := secret_finding_line(finding, path)) is not None:
					lines.append(text)
	except ValueError:
		return invalid
	return list(dict.fromkeys(lines)) or invalid


def delivery_provenance(
	reports: list[dict[str, Any]], settings: dict[str, Any]
) -> dict[str, Any] | None:
	"""Identify unresolved Ruff or integrity findings for safe repeat compaction."""
	writers = [report for report in reports if report.get("check") == "ruff"]
	integrity = [report for report in reports if report.get("check") == "integrity"]
	tracked = [*writers, *integrity]
	if not any(report.get("status") == "failed" for report in tracked):
		return None
	if any(
		report.get("status") == "failed"
		for report in reports
		if report.get("check") not in {"ruff", "integrity"}
	):
		return None
	if any(
		report.get("status") not in {"passed", "failed", "not_applicable"}
		for report in reports
		if report.get("check") not in {"ruff", "integrity"}
	) or any(
		report.get("status") not in {"passed", "failed", "not_applicable"} for report in tracked
	):
		return None
	writer_proof = []
	for writer in writers:
		item = {
			name: writer.get(name)
			for name in ("schema", "status", "root", "engine_version", "selections", "inputs")
		}
		item["files"] = [
			{
				name: file.get(name)
				for name in (
					"path",
					"status",
					"sha256",
					"coverage",
					"component",
					"selection",
					"format",
					"findings",
				)
			}
			for file in writer["files"]
		]
		writer_proof.append(item)
	integrity_proof = [
		{
			"root": report.get("root"),
			"status": report.get("status"),
			"findings": [
				{name: finding.get(name) for name in ("path", "line")}
				for finding in report.get("findings", [])
			],
		}
		for report in integrity
	]
	return {"settings": settings, "writers": writer_proof, "integrity": integrity_proof}


def remember_delivery(path: Path, key: str, signature: str | None) -> bool:
	"""Keep one bounded presentation receipt; never store or reuse a check verdict."""
	with directory_transaction(path.parent):
		previous: dict[str, Any] = {}
		if path.exists():
			with path.open("rb") as source:
				content = source.read(MAX_DELIVERY_STATE_BYTES + 1)
			if len(content) > MAX_DELIVERY_STATE_BYTES:
				return False
			previous = json.loads(content)
			if (
				not isinstance(previous, dict)
				or previous.get("schema") != 1
				or previous.get("managed_by") != MANAGED_BY
			):
				return False
		atomic_json(
			path, {"schema": 1, "managed_by": MANAGED_BY, "key": key, "signature": signature}
		)
		return (
			signature is not None
			and previous.get("key") == key
			and previous.get("signature") == signature
		)


def deliver_feedback(
	event: str,
	reports: list[dict[str, Any]],
	*,
	session_id: object,
	settings: dict[str, Any],
	state_dir: Path,
	evidence_dir: Path | None = None,
) -> dict[str, Any] | None:
	"""Compact unchanged failures only after the selected checks have completed."""
	result = feedback(event, reports, evidence_dir=evidence_dir)
	if (
		event != "PostToolUse"
		or not isinstance(session_id, str)
		or not 0 < len(session_id) <= MAX_EVENT_ID_CHARS
	):
		return result
	try:
		roots = sorted(
			{
				report["root"]
				for report in reports
				if report.get("check") in {"ruff", "integrity"} and report.get("root")
			}
		)
		if not roots or any(not Path(root).is_absolute() for root in roots):
			return result
		directory = evidence_dir or runtime_directory() / "reports"
		if any(
			path.resolve().is_relative_to(Path(root).resolve())
			for root in roots
			for path in (state_dir, directory)
		):
			return result
		key = hashlib.sha256(
			json.dumps([session_id, roots], ensure_ascii=True).encode()
		).hexdigest()
		proof = delivery_provenance(reports, settings)
		signature = (
			None
			if proof is None
			else hashlib.sha256(
				json.dumps(proof, sort_keys=True, ensure_ascii=True).encode()
			).hexdigest()
		)
		repeated = remember_delivery(state_dir / "feedback" / "latest.json", key, signature)
		if not repeated or result is None:
			return result
		context = result["hookSpecificOutput"]["additionalContext"]
		identity = hashlib.sha256(context.encode("utf-8")).hexdigest()
		path = directory / f"delivery-{identity}.json"
		atomic_json(path, {"schema": 1, "context": context})
		checks = [
			"Ruff" if name == "ruff" else "Integrity"
			for name in ("ruff", "integrity")
			if any(
				report.get("check") == name and report.get("status") == "failed"
				for report in reports
			)
		]
		header = f"{' and '.join(checks)} FAILED: unchanged findings; verification repeated.\n"
		footer = (
			f"\nResolve the remaining findings. Full source-free report: {path}\n{ACTION_REQUIRED}"
		)
		remaining = MAX_CONTEXT_CHARS - len(header) - len(footer)
		if remaining <= 0:
			return result
		first = context.splitlines()[1]
		result["hookSpecificOutput"]["additionalContext"] = header + first[:remaining] + footer
	except (OSError, ValueError, TypeError, KeyError, IndexError):
		# Presentation optimization cannot suppress the original unresolved status.
		return result
	return result


ACTION_REQUIRED = (
	"ACTION REQUIRED: Fix the reported findings within the task scope. "
	"The hook will check the next edit automatically. "
	"Report unresolved or out-of-scope blockers explicitly."
)
UNCHECKED_ACTION_REQUIRED = (
	"ACTION REQUIRED: Immediately report this incomplete verification to the user. "
	"Include the check and diagnostic above, repository, affected paths, and any available "
	"source-free evidence path; identify unavailable details and never include secrets or source. "
	"Do not claim verification passed. If the user has already authorized diagnosing or "
	"repairing this incomplete verification, continue within that authorized scope and "
	"report progress; repeated reports of the same unresolved condition do not require "
	"renewed approval. Otherwise pause the task and await the user's instructions "
	"before repairing prerequisites or continuing work."
)


def feedback(
	event: str, reports: list[dict[str, Any]], *, evidence_dir: Path | None = None
) -> dict[str, Any] | None:
	"""Preserve original tool output except when existing security protection blocks."""
	lines, security_block = report_lines(reports)
	for report in reports:
		if report.get("status") == "unchecked":
			try:
				directory = evidence_dir or runtime_directory() / "reports"
				identity, evidence_path = retain(report, directory)
			except OSError:
				identity, evidence_path = "unavailable", None
			lines.append(
				f"Retained diagnostic: id={identity} evidence={evidence_path or 'unavailable'}"
			)
	if not lines:
		return None
	action = (
		UNCHECKED_ACTION_REQUIRED
		if any(" UNCHECKED:" in line for line in lines)
		else ACTION_REQUIRED
	)
	text = "Python quality checks\n" + "\n".join(lines) + "\n" + action
	if len(text) > MAX_CONTEXT_CHARS:
		try:
			directory = evidence_dir or runtime_directory() / "reports"
			identity = hashlib.sha256(text.encode("utf-8")).hexdigest()
			path = directory / f"{identity}.json"
			atomic_json(path, {"schema": 1, "context": text})
			footer = f"\nUnresolved findings remain. Full source-free evidence: {path}\n{action}"
		except OSError:
			footer = (
				"\nUNCHECKED: full evidence could not be retained; unresolved findings remain. "
				"Runtime storage needs recovery.\n" + UNCHECKED_ACTION_REQUIRED
			)
		text = text[: max(0, MAX_CONTEXT_CHARS - len(footer))] + footer
	result: dict[str, Any] = {
		"hookSpecificOutput": {"hookEventName": event, "additionalContext": text},
	}
	if security_block:
		result.update(decision="block", reason=text)
	return result
