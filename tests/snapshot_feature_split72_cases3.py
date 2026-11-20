"""Feature test partition."""

from __future__ import annotations

import sys

from snapshot_feature_split72_support import (
	BLOCKING_OUTPUT_FAILSAFE_SECONDS as BLOCKING_OUTPUT_FAILSAFE_SECONDS,
)
from snapshot_feature_split72_support import (
	EXPECTED_BATCH_ATTRIBUTE_PROCESSES as EXPECTED_BATCH_ATTRIBUTE_PROCESSES,
)
from snapshot_feature_split72_support import (
	EXPECTED_BATCH_OBJECT_PROCESSES as EXPECTED_BATCH_OBJECT_PROCESSES,
)
from snapshot_feature_split72_support import (
	Callable as Callable,
)
from snapshot_feature_split72_support import (
	Path as Path,
)
from snapshot_feature_split72_support import (
	SnapshotError as SnapshotError,
)
from snapshot_feature_split72_support import (
	_BlockingOutput as _BlockingOutput,
)
from snapshot_feature_split72_support import (
	_CapturedInput as _CapturedInput,
)
from snapshot_feature_split72_support import (
	_fake_batch_process as _fake_batch_process,
)
from snapshot_feature_split72_support import (
	_FakeBatchProcess as _FakeBatchProcess,
)
from snapshot_feature_split72_support import (
	_git as _git,
)
from snapshot_feature_split72_support import (
	_init_and_stage as _init_and_stage,
)
from snapshot_feature_split72_support import (
	_materialization_context as _materialization_context,
)
from snapshot_feature_split72_support import (
	_staged_entry as _staged_entry,
)
from snapshot_feature_split72_support import (
	candidate_snapshot as candidate_snapshot,
)
from snapshot_feature_split72_support import (
	hashlib as hashlib,
)
from snapshot_feature_split72_support import (
	io as io,
)
from snapshot_feature_split72_support import (
	os as os,
)
from snapshot_feature_split72_support import (
	pytest as pytest,
)
from snapshot_feature_split72_support import (
	snapshot_module as snapshot_module,
)
from snapshot_feature_split72_support import (
	stat as stat,
)
from snapshot_feature_split72_support import (
	subprocess as subprocess,
)
from snapshot_feature_split72_support import (
	threading as threading,
)
from snapshot_feature_split72_support import (
	time as time,
)

from quality_gate import snapshot_batch


