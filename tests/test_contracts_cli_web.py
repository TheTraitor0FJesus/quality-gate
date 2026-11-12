"""CLI contract tests for web assets and supplemental tests."""

from __future__ import annotations

from pathlib import Path

from test_contracts_cli import EXIT_UNCHECKED, _git, _init_and_stage, _manifest, _run

from quality_gate.contracts import (
	MAX_SUPPLEMENTAL_TARGET_MATCHES,
	MAX_SUPPLEMENTAL_TESTS,
	load_manifest,
)

EXPECTED_JAVASCRIPT_FILE_KIB = 100
EXPECTED_CSS_FILE_KIB = 50
EXPECTED_JAVASCRIPT_TOTAL_KIB = 250
EXPECTED_CSS_TOTAL_KIB = 100


def _web_component(
	*,
	name: str = "frontend",
	root: str = "assets",
	javascript: str = '["js/**/*.js"]',
	css: str = '["css/**/*.css"]',
	exclude: str = "[]",
	limits: str = "",
) -> str:
	return f"""
[[web]]
name = "{name}"
root = "{root}"
javascript = {javascript}
css = {css}
exclude = {exclude}
{limits}
"""


def _supplemental_test(
	*,
	name: str = "node-functional",
	runner: str = "node-test",
	targets: str = '["tests/node/**/*.test.js"]',
) -> str:
	return f'''
[[supplemental_tests]]
name = "{name}"
runner = "{runner}"
targets = {targets}
'''


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
