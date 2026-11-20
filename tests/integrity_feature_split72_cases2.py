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
