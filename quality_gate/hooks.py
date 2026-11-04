"""Stable native-Git hook protocol shared by local wrappers and CI."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from .admission import AdmissionError, queue_timeout_seconds, running_in_ci
from .contracts import Status, ValidationError, load_manifest, parse_manifest
from .distribution import DistributionError, PolicyCache, verify_release
from .edit_process import run_process
from .edit_protocol import EditError
from .operation import OPERATION_CLEANUP_SECONDS
from .secrets import secret_push_commit_result

_LOGGER = logging.getLogger(__name__)
PUSH_FIELD_COUNT = 4
OBJECT_ID_LENGTH = 40
OBJECT_ID = re.compile(r"^[0-9a-fA-F]{40}$")
REF_NAME = re.compile(r"^refs/[A-Za-z0-9._/-]+$")
REMOTE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
MAX_PUSH_PAYLOAD_LENGTH = 1024 * 1024
MAX_PUSH_LINE_LENGTH = 4096
MAX_PUSH_LINES = 1000
MANIFEST_NAME = "quality-gate.toml"


class HookInputError(ValueError):
	"""Git supplied an invalid hook input record."""


@dataclass(frozen=True, slots=True)
class PushRef:
	"""The local and remote ref names from one pre-push input line."""

	local_ref: str
	remote_ref: str
	local_oid: str = ""
	remote_oid: str = ""


def read_push_refs(payload: str) -> tuple[PushRef, ...]:
	"""Parse Git's four-column pre-push protocol without inspecting object data."""
	if len(payload.encode("utf-8")) > MAX_PUSH_PAYLOAD_LENGTH:
		raise HookInputError("pre-push input exceeds the size limit")
	refs: list[PushRef] = []
	for line_number, line in enumerate(payload.splitlines(), start=1):
		if not line.strip():
			continue
		if line_number > MAX_PUSH_LINES or len(line) > MAX_PUSH_LINE_LENGTH:
			raise HookInputError("pre-push input exceeds the line limit")
		fields = line.split()
		if len(fields) != PUSH_FIELD_COUNT:
			raise HookInputError(f"pre-push input line {line_number} must contain four fields")
		local_ref, local_sha, remote_ref, remote_sha = fields
		if (
			len(local_sha) != OBJECT_ID_LENGTH
			or len(remote_sha) != OBJECT_ID_LENGTH
			or not OBJECT_ID.fullmatch(local_sha)
			or not OBJECT_ID.fullmatch(remote_sha)
		):
			raise HookInputError(f"pre-push input line {line_number} contains an invalid object id")
		if local_ref != "(delete)" and not REF_NAME.fullmatch(local_ref):
			raise HookInputError(f"pre-push input line {line_number} contains an invalid ref")
		if not REF_NAME.fullmatch(remote_ref):
			raise HookInputError(f"pre-push input line {line_number} contains an invalid ref")
		refs.append(PushRef(local_ref, remote_ref, local_sha.lower(), remote_sha.lower()))
	return tuple(refs)


def default_branch_names(remote: str, symbolic_head: str | None) -> frozenset[str] | None:
	"""Return the known default branch name, or no name when Git cannot prove it."""
	if symbolic_head and REMOTE_NAME.fullmatch(remote):
		prefix = f"refs/remotes/{remote}/"
		if symbolic_head.startswith(prefix):
			name = symbolic_head.removeprefix(prefix)
			if name and REF_NAME.fullmatch(f"refs/heads/{name}"):
				return frozenset({name})
	return None


def protected_push_refs(
	refs: Iterable[PushRef], default_branches: Iterable[str]
) -> tuple[PushRef, ...]:
	"""Select every update or deletion targeting a configured default branch."""
	protected = frozenset(default_branches)
	return tuple(
		item
		for item in refs
		if item.remote_ref.startswith("refs/heads/")
		and item.remote_ref.removeprefix("refs/heads/") in protected
	)


