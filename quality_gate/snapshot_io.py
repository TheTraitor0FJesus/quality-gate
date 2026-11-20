"""Run potentially blocking snapshot filesystem phases under Gate supervision."""

from __future__ import annotations

import json
import math
import os
import sys
import time
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .edit_protocol import EditError

if TYPE_CHECKING:
	from .snapshot import _MaterializationContext

_Action = Literal["initialize", "copy", "fingerprint", "materialize"]
_inherited_tree: ContextVar[bool] = ContextVar("snapshot_inherited_process_tree", default=False)
_WORKER = """
import sys
sys.path.insert(0, sys.argv[1])
from quality_gate.snapshot_io import _worker
_worker(sys.argv[2], sys.argv[3:])
"""


def positive_snapshot_limit(value: int, name: str) -> int:
	from .snapshot import SnapshotError

	if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
		raise SnapshotError(f"{name} must be a positive integer")
	return value


def validate_snapshot_deadline(deadline: float | None) -> float | None:
	from .snapshot import SnapshotError

	if deadline is None:
		return None
	if (
		isinstance(deadline, bool)
		or not isinstance(deadline, (int, float))
		or not math.isfinite(deadline)
	):
		raise SnapshotError("snapshot deadline must be a finite monotonic timestamp")
	return float(deadline)


def inherits_snapshot_worker_tree() -> bool:
	"""Git descendants of the file worker stay in its supervisor-owned tree."""
	return _inherited_tree.get()


def _worker(action: _Action, paths: list[str]) -> None:
	from .snapshot import SnapshotError, _index_fingerprint
	from .snapshot_batch import _copy_index

	path = Path(paths[0])
	try:
		if action == "initialize":
			path.mkdir()
			(path / "candidate").mkdir()
			fingerprint = None
		elif action == "copy":
			fingerprint = _copy_index(path, Path(paths[1]))
		elif action == "fingerprint":
			fingerprint = _index_fingerprint(path)
		elif action == "materialize":
			_materialize_in_worker(paths)
			fingerprint = None
		else:
			raise ValueError("unknown snapshot filesystem phase")
	except SnapshotError as error:
		sys.stdout.write(json.dumps({"error": str(error)}))
	else:
		sys.stdout.write(
			json.dumps(
				{
					"fingerprint": None
					if fingerprint is None
					else (*fingerprint[:3], fingerprint[3].hex())
				}
			)
		)


def _materialize_in_worker(arguments: list[str]) -> None:
	from .snapshot import _MaterializationContext, _materialize_candidate

	repository, candidate_root, index_path, index_copy = map(Path, arguments[:4])
	timeout, max_blob_bytes, deadline, *raw_fingerprint = arguments[4:]
	source_fingerprint = (
		(
			int(raw_fingerprint[0]),
			int(raw_fingerprint[1]),
			int(raw_fingerprint[2]),
			bytes.fromhex(raw_fingerprint[3]),
		)
		if raw_fingerprint
		else None
	)
	token = _inherited_tree.set(True)
	try:
		_materialize_candidate(
			_MaterializationContext(
				repository=repository,
				candidate_root=candidate_root,
				index_path=index_path,
				source_fingerprint=source_fingerprint,
				index_copy=index_copy,
				timeout_seconds=int(timeout),
				max_blob_bytes=int(max_blob_bytes),
				deadline=float(deadline) if deadline else None,
			)
		)
	finally:
		_inherited_tree.reset(token)


def supervise_materialization(context: _MaterializationContext) -> None:
	fingerprint = context.source_fingerprint
	identity = () if fingerprint is None else (*map(str, fingerprint[:3]), fingerprint[3].hex())
	supervised_snapshot_io(
		"materialize",
		(
			context.repository,
			context.candidate_root,
			context.index_path,
			context.index_copy,
			str(context.timeout_seconds),
			str(context.max_blob_bytes),
			"" if context.deadline is None else str(context.deadline),
			*identity,
		),
		context.deadline,
	)


def supervised_snapshot_io(
	action: _Action,
	arguments: tuple[Path | str, ...],
	deadline: float | None,
) -> tuple[int, int, int, bytes] | None:
	from .operation import current_operation
	from .snapshot import SnapshotError

	operation = current_operation()
	if operation is None:
		raise RuntimeError("snapshot filesystem worker requires a Gate operation")
	end = operation.deadline if deadline is None else min(deadline, operation.deadline)
	remaining = end - time.monotonic()
	if remaining <= 0:
		raise SnapshotError("candidate snapshot exceeded its operation deadline")
	try:
		exit_code, output, _ = operation.run_process(
			[
				sys.executable,
				"-I",
				"-S",
				"-B",
				"-c",
				_WORKER,
				os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
				action,
				*map(str, arguments),
			],
			root=operation.root,
			environment=dict(os.environ),
			timeout_seconds=remaining,
			limit=4096,
			check_execution=False,
		)
	except (EditError, OSError) as error:
		raise SnapshotError("candidate snapshot filesystem phase could not complete") from error
	if exit_code:
		raise SnapshotError("candidate snapshot filesystem phase failed")
	report = json.loads(output)
	if "error" in report:
		raise SnapshotError(report["error"])
	fingerprint = report["fingerprint"]
	if fingerprint is None:
		return None
	size, modified, changed, digest = fingerprint
	return int(size), int(modified), int(changed), bytes.fromhex(digest)
