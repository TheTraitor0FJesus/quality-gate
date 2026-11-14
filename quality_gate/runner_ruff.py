"""Per-check Ruff evidence reuse within the active Gate operation."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import runner as _runner
from .admission import running_in_ci
from .distribution import ExternalTool, ReleaseManifest
from .edit_inputs import Configuration
from .edit_protocol import EditError
from .evidence_candidate import CandidateEvidence
from .evidence_identity import _REVIEWED_RUFF_POLICIES
from .launcher import PreparedEnvironment
from .runner_ruff_evidence import (
	LookupStats,
	_assert_evidence_current,
	_collect_evidence,
	_prepare_evidence,
	_publish_evidence,
	_verified_ruff,
)
from .runner_ruff_execution import _check_import_rules, _eligible_files, _local_format, _local_lint
from .runner_ruff_reports import _build_ruff_results
from .runtime import RuntimeIdentity
from .snapshot import SnapshotError
from .verification_evidence import EngineIdentity, EvidenceStore

_MAX_TARGET_ARGUMENT_CHARS = 24_000


class RuffEvidenceChecks:
	"""Run one complete lint and format pair while retaining each observed result."""

	def __init__(
		self,
		root: Path,
		component: _runner.PythonComponent,
		prepared: PreparedEnvironment,
		python: Path,
		index: int,
		environment: dict[str, str],
		deadline: float,
		timeout: int,
		tool: list[str],
		native_tool: Path,
		tool_sha: str,
		repository: str,
		engine: EngineIdentity,
		release: ReleaseManifest,
		ruff_tool: ExternalTool,
		runtime: RuntimeIdentity,
		store: EvidenceStore,
		candidate: CandidateEvidence,
		policy_sha: str,
		release_sha: str,
		python_sha: str,
	) -> None:
		self.root, self.component, self.prepared, self.python = root, component, prepared, python
		self.index, self.environment, self.deadline, self.timeout = (
			index,
			environment,
			deadline,
			timeout,
		)
		self.tool, self.native_tool, self.tool_sha = tool, native_tool, tool_sha
		self.repository, self.engine, self.release, self.ruff_tool = (
			repository,
			engine,
			release,
			ruff_tool,
		)
		self.runtime, self.store, self.candidate = runtime, store, candidate
		self.policy_sha, self.release_sha, self.python_sha = policy_sha, release_sha, python_sha
		self.policy = prepared.policy_root / "quality_gate" / "policy" / "ruff.toml"
		self.arguments = [component.path.relative_to(root).as_posix()]
		self.common = ["--config", str(self.policy), "--no-cache", "--force-exclude", "--quiet"]
		self.started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
		self.started_monotonic = time.monotonic()
		self.check_id = f"python.component_{index}.ruff"
		self.format_id = f"python.component_{index}.format"
		self.configurations: dict[str, Configuration] = {}
		self.identities: dict[str, dict[str, Any]] = {}
		self.cached: dict[str, dict[str, Any]] = {}
		self.lookup = LookupStats()
		self.files: dict[str, tuple[bytes, int]] = {}
		self.fresh: dict[str, dict[str, object]] = {}
		self.eligible: list[str] = []
		self.targets: list[str] = []
		self.import_findings: list[dict[str, object]] = []
		self.syntax_paths: set[str] = set()
		self.lint_complete = False

	def assert_current(self) -> None:
		_assert_evidence_current(
			self.candidate,
			self.files,
			self.configurations,
			self.engine,
			self.native_tool,
			self.tool_sha,
			self.policy,
			self.policy_sha,
			self.prepared,
			self.release_sha,
			self.python,
			self.python_sha,
		)

	def lint(self) -> _runner.CheckResult:
		"""Run fresh import rules and missing file-local lint before recording Ruff."""
		self.eligible = _eligible_files(
			self.tool,
			self.common,
			self.arguments,
			self.root,
			self.environment,
			self.deadline,
			self.timeout,
		)
		misses: list[str] = []
		_collect_evidence(
			self.store,
			self.engine,
			self.repository,
			self.ruff_tool,
			self.release,
			self.tool_sha,
			self.runtime,
			self.eligible,
			self.root,
			misses,
			self.candidate,
			self.files,
			self.configurations,
			self.release_sha,
			self.policy_sha,
			self.python_sha,
			self.identities,
			self.cached,
			self.lookup,
		)
		self.targets = list(misses)
		if len(" ".join(self.targets)) + 2048 > _MAX_TARGET_ARGUMENT_CHARS:
			self.targets = list(self.eligible)
			self.cached.clear()
			self.lookup.producer_durations_ms.clear()
		if self.eligible:
			self.import_findings = _check_import_rules(
				self.tool,
				self.common,
				self.arguments,
				self.root,
				self.environment,
				self.deadline,
				self.timeout,
				self.eligible,
			)
			findings, self.syntax_paths = _local_lint(
				self.tool,
				self.common,
				self.targets,
				self.root,
				self.environment,
				self.deadline,
				self.timeout,
			)
			for path in self.targets:
				file_findings = findings.get(path, [])
				lint_status = "failed" if file_findings else "passed"
				self.fresh[path] = {
					"status": lint_status,
					"lint_status": lint_status,
					"format_status": "passed",
					"findings": file_findings,
				}
		if set(self.cached) | set(self.fresh) != set(self.eligible):
			raise ValueError("Ruff evidence did not cover the selected scope")
		self.assert_current()
		self.lint_complete = True
		return self._with_lookup_summary(
			_build_ruff_results(
				self.cached | self.fresh,
				self.import_findings,
				self.cached,
				self.eligible,
				self.check_id,
				self.format_id,
			)[0]
		)

	def format(self) -> _runner.CheckResult:
		"""Run missing format checks, revalidate all hits and publish complete outcomes."""
		unformatted = _local_format(
			self.tool,
			self.common,
			self.targets,
			self.root,
			self.environment,
			self.deadline,
			self.timeout,
			self.syntax_paths,
		)
		for path, outcome in self.fresh.items():
			format_status = (
				"failed" if path in self.syntax_paths or path in unformatted else "passed"
			)
			outcome["format_status"] = format_status
			outcome["status"] = (
				"failed"
				if outcome["lint_status"] == "failed" or format_status == "failed"
				else "passed"
			)
		self.assert_current()
		_publish_evidence(
			self.candidate,
			self.store,
			self.engine,
			self.files,
			self.native_tool,
			self.tool_sha,
			self.policy,
			self.policy_sha,
			self.prepared,
			self.release_sha,
			self.python,
			self.python_sha,
			self.fresh,
			self.identities,
			self.configurations,
			self.root,
			self.started_at,
			self.started_monotonic,
		)
		return self._with_lookup_summary(
			_build_ruff_results(
				self.cached | self.fresh,
				self.import_findings,
				self.cached,
				self.eligible,
				self.check_id,
				self.format_id,
			)[1]
		)

	def _with_lookup_summary(self, result: _runner.CheckResult) -> _runner.CheckResult:
		if not self.cached:
			return result
		durations = self.lookup.producer_durations_ms
		return replace(
			result,
			summary=(
				f"{result.summary}; shared evidence lookup {self.lookup.lookup_ms} ms; "
				f"original producer operation duration {min(durations)} to {max(durations)} ms"
			),
		)

	def lint_checked(self) -> _runner.CheckResult:
		try:
			return self.lint()
		except (
			_runner.QualityGateError,
			OSError,
			ValueError,
			TypeError,
			KeyError,
			EditError,
			SnapshotError,
		) as error:
			return _unchecked(self.check_id, error)

	def format_checked(self) -> _runner.CheckResult:
		try:
			return self.format()
		except (
			_runner.QualityGateError,
			OSError,
			ValueError,
			TypeError,
			KeyError,
			EditError,
			SnapshotError,
		) as error:
			return _unchecked(self.format_id, error)


def _unchecked(check_id: str, error: Exception) -> _runner.CheckResult:
	return _runner._error_result(
		_runner.QualityGateError(
			f"Ruff evidence verification is incomplete: {error}",
			check_id=check_id,
			exit_code=_runner.EXIT_UNCHECKED,
			recovery_action="restore complete Ruff reports and retry the quality gate",
		)
	)


def prepare_ruff_evidence(
	root: Path,
	repository: Path,
	component: _runner.PythonComponent,
	prepared: PreparedEnvironment,
	python: Path,
	index: int,
	environment: dict[str, str],
	deadline: float,
	timeout: int,
) -> RuffEvidenceChecks | None:
	"""Return a cache-capable plan or let the existing full-scope runner execute."""
	if running_in_ci():
		return None
	tool, native_tool, tool_sha = _verified_ruff(prepared, python)
	policy = prepared.policy_root / "quality_gate" / "policy" / "ruff.toml"
	(
		repository_id,
		engine,
		release,
		ruff_tool,
		runtime,
		store,
		candidate,
		enabled,
		policy_sha,
		release_sha,
		python_sha,
	) = _prepare_evidence(
		repository,
		prepared,
		index,
		tool_sha,
		policy,
		python,
		deadline,
		timeout,
	)
	if (
		not enabled
		or native_tool is None
		or tool_sha is None
		or repository_id is None
		or engine is None
		or release is None
		or ruff_tool is None
		or runtime is None
		or store is None
		or candidate is None
		or policy_sha not in _REVIEWED_RUFF_POLICIES
		or ruff_tool.version != "0.16.4"
	):
		return None
	return RuffEvidenceChecks(
		root,
		component,
		prepared,
		python,
		index,
		environment,
		deadline,
		timeout,
		tool,
		native_tool,
		tool_sha,
		repository_id,
		engine,
		release,
		ruff_tool,
		runtime,
		store,
		candidate,
		policy_sha,
		release_sha,
		python_sha,
	)
