"""Feature test partition."""

from __future__ import annotations

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
	EXPECTED_FIXED_DISCOVERY_PROCESSES as EXPECTED_FIXED_DISCOVERY_PROCESSES,
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
	assert process_counts == [
		(
			2,
			EXPECTED_FIXED_DISCOVERY_PROCESSES,
			EXPECTED_BATCH_ATTRIBUTE_PROCESSES,
			EXPECTED_BATCH_OBJECT_PROCESSES,
		),
		(
			256,
			EXPECTED_FIXED_DISCOVERY_PROCESSES,
			EXPECTED_BATCH_ATTRIBUTE_PROCESSES,
			EXPECTED_BATCH_OBJECT_PROCESSES,
		),
	]


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
		snapshot_module._reject_filtered_paths(tmp_path, (entry,), tmp_path / "index", 1)

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
		snapshot_module._read_git_blobs(context, (entry,), tmp_path / "objects")

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

	object_sizes = snapshot_module._read_git_blobs(context, (entry, second), object_directory)

	assert object_sizes == {entry.object_id: 6}
	assert process.stdin.captured == entry.object_id + b"\n"
	assert (object_directory / entry.object_id.decode("ascii")).read_bytes() == b"shared"
	assert process.waited
	assert process.wait_timeouts and all(timeout is not None for timeout in process.wait_timeouts)
	assert not process.killed


def test_candidate_snapshot_aggregate_bound_scales_with_indexed_paths(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""The index-derived bound must exceed the former fixed 1 GiB ceiling."""
	max_blob_bytes = 64 * 1024 * 1024
	path_count = 1024 * 1024 * 1024 // max_blob_bytes + 1
	entry = _staged_entry()
	entries = tuple(
		_staged_entry(path=f"copy-{index}.txt".encode("ascii")) for index in range(path_count)
	)
	large_object = entry.object_id + b" blob " + str(max_blob_bytes).encode("ascii") + b"\n"
	process = _fake_batch_process(monkeypatch, large_object)
	context = _materialization_context(tmp_path, max_blob_bytes=max_blob_bytes)

	with pytest.raises(SnapshotError, match="truncated candidate batch"):
		snapshot_module._read_git_blobs(context, entries, tmp_path / "objects")

	assert process.killed
	assert process.waited
	assert process.wait_timeouts and all(timeout is not None for timeout in process.wait_timeouts)
	assert process.stdin.captured == entry.object_id + b"\n"


def test_candidate_snapshot_reaps_batch_process_at_deadline(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	process = _fake_batch_process(monkeypatch, _BlockingOutput())

	with pytest.raises(SnapshotError, match="before its deadline"):
		snapshot_module._reject_filtered_paths(tmp_path, (_staged_entry(),), tmp_path / "index", 1)

	assert process.killed
	assert process.waited
	assert process.wait_timeouts and all(timeout is not None for timeout in process.wait_timeouts)
