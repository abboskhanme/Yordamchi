# Assumptions

Owner decisions are listed in `docs/REQUIREMENTS.md` §0 and are not repeated here. Each entry here
is a call made **for** the owner. "(brief)" means the coordinator made it in the project brief;
everything else is the analyst's. "(Qn, pending)" means it stands until the owner answers that
question. Correct any line and it changes.

## Scope and roles
- [2026-10-23] What does "switch to ChatGPT" mean for the dev bot? → The official OpenAI Codex CLI, headless, logged in with the owner's ChatGPT plan → it mirrors the Claude Code model (official binary, own subscription, session resume) (brief).
- [2026-10-23] When is Codex delivered? → Phase P4, after the Claude path is proven, and only once the same gate suite passes on it → the gate and commit policy must not differ by executor; dev bot first.
- [2026-10-23] Which rules does Codex follow? → The owner's global rules, given to Codex as its instructions file, plus the same Yordamchi authorization → Codex does not read `~/.claude/CLAUDE.md`.
- [2026-10-23] Should Claude Code's own Channels (Telegram plugin) or Remote Control be the base? → No, but they remain a manual fallback → Channels attaches to one open session with DM pairing and has no multi-project routing, burst assembly, voice or approval policy. Remote Control uses the Claude app, not Telegram.
- [2026-10-23] Should Hermes Agent (as in AIAgents) be reused? → No → its approval gates are weak, it has known silent-polling and duplicate-send bugs (AIAgents R9), and the owner fixed a Python/aiogram stack.
- [2026-10-23] Personal bot in a group? → At most one optional private group, configured at setup, under the same owner-only rules → brief says "optionally a private group".
- [2026-10-23] Does the dev bot reply with voice? → No: text only, voice only on request → dev output is code, paths and diffs. Text-plus-voice by default is a personal-bot requirement.
- [2026-10-23] Reply language → Uzbek (Latin) by default; the personal bot mirrors the language the owner writes in → global rule: client-facing output is Uzbek.

## Conversation (burst, interrupt, no buttons)
- [2026-10-23] Initial quiet windows → 1.2 s after a text that reads as complete (≥ 3 words, ends in `?`/`.`); 2 s after other text; 10 s after a bare photo/file/link; an album stays open until complete + 1.5 s; a voice note until transcribed + 2 s; "kut" holds up to 2 min; hard cap 30 s from the first part. All configurable → short enough for fast answers (D10), long enough for "image first, explanation next".
- [2026-10-23] Lone screenshot on the dev bot, nothing follows → read-only diagnosis of the inferred project, with a proposed fix, and no code change → acting without interrogating, but not changing code nobody asked to change.
- [2026-10-23] Personal bot: a message arriving before the reply is sent → merged; the reply covers all parts → this is how a person answers. After the reply it is a normal next turn in the same conversation.
- [2026-10-23] Interrupt words → "to'xta", "stop", "bas", "kut", "yo'q, boshqacha…", "hammasini to'xtat", text or voice. "kut" during assembly holds the window; during a run it stops and waits → matches how the owner speaks.
- [2026-10-23] Acknowledgement without buttons → "typing…" plus a "seen" message reaction → reactions are not buttons and do not notify.
- [2026-10-23] Slash commands → typed aliases only (`/holat`…), no BotFather command menu; natural language is primary → the command menu is a button-like UI the owner rejected (D8).
- [2026-10-23] How to keep a flat group readable → every project message starts with the project name, and each new post for a project replies to that project's previous bot message, giving visual chains without Topics → Topics rejected (D4).
- [2026-10-23] Progress style → one live progress message per project, edited at most every 5 s; the final result is a new message (it notifies) → Telegram rate limits (R-41) and less noise.

## Approvals and git (D9 interpretation)
- [2026-10-23] What is an "important decision"? → The UC-13 list: deploy or server writes (including a push that triggers a CI deploy), irreversible data loss (DB drop or truncate, destructive migration on non-test DBs, `down -v`, volume rm or prune, deleting a project folder, untracked files or anything outside the project, force push to the default branch, deleting a GitHub repo, making a repo public), spending money, sending to third parties, architecture forks including the stack choice → the owner named deploy/prod and "comparable weight"; these are the irreversible or outward-facing ones.
- [2026-10-23] Is deleting git-tracked files or ignored build artefacts important? → No, automatic → recoverable through git or a rebuild. The owner said not to be asked about routine actions.
- [2026-10-23] The owner explicitly asks to deploy: does the bot still confirm? → Yes, one confirmation saying what goes where, then it runs → coordinator AC "a deploy request produces exactly one clear question". "Always do what the owner says" is read as never refusing or ignoring an instruction.
- [2026-10-23] Binding a conversational "ha" → it approves only if it replies to the decision, names it, or is the only one pending; it is bound to the exact command and host, single-use, and expires after 2 h → without buttons, a stray "ha" must not approve a deploy (R-18).
- [2026-10-23] New GitHub repos → private, under the `gh` active account `abboskhanme` → coordinator default; it is the logged-in account.
- [2026-10-23] Secret safety with auto-push → deterministic pre-push secret scan blocks `.env` and keys and tells the owner the file → "never commit .env" must hold without a human in the loop.
- [2026-10-23] Overriding "ask before push" in `~/.claude/CLAUDE.md` → an explicit per-session owner authorization for commit, push, branch and private repo creation; the architect picks the mechanism → the owner wants no push questions, and the global file must not be edited (out of scope).
- [2026-10-23] Server rules → sessions follow `~/Documents/Projects/BONVI_SERVER.md` (loaded through the global rules), and the gate treats the Windows host `server` as jump-only → 1C there is production (brief).
- [2026-10-23] Read-only server commands without asking (Q3, pending) → yes, but only from an allowlist (uptime, disk, `docker ps`, `systemctl status`, log tails); everything else asks → fast server reports need it, and reads do not change production.

## Speed and models
- [2026-10-23] Latency targets → those in REQUIREMENTS §4.2 (ack ≤ 1 s, digest report ≤ 4 s, first Q&A text ≤ 8 s, voice ≤ 12 s after its text, investigation "Tekshiryapman" ≤ 2 s / result ≤ 3 min) → coordinator ranges; these are achievable with a warm session and the digest, and measurable as p90.
- [2026-10-23] Model tiers → a small/fast tier (Haiku class) for routing and burst decisions; a mid tier (Sonnet class, low/medium effort) for Q&A and reports; the owner's default `opus` for coding and deep investigation; changeable by chat → speed on questions, strength on code.
- [2026-10-23] Digest refresh → after every turn; on git change (≤ 5 min lag for projects active in the last 7 days, hourly for others); server checks every 30 min for owner-listed hosts (Q3) → reports must not need a coding session.
- [2026-10-23] Freshness rule → every report states its data age; server data older than 30 min is re-checked before answering → protects against stale-but-confident reports (R-45).
- [2026-10-23] Session lifetime → one long-lived session per project ("X yangi sessiya" starts fresh); the personal bot starts a new session after 6 h of inactivity, carrying a short summary ("yangi suhbat" resets) → keeps context, and stops latency and tokens growing without bound.

## Personal bot
- [2026-10-23] Video and YouTube understanding → done through Gemini as a tool; Claude writes the answer → Claude cannot watch video, and the owner allowed Gemini "where useful".
- [2026-10-23] Text and voice limits → default text ≤ 3,500 characters / 40 lines; the voice is 15–35 s of key points, never reading URLs or code; every substantive reply gets voice unless it is turned off → brief: 20–30 s voice, compressed long answers.
- [2026-10-23] Screenshots and downloads → headless browser with a clean profile (no owner cookies); files ≤ 50 MB; logged-in browsing is out of scope.
- [2026-10-23] "Send me the video from this site" (owner asked for it explicitly) → in scope via yt-dlp, YouTube included, for personal use, re-encoded to fit ≤ 50 MB → the owner requested it; the platform terms-of-service risk is his to accept, and it's noted in RISKS.
- [2026-10-23] Mixed-language voice → Gemini transcription with `uz-UZ`/`ru-RU`/`en-US` hints and a vocabulary built from the project registry, as in OvozYoz → the owner mixes in Russian and English terms; exact project names are needed for routing.
- [2026-10-23] Integration tools on the personal bot → read-only tools only (MCP `readOnlyHint` or allowed by the owner when adding) → the personal bot informs; it never acts.
- [2026-10-23] A plain API key (not MCP) as an integration → a generic read-only HTTP GET tool with base URL and auth header; MCP is first-class → covers "here is a key for site X" without per-site code.
- [2026-10-23] Fallback brain while Claude is unavailable (Q4, pending) → Gemini text, labelled "zaxira model"; other providers as a full brain are v2; keys are stored now → reliability without building a second agent runtime.
- [2026-10-23] Read-only bridge (brief) → the personal bot reads the dev digest only (read-only DB access), holds no dev token and no executor handle → web content must never reach the dev executor.

