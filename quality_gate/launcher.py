"""Stable preparation boundary for policy selection and consumer runtimes."""

from __future__ import annotations

import re
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace
from pathlib import Path

from .contracts import Manifest, load_manifest
from .distribution import DistributionError, PolicyCache, ReleaseManifest, load_release_manifest
from .operation import current_operation
from .runtime import (
	RuntimeIdentity,
	RuntimeInspection,
	RuntimeManager,
	RuntimeUnavailable,
	runtime_identity,
)

_POLICY_RELEASE = re.compile(r"^v\d+\.\d+\.\d+$")


def _preparation_stage(name: str, scope: str) -> AbstractContextManager[None]:
	operation = current_operation()
	return nullcontext() if operation is None else operation.stage(name, scope)


def _annotate_tool_cache(stage: str, value: str) -> None:
	operation = current_operation()
	if operation is not None:
		operation.annotate_tool_cache(stage, value)


@dataclass(frozen=True, slots=True)
class PreparedEnvironment:
	"""The exact policy and runtime selections used by a consumer invocation."""

	root: Path
	manifest: Manifest
	policy_root: Path
	release_manifest: ReleaseManifest
	runtimes: tuple[RuntimeInspection, ...]
	cache_root: Path
	runtime_identities: tuple[RuntimeIdentity | None, ...]


def prepare(
	root: Path,
	*,
	cache_dir: Path | None = None,
	create_runtimes: bool = False,
	repository_root: Path | None = None,
) -> PreparedEnvironment:
	"""Select the manifest release and optionally prepare every Python runtime."""
	return _prepare(
		root,
		cache_dir=cache_dir,
		create_runtimes=create_runtimes,
		repository_root=repository_root,
	)


def prepare_bootstrap(
	root: Path,
	*,
	policy_release: str,
	cache_dir: Path | None = None,
	create_runtimes: bool = False,
	repository_root: Path | None = None,
) -> PreparedEnvironment:
	"""Prepare a previous immutable policy release for this repository's CI bootstrap."""
	return _prepare(
		root,
		cache_dir=cache_dir,
		create_runtimes=create_runtimes,
		repository_root=repository_root,
		policy_release=policy_release,
	)


def _prepare(
	root: Path,
	*,
	cache_dir: Path | None,
	create_runtimes: bool,
	repository_root: Path | None,
	policy_release: str | None = None,
) -> PreparedEnvironment:
	"""Prepare one selected policy release and optionally every Python runtime."""
	actual_root = root.resolve()
	identity_root = (repository_root or actual_root).resolve()
	manifest = load_manifest(actual_root)
	if policy_release is not None:
		if _POLICY_RELEASE.fullmatch(policy_release) is None:
			raise DistributionError("policy release override must use vMAJOR.MINOR.PATCH")
		manifest = replace(manifest, policy_release=policy_release)
	cache = PolicyCache(cache_dir)
	policy_stage = "policy.cache-lookup-validation"
	try:
		with _preparation_stage(
			policy_stage, f"immutable policy release {manifest.policy_release}"
		):
			policy_root = cache.select(manifest.policy_release)
	except DistributionError:
		_annotate_tool_cache(policy_stage, "cache miss or invalid release")
		raise
	_annotate_tool_cache(policy_stage, "verified cache hit")
	validation_stage = "policy.release-manifest-validation"
	try:
		with _preparation_stage(validation_stage, "cached immutable release inventory"):
			release_manifest = load_release_manifest(policy_root)
	except DistributionError:
		_annotate_tool_cache(validation_stage, "release validation failed")
		raise
	_annotate_tool_cache(validation_stage, "release inventory validated")
	manager = RuntimeManager(cache)
	runtimes: list[RuntimeInspection] = []
	runtime_identities: list[RuntimeIdentity | None] = []
	for component_index, component in enumerate(manifest.python, start=1):
		runtime_stage = f"runtime.cache-validation.component_{component_index}"
		try:
			with _preparation_stage(runtime_stage, str(component.path).replace("\\", "/")):
				try:
					identity = runtime_identity(
						actual_root,
						manifest,
						component,
						repository=str(identity_root),
					)
				except RuntimeUnavailable as error:
					if create_runtimes:
						raise
					runtime_identities.append(None)
					inspection = RuntimeInspection(
						manager.root / "unavailable" / component.name,
						None,
						False,
						str(error),
					)
				else:
					runtime_identities.append(identity)
					inspection = (
						manager.ensure(
							identity_root,
							manifest,
							component,
							policy_root=policy_root,
							release_manifest=release_manifest,
						)
						if create_runtimes
						else manager.inspect(identity_root, identity)
					)
		except RuntimeUnavailable:
			_annotate_tool_cache(runtime_stage, "runtime identity or cache unavailable")
			raise
		runtimes.append(inspection)
		_annotate_tool_cache(
			runtime_stage,
			"validated hit" if inspection.current else f"miss or stale: {inspection.reason}",
		)
	return PreparedEnvironment(
		actual_root,
		manifest,
		policy_root,
		release_manifest,
		tuple(runtimes),
		cache.requested_root,
		tuple(runtime_identities),
	)
