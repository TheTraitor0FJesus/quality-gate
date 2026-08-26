"""Shared local and CI quality gate."""

from .contracts import (
	CheckResult,
	Finding,
	Manifest,
	Status,
	ValidationError,
	Verdict,
	Waiver,
	fingerprint_secret,
	load_manifest,
	redact,
)
from .distribution import DistributionError, PolicyCache, ReleaseManifest
from .runtime import RuntimeInspection, RuntimeManager, RuntimeUnavailable, runtime_fingerprint
from .secrets import secret_audit_result, secret_candidate_result, secret_history_result

__version__ = "2.0.0"

__all__ = [
	"CheckResult",
	"Finding",
	"Manifest",
	"Status",
	"ValidationError",
	"Verdict",
	"Waiver",
	"fingerprint_secret",
	"load_manifest",
	"redact",
	"DistributionError",
	"PolicyCache",
	"ReleaseManifest",
	"RuntimeInspection",
	"RuntimeManager",
	"RuntimeUnavailable",
	"runtime_fingerprint",
	"secret_audit_result",
	"secret_candidate_result",
	"secret_history_result",
]
