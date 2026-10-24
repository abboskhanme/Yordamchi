# Pending wiring

Shared-file changes that parallel tasks asked for. They are applied by the merge train (MT) one at a time,
after the tasks writing the same files have merged. Remove an item once it is done.

## From T09 (Telegram adapter)
- [ ] `core/errors.py` + `core/strings.py`: add a `FileTooLarge(ExternalError, retryable=False)` class (params `limit_mb`, `size_mb`) with codes `file_too_large_in` / `file_too_large_out` and matching Uzbek `S.ERRORS` entries (SPEC §3.2 names `S.FILE_TOO_LARGE_IN`). Then make `adapters/telegram/translate.py` raise it instead of the interim `TelegramFileTooLarge(ProviderUnavailable)`. Do this after T08 merges, because T08 is editing these files.
- [ ] Optional precise codes: `telegram_chat_migrated` (carries `migrate_to_chat_id`), `telegram_forbidden`, `telegram_request_refused`.
- [ ] `pyproject.toml` C5: allow `aiohttp` in `yordamchi.adapters.telegram.**`. Then replace the module-name sniffing in `translate_transport()` with real `aiohttp` exception classes.
- [x] T34 `bots/dev/app.py`: build `TelegramMessenger(config.token)`, hand `messenger.bot` to the polling runner, and `await messenger.close()` on shutdown. Done in T34.
- [ ] After X01: re-record `tests/fixtures/telegram/` from a test bot.

## From T13 (STT adapters) — mostly for T19 `shared/voice`
- [ ] T19: add `mode: str` to `Transcriber.transcribe`, so the `stt.mode` setting reaches Gemini, which uses the constant `"smart"` today.
- [ ] T19: add `validate_key(key, *, models, timeout) -> SpeechKeyCheck` to the `Transcriber` Protocol, so services can call it through the port. Both adapters implement it already. Update the CONVENTIONS §4 port table if the signature changes.
- [ ] T19: add a setting `stt.openrouter_fallback_model` (default `google/gemini-3.5-transcribe`) and pass it per call.
- [ ] T19 + cost: add an optional `cost_usd_micros` / usage field to `Transcript`, so OpenRouter's `usage.cost` reaches `usage_records` (NFR-COST).
- [ ] Optional error codes: `stt_audio_unreadable` (ffmpeg failure) and `stt_model_unavailable` (404 or missing model). Both map to `ProviderUnavailable` today.
- [ ] `pyproject.toml` C5: confine `httpx` to `yordamchi.adapters.gemini`. It comes in transitively through google-genai.
- [x] T34 `app.py`: build `GeminiTranscriber(api_key=<vault reader google_api_key>)` and `OpenRouterTranscriber(api_key=<vault reader openrouter_api_key>, ffmpeg=paths.bin / "ffmpeg")`, and call `aclose()` on both at shutdown. Done in T34.

