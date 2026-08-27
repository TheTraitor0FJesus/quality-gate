"""Public tests for the self-hosted release controller."""

from __future__ import annotations

import hashlib
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from quality_gate.release import ReleaseControllerError, verify_release_candidate


def _source(root: Path, version: str = "2.0.0") -> None:
	(root / "AGENTS.md").write_text("contract\n", encoding="utf-8")
	(root / "pyproject.toml").write_text(
		f'[project]\nname = "quality-gate"\nversion = "{version}"\n',
		encoding="utf-8",
	)
	(root / "quality-gate.toml").write_text(
		f"""[quality]
schema = 2
policy_release = "v{version}"

[repository]
name = "quality-gate"
domains = ["repository"]
required_documents = ["AGENTS.md"]
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
	if valid_wheel:
		package = f"quality_gate-{version}.dist-info"
		with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
			archive.writestr("quality_gate/__init__.py", f'__version__ = "{version}"\n')
			archive.writestr(
				"quality_gate/__main__.py",
				"import argparse\n"
				"import os\n"
				"from pathlib import Path\n"
				"parser = argparse.ArgumentParser()\n"
				'parser.add_argument("--root", type=Path, required=True)\n'
				'commands = parser.add_subparsers(dest="stage", required=True)\n'
				'setup = commands.add_parser("setup")\n'
				'setup.add_argument("--cache-dir", type=Path, required=True)\n'
				'commands.add_parser("audit")\n'
				"options = parser.parse_args()\n"
				'expected_root_value = os.environ.get("QUALITY_GATE_RELEASE_EXPECT_ROOT", options.root)\n'
				"expected_root = Path(expected_root_value)\n"
				"if options.root.resolve() != expected_root.resolve():\n"
				'    parser.error("--root does not identify the release source")\n'
				'if options.stage == "setup":\n'
				'    expected_cache = Path.cwd() / "quality-gate"\n'
				"    if options.cache_dir.resolve() != expected_cache.resolve():\n"
				'        parser.error("--cache-dir does not identify the release cache")\n'
				'marker = os.environ.get("QUALITY_GATE_RELEASE_PROBE")\n'
				"if marker:\n"
				'    with open(marker, "a", encoding="utf-8") as stream:\n'
				'        stream.write(f"{options.stage}\\n")\n'
				'if os.environ.get("QUALITY_GATE_RELEASE_FAIL_STAGE") == options.stage:\n'
				"    raise SystemExit(1)\n",
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
	digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
	(artifact / "release.toml").write_text(
		f'''[release]
version = "v{version}"

[[release.files]]
path = "{wheel.name}"
sha256 = "{digest}"
''',
		encoding="utf-8",
	)
	return artifact


def test_release_controller_accepts_a_verified_self_host_candidate(tmp_path: Path) -> None:
	_source(tmp_path)
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

	assert marker.read_text(encoding="utf-8").splitlines() == ["setup", "audit"]


@pytest.mark.parametrize(
	("stage", "expected_calls", "error"),
	[
		("setup", ["setup"], "runtime setup failed"),
		("audit", ["setup", "audit"], "audit failed"),
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
