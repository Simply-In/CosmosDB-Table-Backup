import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("event", ["backup.failed", "backupXfailed", "backup-failed"])
def test_startup_gate_requires_exact_event(tmp_path: Path, event: str) -> None:
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == image ]]; then echo linux/amd64; exit 0; fi\n'
        'if [[ "$*" == *"import os"* ]]; then exit 0; fi\n'
        'echo "configuration error:"\n'
        'if [[ "$*" == *restore* ]]; then event=restore.failed; '
        'else event="$MOCK_EVENT"; fi\n'
        'printf \'{"event":"%s"}\\n\' "$event"\n'
        "exit 2\n"
    )
    docker.chmod(0o700)
    result = subprocess.run(  # noqa: S603 - Fixed repository script, no shell interpolation.
        ["/bin/bash", str(ROOT / "scripts/smoke-test-image.sh")],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "IMAGE": "synthetic-test-image",
            "MOCK_EVENT": event,
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == (0 if event == "backup.failed" else 1)
