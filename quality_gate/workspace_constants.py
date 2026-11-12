"""Names and limits for authenticated workspace cleanup."""

TEMP_DIRECTORY_NAME = "temp"
_OWNERSHIP_RECORD_PREFIX = ".quality-gate-owner-"
_CLEANUP_RECORD_PREFIX = ".quality-gate-cleanup-record-"
_CLEANUP_RETRY_PREFIX = ".quality-gate-cleanup-retry-"
_OWNERSHIP_RECORD_MAX_BYTES = 4096
_CLEANUP_AREA_NAME = ".quality-gate-cleanup"
_CLEANUP_AREA_MARKER_NAME = ".quality-gate-cleanup-area"
_QUARANTINE_JOURNAL_MAX_BYTES = 4096
_QUARANTINE_JOURNAL_SCHEMA = 1
_OWNERSHIP_MARKER_NAME = ".quality-gate-workspace"
_OWNERSHIP_MARKER_MAX_BYTES = 4096
_OWNERSHIP_RECORD_SCHEMA = 2
_CLEANUP_RECORD_SCHEMA = 2
_ACTIVE_LOCK_NAME = ".active.lock"
_ROOT_CONTROL_FILE_NAMES = frozenset({_ACTIVE_LOCK_NAME, _OWNERSHIP_MARKER_NAME})
