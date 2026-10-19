# SPDX-License-Identifier: AGPL-3.0-or-later
import pytest

from jesinia.services.agents.domain.failures import is_transient_failure, redact_secrets


@pytest.mark.parametrize(
    "message",
    [
        "Selected model is at capacity. Please try a different model.",
        "Rate limit reached for requests",
        "rate_limit_error: slow down",
        "429 Too Many Requests",
        "API Error: 529 Overloaded",
        "overloaded_error",
        "503 Service Unavailable",
        "upstream returned 502 Bad Gateway",
        "Internal server error",
        "the service is temporarily unavailable",
    ],
)
def test_provider_capacity_errors_are_transient(message: str) -> None:
    assert is_transient_failure(message)


@pytest.mark.parametrize(
    "message",
    [
        "the CLI is logged out",
        "model not supported",
        "Failed to authenticate. API Error: 401",
        "codex exited with code 2",
        "no final message: the agent finished its turn without replying",
        "read 5000 lines",
    ],
)
def test_other_errors_are_not_transient(message: str) -> None:
    assert not is_transient_failure(message)


def test_redact_secrets_masks_bearer_tokens_and_api_keys() -> None:
    text = "Authorization: Bearer abc.DEF-123_x failed; key sk-proj-ABCdef1234567890 rejected"
    clean = redact_secrets(text)
    assert "abc.DEF-123_x" not in clean and "sk-proj-ABCdef1234567890" not in clean
    assert "Bearer ***" in clean and "failed" in clean
