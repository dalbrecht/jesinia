# SPDX-License-Identifier: AGPL-3.0-or-later
import pytest

from jesinia.services.kb.domain.assets import image_media_type, is_asset_file_name, is_page_id

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