## From T08 (inbox, audit, vault, cost)
- [ ] Optional setting `timeouts.ack_s` (float 2.5, Range(0.5, 10), TIMEOUTS, BOTH): add it to SPEC §4.3, to `SPEC_KEYS` in `test_registry` and to the inbox defaults block, then replace `inbox.domain.ACK_TIMEOUT_S` with it.
- [x] T14 done: OwnerOnly mints `OwnerOrigin`; the poller handles offsets. Remaining part → see T34 under "From T14".
- [x] T15 done: inbox acks are priority-0 outbox rows, written in the same transaction; typing dedupe and repeat are in the outbox.
- [ ] T16: consume `list_received_updates` and `mark_updates_attached(s, …)`, and add `ForeignKey("bursts.id")` to `inbound_updates.burst_id`.
- [x] T34:
  - `FileKeystore(paths.master_key)`. Done in T34. A missing or insecure master key shows as `master.key` in `holat?`'s degraded line; republished every 10 min.
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
- [x] T34 `app.py`:
  - `OpenSshRemoteShell( Done in T34. MacOsHost in the dev bot; the personal bot's at P3.
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
- [x] T34 `app.py`: `ClaudeAgentRuntime(cli_path=default_cli_path(home), search_path=proc.build_search_path(paths, login_path), githooks_dir=paths.githooks)`. The startup `os.environ` scrub is still required, because the SDK merges the inherited env. Done in T34.
- [ ] Prompts: move `guard.SUBAGENT_QUESTION_MESSAGE` (an English constant in the adapter) into `prompts/dev/`.
- [ ] STACK §8 re-check list: re-verify the private SDK API in `adapters/claude/control.py` (`_query.receive_messages`, `_send_control_request`, `_initialization_result["pid"]`) and the `CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS` opt-in on every SDK or CLI upgrade.

## From T16 (burst)
- [x] T34 `app.py`:
  - run `burst.service.run_bursts( Done in T34. The router marks each burst through routing/turns/questions, or `burst.cancel_burst` for interrupts and commands.
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
- [x] T46 QOLLANMA: a line on how "kut" and "to'xta" behave. Done in T46 (QOLLANMA §2, §3).

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
- [x] T34 `app.py`:
  - set `DevDeps.git = GitCli( Done in T34.
  - set `DevDeps.git = GitCli(build_search_path(paths, login_shell_path))`;
  - run `projects.service.sync_projects(deps)` under `core.aio.forever`, at startup and then every `SCAN_INTERVAL_S` (120 s);
  - add intent rows `alias_project`, `archive_project`, `describe_project` and `list_projects`.
- [ ] Optional settings `schedule.projects_scan_s` (120) and `timeouts.git_s` (10) in SPEC §4.3 and `SPEC_KEYS`, replacing the domain constants.
- [ ] T20: content and fuzzy routing only over `Project.routable`; exact names via `find_project` / `domain.find_projects`; use `ProjectAmbiguous` / `ProjectNotFound`.
- [ ] **T17b (orchestrator decision, fail closed):** platform Git integrations deploy without a workflow file. If a project has `vercel.json`, `.vercel/project.json`, `netlify.toml`, `render.yaml`, `railway.json`/`railway.toml` or `app.yaml` (App Engine), its default branch counts as deploying. Small change in `dev/projects/domain.py` plus tests; it can ride with T20 or T24.

## From T12 (assistant pool)
- [x] T34 `app.py`:
  - build `ClaudeAssistant( Done in T34.
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
- [x] T34 `bots/<bot>/app.py`: Dev bot done in T34; the personal bot's at P3.
  - `dp = common.build_dispatcher(deps, telegram.router)`, with `dp["deps"] = deps`;
  - run the poller: `aio.forever("poller", lambda: common.run_polling(deps, dp, messenger.bot, heartbeat=<T32 writer>))`;
  - handlers take `origin: OwnerOrigin`, build `IncomingUpdate`, and await only `inbox.service.accept_update`;
  - every job runs as `await common.guarded(deps, work(), job="…")`.
- [x] T34: the aiogram Message → `IncomingUpdate` translation (attachments, caption, `media_group_id`, `reply_to`) lives in each `bots/<bot>/telegram.py`, because `common.py` is locked. Dev bot done in T34 (`bots/dev/telegram.incoming_of`); the personal bot's at P3.
- [ ] T32: the heartbeat writer is the `heartbeat` hook (`PollTick(at, updates)`). Still to add: a polling pause mode (heartbeat at least every 30 s while paused) for self-update probation (P2).
- [ ] T15 (optional): send the `guarded()` owner notice as a priority-0 outbox row.
- [ ] T28 check script and QOLLANMA (QOLLANMA §1 anonymous-admin line done in T46; check script still open):
  - `DEV_GROUP_ID` must be a `-100…` **supergroup** id. A basic group that Telegram later upgrades changes its id, and the bot would then leave it as foreign.
  - The owner must not post as an **anonymous admin**: messages then come from `GroupAnonymousBot` and are dropped.

## From T15 (outbox)
- [x] T34 `app.py`:
  - `aio.spawn("outbox" Done in T34.
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
- [x] T34:
  - control socket: Done in T34. `bots/dev/control_socket.py`; `status` too.
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
- [x] T34:
  - the burst `notify` starts Done in T34.
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
- [x] **Blocks T43:** pin the ffmpeg + ffprobe 9.0.2 arm64 download URLs and SHA-256 in STACK §5 and `FFMPEG_PINS` in `scripts/install.py`. Done 2026-10-24: build `1789931890_9.0.2`, checked against the publisher's `.sha256` files; `test_install.py` keeps STACK and `FFMPEG_PINS` equal. `--skip-ffmpeg` is no longer needed.
- [x] `python -m yordamchi.check`: add the shim `src/yordamchi/check.py` (`from yordamchi.bots.cli.check import main; raise SystemExit(main())`) or a console script. Done 2026-10-24 with the shim; the installer's final check now runs it. Left: DEPLOY.md §1 step 4 still shows the long form with "once its entry point is wired" (both forms work); drop that note in the next DEPLOY edit.
- [x] `pyproject.toml`: the per-file ignore `"scripts/**" = ["TID251", "S603", "E402"]`, and add `scripts` to the mypy files list. Done 2026-10-24; mypy found nothing new.
- [ ] T57: reuse the release naming and these `uv sync` flags: `--frozen --no-dev --no-editable --compile-bytecode --link-mode clone --python 3.12.15`.
- [ ] Before T43: T32 and T34 merged (otherwise the dev plist crash-loops), and both tokens rotated (`--env-only --rotate-tokens`, DEPLOY.md §6).
- [ ] The SPEC §9.3 "ClaudeManager is running" check needs its process name.

## From T20 (routing)
- [x] Router order (T34/T24): Done in T34 (`bots/dev/pipeline.py`), with one addition: commands that can never be an answer ("holat?", `/sozlamalar`, "loyihalar") come before step 2, because an only open agent question binds any text (see From T34).
  1. pre-parse: interrupts and the self-test;
  2. `answer_clarification`;
  3. approvals and question answers (`answer_decision`);
  4. `correct_route` (wrong-target intent);
  5. `route_burst`, or `decide_route` + `record_route(s, …)` inside T24's uow.
- [ ] `RouteFacts` inputs: T34: all but `reported` (no reader of each project's latest bot message yet; see From T34).
  - `reply_projects` from `message_links`;
  - `reported` activity from outbox/turns;
  - `running` as `RunningTurn(message_ids, media_group_ids, last_part_at)`;
  - `open_questions` / `others_pending` from T29/T18.
- [x] The outbox sends `Routed/Resolution/Correction/Expired.reply` before any delivery. After the question is sent, `set_clarification_message(deps, id, message_id)`. Done in T34 (`outbox.enqueue_reply`; a watcher sets the message id once the question is sent).
- [x] Burst: add `get_turn_input(deps, burst_id)`, so T24 can re-deliver for `Resolution` / `Correction`. Done in T34 (`burst.service.get_turn_input`).
- [x] T34:
  - `warm_routing(deps)` at startup Done in T34.
  - `warm_routing(deps)` at startup and after settings changes;
  - `expire_clarifications(deps)` about every minute;
  - retention calls `routing.service.apply_retention`.
- [ ] T24 wrong target (`CorrectionKind.MOVED`):
  - stop the turn and list its files;
  - re-deliver to `to_project`;
  - `decisions(kind=wrong_target_revert)`; on "ha" `_REVERTED`, on "yo'q" `_KEPT`.
- [ ] Routing asks don't carry images yet (≤ 2 images need a media download).
- [ ] Settings (SPEC §4.3 + `SPEC_KEYS`): `routing.clarify_expire_min` = 30, `routing.correct_window_min` = 120 (T21 adds `models.routing_effort`).
- [x] T46 QOLLANMA: answering "X'gami yoki Y'gami?" ("2", a name, "hech biri") and "bu X uchun edi". Done in T46 (QOLLANMA §2, §3).

## From T27 (burst + secrets evals)
- [x] Redact bugs T27-B1 and T27-B2 fixed on main (5cc9076), with regression tests.
- [ ] Once routing is wired, count its clarifying questions in the burst eval's "asks nothing" measure. T45 owner grading is still to come.
- [ ] When executor sessions exist, extend the secrets leak search to the Claude/Codex transcripts.
- [ ] Gap: an edit to a message already in a running turn is not a continuation (`burst/domain.py` `continues()`); decide in T24/T29.

## From T18b (project hooks, stash, fetch)
- [x] T18b done (ab79be9). The `githooks/` wrappers cover pre-commit, prepare-commit-msg, commit-msg, post-commit, post-checkout, post-merge, pre-rebase, post-rewrite and pre-push.
- [ ] T24: `YORDAMCHI_PY` in `spec.env` for every session; commits and checkouts fail without it.
- [ ] T24: `GateScope.turn_started_at` (tz-aware UTC); without it every `stash drop|clear` asks.
- [ ] Docs: STACK/CONVENTIONS port table: `Git.stash_entries` is new.

## From T21 (settings)
- [x] T34 `bots/dev`: Done in T34. A change by chat is a message starting with "sozlama…" (`intents._SETTINGS_REQUEST`).
  - "sozlamalaring qanday?" → `settings.service.show_settings(deps, origin)`;
  - a change by chat → `handle_request(deps, origin, text, ask=partial(gate.service.ask_setting_change, deps))`;
  - "oldingi holatiga qaytar" → `revert_setting(deps, origin, key, ask=…)`;
  - "ha"/"yo'q" go through `gate.answer_decision` (settings decisions are in `pending_items`).
- [ ] T34 assistant pools: size them from `assistant.warm_clients`, and re-warm after `models.*` / `assistant.warm_clients` changes. T34: a kind at "0" is not warmed, and every kind re-warms after any settings change; the 1-vs-2 (spare) count needs an adapter option (see From T34).
- [ ] T20 follow-up: pass `models.routing_effort` as `effort=` on routing asks; route the settings intent to `handle_request`. T34b: pre-parse takes the clear cues ("sozlama…", `/sozlamalar <request>`, "javoblaringni …", "ovozingni …", "o'zingni …"); routing's own `change_setting` intent is still open (From T34b).
- [ ] P3: a conversational `AskOwner` for the personal bot (stores `PendingChange.to_json()`, then `apply_pending_change`).
- [ ] SPEC §4.3: add `timeouts.settings_s`, `assistant.warm_clients`, `models.routing_effort` and the `DictOf` check; mention `prompts/dev/settings_context.md` in §3.2.15.

## From T24 (turns)
- [x] T34:
  - build `TurnsState()`, then run `recover_turns(deps)` first; Done in T34.
  - build `TurnsState()`, then run `recover_turns(deps)` first; its `interrupted` list feeds `S.HEALTH_RESTARTED` (T32);
  - `aio.forever("turns", lambda: turns.run_scheduler(deps, state))`.
- [x] T34 router:
  - `turns.service.route_turn( Done in T34.
  - `turns.service.route_turn(deps, state, item, facts)`; when `submitted` is None, handle `routed` (an ASK reply + `set_clarification_message`, reports, …);
  - `Correction.MOVED` → `correct_wrong_target(deps, state, from_project=, to_project=, burst_id=)`;
  - when a revert offer is open, call `answer_revert` before `gate.answer_decision`;
  - `gate.answer_decision` results → `deliver_decision(deps, state, answer)`.
- [x] T34 intents: `stop_turn`, `stop_all`, `redirect_turn`, `continue_turn`, `retry_delivery`. Done in T34.
- [x] T34 control socket scope: `state.scope_for(session_token)`. Done in T34.
- [x] T34 lifecycle: Done in T34. Photos go in as `images` (`turns.service.save_images`); other files are not named to the agent, which may not read under DATA (see From T34).
  - on SIGTERM, `shutdown_turns` before the outbox flush;
  - retention calls `turns.service.apply_retention`;
  - media paths go in via `request_from_input(files=, images=)`.
- [x] Burst: `get_turn_input(deps, burst_id)` is needed for `Resolution` and `Correction` re-delivery. Done in T34.
- [x] T29: set `state.questions` to a hook that posts `S.AGENT_QUESTION`; answers go through `answer_question(..., fallback_text=)`. Done (T29 + T34 wiring).
- [ ] T30: read `latest_handoff(deps, project)`. When `needs_synthesis` is set, it carries the last 20 progress lines; a turn-end hook is still to add.
- [ ] T31: refine `prompts/dev/investigate_system.md`; investigations are `submit_turn(kind=TurnKind.INVESTIGATE)`.
- [ ] T32: smoke turns need a registered project row; register a hidden `smoke` project or relax the check.
- [ ] Outbox FKs to `turns.id` (`ON DELETE SET NULL`) are now possible.
- [ ] SPEC §4.2/§4.3: the new `turns` columns (`start_dirty`, `start_files_json`, `waiting_since`, `handoff_json`, `revert_status`), the queue reasons `disk`/`busy`, and `guard.max_pressure_level` Range(1, 3).
- [ ] Open gap (T20/T27): an edit to a message already in a running turn is not treated as a continuation.

## From T32 (health)
- [x] T34 startup:
  - `hs = health.new_state( Done in T34.
  - `hs = health.new_state(deps, version=<release>, sdk_version=…, degraded=<missing ffmpeg/gh/node>)`.
  - Order: `turns.recover_turns` → `health.report_start(deps, hs, interrupted=…)` (this runs before the first heartbeat write) → start the loops.
- [x] T34 loops:
  - the poller hook calls `health.record_poll( Done in T34.
  - the poller hook calls `health.record_poll(deps, hs, at=tick.at, updates=tick.updates)`;
  - `aio.forever` runs `health.run_heartbeat(deps, hs)`, `health.run_online(deps)` (dev only) and `health.run_checks(deps, hs, smoke=partial(turns.service.run_smoke_turn, deps, turns_state))`.
- [x] T34 daily: `health.apply_retention` and `health.purge_media`. Done in T34. At `schedule.retention_at`, with every feature's retention.
- [x] T34 intent: `show_health` ("holat?", `/holat`) → `health.show_health(deps, hs, work=partial(turns.service.work_status, deps))`. Done in T34.
- [x] T34 SIGTERM: after the outbox flush, `health.mark_clean_exit(deps, hs)`. Done in T34.
- [x] T34 errors: call `health.note_error(deps, hs, code)` wherever error codes surface. Done in T34. Every job of the dev bot runs through `bots/dev/jobs.run`.
- [ ] P2: `health.set_mode(deps, hs, Mode.PAUSED | Mode.PROBATION)`.
- [ ] T35: goldens for `domain.holat_text`.
- [ ] Gate probe (SPEC §5.6, `ssh yordamchi-nohost true` must be denied) is not run yet. It needs a dry-run gate check API.
- [ ] **T32b (orchestrator decision, before T43): a network outage must not look like a dead bot.** The heartbeat gains `last_poll_attempt_at` and `last_poll_error_kind` (network or other). The watchdog restarts only when no poll *attempt* happened for 120 s (stuck loop). Polls that fail with network errors mean "offline", not "dead": the watchdog logs it and sends no alert, so a long Wi-Fi drop doesn't cause restart loops or crash-loop alerts. This needs a supervisor change plus a health change, with tests for both.
- [ ] `pressure` health events are not recorded yet.

## From T30 (digest)
- [ ] T34 `app.py`: T34: done but `server_checks`, whose reader is the T30 follow-up of From T31 (`latest_statuses` returns `HostStatus`, not `ServerCheck`); `server_status` goes to `dev/servers` meanwhile.
  - `DigestState(turns=turns_state, server_checks=<T31 reader>)`;
  - `turns_state.turn_ended = partial(digest.service.refresh_after_turn, state=digest_state)`;
  - `aio.forever("digest", lambda: digest.service.run_digest_poll(deps, digest_state))`;
  - `digest.service.warm_report(deps)` at startup and after settings changes.
- [x] T34 router: when `route_turn` submits nothing and the intent is in `digest.domain.REPORT_INTENTS`: Done in T34. `needs_server_check` and `server_status` go to `servers.answer_server_status`.
  - call `digest.service.answer_report(deps, digest_state, digest.domain.question_of(item, routed.decision, route_id=routed.route_id))`;
  - hand `needs_server_check` to T31;
  - `/hisobot [X]` takes the same path.
- [ ] T31: provide `latest_checks(deps) -> Sequence[digest.domain.ServerCheck]` and handle `needs_server_check`.
- [ ] T24 gap: `handoff_json` is written only for turns that end done. `_fail_turn` and `_end_stopped` should write a block-less `HandoffNote`, so interrupted and failed turns are synthesized too.
- [x] Routing: `Intent.INVESTIGATE` via `route_turn` sends no "Tekshiryapman" yet. Done in T34b (`route_turn` queues `S.CHECKING` with the delivery, priority 0).
- [ ] Docs:
  - SPEC §4.2: the extra `digests` columns;
  - SPEC §4.3: candidate keys `timeouts.report_budget_s`, `timeouts.digest_refresh_s`, `timeouts.handoff_synthesis_s`, `digest.active_within_days`;
  - the new prompts `report_context.md`, `digest_handoff_fallback.md`;
  - QOLLANMA lines. Done in T46 (QOLLANMA §5).

## From T29 (questions)
- [x] T34:
  - `state.questions = questions.service.open_question`; Done in T34.
  - `state.questions = questions.service.open_question`;
  - startup: `recover_questions(deps, state)` after `recover_turns`;
  - retention: `questions.service.apply_retention`.
- [x] T34 router step 3: after `answer_revert`, call `answer_owner(...)`. It handles gate decisions and agent questions, so it replaces the direct call to `gate.answer_decision`. Done in T34.
- [x] T34: when `answer_owner` returns ASKED and the outbox has sent `message_key`, call `routing.set_clarification_message(...)`. Done in T34.
- [x] T34: a routing `Resolution` with `Intent.ANSWER` → `answer_clarified(deps, state, item, asked=get_turn_input(resolution.burst_id), project_id=…)`. `burst.get_turn_input` is still to build. Skip `Resolution.reply` here, so the owner doesn't get "→ X" twice. Done in T34.
- [x] T34 `RouteFacts`:
  - `open_questions = await waiting_projects(deps)`; Done in T34.
  - `open_questions = await waiting_projects(deps)`;
  - set `others_pending` (routing) and `RevertReply.others_pending` (turns) when `open_items` or `gate.pending_items` is non-empty.
- [ ] Outbox: `ForeignKey("questions.id", ondelete="SET NULL")` on `message_links.question_id`.
- [ ] Docs:
  - SPEC §4.2 `questions`: `UNIQUE(turn_id, tool_use_id)`;
  - SPEC §3.2.2: the binding order;
  - the prompt `questions_answer.md`;
  - QOLLANMA: how to answer ("2", option text, own words, voice, reply, "BonviHR 2"). Done in T46 (QOLLANMA §4).

## From T31 (servers)
- [x] T34 `app.py`:
  - `aio.forever("servers" Done in T34.
  - `aio.forever("servers", lambda: servers.run_server_checks(deps))`;
  - retention calls `servers.service.apply_retention`.
- [x] T34 router: `server_status` → `answer_server_status(...)` as a guarded background job (a fresh check can take up to 150 s). Done in T34.
- [x] T34 router: a server investigation (the text names a configured host) → `investigate_server(...)`. Done in T34. Other intents about a named host: status from `dev/servers`, work to the project.
- [x] T34 intents:
  - "X serverini kuzat" Done in T34.
  - "X serverini kuzat" → `add_server_host`;
  - "X'ni kuzatma" → `remove_server_host`;
  - both raise `ServerJumpOnly` / `ServerNotWatched` / `SettingInvalid`.
- [ ] T30: consume `servers.service.latest_statuses` / `domain.status_line`, and refresh the digest after a check pass.
- [ ] Follow-up: add `systemctl --failed --no-legend` to `gate.readonly_remote_commands` (gate block of `defaults.py`) and to SPEC §5.7. Update the servers tests that expect `units: refused` (3 tests). Until then, failed units are not detected.
- [ ] SPEC §4.3: `servers.log_paths`, `servers.disk_warn_pct`, `servers.memory_warn_pct`, `timeouts.server_command_s`, `timeouts.server_check_s`.
- [ ] T22 SSH codes → `S.SERVERS_REASONS` entries once they exist.
- [ ] X06: the owner's host list, then a live SSH test.

## From T32b (offline ≠ dead)
- [x] T34 poller hook: `health.record_poll(deps, hs, at=tick.at, updates=tick.updates, error=tick.error)`. `error` is now required. Done in T34.
- [ ] SPEC §2.8: the heartbeat fields `last_poll_attempt_at`, `last_poll_error_kind`, `consecutive_poll_failures`, and the offline rule. SPEC §4.2: the `network_gap` event kind.

## From T51 (cost)
- [x] T34b: "xarajat?" / `/xarajat` → `cost.service.show_cost(deps, chat_id=…, ref=…, reply_to=…)` (an early command, answered as a reply to the question), and `aio.forever("cost", lambda: cost.service.run_cost_watch(deps))` in the dev bot (the 80 %/100 % cap notices, the optional daily summary).
- [ ] P3: the same loop in the personal bot (both bots write their own ledger; the caps are one budget).

## From T34 (dev bot wiring)
- [x] T33 in the dev bot: `IncomingUpdate(forward=inbox.domain.forward_of(message.model_dump(mode="json")))`; commands, interrupts and the revert answer read `TurnInput.own_text` (`RevertReply.of`); `settings.handle_request(…, item.own_text, …)`. `dev/questions` still builds `DecisionReply(…)` from `answer_text` (forwarded parts left out) rather than `DecisionReply.of`: a T29 follow-up, same effect.
- [x] T24: `turns.service.new_session(deps, state, project_id)` for "X yangi sessiya" (SPEC §6, P1): retire the current `agent_sessions` row (`store.retire_agent_session`) and close its live process; until then the intent answers `S.NOT_YET(phase="P1")`. Done in T34b: a working turn finishes in the old session first; the next turn opens a fresh one seeded with the handoff note (`prompts/dev/turns_new_session.md`).
- [ ] `RouteFacts.reported`: a reader of each project's latest bot message (result, question) from the outbox's `message_links`, for routing's context signal.
- [ ] T12: `Assistant.warm` takes the spare count from `assistant.warm_clients` ("1" = live client only); the adapter keeps a spare today.
- [ ] Files other than photos (documents, video, audio) sent with an instruction: saved nowhere the agent may read (the gate forbids DATA). Decide a project-local inbox folder or adapter-side attachments.
- [ ] T24 `domain.title_of`: a burst that starts with a photo gets the title "[image]" (reports show «[image]»); skip bare part labels.
- [ ] P2: the control socket's `selftest` and `headers`, the probation peek poller (`app._probation` only waits for the flag now), and `status.drained`.
- [ ] P3: the personal bot's `bots/personal/{app,telegram,intents}.py`, on the same `common` pieces.
- [ ] SPEC §6.1 / QOLLANMA (T46): QOLLANMA part done in T46 (§3, §5–§7) and T34b (§2 "kut" and typing, §3 "yangi sessiya", §5 "xarajat?", §7 settings in plain words); SPEC §6.1 still open: the early commands before binding, "sozlama: …" and the cue words for a settings change by chat, "X serverini kuzat" / "X'ni kuzatma", `/hisobot [X]`, `/stop`.

## From T34b (natural talk)
- [ ] SPEC §3.2.5 / §6: "kut" with nothing assembling no longer stops the running turn; it opens a burst that holds for the next part (`burst.hold_max_s`), acknowledged with `ui.hold_reaction`; a hold that nothing follows lapses (`cancelled`, no turn input).
- [ ] SPEC §4.3: `ui.hold_reaction` (👌) and `ack.typing_for_work` (false).
- [ ] Routing's LLM `change_setting` intent (messages about the bot with no cue word, "rasmdan keyin 15 soniya kut"): built and recorded once in T34b, but that live re-record missed UC-10 (clear 96.3 %, context 85.2 %) on cases it does not touch, so it is held back for the orchestrator's call; its 12 settings and 9 near-miss cases scored 21/21. Until then such a request needs "sozlama: …".
- [ ] SELF_UPDATE stays `S.NOT_YET` in the dev bot until its P2 drain and probation wiring.

## From the first live start (T43, 2026-10-24, release 2026.10.24-8feba39)
- [ ] The first start of a fresh install says "Qayta ishga tushdim (oldingi ish kutilmaganda uzildi)". With no
      previous heartbeat it should be a first-start line, not "cut off unexpectedly".
- [ ] The first routing ask on a cold `haiku` client timed out at `timeouts.routing_s` (3.8 s), so four queued
      "Salom" greetings got "qaysi loyiha uchun?". Warm the routing client before the first poll is processed, and a
      bare greeting should never ask for a project (T44 latency; T47 re-record).
- [ ] `setMessageReaction` failed (`provider_unavailable`, reason `other`) on 5 of the 6 queued parts. Two were
      service messages; find out why the texts failed (old message? reaction list of the group?) and log the
      Telegram description.
