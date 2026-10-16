# SPDX-License-Identifier: AGPL-3.0-or-later
from pathlib import Path

import check_docs

REPO = Path(__file__).resolve().parents[2]


def test_repository_docs_are_clean() -> None:
    assert check_docs.problems(REPO) == []
    assert check_docs.main([str(REPO)]) == 0


def test_detects_problems(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "docs/adr").mkdir(parents=True)
    (tmp_path / "docs/conventions").mkdir()
    (tmp_path / "README.md").write_text(
        "[ok](docs/adr/0001-x.md) [gone](docs/nope.md#x) [web](https://x.example) [top](#a)\n"
        "```\n[in a fence](nowhere.md)\n```\n![image](missing.png)\n"
    )
    (tmp_path / "docs/adr/0001-x.md").write_text("# ADR\n\n- **Status:** Accepted\n\n## Context\n")
    (tmp_path / "docs/conventions/kb-format.md").write_text("nothing exact here\n")
    found = check_docs.problems(tmp_path)
    assert "README.md: broken link docs/nope.md#x" in found
    assert not any("nowhere" in p or "missing.png" in p or "https" in p for p in found)
    assert any("0001-x.md: missing **Date:**, ## Decision, ## Consequences" in p for p in found)
    assert sum("kb-format.md: must contain" in p for p in found) == 4
    assert check_docs.main([str(tmp_path)]) == 1
    assert "broken link" in capsys.readouterr().out
