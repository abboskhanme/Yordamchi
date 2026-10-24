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
- [x] T14 done: OwnerOnly mints `OwnerOrigin`; the poller handles offsets. Remaining part → see T34 under "From T14".
- [x] T15 done: inbox acks are priority-0 outbox rows, written in the same transaction; typing dedupe and repeat are in the outbox.
- [ ] T16: consume `list_received_updates` and `mark_updates_attached(s, …)`, and add `ForeignKey("bursts.id")` to `inbound_updates.burst_id`.
- [ ] T34:
  - `FileKeystore(paths.master_key)`.
  - `vault.service.publish_secrets(deps)` at startup. `VaultKeyMissing` or `VaultKeyInsecure` → degraded mode, shown in `holat?`. Republish periodically.
  - The retention scheduler calls `{inbox,audit,vault,cost}.service.apply_retention(deps)`.
- [ ] T18: read `secret-hashes.json` as `{"sha256": [sorted hex]}`.
- [ ] T19 / P2: read keys with `vault.service.open_secret(deps, name)`. A newer key of the same kind arrives as `<kind>_2`; the consumer decides whether to retire the old one.
- [x] T28: the installer writes `master.key` as base64(32 random bytes) plus a newline, mode 0600, in a 0700 directory (the same format as `tests.factories.write_master_key`).
- [ ] T42 / core: `core.log.configure` must pin the `aiosqlite` and `sqlalchemy.engine` loggers to ≥ INFO. At DEBUG, aiosqlite logs SQL params, i.e. message content.

## From T23 (supervisor)
- [x] T28 plist: `EnvironmentVariables` uses `YORDAMCHI_DATA_DIR` (SPEC §9.1 is fixed). The installer builds the supervisor venv and runs `python -I -m yordamchi_supervisor`. The supervisor plist sets `ExitTimeOut` 10.
- [x] T28 `docs/DEPLOY.md`: restore steps (`backups.py` docstring, `test_backups.py::Restore`), where the logs are, the supervisor heartbeat.
- [x] T28 check script: `run/supervisor.heartbeat.json` is fresh; `dev.env` is 0600.
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

## From T17 (projects + git)
- [ ] T34 `app.py`:
  - set `DevDeps.git = GitCli(build_search_path(paths, login_shell_path))`;
  - run `projects.service.sync_projects(deps)` under `core.aio.forever`, at startup and then every `SCAN_INTERVAL_S` (120 s);
  - add intent rows `alias_project`, `archive_project`, `describe_project` and `list_projects`.
- [ ] Optional settings `schedule.projects_scan_s` (120) and `timeouts.git_s` (10) in SPEC §4.3 and `SPEC_KEYS`, replacing the domain constants.
- [ ] T20: content and fuzzy routing only over `Project.routable`; exact names via `find_project` / `domain.find_projects`; use `ProjectAmbiguous` / `ProjectNotFound`.
- [ ] **T17b (orchestrator decision, fail closed):** platform Git integrations deploy without a workflow file. If a project has `vercel.json`, `.vercel/project.json`, `netlify.toml`, `render.yaml`, `railway.json`/`railway.toml` or `app.yaml` (App Engine), its default branch counts as deploying. Small change in `dev/projects/domain.py` plus tests; it can ride with T20 or T24.

## From T12 (assistant pool)
- [ ] T34 `app.py`:
  - build `ClaudeAssistant(cli_path=default_cli_path(home), search_path=..., sessions_dir=DATA/sessions, clock=SystemClock())`;
  - call `warm()` at startup and after a settings change;
  - call `await assistant.close(timeout=...)` at shutdown.
- [ ] **Memory budget (orchestrator decision 2026-10-24):** keep warm processes few, since this Mac already sits at pressure level 2.
  - `routing`: live + spare.
  - `report`: live only (a cold ask is 2.2 s, still within 4 s).
  - `settings`: no warm client, cold on demand.
  - Make this a setting `assistant.warm_clients` (dict kind → 0|1|2; defaults routing=2, report=1, settings=0) in SPEC §4.3 / DEF. The pressure guard counts these processes.
