import pytest

from cosmos_table_backup.config import BackupConfig, ConfigurationError

BASE = {
    "COSMOS_TABLE_ENDPOINT": "https://source.table.cosmos.azure.com",
    "BACKUP_STORAGE_ACCOUNT_URL": "https://backup.blob.core.windows.net",
    "BACKUP_CONTAINER_NAME": "backups",
    "BACKUP_KEY_ID": "https://vault.vault.azure.net/keys/backup/version1",
    "AZURE_CLIENT_ID": "backup-id",
    "EXCLUDED_TABLES_JSON": '["audit"]',
    "APPLICATIONINSIGHTS_CONNECTION_STRING": "InstrumentationKey=test",
    "APPLICATIONINSIGHTS_AUTHENTICATION_STRING": "Authorization=AAD;ClientId=backup-id",
}


def test_typed_configuration() -> None:
    config = BackupConfig.from_env({**BASE, "BACKUP_PAGE_SIZE": "10", "BACKUP_BLOCK_SIZE": "65536"})
    assert config.page_size == 10
    assert config.block_size == 65536
    assert config.upload_concurrency == config.upload_queue_blocks == 2
    assert config.excluded_tables == frozenset({"audit", "cards"})
    assert config.managed_identity_client_id == "backup-id"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("COSMOS_TABLE_ENDPOINT", "http://unsafe"),
        ("BACKUP_CONTAINER_NAME", "Upper"),
        ("BACKUP_KEY_ID", "https://vault.vault.azure.net/keys/not-versioned"),
        ("BACKUP_PAGE_SIZE", "0"),
        ("BACKUP_BLOCK_SIZE", "12"),
        ("BACKUP_UPLOAD_CONCURRENCY", "0"),
        ("BACKUP_UPLOAD_CONCURRENCY", "9"),
        ("BACKUP_UPLOAD_CONCURRENCY", "many"),
        ("BACKUP_UPLOAD_QUEUE_BLOCKS", "0"),
        ("BACKUP_UPLOAD_QUEUE_BLOCKS", "9"),
        ("BACKUP_UPLOAD_QUEUE_BLOCKS", "many"),
    ],
)
def test_invalid_configuration_fails_closed(name: str, value: str) -> None:
    with pytest.raises(ConfigurationError):
        BackupConfig.from_env({**BASE, name: value})


@pytest.mark.parametrize(
    "value",
    ["not-json", "{}", '[["cards"]', '["cards", "cards"]', '[""]', "[1]"],
)
def test_invalid_excluded_tables_fail_closed(value: str) -> None:
    with pytest.raises(ConfigurationError, match="EXCLUDED_TABLES_JSON"):
        BackupConfig.from_env({**BASE, "EXCLUDED_TABLES_JSON": value})


def test_cards_cannot_be_removed_from_exclusions() -> None:
    config = BackupConfig.from_env({**BASE, "EXCLUDED_TABLES_JSON": "[]"})
    assert config.excluded_tables == frozenset({"cards"})


def test_missing_and_non_integer_configuration() -> None:
    with pytest.raises(ConfigurationError):
        BackupConfig.from_env({})
    with pytest.raises(ConfigurationError):
        BackupConfig.from_env({**BASE, "BACKUP_PAGE_SIZE": "many"})


@pytest.mark.parametrize("limit", ["1", "8"])
def test_upload_limits_are_configurable(limit: str) -> None:
    config = BackupConfig.from_env(
        {**BASE, "BACKUP_UPLOAD_CONCURRENCY": limit, "BACKUP_UPLOAD_QUEUE_BLOCKS": limit}
    )
    assert config.upload_concurrency == config.upload_queue_blocks == int(limit)