## Reliability, security, ops
- [2026-10-23] LaunchAgent or LaunchDaemon? → user LaunchAgents, as with the owner's Hermes and ClaudeManager agents → the Claude keychain login needs the user session; the cost is no service before login (R-02).
- [2026-10-23] Watchdog → a separate small process: heartbeat stale > 120 s or ≥ 3 crashes in 10 min → restart and alert, at most one alert per 30 min → covers polling that dies silently (Hermes lesson).
- [2026-10-23] Mac-attach detection → the architect verifies it, starting from `~/.claude/sessions/<pid>.json` (`sessionId`, `kind: interactive`) with a process-scan fallback; the Mac session wins → the format is internal and may change (R-11).
- [2026-10-23] Interrupted turns after a crash → never re-run automatically; the bot asks → re-running can repeat side effects.
- [2026-10-23] Deleting a pasted-key message (brief) → yes, within 5 s; the dev bot needs the delete-messages admin right; recommend rotating the key → Telegram cloud chats are not end-to-end encrypted.
- [2026-10-23] Claude tokens pasted in chat → refused, pointing to ClaudeManager → Anthropic terms forbid collecting or storing claude.ai credentials, and D7 applies.
- [2026-10-23] Master key for the secret store → a 0600 file outside the repo (STACK §2.2) → the uv Python is ad-hoc signed, so the Keychain would re-prompt after every Python update and nobody is at the Mac to click; same choice as AgentsRoom.
- [2026-10-23] Voice format (brief) → OGG/Opus voice notes; the encoder is a day-one install (no ffmpeg on the Mac) → Telegram voice bubble requirement.
- [2026-10-23] Paid API caps (initial) → Gemini $40/month, OpenRouter $10, OpenAI $10; warn at 80%, pause at 100% → ~$0.01 per 30 s TTS (AIAgents `VOICE.md`) × ~100 voice replies/day ≈ $30/month, plus STT.
- [2026-10-23] Runaway guard → 60 min wall time, 200 agent steps, or 5 identical failing tool calls per turn, then pause and ask → bounds a stuck agent without stopping normal builds.
- [2026-10-23] Concurrency (Q2, owner answered "10–15") → default cap 15 working turns, changeable by chat; the owner parks projects by chat; a memory/disk-pressure guard queues new turns instead of letting the Mac swap or crash; the Q&A path is outside the cap → the owner wants 10–15, and the guard keeps 16 GB RAM safe without a lower fixed cap.
- [2026-10-23] Project registry → auto-discover top-level folders in `~/Documents/Projects`; folders named `*Archive*` or `*-wt-*` (worktrees) are excluded from routing unless named; Yordamchi itself is registered → avoids confusing look-alike folders (R-10).
- [2026-10-23] New project naming → PascalCase Latin, like the existing folders; never overwrite → consistency with `~/Documents/Projects`.
- [2026-10-23] New project description → closes after 2 min of silence or "tamom"/"boshla"; plan summary within 15 min → descriptions come in several messages.
- [2026-10-23] Retention → audit 90 days; media cache 7 days; logs ≤ 200 MB, rotated → about 31 GB free disk.
- [2026-10-23] Gemini key tier → paid tier with a spend cap → free-tier prompts may be used for training.
- [2026-10-23] Where the Mac is (Q1, pending) → it stays plugged in and awake at home or the office; offline while travelling is accepted in v1; pinned heartbeat; off-Mac relay is v2 → avoids extra infrastructure for v1.
- [2026-10-23] Self-update (Q5, pending) → automatic after tests pass, with health check and auto-rollback; protected-core changes and safeguard-weakening settings ask once → the owner wants few questions, and the core guards everything else (brief: safeguards).
- [2026-10-23] When does self-update ship? → Phase P2 (it was P4) → D14: every later change, including the personal bot, should already be possible by chat.
- [2026-10-23] What counts as the protected core? → the self-updater, watchdog, owner allowlist, important-decision gate and secret store → a broken change there removes the safety net itself.
- [2026-10-23] Self-updater independence → a separate program with its own pinned runtime, never importing bot code, never upgraded in the same update as the bots → it must survive a broken bot update (D14).
- [2026-10-23] Messages during a self-update → polling pauses and Telegram holds updates (≤ 24 h); they are processed after the switch or rollback → nothing is written to a DB that may be rolled back, and nothing is lost.
- [2026-10-23] Settings change confirmations → every change is restated in one line and applied without a question, except safeguard-weakening ones (removing an important-decision item, disabling the secret scan, raising a cap, changing owner or group ids) → few questions, but no silent loss of a safeguard.
- [2026-10-23] Who can change settings? → only the owner's own messages; never web, file, tool or executor content → injection must not reconfigure the bots.
- [2026-10-23] "Schedules" as settings → built-in schedules (digest refresh, server checks, heartbeat, cost summary, quiet hours) are configurable; owner-defined recurring briefings are not in v1 and can be added later by self-update → keeps v1 bounded while staying extensible by chat.
- [2026-10-23] Run /plan-estimate? → skipped → personal project for the owner himself, not a client proposal; no quote needed.

