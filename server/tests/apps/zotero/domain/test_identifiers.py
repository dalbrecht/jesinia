# SPDX-License-Identifier: AGPL-3.0-or-later
import pytest

from jesinia.apps.zotero.domain.errors import (
    ForbiddenWrite,
    StaleVersion,
    ZoteroError,
    ZoteroUnavailable,
)
from jesinia.apps.zotero.domain.identifiers import (
    ItemKey,
    find_arxiv_id,
    find_doi,
    title_needs_fix,
    zotero_annotation_url,
    zotero_select_url,
)


@pytest.mark.parametrize(
    "texts, expected",
    [
        (("2510.15061",), "2510.15061"),
        (("2506.18559v1",), "2506.18559"),
        (("[2402.12354] LoRA+: Efficient Low Rank Adaptation",), "2402.12354"),
        (("", "https://arxiv.org/pdf/2403.01234v2"), "2403.01234"),
        (("arXiv:2403.01234",), "2403.01234"),
        (("https://arxiv.org/abs/hep-th/9901001v2",), "hep-th/9901001"),
        (("the-illusion-of-thinking.pdf",), None),
        ((), None),
    ],
)
def test_should_find_arxiv_id_in_any_text(texts: tuple[str, ...], expected: str | None) -> None:
    assert find_arxiv_id(*texts) == expected


def test_should_find_doi_and_strip_resolver_prefix() -> None:
    assert find_doi("see https://doi.org/10.1073/PNAS.1907373116") == "10.1073/pnas.1907373116"


def test_should_strip_trailing_punctuation_from_doi() -> None:
    assert find_doi("doi: 10.1000/xyz123).") == "10.1000/xyz123"


def test_should_return_none_when_no_doi_present() -> None:
    assert find_doi("nothing here", "") is None


@pytest.mark.parametrize(
    "title, expected",
    [
        ("2510.15061", True),
        ("2506.18559v1", True),
        ("[2506.18559v1]", True),
        ("P10-4007.pdf", True),
        ("", True),
        ("   ", True),
        ("https://www.researchgate.net/profile/x.pdf?_tp=1", True),
        ("Deep Unsupervised Learning using Nonequilibrium Thermodynamics", False),
    ],
)
def test_should_detect_titles_needing_a_fix(title: str, expected: bool) -> None:
    assert title_needs_fix(title) is expected


def test_should_build_select_url() -> None:
    assert zotero_select_url(ItemKey("ABCD1234")) == "zotero://select/library/items/ABCD1234"


def test_should_build_annotation_url_with_page() -> None:
    url = zotero_annotation_url(ItemKey("ATT1"), ItemKey("ANN1"), "12")
    assert url == "zotero://open-pdf/library/items/ATT1?page=12&annotation=ANN1"


def test_should_omit_page_from_annotation_url_when_label_empty() -> None:
    url = zotero_annotation_url(ItemKey("ATT1"), ItemKey("ANN1"), "")
    assert url == "zotero://open-pdf/library/items/ATT1?annotation=ANN1"


def test_should_share_zotero_error_base() -> None:
    assert issubclass(StaleVersion, ZoteroError)
    assert issubclass(ForbiddenWrite, ZoteroError)
    assert issubclass(ZoteroUnavailable, ZoteroError)


def test_should_store_state_on_zotero_unavailable() -> None:
    error = ZoteroUnavailable("api_off", "local API disabled")
    assert error.state == "api_off"
    assert "local API disabled" in str(error)
    assert ZoteroUnavailable("not_running").state == "not_running"
