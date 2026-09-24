"""Public tests for the self-hosted release controller."""

from __future__ import annotations

import hashlib
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from quality_gate.release import ReleaseControllerError, verify_release_candidate
from quality_gate.release_contract import (
	UNIFIED_RELEASE_DEPENDENCIES,
	UNIFIED_RELEASE_PACKAGE_VERSION,
	UNIFIED_RELEASE_PLATFORM_TOOL_PATHS,
	UNIFIED_RELEASE_POLICY_FILES,
	UNIFIED_RELEASE_TOOLS,
)


def _source(
	root: Path,
	version: str = "2.0.0",
	*,
	policy_release: str | None = None,
) -> None:
	selected_policy_release = policy_release or f"v{version}"
	(root / "AGENTS.md").write_text("contract\n", encoding="utf-8")
	(root / ".release").mkdir()
	(root / ".release" / "version.toml").write_text(f'version = "{version}"\n', encoding="utf-8")
	(root / ".release" / "notes.md").write_text(
		f"""# Quality Gate v{version}

Version: {version}
Impact: MAJOR
Changes: release controller fixture.
Required adaptation: fixture only.

## Interface
fixture

## Integrations
fixture

## Configuration
fixture

## Persisted data
fixture

## Delivery/runtime
fixture
""",
		encoding="utf-8",
	)
	(root / "quality_gate").mkdir()
	(root / "quality_gate" / "__init__.py").write_text(
		f'__version__ = "{version}"\n', encoding="utf-8"
	)
	(root / "pyproject.toml").write_text(
		f'[project]\nname = "quality-gate"\nversion = "{version}"\n',
		encoding="utf-8",
	)
	(root / "quality-gate.toml").write_text(
		f"""[quality]
schema = 2
policy_release = "{selected_policy_release}"

[repository]
name = "quality-gate"
domains = ["repository"]
required_documents = ["AGENTS.md", ".release/version.toml", ".release/notes.md"]
""",
		encoding="utf-8",
	)
	workflow = root / ".github" / "workflows" / "quality.yml"
	workflow.parent.mkdir(parents=True)
	workflow.write_text(
		"""name: Quality Gate
on:
  pull_request:
  push:
    branches: [main]
permissions: read
concurrency:
  group: quality-test
  cancel-in-progress: true
jobs:
  quality-gate:
    name: Quality Gate
    runs-on: ubuntu-latest
    timeout-minutes: 1
""",
		encoding="utf-8",
	)
	environment = os.environ.copy()
	environment["GIT_CONFIG_GLOBAL"] = str(root / "missing-global-config")
	environment["GIT_CONFIG_NOSYSTEM"] = "1"
	for arguments in (
		("init",),
		("config", "user.name", "Quality Gate Test"),
		("config", "user.email", "quality-gate@example.test"),
		("add", "."),
		("commit", "-m", "source"),
	):
		result = subprocess.run(
			["git", *arguments], cwd=root, env=environment, capture_output=True, text=True
		)
		assert result.returncode == 0, result.stderr


