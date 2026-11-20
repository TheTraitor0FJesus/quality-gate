"""Fixtures and helpers for the split feature tests."""

from __future__ import annotations

import hashlib
import io
import os
import stat
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from quality_gate import snapshot as snapshot_module

SnapshotError = snapshot_module.SnapshotError

candidate_snapshot = snapshot_module.candidate_snapshot

EXPECTED_BATCH_ATTRIBUTE_PROCESSES = 1

EXPECTED_BATCH_OBJECT_PROCESSES = 1

BLOCKING_OUTPUT_FAILSAFE_SECONDS = 2


def _git(
	root: Path, *arguments: str, input_data: str | None = None
) -> subprocess.CompletedProcess[str]:
	environment = os.environ.copy()
	environment["GIT_CONFIG_GLOBAL"] = str(root / "missing-global-config")
	environment["GIT_CONFIG_NOSYSTEM"] = "1"
	return subprocess.run(
		["git", *arguments],
		cwd=root,
		env=environment,
		capture_output=True,
		input=input_data,
		text=True,
		check=False,
	)


def _init_and_stage(root: Path, *paths: str) -> None:
	assert _git(root, "init").returncode == 0
	assert _git(root, "add", *paths).returncode == 0


class _CapturedInput(io.BytesIO):
	def __init__(self) -> None:
		super().__init__()
		self.captured: bytes | None = None

	def close(self) -> None:
		if not self.closed:
			self.captured = self.getvalue()
		super().close()


class _FakeBatchProcess:
	def __init__(self, response: bytes | object) -> None:
		self.stdin = _CapturedInput()
		self.stdout = io.BytesIO(response) if isinstance(response, bytes) else response
		self.returncode: int | None = None
		self.killed = False
		self.waited = False
		self.wait_timeouts: list[float | None] = []

	def poll(self) -> int | None:
		return self.returncode

	def kill(self) -> None:
		self.killed = True
		self.returncode = -9
		release = getattr(self.stdout, "release", None)
		if release is not None:
			release()

	def wait(self, timeout: float | None = None) -> int:
		self.waited = True
		self.wait_timeouts.append(timeout)
		if self.returncode is None:
			self.returncode = 0
		return self.returncode


def _staged_entry(
	*,
	mode: bytes = b"100644",
	object_id: bytes = b"e69de29bb2d1d6434b8b29ae775ad8c2e48c5391",
	path: bytes = b"file.txt",
) -> snapshot_module._StagedEntry:
	return snapshot_module._StagedEntry(
		mode=mode,
		object_id=object_id,
		raw_path=path,
		relative_path=path.decode("utf-8"),
	)


def _materialization_context(
	root: Path, *, max_blob_bytes: int = 16
) -> snapshot_module._MaterializationContext:
	candidate_root = root / "candidate"
	candidate_root.mkdir()
	return snapshot_module._MaterializationContext(
		repository=root,
		candidate_root=candidate_root,
		index_path=root / "index",
		source_fingerprint=None,
		index_copy=root / "copied-index",
		timeout_seconds=1,
		max_blob_bytes=max_blob_bytes,
	)


def _fake_batch_process(
	monkeypatch: pytest.MonkeyPatch, response: bytes | object
) -> _FakeBatchProcess:
	process = _FakeBatchProcess(response)
	monkeypatch.setattr(snapshot_module.subprocess, "Popen", lambda *args, **kwargs: process)
	monkeypatch.setattr(snapshot_module._batch, "_windows_job", lambda child: None)
	monkeypatch.setattr(snapshot_module._batch, "_resume_windows", lambda child: None)
	real_stop = snapshot_module._batch._stop
	monkeypatch.setattr(
		snapshot_module._batch,
		"_stop",
		lambda child, job, own_tree, deadline: real_stop(child, None, False, deadline),
	)
	return process


class _BlockingOutput:
	def __init__(self) -> None:
		self.released = threading.Event()

	def read1(self, size: int) -> bytes:
		self.released.wait(BLOCKING_OUTPUT_FAILSAFE_SECONDS)
		return b""

	def read(self, size: int) -> bytes:
		return self.read1(size)

	def release(self) -> None:
		self.released.set()

	def close(self) -> None:
		self.release()


__all__ = [
	"BLOCKING_OUTPUT_FAILSAFE_SECONDS",
	"Callable",
	"EXPECTED_BATCH_ATTRIBUTE_PROCESSES",
	"EXPECTED_BATCH_OBJECT_PROCESSES",
	"Path",
	"SnapshotError",
	"_BlockingOutput",
	"_CapturedInput",
	"_FakeBatchProcess",
	"_fake_batch_process",
	"_git",
	"_init_and_stage",
	"_materialization_context",
	"_staged_entry",
	"candidate_snapshot",
	"hashlib",
	"io",
	"os",
	"pytest",
	"snapshot_module",
	"stat",
	"subprocess",
	"threading",
	"time",
]
