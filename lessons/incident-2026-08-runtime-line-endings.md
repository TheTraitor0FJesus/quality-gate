---
id: incident-2026-08-runtime-line-endings
status: learned
incident: Quality Gate v2.0.1 could not reuse a Windows runtime for the exact Git candidate.
expected_layer: runtime.available
miss_cause: Runtime fingerprint tests did not cover Git line-ending conversion.
adaptation: Canonicalize CRLF dependency inputs to LF before hashing runtime identities.
evidence: test_runtime_fingerprint_ignores_dependency_line_ending_conversion
---

# Windows dependency input fingerprint

Schema 2 migration of ARGUS_WEB exposed the defect. Setup hashed the CRLF working-tree copy of a
tracked requirements file, while candidate verification hashed its LF Git-index copy. The unequal
fingerprints made the prepared runtime appear unavailable.
