"""No usable secret in any file after install, rotate and run (owner requirement 2026-10-24; the T42 leak search).

One fixture Mac goes through the life the owner's Mac had: a legacy `master.key` file and a plaintext dev token in
`dev.env` (the state before this change), then the installer, then `--env-only --rotate-tokens`, then a run: both bots
load their config, the dev bot stores and opens a Google key in its vault through `KeychainKeystore` and publishes
`secret-hashes.json`, and the check script runs. The Keychain is a `FakeSecurity`.

Afterwards every file under the fixture HOME (DATA, the DBs and their WAL, `~/.config/yordamchi`, logs, LaunchAgents)
is searched for every secret this life touched: old and new tokens, the master key (base64 and raw bytes) and the
vault value. "Found" means any 16-character window of the value, as in the secrets eval, so a partial copy counts.
Only the fake Keychain holds the values; the files hold references, ciphertext and one-way hashes.
"""

import base64
import json
import time
from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Final

from tests.factories import (
    DEV_BOT_TOKEN,
    DEV_GROUP_ID,
    FAKE_KEYS,
    OWNER_ID,
    PERSONAL_BOT_TOKEN,
    Fakes,
    make_dev_deps,
    make_owner_origin,
    open_test_db,
    write_master_key,
)
from tests.fakes.clock import DEFAULT_START
from tests.fakes.security import FakeSecurity
from tests.unit.ops.test_install import NEW_DEV_TOKEN, Rig, make_rig, write_env
from yordamchi.adapters.keystore.keychain import KeychainKeystore
from yordamchi.bots.cli import check
from yordamchi.core import config, keychain
from yordamchi.core.paths import BotName
from yordamchi.shared.vault import service as vault
from yordamchi.shared.vault.domain import NewSecret

NEW_PERSONAL_TOKEN: Final = "444555666:" + ("AAnewPersonalTokenRotated_" + "0123456789")[:35]
GOOGLE_KEY: Final = FAKE_KEYS["google_api_key"]
WINDOW: Final = 16


def windows(value: str) -> list[bytes]:
    if len(value) <= WINDOW:
        return [value.encode()]
    return [value[start : start + WINDOW].encode() for start in range(len(value) - WINDOW + 1)]


