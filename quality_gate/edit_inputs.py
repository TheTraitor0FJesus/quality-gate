"""Bounded exact working-tree files and configuration freshness."""

from __future__ import annotations

import io
import json
import re
import stat
import tokenize
import tomllib
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .edit_policy import sha256
from .edit_protocol import (
	FIRST_PRINTABLE_CHARACTER,
	MAX_CONFIG_BYTES,
	MAX_CONFIG_TOTAL_BYTES,
	MAX_FILE_BYTES,
	MAX_FILES,
	MAX_PATH_CHARS,
	MAX_TOTAL_BYTES,
	EditError,
)

_MAX_IMPORT_NAMES = 256
_MAX_IMPORT_SEGMENTS = 32


def regular_path(root: Path, relative: str, *, optional: bool = False) -> Path:
	"""Reject escapes and all reparse-point path segments before reading or writing."""
	if (
		not relative
		or len(relative) > MAX_PATH_CHARS
		or any(ord(char) < FIRST_PRINTABLE_CHARACTER for char in relative)
	):
		raise EditError("unsafe_path")
	parts = relative.replace("\\", "/").split("/")
	if any(part in {"", ".", ".."} or ":" in part for part in parts):
		raise EditError("unsafe_path")
	path = root
	for part in parts:
		path = path / part
		try:
			metadata = path.lstat()
		except FileNotFoundError:
			if optional:
				return root.joinpath(*parts)
			raise EditError("io") from None
		if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & getattr(
			stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
		):
			raise EditError("unsafe_path")
	if not path.is_file() or path.stat().st_nlink != 1:
		raise EditError("unsafe_path")
	resolved = path.resolve(strict=True)
	if not resolved.is_relative_to(root):
		raise EditError("unsafe_path")
	return resolved


def read_bounded(path: Path, limit: int) -> bytes:
	with path.open("rb") as stream:
		content = stream.read(limit + 1)
	if len(content) > limit:
		raise EditError("limits")
	return content


@dataclass(frozen=True, slots=True)
class SourceFile:
	path: str
	content: bytes
	sha256: str


def _module_names(files: list[SourceFile]) -> set[tuple[str, ...]] | None:
	"""Overapproximate import candidates without depending on Python grammar versions."""
	names: set[tuple[str, ...]] = set()
	try:
		for source in files:
			chain: tuple[str, ...] = ()
			dotted = False
			for token in tokenize.tokenize(io.BytesIO(source.content).readline):
				if token.type == tokenize.ERRORTOKEN:
					return None
				if token.type == tokenize.NAME:
					name = unicodedata.normalize("NFKC", token.string)
					chain = (*chain, name) if dotted else (name,)
					names.add(chain)
					dotted = False
					if len(names) > _MAX_IMPORT_NAMES or len(chain) > _MAX_IMPORT_SEGMENTS:
						return None
				elif token.type == tokenize.OP and token.string == "." and chain:
					dotted = True
				elif token.type not in {tokenize.NL, tokenize.COMMENT, tokenize.ENCODING}:
					chain, dotted = (), False
	except (SyntaxError, UnicodeError, LookupError, tokenize.TokenError):
		return None
	return names


class ImportContext:
	"""Bounded source-root facts used by Ruff's first-party import classification."""

	def __init__(self, root: Path, files: list[SourceFile]) -> None:
		self.facts: dict[Path, tuple[bool, bool]] = {}
		self.identity: str | None = None
		names = _module_names(files)
		if names is None:
			return
		try:
			for base in (root, root / "src"):
				for name in names:
					module = base.joinpath(*name)
					for candidate in (
						module,
						module.with_suffix(".py"),
						module.with_suffix(".pyi"),
					):
						self.facts[candidate] = candidate.is_dir(), candidate.is_file()
		except OSError:
			return
		self.identity = sha256(
			json.dumps(sorted((str(path), state) for path, state in self.facts.items())).encode()
		)

	def assert_current(self) -> None:
		if self.identity is not None and any(
			(path.is_dir(), path.is_file()) != expected for path, expected in self.facts.items()
		):
			raise EditError("stale_input")


def sources(root: Path, entries: list[Any]) -> list[SourceFile]:
	if len(entries) > MAX_FILES:
		raise EditError("limits")
	selected: dict[str, SourceFile] = {}
	total = 0
	for entry in entries:
		if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
			raise EditError("invalid_request")
		relative, expected = entry["path"], entry["sha256"]
		if (
			not isinstance(relative, str)
			or not isinstance(expected, str)
			or not re.fullmatch(r"[0-9a-f]{64}", expected)
		):
			raise EditError("invalid_request")
		path = regular_path(root, relative)
		relative = path.relative_to(root).as_posix()
		content = read_bounded(path, MAX_FILE_BYTES)
		if sha256(content) != expected:
			raise EditError("stale_input")
		if relative in selected:
			continue
		total += len(content)
		if total > MAX_TOTAL_BYTES:
			raise EditError("limits")
		selected[relative] = SourceFile(relative, content, expected)
	return list(selected.values())


class Configuration:
	"""Capture only named configuration paths; never enumerate project files."""

	def __init__(self, root: Path) -> None:
		self.root = root
		self.identities: dict[str, str | None] = {}
		self.total = 0

	def capture(self, relative: str, *, required: bool = False) -> bytes | None:
		path = regular_path(self.root, relative, optional=not required)
		content = read_bounded(path, MAX_CONFIG_BYTES) if path.exists() else None
		identity = sha256(content) if content is not None else None
		if relative in self.identities and self.identities[relative] != identity:
			raise EditError("stale_input")
		if content is None:
			self.identities[relative] = None
			return None
		if relative not in self.identities:
			self.total += len(content)
		if self.total > MAX_CONFIG_TOTAL_BYTES:
			raise EditError("limits")
		self.identities[relative] = identity
		if path.name in {"pyproject.toml", "ruff.toml", ".ruff.toml"}:
			try:
				tomllib.loads(content.decode("utf-8"))
			except (ValueError, UnicodeError) as error:
				raise EditError("configuration") from error
		return content

	def for_sources(self, files: list[SourceFile]) -> None:
		for source in files:
			for directory in (Path(source.path).parent, *Path(source.path).parent.parents):
				for name in ("pyproject.toml", "ruff.toml", ".ruff.toml", ".gitignore", ".ignore"):
					self.capture((directory / name).as_posix())
		if (self.root / ".git").is_dir():
			self.capture(".git/info/exclude")

	def assert_current(self) -> None:
		for relative, expected in self.identities.items():
			path = regular_path(self.root, relative, optional=expected is None)
			actual = sha256(read_bounded(path, MAX_CONFIG_BYTES)) if path.exists() else None
			if actual != expected:
				raise EditError("stale_input")

	def as_list(self) -> list[dict[str, str | None]]:
		return [
			{"path": path, "sha256": digest} for path, digest in sorted(self.identities.items())
		]
