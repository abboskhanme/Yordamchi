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

## From T08 (inbox, audit, vault, cost)
- [ ] Optional setting `timeouts.ack_s` (float 2.5, Range(0.5, 10), TIMEOUTS, BOTH): add it to SPEC §4.3, to `SPEC_KEYS` in `test_registry` and to the inbox defaults block, then replace `inbox.domain.ACK_TIMEOUT_S` with it.
- [ ] T14: OwnerOnly mints the `OwnerOrigin`. The handler builds an `IncomingUpdate` (`domain.kind_for_text`) and awaits only `inbox.service.accept_update(deps, origin, update)`. The poller calls `get_poll_offset`, and `advance_poll_offset(deps, ids)` after each batch.
- [ ] T15: move the calls in `inbox.service._acknowledge` into priority-0 outbox rows inside `_record`'s transaction, and add typing dedupe and repeat (4 s, up to 60 s). **Known gap until then:** a crash after commit and before the ack leaves the key message undeleted.
- [ ] T16: consume `list_received_updates` and `mark_updates_attached(s, …)`, and add `ForeignKey("bursts.id")` to `inbound_updates.burst_id`.
- [ ] T34:
  - `FileKeystore(paths.master_key)`.
  - `vault.service.publish_secrets(deps)` at startup. `VaultKeyMissing` or `VaultKeyInsecure` → degraded mode, shown in `holat?`. Republish periodically.
  - The retention scheduler calls `{inbox,audit,vault,cost}.service.apply_retention(deps)`.
- [ ] T18: read `secret-hashes.json` as `{"sha256": [sorted hex]}`.
- [ ] T19 / P2: read keys with `vault.service.open_secret(deps, name)`. A newer key of the same kind arrives as `<kind>_2`; the consumer decides whether to retire the old one.
- [ ] T28: the installer writes `master.key` as base64(32 random bytes) plus a newline, mode 0600, in a 0700 directory (the same format as `tests.factories.write_master_key`).
- [ ] T42 / core: `core.log.configure` must pin the `aiosqlite` and `sqlalchemy.engine` loggers to ≥ INFO. At DEBUG, aiosqlite logs SQL params, i.e. message content.

## From T23 (supervisor)
- [ ] T28 plist: `EnvironmentVariables` uses `YORDAMCHI_DATA_DIR` (SPEC §9.1 is fixed). The installer builds the supervisor venv and runs `python -I -m yordamchi_supervisor`. The supervisor plist sets `ExitTimeOut` 10.
- [ ] T28 `docs/DEPLOY.md`: restore steps (`backups.py` docstring, `test_backups.py::Restore`), where the logs are, the supervisor heartbeat.
- [ ] T28 check script: `run/supervisor.heartbeat.json` is fresh; `dev.env` is 0600.
- [ ] **T32 must ship the bot heartbeat writer** (`run/<bot>.heartbeat.json`, written atomically, with `started_at`) before T43. Otherwise the watchdog restarts the dev bot every ~2 min.
- [ ] Optional: a low-disk alert in the supervisor, as a backstop for when the dev bot is down.

## From T22 (ssh + macos adapters)
- [ ] T24 + DEF: add the setting `guard.max_pressure_level` (int, default 2, OneOf(1, 2, 4), a safeguard: raising it weakens it) and use it in the scheduler (SPEC §5.6, revised). An unreadable pressure level queues the turn and logs it.
- [ ] Error codes `ssh_host_unknown` and `ssh_host_key_rejected` (optionally also `ssh_dns_failed`, `ssh_config_invalid`) in `core/errors.py` and `core/strings.py`, then update `adapters/ssh/translate.py::to_error`. Until then the owner sees a generic "javob bermayapti".
- [ ] T34 `app.py`:
  - `OpenSshRemoteShell(ssh="/usr/bin/ssh", path=<login-shell PATH + SYSTEM_PATH, without paths.shims>)`.
  - `MacOsHost(home=paths.home)` in both bots.
- [ ] T31: pass the ≤ 3 min timeout from settings, and truncate `RemoteResult` output before storing it (`core.proc.run` has no output cap).
- [ ] T54: match `interactive_sessions()` ids against our own sessions. Without a registry, an orphaned SDK process counts as the owner's (deliberately conservative).

