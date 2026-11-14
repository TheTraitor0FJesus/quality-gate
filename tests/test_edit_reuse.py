"""Optional persistence failures cannot leak temporary result/key files."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from quality_gate.edit_reuse import ResultReuse
from quality_gate.verification_evidence import EngineIdentity, EvidenceProducer


def _evidence() -> tuple[dict[str, object], EngineIdentity, EvidenceProducer]:
	engine = EngineIdentity(form="wheel", version="4.6.0", digest="a" * 64)
	scope = {"check": "edit.batch.v1", "paths": ["src/example.py"]}
	identity = {
		"kind": "edit.batch.v1",
		"engine": engine.as_dict(),
		"scope": scope,
		"repository": "b" * 64,
	}
	producer = EvidenceProducer(
		operation="edit-check.validate",
		engine=engine,
		started_at="2026-11-02T10:00:00Z",
		completed_at="2026-11-02T10:00:00Z",
		duration_ms=0,
	)
	return identity, engine, producer


def test_cache_permission_failure_removes_temporary_files(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
	monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
	storage = ResultReuse(time.monotonic() + 10)
	identity, engine, producer = _evidence()

	def fail_permissions(path: Path, mode: int) -> None:
		raise OSError("Cache permissions unavailable")

	monkeypatch.setattr(Path, "chmod", fail_permissions)
	storage.remember(identity, {"status": "passed"}, producer=producer)
	assert list(storage.root.iterdir()) == []
	assert storage.lookup(identity, expected_engine=engine) is None
