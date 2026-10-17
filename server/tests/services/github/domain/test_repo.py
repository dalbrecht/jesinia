# SPDX-License-Identifier: AGPL-3.0-or-later
import pytest

from jesinia.services.github.domain.pr import InvalidPullRequestReferenceError
from jesinia.services.github.domain.pr import validate_repo as validate_pr_repo
from jesinia.services.github.domain.repo import (
    GitHubReferenceError,
    InvalidPathSegmentError,
    InvalidRepoError,
    path_segment,
    validate_repo,
)


@pytest.mark.parametrize(
    ("value", "encoded"),
    [
        ("bug", "bug"),
        ("priority: high", "priority%3A%20high"),
        ("v1.0", "v1%2E0"),
        ("...", "%2E%2E%2E"),
        ("a/b", "a%2Fb"),
        ("%2e%2e", "%252e%252e"),
        ("a?b#c", "a%3Fb%23c"),
        ("área~x_y-z", "%C3%A1rea~x_y-z"),
    ],
)
def test_path_segment_encodes_everything_that_could_change_the_path(
    value: str, encoded: str
) -> None:
    assert path_segment(value) == encoded


@pytest.mark.parametrize("value", ["", ".", ".."])
def test_path_segment_refuses_empty_and_dot_segments(value: str) -> None:
    with pytest.raises(InvalidPathSegmentError):
        path_segment(value)


def test_repo_errors_are_neutral_and_the_pr_error_stays_for_pr_refs() -> None:
    assert validate_repo("acme/api") == "acme/api"
    for bad in ("acme", "acme/..", "-x/y", "acme/a/b"):
        with pytest.raises(InvalidRepoError):
            validate_repo(bad)
    assert issubclass(InvalidRepoError, GitHubReferenceError)
    with pytest.raises(InvalidPullRequestReferenceError) as caught:
        validate_pr_repo("acme/..")
    assert isinstance(caught.value.__cause__, InvalidRepoError)
