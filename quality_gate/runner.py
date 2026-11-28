"""Deterministic quality checks used by the local hook and CI."""

from __future__ import annotations

import hashlib as hashlib
import io as io
import json as json
import locale as locale
import logging as logging
import os as os
import platform as platform
import re as re
import subprocess as subprocess
import sys as sys
import tempfile as tempfile
import threading as threading
import time as time
import tokenize as tokenize
import tomllib as tomllib
from collections.abc import Callable as Callable
from collections.abc import Iterable as Iterable
from collections.abc import Iterator as Iterator
from contextlib import (
	AbstractContextManager as AbstractContextManager,
)
from contextlib import (
	ExitStack as ExitStack,
)
from contextlib import (
	contextmanager as contextmanager,
)
from contextlib import (
	nullcontext as nullcontext,
)
from dataclasses import dataclass as dataclass
from dataclasses import replace as replace
from pathlib import Path as Path
from pathlib import PurePosixPath as PurePosixPath
from typing import Literal as Literal

from . import __version__ as __version__
from .admission import (
	AdmissionError as AdmissionError,
)
from .admission import (
	child_admission_environment as child_admission_environment,
)
from .admission import (
	gate_admission as gate_admission,
)
from .admission import (
	running_in_ci as running_in_ci,
)
from .contracts import (
	DEFAULT_COMMAND_TIMEOUT_SECONDS as DEFAULT_COMMAND_TIMEOUT_SECONDS,
)
from .contracts import (
	DEFAULT_GATE_TIMEOUT_SECONDS as DEFAULT_GATE_TIMEOUT_SECONDS,
)
from .contracts import (
	CheckResult as CheckResult,
)
from .contracts import (
	Finding as Finding,
)
from .contracts import (
	Manifest as Manifest,
)
from .contracts import PythonComponent as _ManifestPythonComponent
from .contracts import (
	Status as Status,
)
from .contracts import (
	ValidationError as ValidationError,
)
from .contracts import (
	Verdict as Verdict,
)
from .contracts import (
	WebComponent as WebComponent,
)
from .contracts import (
	load_manifest as load_manifest,
)
from .contracts import (
	redact as redact,
)
from .distribution import DistributionError as DistributionError
from .edit_protocol import EditError as EditError
from .integrity import (
	documentation_results as documentation_results,
)
from .integrity import (
	git_integrity_results as git_integrity_results,
)
from .integrity import (
	workflow_result as workflow_result,
)
from .launcher import (
	PreparedEnvironment as PreparedEnvironment,
)
from .launcher import (
	prepare as prepare,
)
from .launcher import (
	prepare_bootstrap as prepare_bootstrap,
)
from .lessons import lessons_result as lessons_result
from .native_cache import (
	NativeToolCache as NativeToolCache,
)
from .native_cache import (
	NativeToolCacheError as NativeToolCacheError,
)
from .native_cache import (
	NativeToolCacheIdentity as NativeToolCacheIdentity,
)
from .native_cache import (
	NativeToolCacheSelection as NativeToolCacheSelection,
)
from .native_cache import (
	native_tool_source_workspace as native_tool_source_workspace,
)
from .operation import (
	GateOperation as GateOperation,
)
from .operation import (
	OperationTimeoutError as OperationTimeoutError,
)
from .operation import (
	current_operation as current_operation,
)
from .operation import (
	observe_check as observe_check,
)
from .operation import (
	run_probe as run_probe,
)
from .reporting import render as render
from .runner_dependencies import (
	_dependency_command as _dependency_command,
)
from .runner_dependencies import (
	_dependency_configuration as _dependency_configuration,
)
from .runner_dependencies import (
	_dependency_hygiene_result as _dependency_hygiene_result,
)
from .runner_dependencies import (
	_dependency_hygiene_result_impl as _dependency_hygiene_result_impl,
)
from .runner_dependencies import (
	_dependency_inline_suppressions as _dependency_inline_suppressions,
)
from .runner_dependencies import (
	_dependency_issue as _dependency_issue,
)
from .runner_dependencies import (
	_dependency_requirement_groups as _dependency_requirement_groups,
)
from .runner_dependencies import (
	_read_dependency_report as _read_dependency_report,
)
from .runner_domain import (
	_component_check_index as _component_check_index,
)
from .runner_domain import (
	_manifest_error as _manifest_error,
)
from .runner_domain import (
	_planned_check_scopes as _planned_check_scopes,
)
from .runner_domain import (
	_planned_python_check_scopes as _planned_python_check_scopes,
)
from .runner_domain import (
	_progress as _progress,
)
from .runner_domain import (
	_python_component_check_scope as _python_component_check_scope,
)
from .runner_domain import (
	decode_subprocess_output as decode_subprocess_output,
)
from .runner_domain import (
	emit as emit,
)
from .runner_domain import (
	load_components as load_components,
)
from .runner_domain import (
	relative_path as relative_path,
)
from .runner_domain import (
	repository_root as repository_root,
)
from .runner_process import (
	_bounded_output as _bounded_output,
)
from .runner_process import (
	_ci_history_refs as _ci_history_refs,
)
from .runner_process import (
	_configure_operation_budget as _configure_operation_budget,
)
from .runner_process import (
	_error_result as _error_result,
)
from .runner_process import (
	_remaining as _remaining,
)
from .runner_process import (
	_ruff_environment as _ruff_environment,
)
from .runner_process import (
	_run_bounded_subprocess as _run_bounded_subprocess,
)
from .runner_process import (
	_run_legacy_subprocess as _run_legacy_subprocess,
)
from .runner_process import (
	_run_python_snapshot_checks as _run_python_snapshot_checks,
)
from .runner_process import (
	_runtime_python as _runtime_python,
)
from .runner_process import (
	_safe_environment as _safe_environment,
)
from .runner_process import (
	_snapshot_candidate_identity as _snapshot_candidate_identity,
)
from .runner_process import (
	_tool_command as _tool_command,
)
from .runner_process import (
	required_documents_result as required_documents_result,
)
from .runner_process import (
	run as run,
)
from .runner_process import (
	temporary_directory as temporary_directory,
)
from .runner_process import (
	validate as validate,
)
from .runner_python_checks import (
	_append_missing_component_check as _append_missing_component_check,
)
from .runner_python_checks import (
	_append_missing_test_path_checks as _append_missing_test_path_checks,
)
from .runner_python_checks import (
	_append_not_applicable_python_checks as _append_not_applicable_python_checks,
)
from .runner_python_checks import (
	_append_unavailable_runtime_checks as _append_unavailable_runtime_checks,
)
from .runner_python_checks import (
	_run_python_checks as _run_python_checks,
)
from .runner_python_checks import (
	_run_python_component_checks as _run_python_component_checks,
)
from .runner_python_checks import (
	_run_ready_python_component_checks as _run_ready_python_component_checks,
)
from .runner_python_checks import (
	format_paths as format_paths,
)
from .runner_python_tools import (
	_command_error as _command_error,
)
from .runner_python_tools import (
	_command_results as _command_results,
)
from .runner_python_tools import (
	_component_commands as _component_commands,
)
from .runner_python_tools import (
	_execute_commands as _execute_commands,
)
from .runner_python_tools import (
	_mark_component_checks_not_run as _mark_component_checks_not_run,
)
from .runner_python_tools import (
	_native_cache_identity as _native_cache_identity,
)
from .runner_python_tools import (
	_native_cache_repository_identity as _native_cache_repository_identity,
)
from .runner_python_tools import (
	_native_tool_configuration as _native_tool_configuration,
)
from .runner_python_tools import (
	_normalized_native_tool_options as _normalized_native_tool_options,
)
from .runner_python_tools import (
	_record_command_result as _record_command_result,
)
from .runner_snapshot import (
	_append_python_snapshot_checks as _append_python_snapshot_checks,
)
from .runner_snapshot import (
	_append_snapshot_baseline_checks as _append_snapshot_baseline_checks,
)
from .runner_snapshot import (
	_append_snapshot_secret_checks as _append_snapshot_secret_checks,
)
from .runner_snapshot import (
	_check_snapshot as _check_snapshot,
)
from .runner_snapshot import (
	_execute_snapshot_operation as _execute_snapshot_operation,
)
from .runner_snapshot import (
	_load_snapshot_manifest as _load_snapshot_manifest,
)
from .runner_snapshot import (
	_prepare_snapshot_environment as _prepare_snapshot_environment,
)
from .runner_snapshot import (
	_record_admission_failure as _record_admission_failure,
)
from .runner_snapshot import (
	_record_snapshot_failure as _record_snapshot_failure,
)
from .runner_snapshot import (
	_record_workspace_failure as _record_workspace_failure,
)
from .runner_snapshot import (
	_run_candidate_checks as _run_candidate_checks,
)
from .runner_snapshot import (
	_run_snapshot as _run_snapshot,
)
from .runner_snapshot import (
	_run_snapshot_operation as _run_snapshot_operation,
)
from .runner_snapshot import (
	_snapshot_failure_stage as _snapshot_failure_stage,
)
from .runner_snapshot import (
	audit as audit,
)
from .runner_snapshot import (
	bootstrap_audit as bootstrap_audit,
)
from .runner_snapshot import (
	bootstrap_check as bootstrap_check,
)
from .runner_snapshot import (
	bootstrap_product_audit as bootstrap_product_audit,
)
from .runner_snapshot import (
	check as check,
)
from .runner_web import (
	_biome_asset_paths as _biome_asset_paths,
)
from .runner_web import (
	_biome_command as _biome_command,
)
from .runner_web import (
	_biome_component_commands as _biome_component_commands,
)
from .runner_web import (
	_biome_inventory as _biome_inventory,
)
from .runner_web import (
	_biome_unchecked_results as _biome_unchecked_results,
)
from .runner_web import (
	_fixed_check_result as _fixed_check_result,
)
from .runner_web import (
	_is_excluded as _is_excluded,
)
from .runner_web import (
	_is_link as _is_link,
)
from .runner_web import (
	_run_web_checks as _run_web_checks,
)
from .runner_web import (
	_unsafe_web_path as _unsafe_web_path,
)
from .runner_web import (
	_web_assets as _web_assets,
)
from .runner_web import (
	_web_budget_check as _web_budget_check,
)
from .runner_web import (
	_web_budget_result as _web_budget_result,
)
from .runner_web import (
	_web_check_results as _web_check_results,
)
from .runner_web import (
	web_budget_results as web_budget_results,
)
from .runtime import (
	RuntimeIdentity as RuntimeIdentity,
)
from .runtime import (
	RuntimeUnavailable as RuntimeUnavailable,
)
from .runtime import (
	runtime_fingerprint as runtime_fingerprint,
)
from .secrets import (
	secret_audit_result as secret_audit_result,
)
from .secrets import (
	secret_candidate_result as secret_candidate_result,
)
from .secrets import (
	secret_history_result as secret_history_result,
)
from .snapshot import (
	CandidateSnapshot as CandidateSnapshot,
)
from .snapshot import (
	SnapshotError as SnapshotError,
)
from .snapshot import (
	candidate_snapshot as candidate_snapshot,
)
from .snapshot import (
	remove_snapshot as remove_snapshot,
)
from .temp_workspace import TemporaryWorkspaceError as _TemporaryWorkspaceError
from .temp_workspace import temporary_workspace as _temporary_workspace

