"""Offline timing contracts; no multi-hour or Azure execution is performed."""

import importlib.util
import json
import re
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "timeout_on_demand", ROOT / "scripts/run-on-demand.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def backup_job(timeout=7500):
    return {
        "properties": {
            "configuration": {"triggerType": "Manual", "replicaTimeout": timeout},
            "template": {"containers": [{"name": "backup", "env": []}]},
        }
    }


def test_infrastructure_and_workflow_cover_bounded_backup_budget():
    bicep = (ROOT / "infra/deployment/backup.bicep").read_text()
    backup, restore = bicep.split("module restoreJob", 1)
    timeout = int(re.search(r"replicaTimeout: (\d+)", backup)[1])
    assert MODULE.BACKUP_WORK_SECONDS == 7200
    assert MODULE.CONSOLE_HOLD_SECONDS == 180
    assert MODULE.BACKUP_PROCESS_MARGIN_SECONDS == 120
    assert timeout == MODULE.BACKUP_REPLICA_TIMEOUT_SECONDS == 7500
    assert MODULE.BACKUP_DEADLINE_SECONDS == timeout + 300 == 7800
    assert "replicaRetryLimit: 1" in backup
    assert "replicaTimeout: 14400" in restore and "replicaRetryLimit: 0" in restore
    # Ordinary executions inherit the job ceiling but still do not request a hold.
    assert "GOVERNED_CONSOLE_HOLD_SECONDS" not in bicep
    workflow = (ROOT / ".github/workflows/run-on-demand.yml").read_text()
    minutes = int(re.search(r"timeout-minutes: (\d+)", workflow)[1])
    deadlines = (
        MODULE.BACKUP_DEADLINE_SECONDS + MODULE.PLAN_DEADLINE_SECONDS + MODULE.DATA_DEADLINE_SECONDS
    )
    assert minutes == 355
    assert minutes * 60 - deadlines == 2700


@pytest.mark.parametrize("work_seconds", [7199, 7200])
def test_near_budget_work_plus_hold_can_reach_terminal_success(monkeypatch, work_seconds):
    flow = MODULE.Orchestrator("subscription", "group")
    flow.active = ("backup", "owned")
    flow.messages = Mock(return_value=['{"event":"backup.completed"}'])
    elapsed = [0]
    finish = work_seconds + MODULE.CONSOLE_HOLD_SECONDS + MODULE.BACKUP_PROCESS_MARGIN_SECONDS
    assert finish <= MODULE.BACKUP_REPLICA_TIMEOUT_SECONDS
    assert finish > 7200  # The old replica ceiling would invalidate this successful work.
    monkeypatch.setattr(MODULE.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(
        MODULE.time, "sleep", lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)
    )
    calls = []

    def azure(*args):
        calls.append(args)
        status = "Succeeded" if elapsed[0] >= finish + 15 else "Running"
        return {"properties": {"status": status}}

    monkeypatch.setattr(MODULE, "azure", azure)
    flow.wait("backup", "owned", MODULE.BACKUP_DEADLINE_SECONDS)
    assert elapsed[0] < MODULE.BACKUP_DEADLINE_SECONDS
    assert flow.active is None
    assert not any(args[:3] == ("containerapp", "job", "stop") for args in calls)


def test_completion_evidence_does_not_bypass_deadline(monkeypatch):
    flow = MODULE.Orchestrator("subscription", "group")
    flow.active = ("backup", "owned")
    flow.messages = Mock(return_value=['{"event":"backup.completed"}'])
    elapsed = [0]
    monkeypatch.setattr(MODULE.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(
        MODULE.time, "sleep", lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)
    )
    azure = Mock(return_value={"properties": {"status": "Running"}})
    monkeypatch.setattr(MODULE, "azure", azure)
    with pytest.raises(MODULE.SmokeError, match="exceeded orchestration deadline"):
        flow.wait("backup", "owned", MODULE.BACKUP_DEADLINE_SECONDS)
    assert elapsed[0] == MODULE.BACKUP_DEADLINE_SECONDS
    assert azure.call_args.args[:3] == ("containerapp", "job", "stop")
    assert azure.call_args.args[-2:] == ("--job-execution-name", "owned")
    assert flow.active is None


@pytest.mark.parametrize("timeout", [None, True, 7200, 7379, 7499, 7501, 86400, "7500"])
def test_stale_or_altered_timeout_fails_before_start(monkeypatch, timeout):
    flow = MODULE.Orchestrator("subscription", "group")
    job = backup_job(timeout)
    flow.idle = Mock()
    flow.job = Mock(return_value=job)
    azure = Mock()
    monkeypatch.setattr(MODULE, "azure", azure)
    with pytest.raises(MODULE.SmokeError, match="timeout budget"):
        flow.start("backup", job, [], None)
    azure.assert_not_called()


def test_backup_start_uses_only_container_overrides_and_checks_configuration(monkeypatch):
    flow = MODULE.Orchestrator("subscription", "group")
    job = backup_job()
    flow.idle = Mock()
    flow.job = Mock(return_value=job)
    original = json.dumps(job)

    def azure(*args):
        template = json.loads(Path(args[args.index("--yaml") + 1]).read_text())
        assert set(template) == {"containers"}
        assert template["containers"][0]["env"] == [
            {"name": "GOVERNED_CONSOLE_HOLD_SECONDS", "value": "180"}
        ]
        return {"name": "owned"}

    monkeypatch.setattr(MODULE, "azure", azure)
    assert flow.start("backup", job, [], None) == "owned"
    assert json.dumps(job) == original
    flow.job = Mock(return_value=backup_job(7200))
    with pytest.raises(MODULE.SmokeError, match="configuration changed"):
        flow.start("backup", job, [], None)