def _artifact(root: Path, version: str = "2.0.0", *, valid_wheel: bool = True) -> Path:
	artifact = root / "release"
	artifact.mkdir()
	wheel = artifact / f"quality_gate-{version}-py3-none-any.whl"
	files: list[tuple[str, str, bytes]] = []
	tools: list[tuple[str, str, str, bytes]] = []
	if valid_wheel:
		package = f"quality_gate-{version}.dist-info"
		with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
			archive.writestr("quality_gate/__init__.py", f'__version__ = "{version}"\n')
			archive.writestr(
				"quality_gate/_probe.py",
				"import os\n"
				"from pathlib import Path\n"
				"def record(stage, policy_release, root, cache_dir=None):\n"
				"    expected_root = Path(os.environ.get(\n"
				'        "QUALITY_GATE_RELEASE_EXPECT_ROOT", root\n'
				"    ))\n"
				"    if Path(root).resolve() != expected_root.resolve():\n"
				"        raise SystemExit(2)\n"
				"    if cache_dir is not None:\n"
				'        expected_cache = Path.cwd() / "quality-gate"\n'
				"        if Path(cache_dir).resolve() != expected_cache.resolve():\n"
				"            raise SystemExit(2)\n"
				'    marker = os.environ.get("QUALITY_GATE_RELEASE_PROBE")\n'
				"    if marker:\n"
				'        with open(marker, "a", encoding="utf-8") as stream:\n'
				'            stream.write(f"{stage} {policy_release}\\n")\n'
				'    if os.environ.get("QUALITY_GATE_RELEASE_FAIL_STAGE") == stage:\n'
				"        raise SystemExit(1)\n",
			)
			archive.writestr(
				"quality_gate/launcher.py",
				"from ._probe import record\n"
				"def prepare_bootstrap(root, *, policy_release, cache_dir, create_runtimes):\n"
				"    if not create_runtimes:\n"
				"        raise SystemExit(2)\n"
				'    record("setup", policy_release, root, cache_dir)\n',
			)
			archive.writestr(
				"quality_gate/runner.py",
				"from types import SimpleNamespace\n"
				"from ._probe import record\n"
				"def bootstrap_audit(root, *, policy_release):\n"
				'    record("audit", policy_release, root)\n'
				"    return SimpleNamespace(exit_code=0)\n",
			)
			archive.writestr(
				f"{package}/METADATA",
				f"Metadata-Version: 2.1\nName: quality-gate\nVersion: {version}\n",
			)
			archive.writestr(
				f"{package}/WHEEL",
				"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
			)
			archive.writestr(f"{package}/RECORD", "")
	else:
		wheel.write_bytes(b"not a wheel")
	files.append((wheel.name, "artifact", wheel.read_bytes()))
	if version == UNIFIED_RELEASE_PACKAGE_VERSION:
		files.extend(
			(
				f"{name}-{dependency_version}-py3-none-any.whl",
				"dependency",
				f"dependency:{name}".encode(),
			)
			for name, dependency_version in UNIFIED_RELEASE_DEPENDENCIES.items()
		)
		files.extend(
			(path, "policy", f"policy:{path}".encode()) for path in UNIFIED_RELEASE_POLICY_FILES
		)
		platform = "windows" if os.name == "nt" else "linux"
		for name, tool_version in UNIFIED_RELEASE_TOOLS.items():
			if name in {"gitleaks", "biome"}:
				path = UNIFIED_RELEASE_PLATFORM_TOOL_PATHS[platform][name]
			else:
				path = f"{name}-{tool_version}-py3-none-any.whl"
			tools.append((name, tool_version, path, f"tool:{name}".encode()))
	manifest = ["[release]", f'version = "v{version}"', ""]
	for path, kind, content in files:
		file_path = artifact / path
		file_path.parent.mkdir(parents=True, exist_ok=True)
		file_path.write_bytes(content)
		manifest.extend(
			[
				"[[release.files]]",
				f'path = "{path}"',
				f'sha256 = "{hashlib.sha256(content).hexdigest()}"',
				f'kind = "{kind}"',
				"",
			]
		)
	for name, tool_version, path, content in tools:
		file_path = artifact / path
		file_path.parent.mkdir(parents=True, exist_ok=True)
		file_path.write_bytes(content)
		manifest.extend(
			[
				"[[release.tools]]",
				f'name = "{name}"',
				f'version = "{tool_version}"',
				f'path = "{path}"',
				f'sha256 = "{hashlib.sha256(content).hexdigest()}"',
				"",
			]
		)
	(artifact / "release.toml").write_text("\n".join(manifest), encoding="utf-8")
	return artifact


def test_release_controller_accepts_a_verified_self_host_candidate(tmp_path: Path) -> None:
	_source(tmp_path)
	(tmp_path / ".release" / "notes.md").write_text(
		"Общий механизм публикации. Существующие форматы пакетов сохраняются.\n", encoding="utf-8"
	)
	candidate = verify_release_candidate(tmp_path, _artifact(tmp_path))

	assert candidate.version == "v2.0.0"
	assert candidate.manifest.wheel is not None


