"""Feature test partition."""

from __future__ import annotations

from contracts_cli_feature_split72_support import (
	EXIT_QUALITY_FAILURE as EXIT_QUALITY_FAILURE,
)
from contracts_cli_feature_split72_support import (
	EXIT_UNCHECKED as EXIT_UNCHECKED,
)
from contracts_cli_feature_split72_support import (
	EXPECTED_CSS_FILE_KIB as EXPECTED_CSS_FILE_KIB,
)
from contracts_cli_feature_split72_support import (
	EXPECTED_CSS_TOTAL_KIB as EXPECTED_CSS_TOTAL_KIB,
)
from contracts_cli_feature_split72_support import (
	EXPECTED_JAVASCRIPT_FILE_KIB as EXPECTED_JAVASCRIPT_FILE_KIB,
)
from contracts_cli_feature_split72_support import (
	EXPECTED_JAVASCRIPT_TOTAL_KIB as EXPECTED_JAVASCRIPT_TOTAL_KIB,
)
from contracts_cli_feature_split72_support import (
	MAX_SUPPLEMENTAL_TARGET_MATCHES as MAX_SUPPLEMENTAL_TARGET_MATCHES,
)
from contracts_cli_feature_split72_support import (
	MAX_SUPPLEMENTAL_TESTS as MAX_SUPPLEMENTAL_TESTS,
)
from contracts_cli_feature_split72_support import (
	REPOSITORY as REPOSITORY,
)
from contracts_cli_feature_split72_support import (
	SCHEMA_VERSION as SCHEMA_VERSION,
)
from contracts_cli_feature_split72_support import (
	VALID_WORKFLOW as VALID_WORKFLOW,
)
from contracts_cli_feature_split72_support import (
	CheckResult as CheckResult,
)
from contracts_cli_feature_split72_support import (
	Finding as Finding,
)
from contracts_cli_feature_split72_support import (
	Path as Path,
)
from contracts_cli_feature_split72_support import (
	PolicyCache as PolicyCache,
)
from contracts_cli_feature_split72_support import (
	Status as Status,
)
from contracts_cli_feature_split72_support import (
	Verdict as Verdict,
)
from contracts_cli_feature_split72_support import (
	__version__ as __version__,
)
from contracts_cli_feature_split72_support import (
	_git as _git,
)
from contracts_cli_feature_split72_support import (
	_init_and_stage as _init_and_stage,
)
from contracts_cli_feature_split72_support import (
	_manifest as _manifest,
)
from contracts_cli_feature_split72_support import (
	_run as _run,
)
from contracts_cli_feature_split72_support import (
	_stage_lesson_repository as _stage_lesson_repository,
)
from contracts_cli_feature_split72_support import (
	_supplemental_test as _supplemental_test,
)
from contracts_cli_feature_split72_support import (
	_web_component as _web_component,
)
from contracts_cli_feature_split72_support import (
	date as date,
)
from contracts_cli_feature_split72_support import (
	fingerprint_secret as fingerprint_secret,
)
from contracts_cli_feature_split72_support import (
	hashlib as hashlib,
)
from contracts_cli_feature_split72_support import (
	json as json,
)
from contracts_cli_feature_split72_support import (
	load_manifest as load_manifest,
)
from contracts_cli_feature_split72_support import (
	os as os,
)
from contracts_cli_feature_split72_support import (
	pytest as pytest,
)
from contracts_cli_feature_split72_support import (
	shutil as shutil,
)
from contracts_cli_feature_split72_support import (
	subprocess as subprocess,
)
from contracts_cli_feature_split72_support import (
	sys as sys,
)
from contracts_cli_feature_split72_support import (
	timedelta as timedelta,
)
from contracts_cli_feature_split72_support import (
	tomllib as tomllib,
)


def test_validate_accepts_non_python_manifest(tmp_path: Path) -> None:
	(tmp_path / "quality-gate.toml").write_text(_manifest(), encoding="utf-8")
	result = _run(tmp_path, "validate")

	assert result.returncode == 0
	assert "schema 2" in result.stdout


def test_version_cli_reports_the_installed_quality_gate_version(tmp_path: Path) -> None:
	result = _run(tmp_path, "version")

	assert result.returncode == 0
	assert result.stdout.strip() == f"quality-gate {__version__}"


