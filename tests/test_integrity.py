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


def test_workflow_hygiene_accepts_quality_gate_and_tracker_deployment(tmp_path: Path) -> None:
	_repository_workflows(tmp_path)

	result = workflow_result(tmp_path, _manifest(tmp_path))

	assert result.status.value == "passed"


@pytest.mark.parametrize(
	"condition",
	[
		"needs.prepare.outputs.status == 'build-required'",
		"\"needs.prepare.outputs.status == 'build-required'\"",
	],
	ids=("plain-condition", "double-quoted-condition"),
)
def test_workflow_hygiene_accepts_exact_shared_image_writer_release_route(
	tmp_path: Path, condition: str
) -> None:
	text = _shared_image_release().replace(
		"if: needs.prepare.outputs.status == 'build-required'",
		f"if: {condition}",
	)
	_image_release_fixture(tmp_path, workflow_text=text)

	result = workflow_result(tmp_path, _manifest(tmp_path))

	assert result.status.value == "passed", result.findings


def test_workflow_hygiene_accepts_multiple_image_producers_by_unique_local_names(
	tmp_path: Path,
) -> None:
	_multi_image_release_fixture(tmp_path)

	result = workflow_result(tmp_path, _manifest(tmp_path))

	assert result.status.value == "passed", result.findings


def test_workflow_hygiene_accepts_block_list_image_producer_dependencies(
	tmp_path: Path,
) -> None:
	workflow_dir = _multi_image_release_fixture(tmp_path)
	workflow = workflow_dir / "release.yml"
	workflow.write_text(
		workflow.read_text(encoding="utf-8").replace(
			"needs: [prepare, build-and-verify, build-second]",
			"needs:\n      - prepare\n      - build-and-verify\n      - build-second",
			1,
		),
		encoding="utf-8",
	)

	result = workflow_result(tmp_path, _manifest(tmp_path))

	assert result.status.value == "passed", result.findings


def test_workflow_hygiene_rejects_image_producer_without_packages_read(tmp_path: Path) -> None:
	text = _shared_image_release().replace("      packages: read\n", "", 1)
	_image_release_fixture(tmp_path, workflow_text=text)

	result = workflow_result(tmp_path, _manifest(tmp_path))

	assert result.status.value == "failed"
	assert any(
		"image producer job lacks explicit read-only" in finding.message
		for finding in result.findings
	)


def test_workflow_hygiene_rejects_image_producer_without_contents_read(tmp_path: Path) -> None:
	text = _shared_image_release().replace("      contents: read\n", "", 1)
	_image_release_fixture(tmp_path, workflow_text=text)

	result = workflow_result(tmp_path, _manifest(tmp_path))

	assert result.status.value == "failed"
	assert any(
		"image producer job lacks explicit read-only" in finding.message
		for finding in result.findings
	)


def test_workflow_hygiene_rejects_write_permission_on_image_producer(tmp_path: Path) -> None:
	text = _shared_image_release().replace("      packages: read\n", "      packages: write\n", 1)
	_image_release_fixture(tmp_path, workflow_text=text)

	result = workflow_result(tmp_path, _manifest(tmp_path))

	assert result.status.value == "failed"
	assert any(
		"image producer job lacks explicit read-only" in finding.message
		for finding in result.findings
	)


def _assert_image_workflow_finding(root: Path, message: str) -> None:
	result = workflow_result(root, _manifest(root))

	assert result.status.value == "failed"
	assert any(message in finding.message for finding in result.findings)


def test_workflow_hygiene_rejects_unmapped_image_producer_name(tmp_path: Path) -> None:
	text = _shared_image_release()
	config = """schema = 1
workflow = ".github/workflows/release.yml"

[[producers]]
name = "container"
job = "Build images"
checks = ["Verify image"]
deliverables = ["image.tar"]
kind = "image"
image_repository = "ghcr.io/example/product"
"""
	config = config.replace('job = "Build images"', 'job = "build-and-verify"')
	_image_release_fixture(tmp_path, workflow_text=text, publisher_config=config)
	_assert_image_workflow_finding(
		tmp_path,
		"image producer does not map to one local job with a unique literal name",
	)


def test_workflow_hygiene_rejects_duplicate_image_producer_names(tmp_path: Path) -> None:
	text = _shared_image_release().replace(
		"  write-image:\n",
		"""  build-duplicate:
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
""",
		1,
	)
	_image_release_fixture(tmp_path, workflow_text=text)
	_assert_image_workflow_finding(
		tmp_path,
		"image producer does not map to one local job with a unique literal name",
	)


