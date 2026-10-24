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