ManifestPythonComponent = _ManifestPythonComponent
TemporaryWorkspaceError = _TemporaryWorkspaceError
temporary_workspace = _temporary_workspace

_LOGGER = logging.getLogger(__name__)
MANIFEST_NAME = "quality-gate.toml"
POLICY_DIR = Path(__file__).resolve().parent / "policy"
EXIT_UNCHECKED = 2
MAX_COMMAND_OUTPUT_CHARS = 16_384
MAX_COMMAND_OUTPUT_BYTES = MAX_COMMAND_OUTPUT_CHARS * 4 + 1
RUFF_THREADS_ENV = "QUALITY_GATE_RUFF_THREADS"
DEFAULT_RUFF_THREADS = 4
MAX_RUFF_THREADS = 64
MAX_DEPTRY_REPORT_BYTES = 1024 * 1024
MAX_DEPTRY_SOURCE_BYTES = 8 * 1024 * 1024
MAX_DEPTRY_TEXT_CHARS = 1000
PROCESS_CLEANUP_TIMEOUT_SECONDS = 1.0
CHECK_ID_COMPONENT_PARTS = 3
BIOME_POLICY = "biome.toml"
BIOME_CONFIG = "biome.json"
BIOME_MAX_FILE_BYTES = 5 * 1024 * 1024
_DEPTRY_MODULE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$")
_DEPTRY_INLINE_IGNORE = re.compile(r"#\s*deptry:\s*ignore(?:\s*\[[A-Z0-9,\s]+\])?(?=\s|$)")
_DEPTRY_SUPPRESSION_KEYS = {
	"exclude",
	"extend_exclude",
	"ignore",
	"ignore_notebooks",
	"per_rule_ignores",
}


