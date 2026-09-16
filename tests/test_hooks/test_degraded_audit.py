"""Regression tests for durable records from degraded hook paths."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_WRITER = _ROOT / "scripts" / "hooks" / "degraded_audit.sh"
_BASH = shutil.which("bash")


pytestmark = pytest.mark.skipif(_BASH is None, reason="bash is required")


def _records(store: Path) -> list[dict]:
    return [json.loads(path.read_text()) for path in store.glob("*.jsonl")]


def test_writer_publishes_one_complete_private_json_record(tmp_path: Path):
    store = tmp_path / "audit"
    reason = 'quote " slash \\ newline\nend'
    result = subprocess.run(
        [_BASH, str(_WRITER), "guard-name", "blocked", reason, "gated_operation"],
        env={**os.environ, "GENESIS_DEGRADED_AUDIT_DIR": str(store)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    files = sorted(store.glob("*.jsonl"))
    assert len(files) == 1
    assert stat.S_ISREG(files[0].stat().st_mode)
    assert stat.S_IMODE(store.stat().st_mode) == 0o700
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert _records(store) == [
        {
            "timestamp": files[0].stem.split("-", 1)[0],
            "event": "hook_degraded",
            "source": "guard-name",
            "verdict": "blocked",
            "reason": reason,
            "operation": "gated_operation",
        }
    ]


def test_import_time_degradation_is_recorded_without_changing_exit_code(tmp_path: Path):
    store = tmp_path / "audit"
    hooks = _ROOT / "scripts" / "hooks"
    code = (
        "import hook_input; "
        "hook_input.degraded_exit('poisoned_guard', gated=r'git\\s+clean', "
        "exc=RuntimeError('broken helper'))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        input='{"tool_input": {"command": "printf ok"}}',
        cwd=hooks,
        env={
            **os.environ,
            "PYTHONPATH": str(hooks),
            "GENESIS_DEGRADED_AUDIT_DIR": str(store),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    rows = _records(store)
    assert len(rows) == 1
    assert rows[0]["source"] == "poisoned_guard"
    assert rows[0]["verdict"] == "allowed"
    assert rows[0]["operation"] == "no_gated_operation"
    assert "printf ok" not in rows[0]["reason"]


def test_bash_safety_fallback_is_recorded(tmp_path: Path):
    root = tmp_path / "partial"
    hooks = root / "scripts" / "hooks"
    hooks.mkdir(parents=True)
    hook = root / "scripts" / "bash_safety_hook.sh"
    shutil.copy(_ROOT / "scripts" / "bash_safety_hook.sh", hook)
    shutil.copy(_WRITER, hooks / "degraded_audit.sh")
    store = tmp_path / "audit"
    result = subprocess.run(
        [_BASH, str(hook)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "git checkout foo"}}),
        cwd=tmp_path,
        env={**os.environ, "GENESIS_DEGRADED_AUDIT_DIR": str(store)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    rows = _records(store)
    assert len(rows) == 1
    assert rows[0]["source"] == "bash_safety_hook"
    assert rows[0]["reason"] == "git_discard_guard_unavailable"
    assert rows[0]["verdict"] == "allowed"


def test_genesis_launcher_records_missing_venv(tmp_path: Path):
    root = tmp_path / "genesis"
    (root / ".claude" / "hooks").mkdir(parents=True)
    (root / "scripts" / "hooks").mkdir(parents=True)
    wrapper = root / ".claude" / "hooks" / "genesis-hook"
    shutil.copy(_ROOT / ".claude" / "hooks" / "genesis-hook", wrapper)
    wrapper.chmod(0o755)
    shutil.copy(_WRITER, root / "scripts" / "hooks")
    store = tmp_path / "audit"
    result = subprocess.run(
        [str(root / ".claude" / "hooks" / "genesis-hook"), "missing.py"],
        cwd=root,
        env={
            **os.environ,
            "HOME": str(tmp_path / "home"),
            "GENESIS_DEGRADED_AUDIT_DIR": str(store),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    rows = _records(store)
    assert len(rows) == 1
    assert rows[0]["source"] == "genesis-hook"
    assert rows[0]["reason"] == "genesis_venv_unavailable"
