"""Feature-preserving test partition."""

from __future__ import annotations

from ci_feature_support72 import (
	DEPENDABOT as DEPENDABOT,
)
from ci_feature_support72 import (
	EXPECTED_CI_CHECK_INVOCATIONS as EXPECTED_CI_CHECK_INVOCATIONS,
)
from ci_feature_support72 import (
	FULL_SHA as FULL_SHA,
)
from ci_feature_support72 import (
	LEGACY_TEST_RELEASE as LEGACY_TEST_RELEASE,
)
from ci_feature_support72 import (
	PARITY_SCRIPT as PARITY_SCRIPT,
)
from ci_feature_support72 import (
	PARITY_WORKFLOW as PARITY_WORKFLOW,
)
from ci_feature_support72 import (
	POLICY_RELEASE as POLICY_RELEASE,
)
from ci_feature_support72 import (
	REPOSITORY as REPOSITORY,
)
from ci_feature_support72 import (
	RESULT_LINE as RESULT_LINE,
)
from ci_feature_support72 import (
	SCANNER_RELEASE as SCANNER_RELEASE,
)
from ci_feature_support72 import (
	WORKFLOW as WORKFLOW,
)
from ci_feature_support72 import (
	DistributionError as DistributionError,
)
from ci_feature_support72 import (
	Path as Path,
)
from ci_feature_support72 import (
	PolicyCache as PolicyCache,
)
from ci_feature_support72 import (
	_assert_shallow_history_is_unchecked as _assert_shallow_history_is_unchecked,
)
from ci_feature_support72 import (
	_build_wheel as _build_wheel,
)
from ci_feature_support72 import (
	_cached_policy_root as _cached_policy_root,
)
from ci_feature_support72 import (
	_copy_legacy_no_python_fixture as _copy_legacy_no_python_fixture,
)
from ci_feature_support72 import (
	_git as _git,
)
from ci_feature_support72 import (
	_install_wheel as _install_wheel,
)
from ci_feature_support72 import (
	_installed_scanner as _installed_scanner,
)
from ci_feature_support72 import (
	_make_writable as _make_writable,
)
from ci_feature_support72 import (
	_policy_root as _policy_root,
)
from ci_feature_support72 import (
	_release_gate as _release_gate,
)
from ci_feature_support72 import (
	_result_surface as _result_surface,
)
from ci_feature_support72 import (
	_run_cli as _run_cli,
)
from ci_feature_support72 import (
	_valid_parity_result as _valid_parity_result,
)
from ci_feature_support72 import (
	_write_parity_pair as _write_parity_pair,
)
from ci_feature_support72 import (
	_write_release as _write_release,
)
from ci_feature_support72 import (
	ci_parity as ci_parity,
)
from ci_feature_support72 import (
	compare_results as compare_results,
)
from ci_feature_support72 import (
	hashlib as hashlib,
)
from ci_feature_support72 import (
	json as json,
)
from ci_feature_support72 import (
	os as os,
)
from ci_feature_support72 import (
	pytest as pytest,
)
from ci_feature_support72 import (
	re as re,
)
from ci_feature_support72 import (
	runner as runner,
)
from ci_feature_support72 import (
	shutil as shutil,
)
from ci_feature_support72 import (
	stat as stat,
)
from ci_feature_support72 import (
	subprocess as subprocess,
)
from ci_feature_support72 import (
	sys as sys,
)
from ci_feature_support72 import (
	tomllib as tomllib,
)


