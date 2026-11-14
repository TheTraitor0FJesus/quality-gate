from __future__ import annotations

from pathlib import Path

import pytest
from cli_feature_support72 import _disposable_repository, _git, _invoke, _patch_runtime


def test_cli_rejects_python_component_at_repository_root(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
	root = _disposable_repository(tmp_path, "valid")
	manifest = root / "quality-gate.toml"
	manifest.write_text(
		manifest.read_text(encoding="utf-8").replace('path = "app"', 'path = "."'),
		encoding="utf-8",
	)
	assert _git(root, "add", ".").returncode == 0
	_patch_runtime(monkeypatch, root, [], None)
	result, output = _invoke(root, monkeypatch, capsys, verbose=True)

	assert result == 2, output
	assert "python[0].path: must be a repository-relative path" in output
