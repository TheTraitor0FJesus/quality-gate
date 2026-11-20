"""The provider reuses only complete, authenticated matching evidence."""

from __future__ import annotations

import hmac
import json
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from quality_gate import verification_evidence
from quality_gate.edit_reuse import ResultReuse
from quality_gate.verification_evidence import (
	EngineIdentity,
	EvidenceProducer,
	EvidenceStore,
	engine_identity,
	identity_digest,
)


def _engine(form: str = "source") -> EngineIdentity:
	return EngineIdentity(form=form, version="4.6.0", digest="a" * 64)


def _scope() -> dict[str, object]:
	return {
		"check": "ruff.file-local.v1",
		"files": [{"path": "src/example.py", "mode": 0o644, "sha256": "c" * 64}],
	}


def _identity(engine: EngineIdentity) -> dict[str, object]:
	return {
		"kind": "ruff.file-local.v1",
		"engine": engine.as_dict(),
		"repository": "b" * 64,
		"scope": _scope(),
		"tool": {"name": "ruff", "version": "0.16.4", "digest": "d" * 64},
		"policy": {"release": "v4.6.0", "digest": "e" * 64},
	}


def _producer(engine: EngineIdentity) -> EvidenceProducer:
	return EvidenceProducer(
		operation="quality-gate.check",
		engine=engine,
		started_at="2026-11-02T10:00:00Z",
		completed_at="2026-11-02T10:00:00Z",
		duration_ms=0,
	)


def test_provider_reuses_a_complete_failed_file_result(tmp_path: Path) -> None:
	engine = _engine()
	identity = _identity(engine)
	outcome = {
		"status": "failed",
		"lint_status": "failed",
		"format_status": "passed",
		"findings": [{"rule": "F401", "path": "src/example.py", "line": 1, "column": 1}],
	}
	store = EvidenceStore(time.monotonic() + 10, cache_root=tmp_path)

	store.publish(
		identity,
		producer=_producer(engine),
		scope=_scope(),
		outcome=outcome,
	)

	hit = store.lookup(identity, expected_engine=engine)
	assert hit is not None
	assert hit.outcome == outcome
	assert hit.producer.operation == "quality-gate.check"
	assert hit.scope["check"] == "ruff.file-local.v1"


def test_provider_does_not_reuse_when_engine_form_differs(tmp_path: Path) -> None:
	source_engine = _engine("source")
	wheel_engine = _engine("wheel")
	identity = _identity(source_engine)
	store = EvidenceStore(time.monotonic() + 10, cache_root=tmp_path)
	store.publish(
		identity,
		producer=_producer(source_engine),
		scope=_scope(),
		outcome={
			"status": "passed",
			"lint_status": "passed",
			"format_status": "passed",
			"findings": [],
		},
	)

	assert store.lookup(identity, expected_engine=wheel_engine) is None


def test_provider_does_not_publish_partial_file_result(tmp_path: Path) -> None:
	engine = _engine()
	identity = _identity(engine)
	store = EvidenceStore(time.monotonic() + 10, cache_root=tmp_path)
	store.publish(
		identity, producer=_producer(engine), scope=_scope(), outcome={"status": "passed"}
	)

	assert store.lookup(identity, expected_engine=engine) is None


def test_provider_rejects_authenticated_partial_file_record(tmp_path: Path) -> None:
	engine = _engine()
	identity = _identity(engine)
	store = EvidenceStore(time.monotonic() + 10, cache_root=tmp_path)
	store.publish(
		identity,
		producer=_producer(engine),
		scope=_scope(),
		outcome={
			"status": "passed",
			"lint_status": "passed",
			"format_status": "passed",
			"findings": [],
		},
	)
	root = tmp_path / "verification-evidence-v1"
	path = root / f"{identity_digest(identity)}.json"
	record = json.loads(path.read_text(encoding="utf-8"))
	record["payload"]["outcome"] = {"status": "passed"}
	payload = json.dumps(
		record["payload"], sort_keys=True, ensure_ascii=True, separators=(",", ":")
	).encode("ascii")
	record["mac"] = hmac.digest((root / "key").read_bytes(), payload, "sha256").hex()
	path.write_text(json.dumps(record), encoding="utf-8")

	assert store.lookup(identity, expected_engine=engine) is None