def every_file(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            yield path


def leaks(root: Path, secrets: dict[str, str], raw: dict[str, bytes]) -> list[str]:
    found: list[str] = []
    for path in every_file(root):
        data = path.read_bytes()
        found += [f"{name} in {path.relative_to(root)}" for name, value in secrets.items() if _holds(data, value)]
        found += [f"{name} in {path.relative_to(root)}" for name, value in raw.items() if value in data]
    return found


def _holds(data: bytes, value: str) -> bool:
    return any(piece in data for piece in windows(value))


def security_runner(security: FakeSecurity) -> check.Runner:
    async def run(argv: Sequence[str], timeout: float) -> check.RunResult:
        if argv[0] != keychain.SECURITY:
            return check.RunResult(1)
        done = security(argv, stdin=None, timeout=timeout)
        return check.RunResult(done.returncode, done.stdout.decode())

    return run


def no_bot_api(token: str) -> check.BotApi:
    raise AssertionError("the offline check never calls the Bot API")


async def run_the_bots(rig: Rig) -> str:
    """Both configs load; the dev vault stores, opens and publishes a key; the check script runs. Returns its output."""
    dev = config.load_dev_config({}, home=rig.home, keychain_runner=rig.security)
    personal = config.load_personal_config({}, home=rig.home, keychain_runner=rig.security)
    assert (dev.bot_token, personal.bot_token) == (NEW_DEV_TOKEN, NEW_PERSONAL_TOKEN)
    paths = rig.paths
    keystore = KeychainKeystore(bot=BotName.DEV, legacy_key_file=paths.master_key, runner=rig.security)
    db = await open_test_db(paths.db_file(BotName.DEV))
    try:
        deps = replace(make_dev_deps(db=db, paths=paths, fakes=Fakes()), keystore=keystore)
        async with db.uow() as s:
            await vault.add_secrets(
                s,
                keystore,
                origin=make_owner_origin(BotName.DEV),
                secrets=[NewSecret(kind="google_api_key", value=GOOGLE_KEY)],
                at=DEFAULT_START,
            )
        assert await vault.publish_secrets(deps) == 1
        assert await vault.open_secret(deps, "google_api_key") == GOOGLE_KEY
    finally:
        await db.dispose()
    ctx = check.Context(
        paths=paths,
        environ={},
        uid=501,
        now=time.time(),  # noqa: TID251  # the check compares heartbeat mtimes with the wall clock
        run=security_runner(rig.security),
        bot_api=no_bot_api,
        search_path="",
        telegram=False,
    )
    return check.render(await check.run_checks(ctx), colour=False)


async def test_no_secret_value_is_in_any_file_after_install_rotate_and_run(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    paths = rig.paths
    legacy_key = write_master_key(paths.master_key)  # before: the key in a file, and a plaintext token in dev.env
    before = f"DEV_BOT_TOKEN={DEV_BOT_TOKEN}\nOWNER_ID={OWNER_ID}\nDEV_GROUP_ID={DEV_GROUP_ID}\n"
    write_env(paths.env_file(BotName.DEV), before)

    rig.prompter.answers = {"PERSONAL_BOT_TOKEN": [PERSONAL_BOT_TOKEN]}
    assert rig.installer(enable_personal=True).install() == 0, rig.out.getvalue()
    rig.prompter.answers = {"DEV_BOT_TOKEN": [NEW_DEV_TOKEN], "PERSONAL_BOT_TOKEN": [NEW_PERSONAL_TOKEN]}
    assert rig.installer(env_only=True, rotate_tokens=True).install() == 0, rig.out.getvalue()
    report = await run_the_bots(rig)

    master_b64 = base64.b64encode(legacy_key).decode()
    secrets = {
        "old dev token": DEV_BOT_TOKEN,
        "old personal token": PERSONAL_BOT_TOKEN,
        "new dev token": NEW_DEV_TOKEN,
        "new personal token": NEW_PERSONAL_TOKEN,
        "master key": master_b64,
        "vault value": GOOGLE_KEY,
    }
    assert leaks(rig.home, secrets, {"raw master key": legacy_key}) == []
    for output in (rig.out.getvalue(), report):
        assert [name for name, value in secrets.items() if any(p.decode() in output for p in windows(value))] == []

    # The search had something to search, and the secrets are where they belong.
    assert rig.security.get(keychain.MASTER_KEY) == master_b64  # imported, then the file was deleted
    assert not paths.master_key.exists()
    assert (rig.security.get(keychain.DEV_BOT_TOKEN), rig.security.get(keychain.PERSONAL_BOT_TOKEN)) == (
        NEW_DEV_TOKEN,
        NEW_PERSONAL_TOKEN,
    )
    assert f"DEV_BOT_TOKEN={keychain.DEV_BOT_TOKEN.reference}\n" in paths.env_file(BotName.DEV).read_text()
    personal_env = paths.env_file(BotName.PERSONAL).read_text()
    assert f"PERSONAL_BOT_TOKEN={keychain.PERSONAL_BOT_TOKEN.reference}\n" in personal_env
    assert paths.db_file(BotName.DEV).stat().st_size > 0
    assert json.loads(paths.secret_hashes.read_text())["sha256"]  # one-way hashes for the pre-push scan
    searched = {path.name for path in every_file(rig.home)}
    assert {"dev.env", "personal.env", "dev.db", "secret-hashes.json", "uz.yordamchi.dev.plist"} <= searched
    assert "XATO" not in report.split("== Maxfiy qiymatlar (Keychain) ==")[1].split("==")[0]

    # And the search would have seen a leak: a 16-character piece of the new token, planted in a log file.
    canary = paths.logs / "dev.jsonl"
    canary.write_text(f'{{"event": "oops", "detail": "{NEW_DEV_TOKEN[4:20]}"}}\n')
    assert leaks(rig.home, secrets, {}) == [f"new dev token in {canary.relative_to(rig.home)}"]
