"""Shared preparation and publication entry point for product adapters."""

from __future__ import annotations

from .publication import PublicationError
from .publication_artifacts import ArtifactReader
from .publication_evidence import positive
from .publication_flow_support import RECEIPT_MARKER as RECEIPT_MARKER
from .publication_flow_support import Context as Context
from .publication_flow_support import GitHub
from .publication_flow_support import canonical as canonical
from .publication_flow_support import sha as sha
from .publication_images import _Images
from .publication_prepare import _Preparation
from .publication_producers import _Producers
from .publication_release_flow import _ReleaseFlow


class Publisher(_Preparation, _Producers, _Images, _ReleaseFlow):
	"""Validate authorization, verify evidence, and publish one candidate."""

	def __init__(
		self,
		api: GitHub,
		context: Context,
		*,
		evidence_wait_seconds: float = 30.0,
		evidence_poll_seconds: float = 2.0,
	) -> None:
		self.api = api
		self.context = context
		self.artifacts = ArtifactReader(
			api,
			wait_seconds=evidence_wait_seconds,
			poll_seconds=evidence_poll_seconds,
		)
		if sha(context.provider_sha) != sha(context.helper_sha):
			raise PublicationError("executed helper differs from reusable workflow revision")
		sha(context.run_source_sha)
		sha(context.run_head_sha)
		positive(context.run_id, "run ID")
		positive(context.attempt, "run attempt")
