# SPDX-License-Identifier: AGPL-3.0-or-later
import pytest

from jesinia.apps.rss.domain.reader import (
    READ,
    READING_LIST,
    STARRED,
    FeedStream,
    InvalidItemId,
    InvalidStream,
    LabelStream,
    StateStream,
    feed_stream_id,
    item_long_id,
    label_id,
    label_name,
    parse_item_id,
    parse_stream,
    write_token,
)


def test_should_encode_item_ids_as_16_hex_digits() -> None:
    assert item_long_id(123) == "tag:google.com,2005:reader/item/000000000000007b"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("tag:google.com,2005:reader/item/000000000000007b", 123),
        ("123", 123),
        ("-1", -1),
        ("tag:google.com,2005:reader/item/ffffffffffffffff", -1),
    ],
)
def test_should_parse_long_and_short_item_ids(text: str, expected: int) -> None:
    assert parse_item_id(text) == expected


@pytest.mark.parametrize(
    "text",
    ["", "abc", "7b", "tag:google.com,2005:reader/item/xyz", "tag:google.com,2005:reader/item/"],
)
def test_should_reject_unreadable_item_ids(text: str) -> None:
    with pytest.raises(InvalidItemId):
        parse_item_id(text)


def test_should_parse_every_stream_kind() -> None:
    assert parse_stream(READING_LIST) == StateStream("reading-list")
    assert parse_stream(STARRED) == StateStream("starred")
    assert parse_stream(READ) == StateStream("read")
    assert parse_stream("user/1234/state/com.google/starred") == StateStream("starred")
    assert parse_stream("feed/42") == FeedStream(42)
    assert parse_stream("user/-/label/Tech News") == LabelStream("Tech News")


@pytest.mark.parametrize(
    "text", ["", "feed/x", "feed/", "user/-/label/", "user/-/state/com.google/like", "splice/1"]
)
def test_should_reject_unknown_streams(text: str) -> None:
    with pytest.raises(InvalidStream):
        parse_stream(text)


def test_labels_and_feed_ids_round_trip() -> None:
    assert label_id("Tech") == "user/-/label/Tech"
    assert label_name("user/-/label/Tech") == "Tech"
    assert label_name("user/99/label/Tech") == "Tech"
    assert label_name(STARRED) is None
    assert feed_stream_id(7) == "feed/7"


def test_write_tokens_should_be_bound_to_the_reader_token() -> None:
    first, second = write_token("auth-a"), write_token("auth-b")
    assert first == write_token("auth-a") and first != second and len(first) == 57
