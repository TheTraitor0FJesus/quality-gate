"""Exact-release compatibility and explicit native Ruff provisioning."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import os
import re
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .distribution import PolicyCache, ReleaseManifest, verify_release
from .edit_protocol import MAX_CONFIG_BYTES, SUPPORTED_POLICIES, EditError

RECORD_COLUMNS = 3


def sha256(content: bytes) -> str:
	return hashlib.sha256(content).hexdigest()


def file_sha256(path: Path, limit: int) -> str:
	"""Hash bounded artifacts without allocating the maximum size for every read."""
	digest = hashlib.sha256()
	total = 0
	with path.open("rb") as stream:
		while block := stream.read(1024 * 1024):
			total += len(block)
			if total > limit:
				raise EditError("limits")
			digest.update(block)
	return digest.hexdigest()


def select_release(cache: PolicyCache, version: str) -> tuple[Path, ReleaseManifest]:
	"""Verify without PolicyCache.select's corrupt-entry quarantine mutation."""
	if version not in SUPPORTED_POLICIES:
		raise EditError("unsupported_policy")
	root = cache.releases / version
	if not root.is_dir():
		raise EditError("release")
	for path in (root, *root.rglob("*")):
		if (
			path.is_symlink()
			or getattr(path.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
		):
			raise EditError("release")
	manifest = verify_release(root)
	if manifest.version != version:
		raise EditError("release")
	return root, manifest


@dataclass(frozen=True, slots=True)
class RuffArtifact:
	"""Native payload identity authenticated by the already verified wheel's RECORD."""

	wheel: Path
	member: str
	version: str
	sha256: str
	size: int

	def extract(self) -> bytes:
		with zipfile.ZipFile(self.wheel) as archive:
			binary = archive.read(self.member)
		if len(binary) != self.size or sha256(binary) != self.sha256:
			raise EditError("tool")
		return binary


def ruff_artifact(root: Path, manifest: ReleaseManifest) -> RuffArtifact:
	tool = next((tool for tool in manifest.tools if tool.name == "ruff"), None)
	if tool is None or tool.version != "0.16.4":
		raise EditError("tool")
	try:
		with zipfile.ZipFile(root / tool.path) as archive:
			name = "ruff.exe" if os.name == "nt" else "ruff"
			members = [item for item in archive.infolist() if item.filename.endswith("/" + name)]
			if len(members) != 1 or members[0].file_size > 64 * 1024 * 1024:
				raise EditError("tool")
			member = members[0]
			record = archive.getinfo(f"ruff-{tool.version}.dist-info/RECORD")
			if record.file_size > MAX_CONFIG_BYTES:
				raise EditError("tool")
			rows = list(csv.reader(io.StringIO(archive.read(record).decode("utf-8"))))
			matches = [
				row for row in rows if len(row) == RECORD_COLUMNS and row[0] == member.filename
			]
			if len(matches) != 1 or not re.fullmatch(r"sha256=[A-Za-z0-9_-]{43}", matches[0][1]):
				raise EditError("tool")
			row = matches[0]
			if int(row[2]) != member.file_size:
				raise EditError("tool")
			digest = base64.urlsafe_b64decode(row[1].removeprefix("sha256=") + "=").hex()
			return RuffArtifact(
				root / tool.path, member.filename, tool.version, digest, member.file_size
			)
	except (OSError, ValueError, KeyError, zipfile.BadZipFile, RuntimeError) as error:
		raise EditError("tool") from error


def tool_path(cache: PolicyCache, digest: str) -> Path:
	return cache.root / "edit-tools" / digest / ("ruff.exe" if os.name == "nt" else "ruff")


def setup_tool(cache: PolicyCache, version: str) -> Path:
	"""Explicit offline preparation after sync; never called by edit-check."""
	root, manifest = select_release(cache, version)
	artifact = ruff_artifact(root, manifest)
	binary = artifact.extract()
	target = tool_path(cache, artifact.sha256)
	if target.is_file() and not target.is_symlink() and target.read_bytes() == binary:
		return target
	target.parent.mkdir(parents=True, exist_ok=True)
	with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
		staged = Path(stream.name)
		stream.write(binary)
	try:
		staged.chmod(0o700)
		os.replace(staged, target)
	finally:
		staged.unlink(missing_ok=True)
	return target