def test_ci_and_local_cli_runs_match_on_a_release_backed_repository(
	tmp_path_factory: pytest.TempPathFactory, _release_gate: tuple[Path, Path]
) -> None:
	"""Verify local and CI runs expose one release, tool, result, and redaction surface."""

	# Leave room for cache staging beneath the nested Windows Gate workspace.
	tmp_path = tmp_path_factory.mktemp("parity")
	scanner = _installed_scanner()
	assert scanner is not None, "the active policy release must provide Gitleaks"
	gate, wheel = _release_gate
	root = tmp_path / "repository"
	_copy_legacy_no_python_fixture(root)
	_make_writable(root)
	workflow = root / ".github" / "workflows" / "quality.yml"
	workflow.parent.mkdir(parents=True)
	shutil.copy2(WORKFLOW, workflow)
	_git(root, "init")
	_git(root, "add", ".")
	_git(
		root,
		"-c",
		"user.name=Quality Gate Test",
		"-c",
		"user.email=quality-gate@example.test",
		"commit",
		"-m",
		"base",
	)
	base = _git(root, "rev-parse", "HEAD")
	credential = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"[:36]
	credential_path = root / "credentials.txt"
	credential_path.write_text(f"access={credential}\n", encoding="utf-8")
	_git(root, "add", "credentials.txt")
	_git(
		root,
		"-c",
		"user.name=Quality Gate Test",
		"-c",
		"user.email=quality-gate@example.test",
		"commit",
		"-m",
		"add credential fixture",
	)
	credential_path.unlink()
	_git(root, "add", "credentials.txt")
	_git(
		root,
		"-c",
		"user.name=Quality Gate Test",
		"-c",
		"user.email=quality-gate@example.test",
		"commit",
		"-m",
		"remove credential fixture",
	)

	release = root / ".release"
	_write_release(release, scanner, wheel)

	cache_bases = [root / ".local-cache", root / ".ci-cache"]
	selected_releases = []
	for cache_base in cache_bases:
		cache = PolicyCache(cache_base / "quality-gate")
		cache.sync(release)
		selected = cache.select(LEGACY_TEST_RELEASE)
		selected_releases.append(
			tomllib.loads((selected / "release.toml").read_text(encoding="utf-8"))["release"]
		)
	output_marker = "PARITY_OUTPUT_MARKER_7c83e5"
	environments = [
		{
			**os.environ,
			"LOCALAPPDATA": str(cache_base),
			"XDG_CACHE_HOME": str(cache_base),
			"QUALITY_GATE_PARITY_MARKER": output_marker,
		}
		for cache_base in cache_bases
	]
	local = _run_cli(gate, root, environments[0], "--base", base, "--head", "HEAD")
	ci = _run_cli(gate, root, environments[1], "--base", base, "--head", "HEAD")

	assert local.returncode == ci.returncode == 1
	assert _result_surface(local.stdout) == _result_surface(ci.stdout)
	assert _result_surface(local.stdout)
	assert _result_surface(local.stdout)["secrets.history"] == "failed"
	assert [release["version"] for release in selected_releases] == [
		LEGACY_TEST_RELEASE,
		LEGACY_TEST_RELEASE,
	]
	assert [
		[(tool["name"], tool["version"]) for tool in release["tools"]]
		for release in selected_releases
	] == [[("gitleaks", "8.30.1")], [("gitleaks", "8.30.1")]]
	assert "credentials.txt" in local.stdout
	assert "credentials.txt" in ci.stdout
	assert credential not in local.stdout + local.stderr
	assert credential not in ci.stdout + ci.stderr
	assert credential[4:20] not in local.stdout + local.stderr
	assert credential[4:20] not in ci.stdout + ci.stderr
	assert output_marker not in local.stdout
	assert output_marker not in ci.stdout

	_assert_shallow_history_is_unchecked(root, tmp_path, gate, environments[1], base, credential)


def test_ci_reports_an_unavailable_release_scanner_as_unchecked(
	tmp_path: Path, _release_gate: tuple[Path, Path]
) -> None:
	"""Verify an unusable release tool cannot produce a clean CI verdict."""

	root = tmp_path / "repository"
	_copy_legacy_no_python_fixture(root)
	_make_writable(root)
	_git(root, "init")
	_git(root, "add", ".")
	_git(
		root,
		"-c",
		"user.name=Quality Gate Test",
		"-c",
		"user.email=quality-gate@example.test",
		"commit",
		"-m",
		"base",
	)
	release = root / ".release"
	gate, wheel = _release_gate
	_write_release(release, b"not an executable scanner", wheel)
	cache_base = root / ".ci-cache"
	PolicyCache(cache_base / "quality-gate").sync(release)
	environment = {
		**os.environ,
		"LOCALAPPDATA": str(cache_base),
		"XDG_CACHE_HOME": str(cache_base),
	}

	result = _run_cli(gate, root, environment, "--base", "HEAD", "--head", "HEAD")

	assert result.returncode == runner.EXIT_UNCHECKED
	assert _result_surface(result.stdout)["secrets.candidate"] == "unchecked"
	assert _result_surface(result.stdout)["secrets.history"] == "unchecked"
