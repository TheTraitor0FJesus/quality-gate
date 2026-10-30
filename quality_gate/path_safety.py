"""Shared checks for filesystem links and Windows reparse points."""

from __future__ import annotations

import os
import stat


def is_reparse_point(metadata: os.stat_result) -> bool:
	return bool(
		getattr(metadata, "st_file_attributes", 0)
		& getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
	)


def is_link_or_reparse(metadata: os.stat_result) -> bool:
	return stat.S_ISLNK(metadata.st_mode) or is_reparse_point(metadata)
