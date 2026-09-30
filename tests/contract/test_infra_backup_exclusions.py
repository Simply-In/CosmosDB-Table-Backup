import json
from pathlib import Path

import pytest

from cosmos_table_backup.config import BackupConfig


@pytest.mark.parametrize("excluded", [[], ["cards"], ["cards", "audit"], ['quote"', "slash\\"]])
def test_infra_exclusions_use_json_serialization(excluded: list[str]) -> None:
    deployment = Path(__file__).resolve().parents[2] / "infra/deployment/backup.bicep"
    assert (
        "{ name: 'EXCLUDED_TABLES_JSON', value: string(excludedTables) }" in deployment.read_text()
    )
    # Bicep string(array) emits JSON; the application must retain every supplied name.
    config = BackupConfig.from_env(
        {
            "COSMOS_TABLE_ENDPOINT": "https://source.table.cosmos.azure.com",
            "BACKUP_STORAGE_ACCOUNT_URL": "https://backup.blob.core.windows.net",
            "BACKUP_CONTAINER_NAME": "backups",
            "BACKUP_KEY_ID": "https://vault.vault.azure.net/keys/backup/version1",
            "AZURE_CLIENT_ID": "backup-id",
            "EXCLUDED_TABLES_JSON": json.dumps(excluded),
            "APPLICATIONINSIGHTS_CONNECTION_STRING": "InstrumentationKey=test",
            "APPLICATIONINSIGHTS_AUTHENTICATION_STRING": "Authorization=AAD;ClientId=backup-id",
        }
    )
    assert config.excluded_tables == frozenset([*excluded, "cards"])
