"""Bounded staged Git batch materialization."""

from __future__ import annotations

from . import snapshot as _snapshot


def _prepare_index(
	repository: _snapshot.Path,
	staging_root: _snapshot.Path,
	timeout_seconds: int,
	deadline: float | None = None,
) -> tuple[_snapshot.Path, _snapshot._IndexFingerprint, _snapshot.Path]:
	_snapshot._check_deadline(deadline)
	index_path = _snapshot._index_path(repository, timeout_seconds, deadline)
	source_fingerprint = _snapshot._index_fingerprint(index_path, deadline)
	index_copy = staging_root / "index"
	if source_fingerprint is not None:
		_snapshot.shutil.copyfile(index_path, index_copy)
	_snapshot._check_deadline(deadline)
	if _snapshot._index_fingerprint(index_path, deadline) != source_fingerprint:
		raise _snapshot.SnapshotError("the Git index changed during candidate construction")
	return (index_path, source_fingerprint, index_copy)


def _candidate_limits(
	repository: _snapshot.Path,
	index_file: _snapshot.Path,
	timeout_seconds: int,
	deadline: float | None = None,
) -> tuple[int, int]:
	try:
		manifest = _snapshot._git(
			repository,
			("show", ":quality-gate.toml"),
			timeout_seconds,
			index_file=index_file,
			deadline=deadline,
		).stdout
	except _snapshot.SnapshotError:
		_snapshot._check_deadline(deadline)
		return (_snapshot.DEFAULT_COMMAND_TIMEOUT_SECONDS, _snapshot.DEFAULT_MAX_BLOB_SIZE_MIB)
	_snapshot._check_deadline(deadline)
	if len(manifest) > _snapshot._MAX_MANIFEST_BYTES:
		raise _snapshot.SnapshotError("the staged quality manifest exceeds the snapshot limit")
	try:
		table = _snapshot.tomllib.loads(manifest.decode("utf-8"))
	except (_snapshot.tomllib.TOMLDecodeError, UnicodeDecodeError):
		return (_snapshot.DEFAULT_COMMAND_TIMEOUT_SECONDS, _snapshot.DEFAULT_MAX_BLOB_SIZE_MIB)
	repository_table = table.get("repository")
	if not isinstance(repository_table, _snapshot.Mapping):
		return (_snapshot.DEFAULT_COMMAND_TIMEOUT_SECONDS, _snapshot.DEFAULT_MAX_BLOB_SIZE_MIB)
	defaults = repository_table.get("defaults")
	limits = repository_table.get("limits")
	command_timeout = (
		defaults.get("command_timeout_seconds")
		if isinstance(defaults, _snapshot.Mapping)
		else _snapshot.DEFAULT_COMMAND_TIMEOUT_SECONDS
	)
	max_blob_size = (
		limits.get("max_blob_size_mib")
		if isinstance(limits, _snapshot.Mapping)
		else _snapshot.DEFAULT_MAX_BLOB_SIZE_MIB
	)
	if (
		isinstance(command_timeout, bool)
		or not isinstance(command_timeout, int)
		or command_timeout <= 0
	):
		command_timeout = _snapshot.DEFAULT_COMMAND_TIMEOUT_SECONDS
	if isinstance(max_blob_size, bool) or not isinstance(max_blob_size, int) or max_blob_size <= 0:
		max_blob_size = _snapshot.DEFAULT_MAX_BLOB_SIZE_MIB
	return (command_timeout, max_blob_size)


def _reject_filtered_paths(
	repository: _snapshot.Path,
	entries: tuple[_snapshot._StagedEntry, ...],
	index_file: _snapshot.Path,
	timeout_seconds: int,
	deadline: float | None = None,
) -> None:
	_snapshot._check_deadline(deadline)
	if not entries:
		return

	def write_paths(stream: _snapshot.BinaryIO) -> None:
		for entry in entries:
			_snapshot._check_deadline(deadline)
			stream.write(entry.raw_path + b"\x00")

	def read_attributes(stream: _snapshot.BinaryIO) -> None:
		output = _snapshot._GitBatchOutput(stream, deadline)
		for entry in entries:
			_snapshot._check_deadline(deadline)
			path = output.read_nul_field(len(entry.raw_path))
			attribute = output.read_nul_field(len(b"filter"))
			value = output.read_nul_field(_snapshot._MAX_ATTRIBUTE_VALUE_BYTES)
			if path != entry.raw_path or attribute != b"filter":
				raise _snapshot.SnapshotError(
					"Git returned a malformed candidate attribute response"
				)
			if value.strip().lower() != b"unspecified":
				raise _snapshot.SnapshotError(
					"Git clean/smudge filters are not supported by this snapshot"
				)
		output.assert_eof("Git returned an unexpected candidate attribute response")

	_snapshot._run_git_batch(
		repository,
		index_file,
		timeout_seconds,
		("check-attr", "--cached", "-z", "--stdin", "filter"),
		write_paths,
		read_attributes,
		deadline=deadline,
	)


