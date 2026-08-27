from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from quality_gate import runner
from quality_gate.cli import main
from quality_gate.contracts import Manifest, load_manifest
from quality_gate.integrity import (
	documentation_component_result,
	documentation_link_result,
	git_integrity_results,
	workflow_result,
)

FIXTURES = Path(__file__).parent / "fixtures"
VALID_WORKFLOW = """name: Quality gate
on:
  pull_request:
  push:
permissions: read
concurrency:
  group: quality-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true
jobs:
  quality-gate:
    name: quality-gate
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@0123456789abcdef0123456789abcdef01234567
"""


def _manifest(root: Path, fixture: str = "no-python") -> Manifest:
	return load_manifest(FIXTURES / fixture)


def _git(root: Path, *arguments: str) -> None:
	result = subprocess.run(
		["git", *arguments],
		cwd=root,
		capture_output=True,
		check=False,
	)
	assert result.returncode == 0, result.stderr.decode(errors="replace")


def test_git_checks_collect_hygiene_classes_without_binary_content(tmp_path: Path) -> None:
	if os.name == "nt":
		pytest.skip("a Windows working tree cannot materialize two case-colliding files")
	root = tmp_path / "candidate"
	(root / "nested").mkdir(parents=True)
	(root / "merge.py").write_text("<<<<<<< HEAD\nvalue = 1\n=======\n", encoding="utf-8")
	(root / "nested" / ".DS_Store").write_bytes(b"binary\x00<<<<<<< HEAD")
	(root / "Readme.md").write_text("one", encoding="utf-8")
	(root / "README.md").write_text("two", encoding="utf-8")
	(root / "large.bin").write_bytes(b"x" * (5 * 1024 * 1024 + 1))

	results = {result.check_id: result for result in git_integrity_results(root, _manifest(root))}

	assert results["repository.git.conflict_markers"].status.value == "failed"
	assert results["repository.git.tracked_junk"].status.value == "failed"
	assert results["repository.git.case_collisions"].status.value == "failed"
	assert results["repository.git.large_blobs"].status.value == "failed"
	assert "<<<<<<<" not in results["repository.git.conflict_markers"].summary


def test_binary_marker_is_not_reported_as_text_conflict(tmp_path: Path) -> None:
	root = tmp_path / "candidate"
	root.mkdir()
	(root / "image.bin").write_bytes(b"\x00<<<<<<< HEAD\x00")

	result = git_integrity_results(root, _manifest(root))[0]

	assert result.status.value == "passed"


def test_case_collision_is_read_from_the_staged_index(tmp_path: Path) -> None:
	root = tmp_path / "repository"
	root.mkdir()
	shutil.copy(FIXTURES / "no-python" / "quality-gate.toml", root / "quality-gate.toml")
	(root / "README.md").write_text("same", encoding="utf-8")
	_git(root, "init")
	_git(root, "add", "quality-gate.toml", "README.md")
	blob = subprocess.run(
		["git", "hash-object", "-w", "--stdin"],
		cwd=root,
		input=b"same",
		capture_output=True,
		check=False,
	)
	assert blob.returncode == 0
	_git(
		root,
		"update-index",
		"--add",
		"--cacheinfo",
		f"100644,{blob.stdout.decode().strip()},Readme.md",
	)

	result = next(
		item
		for item in git_integrity_results(root, _manifest(root), repository=root)
		if item.check_id == "repository.git.case_collisions"
	)

	assert result.status.value == "failed"


def test_unsafe_symlink_is_reported_when_supported(tmp_path: Path) -> None:
	root = tmp_path / "candidate"
	root.mkdir()
	link = root / "outside.txt"
	try:
		link.symlink_to(tmp_path / "not-in-candidate")
	except OSError:
		pytest.skip("symbolic links are unavailable on this platform")

	result = next(
		item
		for item in git_integrity_results(root, _manifest(root))
		if item.check_id == "repository.git.unsafe_symlinks"
	)

	assert result.status.value == "failed"


def test_workflow_hygiene_accepts_pinned_bounded_workflow(tmp_path: Path) -> None:
	workflow = tmp_path / ".github" / "workflows"
	workflow.mkdir(parents=True)
	(workflow / "quality.yml").write_text(
		"""name: Quality gate
on:
  pull_request:
  push:
permissions: read
concurrency:
  group: quality-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true
jobs:
  quality-gate:
    name: quality-gate
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@0123456789abcdef0123456789abcdef01234567
""",
		encoding="utf-8",
	)

	result = workflow_result(tmp_path, _manifest(tmp_path))

	assert result.status.value == "passed"


def test_workflow_hygiene_accepts_a_pinned_reusable_caller(tmp_path: Path) -> None:
	"""Accept a reusable caller whose workflow reference is immutable."""

	workflow = tmp_path / ".github" / "workflows"
	workflow.mkdir(parents=True)
	workflow_text = (
		"""name: Quality Gate
on:
  workflow_call:
  pull_request:
  push:
permissions:
  contents: read
concurrency:
  group: quality-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true
jobs:
  quality-gate:
    name: Quality Gate
"""
		+ "    uses: TheTraitor0FJesus/quality-gate/.github/workflows/quality.yml@"
		+ "0123456789abcdef0123456789abcdef01234567"
		+ "\n"
	)
	(workflow / "quality.yml").write_text(
		workflow_text,
		encoding="utf-8",
	)

	result = workflow_result(tmp_path, _manifest(tmp_path))

	assert result.status.value == "passed"


