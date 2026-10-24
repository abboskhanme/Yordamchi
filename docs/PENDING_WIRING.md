# Pending wiring

Shared-file changes that parallel tasks asked for. They are applied by the merge train (MT) one at a time,
after the tasks writing the same files have merged. Remove an item once it is done.

## From T09 (Telegram adapter)
- [ ] `core/errors.py` + `core/strings.py`: add a `FileTooLarge(ExternalError, retryable=False)` class (params `limit_mb`, `size_mb`) with codes `file_too_large_in` / `file_too_large_out` and matching Uzbek `S.ERRORS` entries (SPEC §3.2 names `S.FILE_TOO_LARGE_IN`). Then make `adapters/telegram/translate.py` raise it instead of the interim `TelegramFileTooLarge(ProviderUnavailable)`. Do this after T08 merges, because T08 is editing these files.
- [ ] Optional precise codes: `telegram_chat_migrated` (carries `migrate_to_chat_id`), `telegram_forbidden`, `telegram_request_refused`.
- [ ] `pyproject.toml` C5: allow `aiohttp` in `yordamchi.adapters.telegram.**`. Then replace the module-name sniffing in `translate_transport()` with real `aiohttp` exception classes.
- [ ] T34 `bots/dev/app.py`: build `TelegramMessenger(config.token)`, hand `messenger.bot` to the polling runner, and `await messenger.close()` on shutdown.
- [ ] After X01: re-record `tests/fixtures/telegram/` from a test bot.

## From T13 (STT adapters) — mostly for T19 `shared/voice`
- [ ] T19: add `mode: str` to `Transcriber.transcribe`, so the `stt.mode` setting reaches Gemini, which uses the constant `"smart"` today.
- [ ] T19: add `validate_key(key, *, models, timeout) -> SpeechKeyCheck` to the `Transcriber` Protocol, so services can call it through the port. Both adapters implement it already. Update the CONVENTIONS §4 port table if the signature changes.
- [ ] T19: add a setting `stt.openrouter_fallback_model` (default `google/gemini-3.5-transcribe`) and pass it per call.
- [ ] T19 + cost: add an optional `cost_usd_micros` / usage field to `Transcript`, so OpenRouter's `usage.cost` reaches `usage_records` (NFR-COST).
- [ ] Optional error codes: `stt_audio_unreadable` (ffmpeg failure) and `stt_model_unavailable` (404 or missing model). Both map to `ProviderUnavailable` today.
- [ ] `pyproject.toml` C5: confine `httpx` to `yordamchi.adapters.gemini`. It comes in transitively through google-genai.
- [ ] T34 `app.py`: build `GeminiTranscriber(api_key=<vault reader google_api_key>)` and `OpenRouterTranscriber(api_key=<vault reader openrouter_api_key>, ffmpeg=paths.bin / "ffmpeg")`, and call `aclose()` on both at shutdown.