class _GitBatchOutput:
	"""Read bounded fields from a Git batch protocol without buffering its stream."""

	def __init__(self, stream: _snapshot.BinaryIO, deadline: float | None = None) -> None:
		self._stream = stream
		self._deadline = deadline
		self._buffer = bytearray()

	def _read_more(self, maximum: int) -> None:
		_snapshot._check_deadline(self._deadline)
		read = getattr(self._stream, "read1", self._stream.read)
		chunk = read(min(_snapshot._BATCH_READ_CHUNK_BYTES, maximum))
		_snapshot._check_deadline(self._deadline)
		if not chunk:
			raise _snapshot.SnapshotError("Git returned truncated candidate batch data")
		self._buffer.extend(chunk)

	def read_nul_field(self, max_bytes: int) -> bytes:
		while True:
			_snapshot._check_deadline(self._deadline)
			separator = self._buffer.find(0)
			if separator >= 0:
				if separator > max_bytes:
					raise _snapshot.SnapshotError("Git returned an oversized candidate batch field")
				value = bytes(self._buffer[:separator])
				del self._buffer[: separator + 1]
				return value
			if len(self._buffer) > max_bytes:
				raise _snapshot.SnapshotError("Git returned an oversized candidate batch field")
			self._read_more(max_bytes + 1 - len(self._buffer))

	def read_line(self, max_bytes: int) -> bytes:
		while True:
			_snapshot._check_deadline(self._deadline)
			separator = self._buffer.find(b"\n")
			if separator >= 0:
				if separator + 1 > max_bytes:
					raise _snapshot.SnapshotError(
						"Git returned an oversized candidate object header"
					)
				line = bytes(self._buffer[: separator + 1])
				del self._buffer[: separator + 1]
				return line
			if len(self._buffer) >= max_bytes:
				raise _snapshot.SnapshotError("Git returned a malformed candidate object header")
			self._read_more(max_bytes - len(self._buffer))

	def read_exactly(self, count: int) -> bytes:
		while len(self._buffer) < count:
			_snapshot._check_deadline(self._deadline)
			self._read_more(count - len(self._buffer))
		value = bytes(self._buffer[:count])
		del self._buffer[:count]
		return value

	def assert_eof(self, message: str) -> None:
		_snapshot._check_deadline(self._deadline)
		if self._buffer:
			raise _snapshot.SnapshotError(message)
		read = getattr(self._stream, "read1", self._stream.read)
		if read(1):
			raise _snapshot.SnapshotError(message)
		_snapshot._check_deadline(self._deadline)


def _stop_and_reap(
	process: _snapshot.subprocess.Popen[bytes],
	workers: list[_snapshot.threading.Thread],
	deadline: float,
) -> None:
	if process.poll() is None:
		try:
			process.kill()
		except OSError:
			if process.poll() is None:
				raise
	try:
		process.wait(timeout=max(0.0, deadline - _snapshot.time.monotonic()))
	except _snapshot.subprocess.TimeoutExpired as exc:
		if process.poll() is None:
			raise _snapshot.SnapshotError(
				"Git could not stop the candidate batch before its deadline"
			) from exc
	for worker in workers:
		worker.join(max(0.0, deadline - _snapshot.time.monotonic()))
	if any(worker.is_alive() for worker in workers):
		raise _snapshot.SnapshotError(
			"Git candidate batch cleanup did not finish before its deadline"
		)


def _close_pipe(stream: _snapshot.BinaryIO | None) -> None:
	if stream is None:
		return
	try:
		stream.close()
	except OSError:
		pass


