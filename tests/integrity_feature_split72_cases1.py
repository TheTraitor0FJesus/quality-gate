"""Feature test partition."""

from __future__ import annotations

from integrity_feature_split72_support import (
	FIXTURES as FIXTURES,
)
from integrity_feature_split72_support import (
	PROVIDER_SHA as PROVIDER_SHA,
)
from integrity_feature_split72_support import (
	TRACKER_DEPLOY as TRACKER_DEPLOY,
)
from integrity_feature_split72_support import (
	VALID_WORKFLOW as VALID_WORKFLOW,
)
from integrity_feature_split72_support import (
	Manifest as Manifest,
)
from integrity_feature_split72_support import (
	Path as Path,
)
from integrity_feature_split72_support import (
	SimpleNamespace as SimpleNamespace,
)
from integrity_feature_split72_support import (
	_assert_image_workflow_finding as _assert_image_workflow_finding,
)
from integrity_feature_split72_support import (
	_git as _git,
)
from integrity_feature_split72_support import (
	_image_release_fixture as _image_release_fixture,
)
from integrity_feature_split72_support import (
	_manifest as _manifest,
)
from integrity_feature_split72_support import (
	_multi_image_release_fixture as _multi_image_release_fixture,
)
from integrity_feature_split72_support import (
	_provider_internal_policy_tree as _provider_internal_policy_tree,
)
from integrity_feature_split72_support import (
	_repository_workflows as _repository_workflows,
)
from integrity_feature_split72_support import (
	_shared_image_release as _shared_image_release,
)
from integrity_feature_split72_support import (
	date as date,
)
from integrity_feature_split72_support import (
	documentation_component_result as documentation_component_result,
)
from integrity_feature_split72_support import (
	documentation_link_result as documentation_link_result,
)
from integrity_feature_split72_support import (
	git_integrity_results as git_integrity_results,
)
from integrity_feature_split72_support import (
	integrity as integrity,
)
from integrity_feature_split72_support import (
	load_manifest as load_manifest,
)
from integrity_feature_split72_support import (
	main as main,
)
from integrity_feature_split72_support import (
	os as os,
)
from integrity_feature_split72_support import (
	pytest as pytest,
)
from integrity_feature_split72_support import (
	runner as runner,
)
from integrity_feature_split72_support import (
	shutil as shutil,
)
from integrity_feature_split72_support import (
	stat as stat,
)
from integrity_feature_split72_support import (
	subprocess as subprocess,
)
from integrity_feature_split72_support import (
	sys as sys,
)
from integrity_feature_split72_support import (
	workflow_result as workflow_result,
)


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
