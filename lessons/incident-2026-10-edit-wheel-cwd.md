---
id: incident-2026-10-edit-wheel-cwd
status: learned
incident: Published v2.6.0 edit-check failed when the native launcher imported its wheel directly.
expected_layer: python.component_1.pytest
miss_cause: CLI tests imported source or extracted installations, where the package root was a directory.
adaptation: Start the isolated worker in its private workspace and test public CLI execution with a real wheel on PYTHONPATH.
evidence: test_wheel_on_pythonpath_checks_working_files_without_extraction covers both CLI modules, exact policy pins, real Ruff fixes, final hashes, immutable wheel bytes and workspace cleanup.
---

# Wheel import locations are not working directories

The native release launcher verifies an immutable wheel and adds that archive to
`PYTHONPATH`. Under zipimport, the engine package root names a file. Using it as
the worker's cwd failed before file processing and produced a source-free `io`
response. Installing the same wheel into site-packages hid the defect.

Keep the import location explicit, independently of the existing private workspace
used as cwd. The regression builds the actual package wheel once, then launches it
without extraction from a separate directory on Windows and Linux. Policy caches
contain real pinned Ruff and controlled remaining inventory; they do not replace
genuine immutable-release or host activation acceptance.