def test_direct_cli_script_invocation_keeps_the_package_bootstrap(tmp_path: Path) -> None:
	environment = os.environ.copy()
	environment.pop("PYTHONPATH", None)
	result = subprocess.run(
		[sys.executable, str(REPOSITORY / "quality_gate" / "cli.py"), "version"],
		cwd=tmp_path,
		env=environment,
		capture_output=True,
		text=True,
		check=False,
	)

	assert result.returncode == 0, result.stderr
	assert result.stdout.strip() == f"quality-gate {__version__}"


def test_validate_accepts_python_manifest(tmp_path: Path) -> None:
	(tmp_path / "quality-gate.toml").write_text(_manifest(python=True), encoding="utf-8")
	result = _run(tmp_path, "validate")

	assert result.returncode == 0


def test_validate_accepts_web_manifest_with_default_asset_budgets(tmp_path: Path) -> None:
	(tmp_path / "quality-gate.toml").write_text(_manifest(web=_web_component()), encoding="utf-8")

	result = _run(tmp_path, "validate")
	component = load_manifest(tmp_path).web[0]

	assert result.returncode == 0
	assert component.javascript_file_kib == EXPECTED_JAVASCRIPT_FILE_KIB
	assert component.css_file_kib == EXPECTED_CSS_FILE_KIB
	assert component.javascript_total_kib == EXPECTED_JAVASCRIPT_TOTAL_KIB
	assert component.css_total_kib == EXPECTED_CSS_TOTAL_KIB


def test_validate_accepts_supplemental_node_tests(tmp_path: Path) -> None:
	target = tmp_path / "tests" / "node" / "smoke.test.js"
	target.parent.mkdir(parents=True)
	target.write_text("test('smoke', () => {});\n", encoding="utf-8")
	(tmp_path / "quality-gate.toml").write_text(
		_manifest() + _supplemental_test(), encoding="utf-8"
	)

	result = _run(tmp_path, "validate")
	supplemental = load_manifest(tmp_path).supplemental_tests

	assert result.returncode == 0
	assert supplemental[0].name == "node-functional"
	assert supplemental[0].runner == "node-test"
	assert supplemental[0].targets == ("tests/node/**/*.test.js",)


def test_validate_rejects_invalid_supplemental_declarations(tmp_path: Path) -> None:
	valid_target = tmp_path / "tests" / "node" / "smoke.test.js"
	valid_target.parent.mkdir(parents=True)
	valid_target.write_text("test('smoke', () => {});\n", encoding="utf-8")
	invalid_manifests = (
		_manifest() + _supplemental_test(runner="arbitrary-command"),
		_manifest() + _supplemental_test(name="INVALID NAME"),
		_manifest() + _supplemental_test() + _supplemental_test(),
		_manifest() + _supplemental_test(targets="[]"),
		_manifest() + _supplemental_test(targets='["../outside.test.js"]'),
		_manifest() + _supplemental_test(targets='["tests/node/missing.test.js"]'),
		_manifest() + _supplemental_test(targets='[""]'),
	)

	for manifest in invalid_manifests:
		(tmp_path / "quality-gate.toml").write_text(manifest, encoding="utf-8")
		result = _run(tmp_path, "validate")
		assert result.returncode == EXIT_UNCHECKED


def test_validate_rejects_unbounded_supplemental_target_matches(tmp_path: Path) -> None:
	target_root = tmp_path / "tests" / "node"
	target_root.mkdir(parents=True)
	for index in range(MAX_SUPPLEMENTAL_TARGET_MATCHES + 1):
		(target_root / f"case-{index}.test.js").write_text("\n", encoding="utf-8")
	(tmp_path / "quality-gate.toml").write_text(
		_manifest() + _supplemental_test(targets='["tests/node/*.test.js"]'), encoding="utf-8"
	)

	result = _run(tmp_path, "validate")

	assert result.returncode == EXIT_UNCHECKED


