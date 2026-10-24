"""`stt_harness.live_key`: a live test's key comes from the shell, else from the owner's Keychain, never from a file
(owner requirement 2026-10-24). Offline: the Keychain here is a `FakeSecurity`."""

import pytest

from tests.fakes.security import FakeSecurity
from tests.integration.stt_harness import GOOGLE_KEY, OPENROUTER_KEY, live_key
from yordamchi.core import keychain, redact

GOOGLE_VAR = "YORDAMCHI_LIVE_GOOGLE_API_KEY"
OPENROUTER_VAR = "YORDAMCHI_LIVE_OPENROUTER_API_KEY"


def test_the_shell_variable_wins_and_the_keychain_is_not_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GOOGLE_VAR, f" {GOOGLE_KEY} ")
    security = FakeSecurity()
    assert live_key(GOOGLE_VAR, runner=security) == GOOGLE_KEY
    assert security.calls == []


@pytest.mark.parametrize(
    ("name", "item", "value"),
    [(GOOGLE_VAR, keychain.GOOGLE_API_KEY, GOOGLE_KEY), (OPENROUTER_VAR, keychain.OPENROUTER_API_KEY, OPENROUTER_KEY)],
)
def test_without_it_the_keychain_item_is_read_and_masked(
    monkeypatch: pytest.MonkeyPatch, name: str, item: keychain.KeychainItem, value: str
) -> None:
    monkeypatch.delenv(name, raising=False)
    security = FakeSecurity()
    security.put(item, value)
    assert live_key(name, runner=security) == value
    assert security.calls == [tuple(keychain.find_argv(item, reveal=True))]
    assert value not in redact.scrubbed(f"key {value}")


@pytest.mark.parametrize("locked", [False, True], ids=["missing", "locked"])
def test_without_either_the_test_is_skipped_with_both_options_named(
    monkeypatch: pytest.MonkeyPatch, locked: bool
) -> None:
    monkeypatch.delenv(GOOGLE_VAR, raising=False)
    with pytest.raises(pytest.skip.Exception, match=r"uz\.yordamchi/google-api-key"):
        live_key(GOOGLE_VAR, runner=FakeSecurity(locked=locked))


def test_a_name_without_a_keychain_item_never_asks_the_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("YORDAMCHI_LIVE_OTHER", raising=False)
    security = FakeSecurity()
    with pytest.raises(pytest.skip.Exception, match="set YORDAMCHI_LIVE_OTHER"):
        live_key("YORDAMCHI_LIVE_OTHER", runner=security)
    assert security.calls == []
