"""Quality Gate runner: snapshot operations."""

from __future__ import annotations

from typing import Literal

from . import runner as _runner


def _load_snapshot_manifest(
	root: _runner.Path, results: _runner._OperationResults
) -> _runner.Manifest | None:
	try:
		operation = _runner.current_operation()
		if operation is None:
			manifest = _runner.load_manifest(root)
		else:
			with operation.stage("manifest.contract", "staged quality-gate.toml", check=True):
				manifest = _runner.load_manifest(root)
				operation.configure_budget(
					manifest.repository.gate_timeout_seconds,
					manifest.repository.command_timeout_seconds,
				)
	except _runner.ValidationError as error:
		operation = _runner.current_operation()
		if operation is not None:
			operation.mark_incomplete(
				"manifest.contract",
				"invalid staged manifest prevented the remaining checks",
			)
		results.append(_runner._error_result(_runner._manifest_error(error)))
		return None
	results.manifest = manifest
	return manifest


def _append_snapshot_baseline_checks(
	root: _runner.Path,
	manifest: _runner.Manifest,
	results: _runner._OperationResults,
	*,
	repository_root: _runner.Path | None,
	index_file: _runner.Path | None,
	mode: Literal["check", "audit", "product"],
) -> None:
	results.append(
		_runner.observe_check(
			"manifest.documents",
			"staged required documents",
			lambda: _runner.required_documents_result(root, manifest),
		)
	)
	results.extend(
		[
			*_runner.git_integrity_results(
				root,
				manifest,
				repository=repository_root or root,
				index_file=index_file,
			),
			_runner.observe_check(
				"repository.workflow",
				"staged GitHub workflow files",
				lambda: _runner.workflow_result(
					root,
					manifest,
					repository_root=repository_root or root,
				),
			),
			*_runner.documentation_results(root, manifest),
		]
	)
	results.append(
		_runner.observe_check(
			"lessons.learning",
			"staged lessons collection",
			lambda: _runner.lessons_result(root, is_complete_required=mode == "audit"),
		)
	)
	results.extend(_runner.web_budget_results(root, manifest))


def _prepare_snapshot_environment(
	root: _runner.Path,
	manifest: _runner.Manifest,
	results: _runner._OperationResults,
	*,
	repository_root: _runner.Path | None,
	mode: Literal["check", "audit", "product"],
	policy_release: str | None,
) -> tuple[list[_runner.PythonComponent], _runner.PreparedEnvironment] | None:
	try:
		components = _runner.load_components(root)
		operation = _runner.current_operation()
		if operation is not None:
			operation.plan_checks(_runner._planned_check_scopes(root, manifest, components, mode))

		def prepare_environment() -> _runner.PreparedEnvironment:
			if policy_release is not None:
				return _runner.prepare_bootstrap(
					root,
					policy_release=policy_release,
					repository_root=repository_root or root,
				)
			return _runner.prepare(root, repository_root=repository_root or root)

		if operation is None:
			prepared = prepare_environment()
		else:
			with operation.stage(
				"policy.cache-validation",
				"selected immutable policy release and component runtime caches",
			):
				prepared = prepare_environment()
		operation = _runner.current_operation()
		if operation is not None:
			operation.plan_checks(
				_runner._planned_check_scopes(root, manifest, components, mode, prepared=prepared)
			)
		return components, prepared
	except _runner.QualityGateError as error:
		operation = _runner.current_operation()
		if operation is not None:
			operation.mark_incomplete(
				"policy.cache-validation",
				"policy or runtime preparation prevented the remaining checks",
			)
		results.append(_runner._error_result(error))
	except (_runner.DistributionError, _runner.RuntimeUnavailable, OSError) as error:
		operation = _runner.current_operation()
		if operation is not None:
			operation.mark_incomplete(
				"policy.cache-validation",
				"policy or runtime preparation prevented the remaining checks",
			)
		results.append(
			_runner._error_result(
				_runner.QualityGateError(
					str(error),
					check_id="runtime.policy",
					exit_code=_runner.EXIT_UNCHECKED,
					recovery_action="run sync and setup, then retry the quality gate",
				)
			)
		)
	return None


