# SPDX-License-Identifier: AGPL-3.0-or-later
import json
from pathlib import Path

import pytest
from coverage_per_file import below_minimum, main, minimum_for


def _report(**pcts: float) -> dict[str, object]:
    return {"files": {n: {"summary": {"percent_covered": p}} for n, p in pcts.items()}}


def test_lists_files_below_minimum() -> None:
    assert below_minimum(_report(a=100.0, b=94.9, c=95.0), 95.0) == [("b", 94.9)]


def test_rejects_malformed_report() -> None:
    with pytest.raises(ValueError):
        below_minimum({"files": []}, 95.0)


def test_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good, bad = tmp_path / "good.json", tmp_path / "bad.json"
    good.write_text(json.dumps(_report(a=99.0)))
    bad.write_text(json.dumps(_report(a=80.0)))
    assert main([str(good)]) == 0
    assert main([str(bad), "75"]) == 0
    assert main([str(bad)]) == 1 and "80.0% < 95%: a" in capsys.readouterr().out
    assert main([]) == 2


def test_core_and_domain_need_100() -> None:
    assert minimum_for("jesinia/core/config.py") == 100.0
    assert minimum_for("jesinia/services/kb/domain/page.py") == 100.0
    assert minimum_for("jesinia/services/kb/application/service.py") == 95.0
    report = _report(**{"jesinia/core/x.py": 99.0, "jesinia/services/s/application/y.py": 96.0})
    assert below_minimum(report, 95.0) == [("jesinia/core/x.py", 99.0)]