def test_candidate_snapshot_process_count_is_fixed_discovery_plus_two_batches(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	small = tmp_path / "small"
	large = tmp_path / "large"
	small.mkdir()
	large.mkdir()
	path_counts = ((small, 2), (large, 256))
	for root, count in path_counts:
		assert _git(root, "init").returncode == 0
		for index in range(count):
			(root / f"file-{index:04}.txt").write_bytes(f"content {index}\n".encode())
		assert _git(root, "add", "--", ".").returncode == 0

	monkeypatch.delenv("GIT_INDEX_FILE", raising=False)
	real_popen: Callable[..., subprocess.Popen[bytes]] = snapshot_module.subprocess.Popen
	processes: list[tuple[str, ...]] = []

	def record(command: object) -> None:
		if isinstance(command, (list, tuple)) and command and command[0] == "git":
			processes.append(tuple(str(argument) for argument in command[1:]))

	def count_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
		record(args[0] if args else kwargs.get("args"))
		return real_popen(*args, **kwargs)

	monkeypatch.setattr(snapshot_module.subprocess, "Popen", count_popen)
	process_counts: list[tuple[int, int, int, int]] = []
	for root, path_count in path_counts:
		processes.clear()
		expected_paths = {f"file-{index:04}.txt" for index in range(path_count)}
		with candidate_snapshot(root) as snapshot:
			assert {
				path.relative_to(snapshot.root).as_posix()
				for path in snapshot.root.rglob("*")
				if path.is_file() or path.is_symlink()
			} == expected_paths
			assert all(
				(snapshot.root / path).read_bytes() == f"content {index}\n".encode()
				for index, path in enumerate(sorted(expected_paths))
			)
		attribute_count = sum(command[:1] == ("check-attr",) for command in processes)
		object_count = sum(command[:1] == ("cat-file",) for command in processes)
		fixed_count = len(processes) - attribute_count - object_count
		process_counts.append((path_count, fixed_count, attribute_count, object_count))
	assert process_counts[0][1] == process_counts[1][1]
	assert all(
		attributes == EXPECTED_BATCH_ATTRIBUTE_PROCESSES
		and objects == EXPECTED_BATCH_OBJECT_PROCESSES
		for _, _, attributes, objects in process_counts
	)


@pytest.mark.parametrize(
	("response", "message"),
	[
		(b"fale.txt\0filter\0unspecified\0", "malformed candidate attribute"),
		(b"file.txt\0filter", "truncated candidate batch"),
		(b"file.txt\0filter\0" + b"x" * 1025 + b"\0", "oversized candidate batch field"),
		(
			b"file.txt\0filter\0unspecified\0extra\0",
			"unexpected candidate attribute response",
		),
	],
)
def test_candidate_snapshot_reaps_process_after_malformed_attribute_response(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, response: bytes, message: str
) -> None:
	process = _fake_batch_process(monkeypatch, response)
	entry = _staged_entry()

	with pytest.raises(SnapshotError, match=message):
		snapshot_batch._reject_filtered_paths(tmp_path, (entry,), tmp_path / "index", 1)

	assert process.killed
	assert process.waited
	assert process.wait_timeouts and all(timeout is not None for timeout in process.wait_timeouts)
	assert process.stdin.captured == b"file.txt\0"


@pytest.mark.parametrize(
	("response_factory", "max_blob_bytes", "message"),
	[
		(lambda oid: oid + b" missing\n", 16, "staged blob object"),
		(lambda oid: b"f" * 40 + b" blob 0\n\n", 16, "unexpected candidate object"),
		(lambda oid: oid + b" tree 0\n\n", 16, "non-blob candidate object"),
		(lambda oid: oid + b" blob -1\n\n", 16, "invalid candidate object length"),
		(lambda oid: oid + b" blob 3\nabc\n", 2, "exceeds"),
		(lambda oid: oid + b" blob 3\nab", 16, "truncated candidate batch"),
		(lambda oid: oid + b" blob 3\nabc\n", 16, "inconsistent with its identity"),
		(lambda oid: oid + b" blob 0\nX", 16, "malformed candidate object framing"),
		(lambda oid: oid + b" blob 0\n\nextra", 16, "unexpected candidate object data"),
	],
)
def test_candidate_snapshot_reaps_process_after_invalid_object_response(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	response_factory: Callable[[bytes], bytes],
	max_blob_bytes: int,
	message: str,
) -> None:
	entry = _staged_entry()
	process = _fake_batch_process(monkeypatch, response_factory(entry.object_id))
	context = _materialization_context(tmp_path, max_blob_bytes=max_blob_bytes)

	with pytest.raises(SnapshotError, match=message):
		snapshot_batch._read_git_blobs(context, (entry,), tmp_path / "objects")

	assert process.killed
	assert process.waited
	assert process.wait_timeouts and all(timeout is not None for timeout in process.wait_timeouts)
	assert process.stdin.captured == entry.object_id + b"\n"


def test_candidate_snapshot_requests_repeated_object_only_once(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	assert _git(tmp_path, "init").returncode == 0
	object_id = (
		_git(tmp_path, "hash-object", "--stdin", input_data="shared").stdout.strip().encode("ascii")
	)
	entry = _staged_entry(object_id=object_id)
	second = _staged_entry(object_id=object_id, path=b"duplicate.txt")
	process = _fake_batch_process(monkeypatch, entry.object_id + b" blob 6\nshared\n")
	context = _materialization_context(tmp_path)
	object_directory = tmp_path / "objects"

	object_sizes = snapshot_batch._read_git_blobs(context, (entry, second), object_directory)

	assert object_sizes == {entry.object_id: 6}
	assert process.stdin.captured == entry.object_id + b"\n"
	assert (object_directory / entry.object_id.decode("ascii")).read_bytes() == b"shared"
	assert process.waited
	assert process.wait_timeouts and all(timeout is not None for timeout in process.wait_timeouts)
	assert not process.killed


def test_candidate_snapshot_rejects_aggregate_bound_with_valid_blob_size(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Two materialized copies and a cached blob exceed the total budget."""
	max_blob_bytes = 16
	path_count = 2
	monkeypatch.setattr(snapshot_module, "_MAX_SNAPSHOT_DATA_BYTES", 40)
	entry = _staged_entry()
	entries = tuple(
		_staged_entry(path=f"copy-{index}.txt".encode("ascii")) for index in range(path_count)
	)
	large_object = entry.object_id + b" blob " + str(max_blob_bytes).encode("ascii") + b"\n"
	process = _fake_batch_process(monkeypatch, large_object)
	context = _materialization_context(tmp_path, max_blob_bytes=max_blob_bytes)

	with pytest.raises(SnapshotError, match="aggregate snapshot limit"):
		snapshot_batch._read_git_blobs(context, entries, tmp_path / "objects")

	assert process.killed
	assert process.waited
	assert process.wait_timeouts and all(timeout is not None for timeout in process.wait_timeouts)
	assert process.stdin.captured == entry.object_id + b"\n"


def test_candidate_snapshot_reaps_batch_process_at_deadline(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	process = _fake_batch_process(monkeypatch, _BlockingOutput())

	with pytest.raises(SnapshotError, match="before its deadline"):
		snapshot_batch._reject_filtered_paths(tmp_path, (_staged_entry(),), tmp_path / "index", 1)

	assert process.killed
	assert process.waited
	assert process.wait_timeouts and all(timeout is not None for timeout in process.wait_timeouts)


def test_candidate_snapshot_stops_descendant_holding_real_batch_pipe(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	real_popen = subprocess.Popen
	launched: list[subprocess.Popen[bytes]] = []
	script = (
		"import subprocess, sys; "
		"subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(8)'])"
	)

	def launch(command: list[str], **options: object) -> subprocess.Popen[bytes]:
		assert command[0] == "git"
		process = real_popen([sys.executable, "-B", "-c", script], **options)
		launched.append(process)
		return process

	monkeypatch.setattr(subprocess, "Popen", launch)
	started = time.monotonic()
	with pytest.raises(SnapshotError, match="before its deadline"):
		snapshot_batch._reject_filtered_paths(
			tmp_path, (_staged_entry(),), tmp_path / "index", 6, deadline=started + 1
		)
	assert time.monotonic() - started < 4
	assert len(launched) == 1 and launched[0].poll() is not None


@pytest.mark.parametrize("location", ["initialize", "objects", "candidate"])
def test_snapshot_materialization_timeout_runs_owned_cleanup(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
	from contextlib import nullcontext

	from quality_gate import runner
	from quality_gate.operation import GateOperation
	from quality_gate.snapshot_io import _WORKER

	(tmp_path / "tracked.txt").write_bytes(b"staged\n")
	_init_and_stage(tmp_path, "tracked.txt")
	reached = tmp_path / "materialization-started"
	injection = f"""
from pathlib import Path
from time import sleep
real_open = Path.open
def stall_write(path, mode='r', *args, **kwargs):
    if path.parent.name == {location!r} and mode == 'wb':
        with real_open(Path({str(reached)!r}), 'wb') as marker:
            marker.write(b'entered the blocking call')
        sleep(30)
    return real_open(path, mode, *args, **kwargs)
Path.open = stall_write
"""
	if location == "initialize":
		injection = f"""
from pathlib import Path
from time import sleep
real_mkdir = Path.mkdir
def stall_mkdir(path, *args, **kwargs):
    if path.name.startswith('quality-gate-candidate-'):
        Path({str(reached)!r}).write_bytes(b'entered the blocking call')
        sleep(30)
    return real_mkdir(path, *args, **kwargs)
Path.mkdir = stall_mkdir
"""
	run_process = GateOperation.run_process

	def stall_materialization(
		gate: GateOperation, command: list[str], **options: object
	) -> tuple[int, bytes, bytes]:
		if _WORKER in command:
			position = command.index(_WORKER)
			action = "initialize" if location == "initialize" else "materialize"
			if command[position + 2] == action:
				gate.deadline = time.monotonic() + 4
				command = [*command]
				command[position] = injection + _WORKER
		return run_process(gate, command, **options)

	monkeypatch.setattr(runner, "gate_admission", nullcontext)
	monkeypatch.setattr(GateOperation, "run_process", stall_materialization)
	started = time.monotonic()
	verdict = runner.check(tmp_path)
	assert time.monotonic() - started < 20
	assert reached.read_bytes() == b"entered the blocking call"
	assert verdict.exit_code == runner.EXIT_UNCHECKED
	assert verdict.operation is not None and verdict.operation.timed_out
	assert verdict.operation.current_stage == "candidate.snapshot"
	assert not list((tmp_path / "temp").glob("qg-*"))
	assert any(stage.stage == "cleanup" for stage in verdict.operation.stages)
