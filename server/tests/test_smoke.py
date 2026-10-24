# SPDX-License-Identifier: AGPL-3.0-or-later
import tomllib
from pathlib import Path

import jesinia

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_package_version_matches_pyproject() -> None:
    # release-please bumps both (ADR 0011); a mismatch means one was missed.
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    assert jesinia.__version__ == project["version"]
