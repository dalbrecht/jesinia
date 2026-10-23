# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Keychain key store, driven through an injected fake ``run``."""

from __future__ import annotations

import pytest

from jesinia.apps.zotero.domain.errors import ZoteroError
from jesinia.apps.zotero.infrastructure import keychain
from jesinia.apps.zotero.infrastructure.keychain import KeychainKeyStore

KEY = "AbC123xyz0AbC123xyz0AbC123xyz012"


class FakeRun:
    def __init__(self, code: int = 0, out: str = "") -> None:
        self.code, self.out = code, out
        self.calls: list[tuple[list[str], str | None]] = []

    async def __call__(self, argv: list[str], stdin: str | None) -> tuple[int, str]:
        self.calls.append((argv, stdin))
        return self.code, self.out


async def test_should_write_key_via_stdin_not_argv() -> None:
    run = FakeRun()
    await KeychainKeyStore(run).write(KEY)
    argv, stdin = run.calls[0]
    assert argv == ["security", "-i"]
    assert all(KEY not in part for part in argv)
    assert stdin == (f"add-generic-password -U -s jesinia-zotero -a local-api -w {KEY}\nquit\n")


@pytest.mark.parametrize("bad", ["", "a b", "a\nquit", 'a"b', "a\\b"])
async def test_should_refuse_to_store_a_malformed_key(bad: str) -> None:
    run = FakeRun()
    with pytest.raises(ValueError, match="malformed") as raised:
        await KeychainKeyStore(run).write(bad)
    assert run.calls == [] and bad not in str(raised.value) or bad == ""


async def test_should_not_leak_key_when_the_write_fails() -> None:
    with pytest.raises(ZoteroError) as raised:
        await KeychainKeyStore(FakeRun(code=1)).write(KEY)
    assert KEY not in str(raised.value)


async def test_should_read_key_from_keychain() -> None:
    run = FakeRun(out=f"{KEY}\n")
    assert await KeychainKeyStore(run).read() == KEY
    assert run.calls[0] == (
        ["security", "find-generic-password", "-s", "jesinia-zotero", "-a", "local-api", "-w"],
        None,
    )


async def test_should_return_none_when_keychain_has_no_item() -> None:
    assert await KeychainKeyStore(FakeRun(code=44)).read() is None


async def test_should_return_none_when_keychain_item_is_blank() -> None:
    assert await KeychainKeyStore(FakeRun(out="\n")).read() is None


async def test_should_raise_when_the_keychain_read_fails() -> None:
    with pytest.raises(ZoteroError):
        await KeychainKeyStore(FakeRun(code=51)).read()


async def test_should_report_missing_binary_as_exit_127(monkeypatch: pytest.MonkeyPatch) -> None:
    async def missing(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(keychain, "spawn", missing)
    assert await keychain._run(["security"], None) == (127, "")


async def test_should_run_a_real_process_with_and_without_stdin() -> None:
    assert await keychain._run(["cat"], "hello") == (0, "hello")
    code, out = await keychain._run(["cat"], None)
    assert (code, out) == (0, "")