def _append_snapshot_secret_checks(
	root: _runner.Path,
	manifest: _runner.Manifest,
	prepared: _runner.PreparedEnvironment,
	results: _runner._OperationResults,
	*,
	repository_root: _runner.Path | None,
	base: str | None,
	head: str | None,
	mode: Literal["check", "audit", "product"],
) -> None:
	operation = _runner.current_operation()
	if operation is None:
		history_base, history_head = _runner._ci_history_refs(base, head)
	else:
		with operation.stage("history.refs", "requested CI base and candidate references"):
			history_base, history_head = _runner._ci_history_refs(base, head)
	results.append(
		_runner.observe_check(
			"secrets.candidate",
			"staged candidate tree",
			lambda: _runner.secret_candidate_result(root, manifest, prepared),
			process_required=True,
		)
	)
	if mode in ("audit", "product"):
		results.append(
			_runner.observe_check(
				"secrets.audit",
				"all reachable Git history",
				lambda: _runner.secret_audit_result(repository_root or root, manifest, prepared),
				process_required=True,
			)
		)
		return
	results.append(
		_runner.observe_check(
			"secrets.history",
			"requested base-to-head Git range",
			lambda: _runner.secret_history_result(
				repository_root or root,
				manifest,
				prepared,
				base=history_base,
				head=history_head,
			),
			process_required=True,
		)
	)


def _append_python_snapshot_checks(
	root: _runner.Path,
	manifest: _runner.Manifest,
	components: list[_runner.PythonComponent],
	prepared: _runner.PreparedEnvironment,
	results: _runner._OperationResults,
	deadline: float,
) -> None:
	if not components:
		results.append(
			_runner.CheckResult(
				check_id="python.components",
				status=_runner.Status.NOT_APPLICABLE,
				summary="no Python component is declared",
				recovery_action="declare a Python component if Python checks apply",
			)
		)
		return
	try:
		run_results = _runner._run_python_snapshot_checks(
			root, manifest, components, prepared, deadline=deadline
		)
	except _runner.QualityGateError as error:
		operation = _runner.current_operation()
		if operation is not None:
			operation.mark_incomplete(
				"python.checks",
				"Python verification preparation prevented the remaining component checks",
			)
		results.append(_runner._error_result(error))
		return
	except (_runner.DistributionError, _runner.RuntimeUnavailable, OSError) as error:
		operation = _runner.current_operation()
		if operation is not None:
			operation.mark_incomplete(
				"python.checks",
				"Python verification preparation prevented the remaining component checks",
			)
		results.append(
			_runner._error_result(
				_runner.QualityGateError(
					str(error),
					check_id="runtime.policy",
					exit_code=_runner.EXIT_UNCHECKED,
					recovery_action="run sync and setup, then retry the quality gate",
				)
			)
		)
		return
	results.extend(run_results)


def _check_snapshot(
	actual_root: _runner.Path,
	*,
	verbose: bool = False,
	repository_root: _runner.Path | None = None,
	index_file: _runner.Path | None = None,
	base: str | None = None,
	head: str | None = None,
	mode: Literal["check", "audit", "product"] = "check",
	policy_release: str | None = None,
) -> _runner.Verdict:
	actual_root = actual_root.resolve()
	results = _runner._OperationResults(actual_root)
	manifest = _runner._load_snapshot_manifest(actual_root, results)
	if manifest is None:
		return _runner.Verdict(tuple(results))
	_runner._append_snapshot_baseline_checks(
		actual_root,
		manifest,
		results,
		repository_root=repository_root,
		index_file=index_file,
		mode=mode,
	)
	prepared_snapshot = _runner._prepare_snapshot_environment(
		actual_root,
		manifest,
		results,
		repository_root=repository_root,
		mode=mode,
		policy_release=policy_release,
	)
	if prepared_snapshot is None:
		return _runner.Verdict(tuple(results))
	components, prepared = prepared_snapshot
	_runner._append_snapshot_secret_checks(
		actual_root,
		manifest,
		prepared,
		results,
		repository_root=repository_root,
		base=base,
		head=head,
		mode=mode,
	)
	_runner._progress("repository checks completed; 1 stage remaining.")
	operation = _runner.current_operation()
	deadline = (
		operation.deadline
		if operation is not None
		else _runner.time.monotonic() + manifest.repository.gate_timeout_seconds
	)
	results.extend(
		_runner._web_check_results(actual_root, manifest, prepared, results, deadline=deadline)
	)
	_runner._append_python_snapshot_checks(
		actual_root, manifest, components, prepared, results, deadline
	)
	_runner._progress("component checks completed; preparing report.")
	return _runner.Verdict(tuple(results))


def check(
	root: _runner.Path | None = None,
	*,
	verbose: bool = False,
	base: str | None = None,
	head: str | None = None,
	profile: bool = False,
) -> _runner.Verdict:
	"""Run the complete quality contract against the exact staged candidate."""
	verdict = _runner._run_snapshot(
		root,
		verbose=verbose,
		base=base,
		head=head,
		mode="check",
		profile=profile,
	)
	_runner.emit(_runner.render(verdict, verbose=verbose, profile=profile))
	return verdict