def _wait_for_git_batch(
	process: _snapshot.subprocess.Popen[bytes],
	workers: list[_snapshot.threading.Thread],
	worker_errors: list[BaseException],
	deadline: float,
) -> None:
	while any(worker.is_alive() for worker in workers):
		if worker_errors:
			break
		remaining = deadline - _snapshot.time.monotonic()
		if remaining <= 0:
			raise _snapshot.SnapshotError(
				"Git could not provide the candidate snapshot before its deadline"
			)
		workers[1].join(min(remaining, 0.05))
		workers[0].join(0)
	if _snapshot.time.monotonic() >= deadline:
		raise _snapshot.SnapshotError(
			"Git could not provide the candidate snapshot before its deadline"
		)
	if worker_errors:
		error = worker_errors[0]
		if isinstance(error, _snapshot.SnapshotError):
			raise error
		raise _snapshot.SnapshotError("Git could not provide the candidate snapshot") from error
	try:
		process.wait(timeout=max(0.0, deadline - _snapshot.time.monotonic()))
	except _snapshot.subprocess.TimeoutExpired as exc:
		raise _snapshot.SnapshotError(
			"Git could not provide the candidate snapshot before its deadline"
		) from exc
	if process.returncode != 0:
		raise _snapshot.SnapshotError(
			f"Git could not provide the candidate snapshot (exit code {process.returncode})"
		)


def _run_git_batch(
	repository: _snapshot.Path,
	index_file: _snapshot.Path,
	timeout_seconds: int,
	arguments: tuple[str, ...],
	write_input: _snapshot.Callable[[_snapshot.BinaryIO], None],
	read_output: _snapshot.Callable[[_snapshot.BinaryIO], None],
	*,
	deadline: float | None = None,
) -> None:
	_snapshot._check_deadline(deadline)
	try:
		process = _snapshot.subprocess.Popen(
			["git", *arguments],
			cwd=repository,
			env={**_snapshot.os.environ, "GIT_INDEX_FILE": str(index_file)},
			stdin=_snapshot.subprocess.PIPE,
			stdout=_snapshot.subprocess.PIPE,
			stderr=_snapshot.subprocess.DEVNULL,
		)
	except OSError as exc:
		raise _snapshot.SnapshotError("Git could not provide the candidate snapshot") from exc
	stdin = _snapshot.cast(_snapshot.BinaryIO, process.stdin)
	stdout = _snapshot.cast(_snapshot.BinaryIO, process.stdout)
	worker_errors: list[BaseException] = []
	workers: list[_snapshot.threading.Thread] = []
	cleanup_deadline = _snapshot.time.monotonic() + timeout_seconds
	batch_deadline = cleanup_deadline - min(1.0, timeout_seconds / 4)
	if deadline is not None:
		batch_deadline = min(batch_deadline, deadline)

	def run_worker(
		operation: _snapshot.Callable[[_snapshot.BinaryIO], None], pipe: _snapshot.BinaryIO
	) -> None:
		try:
			operation(pipe)
		except Exception as exc:
			worker_errors.append(exc)

	def send_input(pipe: _snapshot.BinaryIO) -> None:
		try:
			write_input(pipe)
		finally:
			_snapshot._close_pipe(pipe)

	try:
		for operation, pipe in ((send_input, stdin), (read_output, stdout)):
			worker = _snapshot.threading.Thread(
				target=run_worker, args=(operation, pipe), daemon=True
			)
			worker.start()
			workers.append(worker)
		_snapshot._wait_for_git_batch(process, workers, worker_errors, batch_deadline)
	except BaseException as exc:
		if process.poll() is None or any(worker.is_alive() for worker in workers):
			try:
				_snapshot._stop_and_reap(process, workers, cleanup_deadline)
			except _snapshot.SnapshotError as cleanup_error:
				raise cleanup_error from exc
		raise
	finally:
		_snapshot._close_pipe(stdin)
		_snapshot._close_pipe(stdout)


def _object_header(output: _snapshot._GitBatchOutput, requested_id: bytes) -> int:
	header = output.read_line(_snapshot._MAX_OBJECT_HEADER_BYTES)
	if not header.endswith(b"\n"):
		raise _snapshot.SnapshotError("Git returned a malformed candidate object header")
	fields = header[:-1].split(b" ")
	if (
		len(fields) == _snapshot._MISSING_OBJECT_HEADER_FIELDS
		and fields[0] == requested_id
		and (fields[1] == b"missing")
	):
		raise _snapshot.SnapshotError("Git could not provide a staged blob object")
	if len(fields) != _snapshot._OBJECT_HEADER_FIELDS:
		raise _snapshot.SnapshotError("Git returned a malformed candidate object header")
	object_id, object_type, size_bytes = fields
	if object_id != requested_id:
		raise _snapshot.SnapshotError("Git returned an unexpected candidate object")
	if object_type != b"blob":
		raise _snapshot.SnapshotError("Git returned a non-blob candidate object")
	if (
		not size_bytes
		or len(size_bytes) > _snapshot._MAX_OBJECT_SIZE_DIGITS
		or (not size_bytes.isdigit())
	):
		raise _snapshot.SnapshotError("Git returned an invalid candidate object length")
	return int(size_bytes)


