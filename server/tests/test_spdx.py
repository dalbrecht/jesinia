# SPDX-License-Identifier: AGPL-3.0-or-later
from pathlib import Path

from check_spdx import main, missing_spdx


def test_reports_missing_header(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("print(1)\n")
    (tmp_path / "b.py").write_text("# SPDX-License-Identifier: AGPL-3.0-or-later\n")
    assert missing_spdx(tmp_path, suffixes={".py"}) == [tmp_path / "a.py"]


def test_header_must_be_in_first_three_lines(tmp_path: Path) -> None:
    (tmp_path / "late.py").write_text("\n\n\n# SPDX-License-Identifier: AGPL-3.0-or-later\n")
    (tmp_path / "shebang.sh").write_text(
        "#!/bin/bash\n# SPDX-License-Identifier: AGPL-3.0-or-later\n"
    )
    assert missing_spdx(tmp_path, suffixes={".py", ".sh"}) == [tmp_path / "late.py"]


def test_skips_vendored_and_build_dirs(tmp_path: Path) -> None:
    for d in (".standards", "node_modules", ".venv", "dist", "static"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "x.py").write_text("print(1)\n")
    (tmp_path / "web" / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "web" / "node_modules" / "pkg" / "y.ts").write_text("x\n")
    assert missing_spdx(tmp_path, suffixes={".py", ".ts"}) == []


def test_named_files_are_checked(tmp_path: Path) -> None:
    (tmp_path / "setup").write_text("#!/bin/bash\necho hi\n")
    assert missing_spdx(tmp_path, suffixes=set(), names={"setup"}) == [tmp_path / "setup"]


def test_results_are_sorted(tmp_path: Path) -> None:
    for n in ("c.py", "a.py", "b.py"):
        (tmp_path / n).write_text("x = 1\n")
    assert missing_spdx(tmp_path, suffixes={".py"}) == [
        tmp_path / n for n in ("a.py", "b.py", "c.py")
    ]


def test_cli_exit_codes(tmp_path: Path, capsys: object) -> None:
    (tmp_path / "ok.ts").write_text("// SPDX-License-Identifier: AGPL-3.0-or-later\n")
    assert main([str(tmp_path)]) == 0
    (tmp_path / "bad.css").write_text("a {}\n")
    assert main([str(tmp_path)]) == 1
