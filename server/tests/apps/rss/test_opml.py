# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import pytest

from jesinia.apps.rss.domain.opml import InvalidOpml, OpmlFeed, parse_opml, render_opml

NESTED = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="1.0">
  <head><title>Reader subscriptions</title></head>
  <body>
    <outline text="Loose" title="Loose feed" type="rss" xmlUrl="https://loose.example/feed"/>
    <outline text="Tech" title="Tech">
      <outline text="Blog A" type="rss" xmlUrl="https://a.example/rss" htmlUrl="https://a.example/"/>
      <outline title="Python">
        <outline text="Deep" xmlUrl="https://deep.example/atom"/>
        <outline title="Deeper">
          <outline xmlUrl="https://deeper.example/feed.json"/>
        </outline>
      </outline>
    </outline>
    <outline text="Empty folder"/>
    <outline text="  " xmlUrl="  "/>
  </body>
</opml>"""


def test_opml_round_trip_keeps_one_folder_level() -> None:
    feeds = parse_opml(NESTED)
    assert feeds == [
        OpmlFeed("https://loose.example/feed", "Loose feed", None),
        OpmlFeed("https://a.example/rss", "Blog A", "Tech"),
        OpmlFeed("https://deep.example/atom", "Deep", "Tech"),
        OpmlFeed("https://deeper.example/feed.json", "", "Tech"),
    ]
    rendered = render_opml(feeds)
    assert parse_opml(rendered) == feeds
    # Exactly one folder level is written: the Tech outline holds feeds only.
    assert rendered.count('<outline text="Tech" title="Tech">') == 1
    assert "Python" not in rendered


def test_render_escapes_markup_and_groups_folders_in_first_seen_order() -> None:
    feeds = [
        OpmlFeed("https://x.example/?a=1&b=2", 'Q & "A" <news>', "B & C"),
        OpmlFeed("https://y.example/", "Y", None),
        OpmlFeed("https://z.example/", "Z", "B & C"),
        OpmlFeed("https://w.example/", "W", "Another"),
    ]
    rendered = render_opml(feeds)
    assert rendered.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<opml version="2.0">')
    assert "&amp;b=2" in rendered and "&lt;news&gt;" in rendered and "&quot;A&quot;" in rendered
    assert parse_opml(rendered) == [feeds[1], feeds[0], feeds[2], feeds[3]]


def test_render_of_nothing_is_a_valid_empty_document() -> None:
    assert parse_opml(render_opml([])) == []


def test_title_falls_back_from_title_to_text() -> None:
    xml = '<opml><body><outline text="By text" xmlUrl="https://t.example/"/></body></opml>'
    assert parse_opml(xml) == [OpmlFeed("https://t.example/", "By text", None)]


@pytest.mark.parametrize(
    ("xml", "cause"),
    [
        ("not xml at all", "not well-formed XML"),
        ("<rss><channel/></rss>", "the root element is <rss>, not <opml>"),
        ("<opml><head/></opml>", "the document has no <body>"),
        (
            '<?xml version="1.0"?><!DOCTYPE opml [<!ENTITY a "aaaa">]><opml><body/></opml>',
            "document type declarations are not allowed",
        ),
    ],
)
def test_parse_rejects_documents_that_are_not_opml(xml: str, cause: str) -> None:
    with pytest.raises(InvalidOpml, match=cause):
        parse_opml(xml)


def test_parse_reports_the_xml_cause() -> None:
    with pytest.raises(InvalidOpml) as info:
        parse_opml("<opml><body>")
    assert info.value.__cause__ is not None
