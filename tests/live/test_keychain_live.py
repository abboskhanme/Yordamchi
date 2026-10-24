"""The real `/usr/bin/security` with `core.keychain`'s command lines (owner requirement 2026-10-24).

The one test that touches the real login Keychain, and only a throwaway item: service `uz.yordamchi.test` (never the
`uz.yordamchi` service the bots use), a random account, deleted afterwards. It proves what the offline fakes assume:
a value written through `security -i` stdin reads back byte for byte with `-w`, an existence check without `-w`
prints no value, and a missing item exits 44.

Opt in with `pytest -m live tests/live/test_keychain_live.py`.
"""

import base64
import os
import secrets

import pytest

from yordamchi.core import keychain
from yordamchi.core.errors import KeychainItemMissing
from yordamchi.core.keychain import KeychainItem

pytestmark = pytest.mark.live

TIMEOUT_S = 10.0


def test_a_throwaway_item_round_trips_through_stdin_and_is_deleted() -> None:
    item = KeychainItem("uz.yordamchi.test", f"live-{secrets.token_hex(6)}")
    value = base64.b64encode(os.urandom(32)).decode()
    run = keychain.security_runner
    try:
        written = run(keychain.write_argv(), stdin=keychain.add_script(item, value), timeout=TIMEOUT_S)
        assert written.returncode == 0
        read = run(keychain.find_argv(item, reveal=True), stdin=None, timeout=TIMEOUT_S)
        assert keychain.password_from(read, item) == value
        exists = run(keychain.find_argv(item, reveal=False), stdin=None, timeout=TIMEOUT_S)
        assert exists.returncode == 0
        assert value.encode() not in exists.stdout + exists.stderr
    finally:
        run(keychain.delete_argv(item), stdin=None, timeout=TIMEOUT_S)
    gone = run(keychain.find_argv(item, reveal=True), stdin=None, timeout=TIMEOUT_S)
    assert gone.returncode == keychain.ITEM_NOT_FOUND_EXIT
    with pytest.raises(KeychainItemMissing):
        keychain.password_from(gone, item)