def test_workflow_hygiene_accepts_a_same_commit_reusable_caller(tmp_path: Path) -> None:
	"""Accept a local reusable caller that is intrinsically pinned to the same commit."""

	workflow = tmp_path / ".github" / "workflows"
	workflow.mkdir(parents=True)
	(workflow / "quality.yml").write_text(
		"""name: Quality Gate
on:
  workflow_call:
  pull_request:
  push:
permissions:
  contents: read
concurrency:
  group: quality-${{ github.ref }}
  cancel-in-progress: true
jobs:
  quality-gate:
    name: Quality Gate
    uses: ./.github/workflows/reusable.yml
""",
		encoding="utf-8",
	)

	result = workflow_result(tmp_path, _manifest(tmp_path))

	assert result.status.value == "passed"


def test_workflow_hygiene_reports_mutable_reference_and_missing_controls(tmp_path: Path) -> None:
	workflow = tmp_path / ".github" / "workflows"
	workflow.mkdir(parents=True)
	(workflow / "quality.yml").write_text(
		"""name: Quality gate
on:
  push:
jobs:
  quality-gate:
    name: quality-gate
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@main
""",
		encoding="utf-8",
	)

	result = workflow_result(tmp_path, _manifest(tmp_path))

	assert result.status.value == "failed"
	assert any("pinned" in finding.message for finding in result.findings)
	assert any("timeout" in finding.message for finding in result.findings)


def test_malformed_workflow_is_unchecked(tmp_path: Path) -> None:
	workflow = tmp_path / ".github" / "workflows"
	workflow.mkdir(parents=True)
	(workflow / "quality.yml").write_text("name: broken\n", encoding="utf-8")

	result = workflow_result(tmp_path, _manifest(tmp_path))

	assert result.status.value == "unchecked"


def test_missing_workflow_is_unchecked(tmp_path: Path) -> None:
	result = workflow_result(tmp_path, _manifest(tmp_path))

	assert result.status.value == "unchecked"


def test_documentation_checks_links_and_manifest_component_paths(tmp_path: Path) -> None:
	manifest = load_manifest(FIXTURES / "valid")
	(tmp_path / "README.md").write_text(
		"The component is `app`.\n\n[missing](docs/missing.md)\n", encoding="utf-8"
	)

	links = documentation_link_result(tmp_path, manifest)
	components = documentation_component_result(tmp_path, manifest)

	assert links.status.value == "failed"
	assert components.status.value == "passed"


def test_exact_current_waiver_applies_only_to_one_target(tmp_path: Path) -> None:
	manifest_path = tmp_path / "quality-gate.toml"
	today = date.today().isoformat()
	manifest_path.write_text(
		f"""[[waivers]]
kind = "standard"
check_id = "repository.git.conflict_markers"
target = "merge.py"
reason = "fixture"
approved_by = "owner@example.com"
reviewed_on = "{today}"
expires_on = "{today}"

[quality]
schema = 2
policy_release = "v2.0.0"

[repository]
name = "waived"
domains = ["repository"]
required_documents = ["quality-gate.toml"]
""",
		encoding="utf-8",
	)
	(root := tmp_path / "candidate").mkdir()
	(root / "merge.py").write_text("<<<<<<< HEAD\n", encoding="utf-8")
	manifest = load_manifest(manifest_path.parent)

	result = next(
		item
		for item in git_integrity_results(root, manifest)
		if item.check_id == "repository.git.conflict_markers"
	)

	assert result.status.value == "waived"


def test_cli_checks_the_staged_repository_candidate(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	root = tmp_path / "repository"
	shutil.copytree(FIXTURES / "no-python", root)
	for path in (root, *root.rglob("*")):
		if not path.is_symlink():
			path.chmod(path.stat().st_mode | stat.S_IWUSR)
	workflow = root / ".github" / "workflows" / "quality.yml"
	workflow.parent.mkdir(parents=True)
	workflow.write_text(VALID_WORKFLOW, encoding="utf-8")
	_git(root, "init")
	(root / "conflict.py").write_text("<<<<<<< HEAD\n", encoding="utf-8")
	_git(root, "add", ".")
	(root / "conflict.py").write_text("resolved = True\n", encoding="utf-8")
	monkeypatch.setattr(
		runner,
		"prepare",
		lambda *_args, **_kwargs: SimpleNamespace(
			policy_root=FIXTURES,
			release_manifest=None,
			runtimes=(),
		),
	)
	monkeypatch.setattr(
		runner,
		"secret_candidate_result",
		lambda *_args, **_kwargs: runner.CheckResult(
			"secrets.candidate", runner.Status.PASSED, "no credentials detected"
		),
	)
	monkeypatch.setattr(
		runner,
		"secret_history_result",
		lambda *_args, **_kwargs: runner.CheckResult(
			"secrets.history",
			runner.Status.NOT_APPLICABLE,
			"base-to-head history scan is not requested",
			recovery_action="provide a verified CI base reference when range scanning applies",
		),
	)
	monkeypatch.setattr(sys, "argv", ["quality-gate", "--root", str(root), "check"])

	result = main()
	output = capsys.readouterr().out

	assert result == 1
	assert "repository.git.conflict_markers: failed" in output
	assert "conflict.py" in output
