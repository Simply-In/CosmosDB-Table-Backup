#!/usr/bin/env python3
# Arguments are validated bindings and passed without a shell to the operator's Azure CLI.
# ruff: noqa: S603, S607
"""Governed private/keyless backup and isolated restore orchestration."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import UUID

BACKUP_WORK_SECONDS = 7200
CONSOLE_HOLD_SECONDS = 180
BACKUP_PROCESS_MARGIN_SECONDS = 120
BACKUP_REPLICA_TIMEOUT_SECONDS = (
    BACKUP_WORK_SECONDS + CONSOLE_HOLD_SECONDS + BACKUP_PROCESS_MARGIN_SECONDS
)
BACKUP_STATUS_MARGIN_SECONDS = 300
BACKUP_DEADLINE_SECONDS = BACKUP_REPLICA_TIMEOUT_SECONDS + BACKUP_STATUS_MARGIN_SECONDS
PLAN_DEADLINE_SECONDS = 3600
DATA_DEADLINE_SECONDS = 7200

REPORT: dict = {"schema_version": 1, "status": "started", "stage": "configuration"}
REPORT_PATH = Path("smoke-result.json")


class SmokeError(RuntimeError):
    """A fail-closed orchestration gate failed."""


def azure(*args: str) -> object:
    result = subprocess.run(
        ["az", *args, "--only-show-errors", "--output", "json"],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode:
        raise SmokeError("Azure operation failed; inspect private operator diagnostics")
    try:
        return json.loads(result.stdout or "null")
    except ValueError as exc:
        raise SmokeError("Azure returned invalid JSON") from exc


def environment(container: dict) -> dict[str, str]:
    entries = container.get("env", [])
    values = {entry["name"]: entry.get("value", "") for entry in entries}
    if len(values) != len(entries) or any("secretRef" in entry for entry in entries):
        raise SmokeError("Unexpected duplicate or secret-based job configuration")
    return values


def container_for(job: dict, name: str) -> dict:
    if job["properties"]["configuration"]["triggerType"] != "Manual":
        raise SmokeError("On-demand execution requires Manual jobs")
    containers = job["properties"]["template"]["containers"]
    if len(containers) != 1 or containers[0]["name"] != name:
        raise SmokeError("Unexpected job container")
    return containers[0]


def validate_plan(plan: object, backup_id: str, source: str, target: str) -> dict:
    if not isinstance(plan, dict) or plan.get("schema_version") != 1:
        raise SmokeError("Invalid plan schema")
    if plan.get("backup_id") != backup_id:
        raise SmokeError("Plan backup binding mismatch")
    for field, expected in (
        ("source_account_resource_id", source),
        ("target_account_resource_id", target),
    ):
        if str(plan.get(field, "")).rstrip("/").lower() != expected.rstrip("/").lower():
            raise SmokeError("Plan resource binding mismatch")
    tables = plan.get("table_names")
    if not isinstance(tables, list) or not 1 <= len(tables) <= 100:
        raise SmokeError("Plan table bound exceeded")
    if any(
        not isinstance(name, str)
        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{2,62}", name)
        or name == "cards"
        for name in tables
    ):
        raise SmokeError("Invalid or protected plan table")
    if len(set(tables)) != len(tables) or len({name.lower() for name in tables}) != len(tables):
        raise SmokeError("Ambiguous plan table set")
    return plan


def log_messages(raw: str) -> list[str]:
    messages = []
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            # The pinned CLI can emit unescaped quotes inside its Log JSON string.
            match = re.fullmatch(r'\{"TimeStamp":"[0-9TZ:+.\-]+","Log":"(.*)"\}', line)
            if match:
                messages.append(match.group(1))
            continue
        if isinstance(entry, dict):
            text = entry.get("Log", "")
            if isinstance(text, str):
                messages.append(text)
    return messages


def event(messages: list[str], name: str, backup_id: str | None = None) -> dict:
    found = []
    for message in messages:
        # Direct ACA console envelopes can prepend an RFC3339 timestamp.
        start = message.find("{")
        if start < 0:
            continue
        try:
            value = json.loads(message[start:])
        except ValueError:
            continue
        if (
            isinstance(value, dict)
            and value.get("event") == name
            and (backup_id is None or value.get("backup_id") == backup_id)
        ):
            found.append(value)
    if len(found) != 1:
        raise SmokeError("Required unique completion event not available")
    return found[0]


def decode_plan(messages: list[str]) -> object | None:
    parts: dict[int, str] = {}
    total = None
    digest = None
    complete = False
    for message in messages:
        match = re.search(
            r"restore\.plan\.part:(\d+)/(\d+):([0-9a-f]{64}):([A-Za-z0-9+/=]+)$", message
        )
        if match:
            index, size = int(match[1]), int(match[2])
            if not 1 <= size <= 57 or index >= size or index in parts or len(match[4]) > 384:
                raise SmokeError("Invalid plan framing")
            if (total is not None and size != total) or (digest is not None and digest != match[3]):
                raise SmokeError("Inconsistent plan frame binding")
            total, digest = size, match[3]
            parts[index] = match[4]
        match = re.search(r"restore\.plan\.complete:(\d+):([0-9a-f]{64})$", message)
        if match:
            if (
                complete
                or (total is not None and int(match[1]) != total)
                or (digest is not None and digest != match[2])
            ):
                raise SmokeError("Invalid plan completion")
            total, digest, complete = int(match[1]), match[2], True
    if not total or not digest or not complete or len(parts) != total:
        return None
    encoded = "".join(parts[index] for index in range(total))
    if len(encoded) > 21848:
        raise SmokeError("Plan payload bound exceeded")
    raw = base64.b64decode(encoded, validate=True)
    if len(raw) > 16384 or hashlib.sha256(raw).hexdigest() != digest:
        raise SmokeError("Plan transport integrity failed")
    return json.loads(raw)


class Orchestrator:
    def __init__(self, subscription: str, resource_group: str):
        self.subscription = subscription
        self.resource_group = resource_group
        self.common = ("--subscription", subscription, "--resource-group", resource_group)
        self.active: tuple[str, str] | None = None
        self.evidence: dict[str, list[str]] = {}

    def job(self, name: str) -> dict:
        return azure("containerapp", "job", "show", *self.common, "--name", name)

    def idle(self, name: str) -> None:
        executions = azure("containerapp", "job", "execution", "list", *self.common, "--name", name)
        if any(
            item["properties"]["status"] in {"Running", "Processing", "Pending"}
            for item in executions
        ):
            raise SmokeError("Concurrent job execution exists")

    def start(
        self,
        name: str,
        job: dict,
        arguments: list[str],
        backup_id: str | None,
        preparation: dict | None = None,
    ) -> str:
        self.idle(name)
        current = self.job(name)
        if current["properties"]["template"] != job["properties"]["template"] or current[
            "properties"
        ].get("configuration") != job["properties"].get("configuration"):
            raise SmokeError("Job configuration changed during orchestration")
        if not arguments and (
            type(job["properties"].get("configuration", {}).get("replicaTimeout")) is not int
            or job["properties"]["configuration"]["replicaTimeout"]
            != BACKUP_REPLICA_TIMEOUT_SECONDS
        ):
            raise SmokeError("Backup job must reserve the governed timeout budget")
        template = json.loads(json.dumps(job["properties"]["template"]))
        container = template["containers"][0]
        container["command"] = ["python", "-m", "cosmos_table_backup.cli"]
        container["args"] = arguments
        container["env"] = [
            entry
            for entry in container["env"]
            if entry["name"]
            not in {"RESTORE_BACKUP_ID", "RESTORE_PREPARATION_JSON", "RESTORE_DATA_ONLY"}
        ]
        container["env"] = [
            entry for entry in container["env"] if entry["name"] != "GOVERNED_CONSOLE_HOLD_SECONDS"
        ]
        container["env"].append(
            {"name": "GOVERNED_CONSOLE_HOLD_SECONDS", "value": str(CONSOLE_HOLD_SECONDS)}
        )
        if backup_id:
            container["env"].append({"name": "RESTORE_BACKUP_ID", "value": backup_id})
        if preparation is not None:
            container["env"].append(
                {"name": "RESTORE_PREPARATION_JSON", "value": json.dumps(preparation)}
            )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "execution.json"
            path.write_text(json.dumps(template))
            path.chmod(0o600)
            execution = azure(
                "containerapp", "job", "start", *self.common, "--name", name, "--yaml", str(path)
            )
        self.active = (name, execution["name"])
        return execution["name"]

    def cancel(self) -> None:
        if self.active is not None:
            name, execution = self.active
            azure(
                "containerapp",
                "job",
                "stop",
                *self.common,
                "--name",
                name,
                "--job-execution-name",
                execution,
            )
            self.active = None

    def wait(self, name: str, execution: str, timeout: int) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = azure(
                "containerapp",
                "job",
                "execution",
                "show",
                *self.common,
                "--name",
                name,
                "--job-execution-name",
                execution,
            )
            status = value["properties"]["status"]
            container = (
                "backup" if name == os.environ.get("BACKUP_JOB_NAME") else "restore-validation"
            )
            try:
                messages = self.messages(name, execution, container)
                if messages:
                    self.evidence[execution] = messages
            except SmokeError:
                pass
            if status == "Succeeded":
                self.active = None
                return
            if status in {"Failed", "Stopped", "Canceled", "Cancelled"}:
                self.active = None
                raise SmokeError("Runtime execution did not succeed")
            time.sleep(15)
        azure(
            "containerapp",
            "job",
            "stop",
            *self.common,
            "--name",
            name,
            "--job-execution-name",
            execution,
        )
        self.active = None
        raise SmokeError("Runtime execution exceeded orchestration deadline")

    def messages(self, name: str, execution: str, container: str) -> list[str]:
        try:
            result = subprocess.run(
                [
                    "az",
                    "containerapp",
                    "job",
                    "logs",
                    "show",
                    *self.common,
                    "--name",
                    name,
                    "--execution",
                    execution,
                    "--container",
                    container,
                    "--tail",
                    "300",
                    "--format",
                    "json",
                    "--only-show-errors",
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            result = None
        if result is None or result.returncode:
            if execution in self.evidence:
                return self.evidence[execution]
            raise SmokeError("Private execution logs unavailable")
        messages = log_messages(result.stdout)
        return messages or self.evidence.get(execution, [])

    def completion(
        self, name: str, execution: str, container: str, kind: str, backup_id: str | None = None
    ) -> dict:
        for _ in range(12):
            try:
                return event(self.messages(name, execution, container), kind, backup_id)
            except SmokeError:
                time.sleep(10)
        raise SmokeError("Completion evidence unavailable; refusing acceptance")

    def tables(self, account: str) -> set[str]:
        rows = azure("cosmosdb", "table", "list", *self.common, "--account-name", account)
        if len(rows) > 100:
            raise SmokeError("Target table bound exceeded")
        return {row["name"].rsplit("/", 1)[-1] for row in rows}

    def reset(self, account: str, expected: set[str]) -> None:
        for name in sorted(self.tables(account)):
            azure(
                "cosmosdb",
                "table",
                "delete",
                *self.common,
                "--account-name",
                account,
                "--name",
                name,
                "--yes",
            )
        if self.tables(account):
            raise SmokeError("Target deletion has not converged")
        for name in sorted(expected):
            # No throughput argument: the isolated account is serverless.
            azure(
                "cosmosdb",
                "table",
                "create",
                *self.common,
                "--account-name",
                account,
                "--name",
                name,
            )
        if self.tables(account) != expected:
            raise SmokeError("Prepared target table set mismatch")


def main() -> int:
    global REPORT_PATH
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-id", default="", help="Empty starts a fresh backup first")
    parser.add_argument(
        "--report", default="smoke-result.json", help="JSON destination for success or failure"
    )
    arguments = parser.parse_args()
    REPORT_PATH = Path(arguments.report)
    required = [
        "SUBSCRIPTION_ID",
        "RESOURCE_GROUP",
        "SOURCE_ACCOUNT_ID",
        "BACKUP_JOB_NAME",
        "RESTORE_JOB_NAME",
        "REGISTRY_SERVER",
    ]
    if any(not os.environ.get(name) for name in required):
        raise SmokeError("Missing governed workflow configuration")
    values = {name: os.environ[name] for name in required}
    flow = Orchestrator(values["SUBSCRIPTION_ID"], values["RESOURCE_GROUP"])

    def canceled(_signal: int, _frame: object) -> None:
        flow.cancel()
        raise SmokeError("Workflow canceled")

    signal.signal(signal.SIGTERM, canceled)
    signal.signal(signal.SIGINT, canceled)
    backup = flow.job(values["BACKUP_JOB_NAME"])
    restore = flow.job(values["RESTORE_JOB_NAME"])
    reader = container_for(backup, "backup")
    writer = container_for(restore, "restore-validation")
    image = writer["image"]
    if image != reader["image"] or not re.fullmatch(
        re.escape(values["REGISTRY_SERVER"]) + r"/cosmos-table-backup@sha256:[0-9a-f]{64}", image
    ):
        raise SmokeError("Jobs must share the governed immutable image")
    config = environment(writer)
    source = values["SOURCE_ACCOUNT_ID"].rstrip("/")
    backup_config = environment(reader)
    exclusions = json.loads(backup_config.get("EXCLUDED_TABLES_JSON", "null"))
    if not isinstance(exclusions, list) or "cards" not in exclusions:
        raise SmokeError("Protected source exclusion missing")
    source_name = source.rsplit("/", 1)[-1]
    if (
        backup_config.get("COSMOS_TABLE_ENDPOINT")
        != f"https://{source_name}.table.cosmos.azure.com"
    ):
        raise SmokeError("Backup source endpoint binding mismatch")
    if backup_config.get("BACKUP_STORAGE_ACCOUNT_URL") != config.get("BACKUP_STORAGE_ACCOUNT_URL"):
        raise SmokeError("Backup storage binding mismatch")
    if backup_config.get("BACKUP_CONTAINER_NAME") != config.get("BACKUP_CONTAINER"):
        raise SmokeError("Backup container binding mismatch")
    target = config["RESTORE_TARGET_COSMOS_ACCOUNT_RESOURCE_ID"].rstrip("/")
    prefix = (
        f"/subscriptions/{values['SUBSCRIPTION_ID']}/resourceGroups/"
        f"{values['RESOURCE_GROUP']}/providers/Microsoft.DocumentDB/databaseAccounts/"
    )
    account = target[len(prefix) :] if target.lower().startswith(prefix.lower()) else ""
    if not re.fullmatch(r"[a-z0-9-]{3,44}", account) or target.lower() == source.lower():
        raise SmokeError("Target is outside the approved isolated boundary")
    if config.get("RESTORE_SOURCE_ACCOUNT_RESOURCE_ID", "").lower() != source.lower():
        raise SmokeError("Job source binding mismatch")
    if (
        config.get("RESTORE_REQUIRE_ISOLATED_TARGET") != "true"
        or config.get("RESTORE_TARGET_TABLE_ENDPOINT")
        != f"https://{account}.table.cosmos.azure.com"
    ):
        raise SmokeError("Job target binding mismatch")
    live = azure("cosmosdb", "show", *flow.common, "--name", account)
    if (
        live.get("tags", {}).get("purpose") != "isolated-restore-validation"
        or live.get("tags", {}).get("sourceData") != "prohibited"
        or live.get("publicNetworkAccess") != "Disabled"
        or not live.get("disableLocalAuth")
    ):
        raise SmokeError("Target isolation/account security gate failed")
    flow.idle(values["RESTORE_JOB_NAME"])
    backup_id = str(UUID(arguments.backup_id)) if arguments.backup_id else None
    result = REPORT
    result.update({"image": image, "backup_status": "existing_committed", "stage": "backup"})
    if backup_id is None:
        execution = flow.start(values["BACKUP_JOB_NAME"], backup, [], None)
        result["backup_execution"] = execution
        flow.wait(values["BACKUP_JOB_NAME"], execution, BACKUP_DEADLINE_SECONDS)
        completed = flow.completion(
            values["BACKUP_JOB_NAME"], execution, "backup", "backup.completed"
        )
        backup_id = str(UUID(completed["backup_id"]))
        result["backup_status"] = "passed"
    result["backup_id"] = backup_id
    result["stage"] = "authenticated_plan"
    planning = flow.start(
        values["RESTORE_JOB_NAME"], restore, ["restore-test", "--plan"], backup_id
    )
    result["planning_execution"] = planning
    flow.wait(values["RESTORE_JOB_NAME"], planning, PLAN_DEADLINE_SECONDS)
    # Plan transport is finalized by the authenticated runtime implementation.
    plan = None
    for _ in range(12):
        messages = flow.messages(values["RESTORE_JOB_NAME"], planning, "restore-validation")
        decoded = decode_plan(messages)
        if decoded is not None:
            plan = validate_plan(decoded, backup_id, source, target)
            break
        time.sleep(10)
    if plan is None:
        raise SmokeError("Authenticated plan unavailable")
    if (
        plan.get("target_table_endpoint") != config["RESTORE_TARGET_TABLE_ENDPOINT"]
        or plan.get("backup_storage_account_url") != config["BACKUP_STORAGE_ACCOUNT_URL"]
        or plan.get("backup_container_name") != config["BACKUP_CONTAINER"]
        or not re.fullmatch(r"[0-9a-f]{64}", str(plan.get("manifest_sha256", "")))
    ):
        raise SmokeError("Plan storage/endpoint/manifest binding mismatch")
    expected = set(plan["table_names"])
    flow.idle(values["BACKUP_JOB_NAME"])
    flow.idle(values["RESTORE_JOB_NAME"])
    if (
        flow.job(values["RESTORE_JOB_NAME"])["properties"]["template"]
        != restore["properties"]["template"]
    ):
        raise SmokeError("Restore configuration changed before target mutation")
    if azure("cosmosdb", "show", *flow.common, "--name", account) != live:
        raise SmokeError("Target configuration changed before mutation")
    result["stage"] = "isolated_target_preparation"
    flow.reset(account, expected)
    result["stage"] = "restore_data_verification"
    execution = flow.start(
        values["RESTORE_JOB_NAME"], restore, ["restore-test", "--data-only"], backup_id, plan
    )
    result["restore_execution"] = execution
    flow.wait(values["RESTORE_JOB_NAME"], execution, DATA_DEADLINE_SECONDS)
    verified = flow.completion(
        values["RESTORE_JOB_NAME"],
        execution,
        "restore-validation",
        "restore.data_verified",
        backup_id,
    )
    result["stage"] = "final_account_verification"
    if flow.tables(account) != expected:
        raise SmokeError("Final target table set mismatch")
    if azure("cosmosdb", "show", *flow.common, "--name", account) != live:
        raise SmokeError("Target configuration changed during verification")
    if (
        verified.get("table_count") != len(expected)
        or verified.get("status") != "data_verified_pending_table_set"
    ):
        raise SmokeError("Runtime verified table count mismatch")
    if type(verified.get("entity_count")) is not int or verified["entity_count"] < 0:
        raise SmokeError("Runtime verified entity count missing")
    if "backup_execution" in result and completed.get("entity_count") != verified["entity_count"]:
        raise SmokeError("Backup and restore entity counts differ")
    result.update(
        {
            "restore_status": "passed",
            "status": "passed",
            "stage": "complete",
            "tables": verified["table_count"],
            "entities": verified["entity_count"],
            "table_set_verified": True,
        }
    )
    REPORT_PATH.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Never print unsanitized Azure errors, console logs, or private table plans.
        REPORT.update({"status": "failed", "error_type": type(exc).__name__})
        try:
            REPORT_PATH.write_text(json.dumps(REPORT, indent=2) + "\n")
        except OSError as report_error:
            REPORT["error_type"] = type(report_error).__name__
        print(json.dumps(REPORT))
        raise SystemExit(1) from None