## Conventions (plan-conventions)
- [2026-10-23] Where do runtime data and env files live? → `~/Library/Application Support/Yordamchi/` (DB, media cache, `dev.env`, `personal.env`), logs in `~/Library/Logs/Yordamchi/`; never in the repo → a self-update switches code without touching config, and an executor working on Yordamchi's own repo cannot read tokens (R-22).
- [2026-10-23] One `.env` or one per process? → one per process (`dev.env`, `personal.env`) → UC-36: the personal process must not hold the dev bot token.
- [2026-10-23] Supervisor (self-updater + watchdog) dependencies → stdlib only, its own uv project (not a workspace member), never changed in the same update as the app → R-32: a runtime with nothing to upgrade cannot be broken by an upgrade.
- [2026-10-23] Repository layer? → no repository classes or Protocols; per-feature `store.py` functions, tested on real SQLite → nothing has a second persistence implementation, and SQLAlchemy already covers D1 (Postgres by config).
- [2026-10-23] Static checks → ruff, mypy `--strict`, import-linter (MegaPlus Platform's contract setup) and an ast architecture test → pure-Python tooling under uv; mypy makes "only catalogued or scrubbed text reaches Telegram" (`BotText`) mechanical.
- [2026-10-23] What goes into logs? → ids, codes, counts, durations and versions only; never message text, transcripts, prompts or model output → the only way an unknown-format key stays out of logs (UC-04 AC).
- [2026-10-23] Behaviour timeouts → settings passed per call (every port coroutine takes `timeout`), not adapter constants → UC-09 lists latency-related timeouts as runtime settings.

- [2026-10-23] Run plan-ux? → skipped → no screens; the interface is conversation, specified in REQUIREMENTS and the SPEC's message design.
- [2026-10-23] Run plan-data-model separately? → no; the architect writes the data model in SPEC → small single-user SQLite schema, not data-heavy; one fewer handoff.

## Architecture (plan-architect, `docs/SPEC.md`)
- [2026-10-23] STACK C1: set `cleanupPeriodDays` to 365? → No (owner). It stays at 30 days. Yordamchi writes a handoff note (goal, decisions, state, next steps, files touched) to the digest after every finished turn and at park. A resume with a missing transcript starts a new session seeded with that note and tells the owner in one line. Yordamchi never copies transcripts and never sets `cleanupPeriodDays` → the owner's answer; his global settings stay untouched.
- [2026-10-23] Speech key → either a direct Google Gemini key or an OpenRouter key, any tier, given by chat. The bot detects the kind, validates it and configures itself.
  - STT: Google direct; else OpenRouter chat completions with Gemini and the hints in the prompt (its `/audio/transcriptions` has no language or vocabulary options).
  - TTS: Google direct; else OpenRouter `/audio/speech` `google/gemini-3.8-flash-tts` (mp3, then ffmpeg to Opus); else Edge; else text only with a notice.
  - Quota errors are always reported.

  → the owner's answer; checked against openrouter.ai docs on 2026-10-23.
- [2026-10-23] Contradiction: STACK §4.7 gives the supervisor `cryptography` "to read the report token"; CONVENTIONS §12 says `dependencies = []` → stdlib only. The supervisor reports with the dev bot token from the 0600 `dev.env` → bot tokens live in env files (CONVENTIONS §12), so no decryption is needed, and R-32 wants a runtime with nothing to upgrade.
- [2026-10-23] DB split → `dev.db` is written only by the dev bot and `personal.db` only by the personal bot; same schema, one Alembic chain. The personal bot reads `dev.db` only through the bridge (`mode=ro`, `query_only`). The dev bot reads `personal.db` read-only, for numbers and masking only → UC-36 "no write access to the dev DB" holds by construction, with one writer per file.
- [2026-10-23] Settings across the two bots → each bot stores its own. The personal bot falls back to the dev value for keys declared `BOTH`. `PERSONAL`-only keys are changed by talking to the personal bot → one writer per DB; a dev-bot change to a shared key still reaches both bots.
- [2026-10-23] Integrations in personal sessions → an in-process gateway (`yordamchi_int`) exposes only the allowed tools, with no remote MCP config. Dev sessions use native HTTP MCP with `headersHelper` → NFR-SEC-08's exact tool list stays true even when a server adds tools.
- [2026-10-23] Dev session permissions → `bypassPermissions`, a `PreToolUse` gate on every call (subagents included), and `can_use_tool` for `AskUserQuestion`. Investigation and assistant sessions use `dontAsk` → per the docs, hooks run first and deny in every mode, and `AskUserQuestion` always reaches the callback; the owner wants no routine prompts.
- [2026-10-23] Gate interpretations of the owner's UC-13 classes:
  - deploy: `gh workflow run`, `gh release create`, `docker push`, and make/npm targets named deploy, release, publish or prod;
  - deploy push: `gh pr merge` into a deploy branch;
  - sending to third parties: package publish, and mutating HTTP to an external host;
  - "test database": SQLite in a tmp or `tests/` path, or a localhost DB named `*test*`;
  - temp dirs may be deleted freely;
  - never-askable forbidden actions: Yordamchi's data dir, the master key, the keychain, `~/.claude` settings, `launchctl` on `uz.yordamchi.*`.

  → R-20: one extra question is better than a miss; the forbidden set protects the safety net itself.
- [2026-10-23] Indirect deploys and pushes (scripts, Makefiles) → PATH shims for ssh/scp/sftp/rsync, plus a git `pre-push` dispatcher set through `GIT_CONFIG_*` `core.hooksPath` in the executor environment, chaining to the project's own hooks. The deterministic secret scan runs there → it catches indirection without editing any project file.
- [2026-10-23] Self-update health check vs "no DB writes during the switch" → the new version starts in probation:
  - `getUpdates` in peek mode, never advancing the offset;
  - no schedulers and no sessions;
  - a synthetic self-test that only sends "typing".

  → it proves polling and the whole pipeline without consuming an owner message, so a rollback with a DB restore loses nothing.
- [2026-10-23] Waiting for an agent's question → `can_use_tool` holds for up to 30 min. Then the turn ends, and the answer arrives later as a message → no slot or process is held for hours, and it survives restarts.
- [2026-10-23] Redirect ("yo'q, boshqacha qil") → interrupt, then a new turn in the same session; priority `now` is not used → `now` keeps running work going in the background, and the owner wants it stopped.
- [2026-10-23] New runtime dependencies → PyYAML (deploy-branch detection; already needed by the eval `cases.yaml`) and `edge-tts` (P3 fallback TTS, the CONVENTIONS `edge_tts` adapter), both pinned in `uv.lock` → small, and each serves a requirement the stack table left uncovered.
- [2026-10-23] Small defaults → cost summary schedule off (ask "xarajat?"); OAuth redirect port 33418; a new-project name collision continues with the first free `<Name>2` and is said in the plan message → fewer questions; all of them can be changed by chat.
- [2026-10-23] Live stranger test (X02) → dropped; stranger, leave and privacy are verified offline only (recording aiogram session + fakes) → the owner has no second Telegram account.
- [2026-10-23] Live push test repo (X07) → `abboskhanme/yordamchi-smoke`, private, test commits only, created at T39 → owner approved.

## Skeleton (T01/T02, build-backend)
- [2026-10-24] Scrub (SPEC §2.5 step 2) runs before the credential refusal (step 3), but `CLAUDE_CODE_OAUTH_TOKEN` and `CLAUDE_CODE_USE_*` are scrubbed names → the refusal check reads the environment before the scrub → otherwise those refused sources would vanish unseen.
- [2026-10-24] Which credential sources refuse start → STACK §4.4 list, plus any `ANTHROPIC_*` and any `CLAUDE_CONFIG_DIR` value (STACK says "non-default"), in the environment and in `env` of settings files; an unreadable settings file refuses too → SPEC §5.3 "Never: ANTHROPIC_*, CLAUDE_CONFIG_DIR"; fail closed.
- [2026-10-24] Child PATH order: CONVENTIONS §6 says `~/.local/bin` first, SPEC §2.5 says shims first → SPEC order → the ssh/scp shims must shadow the real binaries.
- [2026-10-24] Env files → parsed by `core/config`, never exported to `os.environ`; `YORDAMCHI_*` names are stripped from every child env; unknown keys are reported, not fatal; group ids must be negative → no child inherits a token, and a rolled-back release still starts with a newer env file.
- [2026-10-24] Env templates → `config/{dev,personal}.env.example` (CONVENTIONS §1), not `ops/env/` (brief) → coordinator correction.
- [2026-10-24] Where AES-GCM runs (SPEC §4.5 puts it in the vault) → behind the `Keystore` port (`seal`/`unseal`, `cryptography` confined to `adapters/keystore`) → the master key never leaves the adapter, and the fake uses real AES-GCM.
- [2026-10-24] "BOTH via bridge" for settings → the settings feature reads its own `setting_versions` table on the read-only peer handle `deps.peer_db` (`mode=ro`, `query_only`); `shared/bridge` (P3) owns every other cross-DB read → A2 forbids another feature's store/models.
- [2026-10-24] A key whose default differs per process (`PerBot` like `voice.enabled`, `FROM_BOOTSTRAP` ids) → never shared: the personal bot never falls back to the dev row → else "voice off" said to the dev bot would silence the personal bot.
- [2026-10-24] Snapshot version for the personal bot (two DBs) → own max row id + dev max row id of shared keys → both are monotonic, so the sum detects any change.
- [2026-10-24] A stored setting row that fails today's check → skipped (next source applies) and logged `setting_row_skipped` → a tightened check in a new release never breaks startup.
- [2026-10-24] SPEC names a `Duration` check without defining it → a daily `HH:MM-HH:MM` span (may cross midnight), used by `schedule.quiet_hours` → its only fitting key.
- [2026-10-24] "`weakens=always` on add" → a sixth kind `adds_items`; also on `gate.git_hosts` and `gate.temp_roots` (SPEC silent) → every loosening of the gate asks once.
- [2026-10-24] "`turns.max_wall_min`, `max_steps`, `max_same_failure` (`increases`)" → all three weaken on increase → the parenthetical reads as covering the runaway guards.
- [2026-10-24] Groups and scopes → `Group.ACCESS` added for `access.*`; `ui.seen_reaction` in PERSONA; caps BOTH (one budget), `gate.*` DEV, `speech.video_provider` PERSONAL, `models.settings` BOTH; caps are whole-USD ints; `access.personal_group_id = 0` means none → JSON-only values, closest group.
- [2026-10-24] Which keys to seed → every §4.3 key including P2/P3 ones (in blocks after the P1 blocks) → `test_registry` covers all of them and later tasks rarely touch DEF.
- [2026-10-24] `0001_initial` → generated from the foundation tables only (`setting_versions`, `kv_state`, `health_events`, `audit_events`); JSON columns NOT NULL; post-write ruff hooks so a generated revision passes `make check` unedited → TASKS migration plan.
- [2026-10-24] `persistence/types.py` vs CONVENTIONS §9 "never a stdlib name (A005)" → keep the name fixed by CONVENTIONS §1 and SPEC §4.1, with a per-file A005 ignore.
- [2026-10-24] ruff `ASYNC109` (no `timeout` parameters) contradicts A4 → ignored globally; ruff excludes `*.md` → ruff 0.17 reformatted the snippets in CONVENTIONS (reverted).
- [2026-10-24] AST rules → A9 added (SPEC §3.1: `bots/cli/*` import only `yordamchi.core` and the stdlib); A4 exempts `Clock.sleep`; A1 and C6 also cover `settings/registry.py` and `defaults.py` (domains import them).
- [2026-10-24] `.protected-core` globs whose files do not exist yet (A8 needs a match) → kept as `# pending:` lines; a test fails once a pending glob matches, so it gets promoted.
- [2026-10-24] Ports detail → `Synthesizer.synthesize_stream()` instead of `synthesize(stream=True)`; `SharedDeps` maps provider → Transcriber/Synthesizer; `FakeGit` exists for scripted facts and failures (git semantics stay on real tmp repos); contract files are named after the Protocol (`test_agent_runtime.py`, `test_transcriber.py`) as TASKS does.
- [2026-10-24] Early files of later tasks → `shared/access/domain.py` (`OwnerOrigin` only, T14), `shared/{health,audit}/models.py` (T32, T08), settings `snapshot` + `record_version` only (T21 adds change/revert/list, weakening question, audit row) → the foundation tables and the A5 origin rule need them now.
- [2026-10-24] Data-dir variable names differ: SPEC §3.2.12 `YORDAMCHI_DATA_DIR` (app) vs §9.1 `YORDAMCHI_DATA` (supervisor plist) → the app uses `YORDAMCHI_DATA_DIR`; T23/T28 should pick one.

## Adapters (merge train)
- [2026-10-24] T09 Telegram error mapping → 429 → `RateLimited(retry_after)` (if no parameter, `retry_after` is parsed from the description); 401 → `AuthFailed`; 5xx and network errors → retryable `ProviderUnavailable`; aiogram timeout → `CallTimedOut`; 400/403/404/409 and chat-migrated → non-retryable → one vendor-neutral hierarchy (CONVENTIONS §7).
- [2026-10-24] T09 Telegram retries → a `retry_after` that fits inside the call timeout is waited out; a longer one raises at once so the outbox can park the chat. A send whose delivery is unknown is never resent by the adapter; the outbox decides, using its `dedupe_key` → NFR-REL-05/06 (no duplicates).
- [2026-10-24] T09 Telegram benign refusals → "not modified" counts as a successful edit; a delete that is not allowed returns False; leaving a chat the bot has already left counts as left; replies use `allow_sending_without_reply`; pins are silent → none of these is an owner-facing failure.
- [2026-10-24] T09 Telegram size limits → in MiB: inbound ≤ 20, outbound ≤ 50, photos ≤ 10, checked before transfer → Bot API limits, NFR-CAP-04.
- [2026-10-24] T09 token safety → vendor exceptions are never chained, and descriptions are never logged → aiohttp errors carry the token in the URL (STACK §4.3).

## Spikes S1–S6 (2026-10-24; evidence in docs/SPIKES.md)
- [2026-10-24] S1 follow-up merge → send `priority: "next"` explicitly (it merges into the running turn and gives one result); redirect = `interrupt()` + a new turn; `"now"` is never used → `"now"` backgrounds the running Bash and later starts a turn on its own.
- [2026-10-24] S2 interrupt → `interrupt()` kills the Bash child in ≤ 0.06 s (SIGKILL escalation at ~1.5 s); the backstop process-group kill runs at ~2 s and targets the tool's group, never the CLI's → the CLI shares the bot's process group.
- [2026-10-24] S3 hooks under `bypassPermissions` → a hook deny holds; `PreToolUse` fires in subagents; `AskUserQuestion` reaches `can_use_tool` → T11 as designed; subagents ask through gate-deny + message.
- [2026-10-24] S4 persona on resumed sessions → SPEC §5.4 revised: the frozen prompt holds only fixed parts; owner-editable instructions go into `additionalContext` on every turn; no rotation → 10/10 compliance vs 3/6 with the old design.
- [2026-10-24] S5 `--restricted` → used for the personal session; the C2 tool check is `allowlist ⊆ init ⊆ allowlist ∪ harness` → strict equality would refuse a correct session.
- [2026-10-24] S6 Mac attach → SDK sessions have `entrypoint: "sdk-py"` in `~/.claude/sessions/*.json`, the owner's have `"cli"`; T54 uses `entrypoint` + pid ancestry; the process scan stays primary.
- [2026-10-24] Child env scrub → also `CLAUDE_PID` and `CLAUDE_EFFORT` (core/proc.py) → both leak from a parent Claude session into the child CLI.
- [2026-10-24] Dev sessions disallow `SendMessage`/`ListAgents` → every SDK session opens a cross-session socket; peer input is untrusted.
- [2026-10-24] T13 Gemini SDK retries → disabled on the client (`interactions.sdk_configuration.retry_config = None`), so `core.retry` is the only retry layer; this touches a private SDK object, so re-check it on SDK upgrades (STACK §8) → otherwise one call would become up to 12 requests.
- [2026-10-24] T13 STT instruction text → a fixed technical instruction kept in `adapters/speech_common.py` and not owner-editable; owner-editable STT behaviour is languages, vocabulary, mode and model (all settings) → C3 forbids adapters from importing prompts, and a transcription instruction is not persona.
- [2026-10-24] T13 OpenRouter audio → OGG is converted by ffmpeg to 16 kHz mono WAV; on 400/404/415/422 it falls back to `/audio/transcriptions` without hints; 401/402/403/429 never fall back → keeps hints whenever possible and never masks auth or quota errors.
- [2026-10-24] T13 quota vs rate limit → a daily or zero quota → `QuotaExhausted` (tell the owner); a transient 429 → `RateLimited(retry_after)` → UC-05 F: report quota problems plainly, never drop the voice note silently.
- [2026-10-24] T13 key-kind check → `validate_key` refuses a key that is not wholly that vendor's format, before any request → one vendor's secret never reaches the other.
- [2026-10-24] TTS provider (owner decision) → OpenRouter with Gemini TTS models is primary; Google direct is used only if a key is present; then Edge; then text only, each fallback with a one-line notice → the owner said TTS goes through OpenRouter; the fallbacks keep voice working when OpenRouter fails (NFR-REL).

## Foundations (T08) and supervisor (T23), merged 2026-10-24
- [2026-10-24] Retention periods → `Final` constants in each feature's domain, not settings; every feature has `service.apply_retention(deps)`, which deletes in batches of 500 and never touches non-terminal rows → a chat message must not be able to shorten the audit trail (NFR-SEC-10); short transactions keep the single SQLite writer free.
- [2026-10-24] Acks before the outbox exists (T08) → direct Messenger calls after commit, delete before reply, errors logged, not raised; T15 turns them into priority-0 outbox rows → the update is already durable.
- [2026-10-24] `accept_update(deps, origin: OwnerOrigin, update)` → an origin that doesn't match the update raises before anything is written → only verified owner updates can put secrets in the vault.
- [2026-10-24] Master key missing or insecure when a key arrives → the update is stored `failed` as `[kalit saqlanmadi]`, the message is still deleted, the owner is told; a key file readable by group or others is refused (`VaultKeyInsecure`) → the key must leave the chat even when it can't be stored; NFR-SEC-02 needs 0600.
- [2026-10-24] Master key loading → lazy, cached, retried on the next call after a failed read; the adapter never creates a key (the installer does) → a missing key degrades the vault instead of stopping the bot.
- [2026-10-24] Secret naming → `<kind>`, `<kind>_2`, …; the same value returns the existing secret; intake never replaces or removes → the consumer decides what a newer key means.
- [2026-10-24] Vault AAD includes the row id → insert with NULL ciphertext, flush, seal, update, all in one transaction → SPEC §4.5.
- [2026-10-24] Masking set only grows during a run; `secret-hashes.json` is written by the dev bot only (sha256 of every value still holding ciphertext, from both DBs, 0600, atomic) → one writer; retired keys must still be blocked from pushes.
- [2026-10-24] Cost rows → `is_paid_api` only for google, openrouter and openai; an `anthropic_plan` row may carry the API-equivalent estimate as unpaid → UC-07 shows the estimate separately from paid spend.
- [2026-10-24] Supervisor "installed" → the job is loaded (`launchctl print` exits 0); an unloaded plist is a deliberate bootout and is left alone → no fighting the owner.
- [2026-10-24] Watchdog timing → heartbeat age counts from the latest of file mtime, first sight of the pid, the last kickstart, and a wake (tick gap > 60 s); 30 s kickstart cooldown; crash-loop count = max(new `started_at` values, launchd `runs` increments) in 10 min → no false restarts after sleep; it also catches bots that die before their first heartbeat.
- [2026-10-24] Update suppression → from `request.json` until a terminal status, but ignored after 2 h untouched; restart history reset after an update → the watchdog can't stay muted forever, and an update's own restarts don't count as a crash loop.
- [2026-10-24] Supervisor alerts → `[Supervisor HH:MM]` plain text to the dev group; refused if `dev.env` is not 0600; retried on network/429/5xx (queue of 20); the token is scrubbed by pattern and by exact value → NFR-SEC-03.
- [2026-10-24] Backups → daily from 04:00 Tashkent with catch-up after sleep; `.tmp` is kept only if `integrity_check` is ok; prune only after a good day; pre-update backups kept ×5; low disk skips and alerts → a bad copy never replaces a good one.
- [2026-10-24] launchd log trim → keep a 1 MB tail in `<log>.1`; the supervisor's own log rotates at 10 MB × 2 → total ≤ 200 MB (NFR-CAP-05).
- [2026-10-24] Supervisor extras → `run/supervisor.heartbeat.json` (needed by §3.2.12 self-switch), `run/supervisor.lock` single instance (second copy exits 75), a failing job alerts once per 6 h; test doubles live in `yordamchi_supervisor/testing.py` → mypy resolves root `tests.*` to the app.
- [2026-10-24] Memory pressure source (T22) → only `sysctl kern.memorystatus_vm_pressure_level`; `vm_stat`/`memory_pressure` free % is not used, because it does not predict the level (47 % free yet level 2 here); unreadable → non-retryable error → the scheduler needs the kernel's own verdict.
- [2026-10-24] Scheduler pressure threshold → start turns at level ≤ `guard.max_pressure_level` (default 2); level 4 always queues → this Mac sits at level 2 under normal load; `== 1` would block every coding turn.
- [2026-10-24] Free disk → `statvfs` (what `df` shows as Avail), nearest existing parent for a missing path, APFS purgeable not counted → conservative.
- [2026-10-24] Mac-attach scan (S6) → `ps` is primary (`TZ=UTC LC_ALL=C`, so `lstart` equals `procStart`); a registry record counts only if its process is alive; excluded as ours: `entrypoint == "sdk-py"` and descendants of the bot pid; fallback: a `--resume`/`--session-id` UUID on the command line; when in doubt the Mac session wins; VS Code counts as the owner's → never write into a session the owner might be using.
- [2026-10-24] ClaudeManager read → only email, status, 5h/7d utilization, reset times, auto-switch, active id; never `oauthAccount`, fingerprint or events; a missing or broken file → None → `holat?` needs only these, and nothing secret is read.
- [2026-10-24] SSH hardening → `-T`, `BatchMode=yes`, no forwarding, `PermitLocalCommand=no`, `RemoteCommand=none`, `ControlMaster=no`, `ConnectTimeout` 1–10 s, `--` before the alias; only plain aliases accepted; the child env is an allowlist; the shims dir must not be on its PATH → read-only checks must not open side channels.
- [2026-10-24] SSH errors → only exit 255 is a transport error; other exit codes are the command's result; "could not resolve hostname" is split with `ssh -G` into an unknown alias (not retryable) vs DNS down (retryable); retries are safe because commands are read-only → accurate owner messages.
- [2026-10-24] Turn completion (T10) → the turn ends when the CLI reports `session_state_changed` idle (`CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS=1`, always set); fallback: hold the result while background tasks or our queued inputs remain; held results fold into one `TurnResult` (cost = per-turn delta in micro-USD) → background agents and shells otherwise produce several results per turn (S1/S2).
- [2026-10-24] Interrupt sequence (T10) → guard deny-all → `interrupt` with `cancel_queued` (private API, falls back to public `interrupt()`) → `stop_task` for every background task → wait → backstop kills descendant tool groups (TERM, 0.5 s, KILL), never the bot's own group or pid → "to'xta" stops background work too, and the CLI keeps sharing the bot's group so launchd cleans up on a crash (NFR-REL-07).
- [2026-10-24] Late CLI-initiated turns → delivered as `TurnResult(background=True)`; empty `aborted_tools` results are never shown as answers → no phantom or duplicate replies.
- [2026-10-24] Guard timing (T11) → our guard timeout is `gate.hook_timeout_s`, and the CLI hook timeout is 5 s longer; any exception or timeout → deny → a PreToolUse that the CLI itself times out does not block, so ours must decide first.
- [2026-10-24] Questions in sessions (T11) → main-thread `AskUserQuestion` → `QuestionAsked`, held up to `questions.hold_process_min`, then denied with the parked message; a subagent question is denied with `subagent_question` and goes through gate-deny + message → S3.
- [2026-10-24] Executor options (T10) → `ENABLE_CLAUDEAI_MCP_SERVERS=false` and the state-events variable override `spec.env`; `bare`/`dangerously-skip-permissions` in `extra_args` raise `ConfigInvalid`; HTTP MCP servers carry only `url` + `headersHelper` → D11, R-22, STACK §4.
- [2026-10-24] CLI auto-update → the CLI moved 2.1.309 → 2.1.310 during T10; fixtures are recorded on 2.1.310; `cli_path` is the `~/.local/bin/claude` symlink, so updates apply on the next spawn, and the CLI-version smoke test (T32) guards this → SPEC §5.3.
- [2026-10-24] Burst window signature (T16) → `window(parts, settings) -> Window(closes_at, reason, hold)`; all times come from the parts themselves, and joining depends on the arrival time (≤ `closes_at`) → a restart or a late timer produces exactly the live result (SPEC §4.4 is updated in spirit).
- [2026-10-24] Burst holds → a voice note awaiting its transcript holds the burst for up to `hold_max_s` from its arrival; time held by "kut" doesn't count toward the cap → the voice note and the owner's "wait" are never cut off.
- [2026-10-24] Extra wait cues → a text ending in ":" or "…", or a caption that is only links, waits like media; an album is judged as one unit from its last item → these are the owner's "explanation follows" signals.
- [2026-10-24] Interrupt words → only as a whole phrase, the first clause, or the "yo'q, boshqacha…" prefix; a caption never counts; a voice note counts only if its transcript is ≤ 4 words; "serverni to'xtat" is an instruction; "kut" with nothing assembling counts as an interrupt; an interrupt never drops a voice note awaiting its transcript → avoids false stops.
- [2026-10-24] Follow-up merge (dev) → never merge an interrupt or a follow-up routed to another project; always merge a continuation (a reply to the turn, a late album item, or a part started within `after_text_s` of the turn's last part; a voice note's start = arrival − duration − 1 s); otherwise merge only if routed to the same project with ≥ `merge_min_confidence`. Personal bot: merge only before the reply is sent → UC-02.
- [2026-10-24] `inbound_updates.burst_id` FK → `ON DELETE SET NULL`, indexed → burst retention would otherwise scan or fail.

## Gate domain (T07, merged 2026-10-24)
- [2026-10-24] Shell parsing → a small POSIX scanner, not `shlex` → quoting, nested `$(…)` and heredocs decide data vs code.
- [2026-10-24] Heredocs → into `cat` they are data; into a shell they are parsed and keyword-scanned; into an interpreter they are keyword-scanned → keeps the heredoc commit idiom free.
- [2026-10-24] A program name known only at run time (`$CMD`, `$(…)`, piped code) → ask (`opaque_command`) → fail closed.
- [2026-10-24] Make/npm targets → recipes are judged (incl. `pre`/`post` scripts and install hooks); `$(RUN) pytest` is judged with the variable name; a missing recipe → ask (`recipe_unknown`) → a deploy can't hide behind a script name, and the owner's Makefiles stay free.
- [2026-10-24] Grant key → sha256 of tool + input without description/timeout/background → the agent rewrites the description on retry.
- [2026-10-24] One question per call → the strongest class asks (repo_delete > data_loss_db > docker_destroy > fs_destroy > force_push > deploy > deploy_push > server_write > repo_public > spend > third_party_send); all findings are audited.
- [2026-10-24] Tag pushes and `gh pr merge` → `deploy_push` whenever the project has any deploy branch → the target isn't in the command.
- [2026-10-24] Forbidden (never askable) → DATA, `~/.config/yordamchi`, Claude credentials/settings/CLAUDE.md (incl. recursive readers started above them), keychain reads, killing or launchctl on the bots, the helper or socket, `git push --no-verify`, any hooksPath or `GIT_CONFIG_*` override, `env -i`, any `ANTHROPIC_*`/`CLAUDE_*` assignment, `rm -rf /` or `~` → these would disable the safety net itself.
- [2026-10-24] Approval text → "ha" followed by substantive words ("ha lekin staging'ga") counts as a decline plus an instruction; naming a project with no pending decision approves nothing → R-18.
- [2026-10-24] Shim grants → a consumed grant stores the approved call's ssh/scp/rsync argv (recipe parts as wildcards); the shim allows only a match from the same session within the window → an approved deploy script can run its own ssh lines.
- [2026-10-24] Unknown non-MCP tools → allowed in FULL after path checks, denied in READ_ONLY/PLAN_ONLY → new Claude Code tools must not break coding sessions.
- [2026-10-24] `git reset --hard` / `checkout -- .` → free only when the tree held no uncommitted changes from before the turn; otherwise `fs_destroy` (T18 adds the fact) → the owner's own edits at the Mac are irreversible data; the agent's own changes are routine.

## Projects (T17, merged 2026-10-24)
- [2026-10-24] Archive and `*-wt-*` folders → registered but flagged non-routable (exact name only); files, hidden entries and symlinks are skipped → look-alikes (HRMentor vs HRMentor-wt-agent-api) never win a content route.
- [2026-10-24] Aliases, archived flag, description and executor → live in the `projects.*` settings and are applied on read; the row mirrors them → a chat change applies at once (UC-18).
- [2026-10-24] Deploy branches → workflows from the working tree plus the pushed commit; an unreadable workflow → every branch and tag deploys; only `github.ref == 'refs/heads/…'`-style `if:` narrows the set → it can only over-ask, never miss (UC-13).
- [2026-10-24] Deploy-named workflows → count all their jobs only when no job carries its own deploy signal → otherwise "tests on every branch, deploy on main" would make every push ask.
- [2026-10-24] Deploy cache key → HEAD + workflow names/sizes/mtimes + rules version; the gate always uses the fresh check → stale caches can't hide a deploy.
- [2026-10-24] Platform Git integrations (Vercel, Netlify, Render, Railway, App Engine) → the default branch counts as deploying when their config file exists (T17b) → they deploy without any workflow file.
- [2026-10-24] Git adapter → `find_commit` (not `commit`, for the A-rule grep); `remote_url` strips credentials; `GitFailed(reason)` with only `locked` retryable → safe and explicit.
- [2026-10-24] Assistant structured output (T12) → `output_schema` maps to the CLI's `output_format` json_schema; the only allowed tool is `StructuredOutput`; a bad shape raises, `structured` is never None → routing gets typed JSON or a clear error.
- [2026-10-24] Assistant rotation → "soft" reasons (20 asks, context > 50k tokens, effort change) keep the warm client answering until the spare is ready; "hard" reasons (higher settings version, changed prompt or schema, broken client) retire it before the ask; a never-asked spare counts as fresh → asks stay warm (live: routing median 1.36 s, report 1.07 s).
- [2026-10-24] Assistant concurrency and retries → serialised per kind (the wait counts against the timeout); one retry on the spare only when the client died before streaming → no side effects, no tools.
- [2026-10-24] Warm-process budget → routing live + spare, report live only, settings cold (setting `assistant.warm_clients`) → 6 idle `claude` processes would cost ~1–2.5 GB on a Mac already at pressure level 2.

## Access edge (T14, merged 2026-10-24)
- [2026-10-24] Rule order → leave any foreign group, supergroup or channel (whoever wrote, the owner too); then drop non-owners, kinds the bots never act on, and the owner outside this bot's chats; admit only message, edit or membership updates from the owner in an own chat → SPEC §3.2.1, D12.
- [2026-10-24] Callbacks and inline queries → always dropped, the owner's too → there are no buttons (D8).
- [2026-10-24] Policy read per update from the settings snapshot; strangers cause no DB writes (log ids only); a leave writes one `access_dropped` audit row → changes apply at once, and strangers cannot fill the DB.
- [2026-10-24] Personal bot chats → the owner's DM plus `access.personal_group_id` when non-zero → SPEC §2.2.
- [2026-10-24] Polling constants (25 s long poll, 1→30 s backoff with jitter [0.8, 1], 429 waits ≥ `retry_after`) are code, not settings; after a failure, short `timeout=0` probes with a 10 s budget → the watchdog depends on them; resume ≤ 40 s even with hung connections (NFR-REL-03).
- [2026-10-24] `guarded()` → never raises (re-raises cancellation); the owner notice has a 5 s bound and goes to the bot's home chat if no chat is known; incident id = 8 hex chars → one bad update or job can't stop the bot.

## Outbox (T15, merged 2026-10-24)
- [2026-10-24] Ordering → messages and deletes go strictly in (priority, id) order per chat, and an undue or in-flight row holds back later ones; edits, pins, reactions and typing don't queue → replies never overtake each other.
- [2026-10-24] Rate limits → 1/s per chat, 20/min per group, 30/s overall; edits count, deletes, reactions and typing don't; `retry_after` parks the chat and moves its pending rows; a 429 costs no attempt → NFR-CAP-02/03.
- [2026-10-24] Unknown delivery → resend under the same row, except `echo`, `heartbeat` and `voice` (a duplicate costs more than a loss); at restart every `sending` row is resent (SPEC §2.7) → NFR-REL-05/06.
- [2026-10-24] Attempt caps → messages 30 (~25 min awake, so a 10-min network drop loses nothing), edits and pins 5, delete/react/typing 3.
- [2026-10-24] Key-message delete → retried 3× before the "kalitni almashtiring" advice; the masked reply waits for the delete → UC-04.
- [2026-10-24] Formatting → plain text unless it contains ``` fences (then escaped HTML with `<pre>`); captions always plain; lengths in UTF-16 units; more than `max_parts` parts → one `.md` document (scrubbed, 0600, deleted after the send) → Telegram limits.
- [2026-10-24] Keyboard guard → each op has a fixed list of payload keys, checked at enqueue and before the call; a stored row carrying `reply_markup` fails `payload_invalid` → D8 enforced at the last line.
- [2026-10-24] Progress → one post per turn, then edits at most every `outbox.progress_edit_s`; older pending edits are superseded → UC-11.

## Gate service (T18), voice (T19), installer (T28), merged 2026-10-24
- [2026-10-24] Gate facts → `remote_urls` (credential-free URL per remote; the default push remote comes from `status.upstream`, else `origin`) and `dirty_before_turn`; new reasons `DISCARD_CHANGES` (fs_destroy, asked only when dirty or unknown) and `PUSH_TO_SERVER` (server_write) → T07 follow-ups.
- [2026-10-24] Gate texts → deny text to the agent in English (`prompts/dev/gate_*.md`); owner questions in Uzbek (`S.DECISION_QUESTIONS[class]`: project, #id, command, where, consequence) → CONVENTIONS §3.
- [2026-10-24] Audit → routine allows without findings aren't audited; denies, allows with findings, questions, answers, grants and expiries are; the owner's instruction text is never stored → useful trail, no content.
- [2026-10-24] pre-push hook → blocks when the hash file is missing or corrupt, or when a finding is reported (even with the bot down); a clean push passes with the bot down; it scans every pushed commit (`--no-renames --diff-filter=d`), so a secret added and later deleted is still caught; the agent can't disable it via env → NFR-SEC-02/10.
- [2026-10-24] `ToolCall` carries the hook's `cwd` → relative paths after `cd` are judged correctly.
- [2026-10-24] The gate's fact reader never opens files in DATA, `~/.config/yordamchi` or `~/.claude`, nor non-regular files (FIFOs) → no side-channel reads.
- [2026-10-24] Project hooks under the bot's `core.hooksPath` → pass-through wrappers (T18b) → otherwise husky/pre-commit hooks would silently not run in bot sessions.
- [2026-10-24] Voice provider fallback → also on `rate_limited` and `not_installed`; not on `call_timed_out` (the 5 s budget is spent) → UC-05.
- [2026-10-24] Voice keys → the newest active vault secret of each kind is used; a key that validates retires older keys of its kind; a rejected key is removed; quota or no-answer outcomes keep every key → the owner never has to clean up keys.
- [2026-10-24] Transcripts → keyed `UNIQUE(bot, update_id)`; a `done` row is reused after a restart (no second paid call); an empty transcript counts as failure (`stt_no_speech`) and is still metered.
- [2026-10-24] Vocabulary → owner terms newest first, then project names and aliases, case-insensitive dedupe, ≤ 1,000 terms.
- [2026-10-24] Installer → release = commit date + sha7 (idempotent); a real install needs the typed word `install` or `--yes`; `--home` works only with `--dry-run`; tokens are read hidden, ids visibly; DBs are backed up before migrating; `current` switches only after the migrations pass; on failure the old release comes back; uv `--link-mode clone` → safe, repeatable installs.
- [2026-10-24] Personal plist → staged in `DATA/launchd/`, not `~/Library/LaunchAgents`, until P3 → anything in LaunchAgents loads at login.
- [2026-10-24] Supervisor `ProcessType` = Standard (SPEC §9.1); the bots use Interactive → the supervisor needs no foreground priority.
- [2026-10-24] Check script → red on: `can_join_groups` true, a non-empty command menu, AC sleep ≠ 0, the owner posting as an anonymous admin; free disk and FileVault are info only.

## Routing (T20) and evals (T27), merged 2026-10-24
- [2026-10-24] Strong name match (exact or alias with an Uzbek suffix = 1.0; one typo in words ≥ 6 letters = 0.9) → route with no question; short names ("AI", "BS") count only with a suffix; split names ("Bonvi Call") only after a family word → UC-10.
- [2026-10-24] Look-alikes ask → a digit is never a typo; a typo that fits two names, or that is the cut-off start of another, asks; archived/`*-wt-*` projects never come from the LLM or from context, and a near miss asks naming both → never send to the wrong project silently.
- [2026-10-24] Two projects named, or a reply that disagrees with a name → the LLM picks among exactly those, else ask → "HRMentor'dagi kodni BonviHR'ga ko'chir" must not need a question (a deliberate deviation from SPEC's literal "two matches → ask").
- [2026-10-24] The LLM is asked on every burst for intent and `needs_fresh`, but never overrules a named project → intent drives report vs instruct.
- [2026-10-24] LLM down → the single active project takes the message (confidence 0.8), else ask; a lone screenshot goes to the active project as `investigate` → UC-02, no interrogation.
- [2026-10-24] Clarification → binds to the replied question, else the newest live one; "ha" counts only for a single candidate; it expires after 30 min with a notice → no silent drops.
- [2026-10-24] Wrong target → a short message naming one project plus a marker ("edi", "xato") corrects the latest work within 2 h → UC-10.
- [2026-10-24] Platform deploy files (T17b) → any of 7 files in the working tree makes the default branch deploy; an unknown default branch makes every branch deploy; `DEPLOY_RULES_VERSION` = 2 forces a recheck → fail closed.
- [2026-10-24] Secrets eval → `sk-ant-api` keys are stored (only `sk-ant-oat` is refused, SPEC §4.5); unknown-format keys may stay in the inbox payload but never reach a reply or a log (UC-04 wording); the leak search counts any 16-char fragment.
- [2026-10-24] Redact fix → `generic_secret` stops at `&`, `#` and brackets; neither it nor `telegram_bot_url` re-matches a value that is already masked → T27-B1/B2.

## Project hooks (T18b) and settings (T21), merged 2026-10-24
- [2026-10-24] Project hooks in bot sessions → every git hook goes through `githook.py`, which `exec`s the project's own hook (repo-local `core.hooksPath`, e.g. husky, else `.git/hooks`), with the same argv, stdin and exit code; a hooks dir outside the repo or the git common dir is never followed; an empty or `/dev/null` hooksPath means no hooks → husky and pre-commit keep working, and nothing escapes the repo.
- [2026-10-24] pre-push → the secret scan runs first and cannot be skipped; the project's pre-push runs only after a clean scan → NFR-SEC-10.
- [2026-10-24] Stash safety → `stash drop|clear` asks when the tree was dirty before the turn or when stash entries older than the turn start exist (whole-second precision, so it may over-ask once, never miss) → the owner's stashes are his data.
- [2026-10-24] Fetching over ssh → `git-upload-pack <path>` is a built-in read-only pattern; a custom `--upload-pack` and jump-only hosts still ask → it's git's read protocol, not owner policy.
- [2026-10-24] Settings weakening → the asker is injected; the dev bot uses a gate `settings_weaken` decision (a small protected-core addition in `dev/gate/service.py`, keyed on that kind and tested) → `shared` can't import `dev`.
- [2026-10-24] A settings request is all-or-nothing: if any part weakens a safeguard, the whole request waits for one "ha"; a revert follows the same rule; repeating the request reuses the open decision → one question at most.
- [2026-10-24] Settings list → a grouped summary plus the full list as a `.md` file; values are masked first, then truncated (found and fixed a prefix leak); English doc strings are hidden from the owner (NFR-LANG-01).
- [2026-10-24] NL settings interpretation → an unknown key from the model counts as "unclear" and changes nothing; the clarifying question comes from a fixed template, not from model text → injection-safe (NFR-SEC-09).

## Gate eval (T25) and installer pins, merged 2026-10-24
- [2026-10-24] Gate eval temp roots → the eval runs with `gate.temp_roots = ["/tmp", "/private/tmp"]`, stricter than the default (pytest tmp dirs under `/private/var/folders` would otherwise never ask).
- [2026-10-24] Recipes nested past level 2 (`npm run a` → `b` → `c`) → ask `recipe_unknown`; shell scripts are judged in the caller's cwd; npm uses the nearest `package.json` upward → T25 bugs 1–3.
- [2026-10-24] Shell functions and aliases → judged where they are defined; `npx <pkg>` maps to the package's binary; `script -q /dev/null <cmd>` is a wrapper; `gh api …/dispatches` asks `deploy` → T25 bugs 4–6, 8.
- [2026-10-24] Git and ssh settings whose value is a command (`-c alias.x='!…'`, `core.sshCommand`, `GIT_SSH_COMMAND`, `ProxyCommand`, `LocalCommand`) → judged as local commands, so reading credentials through them is forbidden, not askable → T25 bugs 7, 9.
- [2026-10-24] Side effect → in investigation sessions, `git -c core.pager=less log` is denied unless the pager is on the read-only program list → acceptable; investigations should not page.
- [2026-10-24] ffmpeg → 9.0.2 arm64 static builds from ffmpeg.martin-riedl.de; SHA-256 taken from the publisher's `.sha256` files and re-verified locally; the binaries are Developer-ID signed and include libopus → STACK §5.
- [2026-10-24] `python -m yordamchi.check` → a two-line shim module; it is also reachable as `-m yordamchi.bots.cli.check`.

## Turns (T24) and gate fixes, merged 2026-10-24
- [2026-10-24] `guard.max_pressure_level` → int, default 2, Range(1, 3), weakens on increase; level 4 always queues; an unreadable pressure level queues, while an unreadable disk or session list doesn't block; pressure applies to every kind, disk and the cap only to coding turns → SPEC §5.6 revised.
- [2026-10-24] Wrong-target revert → stored on `turns.revert_status`, not in gate decisions; the order is checkout tracked changes → delete files created in the turn → `git revert` the turn's commits; files the owner changed before the turn are never touched → UC-10, and protected core stays unchanged.
- [2026-10-24] Resume → before each resume, check the transcript file exists (by name only); if it's gone, open a new session seeded with the last handoff note and tell the owner (`S.SESSION_RENEWED`) → C1 fallback, `cleanupPeriodDays` untouched.
- [2026-10-24] Recovery → a `starting` turn with nothing delivered goes back to queued; undelivered follow-ups of an interrupted turn move to a new queued turn; a send that raised is marked failed + `S.DELIVERY_UNCERTAIN` and never resent → NFR-REL-05/07.
- [2026-10-24] Stop semantics → "to'xta" drops the stopped turn's undelivered follow-ups; a runaway pause keeps them; queued or waiting turns are cancelled; at shutdown, working turns become interrupted with "davom ettiraymi?" → UC-03.
- [2026-10-24] Fixed SPEC timings are domain constants for now: interrupt 2 s, usage-limit retry 10 min (40 s after an account switch), investigation 60 steps / 3 min, smoke 5 min on haiku, scheduler tick 15 s.
- [2026-10-24] PEM block inside a source file → the pre-push hook also scans each file's joined added lines for multi-line secrets, reporting the line where the block starts → T25 bug.
- [2026-10-24] Guard fall-through cwd → `can_use_tool` uses the cwd PreToolUse recorded for that tool-use id, else the session's last known cwd (256 ids kept) → both entry points give the same verdict.

## Routing eval round 2 (T26), merged 2026-10-24
- [2026-10-24] UC-10 met on 170 live haiku answers: clear 97.4 %, context 96.2 % (0 wrong), ambiguous 96.6 %, silent wrong 1 → the margins sit on the thresholds, so a re-record can turn the suite red with no code change (RISKS R-10). Owner-written descriptions ("X tavsifi: …") are the cheapest way to widen the margin.
- [2026-10-24] Project descriptions → metadata lines skipped; source order README → CLAUDE.md → SPEC goal → PLAN.md; ≤ 300 chars, topped up when < 80 chars → the model needs real descriptions.
- [2026-10-24] Name rules → an ordinary-word folder name (Yordamchi, Wooden) counts only when capitalized, suffixed, or before "loyiha"; aliases of ≤ 3 letters need a suffix; of several named projects, the only one carrying a target suffix ('da/'ga/'ni…) is the target ('dagi/'dan mark the source); names joined by "va" share a suffix; "arxiv"/"архив" count as archive → "yordamchi skript yoz" no longer goes to Yordamchi.
- [2026-10-24] Context agreement → when the model's first choice (even below threshold) is the one active project, the follow-up routes as context (0.8) → follow-ups no longer ask every time.
- [2026-10-24] `timeouts.routing_s` = 3.8 s (was 2.5; that cut 16 % of asks); routing model stays haiku, effort low (p90 2.8 s) → NFR-SPD routing ≤ 4 s.
- [2026-10-24] `asks: true` grading → for messages that point at no specific candidate set, any question naming ≥ 2 candidates passes; context "wrong" means a wrong project → fair grading on the real registry.
- [2026-10-24] Heartbeat cadence (T32) → in normal mode only a successful `getUpdates` refreshes the file (the idle long poll does so every 25 s); the 30 s timer runs only when paused, in probation, or in the first 60 s → if the timer also refreshed it, a dead poller would be noticed after ~195 s instead of 120 s.
- [2026-10-24] Smoke project → hidden id `.smoke` (the scan skips dot entries, so it can't collide with a folder called `smoke`); its turns post nothing; it passes only when the turn is done, HEAD moved, and `ok-<stamp>.txt` contains "ok"; the first CLI version seen is the baseline → the CLI auto-update check never bothers the owner unless it fails.
- [2026-10-24] Disk warning → repeats every 6 h (SPEC §9.2) and at once on low → blocked; the "Onlayn" pin counts as gone when its edit fails at the first attempt, while an edit that fails after retries is treated as a network drop.
- [2026-10-24] `holat?` → reads its parts concurrently with 1.5 s each; an unreadable part shows "o'qib bo'lmadi" → ≤ 3 s even when a source hangs.

## Digest (T30) and questions (T29), merged 2026-10-24
- [2026-10-24] Report path → a plain status question gets the template at once; a free-form one goes to the warm `report` assistant within `min(timeouts.report_s, budget left)` (budget 3.5 s), with the template as fallback; the live "hozir: «title», ishlayapti, N daq" line always comes first; reports never wait for an ongoing refresh (they check `locked()`) → D10, UC-15 ≤ 4 s.
- [2026-10-24] Stale digest → refresh git within 1 s, and the freshness line says so ("eskirgan edi … hozir yangiladim" / "yangilab bo'lmadi"); a cause/server/log/failure question ("nega", "sabab", "server", "log", "yiqilyap…", "почему") → "Tekshiryapman" plus an investigation turn, written atomically → the fast path stays fast; deep questions get a real investigation.
- [2026-10-24] Handoff synthesis → background only, inside the per-project lock, sharing the report profile (no rotation); if the assistant fails, a deterministic note is stored instead → the C1 fallback always has a note.
- [2026-10-24] `dev/turns` read API → `list_active_turns`, `list_recent_turns`, `turn_statuses` (additive) → A2 forbids reading `turns.store` from other features.
- [2026-10-24] Agent question format → "BonviHR · Savol: …", then numbered options "1. label — description", then a hint on how to answer; no `reply_markup` → UC-12, D8.
- [2026-10-24] Answer binding order → the replied-to question → the newest question of a named project → the project whose bot message was replied to → the only open item (when no gate decision is pending) → a number or option text that fits one project; a bare "2" with questions in two projects asks one which-project question → never guess.
- [2026-10-24] Answer parsing → "2", "2-chi", "ikkinchisi", "oxirgisi", "#2", exact or loose option text ("postgres" → PostgreSQL; "postgres emas" → none), multi-select "1, 3"; an out-of-range number re-asks; anything else goes to the agent verbatim; `answer()` False → the answer is delivered as a message → UC-12.
- [2026-10-24] A question whose turn ended → cancelled on the next read; a parked question stays open → no stale answers.
- [2026-10-24] Speed-up (owner asked for more parallel agents) → P2/P3 features are built in parallel with the P1 finish (up to ~8 agents); their tables go into `0001` while it is not frozen (nothing deployed until T43); P2/P3 wiring (T58, T77) stays sequential after T34 → the owner's wall-clock priority; expand-only migrations from T43 on.
- [2026-10-24] P2/P3 dependencies added once by the merge train: mcp 2.3.0, yt-dlp[default] 2026.10.21 + yt-dlp-ejs 0.8.0, playwright 1.64.1, edge-tts 7.3.1 (STACK pins; C5 confinement already exists) → feature agents never edit pyproject/uv.lock in parallel.

## Servers (T31) and offline handling (T32b), merged 2026-10-24
- [2026-10-24] Server probes → every command is judged by `gate.shim_verdict` with the READ_ONLY policy and the gate's own list, so there is no second list; the jump-only host `server` is never a target; stderr is never read (Bonvi VM banner) → a single source of truth; BONVI_SERVER.md rules.
- [2026-10-24] Server alerts → only when an issue first appears, no recovery messages; an issue that clears and returns within 24 h is not re-alerted; unreachable only after 2 failed checks in a row → no alert spam.
- [2026-10-24] Server output → clipped to 64 KB before parsing and scrubbed before the clip; stored excerpts ≤ 400 chars; 5 newest error lines per log → no huge rows, no secrets.
- [2026-10-24] Offline vs dead (T32b) → network errors (timeouts, DNS, connect, 5xx) mean offline: no kickstart, no alert, restarts during offline aren't counted as a crash loop; non-network failures (401, 409, …) keep the 120 s restart rule; a stuck loop (no attempt for 120 s) restarts; old-format heartbeats use the old rule → a Wi-Fi drop no longer causes restart loops.
- [2026-10-24] Network-gap notice → after ≥ 5 min offline the bot posts "Internet uzilgan edi HH:MM–HH:MM" once it's back (the wake-gap pattern).
- [2026-10-24] Factories name clash → the servers ORM `ServerCheck` is imported as `ServerCheckRow` in `tests/factories.py` (digest has a domain `ServerCheck`).
- [2026-10-24] P4 (Codex executor) → on hold by the owner: not started and not proposed until he asks explicitly; progress figures exclude it → the owner's decision.
- [2026-10-24] One bot for both roles? (owner asked) → no, two bots stay (owner's choice after the trade-offs) → the personal bot reads untrusted web content all day and holds no executor, git, SSH or dev token; one bot would remove that wall or need a front-router plus two backends (+2–3 tasks); separate processes also mean one crash doesn't take both down. The personal bot still answers project-status questions read-only.

## Merges T33 round 3, T74, T71 (2026-10-24)
- [2026-10-24] Long instructions → `additionalContext` carries ≤ 1,500 bytes; anything longer goes inline as a delimited `<yordamchi-context>` block before the owner's text, with spoofed tags defused; one rule for the assistant and the executor → the CLI shows the model only ~2 KB of hook context (T33, R-51).
- [2026-10-24] Origins → every public entry that takes an `OwnerOrigin` calls `require_owner_origin` first; an AST inventory test keeps the refusal table complete (21+ entries) → a lookalike object can't approve or change anything.
- [2026-10-24] Forwarded messages → `IncomingUpdate.forward` / `TurnInput.forwarded`; commands, approvals, answers, settings and stops come only from `own_text`; the executor sees forwards labelled as third-party material → a forwarded "ha" approves nothing (R-52).
- [2026-10-24] Settings/injection eval fixtures → keyed on (suite, prompt hash, kind, owner text, schema), not on the settings table; each fixture stores the table it saw, and a non-failing drift report lists the changes; a full re-record of both suites is mandatory at T47 → parallel features add settings constantly (deliberate deviation from CONVENTIONS §11).
- [2026-10-24] Web artefacts (T74) → a fresh Chromium and empty context per request; file fetch via stdlib urllib (http/https only, certifi); yt-dlp with no cookies, no netrc and no remote components; a 49 MB send cap with a 46 MB re-encode target; anything that can't fit (> ~24 min at defaults) is refused from metadata before download.
- [2026-10-24] TTS (T71) → OpenRouter Gemini TTS returns pcm only (SPEC said mp3; corrected); encoded to OGG/Opus 32 kbps with libopus; ffmpeg is checked before any paid request; every provider failure falls back to the next, notices are deduped once per reason per hour; voices by gender: Edge Madina/Sardor, Gemini Kore/Charon; `speakable()` strips code, URLs, markdown and emoji.
- [2026-10-24] Known flaky test (under load only): `tests/unit/dev/servers/test_service.py::test_the_loop_checks_each_host_every_interval`; a fix agent is on it; merges may land when it is the only red and passes alone 3×.
- [2026-10-24] Best STT model (owner: "eng sifatli") → default stays `gemini-3.5-transcribe`, Google's dedicated transcription model, the only one with `language_codes` + `custom_vocabulary` (project names); the owner's 20 voice notes (T38/S7) decide by measurement against `gemini-3.8-flash` and `gemini-pro-latest` (accuracy, names, mixed-language, ≤ 5 s); the winner becomes the default → quality is measured, not guessed.
- [2026-10-24] Secret storage (owner: a copied file must be unusable) → all secrets are encrypted at rest with the decryption key in the macOS login Keychain (item service `uz.yordamchi`), read without prompts through Apple-signed `/usr/bin/security` and written through `security -i` stdin (never argv); env files hold only `@keychain:` references; the vault master key moves from `master.key` to the Keychain; a plaintext secret in any file is refused at startup → reverses STACK §2.2 (the re-prompt problem doesn't apply via /usr/bin/security; verified on this Mac). Hashes stay only where one-way is correct (`secret-hashes.json` for pre-push detection).
- [2026-10-24] Large videos (300–400 MB) → deferred by the owner ("keyinroq xohishga qarab"); the 20 MB Telegram cloud Bot API limit stays; the bot tells the owner what it can do instead. When revisited: local Telegram Bot API server (Docker, api_id/api_hash from my.telegram.org) + ffmpeg downscale + Gemini Files API, plus a link path; a userbot (full account session) is rejected on security grounds.
- [2026-10-24] Typing status (owner) → "yozmoqda…" shows for quick answers (Q&A, reports, `holat?`, settings) from acceptance until the reply (repeated every 4 s, ≤ 60 s); long work (coding, new project, self-update, investigation turns) gets only the reaction ack plus the progress line; setting `ack.typing_for_work` (default false) switches typing on for work too → the owner's preference (T34b).
- [2026-10-24] OvozYoz (owner: "unga tegma, faqat namuna olishing mumkin"; Yordamchi must stay independent of it) → Yordamchi has no import, path or key shared with OvozYoz (only rewritten patterns); new setting `projects.excluded` (default []; the scan skips a listed folder, listing/routing/`get_project` never return it, removing a name is a weakening) → the owner closes OvozYoz to the dev bot himself if he wants: a default of ["OvozYoz"] would stop him working on it through the bot, and the routing eval's recorded registry includes it.
- [2026-10-24] T40 found that an Alembic batch rebuild (copy + DROP) with `foreign_keys=ON` empties CASCADE children and nulls SET NULL pointers → `migrations/env.py` migrates with `foreign_keys=OFF` and fails the run on any `PRAGMA foreign_key_check` row → a later drop/rename revision cannot silently lose child rows.
- [2026-10-24] `outbox_messages.turn_id`, `message_links.turn_id`, `message_links.question_id` → stay plain integers, not foreign keys → those rows outlive or precede what they mention, nothing deletes through them, and adding a key after T43 would be a table rebuild (`test_foreign_keys.py` pins it).
- [2026-10-24] T34b "kut" → never stops anything; with nothing assembling it holds a burst up to `burst.hold_max_s` (one rule), acknowledged with 👌 (`ui.hold_reaction`) and typing stops; a hold nothing follows ends `cancelled` (`hold_expired`) → matches "kut holds up to 2 min".
- [2026-10-24] T34b settings in plain words → deterministic cues ("sozlama…", `/sozlamalar …`, words addressed to the bot) stay after binding; a cue inside a message naming a project counts as work → no LLM intent yet.
- [2026-10-24] T34b routing LLM `change_setting` intent → held back (patch in the T34b report): its live re-record dropped explicit to 96.3 % and context to 85.2 % (UC-10 floors 97/90) → retry once with the "first ask whether it is about the bot" prompt step removed, at T47's re-record.
- [2026-10-24] T34b investigation ack → "X · Tekshiryapman (~3 daqiqa)" replaces "→ X"; a voice echo is sent first.
- [2026-10-24] T34b "X yangi sessiya" → retires the coding session only; while a turn works or waits it is deferred in memory (a restart forgets it; the owner repeats); a retired session ends `closed`, never resumed.
- [2026-10-24] T34b typing → starts at intake, stops when the message is routed to a turn (same unit of work); `ack.typing_for_work` (default false) keeps it for long work → the owner's "typing only for quick answers".
- [2026-10-24] First install before T41/T42 → the owner wants the dev bot running now; reviews still run before the personal bot (P3) and before any deploy-level feature is trusted; `0001` is frozen from this install on.
- [2026-10-24] Small talk in the dev group (owner: "qandaysan desam ham holat haqida ma'lumot beryapti") → pre-parse intents `greeting`/`thanks`/`help`, EARLY, whole-message only, catalog replies with no project status at all (owner: "oddiy gap uchun ham loyihalar haqida ma'lumot yoki holat haqida hisobotni tiqishtirmasin"), no model → instant, and routing's prompt stays unchanged (no re-record); an LLM `chat` intent for general questions waits for the T47 re-record.
- [2026-10-24] The 👀 "seen" reaction (owner: odd on every message) → `ui.seen_reaction` default empty = no reaction; typing and "→ X" are the ack; "kut" keeps 👌 → the owner can switch it back by chat.
- [2026-10-24] Report model → `models.report` haiku (warm asks 1.7–4.6 s for both models; live sonnet hit the 3.5 s budget twice and fell back to the template); `timeouts.report_s` stays 3.5 (UC-15 ≤ 4 s) → most report questions get a model answer.
- [2026-10-24] ✓✓ on the owner's group messages (owner: no double tick since the reaction went; then "qo'yib olib tashlayversang ham" once told the Bot API 10.3 has no read method for groups, only `readBusinessMessage` for business accounts) → `ui.read_mark` (default 👀): outbox op `mark_read` sets the reaction and removes it at once → ✓✓ with nothing left; `ui.seen_reaction` (a reaction that stays) still wins when set.