def test_workflow_hygiene_rejects_dynamic_image_producer_name(tmp_path: Path) -> None:
	text = _shared_image_release().replace(
		"name: Build images", "name: Build ${{ matrix.platform }}", 1
	)
	_image_release_fixture(tmp_path, workflow_text=text)
	_assert_image_workflow_finding(
		tmp_path,
		"image producer does not map to one local job with a unique literal name",
	)


def test_workflow_hygiene_rejects_matrix_image_producer(tmp_path: Path) -> None:
	text = _shared_image_release().replace(
		"    timeout-minutes: 20\n    permissions:",
		"    timeout-minutes: 20\n    strategy:\n      matrix:\n        platform: [linux, windows]\n    permissions:",
		1,
	)
	_image_release_fixture(tmp_path, workflow_text=text)
	_assert_image_workflow_finding(
		tmp_path,
		"image producer does not map to one local job with a unique literal name",
	)


@pytest.mark.parametrize(
	"strategy",
	[
		"{matrix: {platform: [linux, windows]}}",
		"{'matrix': {platform: [linux, windows]}}",
	],
)
def test_workflow_hygiene_rejects_flow_style_matrix_image_producer(
	tmp_path: Path, strategy: str
) -> None:
	text = _shared_image_release().replace(
		"    timeout-minutes: 20\n    permissions:",
		f"    timeout-minutes: 20\n    strategy: {strategy}\n    permissions:",
		1,
	)
	_image_release_fixture(tmp_path, workflow_text=text)
	_assert_image_workflow_finding(
		tmp_path,
		"image producer does not map to one local job with a unique literal name",
	)


def test_workflow_hygiene_accepts_matrix_text_inside_a_strategy_string(tmp_path: Path) -> None:
	text = _shared_image_release().replace(
		"    timeout-minutes: 20\n    permissions:",
		'    timeout-minutes: 20\n    strategy: "{matrix: text}"\n    permissions:',
		1,
	)
	_image_release_fixture(tmp_path, workflow_text=text)
	result = workflow_result(tmp_path, _manifest(tmp_path))

	assert result.status.value == "passed", result.findings


def test_workflow_hygiene_rejects_matrix_image_writer(tmp_path: Path) -> None:
	text = _shared_image_release().replace(
		"  write-image:\n    name: Write image\n",
		"  write-image:\n    name: Write image\n    strategy: {matrix: {shard: [one, two]}}\n",
		1,
	)
	_image_release_fixture(tmp_path, workflow_text=text)
	_assert_image_workflow_finding(
		tmp_path,
		"release image writer job declares a matrix",
	)


def test_workflow_hygiene_requires_image_publisher_configuration(tmp_path: Path) -> None:
	_image_release_fixture(tmp_path)
	config = tmp_path / ".release" / "publisher.toml"
	config.unlink()
	_assert_image_workflow_finding(
		tmp_path,
		"image writer route has no valid matching publisher configuration",
	)


def test_workflow_hygiene_rejects_invalid_image_publisher_toml(tmp_path: Path) -> None:
	_image_release_fixture(tmp_path)
	config = tmp_path / ".release" / "publisher.toml"
	config.write_text("[[producers]", encoding="utf-8")
	_assert_image_workflow_finding(
		tmp_path,
		"image writer route has no valid matching publisher configuration",
	)


def test_workflow_hygiene_rejects_mismatched_image_publisher_workflow(tmp_path: Path) -> None:
	_image_release_fixture(tmp_path)
	config = tmp_path / ".release" / "publisher.toml"
	config.write_text(
		config.read_text(encoding="utf-8").replace(
			".github/workflows/release.yml", ".github/workflows/other.yml"
		),
		encoding="utf-8",
	)
	_assert_image_workflow_finding(
		tmp_path,
		"image writer route has no valid matching publisher configuration",
	)


def test_workflow_hygiene_requires_image_producer_in_publisher_config(tmp_path: Path) -> None:
	_image_release_fixture(tmp_path)
	config = tmp_path / ".release" / "publisher.toml"
	config.write_text(
		config.read_text(encoding="utf-8")
		.replace('image_repository = "ghcr.io/example/product"\n', "")
		.replace('kind = "image"', 'kind = "archive"'),
		encoding="utf-8",
	)
	_assert_image_workflow_finding(
		tmp_path,
		"image writer route has no valid matching publisher configuration",
	)


