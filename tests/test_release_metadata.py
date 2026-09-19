"""Package metadata protections retained from the retired independent publisher."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.release_metadata import (
	ArtifactIdentity,
	ReleaseError,
	load_release_timeout,
	load_version_authority,
	validate_version_projections,
)

SOURCE_SHA = "a" * 40


def _root(root: Path) -> None:
	(root / ".release").mkdir()
	(root / ".release" / "version.toml").write_text('version = "2.0.6"\n', encoding="utf-8")
	(root / ".release" / "release-tools.toml").write_text(
		"""[timeouts]
github_api_seconds = 60
external_download_seconds = 120
subprocess_seconds = 900
check_wait_seconds = 900
check_poll_seconds = 10
""",
		encoding="utf-8",
	)
	(root / ".release" / "notes.md").write_text(
		"""# Quality Gate v2.0.6

Version: 2.0.6
Impact: PATCH
Changes: owner-merged immutable package publication.
Required adaptation: no consumer pin changes are required.

## Interface
The CLI remains compatible with the v2 contract.

## Integrations
The reusable workflow remains SHA-pinned.

## Configuration
The policy manifest projection follows the version authority.

## Persisted data
N/A — the package has no persisted runtime data.

## Delivery/runtime
Linux and Windows package assets remain the delivery format.
""",
		encoding="utf-8",
	)
	(root / "pyproject.toml").write_text('[project]\nversion = "2.0.6"\n', encoding="utf-8")
	(root / "quality-gate.toml").write_text(
		'[quality]\npolicy_release = "v2.0.6"\n', encoding="utf-8"
	)
	(root / "quality_gate").mkdir()
	(root / "quality_gate" / "__init__.py").write_text('__version__ = "2.0.6"\n', encoding="utf-8")
	(root / ".github" / "workflows").mkdir(parents=True)
	(root / ".github" / "workflows" / "quality.yml").write_text(
		"quality-gate sync --source release --version $QUALITY_GATE_RELEASE\n",
		encoding="utf-8",
	)
	(root / "templates").mkdir()
	(root / "templates" / "quality-gate.toml").write_text(
		'policy_release = "v2.0.6"\n', encoding="utf-8"
	)


def test_version_authority_and_all_projections_are_validated(tmp_path: Path) -> None:
	_root(tmp_path)

	assert load_version_authority(tmp_path) == "2.0.6"
	assert validate_version_projections(tmp_path).version == "2.0.6"

	(tmp_path / "quality_gate" / "__init__.py").write_text(
		'__version__ = "2.0.5"\n', encoding="utf-8"
	)
	with pytest.raises(ReleaseError, match="runtime version"):
		validate_version_projections(tmp_path)


def test_release_operation_timeouts_are_loaded_from_tracked_configuration(tmp_path: Path) -> None:
	(tmp_path / ".release").mkdir()
	(tmp_path / ".release" / "release-tools.toml").write_text(
		"[timeouts]\ngithub_api_seconds = 7\nexternal_download_seconds = 8\n"
		"subprocess_seconds = 9\ncheck_wait_seconds = 10\ncheck_poll_seconds = 11\n",
		encoding="utf-8",
	)

	expected_timeout = 7.0
	assert load_release_timeout(tmp_path, "github_api_seconds") == expected_timeout
	(tmp_path / ".release" / "release-tools.toml").write_text(
		"[timeouts]\ngithub_api_seconds = nan\nexternal_download_seconds = 8\n"
		"subprocess_seconds = 9\n",
		encoding="utf-8",
	)
	with pytest.raises(ReleaseError, match="finite"):
		load_release_timeout(tmp_path, "github_api_seconds")


def test_artifact_names_are_safe_upload_identifiers() -> None:
	with pytest.raises(ReleaseError, match="name is invalid"):
		ArtifactIdentity.from_mapping(
			{
				"version": "2.0.6",
				"source_sha": SOURCE_SHA,
				"platform": "linux",
				"name": "../release.zip",
				"sha256": "0" * 64,
			}
		)