def test_validate_rejects_unbounded_supplemental_declarations(tmp_path: Path) -> None:
	target = tmp_path / "tests" / "node" / "smoke.test.js"
	target.parent.mkdir(parents=True)
	target.write_text("\n", encoding="utf-8")
	declarations = "".join(
		_supplemental_test(name=f"node-functional-{index}")
		for index in range(MAX_SUPPLEMENTAL_TESTS + 1)
	)
	(tmp_path / "quality-gate.toml").write_text(_manifest() + declarations, encoding="utf-8")

	result = _run(tmp_path, "validate")

	assert result.returncode == EXIT_UNCHECKED


def test_check_validates_but_does_not_execute_supplemental_tests(tmp_path: Path) -> None:
	(tmp_path / "quality-gate.toml").write_text(
		_manifest() + _supplemental_test(targets='["tests/node/supplemental.test.js"]'),
		encoding="utf-8",
	)
	(tmp_path / "AGENTS.md").write_text("contract\n", encoding="utf-8")
	target = tmp_path / "tests" / "node" / "supplemental.test.js"
	target.parent.mkdir(parents=True)
	target.write_text(
		"test('supplemental tests stay outside Quality Gate', "
		"() => { throw new Error('must not run'); });\n",
		encoding="utf-8",
	)
	_init_and_stage(tmp_path, "quality-gate.toml", "AGENTS.md", "tests/node/supplemental.test.js")

	result = _run(tmp_path, "check")

	assert result.returncode == 0, result.stdout + result.stderr
	assert "must not run" not in result.stdout


def test_validate_rejects_unsafe_web_paths_patterns_and_duplicate_names(tmp_path: Path) -> None:
	unsafe_manifests = (
		_manifest(web=_web_component(root="..")),
		_manifest(web=_web_component(root="../assets")),
		_manifest(web=_web_component(root="assets/..")),
		_manifest(web=_web_component(root="C:assets")),
		_manifest(web=_web_component(root="C:/assets")),
		_manifest(web=_web_component(root="assets:part")),
		_manifest(web=_web_component(root="assets\x00part")),
		_manifest(web=_web_component(javascript='["../outside/*.js"]')),
		_manifest(web=_web_component(javascript='["js/[broken.js"]')),
		_manifest(web=_web_component() + _web_component()),
	)

	for manifest in unsafe_manifests:
		(tmp_path / "quality-gate.toml").write_text(manifest, encoding="utf-8")
		result = _run(tmp_path, "validate")
		assert result.returncode == EXIT_UNCHECKED


def test_check_measures_staged_web_assets_and_reports_portable_paths(tmp_path: Path) -> None:
	web = _web_component(javascript='["js\\\\**\\\\*.js"]', css="[]")
	(tmp_path / "quality-gate.toml").write_text(_manifest(web=web), encoding="utf-8")
	(tmp_path / "AGENTS.md").write_text("contract\n", encoding="utf-8")
	asset = tmp_path / "assets" / "js" / "app.js"
	asset.parent.mkdir(parents=True)
	asset.write_bytes(b"x" * (100 * 1024 + 1))
	_init_and_stage(tmp_path, "quality-gate.toml", "AGENTS.md", "assets/js/app.js")
	asset.write_bytes(b"x")

	result = _run(tmp_path, "check", "--verbose")

	assert result.returncode == EXIT_UNCHECKED
	assert "web.component_1.javascript_budget: failed" in result.stdout
	assert "assets/js/app.js" in result.stdout
	assert "assets\\js\\app.js" not in result.stdout


