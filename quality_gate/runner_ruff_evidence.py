"""Authenticate local Ruff evidence and revalidate its provider-owned inputs."""

from __future__ import annotations

import hashlib
import os
import platform
import stat
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from . import runner as _runner
from .admission import running_in_ci
from .distribution import ExternalTool, PolicyCache, ReleaseManifest
from .edit_inputs import Configuration, SourceFile
from .edit_policy import file_sha256, ruff_artifact, tool_path
from .edit_protocol import EditError
from .evidence_candidate import CandidateEvidence
from .launcher import PreparedEnvironment
from .runtime import RuntimeIdentity, runtime_fingerprint
from .snapshot import SnapshotError
from .verification_evidence import (
	EngineIdentity,
	EvidenceProducer,
	EvidenceStore,
	engine_identity,
	repository_digest,
	ruff_file_identity,
	valid_ruff_file_outcome,
)


@dataclass
class LookupStats:
	lookup_ns: int = 0
	producer_durations_ms: list[int] = field(default_factory=list)

	@property
	def lookup_ms(self) -> int:
		return max(0, self.lookup_ns // 1_000_000)


def _collect_evidence(
	store: EvidenceStore | None,
	engine: EngineIdentity | None,
	repository: str | None,
	ruff_tool: ExternalTool | None,
	release_manifest: ReleaseManifest | None,
	verified_tool_sha256: str | None,
	runtime_identity_value: RuntimeIdentity | None,
	eligible: list[str],
	root: Path,
	misses: list[str],
	candidate_evidence: CandidateEvidence | None,
	evidence_files: dict[str, tuple[bytes, int]],
	configurations: dict[str, Configuration],
	release_sha256: str,
	policy_sha256: str,
	python_sha256: str,
	identities: dict[str, dict[str, Any]],
	cached: dict[str, dict[str, Any]],
	stats: LookupStats,
) -> None:
	if (
		store is None
		or engine is None
		or repository is None
		or ruff_tool is None
		or release_manifest is None
		or verified_tool_sha256 is None
		or not isinstance(runtime_identity_value, RuntimeIdentity)
	):
		raise ValueError("evidence readiness changed")
	for path in eligible:
		candidate = root / path
		metadata = candidate.lstat()
		if (
			not stat.S_ISREG(metadata.st_mode)
			or getattr(metadata, "st_file_attributes", 0)
			& getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
			or metadata.st_size > 64 * 1024 * 1024
		):
			misses.append(path)
			continue
		content = candidate.read_bytes()
		mode = candidate_evidence.mode(path, content) if candidate_evidence else None
		if mode is None:
			misses.append(path)
			continue
		evidence_files[path] = (content, mode)
		configuration = Configuration(root)
		configuration.capture("quality-gate.toml")
		configuration.for_sources(
			[
				SourceFile(
					path,
					content,
					hashlib.sha256(content).hexdigest(),
					mode,
				)
			]
		)
		configurations[path] = configuration
		configuration_values = [
			item
			for item in configuration.as_list()
			if Path(str(item["path"])).name
			in {
				"quality-gate.toml",
				"pyproject.toml",
				"ruff.toml",
				".ruff.toml",
				".gitignore",
				".ignore",
			}
		]
		identity = ruff_file_identity(
			engine=engine,
			repository=repository,
			path=path,
			mode=mode,
			content_sha256=hashlib.sha256(content).hexdigest(),
			tool_version=ruff_tool.version,
			tool_sha256=verified_tool_sha256,
			policy_release=release_manifest.version,
			release_sha256=release_sha256,
			policy_sha256=policy_sha256,
			python_version=runtime_identity_value.python_version,
			python_sha256=python_sha256,
			runtime_fingerprint=runtime_fingerprint(runtime_identity_value),
			platform=f"{sys.platform}-{platform.machine().casefold()}",
			configuration=configuration_values,
		)
		if identity is None:
			misses.append(path)
			continue
		identities[path] = identity
		configuration.assert_current()
		started = time.monotonic_ns()
		hit = store.lookup(identity, expected_engine=engine)
		stats.lookup_ns += max(0, time.monotonic_ns() - started)
		if hit is not None and valid_ruff_file_outcome(hit.outcome, path=path):
			cached[path] = hit.outcome
			stats.producer_durations_ms.append(hit.producer.duration_ms)
		else:
			misses.append(path)


def _publish_evidence(
	candidate_evidence: CandidateEvidence | None,
	store: EvidenceStore | None,
	engine: EngineIdentity | None,
	evidence_files: dict[str, tuple[bytes, int]],
	native_tool: Path | None,
	verified_tool_sha256: str | None,
	policy_path: Path,
	policy_sha256: str,
	prepared: PreparedEnvironment,
	release_sha256: str,
	python_executable: Path,
	python_sha256: str,
	fresh: dict[str, dict[str, object]],
	identities: dict[str, dict[str, Any]],
	configurations: dict[str, Configuration],
	root: Path,
	started_at: str,
	started_monotonic: float,
) -> None:
	if candidate_evidence is None or store is None or engine is None:
		raise ValueError("candidate evidence could not be revalidated")
	candidate_evidence.assert_current(evidence_files)
	if (
		native_tool is None
		or native_tool.is_symlink()
		or file_sha256(native_tool, 64 * 1024 * 1024) != verified_tool_sha256
		or hashlib.sha256(policy_path.read_bytes()).hexdigest() != policy_sha256
		or hashlib.sha256((prepared.policy_root / "release.toml").read_bytes()).hexdigest()
		!= release_sha256
		or hashlib.sha256(python_executable.read_bytes()).hexdigest() != python_sha256
	):
		raise ValueError("verification artifacts changed during evidence lookup")
	if engine_identity(Path(__file__).parent, version=__version__) != engine:
		raise ValueError("Quality Gate engine changed during Ruff verification")
	for path, outcome in fresh.items():
		identity = identities.get(path)
		captured_configuration = configurations.get(path)
		candidate = root / path
		content = candidate.read_bytes()
		mode = candidate_evidence.mode(path, content)
		if (
			identity is None
			or captured_configuration is None
			or hashlib.sha256(content).hexdigest() != identity["scope"]["files"][0]["sha256"]
			or mode != identity["scope"]["files"][0]["mode"]
		):
			continue
		captured_configuration.assert_current()
		if valid_ruff_file_outcome(outcome, path=path):
			store.publish(
				identity,
				producer=EvidenceProducer(
					operation="quality-gate.check",
					engine=engine,
					started_at=started_at,
					completed_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
					duration_ms=max(0, int((time.monotonic() - started_monotonic) * 1000)),
				),
				scope=identity["scope"],
				outcome=outcome,
			)


def _prepare_evidence(
	repository_root: Path,
	prepared: PreparedEnvironment,
	component_index: int,
	verified_tool_sha256: str | None,
	policy_path: Path,
	python_executable: Path,
	deadline: float,
	gate_timeout: int,
) -> tuple[
	str | None,
	EngineIdentity | None,
	ReleaseManifest | None,
	ExternalTool | None,
	RuntimeIdentity | None,
	EvidenceStore | None,
	CandidateEvidence | None,
	bool,
	str,
	str,
	str,
]:
	policy_sha256 = release_sha256 = python_sha256 = ""
	repository = repository_digest(repository_root)
	engine = engine_identity(Path(__file__).parent, version=__version__)
	release_manifest = getattr(prepared, "release_manifest", None)
	ruff_tool = next(
		(item for item in getattr(release_manifest, "tools", ()) if item.name.casefold() == "ruff"),
		None,
	)
	runtime_values = getattr(prepared, "runtime_identities", ())
	runtime_identity_value = (
		runtime_values[component_index - 1] if len(runtime_values) >= component_index else None
	)
	if (
		repository is None
		or engine is None
		or ruff_tool is None
		or verified_tool_sha256 is None
		or not isinstance(runtime_identity_value, RuntimeIdentity)
		or not policy_path.is_file()
	):
		engine = None
		store = None
	else:
		try:
			policy_sha256 = hashlib.sha256(policy_path.read_bytes()).hexdigest()
			release_sha256 = hashlib.sha256(
				(prepared.policy_root / "release.toml").read_bytes()
			).hexdigest()
			python_sha256 = hashlib.sha256(python_executable.read_bytes()).hexdigest()
			store = EvidenceStore(deadline)
		except OSError:
			store = None
	cache_enabled = store is not None and not running_in_ci()
	candidate_evidence = None
	if cache_enabled:
		try:
			candidate_evidence = CandidateEvidence(repository_root, deadline, gate_timeout)
		except (OSError, ValueError, SnapshotError):
			cache_enabled = False
	return (
		repository,
		engine,
		release_manifest,
		ruff_tool,
		runtime_identity_value,
		store,
		candidate_evidence,
		cache_enabled,
		policy_sha256,
		release_sha256,
		python_sha256,
	)


def _verified_ruff(
	prepared: PreparedEnvironment,
	python_executable: Path,
) -> tuple[list[str], Path | None, str | None]:
	tool_command = _runner._tool_command(prepared, python_executable, "ruff")
	native_tool: Path | None = None
	verified_tool_sha256: str | None = None
	try:
		artifact = ruff_artifact(prepared.policy_root, prepared.release_manifest)
		native_tool = tool_path(PolicyCache(prepared.cache_root), artifact.sha256)
		if (
			native_tool.is_file()
			and not native_tool.is_symlink()
			and os.access(native_tool, os.X_OK)
			and file_sha256(native_tool, 64 * 1024 * 1024) == artifact.sha256
		):
			tool_command = [str(native_tool)]
			verified_tool_sha256 = artifact.sha256
	except (AttributeError, OSError, EditError):
		pass
	return tool_command, native_tool, verified_tool_sha256


def _assert_evidence_current(
	candidate: CandidateEvidence,
	files: dict[str, tuple[bytes, int]],
	configurations: dict[str, Configuration],
	engine: EngineIdentity,
	native_tool: Path,
	tool_sha256: str,
	policy_path: Path,
	policy_sha256: str,
	prepared: PreparedEnvironment,
	release_sha256: str,
	python_executable: Path,
	python_sha256: str,
) -> None:
	"""Revalidate hits before publishing either lint or format results."""
	candidate.assert_current(files)
	for configuration in configurations.values():
		configuration.assert_current()
	if (
		native_tool.is_symlink()
		or file_sha256(native_tool, 64 * 1024 * 1024) != tool_sha256
		or hashlib.sha256(policy_path.read_bytes()).hexdigest() != policy_sha256
		or hashlib.sha256((prepared.policy_root / "release.toml").read_bytes()).hexdigest()
		!= release_sha256
		or hashlib.sha256(python_executable.read_bytes()).hexdigest() != python_sha256
		or engine_identity(Path(__file__).parent, version=__version__) != engine
	):
		raise ValueError("verification artifacts changed during evidence lookup")
