"""Edit-result adapter for the shared provider-owned evidence protocol."""

from __future__ import annotations

from typing import Any

from .verification_evidence import (
	EngineIdentity,
	EvidenceHit,
	EvidenceProducer,
	EvidenceStore,
)
from .verification_evidence import (
	identity_digest as identity_digest,
)


class ResultReuse:
	"""Reuse complete edit batches through the shared authenticated record envelope."""

	def __init__(self, deadline: float) -> None:
		self.store = EvidenceStore(deadline)
		self.root = self.store.root

	def lookup(
		self,
		identity: dict[str, Any] | None,
		*,
		expected_engine: EngineIdentity | None,
	) -> dict[str, Any] | None:
		if identity is None or expected_engine is None:
			return None
		hit: EvidenceHit | None = self.store.lookup(identity, expected_engine=expected_engine)
		return hit.outcome if hit is not None else None

	def remember(
		self,
		identity: dict[str, Any] | None,
		result: dict[str, Any],
		*,
		producer: EvidenceProducer | None,
	) -> None:
		if identity is None or producer is None:
			return
		scope = identity.get("scope")
		if not isinstance(scope, dict):
			return
		self.store.publish(identity, producer=producer, scope=scope, outcome=result)