def _run_candidate_checks(
	operation: _runner.GateOperation,
	snapshot: _runner.CandidateSnapshot,
	snapshot_lifetime: _runner.ExitStack,
	*,
	verbose: bool,
	base: str | None,
	head: str | None,
	mode: Literal["check", "audit", "product"],
	policy_release: str | None,
) -> None:
	with operation.stage(
		"candidate.checks",
		"checks declared by the staged candidate manifest",
	):
		verdict = _runner._check_snapshot(
			snapshot.root,
			verbose=verbose,
			repository_root=operation.root,
			index_file=getattr(snapshot, "repository_index", None),
			base=base,
			head=head,
			mode=mode,
			policy_release=policy_release,
		)
	for result in verdict.results:
		operation.record_unmeasured_result(result)
	with operation.stage(
		"candidate.snapshot-validation",
		"staged Git index remained stable through checks",
	):
		snapshot_lifetime.close()


def _execute_snapshot_operation(
	operation: _runner.GateOperation,
	actual_root: _runner.Path | None,
	*,
	verbose: bool,
	base: str | None,
	head: str | None,
	mode: Literal["check", "audit", "product"],
	policy_release: str | None,
) -> None:
	with operation.stage(
		"repository.root",
		"explicit repository path" if actual_root is not None else "Git repository root",
		process_required=actual_root is None,
	):
		resolved_root = _runner.repository_root(actual_root)
	operation.set_repository_root(resolved_root)
	with operation.stage(
		"manifest.budget",
		"staged repository timeout defaults",
		process_required=True,
	):
		_runner._configure_operation_budget(resolved_root, operation)
	with _runner.ExitStack() as owned_workspaces:
		with operation.stage(
			"temporary.workspace",
			"repository-local or private system temporary workspace",
		):
			owned_workspaces.enter_context(
				_runner.temporary_workspace(
					resolved_root,
					workspace_registered=operation.register_workspace,
					cleanup_owner=operation.handoff_workspace,
					deadline=operation.deadline,
				)
			)
		snapshot_lifetime = _runner.ExitStack()
		owned_workspaces.enter_context(snapshot_lifetime)
		with operation.stage(
			"candidate.snapshot",
			"staged Git index and blob materialization",
		):
			snapshot = snapshot_lifetime.enter_context(
				_runner.candidate_snapshot(
					resolved_root,
					timeout_seconds=operation.command_timeout_seconds,
					deadline=operation.deadline,
					register_cleanup=operation.register_cleanup,
				)
			)
		_runner._progress("preparation completed; 2 stages remaining.")
		operation.candidate = _runner._snapshot_candidate_identity(snapshot)
		operation.scope = "staged Git index; checks declared by the candidate manifest"
		_runner._run_candidate_checks(
			operation,
			snapshot,
			snapshot_lifetime,
			verbose=verbose,
			base=base,
			head=head,
			mode=mode,
			policy_release=policy_release,
		)


def _snapshot_failure_stage(operation: _runner.GateOperation) -> str:
	return (
		"candidate.snapshot-validation"
		if operation.last_completed_stage == "candidate.checks"
		else "candidate.snapshot"
	)


def _record_snapshot_failure(
	operation: _runner.GateOperation,
	error: _runner.SnapshotError,
	mode: Literal["check", "audit", "product"],
) -> None:
	stage = _runner._snapshot_failure_stage(operation)
	if _runner.time.monotonic() >= operation.deadline:
		operation.current_stage = stage
		operation.interrupted_stage = stage
		try:
			operation.remaining()
		except _runner.OperationTimeoutError as timeout:
			raise timeout from error
	operation.mark_incomplete(
		stage,
		"candidate snapshot construction or validation did not complete",
	)
	operation.record_unmeasured_result(
		_runner._error_result(
			_runner.QualityGateError(
				error.message,
				check_id="candidate.snapshot",
				exit_code=_runner.EXIT_UNCHECKED,
				recovery_action=f"restore a stable supported Git index and run {mode} again",
			)
		),
		scope="staged Git index and blob materialization",
	)


def _record_workspace_failure(
	operation: _runner.GateOperation, error: _runner.TemporaryWorkspaceError
) -> None:
	if _runner.time.monotonic() >= operation.deadline:
		operation.current_stage = "temporary.workspace"
		operation.interrupted_stage = "temporary.workspace"
		try:
			operation.remaining()
		except _runner.OperationTimeoutError as timeout:
			raise timeout from error
	operation.mark_incomplete(
		"temporary.workspace",
		"temporary workspace ownership or cleanup transfer did not complete",
	)
	operation.record_unmeasured_result(
		_runner._error_result(
			_runner.QualityGateError(
				str(error),
				check_id="temporary.workspace",
				exit_code=_runner.EXIT_UNCHECKED,
				recovery_action=(
					"restore a writable repository temp directory and retry the quality gate"
				),
			)
		),
		scope="repository temporary workspace",
	)


