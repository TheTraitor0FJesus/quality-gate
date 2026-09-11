"""Stable preparation boundary for policy selection and consumer runtimes."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path

from .contracts import Manifest, load_manifest
from .distribution import DistributionError, PolicyCache, ReleaseManifest, load_release_manifest
from .runtime import RuntimeInspection, RuntimeManager, RuntimeUnavailable, runtime_identity

_POLICY_RELEASE = re.compile(r"^v\d+\.\d+\.\d+$")


@dataclass(frozen=True, slots=True)
class PreparedEnvironment:
	"""The exact policy and runtime selections used by a consumer invocation."""

	root: Path
	manifest: Manifest
	policy_root: Path
	release_manifest: ReleaseManifest
	runtimes: tuple[RuntimeInspection, ...]


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
	policy_root = cache.select(manifest.policy_release)
	release_manifest = load_release_manifest(policy_root)
	manager = RuntimeManager(cache)
	runtimes: list[RuntimeInspection] = []
	for component in manifest.python:
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
			runtimes.append(
				RuntimeInspection(
					manager.root / "unavailable" / component.name,
					None,
					False,
					str(error),
				)
			)
			continue
		if create_runtimes:
			runtimes.append(
				manager.ensure(
					identity_root,
					manifest,
					component,
					policy_root=policy_root,
					release_manifest=release_manifest,
				)
			)
		else:
			runtimes.append(manager.inspect(identity_root, identity))
	return PreparedEnvironment(
		actual_root,
		manifest,
		policy_root,
		release_manifest,
		tuple(runtimes),
	)
