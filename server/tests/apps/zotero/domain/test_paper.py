# SPDX-License-Identifier: AGPL-3.0-or-later
import dataclasses

import pytest

from jesinia.apps.zotero.domain.identifiers import ItemKey
from jesinia.apps.zotero.domain.paper import (
    Annotation,
    Attachment,
    Creator,
    Note,
    Paper,
    ZoteroTag,
)


def test_should_build_a_frozen_paper_with_children() -> None:
    annotation = Annotation(ItemKey("A1"), "quote", "my note", "3", "#ffd400")
    attachment = Attachment(ItemKey("P1"), "PDF", "application/pdf", "a.pdf", "", (annotation,))
    paper = Paper(
        key=ItemKey("K1"),
        version=7,
        item_type="journalArticle",
        title="Title",
        creators=(Creator("Ada", "Lovelace", "author"),),
        date="2020",
        abstract="",
        doi="",
        url="",
        extra="",
        tags=(ZoteroTag("ml", False),),
        related=(ItemKey("K2"),),
        notes=(Note(ItemKey("N1"), "<p>hi</p>"),),
        attachments=(attachment,),
        standalone=False,
    )
    assert paper.attachments[0].annotations[0].text == "quote"
    with pytest.raises(dataclasses.FrozenInstanceError):
        paper.title = "other"  # type: ignore[misc]  # asserting immutability on purpose
