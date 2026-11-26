"""Staged CLI contracts for pytest failure diagnostics."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_cli_python_contract import _disposable_repository, _git, _invoke
from test_cli_python_profile import _patch_real_pytest


@pytest.mark.parametrize("profile", [False, True])
@pytest.mark.parametrize("oversized_stderr", [False, True])
@pytest.mark.parametrize("exception_type", ["ImportError", "RuntimeError"])
@pytest.mark.parametrize("autoload", [False, True])
def test_gate_preserves_plugin_import_failure(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
	profile: bool,
	oversized_stderr: bool,
	exception_type: str,
	autoload: bool,
) -> None:
	root = _disposable_repository(tmp_path, "python-tests")
	if autoload:
		distribution = root / "gate_fixture_plugin-1.0.dist-info"
		distribution.mkdir()
		(distribution / "METADATA").write_text(
			"Name: gate-fixture-plugin\nVersion: 1.0\n", encoding="utf-8"
		)
		(distribution / "entry_points.txt").write_text(
			"[pytest11]\ngate-fixture-plugin = gate_fixture_plugin\n", encoding="utf-8"
		)
	else:
		(root / "pytest.ini").write_text(
			"[pytest]\naddopts = -p gate_fixture_plugin\n", encoding="utf-8"
		)
	prefix = (
		"try:\n    raise ValueError('ghp_' + 'EARLYCHAINSECRET' * 8000)\nexcept ValueError:\n    "
		if oversized_stderr
		else ""
	)
	(root / "gate_fixture_plugin.py").write_text(
		prefix + f"raise {exception_type}("
		"'fixture plugin startup broke: ghp_FINALPLUGINSECRET\\n"
		"Hint: retry after setup, token=secondaryPluginSecret')\n",
		encoding="utf-8",
	)
	(root / "app").mkdir(exist_ok=True)
	(root / "app" / "module.py").write_text("", encoding="utf-8")
	assert _git(root, "add", ".").returncode == 0
	_patch_real_pytest(monkeypatch, root)

	exit_code, output = _invoke(root, monkeypatch, capsys, verbose=True, profile=profile)

	assert exit_code == 1, output
	assert "python.component_1.pytest: failed" in output
	assert f"{exception_type}:" in output
	assert "fixture plugin startup broke: <REDACTED>" in output, output
	assert "Hint: retry after setup" in output
	assert "secondaryPluginSecret" not in output
	assert "FINALPLUGINSECRET" not in output
	assert "EARLYCHAINSECRET" not in output


@pytest.mark.parametrize("profile", [False, True])
@pytest.mark.parametrize(
	("scenario", "addopts", "expected_detail"),
	[
		("fake-import", "-q -s", "actual assertion failure"),
		("capture", "--show-capture=stderr", "response body detail"),
		("redaction", "--tb=long", "boundary diagnostics survived"),
		("internal", "", "internal hook broke"),
		("interrupted", "", "pytest asked to stop"),
		("missing-path", "absent-test-path", "file or directory not found: absent-test-path"),
		("bad-option", "--absent-pytest-option", "unrecognized arguments: --absent-pytest-option"),
		(
			"missing-plugin",
			"-p missing_gate_fixture_plugin",
			'ImportError: Error importing plugin "missing_gate_fixture_plugin"',
		),
		("usage-error", "", "consumer usage error"),
		("finish-error", "", "session finish hook broke"),
		("early-config", "", "early configuration failed"),
		("warnings-limit", "--max-warnings=0", "maximum allowed warnings exceeded"),
		("late-warning", "--max-warnings=0", "maximum allowed warnings exceeded"),
	],
)
def test_gate_preserves_pytest_failure_details(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
	profile: bool,
	scenario: str,
	addopts: str,
	expected_detail: str,
) -> None:
	root = _disposable_repository(tmp_path, "python-tests")
	(root / "pytest.ini").write_text(f"[pytest]\naddopts = {addopts}\n", encoding="utf-8")
	sources = {
		"fake-import": "def test_fails():\n    assert False, 'actual assertion failure'\n",
		"capture": (
			"import sys\n\nimport pytest\n\n"
			"@pytest.fixture\n"
			"def response():\n"
			"    yield\n"
			"    print('response cleanup ' + 'detail', file=sys.stderr)\n\n"
			"def test_fails(response):\n"
			"    print('stdout should ' + 'stay hidden')\n"
			"    print('response body ' + 'detail', file=sys.stderr)\n"
			"    assert False, 'bad response'\n"
		),
		"redaction": (
			"def test_fails():\n"
			"    value = 'prefix' * 1000 + 'ghp_' + 'KNOWNSECRETFRAGMENT' * 300\n"
			"    raise RuntimeError(value + ' boundary diagnostics survived')\n"
		),
		"interrupted": (
			"import pytest\n\n"
			"def test_fails_first():\n    assert False, 'failure before interruption'\n\n"
			"def test_stops():\n    pytest.exit('pytest asked to stop')\n"
		),
		"finish-error": (
			"def test_fails_before_hook():\n"
			"    print('captured before finish error')\n"
			"    assert False, 'assertion before finish error'\n"
		),
		"warnings-limit": (
			"import warnings\n\ndef test_app():\n"
			"    token = 'ghp_' + 'WARNINGSECRET' * 100\n"
			"    warnings.warn('warning threshold probe ' + token + ' ' + 'x' * 1000, "
			"stacklevel=1)\n"
		),
	}
	(root / "tests" / "test_app.py").write_text(
		sources.get(scenario, "def test_app(): pass\n"), encoding="utf-8"
	)
	conftests = {
		"fake-import": (
			"import sys\n"
			"sys.stdout.buffer.write(b'fake: No module named pytest\\n')\n"
			"sys.stdout.buffer.flush()\n"
		),
		"internal": (
			"def pytest_collection_modifyitems(items):\n"
			"    raise RuntimeError('internal hook broke')\n"
		),
		"usage-error": (
			"import pytest\n\n"
			"def pytest_collection_modifyitems(items):\n"
			"    token = 'ghp_' + 'USAGEERRORSECRET' * 5000\n"
			"    raise pytest.UsageError('consumer usage error: ' + token + ' actionable hint')\n"
		),
		"finish-error": (
			"import pytest\n\n"
			"@pytest.hookimpl(tryfirst=True)\n"
			"def pytest_sessionfinish(session):\n"
			"    raise RuntimeError('session finish hook broke')\n"
		),
		"early-config": (
			"def pytest_addoption(parser):\n"
			"    token = 'ghp_' + 'EARLYCONFIGSECRET' * 5000\n"
			"    raise RuntimeError('early configuration failed: ' + token + ' ' + 'x' * 4000)\n"
		),
		"late-warning": (
			"import warnings\n\n"
			"def pytest_terminal_summary(terminalreporter):\n"
			"    warnings.warn('late terminal warning', stacklevel=1)\n"
		),
	}
	if scenario in conftests:
		(root / "tests" / "conftest.py").write_text(conftests[scenario], encoding="utf-8")
	(root / "app").mkdir(exist_ok=True)
	(root / "app" / "module.py").write_text("", encoding="utf-8")
	assert _git(root, "add", ".").returncode == 0
	_patch_real_pytest(monkeypatch, root)

	exit_code, output = _invoke(root, monkeypatch, capsys, verbose=True, profile=profile)

	assert exit_code == 1, output
	assert "python.component_1.pytest: failed" in output
	assert expected_detail in output, output
	assert "restore the required tool" not in output
	assert "stdout should stay hidden" not in output
	additional_details = {
		"capture": ("response cleanup detail",),
		"finish-error": ("assertion before finish error", "captured before finish error"),
		"interrupted": ("failure before interruption",),
	}
	for detail in additional_details.get(scenario, ()):
		assert detail in output
	if scenario == "interrupted":
		assert "restore a collectable pytest suite" not in output
	if scenario == "usage-error":
		assert "actionable hint" in output
		assert "USAGEERRORSECRET" not in output
	if scenario == "warnings-limit":
		assert "2 passed, 1 warning" in output
		assert "test_app.py:5: warning threshold probe" in output, output
		assert "WARNINGSECRET" not in output
		assert "x" * 500 not in output
	if scenario == "late-warning":
		assert "2 passed, 1 warning" in output
		assert "conftest.py:4: late terminal warning" in output, output
	if scenario == "early-config":
		assert "Traceback (most recent call last)" in output
		assert "EARLYCONFIGSECRET" * 2 not in output
		assert "x" * 2_000 not in output
	if scenario == "redaction":
		# The source shows a synthetic building block; the exception must never expose its value.
		assert "KNOWNSECRETFRAGMENT" * 2 not in output
