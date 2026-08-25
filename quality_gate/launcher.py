"""Stable preparation boundary for policy selection and consumer runtimes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .contracts import Manifest, load_manifest
from .distribution import PolicyCache, ReleaseManifest, load_release_manifest
from .runtime import RuntimeInspection, RuntimeManager, runtime_identity


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
	actual_root = root.resolve()
	identity_root = (repository_root or actual_root).resolve()
	manifest = load_manifest(actual_root)
	cache = PolicyCache(cache_dir)
	policy_root = cache.select(manifest.policy_release)
	release_manifest = load_release_manifest(policy_root)
	manager = RuntimeManager(cache)
	runtimes: list[RuntimeInspection] = []
	for component in manifest.python:
		identity = runtime_identity(
			actual_root,
			manifest,
			component,
			repository=str(identity_root),
		)
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
