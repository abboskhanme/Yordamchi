# Pending wiring

Shared-file changes that parallel tasks asked for. They are applied by the merge train (MT) one at a time,
after the tasks writing the same files have merged. Remove an item once it is done.

## From T09 (Telegram adapter)
- [ ] `core/errors.py` + `core/strings.py`: add a `FileTooLarge(ExternalError, retryable=False)` class (params `limit_mb`, `size_mb`) with codes `file_too_large_in` / `file_too_large_out` and matching Uzbek `S.ERRORS` entries (SPEC §3.2 names `S.FILE_TOO_LARGE_IN`). Then make `adapters/telegram/translate.py` raise it instead of the interim `TelegramFileTooLarge(ProviderUnavailable)`. Do this after T08 merges, because T08 is editing these files.
- [ ] Optional precise codes: `telegram_chat_migrated` (carries `migrate_to_chat_id`), `telegram_forbidden`, `telegram_request_refused`.
- [ ] `pyproject.toml` C5: allow `aiohttp` in `yordamchi.adapters.telegram.**`. Then replace the module-name sniffing in `translate_transport()` with real `aiohttp` exception classes.
- [ ] T34 `bots/dev/app.py`: build `TelegramMessenger(config.token)`, hand `messenger.bot` to the polling runner, and `await messenger.close()` on shutdown.
- [ ] After X01: re-record `tests/fixtures/telegram/` from a test bot.
