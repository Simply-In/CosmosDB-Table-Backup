from unittest.mock import Mock

import pytest

from cosmos_table_backup.config import ConfigurationError
from cosmos_table_backup.restore_cli import main


def test_restore_cli_argument_and_configuration_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    assert main(["unexpected"]) == 2
    monkeypatch.setattr(
        "cosmos_table_backup.restore_cli.RestoreConfig.from_env",
        Mock(side_effect=ConfigurationError("bad")),
    )
    assert main([]) == 2


def test_restore_cli_runtime_failure_is_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    config = Mock(log_level="INFO", managed_identity_client_id="restore-id")
    monkeypatch.setattr(
        "cosmos_table_backup.restore_cli.RestoreConfig.from_env", Mock(return_value=config)
    )
    monkeypatch.setattr(
        "cosmos_table_backup.restore_cli.DefaultAzureCredential",
        Mock(side_effect=RuntimeError("auth")),
    )
    assert main([]) == 1
