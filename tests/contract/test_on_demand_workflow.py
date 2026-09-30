"""Offline gates for the governed on-demand orchestration."""

import base64
import hashlib
import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("on_demand", ROOT / "scripts/run-on-demand.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

BACKUP = "23d44059-4793-4d64-b784-4ee270868b49"
SOURCE = (
    "/subscriptions/source/resourceGroups/source"
    "/providers/Microsoft.DocumentDB/databaseAccounts/source"
)
TARGET = (
    "/subscriptions/target/resourceGroups/target"
    "/providers/Microsoft.DocumentDB/databaseAccounts/target"
)


def plan():
    return {
        "schema_version": 1,
        "backup_id": BACKUP,
        "source_account_resource_id": SOURCE,
        "target_account_resource_id": TARGET,
        "table_names": ["Alpha", "Bravo"],
        "target_table_endpoint": "https://target.table.cosmos.azure.com",
        "backup_storage_account_url": "https://storage.blob.core.windows.net",
        "backup_container_name": "backups",
        "manifest_sha256": "a" * 64,
    }


def test_plan_requires_exact_backup_resource_and_bounded_safe_tables():
    assert MODULE.validate_plan(plan(), BACKUP, SOURCE, TARGET) == plan()
    cases = [
        None,
        {**plan(), "backup_id": "other"},
        {**plan(), "source_account_resource_id": TARGET},
        {**plan(), "target_account_resource_id": SOURCE},
        {**plan(), "table_names": []},
        {**plan(), "table_names": ["cards"]},
        {**plan(), "table_names": ["Alpha", "alpha"]},
        {**plan(), "table_names": ["--unsafe"]},
        {**plan(), "table_names": ["X"]},
        {**plan(), "table_names": [f"Table{i}" for i in range(101)]},
    ]
    for value in cases:
        with pytest.raises(MODULE.SmokeError):
            MODULE.validate_plan(value, BACKUP, SOURCE, TARGET)


def test_envelopes_do_not_confuse_http_status_with_completion():
    raw = "\n".join(
        json.dumps({"Log": message})
        for message in [
            "Response status: 200",
            '{"event":"restore.data_verified","backup_id":"wrong"}',
        ]
    )
    with pytest.raises(MODULE.SmokeError):
        MODULE.event(MODULE.log_messages(raw), "restore.data_verified", BACKUP)
    message = json.dumps({"event": "restore.data_verified", "backup_id": BACKUP, "tables": 2})
    assert (
        MODULE.event(["2026-09-30T17:00:00Z " + message], "restore.data_verified", BACKUP)["tables"]
        == 2
    )
    with pytest.raises(MODULE.SmokeError):
        MODULE.event([message, message], "restore.data_verified", BACKUP)


def test_private_plan_transport_fails_closed_on_tampering_and_duplicate_frames():
    raw = json.dumps(plan()).encode()
    encoded = base64.b64encode(raw).decode()
    chunks = [encoded[index : index + 128] for index in range(0, len(encoded), 128)]
    digest = hashlib.sha256(raw).hexdigest()
    frames = [
        f"restore.plan.part:{i}/{len(chunks)}:{digest}:{chunk}" for i, chunk in enumerate(chunks)
    ]
    complete = f"restore.plan.complete:{len(chunks)}:{digest}"
    assert MODULE.decode_plan([*frames, complete]) == plan()
    assert MODULE.decode_plan([*frames[:-1], complete]) is None
    for messages in (
        [*frames, frames[0], complete],
        [*frames, complete, complete],
        [*frames, f"restore.plan.complete:{len(chunks)}:" + "0" * 64],
        [frames[0], f"restore.plan.part:1/57:{digest}:AAAA", complete],
        [f"restore.plan.part:0/58:{digest}:AAAA", complete],
        [f"restore.plan.part:0/1:{digest}:" + "A" * 385, complete],
    ):
        with pytest.raises(MODULE.SmokeError):
            MODULE.decode_plan(messages)


def test_manual_and_unique_container_required():
    job = {
        "properties": {
            "configuration": {"triggerType": "Manual"},
            "template": {"containers": [{"name": "restore-validation"}]},
        }
    }
    assert MODULE.container_for(job, "restore-validation")["name"] == "restore-validation"
    job["properties"]["configuration"]["triggerType"] = "Schedule"
    with pytest.raises(MODULE.SmokeError):
        MODULE.container_for(job, "restore-validation")


def test_reset_uses_arm_only_and_validates_exact_final_set(monkeypatch):
    flow = MODULE.Orchestrator("subscription", "group")
    flow.tables = Mock(side_effect=[{"Old"}, set(), {"Alpha", "Bravo"}])
    az = Mock()
    monkeypatch.setattr(MODULE, "azure", az)
    flow.reset("target", {"Alpha", "Bravo"})
    calls = [call.args for call in az.call_args_list]
    assert len(calls) == 3
    assert calls[0][:3] == ("cosmosdb", "table", "delete")
    assert all("target" in args and "--throughput" not in args for args in calls)
    assert all(args[:3] == ("cosmosdb", "table", "create") for args in calls[1:])


def test_failed_reset_never_starts_creation(monkeypatch):
    flow = MODULE.Orchestrator("subscription", "group")
    flow.tables = Mock(side_effect=[{"Old"}, {"Old"}])
    az = Mock()
    monkeypatch.setattr(MODULE, "azure", az)
    with pytest.raises(MODULE.SmokeError):
        flow.reset("target", {"Alpha"})
    assert az.call_count == 1


def test_execution_override_keeps_job_template_and_pins_uuid(monkeypatch):
    flow = MODULE.Orchestrator("subscription", "group")
    job = {
        "properties": {
            "template": {
                "containers": [{"env": [{"name": "SETTING", "value": "preserved"}]}],
                "initContainers": [],
            }
        }
    }
    flow.idle = Mock()
    flow.job = Mock(return_value=job)

    def start(*args):
        assert args[:3] == ("containerapp", "job", "start")
        template = json.loads(Path(args[args.index("--yaml") + 1]).read_text())
        container = template["containers"][0]
        assert container["command"] == ["python", "-m", "cosmos_table_backup.cli"]
        assert container["args"] == ["restore-test", "--data-only"]
        assert {entry["name"]: entry["value"] for entry in container["env"]} == {
            "SETTING": "preserved",
            "GOVERNED_CONSOLE_HOLD_SECONDS": "180",
            "RESTORE_BACKUP_ID": BACKUP,
        }
        return {"name": "execution"}

    monkeypatch.setattr(MODULE, "azure", start)
    original = json.dumps(job)
    assert flow.start("restore", job, ["restore-test", "--data-only"], BACKUP) == "execution"
    assert json.dumps(job) == original
    flow.job = Mock(return_value={"properties": {"template": {}}})
    with pytest.raises(MODULE.SmokeError):
        flow.start("restore", job, [], BACKUP)


def test_deadline_stops_only_owned_execution(monkeypatch):
    flow = MODULE.Orchestrator("subscription", "group")
    az = Mock()
    monkeypatch.setattr(MODULE, "azure", az)
    with pytest.raises(MODULE.SmokeError):
        flow.wait("restore", "owned-execution", 0)
    assert az.call_args.args[:3] == ("containerapp", "job", "stop")
    assert az.call_args.args[-2:] == ("--job-execution-name", "owned-execution")


def test_failed_runtime_cannot_be_accepted(monkeypatch):
    flow = MODULE.Orchestrator("subscription", "group")
    az = Mock(return_value={"properties": {"status": "Failed"}})
    flow.messages = Mock(return_value=[])
    monkeypatch.setattr(MODULE, "azure", az)
    with pytest.raises(MODULE.SmokeError):
        flow.wait("restore", "failed", 10)
    assert az.call_count == 1


def test_main_requires_aggregate_runtime_and_arm_evidence(monkeypatch, tmp_path):
    source = SOURCE
    target = (
        "/subscriptions/sub/resourceGroups/group"
        "/providers/Microsoft.DocumentDB/databaseAccounts/target"
    )
    values = {
        "SUBSCRIPTION_ID": "sub",
        "RESOURCE_GROUP": "group",
        "SOURCE_ACCOUNT_ID": source,
        "BACKUP_JOB_NAME": "backup",
        "RESTORE_JOB_NAME": "restore",
        "REGISTRY_SERVER": "registry",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    config = {
        "RESTORE_TARGET_COSMOS_ACCOUNT_RESOURCE_ID": target,
        "RESTORE_SOURCE_ACCOUNT_RESOURCE_ID": source,
        "RESTORE_REQUIRE_ISOLATED_TARGET": "true",
        "RESTORE_TARGET_TABLE_ENDPOINT": "https://target.table.cosmos.azure.com",
        "EXCLUDED_TABLES_JSON": '["cards"]',
        "COSMOS_TABLE_ENDPOINT": "https://source.table.cosmos.azure.com",
        "BACKUP_CONTAINER": "backups",
        "BACKUP_CONTAINER_NAME": "backups",
        "BACKUP_STORAGE_ACCOUNT_URL": "https://storage.blob.core.windows.net",
    }

    def job(name):
        return {
            "properties": {
                "configuration": {"triggerType": "Manual"},
                "template": {
                    "containers": [
                        {
                            "name": "backup" if name == "backup" else "restore-validation",
                            "image": "registry/cosmos-table-backup@sha256:" + "a" * 64,
                            "env": [{"name": key, "value": value} for key, value in config.items()],
                        }
                    ]
                },
            }
        }

    flow = Mock()
    flow.common = ("--subscription", "sub", "--resource-group", "group")
    flow.job.side_effect = job
    flow.start.side_effect = ["planning", "restore-run"]
    decoded = {**plan(), "source_account_resource_id": source, "target_account_resource_id": target}
    flow.tables.return_value = {"Alpha", "Bravo"}
    flow.completion.return_value = {
        "event": "restore.data_verified",
        "backup_id": BACKUP,
        "status": "data_verified_pending_table_set",
        "table_count": 2,
        "entity_count": 14,
    }
    live = {
        "tags": {"purpose": "isolated-restore-validation", "sourceData": "prohibited"},
        "publicNetworkAccess": "Disabled",
        "disableLocalAuth": True,
    }
    monkeypatch.setattr(MODULE, "Orchestrator", Mock(return_value=flow))
    monkeypatch.setattr(MODULE, "azure", Mock(return_value=live))
    monkeypatch.setattr(MODULE, "decode_plan", Mock(return_value=decoded))
    monkeypatch.setattr(MODULE.signal, "signal", Mock())
    report = tmp_path / "result.json"
    monkeypatch.setattr(MODULE, "REPORT", {"schema_version": 1})
    monkeypatch.setattr(
        "sys.argv", ["run-on-demand.py", "--backup-id", BACKUP, "--report", str(report)]
    )
    assert MODULE.main() == 0
    result = json.loads(report.read_text())
    assert result["status"] == "passed" and result["table_set_verified"]
    assert result["entities"] == 14
    assert "tables" not in result or result["tables"] == 2
    flow.reset.assert_called_once_with("target", {"Alpha", "Bravo"})
    flow.tables.return_value = {"Alpha", "Bravo", "Unexpected"}
    flow.start.side_effect = ["planning", "restore-run"]
    with pytest.raises(MODULE.SmokeError):
        MODULE.main()


def test_workflow_main_environment_and_safe_inputs():
    text = (ROOT / ".github/workflows/run-on-demand.yml").read_text()
    assert "github.ref == 'refs/heads/main'" in text
    assert "environment: backup-infrastructure" in text
    assert "cancel-in-progress: false" in text
    assert "INPUT_BACKUP_ID: ${{ inputs.backup_id }}" in text
    assert '--backup-id "$INPUT_BACKUP_ID"' in text
    assert "smoke-result.json" in text
    assert "path: plan" not in text
    assert "id-token: write" in text
    assert "timeout-minutes: 355" in text
    assert "if: always()" in text
    for filename in ("deploy-backup.yml", "deploy-image.yml", "publish-image.yml"):
        assert (
            "group: backup-operations-${{ github.repository }}"
            in (ROOT / ".github/workflows" / filename).read_text()
        )


def test_live_evidence_survives_replica_removal_but_requires_terminal_success(monkeypatch):
    flow = MODULE.Orchestrator("sub", "group")
    states = iter(["Running", "Succeeded"])
    monkeypatch.setattr(MODULE, "azure", lambda *args: {"properties": {"status": next(states)}})
    monkeypatch.setattr(MODULE.time, "sleep", Mock())
    messages = ['{"event":"restore.data_verified","backup_id":"' + BACKUP + '"}']
    flow.messages = Mock(side_effect=[messages, MODULE.SmokeError("replica removed")])
    flow.wait("restore", "execution", 10)
    assert flow.evidence["execution"] == messages
    flow.messages = MODULE.Orchestrator.messages.__get__(flow)
    monkeypatch.setattr(MODULE.subprocess, "run", Mock(return_value=Mock(returncode=1, stdout="")))
    assert flow.messages("restore", "execution", "restore-validation") == messages
    assert flow.completion(
        "restore", "execution", "restore-validation", "restore.data_verified", BACKUP
    )


def test_governed_console_window_is_explicit_and_bounded(monkeypatch):
    from cosmos_table_backup import supervisor

    sleep = Mock()
    monkeypatch.setattr(supervisor.time, "sleep", sleep)
    monkeypatch.delenv("GOVERNED_CONSOLE_HOLD_SECONDS", raising=False)
    supervisor.collection_window()
    sleep.assert_not_called()
    monkeypatch.setenv("GOVERNED_CONSOLE_HOLD_SECONDS", "180")
    supervisor.collection_window()
    sleep.assert_called_once_with(180)
    for value in ("-1", "181", "unbounded"):
        monkeypatch.setenv("GOVERNED_CONSOLE_HOLD_SECONDS", value)
        with pytest.raises(ValueError):
            supervisor.collection_window()