def _run_snapshot_operation(
	operation: _runner.GateOperation,
	actual_root: _runner.Path | None,
	*,
	verbose: bool,
	base: str | None,
	head: str | None,
	mode: Literal["check", "audit", "product"],
	policy_release: str | None,
) -> None:
	try:
		_runner._execute_snapshot_operation(
			operation,
			actual_root,
			verbose=verbose,
			base=base,
			head=head,
			mode=mode,
			policy_release=policy_release,
		)
	except _runner.SnapshotError as error:
		_runner._record_snapshot_failure(operation, error, mode)
	except _runner.TemporaryWorkspaceError as error:
		_runner._record_workspace_failure(operation, error)
	except _runner.QualityGateError as error:
		operation.mark_incomplete(
			operation.current_stage or "preparation",
			"a required preparation stage prevented the quality checks",
		)
		operation.record_unmeasured_result(_runner._error_result(error))


def _record_admission_failure(
	operation: _runner.GateOperation | None,
	error: _runner.AdmissionError,
	*,
	wait_ms: int | None,
) -> _runner.Verdict:
	quality_error = _runner.QualityGateError(
		str(error),
		check_id="runtime.admission",
		exit_code=_runner.EXIT_UNCHECKED,
		recovery_action=error.recovery_action,
	)
	if operation is not None and operation.finalized:
		operation.mark_incomplete(
			"runtime.admission",
			"machine-local Gate admission could not be released cleanly",
		)
		operation.record_unmeasured_result(
			_runner._error_result(quality_error), scope="machine-local Gate admission"
		)
		return operation.verdict()
	return _runner.Verdict((_runner._error_result(quality_error),), admission_wait_ms=wait_ms)


def _run_snapshot(
	actual_root: _runner.Path | None,
	*,
	verbose: bool = False,
	base: str | None = None,
	head: str | None = None,
	mode: Literal["check", "audit", "product"] = "check",
	policy_release: str | None = None,
	profile: bool = False,
) -> _runner.Verdict:
	"""Run one candidate snapshot and convert snapshot failures to a verdict."""
	_runner._progress("this check may take a long time; wait for completion without polling.")
	operation: _runner.GateOperation | None = None
	local = not _runner.running_in_ci()
	queued_at = _runner.time.monotonic()
	try:
		admission = _runner.gate_admission() if local else _runner.nullcontext()
		with admission:
			wait_ms = int((_runner.time.monotonic() - queued_at) * 1000) if local else None
			operation = _runner.GateOperation(
				actual_root or _runner.Path.cwd(), mode, profile=profile, admission_wait_ms=wait_ms
			)
			with operation:
				_runner._run_snapshot_operation(
					operation,
					actual_root,
					verbose=verbose,
					base=base,
					head=head,
					mode=mode,
					policy_release=policy_release,
				)
	except _runner.AdmissionError as error:
		wait_ms = (
			int((_runner.time.monotonic() - queued_at) * 1000)
			if local and operation is None
			else None
		)
		return _runner._record_admission_failure(operation, error, wait_ms=wait_ms)
	if operation is None:
		raise RuntimeError("Gate operation was not initialized")
	return operation.verdict()


def bootstrap_check(
	root: _runner.Path,
	*,
	policy_release: str,
	base: str | None = None,
	head: str | None = None,
) -> _runner.Verdict:
	"""Run the current checkout against a previous immutable policy release."""
	verdict = _runner._run_snapshot(
		root,
		base=base,
		head=head,
		policy_release=policy_release,
	)
	_runner.emit(_runner.render(verdict, telemetry=True))
	return verdict


def audit(root: _runner.Path | None = None, *, verbose: bool = False) -> _runner.Verdict:
	"""Run every implemented domain and an explicit full-history secret audit."""
	verdict = _runner._run_snapshot(
		root,
		verbose=verbose,
		mode="audit",
	)
	_runner.emit(_runner.render(verdict, verbose=verbose, telemetry=True))
	return verdict


def bootstrap_audit(
	root: _runner.Path,
	*,
	policy_release: str,
	verbose: bool = False,
) -> _runner.Verdict:
	"""Audit one candidate using an explicitly selected immutable policy release."""
	verdict = _runner._run_snapshot(
		root,
		verbose=verbose,
		mode="audit",
		policy_release=policy_release,
	)
	_runner.emit(_runner.render(verdict, verbose=verbose, telemetry=True))
	return verdict


def bootstrap_product_audit(
	root: _runner.Path, *, policy_release: str, verbose: bool = False
) -> _runner.Verdict:
	"""Check an installed product and all reachable history without release readiness."""
	verdict = _runner._run_snapshot(
		root, verbose=verbose, mode="product", policy_release=policy_release
	)
	_runner.emit(_runner.render(verdict, verbose=verbose, telemetry=True))
	return verdict
