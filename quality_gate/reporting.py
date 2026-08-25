"""Compact deterministic English reporting for agents."""

from __future__ import annotations

from .contracts import MAX_FINDINGS_PER_CHECK, MAX_FINDINGS_TOTAL, Status, Verdict, redact


def render(verdict: Verdict, *, verbose: bool = False) -> str:
	lines = [f"Quality Gate: {verdict.exit_code}"]
	total = 0
	for result in sorted(verdict.results, key=lambda item: item.check_id):
		lines.append(
			f"{result.check_id}: {result.status.value} - {redact(result.summary, result._secrets)}"
		)
		if result.status is Status.PASSED and not verbose:
			continue
		findings = (
			result.ordered_findings if verbose else result.ordered_findings[:MAX_FINDINGS_PER_CHECK]
		)
		if not verbose:
			findings = findings[: MAX_FINDINGS_TOTAL - total]
		for finding in findings:
			location = (
				f"{finding.path}:{finding.line}" if finding.path and finding.line else finding.path
			)
			location = redact(location, result._secrets)
			detail = f"{location}: " if location else ""
			detail += redact(finding.message, result._secrets)
			if finding.action:
				detail += f"; action: {redact(finding.action, result._secrets)}"
			lines.append(f"  - {detail}")
		total += len(findings)
		if not verbose and len(result.ordered_findings) > MAX_FINDINGS_PER_CHECK:
			hidden = len(result.ordered_findings) - MAX_FINDINGS_PER_CHECK
			lines.append(f"  - {hidden} more finding(s); use --verbose")
		if result.recovery_action:
			lines.append(f"  recovery: {redact(result.recovery_action, result._secrets)}")
		if total >= MAX_FINDINGS_TOTAL and not verbose:
			lines.append("  - Further findings hidden; use --verbose")
			break
	return "\n".join(lines)
