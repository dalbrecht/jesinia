# SPDX-License-Identifier: AGPL-3.0-or-later
from pathlib import Path

from jesinia.services.kbsync.domain.redact import MAX_LEN, redact
from tests.services.kbsync.conftest import git

# Fake credentials for redaction tests (not real secrets).
ALICE = "https://alice:ghp_secret123@github.com/a/kb.git/"  # trufflehog:ignore
AT_SIGNS = "https://u:p@ss@w0rd@host.example/repo.git"  # trufflehog:ignore
BOB = "https://bob:tok3n@127.0.0.1:9/none.git"  # trufflehog:ignore


def test_masks_url_credentials() -> None:
    err = f"fatal: unable to access '{ALICE}': 403"
    out = redact(err)
    assert "ghp_secret123" not in out and "alice" not in out
    assert "https://***@github.com/a/kb.git/" in out
    assert redact("ssh://git@host/x") == "ssh://***@host/x"
    assert redact("no urls here") == "no urls here"
    out = redact(AT_SIGNS)
    assert out == "https://***@host.example/repo.git" and "w0rd" not in out


def test_caps_length() -> None:
    out = redact("x" * 1000)
    assert len(out) == MAX_LEN and out.endswith("…")


async def test_push_error_message_is_redacted(repo_with_remote: Path) -> None:
    from tests.services.kbsync.test_cycle import Env

    git(repo_with_remote, "remote", "set-url", "origin", BOB)
    e = Env(repo_with_remote)
    (repo_with_remote / "Notes/a.md").write_text("x\n")
    r = await e.sync.run_cycle("manual")
    assert r.outcome == "error" and "tok3n" not in r.message
