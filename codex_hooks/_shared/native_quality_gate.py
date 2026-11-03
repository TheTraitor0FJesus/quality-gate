#!/usr/bin/env python3
"""Native Git bridge for an immutable cached Quality Gate v2 release."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

REF_NAME = re.compile(r"^refs/[A-Za-z0-9._/-]+$")
REMOTE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
OBJECT_ID = re.compile(r"^[0-9a-fA-F]{40}$")
MAX_PUSH_LINES = 1000
MAX_PUSH_LINE_LENGTH = 4096
MAX_PUSH_PAYLOAD_LENGTH = 1024 * 1024
PUSH_FIELD_COUNT = 4
COMMIT_GATE_TIMEOUT_SECONDS = 44 * 60
MANIFEST_NAME = "quality-gate.toml"
RELEASE_VERSION = re.compile(r"^v\d+\.\d+\.\d+$")


@dataclass(frozen=True, slots=True)
class PushRef:
	"""The local and remote refs from one native Git push record."""

	local_ref: str
	remote_ref: str


def _read_push_refs(payload: str) -> tuple[PushRef, ...]:
	if len(payload.encode("utf-8")) > MAX_PUSH_PAYLOAD_LENGTH:
		raise ValueError("pre-push input exceeds the size limit")
	refs: list[PushRef] = []
	for line_number, line in enumerate(payload.splitlines(), start=1):
		if not line.strip():
			continue
		if line_number > MAX_PUSH_LINES or len(line) > MAX_PUSH_LINE_LENGTH:
			raise ValueError("pre-push input exceeds the line limit")
		fields = line.split()
		if len(fields) != PUSH_FIELD_COUNT:
			raise ValueError(f"pre-push input line {line_number} must contain four fields")
		local_ref, local_sha, remote_ref, remote_sha = fields
		if (
			(local_ref != "(delete)" and not REF_NAME.fullmatch(local_ref))
			or not REF_NAME.fullmatch(remote_ref)
			or not OBJECT_ID.fullmatch(local_sha)
			or not OBJECT_ID.fullmatch(remote_sha)
		):
			raise ValueError(f"pre-push input line {line_number} is invalid")
		refs.append(PushRef(local_ref, remote_ref))
	return tuple(refs)


def _remote_head(repository: Path, remote: str) -> str | None:
	result = subprocess.run(
		["git", "symbolic-ref", "--quiet", f"refs/remotes/{remote}/HEAD"],
		cwd=repository,
		capture_output=True,
		text=True,
		encoding="utf-8",
		check=False,
	)
	return result.stdout.strip() if result.returncode == 0 else None


def _diagnostic(message: str) -> None:
	"""Write the CLI diagnostic while preserving its stderr stream."""
	sys.stderr.write(message + "\n")


def run_pre_push(repository: Path, remote: str, payload: str) -> int:
	"""Block known default-branch updates without importing mutable project code."""
	try:
		refs = _read_push_refs(payload)
	except ValueError as error:
		_diagnostic(
			f"PUSH UNCHECKED: {error}. Retry the push with a supported Git client.",
		)
		return 2
	if not REMOTE_NAME.fullmatch(remote):
		_diagnostic(
			"PUSH UNCHECKED: the remote name is invalid; retry with Git's remote name.",
		)
		return 2
	symbolic_head = _remote_head(repository, remote)
	prefix = f"refs/remotes/{remote}/"
	if symbolic_head is None or not symbolic_head.startswith(prefix):
		_diagnostic(
			"PUSH UNCHECKED: the remote default branch is unknown. "
			"Fetch the remote HEAD or configure it with `git remote set-head`, then retry.",
		)
		return 2
	default_branch = symbolic_head.removeprefix(prefix)
	if not default_branch or not REF_NAME.fullmatch(f"refs/heads/{default_branch}"):
		_diagnostic(
			"PUSH UNCHECKED: the remote HEAD is invalid; configure it and retry.",
		)
		return 2
	blocked = tuple(item for item in refs if item.remote_ref == f"refs/heads/{default_branch}")
	if not blocked:
		return 0
	_diagnostic(
		"PUSH BLOCKED: default branch update or deletion is not allowed "
		f"(refs/heads/{default_branch}). Push a feature branch and open a pull request.",
	)
	return 1


def repository_root() -> Path | None:
	result = subprocess.run(
		["git", "rev-parse", "--show-toplevel"],
		capture_output=True,
		text=True,
		encoding="utf-8",
		check=False,
	)
	if result.returncode != 0 or not result.stdout.strip():
		return None
	return Path(result.stdout.strip()).resolve()


def _staged_manifest(repository: Path) -> bytes | None:
	result = subprocess.run(
		["git", "show", f":{MANIFEST_NAME}"],
		cwd=repository,
		capture_output=True,
		check=False,
	)
	return result.stdout if result.returncode == 0 else None


def _policy_release(manifest: bytes) -> str:
	try:
		document = tomllib.loads(manifest.decode("utf-8"))
		release = document["quality"]["policy_release"]
	except (UnicodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as error:
		raise ValueError("the staged manifest does not declare quality.policy_release") from error
	if not isinstance(release, str) or not RELEASE_VERSION.fullmatch(release):
		raise ValueError("quality.policy_release must use vMAJOR.MINOR.PATCH")
	return release


def _release_wheel(release: str) -> Path:
	local_app_data = os.environ.get("LOCALAPPDATA")
	if not local_app_data:
		raise ValueError("LOCALAPPDATA is unavailable")
	root = Path(local_app_data) / "quality-gate" / "releases" / release
	try:
		document = tomllib.loads((root / "release.toml").read_text(encoding="utf-8"))
		release_table = document["release"]
		if release_table["version"] != release:
			raise ValueError("the cached release identity does not match the manifest")
		files = release_table["files"]
	except (OSError, UnicodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as error:
		raise ValueError(f"cached release {release} is unavailable or invalid") from error
	for item in files:
		path = item.get("path") if isinstance(item, dict) else None
		digest = item.get("sha256") if isinstance(item, dict) else None
		if (
			not isinstance(path, str)
			or not path.startswith("quality_gate-")
			or not path.endswith(".whl")
		):
			continue
		wheel = root / path
		if not wheel.is_file() or not isinstance(digest, str):
			break
		actual = hashlib.sha256(wheel.read_bytes()).hexdigest()
		if actual != digest:
			raise ValueError(f"cached release {release} failed wheel integrity verification")
		return wheel
	raise ValueError(f"cached release {release} does not contain the policy wheel")


def run_released_command(
	repository: Path,
	arguments: list[str],
	*,
	manifest: bytes,
	capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
	"""Run CLI arguments through the verified release selected by a manifest."""
	release = _policy_release(manifest)
	wheel = _release_wheel(release)
	environment = os.environ.copy()
	environment["PYTHONPATH"] = str(wheel)
	environment["PYTHONDONTWRITEBYTECODE"] = "1"
	environment["PYTHONIOENCODING"] = "utf-8"
	try:
		return subprocess.run(
			[sys.executable, "-m", "quality_gate.cli", *arguments],
			cwd=Path(__file__).resolve().parent,
			env=environment,
			capture_output=capture_output,
			text=True,
			encoding="utf-8",
			errors="replace",
			check=False,
			timeout=COMMIT_GATE_TIMEOUT_SECONDS,
		)
	except subprocess.TimeoutExpired:
		return subprocess.CompletedProcess(
			[],
			2,
			"",
			"QUALITY GATE UNCHECKED: verification timed out; retry the operation.\n",
		)


def run_released_quality_gate(
	repository: Path,
	*,
	staged: bool,
	capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
	"""Run the exact cached release declared by the repository manifest."""
	manifest = _staged_manifest(repository) if staged else (repository / MANIFEST_NAME).read_bytes()
	if manifest is None:
		return subprocess.CompletedProcess([], 0, "", "")
	return run_released_command(
		repository,
		["--root", str(repository), "check"],
		manifest=manifest,
		capture_output=capture_output,
	)


def run_pre_commit() -> int:
	"""Run the immutable v2 release declared by the staged candidate."""
	root = repository_root()
	if root is None:
		return 0
	try:
		return run_released_quality_gate(root, staged=True).returncode
	except (OSError, ValueError) as error:
		_diagnostic(f"QUALITY GATE UNCHECKED: {error}. Run sync and setup, then retry.")
		return 2