def test_release_controller_rejects_an_unavailable_workspace_parent(tmp_path: Path) -> None:
	_source(tmp_path)

	with pytest.raises(ReleaseControllerError, match="workspace parent"):
		verify_release_candidate(
			tmp_path,
			_artifact(tmp_path),
			workspace_parent=tmp_path / "missing-workspace-parent",
		)


def test_release_controller_executes_the_verified_wheel(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	_source(tmp_path)
	marker = tmp_path / "release-probe.txt"
	monkeypatch.setenv("QUALITY_GATE_RELEASE_PROBE", str(marker))
	monkeypatch.setenv("QUALITY_GATE_RELEASE_EXPECT_ROOT", str(tmp_path))

	verify_release_candidate(tmp_path, _artifact(tmp_path))

	assert marker.read_text(encoding="utf-8").splitlines() == [
		"setup v2.0.0",
		"audit v2.0.0",
	]


def test_release_controller_audits_with_candidate_policy_when_source_pin_is_previous(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	_source(tmp_path, version="2.2.0", policy_release="v2.1.0")
	marker = tmp_path / "release-probe.txt"
	monkeypatch.setenv("QUALITY_GATE_RELEASE_PROBE", str(marker))
	monkeypatch.setenv("QUALITY_GATE_RELEASE_EXPECT_ROOT", str(tmp_path))

	candidate = verify_release_candidate(tmp_path, _artifact(tmp_path, version="2.2.0"))

	assert candidate.version == "v2.2.0"
	assert marker.read_text(encoding="utf-8").splitlines() == [
		"setup v2.2.0",
		"audit v2.2.0",
	]


def test_release_controller_binds_candidate_to_product_version_not_policy_pin(
	tmp_path: Path,
) -> None:
	_source(tmp_path, version="2.2.0", policy_release="v2.1.0")

	with pytest.raises(ReleaseControllerError, match="does not match product version 2.2.0"):
		verify_release_candidate(
			tmp_path,
			_artifact(tmp_path, version="2.2.0"),
			version="v2.1.0",
		)


@pytest.mark.parametrize(
	("stage", "expected_calls", "error"),
	[
		("setup", ["setup v2.0.0"], "runtime setup failed"),
		("audit", ["setup v2.0.0", "audit v2.0.0"], "audit failed"),
	],
)
def test_release_controller_rejects_a_failed_artifact_stage(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	stage: str,
	expected_calls: list[str],
	error: str,
) -> None:
	_source(tmp_path)
	marker = tmp_path / "release-probe.txt"
	monkeypatch.setenv("QUALITY_GATE_RELEASE_PROBE", str(marker))
	monkeypatch.setenv("QUALITY_GATE_RELEASE_EXPECT_ROOT", str(tmp_path))
	monkeypatch.setenv("QUALITY_GATE_RELEASE_FAIL_STAGE", stage)

	with pytest.raises(ReleaseControllerError, match=error):
		verify_release_candidate(tmp_path, _artifact(tmp_path))

	assert marker.read_text(encoding="utf-8").splitlines() == expected_calls


def test_release_controller_rejects_an_unlearned_lesson(tmp_path: Path) -> None:
	_source(tmp_path)
	lessons = tmp_path / "lessons"
	lessons.mkdir()
	(lessons / "incident.md").write_text(
		"""---
id: incident-1
status: open
incident: A defect escaped.
expected_layer: repository check
miss_cause: Coverage was missing.
adaptation:
evidence:
---
""",
		encoding="utf-8",
	)

	with pytest.raises(ReleaseControllerError, match="lessons"):
		verify_release_candidate(tmp_path, _artifact(tmp_path))


def test_release_controller_rejects_a_corrupt_artifact(tmp_path: Path) -> None:
	_source(tmp_path)
	artifact = _artifact(tmp_path)
	wheel = next(artifact.glob("*.whl"))
	wheel.write_bytes(b"tampered wheel")

	with pytest.raises(ReleaseControllerError, match="checksum"):
		verify_release_candidate(tmp_path, artifact)


def test_release_controller_rejects_an_uninstallable_wheel(tmp_path: Path) -> None:
	_source(tmp_path)

	with pytest.raises(ReleaseControllerError, match="self-host"):
		verify_release_candidate(tmp_path, _artifact(tmp_path, valid_wheel=False))
