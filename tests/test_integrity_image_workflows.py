"""Workflow checks for image publication configuration."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_integrity import (
	PROVIDER_SHA,
	TRACKER_DEPLOY,
	_assert_image_workflow_finding,
	_image_release_fixture,
	_manifest,
	_multi_image_release_fixture,
	_repository_workflows,
	_shared_image_release,
)

from quality_gate.integrity import workflow_result


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
		"    timeout-minutes: 20\n    strategy:\n      matrix:\n"
		"        platform: [linux, windows]\n    permissions:",
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


@pytest.mark.parametrize(
	("original", "replacement"),
	[
		(
			"candidate-id: ${{ needs.prepare.outputs.candidate-id }}",
			"candidate-id: 42",
		),
		(
			"uses: TheTraitor0FJesus/quality-gate/.github/workflows/"
			f"image-writer.yml@{PROVIDER_SHA}",
			"runs-on: ubuntu-latest\n    timeout-minutes: 10\n    steps:\n"
			"      - run: docker push ghcr.io/o/image",
		),
		(
			"needs: [prepare, build-and-verify, write-image]",
			"needs: [prepare, build-and-verify]",
		),
		("    branches: [main]\n", ""),
		("    branches: [main]\n", "    branches: [feature]\n"),
		(
			"uses: TheTraitor0FJesus/quality-gate/.github/workflows/"
			f"image-writer.yml@{PROVIDER_SHA}",
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
