"""Stable native-Git hook protocol shared by local wrappers and CI."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from quality_gate.contracts import load_manifest

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
		refs.append(PushRef(local_ref, remote_ref))
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
	result = subprocess.run(
		["git", "symbolic-ref", "--quiet", f"refs/remotes/{remote}/HEAD"],
		cwd=repository,
		capture_output=True,
		text=True,
		encoding="utf-8",
		check=False,
	)
	_LOGGER.debug("read remote HEAD for native push policy: remote=%s", remote)
	return result.stdout.strip() if result.returncode == 0 else None


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
	if not blocked:
		return 0
	names = ", ".join(sorted({item.remote_ref for item in blocked}))
	sys.stderr.write(
		"PUSH BLOCKED: default branch update or deletion is not allowed "
		f"({names}). Push a feature branch and open a pull request.\n",
	)
	return 1


def run_commit_gate(
	repository: Path,
	runtime_root: Path,
	*,
	timeout_seconds: int | None = None,
) -> int:
	"""Run the selected cached runtime for a staged commit and return its exit code."""
	repository = repository.resolve()
	if not (repository / MANIFEST_NAME).is_file():
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
	if timeout_seconds is None:
		timeout_seconds = load_manifest(repository).repository.gate_timeout_seconds
	environment = os.environ.copy()
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
			timeout=timeout_seconds,
		)
	except subprocess.TimeoutExpired:
		_LOGGER.warning("native commit gate timed out", exc_info=True)
		sys.stderr.write(
			"QUALITY GATE UNCHECKED: verification timed out after "
			f"{timeout_seconds} seconds. Inspect the selected runtime and retry the commit.\n"
		)
		return 2
	return result.returncode