def remote_head(repository: Path, remote: str) -> str | None:
	"""Read an already-known remote HEAD without network access or mutation."""
	if not REMOTE_NAME.fullmatch(remote):
		return None
	try:
		code, output, _ = run_process(
			["git", "symbolic-ref", "--quiet", f"refs/remotes/{remote}/HEAD"],
			root=repository,
			environment=os.environ.copy(),
			deadline=time.monotonic() + 15,
			own_tree=True,
		)
		value = output.decode("utf-8").strip()
	except (OSError, UnicodeError, EditError):
		return None
	_LOGGER.debug("read remote HEAD for native push policy: remote=%s", remote)
	return value if code == 0 else None


def pre_push(repository: Path, remote: str, payload: str) -> int:
	"""Enforce the local default-branch push policy and return a hook exit code."""
	try:
		refs = read_push_refs(payload)
	except HookInputError as error:
		sys.stderr.write(f"PUSH UNCHECKED: {error}. Retry the push with Git.\n")
		return 2
	branches = default_branch_names(remote, remote_head(repository, remote))
	if branches is None:
		sys.stderr.write(
			"PUSH UNCHECKED: the remote default branch is unknown. "
			"Fetch the remote HEAD or configure it with `git remote set-head`, then retry.\n"
		)
		return 2
	blocked = protected_push_refs(refs, branches)
	if blocked:
		names = ", ".join(sorted({item.remote_ref for item in blocked}))
		sys.stderr.write(
			"PUSH BLOCKED: default branch update or deletion is not allowed "
			f"({names}). Push a feature branch and open a pull request.\n",
		)
		return 1
	zero = "0" * OBJECT_ID_LENGTH
	if any(
		(item.local_ref == "(delete)") != (item.local_oid == zero)
		or (item.local_ref == "(delete)" and item.remote_oid == zero)
		for item in refs
	):
		sys.stderr.write(
			"PUSH UNCHECKED: inconsistent ref and object IDs. Retry the push with Git.\n"
		)
		return 2
	updates = [item for item in refs if item.local_ref != "(delete)"]
	if not updates:
		return 0
	try:
		return _scan_push_updates(repository, updates, zero)
	except (OSError, UnicodeError, ValueError, DistributionError, EditError):
		sys.stderr.write(
			"PUSH UNCHECKED: outgoing history or pinned scanner is unavailable. "
			"Fetch or repair the required history, restore the pinned release, and retry.\n"
		)
		return 2


def _scan_push_updates(repository: Path, updates: list[PushRef], zero: str) -> int:
	manifest = load_manifest(repository)
	deadline = time.monotonic() + manifest.repository.command_timeout_seconds
	commits = _outgoing_commits(repository, updates, zero, deadline)
	if not commits:
		return 0
	policy_root = PolicyCache().releases / manifest.policy_release
	release = verify_release(policy_root)
	if release.version != manifest.policy_release:
		raise ValueError("release mismatch")
	prepared = SimpleNamespace(policy_root=policy_root, release_manifest=release)
	for commit in sorted(commits):
		result = secret_push_commit_result(
			repository, manifest, prepared, commit, timeout=deadline - time.monotonic()
		)
		if result.status is Status.FAILED:
			sys.stderr.write("PUSH BLOCKED: credentials detected in outgoing history.\n")
			for finding in result.findings:
				sys.stderr.write(f"{finding.path}: {finding.message}\n")
			return 1
		if result.status not in {Status.PASSED, Status.WAIVED}:
			raise ValueError("scanner unavailable")
	return 0


def _outgoing_commits(
	repository: Path, updates: list[PushRef], zero: str, deadline: float
) -> set[str]:
	environment = os.environ.copy()
	environment.update({"GIT_NO_LAZY_FETCH": "1", "GIT_NO_REPLACE_OBJECTS": "1"})

	def git(*args: str) -> str:
		code, output, _ = run_process(
			["git", *args],
			root=repository,
			environment=environment,
			deadline=deadline,
			own_tree=True,
		)
		if code:
			raise ValueError("Git history unavailable")
		return output.decode("ascii").strip()

	if git("rev-parse", "--is-shallow-repository") != "false":
		raise ValueError("shallow history")
	config_code, config_output, _ = run_process(
		["git", "config", "--get-regexp", r"^(remote\..*\.promisor|extensions\.partialclone)$"],
		root=repository,
		environment=environment,
		deadline=deadline,
		own_tree=True,
	)
	if config_code not in {0, 1} or config_output:
		raise ValueError("partial clone history")
	commits: set[str] = set()
	for item in updates:
		local = git("rev-parse", "--verify", f"{item.local_oid}^{{commit}}")
		negative = (
			[]
			if item.remote_oid == zero
			else ["^" + git("rev-parse", "--verify", f"{item.remote_oid}^{{commit}}")]
		)
		git("rev-list", "--objects", "--no-object-names", "--missing=error", local, *negative)
		commits.update(git("rev-list", local, *negative).splitlines())
	return commits


