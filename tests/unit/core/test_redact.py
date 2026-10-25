"""core/redact: patterns (SPEC §4.5), known vault values, and the process hooks (STACK §4.3)."""

import asyncio
import io
import logging
import sys
import threading

import pytest

from tests.factories import DEV_BOT_TOKEN
from yordamchi.core import redact
from yordamchi.core.log import JsonFormatter

# One sample per SPEC §4.5 format (≥ 10), all fake.
SAMPLES = {
    "openai_api_key": "sk-proj-" + "Ab3dEf6hIj9kLm2nOp5qRs8t",
    "anthropic_api_key": "sk-ant-api03-" + "Zx9Yw8Vu7Ts6Rq5Po4Nm3Lk2Ji1Hg0Fe",
    "google_api_key": "AIza" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q",
    "openrouter_api_key": "sk-or-v1-" + "0123456789abcdef" * 4,
    "github_token": "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
    "github_pat": "github_pat_" + "11ABCDEFG0123456789_abcdefghijklmnopqrstuv",
    "telegram_bot_token": DEV_BOT_TOKEN,
    "aws_access_key_id": "AKIA" + "IOSFODNN7EXAMPLE",
    "slack_token": "xoxb-" + "123456789012-abcdefghijKL",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlLXZhbHVlLWhlcmU",
}


def test_the_newer_google_key_format_is_a_google_key() -> None:
    """2026-10-25: the owner's AI Studio key starts with `AQ.`; not seen as a key, it would reach the model and the
    inbox in plaintext."""
    secret = "AQ." + "Ab8RN6fakeOwUgP2LVQ-9RXhpulBgV0tZwyi8_GW0tGo5cnZmFk"
    hits = redact.find_secrets(f"ovoz uchun gemini key: {secret}")
    assert [(hit.kind, hit.value) for hit in hits] == [("google_api_key", secret)]
    assert secret not in redact.scrubbed(f"key: {secret}")
    assert redact.find_secrets("AQ.short and AQ.") == []


@pytest.mark.parametrize(("kind", "secret"), sorted(SAMPLES.items()))
def test_each_known_format_is_detected_and_masked(kind: str, secret: str) -> None:
    text = f"mana kalit: {secret} — saqlab qo'y"
    hits = redact.find_secrets(text)
    assert [hit.kind for hit in hits] == [kind]
    assert hits[0].value == secret
    masked = redact.scrubbed(text)
    assert secret not in masked
    assert redact.MASK + secret[-4:] in masked


def test_aws_secret_pem_and_generic_formats() -> None:
    aws = "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
    generic = "parol: Q7v!x9Lr2Tz@8Wn4Kp6Hs3Yb"
    assert [hit.kind for hit in redact.find_secrets(aws)] == ["aws_secret_access_key"]
    assert [hit.kind for hit in redact.find_secrets(pem)] == ["pem_private_key"]
    assert [hit.kind for hit in redact.find_secrets(generic)] == ["generic_secret"]
    assert "MIIEowIBAAKCAQEA" not in redact.scrubbed(pem)


def test_a_claude_login_token_is_flagged_for_refusal() -> None:
    hits = redact.find_secrets("sk-ant-oat01-" + "a" * 40)
    assert [(hit.kind, hit.refused) for hit in hits] == [("claude_login_token", True)]


def test_low_entropy_text_next_to_a_keyword_is_not_a_secret() -> None:
    assert redact.find_secrets("token: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa") == []
    assert redact.find_secrets("keyboard shortcuts are listed in the README file of this project") == []


def test_a_bot_token_url_is_masked() -> None:
    url = f"ClientConnectorError: https://api.telegram.org/bot{DEV_BOT_TOKEN}/getUpdates?offset=5"
    masked = redact.scrubbed(url)
    assert DEV_BOT_TOKEN not in masked
    assert "api.telegram.org/bot" + redact.MASK in masked
    assert "/getUpdates?offset=5" in masked


def test_a_bot_url_with_an_unusual_token_shape_is_still_masked() -> None:
    masked = redact.scrubbed("GET https://api.telegram.org/file/botWEIRD-token-shape/documents/f.txt")
    assert "WEIRD-token-shape" not in masked
    assert "/documents/f.txt" in masked