- [ ] Callers (T20, T26, T30, T21):
  - render `prompts/dev/<kind>_system.md` with the fixed parts only;
  - pass `instructions` (from `turn_context.md`) and `prompt_version` on every ask;
  - use `timeouts.<kind>_s` and `models.<kind>`.
- [ ] Settings: add `models.routing_effort` (default `low`). One cold routing ask at default effort took 7 s.
- [ ] Executor: switch to `adapters/claude/cli.py` (`probe`, `user_content`) to remove duplicated code (C7 keeps `assistant.py` from importing `executor.py`).
- [ ] P3 personal `qa` kind:
  - options: WebSearch/WebFetch + SDK servers, `--restricted`, the allowlist hook, `CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION`;
  - pool per conversation.

## From T14 (access + bots/common.py)
- [ ] T34 `bots/<bot>/app.py`:
  - `dp = common.build_dispatcher(deps, telegram.router)`, with `dp["deps"] = deps`;
  - run the poller: `aio.forever("poller", lambda: common.run_polling(deps, dp, messenger.bot, heartbeat=<T32 writer>))`;
  - handlers take `origin: OwnerOrigin`, build `IncomingUpdate`, and await only `inbox.service.accept_update`;
  - every job runs as `await common.guarded(deps, work(), job="…")`.
- [ ] T34: the aiogram Message → `IncomingUpdate` translation (attachments, caption, `media_group_id`, `reply_to`) lives in each `bots/<bot>/telegram.py`, because `common.py` is locked.
- [ ] T32: the heartbeat writer is the `heartbeat` hook (`PollTick(at, updates)`). Still to add: a polling pause mode (heartbeat at least every 30 s while paused) for self-update probation (P2).
- [ ] T15 (optional): send the `guarded()` owner notice as a priority-0 outbox row.
- [ ] T28 check script and QOLLANMA:
  - `DEV_GROUP_ID` must be a `-100…` **supergroup** id. A basic group that Telegram later upgrades changes its id, and the bot would then leave it as foreign.
  - The owner must not post as an **anonymous admin**: messages then come from `GroupAnonymousBot` and are dropped.

## From T15 (outbox)
- [ ] T34 `app.py`:
  - `aio.spawn("outbox", aio.forever("outbox", lambda: outbox.run_sender(deps)))`;
  - on shutdown, `outbox.flush_queue(deps, max_priority=1, within_s=5)` (SPEC §2.6);
  - retention calls `outbox.service.apply_retention(deps)`.
- [ ] `inbox/domain.py`: delete the unused `ACK_TIMEOUT_S`. The ack timeout is now `SenderConfig.ack_timeout_s`.
- [ ] Optional settings (SPEC §4.3 + `SPEC_KEYS`): `timeouts.telegram_ack_s` (2.5), `timeouts.telegram_send_s` (10), `timeouts.telegram_file_s` (60).
- [ ] `schedule.quiet_hours` → `disable_notification`: apply it in the outbox sender.
- [ ] Callers:
  - `outbox.stop_typing(s, …)` when work ends without a reply (T20, T24, P3 Q&A);
  - T24: `show_progress(s, …, every_s=snap[OUTBOX_PROGRESS_EDIT_S])`;
  - pass `max_parts=snap[OUTBOX_MAX_PARTS]` to `enqueue_text` / `enqueue_file`;
  - T20: the reply signal is `outbox.find_message_link(deps, chat_id=…, message_id=…)`;
  - T32 heartbeat:
    - `enqueue_text(kind=HEARTBEAT, ref="online")`;
    - `enqueue_pin(target="heartbeat:online:0")`;
    - store `get_delivery(deps, key).message_id` in `kv_state`;
    - `enqueue_edit(message_id=…)`.
- [ ] FKs (`ON DELETE SET NULL`) on `outbox_messages.project_id` / `turn_id` and on `message_links.project_id` / `turn_id` / `question_id` / `decision_id`, once those tables exist.
- [ ] Optional T09: a `parse_entities_failed` code, so HTML that Telegram refuses is resent as plain text.

