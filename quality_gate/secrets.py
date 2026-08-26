"""Fail-closed, redaction-safe Gitleaks integration."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .contracts import CheckResult, Finding, Manifest, Status, fingerprint_secret
from .distribution import ReleaseManifest

_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")
_GITLEAKS = "gitleaks"
MAX_REFERENCE_LENGTH = 256


class PreparedScanner(Protocol):
	@property
	def policy_root(self) -> Path: ...

	@property
	def release_manifest(self) -> ReleaseManifest: ...


@dataclass(frozen=True, slots=True)
class _SecretFinding:
	finding: Finding
	fingerprint: str


def _unchecked(check_id: str, message: str) -> CheckResult:
	return CheckResult(
		check_id=check_id,
		status=Status.UNCHECKED,
		summary=message,
		findings=(
			Finding(
				message=message,
				action="restore the pinned Gitleaks tool and retry the quality gate",
			),
		),
		recovery_action="restore the pinned Gitleaks tool and retry the quality gate",
	)


def _scanner_path(prepared: PreparedScanner) -> Path | None:
	tools = getattr(getattr(prepared, "release_manifest", None), "tools", ())
	candidates = [tool for tool in tools if getattr(tool, "name", "").casefold() == _GITLEAKS]
	if len(candidates) != 1:
		return None
	policy_root = Path(prepared.policy_root).resolve()
	path = (policy_root / str(candidates[0].path)).resolve()
	if policy_root not in path.parents or path.suffix.casefold() == ".whl":
		return None
	return path if path.is_file() else None


def _run_scanner(command: list[str], root: Path, *, report_path: Path, timeout: float) -> int:
	"""Run Gitleaks without retaining its potentially sensitive console output."""
	log_path = report_path.with_suffix(".log")
	try:
		with log_path.open("wb") as log:
			process = subprocess.Popen(
				command,
				cwd=root,
				env=_scanner_environment(),
				stdout=log,
				stderr=subprocess.STDOUT,
			)
			try:
				return process.wait(timeout=timeout)
			except subprocess.TimeoutExpired:
				process.kill()
				process.wait(timeout=1)
				raise
	except (OSError, subprocess.TimeoutExpired):
		return -1


def _scanner_environment() -> dict[str, str]:
	allowed = {"LANG", "LC_ALL", "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR"}
	return {key: value for key, value in os.environ.items() if key in allowed}


def _safe_location(value: object, root: Path) -> str:
	if not isinstance(value, str) or not value.strip():
		raise ValueError("scanner finding has no file")
	path = value.replace("\\", "/")
	try:
		candidate = Path(path)
		if candidate.is_absolute():
			candidate = candidate.resolve().relative_to(root.resolve())
	except (OSError, ValueError) as error:
		raise ValueError("scanner finding has an unsafe file") from error
	path = candidate.as_posix()
	if path in {"", "."} or path.startswith("../") or "/../" in f"/{path}":
		raise ValueError("scanner finding has an unsafe file")
	return path


def _line(value: object) -> int:
	if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
		raise ValueError("scanner finding has no valid line")
	return value


def _commit(value: object) -> str:
	if value in (None, ""):
		return ""
	if not isinstance(value, str) or not _SHA.fullmatch(value):
		raise ValueError("scanner finding has an unsafe commit")
	return value.lower()


def _parse_report(report_path: Path, root: Path, *, include_commit: bool) -> list[_SecretFinding]:
	try:
		data = json.loads(report_path.read_text(encoding="utf-8"))
	except (OSError, UnicodeError, json.JSONDecodeError) as error:
		raise ValueError("scanner report is unavailable or malformed") from error
	if not isinstance(data, list):
		raise ValueError("scanner report is not a list")
	findings: list[_SecretFinding] = []
	for item in data:
		if not isinstance(item, dict):
			raise ValueError("scanner report contains an invalid finding")
		path = _safe_location(item.get("File", item.get("file")), root)
		line = _line(item.get("StartLine", item.get("start_line", item.get("Line"))))
		value = item.get("Secret", item.get("secret"))
		if not isinstance(value, str) or not value:
			raise ValueError("scanner report does not contain a secret fingerprint source")
		commit = _commit(item.get("Commit", item.get("commit"))) if include_commit else ""
		target = f"{path}:{line}"
		if include_commit:
			if not commit:
				raise ValueError("history finding has no commit")
			target = f"{commit}:{target}"
		fingerprint = fingerprint_secret(value)
		findings.append(
			_SecretFinding(
				Finding(
					path=target,
					message=f"secret detected (fingerprint: {fingerprint})",
					action=(
						"rotate or revoke the credential and remove it from the candidate/history"
					),
				),
				fingerprint,
			)
		)
	return findings


def _apply_waivers(
	check_id: str,
	summary: str,
	findings: list[_SecretFinding],
	manifest: Manifest,
) -> CheckResult:
	if not findings:
		return CheckResult(check_id, Status.PASSED, summary)
	remaining = [
		item
		for item in findings
		if manifest.resolve_waiver(check_id, item.finding.path, item.fingerprint) is None
	]
	if not remaining:
		return CheckResult(
			check_id,
			Status.WAIVED,
			f"{summary}; exact current waiver applies",
			waiver_target=findings[0].finding.path,
		)
	return CheckResult(
		check_id,
		Status.FAILED,
		summary,
		findings=tuple(item.finding for item in remaining),
		recovery_action=(
			"rotate or revoke the credential, remove it from the candidate/history, "
			"then retry the quality gate"
		),
	)


def _scan(
	root: Path,
	manifest: Manifest,
	prepared: PreparedScanner,
	*,
	check_id: str,
	mode: str,
	log_opts: str | None = None,
	include_commit: bool = False,
) -> CheckResult:
	scanner = _scanner_path(prepared)
	if scanner is None:
		return _unchecked(check_id, "pinned Gitleaks tool is unavailable")
	with tempfile.TemporaryDirectory(prefix="quality-gate-gitleaks-") as temporary:
		report_path = Path(temporary) / "report.json"
		command = [
			str(scanner),
			mode,
			"--no-banner",
			"--redact=100",
			"--report-format",
			"json",
			"--report-path",
			str(report_path),
			"--exit-code",
			"1",
		]
		if log_opts is not None:
			command.extend(["--log-opts", log_opts])
		command.append(str(root))
		returncode = _run_scanner(
			command,
			root,
			report_path=report_path,
			timeout=manifest.repository.command_timeout_seconds,
		)
		try:
			findings = _parse_report(report_path, root, include_commit=include_commit)
		except ValueError:
			return _unchecked(check_id, "Gitleaks report is unavailable or malformed")
	if returncode < 0:
		return _unchecked(check_id, "Gitleaks could not complete within the time budget")
	if returncode not in {0, 1}:
		return _unchecked(check_id, "Gitleaks verification is unavailable")
	if returncode == 1 and not findings:
		return _unchecked(check_id, "Gitleaks reported an unavailable or skipped target")
	return _apply_waivers(
		check_id,
		"no credentials detected" if not findings else "credentials detected",
		findings,
		manifest,
	)


def secret_candidate_result(
	root: Path, manifest: Manifest, prepared: PreparedScanner
) -> CheckResult:
	"""Scan the exact materialized candidate tree."""
	return _scan(root, manifest, prepared, mode="dir", check_id="secrets.candidate")


def _resolve_commit(repository: Path, reference: str, timeout: int) -> str | None:
	if not reference or len(reference) > MAX_REFERENCE_LENGTH or "\x00" in reference:
		return None
	refs = (reference, f"refs/remotes/origin/{reference}", f"refs/heads/{reference}")
	resolved: set[str] = set()
	for candidate in dict.fromkeys(refs):
		try:
			result = subprocess.run(
				[
					"git",
					"-C",
					str(repository),
					"rev-parse",
					"--verify",
					"--end-of-options",
					f"{candidate}^{{commit}}",
				],
				capture_output=True,
				check=False,
				timeout=timeout,
			)
		except (OSError, subprocess.TimeoutExpired):
			return None
		if result.returncode:
			continue
		try:
			value = (
				result.stdout.strip().decode("ascii")
				if isinstance(result.stdout, bytes)
				else result.stdout.strip()
			)
		except UnicodeError:
			return None
		if not re.fullmatch(r"[0-9a-fA-F]{40}", value):
			return None
		resolved.add(value.lower())
	return next(iter(resolved)) if len(resolved) == 1 else None


def _is_shallow(repository: Path, timeout: int) -> bool:
	try:
		result = subprocess.run(
			["git", "-C", str(repository), "rev-parse", "--is-shallow-repository"],
			capture_output=True,
			check=False,
			timeout=timeout,
		)
	except (OSError, subprocess.TimeoutExpired):
		return True
	if result.returncode:
		return True
	try:
		value = (
			result.stdout.strip().decode("ascii")
			if isinstance(result.stdout, bytes)
			else result.stdout.strip()
		)
	except UnicodeError:
		return True
	return value.casefold() != "false"


def secret_history_result(
	repository: Path,
	manifest: Manifest,
	prepared: PreparedScanner,
	*,
	base: str | None = None,
	head: str = "HEAD",
) -> CheckResult:
	"""Scan the commits in one verified base-to-head range."""
	if not base:
		return CheckResult(
			"secrets.history",
			Status.NOT_APPLICABLE,
			"base-to-head history scan is not requested",
			recovery_action="provide a verified CI base reference when range scanning applies",
		)
	base_commit = _resolve_commit(repository, base, manifest.repository.command_timeout_seconds)
	head_commit = _resolve_commit(repository, head, manifest.repository.command_timeout_seconds)
	if not base_commit or not head_commit:
		return _unchecked("secrets.history", "base-to-head history is unavailable or ambiguous")
	return _scan(
		repository,
		manifest,
		prepared,
		mode="git",
		log_opts=f"{base_commit}..{head_commit}",
		check_id="secrets.history",
		include_commit=True,
	)


def secret_audit_result(
	repository: Path, manifest: Manifest, prepared: PreparedScanner
) -> CheckResult:
	"""Scan all reachable Git history for a migration baseline."""
	if _is_shallow(repository, manifest.repository.command_timeout_seconds):
		return _unchecked("secrets.audit", "complete Git history is unavailable")
	return _scan(
		repository,
		manifest,
		prepared,
		mode="git",
		log_opts="--all",
		check_id="secrets.audit",
		include_commit=True,
	)