def test_an_oauth_callback_url_is_masked_whole() -> None:
    masked = redact.scrubbed("http://localhost:33418/callback?code=abc123def&state=xyz")
    assert "abc123def" not in masked
    assert masked == redact.MASK


def test_stored_secret_values_are_masked_by_value() -> None:
    redact.set_known_secrets(["db-password-without-pattern-77", "short"])
    masked = redact.scrubbed("connect with db-password-without-pattern-77 or short")
    assert "db-password-without-pattern-77" not in masked
    assert redact.MASK + "n-77" in masked
    assert "short" in masked  # below MIN_KNOWN_SECRET_LEN: words like this are not masked by value
    assert redact.known_secret_count() == 1


def test_the_known_set_is_replaced_not_merged() -> None:
    redact.set_known_secrets(["first-secret-value"])
    redact.add_known_secrets(["second-secret-value"])
    assert redact.known_secret_count() == 2
    redact.set_known_secrets([])
    assert "first-secret-value" in redact.scrubbed("first-secret-value")


def test_scrubbing_stream_catches_a_token_split_across_writes() -> None:
    target = io.StringIO()
    stream = redact.ScrubbingStream(target)
    stream.write(f"error: {DEV_BOT_TOKEN[:15]}")
    stream.write(f"{DEV_BOT_TOKEN[15:]} failed\nnext ")
    stream.flush()
    assert DEV_BOT_TOKEN not in target.getvalue()
    assert target.getvalue().endswith("failed\nnext ")


def test_process_hooks_scrub_uncaught_tracebacks(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = io.StringIO()
    for name in ("stderr", "stdout", "excepthook", "unraisablehook"):
        monkeypatch.setattr(sys, name, getattr(sys, name) if name != "stderr" else captured)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    redact.install_process_hooks()
    redact.install_process_hooks()  # idempotent: no double wrapping
    assert isinstance(sys.stderr, redact.ScrubbingStream)
    assert sys.stderr.target is captured
    error = RuntimeError(f"https://api.telegram.org/bot{DEV_BOT_TOKEN}/sendMessage")
    sys.excepthook(RuntimeError, error, None)
    assert "RuntimeError" in captured.getvalue()
    assert DEV_BOT_TOKEN not in captured.getvalue()


async def test_the_asyncio_handler_logs_through_redaction(caplog: pytest.LogCaptureFixture) -> None:
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    redact.install_asyncio_handler(loop)
    try:
        with caplog.at_level(logging.ERROR, logger="yordamchi.asyncio"):
            loop.call_exception_handler(
                {"message": f"Task failed: bot{DEV_BOT_TOKEN}", "exception": RuntimeError(DEV_BOT_TOKEN)}
            )
    finally:
        loop.set_exception_handler(previous)
    [record] = [r for r in caplog.records if r.name == "yordamchi.asyncio"]
    line = JsonFormatter(process="dev", version="test").format(record)
    assert '"event": "asyncio_unhandled"' in line
    assert DEV_BOT_TOKEN not in line


# --- regressions found by the T27 secrets eval (2026-10-24) ---
_QUERY_KEY = "Zq8rT2vLmN4pXw7KcB1yHs9DjF3gUe6A"


def test_generic_secret_stops_at_the_next_url_query_parameter() -> None:
    text = f"https://hooks.example.com/notify?token={_QUERY_KEY}&channel=dev"
    [hit] = redact.find_secrets(text)
    assert hit.value == _QUERY_KEY
    assert redact.scrubbed(text).endswith("&channel=dev")


def test_scrubbing_a_scrubbed_github_url_leaves_the_marker_whole() -> None:
    text = "remote shunday: https://ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8" + "@github.com/owner/repo.git"
    once = redact.scrubbed(text)
    assert redact.scrubbed(once) == once
    assert once.endswith("@github.com/owner/repo.git")


def test_scrubbing_a_scrubbed_telegram_url_leaves_the_marker_whole() -> None:
    text = f"https://api.telegram.org/bot{DEV_BOT_TOKEN}/getMe"
    once = redact.scrubbed(text)
    assert DEV_BOT_TOKEN not in once
    assert redact.scrubbed(once) == once
    assert once.endswith("/getMe")
