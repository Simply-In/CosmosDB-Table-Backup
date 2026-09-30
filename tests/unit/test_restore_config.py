from uuid import uuid4

import pytest

from cosmos_table_backup.config import ConfigurationError
from cosmos_table_backup.restore_config import RestoreConfig

SOURCE_ID = (
    "/subscriptions/s/resourceGroups/r/providers/Microsoft.DocumentDB/databaseAccounts/source"
)
TARGET_ID = (
    "/subscriptions/s/resourceGroups/r/providers/Microsoft.DocumentDB/databaseAccounts/isolated"
)
BASE = {
    "AZURE_CLIENT_ID": "restore-identity",
    "APPLICATIONINSIGHTS_CONNECTION_STRING": "InstrumentationKey=test",
    "APPLICATIONINSIGHTS_AUTHENTICATION_STRING": ("Authorization=AAD;ClientId=restore-identity"),
    "BACKUP_STORAGE_ACCOUNT_URL": "https://backup.blob.core.windows.net",
    "BACKUP_CONTAINER": "backups",
    "KEY_VAULT_KEY_ID": "https://vault.vault.azure.net/keys/backup-kek",
    "RESTORE_TARGET_COSMOS_ACCOUNT_RESOURCE_ID": TARGET_ID,
    "RESTORE_TARGET_TABLE_ENDPOINT": "https://isolated.table.cosmos.azure.com",
    "RESTORE_SOURCE_ACCOUNT_RESOURCE_ID": SOURCE_ID,
    "RESTORE_REQUIRE_ISOLATED_TARGET": "true",
}


def test_infra_restore_environment_maps_to_config_and_selects_latest() -> None:
    config = RestoreConfig.from_env(BASE)
    assert config.managed_identity_client_id == "restore-identity"
    assert config.backup_container_name == "backups"
    assert config.backup_id is None
    assert config.expected_key_id.endswith("/keys/backup-kek")
    assert config.batch_size == 100
    assert config.max_batch_payload_bytes == 1_250_000


def test_optional_explicit_backup_id_is_validated() -> None:
    backup_id = str(uuid4())
    assert RestoreConfig.from_env({**BASE, "RESTORE_BACKUP_ID": backup_id}).backup_id == backup_id


def test_maximum_conservative_batch_ceiling_is_accepted() -> None:
    config = RestoreConfig.from_env({**BASE, "RESTORE_MAX_BATCH_PAYLOAD_BYTES": "1500000"})
    assert config.max_batch_payload_bytes == 1_500_000


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("RESTORE_REQUIRE_ISOLATED_TARGET", "false"),
        ("RESTORE_TARGET_COSMOS_ACCOUNT_RESOURCE_ID", SOURCE_ID),
        ("RESTORE_TARGET_TABLE_ENDPOINT", "https://wrong.table.cosmos.azure.com"),
        ("RESTORE_TARGET_TABLE_ENDPOINT", "https://backup.table.cosmos.azure.com"),
        ("RESTORE_BACKUP_ID", "latest"),
        ("AZURE_CLIENT_ID", ""),
        ("APPLICATIONINSIGHTS_AUTHENTICATION_STRING", "Authorization=AAD;ClientId=other"),
        ("KEY_VAULT_KEY_ID", "https://vault.vault.azure.net/keys/backup-kek/version"),
        ("RESTORE_BATCH_SIZE", "101"),
        ("RESTORE_MAX_BATCH_PAYLOAD_BYTES", "1500001"),
        ("RESTORE_DOWNLOAD_CHUNK_SIZE", "10"),
    ],
)
def test_restore_guardrails_fail_closed(field: str, value: str) -> None:
    with pytest.raises(ConfigurationError):
        RestoreConfig.from_env({**BASE, field: value})


@pytest.mark.parametrize("value", ["", "1", "yes"])
def test_data_only_mode_flag_is_strict(value: str) -> None:
    with pytest.raises(ConfigurationError, match="true or false"):
        RestoreConfig.from_env({**BASE, "RESTORE_DATA_ONLY": value})


def test_data_only_requires_pinned_backup_and_bounds_assertion() -> None:
    with pytest.raises(ConfigurationError, match="explicit RESTORE_BACKUP_ID"):
        RestoreConfig.from_env({**BASE, "RESTORE_DATA_ONLY": "true"})
    values = {**BASE, "RESTORE_DATA_ONLY": "true", "RESTORE_BACKUP_ID": str(uuid4())}
    assert RestoreConfig.from_env(values).data_only is True
    with pytest.raises(ConfigurationError, match="byte bound"):
        RestoreConfig.from_env(
            {
                **values,
                "RESTORE_MAX_MANIFEST_BYTES": "65536",
                "RESTORE_PREPARATION_JSON": "x" * 16385,
            }
        )
