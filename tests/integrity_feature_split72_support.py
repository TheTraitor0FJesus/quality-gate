"""Fixtures and helpers for the split feature tests."""

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

from quality_gate import integrity, runner
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

TRACKER_DEPLOY = (FIXTURES / "tracker-deploy.yml").read_text(encoding="utf-8")

PROVIDER_SHA = "0123456789abcdef0123456789abcdef01234567"


def _shared_image_release() -> str:
	provider = "TheTraitor0FJesus/quality-gate/.github/workflows/"
	return f"""name: Product release
on:
  pull_request:
    branches: [main]
    types: [closed]
  workflow_dispatch:
    inputs:
      pr_number:
        required: true
        type: string
permissions:
  contents: read
  pull-requests: read
  actions: read
concurrency:
  group: release-${{{{ github.run_id }}}}
  cancel-in-progress: true
jobs:
  prepare:
    name: Prepare release
    if: github.event_name == 'workflow_dispatch' || github.event.pull_request.merged == true
    uses: {provider}release-prepare.yml@{PROVIDER_SHA}
    with:
      pr-number: ${{{{ format('{{0}}', github.event.pull_request.number || inputs.pr_number) }}}}
  build-and-verify:
    name: Build images
    needs: prepare
    if: needs.prepare.outputs.status == 'build-required'
    runs-on: ubuntu-latest
    timeout-minutes: 20
    permissions:
      contents: read
      packages: read
    steps:
      - name: Verify image
        run: true
  write-image:
    name: Write image
    needs: [prepare, build-and-verify]
    if: needs.prepare.outputs.status == 'build-required'
    uses: {provider}image-writer.yml@{PROVIDER_SHA}
    with:
      pr-number: ${{{{ format('{{0}}', github.event.pull_request.number || inputs.pr_number) }}}}
      candidate-id: ${{{{ needs.prepare.outputs.candidate-id }}}}
    permissions:
      contents: read
      pull-requests: read
      actions: read
      packages: write
  publish:
    name: Publish release
    needs: [prepare, build-and-verify, write-image]
    if: needs.prepare.outputs.status == 'build-required'
    uses: {provider}release-publish.yml@{PROVIDER_SHA}
    with:
      pr-number: ${{{{ format('{{0}}', github.event.pull_request.number || inputs.pr_number) }}}}
      candidate-id: ${{{{ needs.prepare.outputs.candidate-id }}}}
    permissions:
      contents: write
      pull-requests: read
      actions: read
      packages: read
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


def _repository_workflows(tmp_path: Path, deploy: str = TRACKER_DEPLOY) -> Path:
	workflow = tmp_path / ".github" / "workflows"
	workflow.mkdir(parents=True)
	(workflow / "quality.yml").write_text(VALID_WORKFLOW, encoding="utf-8")
	(workflow / "deploy.yml").write_text(deploy, encoding="utf-8")
	return workflow


def _image_release_fixture(
	tmp_path: Path,
	workflow_text: str | None = None,
	publisher_config: str | None = None,
) -> Path:
	workflow = _repository_workflows(tmp_path)
	(workflow / "release.yml").write_text(
		workflow_text or _shared_image_release(), encoding="utf-8"
	)
	config = tmp_path / ".release" / "publisher.toml"
	config.parent.mkdir(parents=True, exist_ok=True)
	config.write_text(
		publisher_config
		or """schema = 1
workflow = ".github/workflows/release.yml"

[[producers]]
name = "container"
job = "Build images"
checks = ["Verify image"]
deliverables = ["image.tar"]
kind = "image"
image_repository = "ghcr.io/example/product"
""",
		encoding="utf-8",
	)
	return workflow


def _multi_image_release_fixture(
	tmp_path: Path, *, missing_second_dependency: bool = False
) -> Path:
	text = _shared_image_release().replace(
		"  write-image:\n",
		"""  build-second:
    name: Build second image
    needs: prepare
    if: needs.prepare.outputs.status == 'build-required'
    runs-on: ubuntu-latest
    timeout-minutes: 20
    permissions:
      contents: read
      packages: read
    steps:
      - name: Verify second image
        run: true
  write-image:
""",
		1,
	)
	writer_needs = "[prepare, build-and-verify, build-second]"
	if missing_second_dependency:
		writer_needs = "[prepare, build-and-verify, build-second-extra]"
	text = text.replace("needs: [prepare, build-and-verify]", f"needs: {writer_needs}", 1)
	config = """schema = 1
workflow = ".github/workflows/release.yml"

[[producers]]
name = "first"
job = "Build images"
checks = ["Verify image"]
deliverables = ["first.tar"]
kind = "image"
image_repository = "ghcr.io/example/first"

[[producers]]
name = "second"
job = "Build second image"
checks = ["Verify second image"]
deliverables = ["second.tar"]
kind = "image"
image_repository = "ghcr.io/example/second"
"""
	return _image_release_fixture(tmp_path, workflow_text=text, publisher_config=config)


def _assert_image_workflow_finding(root: Path, message: str) -> None:
	result = workflow_result(root, _manifest(root))

	assert result.status.value == "failed"
	assert any(message in finding.message for finding in result.findings)


def _provider_internal_policy_tree(root: Path) -> Manifest:
	(root / "quality-gate.toml").write_text(
		"""waivers = []

[quality]
schema = 2
policy_release = "v2.0.6"

[repository]
name = "quality-gate"
domains = ["repository"]
required_documents = ["quality-gate.toml"]

[repository.limits]
max_blob_size_mib = 5

[repository.defaults]
command_timeout_seconds = 120
test_timeout_seconds = 300
gate_timeout_seconds = 600
""",
		encoding="utf-8",
	)
	workflows = root / ".github" / "workflows"
	workflows.mkdir(parents=True)
	(workflows / "quality.yml").write_text(VALID_WORKFLOW, encoding="utf-8")
	(workflows / "release-publish.yml").write_text(
		"""name: Shared release publisher
on:
  workflow_call:
permissions:
  contents: read
  pull-requests: read
  actions: read
  packages: read
concurrency:
  group: shared-release
  cancel-in-progress: false
jobs:
  publish:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    permissions:
      contents: write
      pull-requests: read
      actions: read
      packages: read
""",
		encoding="utf-8",
	)
	return load_manifest(root)


__all__ = [
	"FIXTURES",
	"Manifest",
	"PROVIDER_SHA",
	"Path",
	"SimpleNamespace",
	"TRACKER_DEPLOY",
	"VALID_WORKFLOW",
	"_assert_image_workflow_finding",
	"_git",
	"_image_release_fixture",
	"_manifest",
	"_multi_image_release_fixture",
	"_provider_internal_policy_tree",
	"_repository_workflows",
	"_shared_image_release",
	"date",
	"documentation_component_result",
	"documentation_link_result",
	"git_integrity_results",
	"integrity",
	"load_manifest",
	"main",
	"os",
	"pytest",
	"runner",
	"shutil",
	"stat",
	"subprocess",
	"sys",
	"workflow_result",
]
