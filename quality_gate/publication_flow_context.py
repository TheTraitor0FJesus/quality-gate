"""Internal interface shared by publisher operation modules."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from .publication_artifacts import ArtifactReader
from .publication_flow_support import Context, GitHub, _VerifiedImage


class _PublisherContext(Protocol):
	api: GitHub
	context: Context
	artifacts: ArtifactReader

	def _releases(self) -> list[dict[str, object]]: ...

	def _baseline(self) -> str: ...

	def _tag(self, tag: str) -> str | None: ...

	def _pull(self, number: int, *, merged: bool) -> dict[str, object]: ...

	def _dependabot_workflow_only(
		self, number: int, event_pull: Mapping[str, object], pull: Mapping[str, object]
	) -> bool: ...

	def _metadata(self, source: str) -> tuple[str, dict[str, object], list[str]]: ...

	def prepare(
		self, number: int, *, event: Mapping[str, object], validation_only: bool = False
	) -> dict[str, object]: ...

	def _existing(self, version: str) -> dict[str, object] | None: ...

	def _completed(
		self,
		pull: Mapping[str, object],
		source: str,
		version: str,
		config: Mapping[str, object],
		projections: list[str],
	) -> dict[str, object] | None: ...

	def _validate_candidate(
		self, candidate: Mapping[str, object], pull: Mapping[str, object]
	) -> None: ...

	def _candidate(self, number: int, artifact_id: int) -> dict[str, object]: ...

	def _original_artifacts(self, number: int) -> list[dict[str, object]]: ...

	def recover(self, number: int) -> dict[str, object]: ...

	def _recovery_context(self) -> None: ...

	def _latest_producer_job(
		self, job_name: str, checks: list[str], *, minimum_attempt: int = 1
	) -> tuple[dict[str, object], int]: ...

	@staticmethod
	def _artifact_attempt(prefix: str, item: Mapping[str, object]) -> int | None: ...

	@classmethod
	def _contains_evidence(
		cls, items: list[dict[str, object]], prefix: str, expected_name: str | None
	) -> bool: ...

	def _producer_artifacts(
		self, producer_name: str, prefix: str, *, attempt: int | None = None
	) -> list[tuple[int, dict[str, object]]]: ...

	def _select_producer_artifact(
		self,
		producer_name: str,
		prefix: str,
		job_name: str,
		checks: list[str],
		latest_job: dict[str, object],
		latest_attempt: int,
		available: list[tuple[int, dict[str, object]]],
	) -> tuple[int, dict[str, object], dict[str, object], int]: ...

	def _producer_bundle(
		self, candidate: Mapping[str, object], producer: Mapping[str, object]
	) -> tuple[dict[str, object], dict[str, bytes], dict[str, object]]: ...

	def _verification_context(self, candidate: Mapping[str, object]) -> None: ...

	def _image_deliverable_identity(
		self,
		item: Mapping[str, object],
		producer: Mapping[str, object],
		files: Mapping[str, bytes],
		candidate: Mapping[str, object],
		image_receipt: Mapping[str, object] | None,
		proof: Mapping[str, object],
	) -> dict[str, object]: ...

	def _deliverables(
		self, candidate: Mapping[str, object], candidate_artifact_id: int
	) -> tuple[
		list[dict[str, object]],
		dict[str, bytes],
		list[dict[str, object]],
		dict[str, object] | None,
		dict[str, object] | None,
	]: ...

	@staticmethod
	def _image_archive(
		item: Mapping[str, object], files: Mapping[str, bytes]
	) -> tuple[str, str]: ...

	@staticmethod
	def _writer_image(
		receipt: Mapping[str, object],
		*,
		producer: str,
		name: str,
		archive_name: str,
		archive_sha256: str,
		producer_proof: Mapping[str, object],
	) -> dict[str, object]: ...

	def _image_receipt(
		self, candidate: Mapping[str, object], candidate_artifact_id: int
	) -> tuple[dict[str, object], dict[str, object]]: ...

	def write_images(
		self, number: int, candidate_artifact_id: int, output: Path
	) -> dict[str, object]: ...

	def _prior_image_digests(
		self, version: str, candidate: Mapping[str, object], verified: list[_VerifiedImage]
	) -> dict[str, str]: ...

	def _verify_deliverable(
		self,
		item: Mapping[str, object],
		files: Mapping[str, bytes],
		*,
		expected_image_repository: str | None = None,
	) -> None: ...

	def _body(self, candidate: Mapping[str, object], receipt: Mapping[str, object]) -> str: ...

	def _readback(
		self,
		release: Mapping[str, object],
		candidate: Mapping[str, object],
		receipt: Mapping[str, object],
		*,
		draft: bool = False,
	) -> set[str]: ...

	@staticmethod
	def _image_producer_proofs(
		candidate: Mapping[str, object], verification: Mapping[str, object]
	) -> tuple[dict[str, dict[str, object]], dict[str, str]]: ...

	@staticmethod
	def _matches_image_producer_proof(
		image_proof: Mapping[str, object], producer_proof: Mapping[str, object]
	) -> bool: ...

	@classmethod
	def _validate_image_writer_receipt(
		cls,
		candidate: Mapping[str, object],
		items: list[dict[str, object]],
		receipt: Mapping[str, object],
	) -> None: ...

	def _draft_receipt(
		self,
		release: Mapping[str, object],
		candidate: Mapping[str, object],
		receipt: dict[str, object],
	) -> dict[str, object]: ...

	def _publish_release(
		self,
		version: str,
		candidate: Mapping[str, object],
		receipt: dict[str, object],
		contents: Mapping[str, bytes],
	) -> dict[str, object]: ...

	def publish(self, number: int, candidate_artifact_id: int) -> dict[str, object]: ...
