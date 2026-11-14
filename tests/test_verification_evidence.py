"""The provider reuses only complete, authenticated matching evidence."""

from __future__ import annotations

import json
import hmac
import time
import zipfile
from pathlib import Path

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
		"files": [
			{"path": "src/example.py", "mode": 0o644, "sha256": "c" * 64}
		],
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
		outcome={"status": "passed", "lint_status": "passed", "format_status": "passed", "findings": []},
	)

	assert store.lookup(identity, expected_engine=wheel_engine) is None


def test_provider_does_not_publish_partial_file_result(tmp_path: Path) -> None:
	engine = _engine()
	identity = _identity(engine)
	store = EvidenceStore(time.monotonic() + 10, cache_root=tmp_path)
	store.publish(identity, producer=_producer(engine), scope=_scope(), outcome={"status": "passed"})

	assert store.lookup(identity, expected_engine=engine) is None


def test_provider_rejects_authenticated_partial_file_record(tmp_path: Path) -> None:
	engine = _engine()
	identity = _identity(engine)
	store = EvidenceStore(time.monotonic() + 10, cache_root=tmp_path)
	store.publish(
		identity,
		producer=_producer(engine),
		scope=_scope(),
		outcome={"status": "passed", "lint_status": "passed", "format_status": "passed", "findings": []},
	)
	root = tmp_path / "verification-evidence-v1"
	path = root / f"{identity_digest(identity)}.json"
	record = json.loads(path.read_text(encoding="utf-8"))
	record["payload"]["outcome"] = {"status": "passed"}
	payload = json.dumps(record["payload"], sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("ascii")
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
		outcome={"status": "passed", "lint_status": "passed", "format_status": "passed", "findings": []},
	)
	path = tmp_path / "verification-evidence-v1" / f"{identity_digest(identity)}.json"
	record = json.loads(path.read_text(encoding="utf-8"))
	record["mac"] = "0" * 64
	path.write_text(json.dumps(record), encoding="utf-8")

	assert store.lookup(identity, expected_engine=engine) is None


def test_provider_does_not_read_legacy_edit_batch_records(tmp_path: Path) -> None:
	engine = _engine()
	identity = _identity(engine)
	legacy_root = tmp_path / "edit-results-v1"
	legacy_root.mkdir()
	(legacy_root / "00.json").write_text(
		json.dumps(
			{"payload": {"identity": identity_digest(identity), "result": {"status": "passed"}}}
		),
		encoding="utf-8",
	)
	store = EvidenceStore(time.monotonic() + 10, cache_root=tmp_path)

	assert store.lookup(identity, expected_engine=engine) is None


def test_current_engine_identity_separates_source_and_wheel_bytes(tmp_path: Path) -> None:
	source_root = tmp_path / "checkout"
	(source_root / ".git").mkdir(parents=True)
	(source_root / "pyproject.toml").write_text("[project]\nname='quality-gate'\n", encoding="utf-8")
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