def test_workflow_hygiene_requires_each_image_producer_as_a_direct_dependency(
	tmp_path: Path,
) -> None:
	_multi_image_release_fixture(tmp_path, missing_second_dependency=True)

	result = workflow_result(tmp_path, _manifest(tmp_path))

	assert result.status.value == "failed"
	assert any(
		"image writer does not directly depend on every configured image producer"
		in finding.message
		for finding in result.findings
	)


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


@pytest.mark.parametrize(
	("origin", "github_repository", "expected"),
	[
		(
			"git@github.com:TheTraitor0FJesus/quality-gate.git",
			"TheTraitor0FJesus/quality-gate",
			"passed",
		),
		(
			"git@github.com:another-owner/quality-gate.git",
			"TheTraitor0FJesus/quality-gate",
			"failed",
		),
		(
			"git@github.com:TheTraitor0FJesus/quality-gate.git",
			"someone-else/quality-gate",
			"failed",
		),
	],
)
def test_provider_only_workflow_exception_requires_the_canonical_git_origin(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	origin: str,
	github_repository: str,
	expected: str,
) -> None:
	manifest = _provider_internal_policy_tree(tmp_path)
	_git(tmp_path, "init")
	_git(tmp_path, "remote", "add", "origin", origin)
	monkeypatch.setenv("GITHUB_REPOSITORY", github_repository)

	result = workflow_result(tmp_path, manifest)

	assert result.status.value == expected
	if expected == "failed":
		assert any(
			"write-capable workflow is not restricted" in finding.message
			for finding in result.findings
		)


