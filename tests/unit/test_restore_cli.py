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
        "cosmos_table_backup.restore_cli.ManagedIdentityCredential",
        Mock(side_effect=RuntimeError("auth")),
    )
    assert main([]) == 1


@pytest.mark.parametrize("size", [1, 288, 289, 16384])
def test_private_plan_frames_bound_lines_and_roundtrip_digest(size: int) -> None:
    import base64
    import hashlib

    from cosmos_table_backup.restore_cli import plan_frames

    payload = "x" * size
    frames = plan_frames(payload)
    total = len(frames) - 1
    digest = hashlib.sha256(payload.encode()).hexdigest()
    chunks = []
    for index, frame in enumerate(frames[:-1]):
        assert len(frame.encode()) <= 512
        prefix = f"restore.plan.part:{index}/{total}:{digest}:"
        assert frame.startswith(prefix)
        chunks.append(frame[len(prefix) :])
    assert base64.b64decode("".join(chunks), validate=True).decode() == payload
    assert frames[-1] == f"restore.plan.complete:{total}:{digest}"


@pytest.mark.parametrize("payload", ["", "x" * 16385])
def test_plan_framing_rejects_oversized_or_empty_payload(payload: str) -> None:
    from cosmos_table_backup.restore_cli import plan_frames

    with pytest.raises(ValueError):
        plan_frames(payload)


def test_data_only_cli_flag_sets_mode_before_config_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cosmos_table_backup.config import ConfigurationError

    constructor = Mock(side_effect=ConfigurationError("stop"))
    monkeypatch.setattr("cosmos_table_backup.restore_cli.RestoreConfig.from_env", constructor)
    assert main(["--data-only"]) == 2
    assert constructor.call_args.args[0]["RESTORE_DATA_ONLY"] == "true"
