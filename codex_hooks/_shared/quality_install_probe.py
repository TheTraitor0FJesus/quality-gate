"""Verify only the selected Python quality engines before managed activation."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from time import monotonic
from typing import Any

from _shared.quality_events import content_record, safe_path
from _shared.quality_settings import DEFAULT_BUDGET_SECONDS, settings_operation


def _probe_typing(settings: dict[str, Any], root: Path) -> str | None:
	"""Check an explicitly prepared project; activation never provisions its venv."""
	from _shared.quality_background import BackgroundSession, cancel_session, collect, schedule
	from _shared.quality_background_state import CLEANUP_SECONDS, MAX_DRAIN_SECONDS

	root = Path(os.path.abspath(root))
	path, _ = safe_path("probe.py", root)
	current = content_record(path, root, "modified")
	with tempfile.TemporaryDirectory(prefix="codex-types-activation-") as directory:
		state_dir = Path(directory)
		settings_path = state_dir / "probe-settings.json"
		settings_operation(settings_path, "init")
		settings_operation(settings_path, "update", settings)
		session = BackgroundSession(settings_path, state_dir, "activation-probe")
		reason = None
		try:
			queued = schedule(
				root,
				[{"path": "probe.py", "sha256": current["sha256"]}],
				session=session,
				deadline=monotonic() + DEFAULT_BUDGET_SECONDS,
			)
			if queued["status"] != "pending":
				reason = "typing_probe_unchecked"
			else:
				reports = collect(
					session=session,
					deadline=monotonic() + MAX_DRAIN_SECONDS,
					drain=True,
					include_clean=True,
				)
				if len(reports) != 1 or reports[0]["status"] != "passed":
					reason = "typing_probe_unchecked"
		finally:
			if cancel_session(
				session=session,
				deadline=monotonic() + CLEANUP_SECONDS,
			):
				reason = "typing_probe_cleanup"
		return reason


def _probe_content_readers(root: Path, settings: dict[str, Any], deadline: float) -> str | None:
	for name in ("secrets", "integrity"):
		if name not in settings["enabled"]:
			continue
		from file_integrity.check import check_files as integrity_reader
		from secret_scan.check import check_files as secret_reader

		reader = secret_reader if name == "secrets" else integrity_reader
		final_digest = hashlib.sha256((root / "probe.py").read_bytes()).hexdigest()
		result = reader(root, [{"path": "probe.py", "sha256": final_digest}], settings, deadline)
		if result["status"] != "passed":
			return "secret_probe_unchecked" if name == "secrets" else "integrity_probe_unchecked"
	return None


def probe(settings: dict[str, Any], *, typing_probe_root: Path | None = None) -> str | None:
	"""Return a fixed recovery reason, or None when every selected engine passes."""
	enabled = set(settings["enabled"])
	if "types" in enabled and typing_probe_root is None:
		return "typing_probe_required"
	required = ["provider_release", "baseline_release"]
	if "ruff" in enabled:
		required.extend(("baseline_python", "baseline_python_version"))
	if any(not settings.get(name) for name in required):
		return "activation_prerequisites_missing"
	deadline = monotonic() + settings.get("budget_seconds", DEFAULT_BUDGET_SECONDS)
	with tempfile.TemporaryDirectory(prefix="codex-ruff-activation-") as directory:
		root = Path(directory).resolve()
		content = b"VALUE: int = 1\n"
		(root / "probe.py").write_bytes(content)
		files = [{"path": "probe.py", "sha256": hashlib.sha256(content).hexdigest()}]
		if "ruff" in enabled:
			from python_quality.check import check_files as check_ruff

			ruff_result = check_ruff(root, files, settings, deadline)
			if ruff_result["status"] != "passed":
				return "provider_probe_unchecked"
		failure = _probe_content_readers(root, settings, deadline)
		if failure is not None:
			return failure
		if "types" in enabled and typing_probe_root is not None:
			return _probe_typing(settings, typing_probe_root)
	return None
