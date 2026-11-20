"""Fixtures and helpers for the split feature tests."""

from __future__ import annotations

import errno
import os
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_edit_check import process_alive

import quality_gate.temp_workspace as temp_workspace
from quality_gate import launcher, operation, runner
from quality_gate import secrets as secrets_module
from quality_gate.contracts import CheckResult, Finding, Status, load_manifest
from quality_gate.operation import GateOperation
from quality_gate.runtime import RuntimeInspection
from quality_gate.temp_workspace import TemporaryWorkspaceError, temporary_workspace

EXIT_UNCHECKED = 2

PREPARATION_ELAPSED_MS = 2000

TREE_TERMINATION_BOUND_SECONDS = 3

PROBE_TERMINATION_BOUND_SECONDS = 2


__all__ = [
	"CheckResult",
	"EXIT_UNCHECKED",
	"Finding",
	"GateOperation",
	"PREPARATION_ELAPSED_MS",
	"PROBE_TERMINATION_BOUND_SECONDS",
	"Path",
	"RuntimeInspection",
	"SimpleNamespace",
	"Status",
	"TREE_TERMINATION_BOUND_SECONDS",
	"TemporaryWorkspaceError",
	"errno",
	"launcher",
	"load_manifest",
	"operation",
	"os",
	"process_alive",
	"pytest",
	"runner",
	"secrets_module",
	"shutil",
	"signal",
	"sys",
	"temp_workspace",
	"tempfile",
	"temporary_workspace",
	"time",
]
