# SPDX-License-Identifier: AGPL-3.0-or-later
import jesinia


def test_package_imports_with_version() -> None:
    assert jesinia.__version__ == "0.1.0"
