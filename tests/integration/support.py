import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def run_alembic(database_url: str, *args: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=PROJECT_ROOT,
        env={**os.environ, "FOOTBALL_RESULTS_DATABASE_URL": database_url},
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"alembic {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")
    return result.stdout