def test_provider_discards_an_invalid_authenticator(tmp_path: Path) -> None:
	engine = _engine()
	identity = _identity(engine)
	store = EvidenceStore(time.monotonic() + 10, cache_root=tmp_path)
	store.publish(
		identity,
		producer=_producer(engine),
		scope=_scope(),
		outcome={
			"status": "passed",
			"lint_status": "passed",
			"format_status": "passed",
			"findings": [],
		},
	)
	path = tmp_path / "verification-evidence-v1" / f"{identity_digest(identity)}.json"
	record = json.loads(path.read_text(encoding="utf-8"))
	record["mac"] = "0" * 64
	path.write_text(json.dumps(record), encoding="utf-8")

	assert store.lookup(identity, expected_engine=engine) is None


def test_provider_does_not_read_legacy_edit_batch_records(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
	monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
	engine = _engine()
	old_identity = {
		"engine": engine.version,
		"phase": "legacy",
		"root": str(tmp_path),
		"files": [{"path": "src/example.py", "sha256": "c" * 64}],
		"selections": [],
		"inputs": [],
		"imports": [],
		"artifacts": [],
	}
	cache = ResultReuse(time.monotonic() + 10)
	legacy_root = cache.root.with_name("edit-results-v1")
	legacy_root.mkdir(parents=True)
	key = b"k" * 32
	(legacy_root / "key").write_bytes(key)
	digest = identity_digest(old_identity)
	payload = {"identity": digest, "result": {"status": "passed"}}
	canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
	legacy_path = legacy_root / f"{int(digest[:8], 16) % 16:02d}.json"
	legacy_path.write_text(
		json.dumps({"payload": payload, "mac": hmac.digest(key, canonical, "sha256").hex()}),
		encoding="ascii",
	)
	current = old_identity | {"kind": "edit.batch.v1", "engine": engine.as_dict()}
	assert cache.lookup(current, expected_engine=engine) is None
	assert legacy_path.is_file()


def test_concurrent_publications_keep_authenticated_records_within_capacity(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	monkeypatch.setattr(verification_evidence, "_MAX_RECORDS", 3)
	engine = _engine()
	identities = [_identity(engine) | {"repository": f"{index:064x}"} for index in range(6)]
	outcome = {
		"status": "passed",
		"lint_status": "passed",
		"format_status": "passed",
		"findings": [],
	}
	store = EvidenceStore(time.monotonic() + 10, cache_root=tmp_path)

	def publish(identity: dict[str, object]) -> None:
		store.publish(identity, producer=_producer(engine), scope=_scope(), outcome=outcome)

	for identity in identities[:3]:
		publish(identity)
	assert all(store.lookup(identity, expected_engine=engine) for identity in identities[:3])
	with ThreadPoolExecutor(max_workers=3) as executor:
		list(executor.map(publish, identities[3:]))
	paths = set(store.root.glob("*.json"))
	assert len(paths) == 3
	assert len(paths & {store.root / f"{identity_digest(item)}.json" for item in identities[3:]})
	for identity in identities:
		hit = store.lookup(identity, expected_engine=engine)
		assert (hit is not None) == (store.root / f"{identity_digest(identity)}.json" in paths)
		if hit is not None:
			assert hit.outcome == outcome


def test_current_engine_identity_separates_source_and_wheel_bytes(tmp_path: Path) -> None:
	source_root = tmp_path / "checkout"
	(source_root / ".git").mkdir(parents=True)
	(source_root / "pyproject.toml").write_text(
		"[project]\nname='quality-gate'\n", encoding="utf-8"
	)
	source_package = source_root / "quality_gate"
	source_package.mkdir()
	(source_package / "runner.py").write_text("ENGINE = 1\n", encoding="utf-8")
	wheel_package = tmp_path / "site-packages" / "quality_gate"
	wheel_package.mkdir(parents=True)
	(wheel_package / "runner.py").write_text("ENGINE = 1\n", encoding="utf-8")

	source = engine_identity(source_package, version="4.6.0")
	wheel = engine_identity(wheel_package, version="4.6.0")

	assert source is not None and source.form == "source"
	assert wheel is not None and wheel.form == "wheel"
	assert source.digest != wheel.digest


def test_current_engine_identity_supports_an_unextracted_wheel(tmp_path: Path) -> None:
	wheel = tmp_path / "engine.whl"
	with zipfile.ZipFile(wheel, "w") as archive:
		archive.writestr("quality_gate/__init__.py", "VERSION = 1\n")

	identity = engine_identity(wheel / "quality_gate", version="4.6.0")

	assert identity is not None
	assert identity.form == "wheel"