def test_provider_only_workflow_exception_does_not_inherit_parent_repository_origin(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	repository_root = tmp_path / "quality-gate"
	nested_temp = repository_root / ".quality-gate-tmp" / "qg-run"
	nested_temp.mkdir(parents=True)
	monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

	def git_output(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
		if arguments[-2:] == ["rev-parse", "--show-toplevel"]:
			return subprocess.CompletedProcess(
				arguments, 0, stdout=str(repository_root).encode("utf-8")
			)
		return subprocess.CompletedProcess(
			arguments,
			0,
			stdout=b"git@github.com:TheTraitor0FJesus/quality-gate.git",
		)

	monkeypatch.setattr(integrity.subprocess, "run", git_output)

	assert integrity._is_quality_gate_origin(repository_root)
	assert not integrity._is_quality_gate_origin(nested_temp)


def test_provider_self_policy_uses_only_exact_short_lived_v21_bootstrap_waivers() -> None:
	manifest = load_manifest(Path(__file__).resolve().parents[1])
	waivers = [waiver for waiver in manifest.waivers if waiver.check_id == "repository.workflow"]
	bootstrap_targets = {
		".github/workflows/image-writer.yml",
		".github/workflows/release-publish.yml",
		".github/workflows/release.yml",
	}
	parity_target = ".github/workflows/parity.yml"

	assert manifest.policy_release == "v2.1.0"
	assert {waiver.target for waiver in waivers} == bootstrap_targets | {parity_target}
	bootstrap_waivers = [waiver for waiver in waivers if waiver.target in bootstrap_targets]
	assert len(bootstrap_waivers) == 3
	assert {waiver.target for waiver in bootstrap_waivers} == bootstrap_targets
	assert all(waiver.kind == "standard" for waiver in bootstrap_waivers)
	assert all(waiver.approved_by == "TheTraitor0FJesus" for waiver in bootstrap_waivers)
	assert all(waiver.reviewed_on.isoformat() == "2026-09-25" for waiver in bootstrap_waivers)
	assert all(waiver.expires_on.isoformat() == "2026-10-09" for waiver in bootstrap_waivers)
	parity_waivers = [waiver for waiver in waivers if waiver.target == parity_target]
	assert len(parity_waivers) == 1
	assert parity_waivers[0].kind == "standard"
	assert parity_waivers[0].reason == (
		"Parity is intentionally non-gating and runs only by dispatch or weekly schedule."
	)
	assert parity_waivers[0].approved_by == "TheTraitor0FJesus"
	assert parity_waivers[0].reviewed_on.isoformat() == "2026-08-26"
	assert parity_waivers[0].expires_on.isoformat() == "2027-08-26"


@pytest.mark.parametrize(
	("original", "replacement"),
	[
		(
			"candidate-id: ${{ needs.prepare.outputs.candidate-id }}",
			"candidate-id: 42",
		),
		(
			f"uses: TheTraitor0FJesus/quality-gate/.github/workflows/image-writer.yml@{PROVIDER_SHA}",
			"runs-on: ubuntu-latest\n    timeout-minutes: 10\n    steps:\n      - run: docker push ghcr.io/o/image",
		),
		(
			"needs: [prepare, build-and-verify, write-image]",
			"needs: [prepare, build-and-verify]",
		),
		("    branches: [main]\n", ""),
		("    branches: [main]\n", "    branches: [feature]\n"),
		(
			f"uses: TheTraitor0FJesus/quality-gate/.github/workflows/image-writer.yml@{PROVIDER_SHA}",
			"uses: TheTraitor0FJesus/quality-gate/.github/workflows/image-writer.yml@main",
		),
	],
)
def test_workflow_hygiene_rejects_untrusted_image_write_paths(
	tmp_path: Path, original: str, replacement: str
) -> None:
	text = _shared_image_release().replace(original, replacement, 1)
	_image_release_fixture(tmp_path, workflow_text=text)

	result = workflow_result(tmp_path, _manifest(tmp_path))

	assert result.status.value == "failed"
	assert any(
		"write-capable jobs are reachable from pull requests" in finding.message
		or "release writes are not limited" in finding.message
		or "packages: write is not scoped" in finding.message
		or "pinned" in finding.message
		for finding in result.findings
	)


def test_workflow_hygiene_accepts_inline_default_branch_deployment(tmp_path: Path) -> None:
	deploy = TRACKER_DEPLOY.replace("branches:\n      - main", "branches: [main]", 1)
	_repository_workflows(tmp_path, deploy)

	result = workflow_result(tmp_path, _manifest(tmp_path))

	assert result.status.value == "passed"


@pytest.mark.parametrize(
	("original", "replacement", "message"),
	[
		("  push:\n", "  pull_request:\n  push:\n", "reachable from pull requests"),
		("permissions:\n  contents: read", "permissions:\n  contents: write", "broader"),
		("      contents: write", "      issues: write", "unrelated write"),
		("    timeout-minutes: 10\n", "", "timeout"),
		(
			"actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
			"actions/checkout@main",
			"pinned",
		),
	],
)
def test_tracker_deployment_rejects_unsafe_policy_shapes(
	tmp_path: Path,
	original: str,
	replacement: str,
	message: str,
) -> None:
	_repository_workflows(tmp_path, TRACKER_DEPLOY.replace(original, replacement, 1))

	result = workflow_result(tmp_path, _manifest(tmp_path))

	assert result.status.value == "failed"
	assert any(message in finding.message for finding in result.findings)


@pytest.mark.parametrize(
	"push_filter",
	[
		"    branches:\n      - feature/main\n",
		"    tags:\n      - 'v*'\n",
	],
)
def test_repository_rejects_false_default_branch_coverage(tmp_path: Path, push_filter: str) -> None:
	workflow = _repository_workflows(tmp_path)
	quality = (workflow / "quality.yml").read_text(encoding="utf-8")
	quality = quality.replace("  push:\n", f"  push:\n{push_filter}", 1)
	(workflow / "quality.yml").write_text(quality, encoding="utf-8")

	result = workflow_result(tmp_path, _manifest(tmp_path))

	assert result.status.value == "failed"
	assert any("default branch" in finding.message for finding in result.findings)


@pytest.mark.parametrize(
	("trigger", "message"),
	[
		("  pull_request:\n", "pull requests"),
		("  push:\n", "default branch"),
	],
)
def test_repository_quality_gate_requires_pr_and_default_branch_coverage(
	tmp_path: Path,
	trigger: str,
	message: str,
) -> None:
	workflow = _repository_workflows(tmp_path)
	quality = (workflow / "quality.yml").read_text(encoding="utf-8")
	(workflow / "quality.yml").write_text(quality.replace(trigger, "", 1), encoding="utf-8")

	result = workflow_result(tmp_path, _manifest(tmp_path))

	assert result.status.value == "failed"
	assert any(message in finding.message for finding in result.findings)


def test_repository_rejects_ambiguous_quality_gate_jobs(tmp_path: Path) -> None:
	workflow = _repository_workflows(tmp_path)
	deploy = (workflow / "deploy.yml").read_text(encoding="utf-8")
	deploy += """
  duplicate-quality:
    name: Quality Gate
    uses: ./.github/workflows/quality.yml
"""
	(workflow / "deploy.yml").write_text(deploy, encoding="utf-8")

	result = workflow_result(tmp_path, _manifest(tmp_path))

	assert result.status.value == "failed"
	assert any("exactly one" in finding.message for finding in result.findings)


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