def _read_batch_object(
	output: _snapshot._GitBatchOutput,
	object_id: bytes,
	path_count: int,
	object_directory: _snapshot.Path,
	max_blob_bytes: int,
	candidate_bytes: int,
	max_candidate_bytes: int,
	deadline: float | None = None,
) -> tuple[int, int]:
	_snapshot._check_deadline(deadline)
	size = _snapshot._object_header(output, object_id)
	if size > max_blob_bytes:
		raise _snapshot.SnapshotError("a staged blob exceeds the configured snapshot limit")
	candidate_size = size * path_count
	if candidate_bytes + candidate_size > max_candidate_bytes:
		raise _snapshot.SnapshotError("staged blob data exceeds the aggregate snapshot limit")
	candidate_bytes += candidate_size
	object_path = object_directory / object_id.decode("ascii")
	digest = _snapshot.hashlib.new(
		"sha1" if len(object_id) == _snapshot._SHA1_OBJECT_ID_BYTES else "sha256"
	)
	digest.update(b"blob " + str(size).encode("ascii") + b"\x00")
	with object_path.open("wb") as blob_file:
		remaining = size
		while remaining:
			_snapshot._check_deadline(deadline)
			chunk = output.read_exactly(min(remaining, _snapshot._BATCH_READ_CHUNK_BYTES))
			blob_file.write(chunk)
			digest.update(chunk)
			remaining -= len(chunk)
	_snapshot._check_deadline(deadline)
	if digest.hexdigest().encode("ascii") != object_id:
		raise _snapshot.SnapshotError("Git returned object data inconsistent with its identity")
	if output.read_exactly(1) != b"\n":
		raise _snapshot.SnapshotError("Git returned malformed candidate object framing")
	return (size, candidate_bytes)


def _read_git_blobs(
	context: _snapshot._MaterializationContext,
	entries: tuple[_snapshot._StagedEntry, ...],
	object_directory: _snapshot.Path,
) -> dict[bytes, int]:
	_snapshot._check_deadline(context.deadline)
	entries_by_object: dict[bytes, list[_snapshot._StagedEntry]] = {}
	for entry in entries:
		_snapshot._check_deadline(context.deadline)
		entries_by_object.setdefault(entry.object_id, []).append(entry)
	if not entries_by_object:
		return {}
	object_directory.mkdir()
	object_sizes: dict[bytes, int] = {}
	max_candidate_bytes = len(entries) * context.max_blob_bytes

	def write_object_ids(stream: _snapshot.BinaryIO) -> None:
		for object_id in entries_by_object:
			_snapshot._check_deadline(context.deadline)
			stream.write(object_id + b"\n")

	def read_objects(stream: _snapshot.BinaryIO) -> None:
		output = _snapshot._GitBatchOutput(stream, context.deadline)
		candidate_bytes = 0
		for object_id, object_entries in entries_by_object.items():
			_snapshot._check_deadline(context.deadline)
			size, candidate_bytes = _snapshot._read_batch_object(
				output,
				object_id,
				len(object_entries),
				object_directory,
				context.max_blob_bytes,
				candidate_bytes,
				max_candidate_bytes,
				context.deadline,
			)
			object_sizes[object_id] = size
		output.assert_eof("Git returned unexpected candidate object data")

	_snapshot._LOGGER.debug("running Git candidate object batch")
	_snapshot._run_git_batch(
		context.repository,
		context.index_copy,
		context.timeout_seconds,
		("cat-file", "--batch"),
		write_object_ids,
		read_objects,
		deadline=context.deadline,
	)
	return object_sizes


def _read_cached_blob(
	path: _snapshot.Path, expected_size: int, max_blob_bytes: int, deadline: float | None = None
) -> bytes:
	_snapshot._check_deadline(deadline)
	try:
		with path.open("rb") as blob_file:
			blob = blob_file.read(max_blob_bytes + 1)
			if len(blob) > max_blob_bytes or len(blob) != expected_size or blob_file.read(1):
				raise _snapshot.SnapshotError("Git returned an invalid staged blob cache entry")
	except OSError as exc:
		raise _snapshot.SnapshotError("Git could not provide the candidate snapshot") from exc
	_snapshot._check_deadline(deadline)
	return blob
