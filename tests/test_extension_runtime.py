"""Offline event-loop regressions against the shipped extension service worker."""

import subprocess
from pathlib import Path


def test_extension_runtime():
    subprocess.run(
        ["node", "--test", str(Path(__file__).with_name("extension_runtime.cjs"))],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
