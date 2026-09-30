import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("access", "monthly", "expected"),
    [
        ("false", "false", "false"),
        ("true", "false", "true"),
        ("false", "true", "true"),
        ("true", "true", "true"),
    ],
)
def test_restore_mode_resolution(tmp_path: Path, access: str, monthly: str, expected: str) -> None:
    output = tmp_path / "env"
    subprocess.run(  # noqa: S603 - Fixed repository script, no shell interpolation.
        ["/bin/bash", str(ROOT / "scripts/resolve-restore-mode.sh")],
        env={
            **os.environ,
            "INPUT_RESTORE_ACCESS": access,
            "INPUT_ENABLE_RESTORE": monthly,
            "GITHUB_ENV": str(output),
        },
        check=True,
    )
    assert output.read_text() == (
        f"RESTORE_ACCESS_ENABLED={expected}\nRESTORE_SCHEDULE_ENABLED={monthly}\n"
    )


def test_restore_mode_defaults_to_dormant(tmp_path: Path) -> None:
    output = tmp_path / "env"
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"INPUT_RESTORE_ACCESS", "INPUT_ENABLE_RESTORE"}
    }
    subprocess.run(  # noqa: S603 - Fixed repository script, no shell interpolation.
        ["/bin/bash", str(ROOT / "scripts/resolve-restore-mode.sh")],
        env={**env, "GITHUB_ENV": str(output)},
        check=True,
    )
    assert output.read_text() == ("RESTORE_ACCESS_ENABLED=false\nRESTORE_SCHEDULE_ENABLED=false\n")


@pytest.mark.parametrize("name", ["INPUT_RESTORE_ACCESS", "INPUT_ENABLE_RESTORE"])
@pytest.mark.parametrize("invalid", ["TRUE", "1", "", "$(echo true)"])
def test_restore_mode_rejects_invalid_input(tmp_path: Path, name: str, invalid: str) -> None:
    output = tmp_path / "env"
    result = subprocess.run(  # noqa: S603 - Fixed repository script, no shell interpolation.
        ["/bin/bash", str(ROOT / "scripts/resolve-restore-mode.sh")],
        env={
            **os.environ,
            "INPUT_RESTORE_ACCESS": "false",
            "INPUT_ENABLE_RESTORE": "false",
            name: invalid,
            "GITHUB_ENV": str(output),
        },
        capture_output=True,
    )
    assert result.returncode != 0
    assert not output.exists()


def test_workflow_uses_resolved_mode_for_preview_and_apply() -> None:
    workflow = (ROOT / ".github/workflows/deploy-backup.yml").read_text()
    assert "      enable_restore_access:\n" in workflow
    assert "run: bash scripts/resolve-restore-mode.sh" in workflow
    assert workflow.count('restoreAccessEnabled="$RESTORE_ACCESS_ENABLED"') == 2
    assert workflow.count('restoreScheduleEnabled="$RESTORE_SCHEDULE_ENABLED"') == 2
    assert '"$RESTORE_ACCESS_ENABLED" == "true"' in workflow
    assert 'restoreAccessEnabled="$INPUT_ENABLE_RESTORE"' not in workflow


@pytest.mark.parametrize(
    "suffix", [".vault.azure.net", ".vault.azure.cn", ".vault.usgovcloudapi.net"]
)
def test_restore_key_uri_preserves_cloud_suffix(suffix: str) -> None:
    bicep = (ROOT / "infra/deployment/backup.bicep").read_text()
    expected = "https://${names.vault}${environment().suffixes.keyvaultDns}/keys/${names.key}"
    assert f"{{ name: 'KEY_VAULT_KEY_ID', value: '{expected}' }}" in bicep
    uri = (
        expected.replace("${names.vault}", "test-vault")
        .replace("${environment().suffixes.keyvaultDns}", suffix)
        .replace("${names.key}", "backup-kek")
    )
    assert uri == f"https://test-vault{suffix}/keys/backup-kek"
    assert ".." not in uri


def test_restore_grants_are_scoped_and_table_native() -> None:
    bicep = (ROOT / "infra/deployment/backup.bicep").read_text()
    assert "/tableRoleDefinitions/00000000-0000-0000-0000-000000000002" in bicep
    assert "databaseAccounts/tableRoleAssignments@2026-03-15' = if (restoreAccessEnabled)" in bicep
    assert "sqlRoleAssignments" not in bicep
    grant = bicep.split("resource restoreTableContributor", 1)[1].split("module restoreAccount", 1)[
        0
    ]
    assert "parent: restoreTableAccount" in grant
    assert "principalId: restoreIdentity.outputs.principalId" in grant
    assert "scope: restoreAccountId" in grant
    assert "dependsOn: [ restoreAccount ]" in grant
    assert bicep.count("], restoreAccessEnabled ? [") == 3
    assert "roleDefinitionIdOrName: 'Storage Blob Data Reader'" in bicep
    assert "roleDefinitionIdOrName: keyUnwrapperRoleDefinitionId" in bicep
    assert "roleDefinitionIdOrName: '3913510d-42f4-4e42-8a64-420c390055eb'" in bicep
    assert (
        "triggerType: (restoreScheduleEnabled && restoreAccessEnabled) ? 'Schedule' : 'Manual'"
        in bicep
    )
