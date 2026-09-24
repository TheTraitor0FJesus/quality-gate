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


def _root(
	root: Path,
	*,
	version: str = "2.0.6",
	source_policy_release: str | None = None,
) -> None:
	policy_release = source_policy_release or f"v{version}"
	(root / ".release").mkdir()
	(root / ".release" / "version.toml").write_text(
		f'version = "{version}"\n', encoding="utf-8"
	)
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
		f"""# Quality Gate v{version}

Version: {version}
Impact: PATCH
Changes: owner-merged immutable package publication.
Required adaptation: no consumer pin changes are required.

## Interface
The CLI remains compatible with the v2 contract.

## Integrations
The reusable workflow remains SHA-pinned.

## Configuration
The provider source policy pin is independent of the product release.

## Persisted data
N/A — the package has no persisted runtime data.

## Delivery/runtime
Linux and Windows package assets remain the delivery format.
""",
		encoding="utf-8",
	)
	(root / "pyproject.toml").write_text(
		f'[project]\nversion = "{version}"\n', encoding="utf-8"
	)
	(root / "quality-gate.toml").write_text(
		f'[quality]\npolicy_release = "{policy_release}"\n', encoding="utf-8"
	)
	(root / "quality_gate").mkdir()
	(root / "quality_gate" / "__init__.py").write_text(
		f'__version__ = "{version}"\n', encoding="utf-8"
	)
	(root / ".github" / "workflows").mkdir(parents=True)
	(root / ".github" / "workflows" / "quality.yml").write_text(
		"quality-gate sync --source release --version $QUALITY_GATE_RELEASE\n",
		encoding="utf-8",
	)
	(root / "templates").mkdir()
	(root / "templates" / "quality-gate.toml").write_text(
		f'policy_release = "v{version}"\n', encoding="utf-8"
	)


def test_product_projections_and_source_policy_selection_are_validated(tmp_path: Path) -> None:
	_root(tmp_path)

	assert load_version_authority(tmp_path) == "2.0.6"
	projection = validate_version_projections(tmp_path)
	assert projection.version == "2.0.6"
	assert projection.source_policy_release == "v2.0.6"

	(tmp_path / "quality_gate" / "__init__.py").write_text(
		'__version__ = "2.0.5"\n', encoding="utf-8"
	)
	with pytest.raises(ReleaseError, match="runtime version"):
		validate_version_projections(tmp_path)


def test_release_candidate_keeps_the_published_source_policy_pin(tmp_path: Path) -> None:
	_root(tmp_path, version="2.2.0", source_policy_release="v2.1.0")

	projection = validate_version_projections(tmp_path)

	assert projection.version == "2.2.0"
	assert projection.source_policy_release == "v2.1.0"


def test_release_candidate_rejects_an_invalid_source_policy_pin(tmp_path: Path) -> None:
	_root(tmp_path, version="2.2.0", source_policy_release="latest")

	with pytest.raises(ReleaseError, match="quality.policy_release"):
		validate_version_projections(tmp_path)


def test_release_candidate_requires_the_template_to_track_the_product(tmp_path: Path) -> None:
	_root(tmp_path, version="2.2.0", source_policy_release="v2.1.0")
	(tmp_path / "templates" / "quality-gate.toml").write_text(
		'policy_release = "v2.1.0"\n', encoding="utf-8"
	)

	with pytest.raises(ReleaseError, match="template policy version"):
		validate_version_projections(tmp_path)


def test_release_operation_timeouts_are_loaded_from_tracked_configuration(tmp_path: Path) -> None:
	(tmp_path / ".release").mkdir()
	(tmp_path / ".release" / "release-tools.toml").write_text(
		"[timeouts]\ngithub_api_seconds = 7\nexternal_download_seconds = 8\n"
		"subprocess_seconds = 9\ncheck_wait_seconds = 10\ncheck_poll_seconds = 11\n"
		"actions_evidence_wait_seconds = 12\nactions_evidence_poll_seconds = 3\n",
		encoding="utf-8",
	)

	expected_timeout = 7.0
	assert load_release_timeout(tmp_path, "github_api_seconds") == expected_timeout
	assert load_release_timeout(tmp_path, "actions_evidence_wait_seconds") == 12.0
	assert load_release_timeout(tmp_path, "actions_evidence_poll_seconds") == 3.0
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