## From T10 + T11 (Claude executor + guard)
- [ ] T24 sessions:
  - set `setting_sources=("user","project","local")` for coding, investigation and smoke; the adapter passes the spec value through as given;
  - fill the new `SessionSpec` fields `hook_timeout_s` (`gate.hook_timeout_s`), `question_hold_s` (`questions.hold_process_min`) and `question_parked_message` (`prompts/dev/question_parked.md`);
  - pass `YORDAMCHI_SOCK`, `YORDAMCHI_SESSION_TOKEN` and `YORDAMCHI_PY` in `spec.env`.
- [ ] T24 events:
  - `TurnResult.interrupted` is a stopped turn;
  - `background=True` is a late note that no running turn owns;
  - handle `UsageWindow`;
  - `RuntimeFailed(process_died, retryable)` after a backstop kill means `needs_resume`.
- [ ] T34 `app.py`: `ClaudeAgentRuntime(cli_path=default_cli_path(home), search_path=proc.build_search_path(paths, login_path), githooks_dir=paths.githooks)`. The startup `os.environ` scrub is still required, because the SDK merges the inherited env.
- [ ] Prompts: move `guard.SUBAGENT_QUESTION_MESSAGE` (an English constant in the adapter) into `prompts/dev/`.
- [ ] STACK §8 re-check list: re-verify the private SDK API in `adapters/claude/control.py` (`_query.receive_messages`, `_send_control_request`, `_initialization_result["pid"]`) and the `CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS` opt-in on every SDK or CLI upgrade.

## From T16 (burst)
- [ ] T34 `app.py`:
  - run `burst.service.run_bursts(deps, wake=…, notify=…)` under `core.aio.forever`; it does the §2.7 recovery itself;
  - set `wake` after each `accept_update`;
  - `notify` must not block: it wakes the router and starts STT jobs for `events.transcribe`;
  - the router reads `list_closed_bursts` and calls `mark_burst_routed/merged/cancelled(s, …)`;
  - retention calls `burst.service.apply_retention(deps)`.
- [ ] T19:
  - after each STT result, call `record_transcript(deps, update_id=…, text=…, ok=…)`, then `wake`; when `ok=False`, the text is the "ovoz tushunilmadi" marker;
  - after a restart, `recover_bursts` lists the voice notes still waiting;
  - optionally add AUDIO to `TRANSCRIBED_KINDS`.
- [ ] T20 / T24:
  - call `decide_dev_follow_up` before routing asks;
  - `RunningTurn` needs `message_ids` (from `message_links`), `media_group_ids` and `last_part_at`;
  - pre-parse uses `burst.domain.interrupt_of` / `TurnInput.interrupt`, the single interrupt-word list.
- [ ] Future FKs to `bursts.id` (turns, deliveries, route_decisions, answers) must be `ON DELETE SET NULL` or left out. Bursts are deleted after 30 days, while those rows live 90–400 days.
- [ ] T27: the eval runner replays `tests/eval/burst/cases.yaml` (32 dev cases; the format is described at the top of that file).
- [ ] T46 QOLLANMA: a line on how "kut" and "to'xta" behave.

## From T07 (gate domain)
- [ ] DEF: add the setting `gate.shim_grant_window_s` (default 120, weakens on increase; SPEC §5.7). `shim_verdict` takes it as `window_s`.
- [ ] T18 strings: an Uzbek consequence or "where" message for each gate `Reason` code; messages for the no-class asks `opaque_command`, `unparseable` and `recipe_unknown`; deny prompts for `read_only_policy`, `plan_only_policy` and `self_update_main`.
- [ ] T18 service:
  - loop fetching facts until `needs()` is empty (max 3 rounds);
  - make recipes are keyed `ScriptRef("make", "<makefile>:<target>" or "<target>", dir)`;
  - pass None for binary or missing scripts;
  - call `remote_invocations()` when a grant is consumed, so the shims can match.
- [ ] T18 hardening (orchestrator decision 2026-10-24):
  - `git reset --hard`, `git checkout -- .`, `git restore .` and `git stash drop/clear` must **ask (fs_destroy)** when the working tree holds uncommitted changes that existed before the current turn started (the owner may have edited at the Mac). Otherwise they stay free. Add a `dirty_before_turn` fact.
  - Add a `remote_hosts` fact (git remote URL → host), so a push to a remote *name* that points at a server is caught at Layer 1, not only by the ssh shim (residual R-20).
- [ ] T17: `projects.deploy_branches_json` holds fnmatch globs, with `*` when the workflows can't be parsed.
