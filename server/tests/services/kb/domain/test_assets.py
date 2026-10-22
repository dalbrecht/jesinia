# SPDX-License-Identifier: AGPL-3.0-or-later
from pathlib import PurePosixPath

import pytest

from jesinia.services.kb.domain.assets import (
    image_media_type,
    is_asset_file_name,
    is_page_id,
    rebase_asset_links,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 8
GIF = b"GIF89a" + b"\x00" * 8
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 "
AVIF = b"\x00\x00\x00\x1cftypavif" + b"\x00" * 4


@pytest.mark.parametrize(
    ("data", "media_type"),
    [
        (PNG, "image/png"),
        (JPEG, "image/jpeg"),
        (GIF, "image/gif"),
        (b"GIF87a", "image/gif"),
        (WEBP, "image/webp"),
        (AVIF, "image/avif"),
        (b"\x00\x00\x00\x1cftypavis" + b"\x00" * 4, "image/avif"),
    ],
)
def test_should_detect_raster_images_by_magic_bytes(data: bytes, media_type: str) -> None:
    assert image_media_type(data) == media_type


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        b"<html><script>alert(1)</script></html>",
        b"RIFF\x00\x00\x00\x00WAVEfmt ",
        b"\x00\x00\x00\x1cftypmp42",
        b"%PDF-1.7",
    ],
)
def test_should_refuse_anything_that_is_not_a_raster_image(data: bytes) -> None:
    assert image_media_type(data) is None


def test_should_accept_only_lowercase_dashed_uuids_as_page_ids() -> None:
    assert is_page_id("0199e000-0000-7000-8000-000000000001")
    for bad in ["", "..", "0199E000-0000-7000-8000-000000000001", "0199e000", "a/b", "x" * 36]:
        assert not is_page_id(bad), bad


def test_should_accept_only_one_plain_file_name() -> None:
    assert is_asset_file_name("3f2a9c.png")
    assert is_asset_file_name("photo_1-a.JPG")
    bad_names = ["", ".", "..", ".gitignore", "a/b.png", "a\\b.png", "..png", "a b.png"]
    for bad in [*bad_names, "a.png/", "a..png", "%2e.png"]:
        assert not is_asset_file_name(bad), bad


ID = "01a14979-9e48-78ec-b36a-dc04f9ac2fdf"


def test_should_rebase_relative_asset_links_when_a_page_moves_deeper() -> None:
    body = f'Intro\n\n![a](../_assets/{ID}/ab12.png)\n<img src="../_assets/{ID}/cd34.jpg">'
    rebased = rebase_asset_links(body, PurePosixPath("Sources"), PurePosixPath("30 R/Making"))
    assert rebased == (
        f'Intro\n\n![a](../../_assets/{ID}/ab12.png)\n<img src="../../_assets/{ID}/cd34.jpg">'
    )


def test_should_rebase_asset_links_to_and_from_the_kb_root() -> None:
    body = f"![a](_assets/{ID}/ab12.png)"
    deeper = rebase_asset_links(body, PurePosixPath(""), PurePosixPath("A/B"))
    assert deeper == f"![a](../../_assets/{ID}/ab12.png)"
    assert rebase_asset_links(deeper, PurePosixPath("A/B"), PurePosixPath("")) == body


def test_should_leave_links_that_do_not_resolve_to_the_root_assets_folder() -> None:
    body = (
        f"![wrong depth](../../_assets/{ID}/a.png) ![other](img/_assets/{ID}/a.png) "
        f"![web](https://x.test/_assets/{ID}/a.png) `../_assets/{ID}/a.png` in prose"
    )
    assert rebase_asset_links(body, PurePosixPath("Sources"), PurePosixPath("A/B")) == body


def test_should_leave_asset_link_examples_inside_code_alone() -> None:
    link = f"![x](../_assets/{ID}/a.png)"
    body = f"`{link}`\n\n```\n{link}\n```\n\n    {link}\n\n# {link}\n"
    assert rebase_asset_links(body, PurePosixPath("Sources"), PurePosixPath("A/B")) == (
        f"`{link}`\n\n```\n{link}\n```\n\n    {link}\n\n# ![x](../../_assets/{ID}/a.png)\n"
    )