def commit_gate_timeout_seconds(
	staged_manifest: bytes,
	*,
	environment: Mapping[str, str] | None = None,
) -> int:
	"""Return the native hook's outer deadline from its staged schema 2 manifest."""
	try:
		manifest = parse_manifest(staged_manifest.decode("utf-8"))
	except UnicodeError as error:
		raise ValidationError("manifest", "cannot read valid UTF-8 TOML") from error
	return _commit_gate_timeout(manifest.repository.gate_timeout_seconds, environment)


def _commit_gate_timeout(
	gate_timeout_seconds: int, environment: Mapping[str, str] | None = None
) -> int:
	if (
		isinstance(gate_timeout_seconds, bool)
		or not isinstance(gate_timeout_seconds, int)
		or gate_timeout_seconds <= 0
	):
		raise ValueError("gate timeout must be a positive integer")
	if running_in_ci(environment):
		queue_budget_seconds = 0
	else:
		queue_budget_seconds = queue_timeout_seconds(environment)
	return gate_timeout_seconds + queue_budget_seconds + OPERATION_CLEANUP_SECONDS


def run_commit_gate(
	repository: Path,
	runtime_root: Path,
	*,
	timeout_seconds: int | None = None,
	staged_manifest: bytes | None = None,
) -> int:
	"""Run the selected cached runtime for a staged commit and return its exit code.

	Callers with the captured index manifest pass it as ``staged_manifest`` so the
	outer deadline matches the candidate that the CLI will check.
	"""
	repository = repository.resolve()
	if staged_manifest is None and not (repository / MANIFEST_NAME).is_file():
		return 0
	python_candidates = (
		runtime_root / ".venv" / "Scripts" / "python.exe",
		runtime_root / ".venv" / "bin" / "python",
	)
	python_executable = next((path for path in python_candidates if path.is_file()), None)
	if python_executable is None:
		sys.stderr.write(
			"QUALITY GATE UNCHECKED: the selected runtime is unavailable. "
			f"Restore {runtime_root} and retry the commit.\n"
		)
		return 2
	if staged_manifest is not None and timeout_seconds is not None:
		raise ValueError("provide either a staged manifest or a gate timeout, not both")
	environment = os.environ.copy()
	try:
		if staged_manifest is not None:
			outer_timeout = commit_gate_timeout_seconds(staged_manifest, environment=environment)
		else:
			gate_timeout = (
				load_manifest(repository).repository.gate_timeout_seconds
				if timeout_seconds is None
				else timeout_seconds
			)
			outer_timeout = _commit_gate_timeout(gate_timeout, environment)
	except AdmissionError as error:
		sys.stderr.write(
			f"QUALITY GATE UNCHECKED: {error}. Correct the local queue setting and retry.\n"
		)
		return 2
	existing_pythonpath = environment.get("PYTHONPATH")
	environment["PYTHONPATH"] = (
		str(runtime_root)
		if not existing_pythonpath
		else f"{runtime_root}{os.pathsep}{existing_pythonpath}"
	)
	try:
		_LOGGER.debug("run native commit gate: repository=%s runtime=%s", repository, runtime_root)
		result = subprocess.run(
			[
				str(python_executable),
				"-m",
				"quality_gate.cli",
				"--root",
				str(repository),
				"check",
			],
			cwd=runtime_root,
			env=environment,
			check=False,
			timeout=outer_timeout,
		)
	except subprocess.TimeoutExpired:
		_LOGGER.warning("native commit gate timed out", exc_info=True)
		sys.stderr.write(
			"QUALITY GATE UNCHECKED: verification timed out after "
			f"{outer_timeout} seconds. Inspect the selected runtime and retry the commit.\n"
		)
		return 2
	return result.returncode
