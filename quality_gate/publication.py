"""Shared release policy; callers provide source metadata, never executable adapters."""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence

PUBLICATION = (
	"owner merge authorizes publication after the merged commit passes the required release checks."
)
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
MAX_VERSION_LENGTH = 32
MAX_BODY_BYTES = 64 * 1024


class PublicationError(ValueError):
	"""A candidate lacks matching authorization or verified release evidence."""


def _version(value: str) -> tuple[int, ...]:
	if len(value) > MAX_VERSION_LENGTH or VERSION.fullmatch(value) is None:
		raise PublicationError("version must be plain MAJOR.MINOR.PATCH")
	return tuple(int(part) for part in value.split("."))


def _visible_lines(body: str) -> Iterator[str]:
	fence = ""
	html_tag = ""
	html_depth = 0
	for line in body.replace("\r\n", "\n").replace("\r", "\n").splitlines():
		opening = re.match(r"^ {0,3}<(pre|code|blockquote|script|style|textarea)\b", line, re.I)
		if not fence and (html_tag or opening):
			html_tag = html_tag or (opening.group(1).lower() if opening else "")
			html_depth += len(re.findall(rf"<{html_tag}\b", line, re.I))
			html_depth -= len(re.findall(rf"</{html_tag}\s*>", line, re.I))
			if html_depth <= 0:
				html_tag = ""
				html_depth = 0
			yield "```"
			continue
		marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
		if marker:
			yield "```"
			if not fence:
				fence = marker.group(1)
			elif marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence):
				fence = ""
		elif not fence:
			yield line


def _section(body: str) -> list[str]:
	if len(body.encode("utf-8")) > MAX_BODY_BYTES:
		raise PublicationError("PR body exceeds 64 KiB")
	body = re.sub(r"<!--[\s\S]*?(?:-->|$)", "", body)
	lines: list[str] = []
	count = 0
	active = False
	for line in _visible_lines(body):
		if line == "```":
			if active:
				raise PublicationError("code blocks cannot authorize publication")
			continue
		if re.fullmatch(r" {0,3}## Release(?:[ \t]+#+)?[ \t]*", line):
			if re.fullmatch(r"## Release[ \t]*", line) is None:
				raise PublicationError("Release heading must be exactly ## Release")
			count += 1
			active = True
			continue
		if re.match(r"^#{1,2} ", line):
			active = False
		if active and line.strip():
			lines.append(line)
	if count != 1:
		raise PublicationError("exactly one second-level Release section is required")
	return lines


def parse_decision(body: str) -> dict[str, str]:
	"""Parse the sole visible Release declaration without interpreting old source notes."""
	fields: dict[str, str] = {}
	for line in _section(body):
		match = re.fullmatch(r"- (Version|Changes|Publication|No release): ([^\r\n]+)", line)
		if match is None or not match.group(2).strip() or match.group(1) in fields:
			raise PublicationError("Release fields must be unique, nonempty, unquoted list items")
		fields[match.group(1)] = match.group(2).strip()
	if set(fields) == {"No release"}:
		return fields
	if set(fields) != {"Version", "Changes", "Publication"} or fields["Publication"] != PUBLICATION:
		raise PublicationError(
			"Release requires Version, Changes and exact Publication, or only No release"
		)
	_version(fields["Version"])
	return fields


def prepare(
	*,
	body: str,
	version: str,
	base_version: str,
	baseline: str,
	projections: Sequence[str],
) -> dict[str, object]:
	"""Read-only validation of one PR decision and its source version projections.

	The workflow supplies metadata read from exact Git objects. Old notes are deliberately
	absent from this decision interface. Published-candidate readback precedes this new-version
	validation in post-merge preparation.
	"""
	_version(version)
	_version(base_version)
	major, minor, patch = _version(baseline)
	fields = parse_decision(body)
	if any(item != version for item in projections):
		raise PublicationError("native version projections disagree with the authority")
	release = "Version" in fields
	if not release and version != base_version:
		raise PublicationError("No release requires an unchanged version relative to the PR base")
	if release:
		if fields["Version"] != version:
			raise PublicationError("PR version disagrees with source authority")
		if version not in {
			f"{major + 1}.0.0",
			f"{major}.{minor + 1}.0",
			f"{major}.{minor}.{patch + 1}",
		}:
			raise PublicationError(
				"stale version: prepare one normal next major, minor or patch increment"
			)
	return {"status": "validated", "version": version, "release": release}