class OutputReadError(RuntimeError):
	"""A subprocess output reader could not finish safely."""


class QualityGateError(RuntimeError):
	"""A configuration or quality check prevents completion."""

	exit_code = 1

	def __init__(
		self,
		message: str,
		*,
		check_id: str = "runtime.command",
		exit_code: int = 1,
		recovery_action: str = "restore verification and retry the quality gate",
	) -> None:
		super().__init__(redact(message))
		self.check_id = check_id
		self.exit_code = exit_code
		self.recovery_action = recovery_action


_FIXED_CHECK_SCOPES = {
	"manifest.documents": "staged required documents",
	"secrets.candidate": "staged candidate tree",
	"secrets.history": "requested base-to-head Git range",
	"secrets.audit": "all reachable Git history",
}


class _OperationResults(list[CheckResult]):
	"""Retain each observed check immediately so a later timeout cannot erase it."""

	def __init__(self, root: Path) -> None:
		super().__init__()
		self.root = root
		self.manifest: Manifest | None = None

	def append(self, result: CheckResult) -> None:
		operation = current_operation()
		if operation is not None:
			operation.record_unmeasured_result(result, scope=self.scope_for(result.check_id))
		super().append(result)

	def extend(self, results: Iterable[CheckResult]) -> None:
		for result in results:
			self.append(result)

	def append_not_run(self, check_id: str, scope: str, reason: str) -> None:
		"""Retain an expected check that a completed prerequisite prevented."""
		operation = current_operation()
		if operation is not None:
			operation.note_not_run(check_id, scope, reason)
		self.append(
			CheckResult(
				check_id,
				Status.UNCHECKED,
				reason,
				recovery_action="complete the prerequisite and rerun the affected checks",
			)
		)

	def scope_for(self, check_id: str) -> str:
		manifest = self.manifest
		parts = check_id.split(".")
		python_index = _component_check_index(parts, "python")
		if (
			manifest is not None
			and python_index is not None
			and python_index < len(manifest.python)
		):
			return _python_component_check_scope(self.root, manifest.python[python_index], parts[2])
		web_index = _component_check_index(parts, "web")
		if manifest is not None and web_index is not None and web_index < len(manifest.web):
			return manifest.web[web_index].root
		if check_id in _FIXED_CHECK_SCOPES:
			return _FIXED_CHECK_SCOPES[check_id]
		if check_id.startswith("repository.git."):
			return "staged repository tree and Git index"
		if check_id.startswith("repository.documentation."):
			return "staged repository documentation"
		if check_id.startswith("web."):
			return "declared web component assets"
		if check_id.startswith("python."):
			return "declared Python components"
		if check_id.startswith("runtime."):
			return "selected policy and verification runtimes"
		return "staged candidate"


@dataclass(frozen=True)
class PythonComponent:
	path: Path
	tests: Path | None
	requirements: Path | None
	typecheck: bool
	test_paths: tuple[Path, ...] = ()
	timeout_seconds: int = 300
	missing_test_paths: tuple[Path, ...] = ()
	path_exists: bool = True
	dependency_inputs: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class BiomeBinary:
	version: str
	path: str
	sha256: str


_PYTHON_MODULE_COMMAND_LENGTH = 3


@dataclass(frozen=True, slots=True)
class _PythonCheckContext:
	"""Inputs shared by every outcome of one candidate component's checks."""

	root: Path
	manifest: Manifest
	component: PythonComponent
	prepared: PreparedEnvironment
	component_index: int
	report_path: Path
	deadline: float

	def dependency_result(self, python_executable: Path | None = None) -> CheckResult:
		return _dependency_hygiene_result(
			self.root,
			self.manifest,
			self.component,
			self.prepared,
			self.component_index,
			python_executable,
			self.report_path,
			self.deadline,
		)
