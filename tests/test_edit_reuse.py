"""Optional persistence failures cannot leak temporary result/key files."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from quality_gate.edit_reuse import ResultReuse


def test_cache_permission_failure_removes_temporary_files(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
	monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
	storage = ResultReuse(time.monotonic() + 10)

	def fail_permissions(path: Path, mode: int) -> None:
		raise OSError("Cache permissions unavailable")

	monkeypatch.setattr(Path, "chmod", fail_permissions)
	storage.remember("0" * 64, {"status": "passed"})
	assert list(storage.root.iterdir()) == []
	assert storage.lookup("0" * 64) is None