## From T18 (gate service, shims, githooks)
- [ ] T24:
  - per session, `SessionSpec.guard = gate.service.make_guard(deps, lambda: GateScope(project_id, project_root, session_key=agent_sessions.id, policy, turn_id, dirty_before_turn=<git status at turn start>, protected_pids=<bot pid>, mcp lists))`;
  - deliver `DecisionAnswer.delivery` to the turn (`next` or `new_turn`);
  - keep the token → scope map.
- [ ] T34:
  - control socket: `control.method_of(line) in control.GATE_METHODS` → `control.dispatch(line, handlers=GateHandlers(...))`, which calls `service.check_shim` and `service.report_secret_block`;
  - run `post_unasked_decisions` and `vault.publish_secrets` at startup and periodically. **Without the hash file, every push is blocked.**
  - retention calls `gate.service.apply_retention`;
  - routing calls `answer_decision` to bind replies.
- [ ] Outbox: move `post_question` / `_tell` into priority-0 outbox rows in the same uow, and move reply binding to `message_links`.
- [ ] T29: pass open questions into `DecisionReply.open_questions`, and use `service.pending_items`.
- [ ] SPEC docs:
  - §4.3: add `gate.shim_grant_window_s`;
  - §3.1.1: `secret_blocked {cwd, file, line, rule, session_token} → {ok, enforce}`;
  - shim invocation: `-m yordamchi.bots.cli.shim <shims dir> <tool>`.
- [ ] **T18b (orchestrator decisions, P1):**
  - (1) `githooks/` gets pass-through wrappers for pre-commit, commit-msg, prepare-commit-msg, post-commit, post-checkout, post-merge and pre-rebase. Each one runs the project's own hook (its repo-local `core.hooksPath`, e.g. husky, or `.git/hooks/<name>`), so the project's hooks still run in bot sessions.
  - (2) `stash drop/clear` asks when `git stash list` holds entries from before the turn, even with a clean tree.
  - (3) the shim allows read-only `git-upload-pack` (fetch or pull from a server remote).

## From T19 (voice)
- [ ] T34:
  - the burst `notify` starts `voice.service.transcribe_parts(deps, events.transcribe, wake=…, notify=…, named=…)` via `aio.spawn`; dev passes `named=(partial(projects.service.list_projects, deps),)`, personal passes `()`;
  - adapters get `api_key=partial(voice.service.read_speech_key, deps, "google"|"openrouter")`, so build deps first and bind late;
  - when `accept_update` returns speech-key secrets, run `voice.service.configure_speech_keys(...)` as a follow-up job;
  - retention calls `voice.service.apply_retention`;
  - add an intent row for "bu so'zni to'g'ri yoz: …" → `add_owner_term`.
- [ ] Burst: carry `chat_id` on parts in `events.transcribe`.
- [ ] Outbox: send the voice notices as outbox rows in the same uow.
- [ ] SPEC/CONVENTIONS docs:
  - the `transcribe(mode=, fallback_model=)` and `validate_key` signatures;
  - `stt.openrouter_fallback_model` in §4.3;
  - `transcripts (bot, update_id)` in §4.2.
- [ ] Later: P3 TTS key validation; T51 caps (ask before OpenRouter becomes a paid provider); P2 integration names in the vocabulary; P3 cross-bot speech keys; splitting long notes at silences.

## From T28 (installer, plists, check)
- [ ] **Blocks T43:** pin the ffmpeg + ffprobe 9.0.2 arm64 download URLs and SHA-256 in STACK §5 and `FFMPEG_PINS` in `scripts/install.py`. Until then use `--skip-ffmpeg`.
- [ ] `python -m yordamchi.check`: add the shim `src/yordamchi/check.py` (`from yordamchi.bots.cli.check import main; raise SystemExit(main())`) or a console script.
- [ ] `pyproject.toml`: the per-file ignore `"scripts/**" = ["TID251", "S603", "E402"]`, and add `scripts` to the mypy files list.
- [ ] T57: reuse the release naming and these `uv sync` flags: `--frozen --no-dev --no-editable --compile-bytecode --link-mode clone --python 3.12.15`.
- [ ] Before T43: T32 and T34 merged (otherwise the dev plist crash-loops), and both tokens rotated (`--env-only --rotate-tokens`, DEPLOY.md §6).
- [ ] The SPEC §9.3 "ClaudeManager is running" check needs its process name.