def test_check_enforces_default_web_file_and_total_boundaries(tmp_path: Path) -> None:
	web = _web_component(javascript='["js/*.js"]', css='["css/*.css"]')
	(tmp_path / "quality-gate.toml").write_text(_manifest(web=web), encoding="utf-8")
	(tmp_path / "AGENTS.md").write_text("contract\n", encoding="utf-8")
	assets = {
		"assets/js/one.js": 100 * 1024,
		"assets/js/two.js": 100 * 1024,
		"assets/js/three.js": 50 * 1024,
		"assets/css/one.css": 50 * 1024,
		"assets/css/two.css": 25 * 1024,
		"assets/css/three.css": 25 * 1024,
	}
	for relative, size in assets.items():
		path = tmp_path / relative
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_bytes(b"x" * size)
	_init_and_stage(tmp_path, "quality-gate.toml", "AGENTS.md", *assets)

	boundary = _run(tmp_path, "check", "--verbose")
	assert boundary.returncode == EXIT_UNCHECKED
	assert "web.component_1.javascript_budget: passed" in boundary.stdout
	assert "web.component_1.css_budget: passed" in boundary.stdout

	(tmp_path / "assets" / "js" / "three.js").write_bytes(b"x" * (50 * 1024 + 1))
	(tmp_path / "assets" / "css" / "one.css").write_bytes(b"x" * (50 * 1024 + 1))
	assert _git(tmp_path, "add", "assets/js/three.js", "assets/css/one.css").returncode == 0

	above = _run(tmp_path, "check", "--verbose")
	assert above.returncode == EXIT_UNCHECKED
	assert "total JavaScript size 256001 bytes exceeds 256000 bytes" in above.stdout
	assert "total CSS size 102401 bytes exceeds 102400 bytes" in above.stdout
	assert "CSS file size 51201 bytes exceeds 51200 bytes" in above.stdout


def test_check_excludes_oversized_web_assets_only_when_explicitly_configured(
	tmp_path: Path,
) -> None:
	web = _web_component(javascript='["**/*.js"]', css="[]", exclude='["vendor/**"]')
	(tmp_path / "quality-gate.toml").write_text(_manifest(web=web), encoding="utf-8")
	(tmp_path / "AGENTS.md").write_text("contract\n", encoding="utf-8")
	owned = tmp_path / "assets" / "app.js"
	vendor = tmp_path / "assets" / "vendor" / "library.js"
	owned.parent.mkdir(parents=True)
	vendor.parent.mkdir(parents=True)
	owned.write_bytes(b"x")
	vendor.write_bytes(b"x" * (100 * 1024 + 1))
	_init_and_stage(
		tmp_path,
		"quality-gate.toml",
		"AGENTS.md",
		"assets/app.js",
		"assets/vendor/library.js",
	)

	excluded = _run(tmp_path, "check", "--verbose")
	assert excluded.returncode == EXIT_UNCHECKED
	assert "assets/vendor/library.js" not in excluded.stdout

	(tmp_path / "quality-gate.toml").write_text(
		_manifest(web=_web_component(javascript='["**/*.js"]', css="[]")),
		encoding="utf-8",
	)
	assert _git(tmp_path, "add", "quality-gate.toml").returncode == 0

	included = _run(tmp_path, "check", "--verbose")
	assert included.returncode == EXIT_UNCHECKED
	assert "assets/vendor/library.js" in included.stdout


def test_format_cli_requires_explicit_paths(tmp_path: Path) -> None:
	(tmp_path / "quality-gate.toml").write_text(_manifest(python=True), encoding="utf-8")
	result = _run(tmp_path, "format")

	assert result.returncode == EXIT_UNCHECKED
	assert "the following arguments are required: paths" in result.stderr


def test_validate_rejects_schema_one_and_migrate_is_read_only(tmp_path: Path) -> None:
	manifest = 'python = []\n\n[quality]\nschema = 1\npolicy = "quality-gate-v1"\n'
	path = tmp_path / "quality-gate.toml"
	path.write_text(manifest, encoding="utf-8")
	before = path.read_bytes()

	validation = _run(tmp_path, "validate")
	migration = _run(tmp_path, "migrate")

	assert validation.returncode == EXIT_UNCHECKED
	assert "migrate" in validation.stdout
	assert migration.returncode == 0
	assert "schema = 2" in migration.stdout
	assert path.read_bytes() == before
	proposal = tomllib.loads(migration.stdout.split("\n", 1)[1])
	assert proposal["quality"]["schema"] == SCHEMA_VERSION


def test_migrate_preserves_a_python_component(tmp_path: Path) -> None:
	legacy = '[quality]\nschema = 1\n\n[[python]]\npath = "app"\ntests = "tests"\n'
	(tmp_path / "quality-gate.toml").write_text(legacy, encoding="utf-8")

	result = _run(tmp_path, "migrate")
	proposal = tomllib.loads(result.stdout.split("\n", 1)[1])

	assert result.returncode == 0
	assert proposal["python"][0]["path"] == "app"
	assert proposal["python"][0]["test_paths"] == ["tests"]


def test_check_reports_quality_failure_with_exit_one(tmp_path: Path) -> None:
	manifest = tmp_path / "quality-gate.toml"
	manifest.write_text(_manifest(documents=["AGENTS.md", "docs/missing.md"]), encoding="utf-8")
	_init_and_stage(tmp_path, manifest.name)
	result = _run(tmp_path, "check")

	assert result.returncode == EXIT_QUALITY_FAILURE
	assert "manifest.documents: failed" in result.stdout
	assert "restore docs/missing.md" in result.stdout


def test_check_cli_blocks_when_python_runtime_is_unavailable(tmp_path: Path) -> None:
	(tmp_path / "quality-gate.toml").write_text(_manifest(python=True), encoding="utf-8")
	(tmp_path / "AGENTS.md").write_text("contract\n", encoding="utf-8")
	(tmp_path / "quality_gate").mkdir()
	(tmp_path / "tests").mkdir()
	_init_and_stage(tmp_path, "quality-gate.toml", "AGENTS.md")

	result = _run(tmp_path, "check")

	assert result.returncode == EXIT_UNCHECKED
	assert "unchecked" in result.stdout


def test_check_redacts_credential_shaped_required_document(tmp_path: Path) -> None:
	secret = "super-secret-token-123456"
	(tmp_path / "quality-gate.toml").write_text(
		_manifest(documents=[f"token={secret}.txt"]), encoding="utf-8"
	)
	_init_and_stage(tmp_path, "quality-gate.toml")
	result = _run(tmp_path, "check", "--verbose")

	assert result.returncode == EXIT_QUALITY_FAILURE
	assert secret not in result.stdout
	assert "<REDACTED>" in result.stdout


def test_check_verifies_the_staged_candidate_instead_of_the_worktree(tmp_path: Path) -> None:
	(tmp_path / "quality-gate.toml").write_text(
		_manifest(documents=["AGENTS.md", "candidate.md"]), encoding="utf-8"
	)
	(tmp_path / "AGENTS.md").write_text("contract\n", encoding="utf-8")
	(tmp_path / "candidate.md").write_text("staged content\n", encoding="utf-8")
	_init_and_stage(tmp_path, "quality-gate.toml", "AGENTS.md", "candidate.md")
	(tmp_path / "candidate.md").write_text("", encoding="utf-8")

	result = _run(tmp_path, "check", "--verbose")

	assert result.returncode == 0
	assert "manifest.documents: passed" in result.stdout


def test_check_rejects_a_staged_empty_candidate_even_when_worktree_is_repaired(
	tmp_path: Path,
) -> None:
	(tmp_path / "quality-gate.toml").write_text(
		_manifest(documents=["AGENTS.md", "candidate.md"]), encoding="utf-8"
	)
	(tmp_path / "AGENTS.md").write_text("contract\n", encoding="utf-8")
	(tmp_path / "candidate.md").write_text("valid\n", encoding="utf-8")
	_init_and_stage(tmp_path, "quality-gate.toml", "AGENTS.md", "candidate.md")
	(tmp_path / "candidate.md").write_text("", encoding="utf-8")
	assert _git(tmp_path, "add", "candidate.md").returncode == 0
	(tmp_path / "candidate.md").write_text("valid in worktree\n", encoding="utf-8")

	result = _run(tmp_path, "check")

	assert result.returncode == EXIT_QUALITY_FAILURE
	assert "candidate.md" in result.stdout


def test_check_uses_a_staged_rename_at_the_cli_boundary(tmp_path: Path) -> None:
	(tmp_path / "quality-gate.toml").write_text(
		_manifest(documents=["AGENTS.md", "moved.md"]), encoding="utf-8"
	)
	(tmp_path / "AGENTS.md").write_text("contract\n", encoding="utf-8")
	(tmp_path / "renamed.md").write_text("candidate\n", encoding="utf-8")
	_init_and_stage(tmp_path, "quality-gate.toml", "AGENTS.md", "renamed.md")
	assert _git(tmp_path, "mv", "renamed.md", "moved.md").returncode == 0

	result = _run(tmp_path, "check", "--verbose")

	assert result.returncode == 0
	assert "manifest.documents: passed" in result.stdout
