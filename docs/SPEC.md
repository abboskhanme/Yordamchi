# Yordamchi — SPEC (v1)

Status: for owner approval · 2026-10-23 · `plan-architect`. Inputs (settled, not re-opened here): `REQUIREMENTS.md`
(D1–D14, UC-xx, §4 acceptance criteria), `STACK.md` (pins, §2.1 mechanisms, §4 constraints), `CONVENTIONS.md`
(layering, feature recipe), `RISKS.md`, `ASSUMPTIONS.md`. Where this SPEC resolves a contradiction between them, it
says so and the decision is logged in `ASSUMPTIONS.md`. Every value written as `setting.key = X` is a default in
`shared/settings/defaults.py`, changeable by chat (UC-09); it is never a literal elsewhere.

## 0. Owner summary (approval point)

What gets built, in four steps. Each step is usable on its own before the next one starts.
- **P1 — Dev bot core.** One private group: you and the bot. You write or speak. The bot joins a screenshot, text
  and a voice note into one request, picks the project (and always names it), and hands the work to that
  project's Claude Code session. You see one live progress line per project, then a result: Natija, files,
  tests, commit, push. Commits and pushes happen without asking. It asks only about important decisions
  (deploy, server changes, data loss, money, sending to others), and you answer "ha" or "yo'q". Reports
  ("X qanday?") arrive in seconds, even while X is coding. "to'xta" stops work within 3 s. Settings change
  by chat. A watchdog restarts the bot and tells you if it crashes.
- **P2 — Self-running.** "xarajat?". Integrations you add by chat, including sign-in links you open on the
  phone. New projects from a description: plan first, build after your "ha". Handoff to the Mac. Park and
  resume projects. Safe self-update: the bot changes its own code by chat, tests it, switches, checks its
  health, and rolls back by itself if anything breaks.
- **P3 — Personal bot.** Ask anything: a text answer first, then a ~25 s Uzbek voice summary. Photos,
  videos, links, screenshots and video downloads. It reads project status and your integrations. It cannot
  run anything on the Mac or instruct the dev bot.
- **P4 — Codex.** "ChatGPT'ga o't" moves a project to Codex, with the same safety rules.

What you do once: create two bots and the group (setup checklist, about 15 min). Run `sudo pmset -c sleep 0`
and keep the Mac on the charger. Turn off automatic macOS restarts. Paste a Gemini key or an OpenRouter key
into the chat: the bot detects which one it is, checks it and sets itself up.
What you approve now: this plan, the phase order, and the open items in §10.
Not changed: your `~/.claude` settings. A project parked for more than 30 days resumes from a saved note.

## 1. Goal and reference implementations

One developer runs his whole dev workflow, and gets answers to anything, from his phone, through two owner-only
Telegram bots on his MacBook. Speed of answers is the top priority (D10); coding may be slow.

There is no Yordamchi code yet. **The reference implementation inside the repo is the first feature built,
`shared/inbox`**: every later feature copies its file shape (CONVENTIONS §4). External precedents to copy
from: OvozYoz `src/ovozyoz/engines/segment.py` (STT request shape, `_SYSTEM` transcription instruction,
language hints, parallel segments) and `strings.py` (catalog); ClaudeManager
`~/Library/Application Support/uz.claudemanager.desktop/state.json` (token-free: `activeId`, `autoSwitch`,
`accounts[].{email,status,usage.fiveHour/sevenDay.{utilization,resetsAt}}`); WunderkindEmployeesBot
`bot/main.py` (`@dp.errors()` returning `True`); AIAgents `docs/VOICE.md` (Opus encoding: libopus, mono,
48 kHz, `voip`); MegaPlus Platform `backend/lint/import_contracts.py` (C1–C8); BonviCall
`server/tests/test_layering.py` (A-rules).

## 2. Architecture

### 2.1 Processes
| Process | launchd label | Runtime | Responsibility |
|---|---|---|---|
| Dev bot | `uz.yordamchi.dev` | `releases/current/.venv` (Python 3.12.15) | polling, inbox, burst, routing, executor sessions, gate, questions, digest, server checks, outbox, control socket, heartbeat |
| Personal bot (P3) | `uz.yordamchi.personal` | same release folder, own process | polling, inbox, burst, assistant, speech, media tools, integration gateway, outbox, heartbeat |
| Supervisor | `uz.yordamchi.supervisor` | `supervisor/current/.venv`, own pinned Python 3.12.15, **stdlib only** | watchdog, `caffeinate -s`, daily DB backups, launchd log trimming, app self-update (build, test, drain, switch, probation, rollback) |

Children: one `claude` per live session (spawned by the Agent SDK with `cli_path=~/.local/bin/claude`), plus
`git`, `gh`, `ssh`, `ffmpeg`, `yt-dlp`, Chromium (Playwright) and `caffeinate`. There is nothing else: no web
server, no inbound port, no cloud relay. Both bots import the same code from the same release folder, but they
share no memory, no env file and no writable DB file.

### 2.2 Disk layout (decided)
```
~/Library/Application Support/Yordamchi/            DATA, 0700
  dev.env  personal.env                              0600 bootstrap only (token, owner id, chat ids, paths)
  db/dev.db  db/personal.db  (+ -wal, -shm)          0600
  releases/<version>/  releases/current -> <version> app releases, made read-only (chmod -R a-w) after build
  supervisor/releases/<v>/  supervisor/current -> <v>  supervisor/state.json
  run/  dev.sock personal.sock (0600)  <bot>.heartbeat.json  <bot>.clean  <bot>.lasterror
        flags/drain.json  flags/probation.json  secret-hashes.json
  updates/<id>/request.json  updates/<id>/status.json  updates/latest.json
  backups/daily/<date>/  backups/weekly/<date>/  backups/pre-update/<id>/
  media/<yyyymmdd>/…   (cache, purged after 7 days)
  sessions/{routing,report,settings,assistant,smoke}/  (empty private cwds; smoke/ is a scratch git repo)
  bin/ffmpeg  bin/ffprobe                            static 9.0.2 arm64, SHA-256 verified
~/.config/yordamchi/master.key                       0600, dir 0700, `tmutil addexclusion`
~/Library/Logs/Yordamchi/{dev,personal,supervisor}.jsonl (+ .1 .2 .3)   uz.yordamchi.<x>.launchd.log
~/Library/LaunchAgents/uz.yordamchi.{dev,personal,supervisor}.plist
~/Documents/Projects/Yordamchi                       working copy: the executor edits it; nothing runs from it
```
`<version>` = `YYYY.MM.DD-<7-char sha>`. Bootstrap env keys: dev `DEV_BOT_TOKEN`, `OWNER_ID`, `DEV_GROUP_ID`;
personal `PERSONAL_BOT_TOKEN`, `OWNER_ID`, `PERSONAL_GROUP_ID` (optional). Paths default from `core/paths.py`.
Bot tokens stay in these 0600 files (CONVENTIONS §12), so the supervisor can report with the dev token through
stdlib `urllib`. The `cryptography` dependency that STACK §4.7 gives the supervisor is therefore dropped
(logged in ASSUMPTIONS).

### 2.3 DB split (decided)
- **`dev.db` is written only by the dev bot; `personal.db` only by the personal bot.** Both files carry the
  same schema: one Alembic chain, with `upgrade head` run on each file. Each process uses its own subset of
  tables. With a single writer per file there is no cross-process write contention, and UC-36 ("no write
  access to the dev DB") holds by construction.
- The personal process opens `dev.db` only inside `shared/bridge`, as `sqlite+aiosqlite:///file:…/dev.db?mode=ro&uri=true`
  with `PRAGMA query_only=ON`. It reads only these tables: `projects`, `digests`, `server_checks`,
  `integrations` and `vault_secrets` (rows assigned to `personal`), `setting_versions` (keys declared
  `bots=BOTH`), and `usage_records`.
- The dev process opens `personal.db` read-only (same flags) for numbers and masking only: `usage_records`,
  `health_events`, and `vault_secrets` (to mask personal secrets in dev output). It never reads personal
  conversation content.
- **Settings resolution.** Dev: its own latest row, else the default. Personal: its own row, else the dev
  row for a `BOTH` key (read via the bridge), else the default. A dev-bot request to change a
  `PERSONAL`-only key is answered "buni shaxsiy botga ayting". This keeps one writer per file.
- **Integrations.** The bot that added an integration owns its row and refreshes its OAuth tokens. The dev
  bot may assign its integrations to `personal`, and the personal bot then reads them through the bridge.
  The personal bot adds integrations for itself only.

### 2.4 Diagram
```
  Owner phone ──Telegram──┐                                 ┌── Telegram ── Owner DM (P3)
              dev group   │ long polling (no inbound port)  │
        ┌─────────────────▼──────────────┐       ┌──────────▼──────────────────────┐
        │ uz.yordamchi.dev  (release)    │       │ uz.yordamchi.personal (release)  │
        │ poller→access→inbox→burst→STT  │       │ poller→access→inbox→burst→STT    │
        │ →intents/routing→turns→gate    │       │ →intents→assistant(Claude)→TTS   │
        │ digest·servers·outbox·socket   │       │ media·gateway·outbox·socket      │
        └───┬──────────┬─────────┬───────┘       └────┬─────────────┬───────────────┘
   spawns   │          │ rw      │ ro (numbers)  ro   │ (bridge)    │ rw
  ┌─────────▼───────┐  ▼         └────► personal.db ◄─┼─────────────┘
  │ claude ×≤15     │ dev.db ◄────────────────────────┘
  │ (SDK, hooks →   │   ▲  heartbeat files, flags, updates/      ┌──────────────────────────┐
  │  gate in bot)   │   └──────────────────────────────────────► │ uz.yordamchi.supervisor  │
  │ shims: ssh/scp  │      sqlite3 backup API, launchctl         │ stdlib · watchdog ·      │
  │ git pre-push    │◄──── unix socket run/dev.sock ────────     │ backups · self-update    │
  └─────────────────┘                                            └──────────────────────────┘
```

### 2.5 Startup (both bots, `bots/<bot>/app.py`)
1. `os.umask(0o077)`. Install `core/redact` on `sys.stderr`, `sys.excepthook` and the asyncio exception
   handler **before** importing aiogram or the SDK (STACK §4.3).
2. Load the env file named by `YORDAMCHI_ENV_FILE` (set in the plist). Scrub `os.environ`: remove
   `CLAUDECODE` and every `CLAUDE_CODE_*` variable.
3. **Refuse to start** if any STACK §4.4 credential source is present. That covers the listed environment
   variables, plus an `apiKeyHelper`, `env.ANTHROPIC_*`, `env.CLAUDE_CODE_OAUTH_TOKEN` or
   `env.CLAUDE_CONFIG_DIR` in `~/.claude/settings.json`. On refusal it writes `run/<bot>.lasterror`, sends
   `S.STARTUP_REFUSED(what=…)` (at most once per 6 h, marker in `run/`) and exits with code 78.
4. Resolve binaries into `core/proc.clean_env()` PATH, in this order: `releases/current/shims`,
   `~/.local/bin` (claude, gh, node, uv), `DATA/bin` (ffmpeg), then the login-shell PATH (`/bin/zsh -lc 'printf
   %s "$PATH"'`, 5 s timeout, deduped: brings `/usr/local/bin` docker, `~/.cargo/bin`, …), then
   `/usr/bin:/bin:/usr/sbin:/sbin`. A missing `claude` or `git` is fatal. A missing `ffmpeg`, `gh` or `node`
   is a degraded capability, reported in `holat?`.
5. Open the DBs. If `alembic current` is not `head` and no update is in progress, refuse with
   `S.STARTUP_SCHEMA_MISMATCH`. Migrations are run only by the installer and the supervisor.
6. If `run/flags/probation.json` names this version, start in **probation mode** (§3.2.12). Otherwise run
   the recovery pass (§2.7).
7. Start the long-lived loops under `core.aio.forever`: poller, burst timers, router, turn scheduler,
   outbox sender, digest refresher, server checker, scheduler (heartbeat edit, media purge, cost summary,
   CLI-version smoke test), Mac-attach scanner (P2), memory/disk guard, control socket, heartbeat writer.
   Async self-checks (`claude auth status` must report `authMethod: "claude.ai"`, speech keys, ClaudeManager
   state) only populate `holat?`; they never block polling.
8. If `run/<bot>.clean` is absent (an unclean previous exit), post
   `S.HEALTH_RESTARTED(reason, interrupted=[…])` within 60 s. Then delete the clean marker.

### 2.6 Shutdown (SIGTERM from launchd or `kickstart -k`; ExitTimeOut 20 s)
Stop `getUpdates`. Stop starting turns. For each live turn: guard set to deny-all, `interrupt()` with 3 s
grace, turn → `interrupted`. Flush outbox priority 0–1 for up to 5 s. Close sessions, write `run/<bot>.clean`,
exit 0. launchd kills the remaining process group (`AbandonProcessGroup` false), so no orphan `claude`
survives (STACK §4.6).

### 2.7 Crash recovery (startup pass; each state has exactly one outcome)
| Row state at start | Action |
|---|---|
| `inbound_updates.status=received` | re-attach to a burst in `update_id` order |
| `bursts.status=open` | re-arm `closes_at`; close at once if past |
| `bursts.status=closed` (not routed) | route now |
| `deliveries.status=pending` | deliver when the project is free (normal path) |
| `deliveries.status=sending` | unknown whether it arrived: never resend; post `S.DELIVERY_UNCERTAIN` "bajarilmay qoldi — qayta yuboraymi?" and mark `failed`. NFR-REL-05 |
| `turns.status ∈ {starting,running,stopping}` | mark `interrupted`; list it in "Qayta ishga tushdim…" with "davom ettiraymi?". Never auto re-run (NFR-REL-07) |
| `questions.status=open` | keep open; the answer arrives later as a message (`answer_mode=message`) |
| `decisions` pending/approved | keep; expiry rules apply (§3.2.4) |
| `outbox.status=sending` | resend (duplicate allowed, ≤ 2 %); `pending` → send |

### 2.8 Heartbeat, watchdog, "Onlayn"
- **Heartbeat file.** Each bot writes `run/<bot>.heartbeat.json` atomically after every successful
  `getUpdates` (long-poll timeout 25 s) and at least every 30 s while paused. Fields: `pid`, `version`,
  `started_at`, `last_poll_ok_at`, `mode` (`normal|paused|probation`), `loop_lag_ms`.
- **Watchdog** (supervisor, every 15 s):
  - process missing, or heartbeat older than 120 s → `launchctl kickstart -k gui/<uid>/<label>` plus
    `S_SUP.BOT_RESTARTED`;
  - ≥ 3 new `started_at` values within 10 min → crash loop: one alert per 30 min, with the last line of
    `run/<bot>.lasterror` (already scrubbed);
  - suppressed while `updates/<id>` is past `requested` and before `healthy|rolled_back`.
- **Pinned message.** The dev bot sends "Onlayn: HH:MM" (Asia/Tashkent) once, pins it, stores its id in
  `kv_state`, and edits it every `schedule.heartbeat_edit_min = 5`. If the message is gone, it sends and
  pins a new one.
- **Sleep and wake.** A wall-clock gap of ≥ 5 min between heartbeat ticks means the Mac was asleep. After
  the first post-gap `getUpdates` batch the bot posts
  `S.WAKE_GAP(from_, to, n)` "Mac uxlagan edi HH:MM–HH:MM, N ta xabar qayta ishlanmoqda" (NFR-REL-04).
- **Network drop.** `getUpdates` errors back off 1→2→4…≤ 30 s (jitter), so it resumes ≤ 60 s after
  connectivity (NFR-REL-03). The outbox keeps queued messages.
- **Long outage** (owner request 2026-10-27, after a 9.5 h silent outage: the macOS resolver answered only IPv6
  for api.telegram.org, and aiogram kept the bad answer in its 1 h DNS cache after the resolver was repaired):
  - every 5 min of network failures in a row, the poller closes its HTTP session (`PollConfig.renew_session_after_s`);
    the next request opens a new one with a fresh DNS cache and new connections. A process restart is not used: it
    would cut running Claude sessions for a fault that lives in one connection pool;
  - after 10 min offline, the watchdog shows one banner on the Mac (`osascript display notification`), naming every
    bot offline by then: `S_SUP.BOTS_OFFLINE`. One per outage; Telegram cannot carry the news of its own outage.

## 3. Modules, features and key flows

### 3.1 Feature map (CONVENTIONS §1/§4 shape; phase in brackets)
| Feature | Responsibility | Ports used |
|---|---|---|
| `shared/access` [P1] | owner/chat admission policy, foreign-chat leave, mints `OwnerOrigin` | Messenger |
| `shared/inbox` [P1] | idempotent update intake, secret extraction before persistence, ack | Messenger, Keystore |
| `shared/burst` [P1] | adaptive quiet window, albums, voice hold, "kut", hard cap, follow-up merge decision | Clock |
| `shared/voice` [P1 STT, P3 TTS] | speech provider choice, STT with hints + vocabulary, TTS script → OGG/Opus, voice samples | Transcriber, Synthesizer |
| `shared/vault` [P1] | AES-256-GCM secret store, masking set, secret-hash file for the pre-push scan | Keystore |
| `shared/settings` [P1] | registry, snapshot, change/revert/list, weakening check, NL interpretation | Assistant |
| `shared/outbox` [P1] | durable send queue, split/escape/`.md` fallback, coalescing, rate limits | Messenger |
| `shared/cost` [P1 record, P2 report] | usage ledger, caps, 80 %/100 % notices | — |
| `shared/health` [P1] | `holat?`, heartbeat, self-checks, smoke test trigger, wake gap | MacHost, AgentRuntime(probe) |
| `shared/audit` [P1] | append-only audit | — |
| `shared/integrations` [P2] | add/validate/list/remove, OAuth paste-back, token service, session injection specs | McpClient |
| `shared/bridge` [P3] | the only read path from personal to `dev.db` | — |
| `shared/topics` [D4 revised 2026-10-26] | the dev group's forum topics (§3.2.16): one per project, opened on its first work; project ↔ topic; reopen or replace a closed or deleted one; read by the outbox to place each row | Messenger |
| `dev/routing` [P1] | deterministic pre-route + LLM route, clarification, wrong-target correction | Assistant |
| `dev/turns` [P1; park P2] | sessions, scheduler (cap, pressure, queue), deliveries, progress, result, runaway, usage-limit wait, park/resume | AgentRuntime, Git, MacHost |
| `dev/gate` [P1] | classifier, decisions, grants, approval binding, shims/pre-push backends | Git |
| `dev/questions` [P1] | agent questions ↔ owner answers | — |
| `dev/projects` [P1 discovery, P2 chat ops] | registry from `~/Documents/Projects`, descriptions, deploy-branch detection from `.github/workflows/*.yml` (PyYAML `safe_load`, a runtime dependency pinned in `uv.lock`; YAML `on:` may parse as `True`; unparseable → every pushed branch counts as deploying) | Git |
| `dev/digest` [P1] | per-project digest, refresh, report answers, handoff note | Git, Assistant |
| `dev/servers` [P1] | 30-min read-only checks, on-demand investigation | RemoteShell, AgentRuntime |
| `dev/newproject` [P2] | draft collection → folder + repo → plan → approval → build | Git, GitHub, AgentRuntime |
| `dev/handoff` [P2] | Mac-attach detection, queueing, yield | MacHost |
| `dev/selfupdate` [P2] | branch work → request → supervisor → outcome report; supervisor self-switch | Git |
| `personal/qa` [P3] | warm assistant session, compression, "to'liqroq", fallback "zaxira model" | Assistant |
| `personal/media` [P3] | image/video/YouTube/link analysis | MediaAnalyzer, WebTools |
| `personal/artefacts` [P3] | screenshot, file fetch, yt-dlp download ≤ 50 MB | WebTools |
| `personal/files` [UC-37, 2026-10-29] | any file the owner sends read for the assistant (text, OOXML, PDF blocks, audio transcripts); `make_document` (§3.2.17) | MediaAnalyzer, WebTools, DocumentWriter |
| `personal/news` [P3] | news with source + time | Assistant |
| `supervisor/` [P1 watchdog, P2 updater] | separate stdlib program (§3.2.12, §9) | — (own code) |

Adapters: `telegram/`, `claude/{executor,assistant,guard}.py`, `codex/` (P4), `gemini/{stt,tts,video,assistant}.py`,
`openrouter/{stt,tts,video,assistant}.py` (new vendor dir; plain HTTP via aiohttp, C5 line added),
`edge_tts/tts.py` (P3; pin `edge-tts` in `uv.lock` then), `git/`, `github/`, `ssh/`, `mcp/`, `web/{browser,download,ytdl}.py`,
`keystore/file.py`, `macos/host.py`, `system_clock.py`. **New port** `ports/media.py::MediaAnalyzer` (video and YouTube
understanding). It is added to the CONVENTIONS §4 table in the same commit. Every other port is the one CONVENTIONS lists.

Repo additions to the CONVENTIONS §1 layout:
- `shims/{ssh,scp,sftp,rsync}` and `githooks/<hook>`: `/bin/sh` one-liners;
- `bin/yordamchi-helper`;
- `scripts/install.py`;
- `ops/launchd/*.plist.tmpl`;
- their Python in `src/yordamchi/bots/cli/{shim,githook,helper,check}.py`. These modules import only
  `yordamchi.core` (fast start; a new A-rule).

### 3.1.1 Control socket (`run/<bot>.sock`, 0600; one JSON request line → one JSON response line)
| Method | Caller | Request → response |
|---|---|---|
| `headers` | `yordamchi-helper` (dev MCP `headersHelper`) | `{server_name, session_token}` → `{headers}` or `{error_code}` |
| `gate_check` | shims | `{tool, argv, cwd, session_token}` → `{allow: bool, reason}` (fail closed on any error) |
| `secret_blocked` | githooks `pre-push` | `{cwd, file, rule}` → `{ok}` (bot posts `S.PUSH_BLOCKED_SECRET`) |
| `selftest` | supervisor | `{nonce}` → `{ok, steps{access,inbox,burst,intent,outbox,telegram}}` within 20 s |
| `status` | supervisor, check script | `{}` → `{version, mode, live_turns, drained}` |

`session_token` is random per session and identifies the caller. It is not a boundary against
same-user processes; the gate's forbidden rules keep the executor away from the socket.

### 3.2 Key flows
**3.2.1 Inbound → answer (dev bot)**
1. Poller: `getUpdates(offset=poll_offsets.next_offset, timeout=25, allowed_updates=[message, edited_message,
   my_chat_member])`. It feeds each update in order to `dp.feed_update` (sequentially, never as tasks), then
   persists `next_offset=max(update_id)+1`.
2. `OwnerOnly` (`bots/common.py`, policy `access.domain.admit`) returns `Admit | Drop | Leave`:
   - a stranger → `Drop`, log `update_dropped(user_id, chat_id)` only;
   - any foreign group, supergroup or channel, or `my_chat_member` for one → `leaveChat` immediately,
     direct call (≤ 10 s), never via the outbox, never posting there;
   - the owner in any chat but the configured one → `Drop`.

   In a forum dev group an update also carries its topic (§3.2.16 step 4); a topic service message (closed,
   reopened, renamed) is stored for the record and carries no request.

   Effective ids: `access.owner_id`, `access.dev_group_id`, `access.personal_group_id`. Each defaults to the
   bootstrap value; changing it weakens a safeguard.
3. Handler → `inbox.service.accept_update` (one uow):
   - `insert_or_ignore(inbound_updates)`; already present → return;
   - secret scan of text and caption: each hit goes to the vault and is replaced by `[kalit: <name> ••••abcd]`;
     a Claude login token (`sk-ant-oat…`) is replaced by `[rad etildi]` and never stored;
   - store only the redacted payload;
   - enqueue priority 0: delete the owner message plus the masked confirmation (if secrets were found);
     reaction `ui.seen_reaction` (default empty = none: the owner found 👀 on every message odd, 2026-10-24;
     "→ X" and typing are the ack) or, when it is empty, the read mark `ui.read_mark` (👀 set and removed at once,
     op `mark_read`: the owner sees ✓✓ and no reaction; the Bot API has no read method for a bot in a group,
     `readBusinessMessage` being for business accounts only); `sendChatAction(typing)` (deduped per chat for 4 s, repeated
     every 4 s while work for this chat is pending ≤ 60 s).
4. `burst.service.on_part`: attaches to the chat's open burst, or opens one, and recomputes `closes_at` with
   `burst.domain.window()` (§4.4). A burst never holds parts of two forum topics (§3.2.16 step 4).
   - Album items share `media_group_id`.
   - A voice part starts the STT job and holds the window.
   - Interrupt words skip the window entirely (3.2.5).
5. STT: `voice.service.transcribe_part`:
   - download ≤ 20 MB, then `voice.domain.pick_stt()`;
   - call with languages `stt.languages = ["uz-UZ","ru-RU","en-US"]` and vocabulary = registry names +
     aliases + integration names + `stt.extra_terms` (≤ 1,000; the first 200 for prompt-based providers);
   - a voice note ≥ `stt.split_over_s = 20` is split at silences (ffmpeg `silencedetect`) and the segments
     are transcribed in parallel, in order (OvozYoz);
   - one call is bounded by `timeouts.stt_s` + `timeouts.stt_per_audio_s` × the note's seconds;
   - a Google model with a `live` part (`gemini-3.5-transcribe-live`, the default since 2026-10-25: no daily
     request cap on the owner's key) goes over the Live API: ffmpeg → 16 kHz PCM, one session with the server's
     VAD off (`activity_start`, the audio, `activity_end`), the final `input_transcription` until
     `generation_complete`; `transcripts.provider_path = "live"`. Any other model uses the Interactions API;
   - failure → `transcripts.status=failed`, the part stays in the burst as `[ovoz tushunilmadi: <reason>]`.
6. Close → `TurnInput(parts in order)` → `bots/dev/intents.preparse` (deterministic: §6). Unresolved →
   `routing.service.route_burst` (§3.2.6).
7. A routed instruction:
   - ack first: `S.ROUTED(project)` "→ BonviHR", or for voice `S.VOICE_ECHO(text, project)` "Ovoz: «…» →
     BonviHR", sent before any delivery;
   - then `turns.service.submit`. If the project has a running turn and the route is `merge_ok`
     (confidence ≥ `burst.merge_min_confidence = 0.85`), deliver as steering (`deliveries.mode=next`);
   - otherwise queue a new turn. The scheduler starts it when cap, pressure and Mac-attach allow.
   - in a forum the echo and the rest of the work go to the project's own topic, and General gets one line pointing
     there; a burst written in a project's topic goes to that project with no routing model (§3.2.16).
8. Progress: one message per turn, coalesce key `progress:<turn_id>`, ≤ 1 edit per `outbox.progress_edit_s = 5`.
   The final result is a new message (golden format §6.2) that replies to the project's previous bot message.
9. The outbox sender runs per-chat token buckets: 1 msg/s per chat, 20/min for the group, edits counted. It
   honours `retry_after` per chat. Text over 4,096 or a caption over 1,024 is split by `outbox.domain.split`
   at a paragraph or code fence; more than 3 parts → a 1-line summary plus a `.md` file. The adapter raises
   if `reply_markup` is ever passed.

**3.2.2 Agent question → owner answer (UC-12)**
1. `AskUserQuestion` reaches `can_use_tool`. It does so in every mode we use; the docs say it "always falls
   through to the callback".
2. The adapter emits `QuestionAsked(tool_use_id, questions)` and awaits a future, bounded by
   `questions.hold_process_min = 30`.
3. `questions.service.open_question` writes the row, then outbox priority 0 within 5 s:
   `S.AGENT_QUESTION(project, text, options)`, i.e. "BonviHR · Savol: … 1) … 2) …", plus a `message_links` row.
4. The owner answers by number, text or voice, as a reply or not. `questions.domain.bind_answer` binds it to:
   - the replied-to question;
   - else the only open item (question or decision) in the group;
   - else a number or option label that matches exactly one question.

   Otherwise it sends `S.WHICH_ITEM(names)`. A number maps to the option `label`, and anything else becomes
   free text in `answers[question]`.
5. Future alive → `PermissionResultAllow(updated_input={"questions": q, "answers": a})`. Future gone (30-min
   hold elapsed or restart) → a delivery `mode=answer`: "[Yordamchi] Owner's answer to «<question>»: <answer>.
   Continue the task."
6. Hold elapsed → the callback returns `PermissionResultDeny(message=<prompts/dev/question_parked.md>)`, the
   turn becomes `waiting_question` (no slot), and the process closes when idle.

**3.2.3 Important decision (UC-13; never waits in a hook)**
1. Every tool call fires `PreToolUse` (matcher `None`, `timeout=gate.hook_timeout_s = 15`). This includes
   calls inside subagents (`agent_id` set). `adapters/claude/guard.py` calls `SessionSpec.guard(call)`; an
   exception or timeout → deny. A timed-out PreToolUse is also blocked by the CLI (hooks doc).
2. `gate.service.check_call`:
   - `gate.domain.needs(call)` lists the facts to fetch: untracked/ignored status of target paths, the
     default branch, deploy branches (cached per HEAD sha), the local DB URL host;
   - the service fetches them and calls `gate.domain.classify(call, ctx) → GateVerdict(allow|ask|forbid,
     gate_class, target, consequence)`.
3. `allow` → `{}`. `forbid` → deny with `prompts/dev/gate_forbidden.md`, no question. `ask`:
   - an approved, unconsumed, unexpired decision with the same `(project_id, tool_name, input_sha256)` →
     `UPDATE … SET consumed_at=now WHERE consumed_at IS NULL` (one row affected) → allow and audit;
   - else a pending decision with the same hash exists → deny again, no new question;
   - else insert `decisions(pending)` plus outbox priority 0 in one uow → deny with
     `prompts/dev/gate_pending.md` (slot `decision_id`).
4. Question: `S.DECISION_ASK(project, id, what, where, consequence)` "BonviHR · Muhim qaror #17: <nima> →
   <qayer>. Oqibati: <…>. Bajaraymi? (ha / yo'q / boshqa ko'rsatma)".
5. Binding: `gate.domain.bind_approval(text, reply_to, pending_items, mentioned_projects, numbers)`:
   - approve only if the message replies to the decision, or names its project or `#id`, or the decision is
     the only pending item (questions included);
   - else `S.WHICH_DECISION(list)`.

   Approval words `{ha, xa, qil, mayli, bo'pti, roziman, ok, yes, da}`; denial words `{yo'q, yoq, kerakmas,
   bekor, qilma, no, net}`, apostrophe variants normalised.
6. "ha" → `approved`, `grant_expires_at = now + gate.approval_ttl_min = 120`. Delivery
   (`next` if the turn is live, else `new_turn`): "[Yordamchi] Owner approved #17. Run exactly this, once:
   `<command>`". The re-run hits the grant path in step 3. A different command or host asks again (AC).
7. "yo'q" → `denied`, delivery "Owner declined #17; do not do it or anything with the same effect." Any other
   instruction → `denied`, and that instruction is delivered.
8. A pending decision older than its TTL → `expired`. A later "ha" to it creates a fresh decision and asks again.
   Other projects keep running throughout: decisions are per turn.

**3.2.4 Fast report path, digest refresh, handoff note (UC-15, C1)**
1. Intents `report_project`, `report_all` and `server_status` never enter the turn scheduler and never touch a
   project session.
2. `digest.service.answer_report` reads the `digests` row plus live turn rows, which gives "hozir: <title>,
   N daq".
   - A plain status question → template `S.REPORT_PROJECT`.
   - A free-form question → the warm report Assistant (`models.report = "sonnet"`, `models.report_effort =
     "low"`, `timeouts.report_s = 3.5`), with the digest JSON as data. On timeout → the template.
3. Every report ends with `S.FRESHNESS(age)` "Ma'lumot: 3 daqiqa oldin".
4. Fresh data needed (digest component older than `digest.stale_after_min = 30`, or route flag
   `needs_fresh`): send `S.CHECKING(minutes)` "Tekshiryapman (~N daqiqa)" within 2 s, then an `investigate`
   turn. It runs in its own session with the READ_ONLY policy and outside the coding cap; the result follows.
5. Server status: a check ≤ 30 min old answers directly. Otherwise `S.CHECKING`, then an on-demand
   `RemoteShell` read-only check (≤ 3 min).

Refresh triggers:
- after every turn end;
- git poll (`rev-parse HEAD` + `status --porcelain=v2 --branch`) every `schedule.digest_active_poll_s = 120`
  for projects active within 7 days, every `schedule.digest_idle_poll_min = 60` for the rest; a change →
  full refresh (`git log -10 --since=7.days`);
- server checks every `schedule.server_check_min = 30` over `servers.hosts`.

Git and SSH calls happen outside the uow (A3); the results are written in one short uow.

**Handoff note (owner decision 2026-10-23: `cleanupPeriodDays` stays 30).** Written after every finished
turn, and at park time, into `digests.handoff_note` (≤ 4,000 chars).
- Sections: Goal · Decisions · State · Next steps · Files touched.
- The first four come from the `<handoff>` block every final executor message must end with (§5.5).
- Files touched (`git diff --name-status <start_head>`, untracked files created, commits) are added by
  Yordamchi from git, deterministically.
- Missing block (interrupted or failed turn) → the report Assistant synthesizes the note from the previous
  note, the turn's last 20 progress lines and the git facts (`handoff_note_source=synthesized`).

**3.2.5 Interrupt and redirect (UC-03)**
1. Pre-parse, with no window and no LLM, matches `to'xta | stop | bas | kut | hammasini to'xtat | yo'q,
   boshqacha qil: …`. A voice note whose transcript is ≤ 4 words and matches counts too.
2. Target: the replied-to project, else the named project, else the only running turn. Several running and
   no target → `S.WHICH_RUNNING(names)`. "hammasini" → all of them.
3. Stop, in this order:
   - in-memory `turn.stopping = True` (the guard now denies every new tool call);
   - `interrupt()` (2 s);
   - no interrupted result within 2 s → kill the `claude` process group (TERM, 0.5 s, KILL) and mark the
     session `needs_resume`;
   - `S.STOPPED(project)` is sent only after the first step. Total ≤ 3 s.
4. "kut" while running = stop, and the next message for that project starts a new turn in the same session.
   "kut" while assembling = burst `holding` for up to `burst.hold_max_s = 120`.
5. Redirect = stop (step 3), then deliver X as `new_turn` in the same session (resume keeps context), ≤ 5 s.

**3.2.6 Routing (UC-10)** — `routing.domain` signals, in order. A burst written in a project's own forum topic never
gets here: it goes to that project with no model and no question (`route_decisions.method = topic`, §3.2.16 step 5).
1. **Explicit name or alias.**
   - Normalise: lowercase; `ʻ ʼ ’ ‘ \``→`'`; drop Uzbek suffixes (`'da 'dagi 'ga 'ni 'ning 'dan 'dagi`, also
     unquoted after a name).
   - Match against folder names and `projects.aliases` keys; Damerau-Levenshtein ≤ 1 for tokens of ≥ 6 chars.
   - One active match → route (`explicit`, 1.0). Archived and `*-wt-*` projects match only an exact name.
     Two matches → ask.
2. **Reply** to a bot message with `message_links.project_id` → that project (0.95), unless another name is
   explicit.
3. Answer-shaped text while questions or decisions are open → binding (3.2.2/3.2.3).
4. An open new-project draft takes the message unless another project is named.
5. **LLM**: the warm routing Assistant (`models.routing = "haiku"`, `timeouts.routing_s = 2.5`,
   `output_format` JSON schema).
   - Input: text and transcripts; ≤ 2 images downscaled to 1,024 px; the project table (id, name, aliases,
     one-line description); activity within `routing.context_window_min = 15`; open questions; running turns.
   - Output: `{intent, project_id, candidates[≤3], confidence, needs_fresh, is_new_project}`.
6. **Decision.** `confidence ≥ routing.min_confidence = 0.75` and margin ≥ `routing.min_margin = 0.2` → route.
   - Otherwise `S.WHICH_PROJECT(candidates)` "BonviHR'gami yoki HRMentor'gami?". One clarification per turn;
     a name, "birinchisi" or a number resolves it.
   - LLM timeout → the context project (activity ≤ 15 min) if there is exactly one, else ask.
   - Every decision → `route_decisions`.
7. **Chat** (owner, 2026-10-24: "guruhda ham … men bilan sodda yordamchi bot kabi gaplashishi kerak"). The LLM intent
   `chat` is a message about none of his projects: small talk, general knowledge, a programming or tech concept,
   advice, translation or writing help, arithmetic. It routes nowhere (`no_project`): `dev/chat` answers it with the
   warm `chat` Assistant (`models.chat = "sonnet"`, `models.chat_effort = "low"`, `timeouts.chat_s = 30`, persona
   `persona.dev_chat` and `lang.reply` in the per-ask block, §5.4), no tools, no project data, as a priority-0 reply
   to his message; on a timeout or failure, `S.CHAT_FAILED`. Never a turn, a report or "Tekshiryapman".
   - Only the LLM says chat, only for a burst of words (text, voice; no photo, file or link), and never when a
     project is named or replied to: that is a question about the project (`report_project`).
   - It must be sure (`routing.min_confidence`), or, with no project active, have no project in mind at all. Unsure
     with a project active → the ordinary route (the active project's follow-up as a report, or one question).
   - Eval: the `chat` bucket and its near misses in `tests/eval/routing` (≥ 90 % chat, 0 near misses as chat), and
     the answers in `tests/eval/chat`; see `docs/AI_CHAT.md`.

**Wrong target** ("bu X uchun edi"):
- stop the wrong turn (≤ 3 s);
- list its changed files (diff since `start_head` plus untracked files created);
- re-deliver the original input to X;
- ask `decisions(kind=wrong_target_revert)`. On "ha", Yordamchi's Git port, not the agent:
  - runs `git checkout <start_head> -- <tracked files>`;
  - deletes the created untracked files;
  - `git revert --no-edit`s that turn's commits (never a reset or force push).

**3.2.7 Park and resume (UC-18; transcript loss is the normal path, C1)**
- **Park** "X'ni yopib tur":
  1. A running turn gets a `next` message: "Owner is parking this project; stop at a safe point now."
  2. After `park.grace_s = 15` it is interrupted.
  3. The handoff note is refreshed (≤ 5 s).
  4. The session becomes `parked` and the process is closed, all ≤ 30 s from the request. Then
     `S.PARKED(project, note_preview)`.

  A parked project still answers reports from its digest.
- **Resume** "X'ni davom ettiramiz":
  - Existence pre-check: glob `~/.claude/projects/*/<session_id>.jsonl`, never reading or copying content.
  - Present → `resume=<id>`, first delivery "[Yordamchi] Resuming after a pause. Where we stopped: <note>.
    Continue from here."
  - Missing, or the CLI reports no such session (`SessionNotFound`) → a new session seeded with the note,
    plus `S.SESSION_RENEWED(project)` "X: eski sessiya o'chgan (30 kun) — eslatma bilan yangi sessiya
    boshladim". The same rule applies to every resume (handoff, after crash, after a limit).
  - Yordamchi never copies or keeps transcripts, and never sets `cleanupPeriodDays`.

**3.2.8 New project (UC-14)**
1. Intent `new_project` → `project_drafts(collecting)`. Parts are appended until `newproject.draft_silence_s
   = 120` of silence, or "tamom" / "boshla".
2. Name: an LLM-proposed PascalCase Latin name. Any case-insensitive collision → `<Name>2`, `<Name>3`, … (the
   first free one; an existing folder is never touched). The plan message says the name can be changed.
3. `mkdir`; `git init -b main`; `gh repo create abboskhanme/<Name> --private --source . --remote origin`
   (routine, never asked); register the project.
4. Planning turn with gate policy `PLAN_ONLY`:
   - writes allowed only to `docs/**`, `README.md` and `.gitignore`;
   - Bash only read-only commands plus `git add`, `commit` and `push`;
   - prompt `prompts/dev/newproject_plan.md`: REQUIREMENTS + SPEC; the stack choice goes through
     `AskUserQuestion` as an important decision (UC-13 #5).
5. Turn end → `S.PLAN_READY(name, summary)`, the `docs/*.md` files as documents, and
   `decisions(kind=plan_approval)` "Rejani tasdiqlaysizmi? (ha / o'zgartir: …)".
6. "ha" → policy `FULL`, build turn "Plan approved. Build from zero; finish committed and pushed".
   In a forum (§3.2.16 step 8) the plan is the project's first work: its topic opens when the plan turn is queued,
   and steps 4–6 happen there.
   "o'zgartir: …" → a revision turn (`PLAN_ONLY`), then step 5 again. The planning wall limit is
   `newproject.plan_max_min = 14` (AC ≤ 15 min).

**3.2.9 Mac handoff (UC-16)**
1. "X sessiyasi" → `S.MAC_COMMAND`: `cd '<path>' && claude --resume <id>`.
2. Scanner every `handoff.scan_s = 3`:
   - `~/.claude/sessions/*.json` entries with `kind == "interactive"`, `sessionId` in our sessions, and a
     live `pid`;
   - fallback when the registry is unreadable (R-11): `ps -axo pid=,command=` matching `claude .*--resume <id>`.
3. Attach detected:
   - set `mac_attached_at`;
   - a running bot turn → interrupt (≤ 10 s) plus `S.MAC_TOOK_OVER`;
   - new deliveries → `queued_mac` plus `S.MAC_QUEUED` "Mac'da ochiq — navbatga qo'ydim; yangi sessiya
     kerakmi?";
   - "yangi sessiya" → the bot opens a new session id.
4. Detach (gone on two scans 3 s apart) → deliver the queue into the same session id ≤ 30 s.

**3.2.10 Integration add, OAuth from the phone, injection (UC-08, NFR-SEC-07)**
1. "X'ga ulan: <url> [token]". The token was already vaulted at intake (3.2.1 step 3).
   - Intent `integration_add` carries the url, secret ref, name and assignment. The assignment defaults to
     the bot it was said in; the dev bot also accepts "ikkala bot uchun".
2. Validation, ≤ `timeouts.integration_validate_s = 25`:
   - `McpClient.list_tools(url, headers)`; record each tool's `readOnlyHint`;
   - a 401 with OAuth metadata → step 3;
   - any failure → `S.INTEGRATION_FAILED(name, reason, fix)`, within 30 s (AC).
3. OAuth (`mcp` `OAuthClientProvider`):
   - redirect URI `http://localhost:<integrations.oauth_redirect_port = 33418>/callback`;
   - `redirect_handler` → `S.OAUTH_LINK(name, url)`: "Havolani oching, ruxsat bering. So'ng ochilmay qolgan
     sahifa manzilini (`http://localhost:33418/…`) shu yerga yuboring.";
   - the pasted callback URL is treated as a secret at intake (deleted, never logged) → `callback_handler`
     returns `(code, state)`;
   - PKCE and refresh are in the SDK. `TokenStorage` = vault. Wait ≤
     `integrations.oauth_paste_timeout_min = 15`; a restart mid-flow means the owner sends the link again;
   - a server that requires a Client ID Metadata Document (MCP 2026-07-28) works only if
     `integrations.oauth_client_metadata_url` (a static HTTPS JSON) is set; otherwise
     `S.OAUTH_CIMD_NEEDED(name)`.
4. Store the `integrations` row plus the vault secret → `S.INTEGRATION_ADDED(name, tools)`, with read-only
   tools marked.
5. Token refresh: the owning bot refreshes when `expires_at − now < 10 min` (loop every 2 min). Two failures →
   `needs_reauth` → `S.OAUTH_REAUTH(name)` once until fixed (AC).
6. **Dev sessions**, rebuilt at every session open:
   - `mcp_servers[slug] = {"type": "http", "url": url, "headersHelper": "'<DATA>/releases/current/bin/yordamchi-helper' headers"}`;
   - the helper is a `/bin/sh` wrapper around `"$YORDAMCHI_PY" -I -m yordamchi.helper`. It reads
     `CLAUDE_CODE_MCP_SERVER_NAME`, `YORDAMCHI_SOCK` and `YORDAMCHI_SESSION_TOKEN` from env, asks
     `run/dev.sock`, and prints `{"Authorization": "Bearer …"}`. No secret ever goes on argv;
   - `ENABLE_CLAUDEAI_MCP_SERVERS=false` turns account connectors off and keeps project `.mcp.json` and plugins;
   - gate: an MCP tool with `readOnlyHint` or listed in `tool_policy.dev` → allow; any other →
     `third_party_send` → ask.
7. **Personal sessions** get no remote MCP. The in-process gateway server `yordamchi_int` exposes only
   allowed tools (`readOnlyHint`, or allowed when added) as `<slug>__<tool>`. Each one forwards through
   `McpClient.call_tool` with vault headers, so a tool added on the server later never appears (UC-35).
   A plain API-key integration (`kind=api_http`) appears in both bots through `yordamchi_int` as
   `<slug>__get(path, query)`. It is GET only, with the base URL fixed and the auth header added
   in-process, so the key never reaches the model.
   **Built for the personal bot (T76, 2026-10-31; `personal/gateway`):** an MCP integration is two tools,
   `<slug>__tools` (the live list, filtered to the tools read-only or allowed at add time) and `<slug>__call(tool,
   arguments)`, so the toolset signature stays stable. The key may also go as a query parameter (`auth=query`,
   `header_name` holds the parameter name). The owner connects by talking: the assistant gets `connect_api(name,
   base_url, key, auth, param, description, test_path?)` and `connect_mcp(name, url, key?, header?)`, where `key` is
   the vault name in a `[kalit: …]` marker **from the owner's own words in this burst** (any other key is refused), so
   it fills in the base URL and auth style it knows, tests one GET, and can fix it by connecting the same name again.
   An OAuth sign-in link goes straight to the owner; his pasted page address completes it (pipeline step 3).
   "integratsiyalar", "X integratsiyasini o'chir / qaytar", "X'ga qayta ulan" are deterministic commands.
8. Remove → `removed_at`; the integration is absent from the next session. Reverting a removal works for
   30 days. Nothing is account-bound, so the rotation AC holds by construction.

**3.2.11 Speech keys: Gemini direct or OpenRouter (owner decision 2026-10-23)**
1. Intake recognises `AIza[0-9A-Za-z_-]{35}` and the newer `AQ.[A-Za-z0-9_-]{40,120}` → `google_api_key`, and
   `sk-or-v1-[0-9a-f]{64}` → `openrouter_api_key`, in either bot.
2. `voice.service.configure_key` validates, ≤ 10 s:
   - Google: `models.get` for `stt.google_model = "gemini-3.5-transcribe-live"` and `tts.google_model =
     "gemini-3.8-flash-tts"`;
   - OpenRouter: `GET https://openrouter.ai/api/v1/key` (`limit`, `usage`, `is_free_tier`), then the model
     endpoints for `stt.openrouter_model` and `tts.openrouter_model`.
3. Providers come from `voice.domain.pick(keys_status, settings)`. Each `speech.*_provider = "auto"` means:
   | Capability | Order |
   |---|---|
   | STT | Google → OpenRouter → none |
   | TTS | **OpenRouter** (Gemini TTS models; owner decision 2026-10-24) → Google direct (only if a key is present) → Edge (`tts.edge_voice = "uz-UZ-MadinaNeural"`) → text only |
   | Video understanding | Google → OpenRouter → none (pages still work through Claude WebFetch) |
   | News grounding fallback | Google only |
4. The reply `S.SPEECH_CONFIGURED(stt, tts, video, notes)` names each provider and says when the tier is
   free ("bepul tarif: limit tez tugashi mumkin"). OpenRouter becoming a paid speech provider while
   `caps.openrouter_monthly_usd` is still 10 → one weakening question: "Ovoz uchun oyiga ~$30 ketadi; limitni
   $40 ga oshiraymi?".
5. **What OpenRouter supports** (verified on openrouter.ai docs, 2026-10-23):
   - **STT:** `POST /api/v1/audio/transcriptions` with `google/gemini-3.5-transcribe` documents no language
     or vocabulary options (WAV only). The OpenRouter Transcriber therefore uses `POST
     /api/v1/chat/completions` with `stt.openrouter_model = "google/gemini-3.8-flash"`:
     - content = the OvozYoz `_SYSTEM` text, plus "Expected languages: uz-UZ, ru-RU, en-US", plus "Spell
       these terms exactly: <≤ 200 terms>";
     - audio = `input_audio{data: base64 WAV 16 kHz mono (ffmpeg from OGG), format: "wav"}`;
     - minimal reasoning effort;
     - on a 4xx it falls back to `/audio/transcriptions` without hints, recorded in `transcripts.provider_path`.
   - **TTS: available.** `POST /api/v1/audio/speech` with `google/gemini-3.8-flash-tts` (or `-lite-tts`).
     It takes the same 30 voice names and `instructions` (accent/style). `response_format` is `mp3|pcm`:
     there is no Opus, and streaming is undocumented. The adapter requests `mp3` and pipes it through ffmpeg
     to OGG/Opus.
   - **Video:** chat completions `video_url` (base64 data URL). YouTube URLs work only on the AI Studio
     route, so the request sets `provider.order = ["google-ai-studio"]`.
6. Quota and rate limits (Google 429 `RESOURCE_EXHAUSTED`, OpenRouter 429, OpenRouter 402 no credits) map to
   `RateLimited` / `QuotaExhausted`.
   - Text is always sent.
   - Voice skipped → `S.VOICE_SKIPPED(reason)`, at most once per reason per hour.
   - STT failed → `S.STT_FAILED(reason)` (UC-05 F).
   - Voice is never dropped silently.
   - Caps (NFR-COST-02): at `caps.warn_pct` → one `cap_notices` message. At 100 % of a provider's
     monthly cap, paid calls to that provider stop until the owner raises the cap (one weakening question).
     STT moves to the other configured provider, else `S.STT_FAILED(cap)`; TTS moves to Edge, else text only.

   The UC-05 mixed-language eval runs on each configured STT provider, and `holat?` shows the last score
   per provider.

**3.2.12 Self-update (UC-19)**
Bot side (`dev/selfupdate`):
1. Intent `self_update` → Yordamchi creates branch `selfupdate/<id>-<slug>` in the working copy and runs a
   turn with `prompts/dev/selfupdate.md`: work on the branch, run `make check`, commit, push the branch,
   never touch `main`.
2. Turn end → `git diff --name-only main...HEAD`, matched against `.protected-core` read from the **running
   release**, not the branch.
   - Both `supervisor/**` and other paths changed → `S.UPDATE_SPLIT_NEEDED`, refused.
   - Protected core touched → `decisions(kind=selfupdate_core)`, one plain-language question (Q5).
3. Write `updates/<id>/request.json {id, commit, branch, kind: app|supervisor, decision_id}` atomically →
   `S.UPDATE_STARTED`.

Supervisor (`kind=app`). Every step is persisted in `supervisor/state.json` before it runs:
1. **Build.**
   - `git archive <commit>` → `releases/<version>/`, then `uv sync --frozen --no-editable`;
   - `make check` with `YORDAMCHI_DATA_DIR=<tmp>` must exit 0 with a collected test count ≥
     `last_good.tests_count`. Otherwise `refused`, nothing is switched, and the failing tests are reported.
2. **Drain.**
   - Write `run/flags/drain.json`. Bots stop starting turns (they queue with `queue_reason=update_drain`).
   - Running coding turns may finish for up to `selfupdate.drain_max_min = 30`; then the dev bot asks
     (`decisions(kind=drain_timeout)`).
   - Then polling pauses and each bot writes `run/<bot>.drained`.
3. **Snapshot** `dev.db` and `personal.db` with the `sqlite3` backup API → `backups/pre-update/<id>/`.
4. **Migrate**: `<release>/.venv/bin/python -m alembic upgrade head`, per DB file. Record `migration_ran`.
   A failure → step 8.
5. **Switch**: create a `current.tmp` symlink and `os.replace` it onto `current`; write
   `run/flags/probation.json {version, nonce}`; `launchctl kickstart -k` each installed bot.
6. **Probation**, ≤ `selfupdate.health_timeout_s = 120`:
   - each bot runs only the heartbeat, the control socket and a **peek poller**: `getUpdates(offset=N,
     limit=1, timeout=0)`, never advancing, so Telegram keeps every held message. No owner message is
     consumed, so nothing real is written to a DB that may be restored (only the `synthetic=true` self-test rows);
   - healthy = a fresh heartbeat with the new version and `last_poll_ok_at`, and `selftest(nonce)` over the
     socket. The self-test is a synthetic owner update (`synthetic=true`) through access → inbox → burst
     (window 0) → intent `selftest` → outbox → a real `sendChatAction(typing)` acknowledged by Telegram.
7. **Healthy**:
   - set `last_good = version` and delete the probation flag; the bots switch to normal and process the
     held updates;
   - merge: `git switch main && git merge --ff-only <branch>` (`--no-edit` merge if `main` moved), then
     `git push origin main`, then delete the branch locally and remotely;
   - write `status.json` → the dev bot posts `S.UPDATE_DONE(version, changes, tests, health)`.
8. **Unhealthy** (no heartbeat, a crash, a failed self-test or a failed migration):
   - `launchctl bootout` the bots, point `current` at the last good version, and restore the DB copies if
     `migration_ran`;
   - `launchctl bootstrap` the bots, health check again, then `S_SUP.ROLLED_BACK(reason)`. Each bot also
     posts the outcome from `updates/latest.json` when it starts;
   - the last good version also fails → `S_SUP.MANUAL_REPAIR(steps)` (R-32).
9. `kill -9` of the bots or the supervisor at any step: the supervisor resumes from `state.json`. Before
   `healthy` it rolls back; after, it finishes the merge and the report.

`kind=supervisor` (protected core, asked once): the dev bot builds `supervisor/releases/<v>/`, runs
`supervisor/tests`, switches `supervisor/current`, and runs `kickstart -k` on the supervisor. No supervisor
heartbeat within 60 s → it switches back.

**3.2.13 Personal answer (UC-30)**
1. The burst closes (same window rules) → personal pre-parse (§6.1) → `qa.service.answer`.
2. Send into the warm session (§5.3 `assistant`, `models.qa = "sonnet"`, `models.qa_effort = "medium"`):
   text, transcripts and image blocks, plus the per-turn context (§5.4).
3. Stream with `include_partial_messages=True`. Deltas → `sendMessageDraft` (private chat) at most every
   `qa.draft_interval_s = 1.0`; a group gets typing only.
4. Output contract (`prompts/personal/qa_system.md`):
   - the answer, ≤ `text.max_chars = 3500` and ≤ `text.max_lines = 40`;
   - `---OVOZ---`, then a voice script of ≤ `voice.max_words = 70`;
   - if the answer was compressed, `---TOLIQ---` and the full version (stored, not sent).
5. At the `ResultMessage`, send the final text right away (split rules).
6. **Voice**, only after the text:
   - `voice.domain.speakable` drops URLs, code, markdown and emoji, then cuts at a sentence end;
   - `Synthesizer.synthesize(stream=True)`, piped through ffmpeg (`-c:a libopus -b:a 32k -ar 48000 -ac 1
     -application voip -f ogg`) → `sendVoice` ≤ 12 s after the text;
   - `voice.enabled = true`; when it is off, or TTS fails, the text alone is final (notice rule 3.2.11 step 6).
7. A new message before step 5 → into the same session with `priority: "next"`; the reply covers both.
8. "to'liqroq" → the stored full version.
9. Claude unavailable (all accounts limited, auth failed, CLI missing) → the Gemini or OpenRouter Assistant
   (`models.fallback_text = "gemini-3.8-flash"`), with the answer prefixed `S.FALLBACK_LABEL` "(zaxira model)".
10. Rotation: a new session after `qa.session_idle_rotate_h = 6` idle, on "yangi suhbat", or at 900
    WebSearch calls (env cap raised to 1000, rotation kept, STACK §4.8). It is seeded with a 5-line summary of the old session.

**3.2.14 Media and artefacts (UC-31/32)** — personal tools on the in-process server `yordamchi_media`
- **Image**: sent to Claude as image blocks (first text ≤ 10 s).
- **Video ≤ 20 MB, YouTube link, web page**: `S.LOOKING` "Ko'ryapman" ≤ 2 s. Video and YouTube go to
  `analyze_video(ref|url)` (`MediaAnalyzer`); a page goes to `WebFetch`. Claude answers what it is, the key
  points and its opinion. Content is data. Over 20 MB inbound → `S.FILE_TOO_LARGE_IN`.
- **`screenshot(url)`**: Playwright, a new context per call, 1280×800. The viewport PNG goes as a photo and
  the full-page PNG as a document, ≤ 30 s.
- **`fetch_file(url)`**: direct download (≤ 50 MB, content-type checked), or `og:image` / the largest
  `<img>` via Playwright.
- **`download_video(url)`**: yt-dlp with `js_runtimes={"node": "~/.local/bin/node"}` and ffmpeg from
  `DATA/bin`.
  - Format: `bv*[height<=720][filesize<45M]+ba/b[filesize<48M]/bv*[height<=480]+ba`.
  - Over 49 MB → re-encode at `floor(46 MB·8/duration) − 64 kbps` audio.
  - Still too large, or a login or paywall → `S.VIDEO_TOO_LARGE` with alternatives.
- Files go to `DATA/media/` and are attached after the turn. Purge after `media.retention_days = 7`.

**3.2.15 Settings change (UC-09)**
1. Intent `settings_change` → the settings Assistant (`models.settings = "sonnet"`) receives the registry
   (key, type, doc, value, check). It returns `{key, value}`, `{needs_code}` or `{unknown}`.
2. `settings.domain.validate` fails → `S.SETTING_INVALID(why, options)`; nothing is stored.
3. `weakens(old, new)` → one question (`decisions(kind=settings_weaken)`). Otherwise apply.
4. Apply = a new `setting_versions` row, which needs an `OwnerOrigin` (A5) → a one-line
   `S.SETTING_CHANGED`. It takes effect on the next message: every job takes `snap = await
   settings.snapshot(deps)`, and sessions get it per §5.4.
5. "sozlamalaring qanday?" → grouped template plus the full `.md` (≤ 3 s). "oldingi holatiga qaytar [key]" →
   a new row with the prior value. `needs_code` → offer a self-update.

**3.2.16 Forum topics (D4 revised 2026-10-26; `shared/topics`, `0002`)**

The owner: "Split the group into topics, but I won't open topics by hand: you open a topic for each of my projects
automatically. In General I ask general questions. If I ask in General to create a new project, it creates a separate
topic for exactly that project and works with it there."
1. **Setup.** The dev group is a forum supergroup: the owner turns Topics on and gives the bot the "Manage topics"
   admin right, once (`python -m yordamchi.check` shows `is_forum` and `can_manage_topics`). Each owner message says
   whether the chat is a forum (`chat.is_forum` → `topic_chats`). Topics off is no error: the bot is the flat group it
   was, with no topic call at all. `topics.enabled = false` switches the behaviour off too: no topic is opened or
   reopened, a message in a topic is read as one in General, and the bot's topics are set aside (`closed_at`) at the
   router's next pass, so its projects' work goes to General; a project's next work reopens its topic once it is on.
2. **Opening.** A project's topic opens on its first work, never before and never for every project at once:
   `turns.open_topic` runs before a turn's first message is queued (the route, a submit, a report's investigation, a
   server investigation, a new project's plan) and once more as any turn starts. It is named after the project and
   gets one of Telegram's six icon colours, by a hash of the project id (the same every time). Later work reuses it
   (`project_topics`). Without the right Telegram refuses: the work goes to General, the owner is told once
   (`S.TOPICS_NO_RIGHT`), and the bot asks again after `topics.retry_after_min = 30`. A call that does not answer in
   `timeouts.topic_s = 5` also leaves the work in General: work never waits for a topic.
3. **Closed or deleted.** A topic the owner closed (its service message says so) is reopened by the project's next
   work. Telegram tells nobody about a deleted one: the next send into it fails ("message thread not found"), the
   outbox opens a new topic for the project, every row still bound for the old one follows, and the row goes again at
   once (General when no topic can be had, and for a thread no project owns). The live line of a turn that was
   running in the deleted topic is gone with it; its result arrives in the new topic. A pointer written just before
   the repair still links the old topic.
4. **Inbound.** A message carries its topic: `message_thread_id` when `is_topic_message` (Telegram also sets a thread
   id on a reply in General, and presents every topic message as a reply to the topic's first message: that is no
   reply). `bursts.thread_id` keeps it. A burst never holds parts of two topics: the chat keeps one assembling burst
   (its unique index is frozen in `0001`), so a part from another topic closes the assembling one first
   (`close_reason = thread`), or, while that one waits for a transcript, waits itself, still `received`, with every
   later part of its topic behind it. An interrupt word from another topic stands alone and cuts nothing short.
5. **In a project's topic** the pre-parse of §6.1 runs with that project in mind: a bare "to'xta", "davom ettir",
   "qayta yubor", "yangi sessiya", `/stop` or `/hisobot` acts on it; a "ha" or a "2" binds only its open questions,
   decisions and revert offers (the topic names the project, as the words would); no clarification is offered; and
   anything else is its work with no routing model, recorded like any route (`route_decisions.method = topic`) and
   steering its working turn when it is a follow-up (UC-02). Only a voice note is echoed there. Small talk and
   `holat?` are answered in the topic.
6. **In General** (and in a topic the bot did not open) routing is content-first, as before. Work routed from there
   goes on in the project's topic: the echo (or "Tekshiryapman"), the live line, questions, decisions, results and
   files. General gets one line pointing there (`S.TOPIC_POINTER`, a `https://t.me/c/<chat>/<topic>` link), and a
   reply to that line still routes to the project.
7. **Outbound.** Where a row goes is decided when it is written (`outbox_messages.thread_id`): the thread its caller
   names (an answer goes where the owner wrote, a report about a project with a topic too), else the open topic of the
   project the row is about (`About.project_id`), else General. Health, startup and "Onlayn" notices, cross-project
   reports, settings and cost answers are General's. Edits, deletes, reactions and pins name their message and carry
   no thread; a typing hint carries it and is deduped per topic, and a reply ends the hint of its own topic only.
   `dev/gate` posts its questions itself: into the project's topic, or General when that topic is gone.
   Deciding at the write rather than at the send keeps a row's place fixed and visible, needs only the database
   (never a Telegram call inside a unit of work), and the topic exists by then (step 2); the send only repairs a topic
   Telegram refuses (step 3).
8. **New project** (§3.2.8; not wired yet, `docs/PENDING_WIRING.md` From the Topics unit): its plan is its first work,
   so its topic opens when the plan turn is queued; the planning line, the plan's documents and "Rejani
   tasdiqlaysizmi?" go there, and General gets the pointer.

**3.2.17 Files and documents (UC-37; owner, 2026-10-29; `personal/files`, no new table)**

The owner: "It should be able to analyse any file I send. If I send a meeting recording, it should transcribe it and
also prepare the documents I need."
1. **Which parts.** Every part of a closed personal burst that carries a file, except what `personal/media` already
   reads (photos and images sent as files go as image blocks; videos go to `analyze_video`) and voice notes (STT,
   §3.2.11). `files.domain.file_kind(kind, attachment)` decides, by the declared type and then the suffix:
   - `audio`: an audio message, or a file of type `audio/*` or with an audio suffix (`.mp3 .m4a .wav .ogg .opus .aac
     .flac .amr .wma .aiff`);
   - `pdf`; `word` (`.docx`); `sheet` (`.xlsx`); `slides` (`.pptx`);
   - `text`: `text/*`, JSON, XML, and text suffixes (`.txt .md .csv .tsv .json .xml .html .yaml .log .srt .vtt`, code);
   - `other`: anything else (archives, legacy `.doc/.xls/.ppt`, binaries) → `S.FILE_UNSUPPORTED(name)`, which asks for
     PDF, DOCX or text instead.
   A file over `files.max_mb` (≤ 20, Telegram's bot download cap) is refused before the download
   (`S.FILE_TOO_LARGE_IN`).
2. **Reading** (`files.service.read_burst`, before the assistant is asked; each file into
   `DATA/media/<yyyymmdd>/file-<token>/`, 0700/0600, purged with the media after `media.retention_days`):
   - `text`: decoded (UTF-8, UTF-16 by BOM, else cp1251);
   - `word`, `sheet`, `slides`: their text from the OOXML zip (stdlib `zipfile` + `xml.etree`, pure in the domain):
     paragraphs and tables, every sheet as tab-separated rows, every slide's text in order;
   - `pdf`: no extraction. The file goes to Claude as a base64 `document` block beside the image blocks
     (`AssistantRequest.documents`; spike 2026-10-29: the CLI's stream-json input passes it through). At most
     `files.pdf_max_pages` by the PDF's own page count (best effort; a count it cannot read passes) and
     `files.max_pdfs` per answer. The fallback assistant does not get it: its answer says it saw no file.
   - `audio` (the meeting case): "Tinglayapman…" at once (`S.FILE_LISTENING`), a refusal over `files.audio_max_min`
     when Telegram reports the duration, then `WebTools.compress_audio` (static ffmpeg: mono 16 kHz MP3 at
     `files.audio_kbps`, so any container and codec Telegram carries becomes one both readers take), then
     `MediaAnalyzer.analyze_audio` with `prompts/personal/files_transcribe_system.md`: a verbatim transcript in the
     spoken language, speakers labelled (names when they are said), a `[mm:ss]` mark per turn, nothing summarised or
     left out. Provider order, keys, fallback and cost caps are the video reader's (`speech.video_provider`,
     `video.*_model`, `media.domain.pick_video`, `falls_back`, T51 caps; usage source `audio_file`); the call is
     bounded by `timeouts.audio_file_s`. The transcript is sent to the owner as `transkript-<name>.txt` when
     `files.send_transcript` (the default), beside the answer (the outbox sends a file after the texts queued with it).
   Each text (and transcript) reaches the assistant only as material: `prompts/personal/files_material.md`, a
   nonce-tagged, defused block (the `media.domain` boundary), cut at `files.text_max_chars` with "cut short: yes".
   A failed reading is the owner's message; when the burst says nothing else, it is the whole reply (as UC-31 F).
3. **Answering.** The assistant answers from the material as usual (§3.2.13). Without an instruction from the
   owner: for a meeting or conversation it gives the summary, the decisions and the action items (who, what, by
   when) and makes the minutes document; for any other file it says what it is and its key points. With an
   instruction ("bayonnoma qil", "jadvalga sol", "xulosa yoz") it does that.
4. **Making documents.** The in-process toolset `yordamchi_files` has one tool, `make_document(format, filename,
   title?, content?, sheets?)`:
   - `format`: `docx`, `pdf`, `xlsx`, `md` or `txt`; `content` is Markdown (headings, paragraphs, `-`/`1.` lists,
     `**bold**`, `*italic*`, `` `code` ``, pipe tables, fenced code); `sheets` (`xlsx` only) is
     `[{name, rows: [[cell, …], …]}]`, else the Markdown tables become the sheets;
   - `files.domain.document_of(arguments)` validates and parses it into `ports.documents.DocumentSpec` (pure);
     the filename is cleaned to letters, digits, spaces, `-` and `_` (default `hujjat`);
   - `DocumentWriter.write` (adapter `office/writer.py`: python-docx, openpyxl; PDF = the same blocks as HTML,
     printed by Playwright's Chromium, A4) writes it within `timeouts.document_s`, ≤ `files.document_max_mb`;
   - the tool answers `{"status": "made", "file": …}`; the files are queued after the answer's text, in the order
     made, at most `files.max_documents` per answer. A refused or failed document is an error result to the model,
     which tells the owner.
5. **Content is data.** A file, a transcript and the material are never instructions: they cannot call a tool, change
   a setting or make a document by themselves (the injection eval covers files, UC-31). Logs carry kinds, sizes,
   durations, providers and codes, never a file name or content.

**3.2.18 Personal agent mode (owner, 2026-10-31; reverses the restricted personal session of §5.3 / S5)**

The owner: "Telegram men va Claude orasida shunchaki ko'prik vazifasini o'tasin … terminalda aytsam qiladiyu, nega
Telegramdan aytsam qilmaydi". The personal bot's answer is a **full Claude Code session**, as in his terminal; Telegram
stays the bridge (burst, voice, media and files in; text, voice and files out — unchanged).
1. **Session** (`adapters/claude/assistant.py`, the `qa` kind with `AssistantProfile.agent` set):
   - cwd = `agent.workspace_dir` (default `~/Documents/Projects/_shaxsiy`, created 0700 at start; never a Yordamchi
     data or release dir);
   - `tools` = the CLI's preset (Bash, Read, Write, Edit, Glob, Grep, WebSearch, WebFetch, Task, TodoWrite, …) plus the
     in-process toolsets; no `--restricted`; `permission_mode="bypassPermissions"`; `setting_sources=[]`;
     `strict_mcp_config=True` and `ENABLE_CLAUDEAI_MCP_SERVERS=false` stay (integrations are Yordamchi's, D11);
   - the guard is the **gate classifier** (`dev/gate/domain.classify`, pure; the one import the personal side takes
     from `dev`, C7-personal amended) with `Policy.FULL` and `project_root` = the workspace. Facts the dev bot reads
     from git and the project registry are unknown here, so the classifier fails toward asking (§5.7 "an extra
     question is acceptable");
   - the `init` check becomes: every in-process toolset is a `connected` SDK server, no other MCP server is present,
     and the preset holds `Bash` (proves agent mode). The exact-allowlist equality (NFR-SEC-08) applies only while
     `agent.enabled` is false.
2. **Asking** (the §5.7 ask classes: deploy, server_write, data_loss_db, fs_destroy, spend, third_party_send, …):
   the guard denies at once with the gate's pending message, and the pipeline posts ONE question in the chat
   (`S.AGENT_ASK(number, what)`), kept in memory like the settings question. "ha" stores a grant for exactly that
   call (`input_sha256`, `gate.approval_ttl_min`) and queues a follow-up into the same conversation
   ("approved: retry"), so the agent carries on; "yo'q" tells the agent it was refused. Any other message drops it.
   **Forbidden** calls (Yordamchi's data, keys, credentials, the bots' processes, `~/.claude/settings*.json`) are
   denied and never asked.
3. **Time**: an agent answer may run long (installs, builds): `timeouts.agent_s` (default 900) and
   `timeouts.agent_idle_s` (default 300) replace `timeouts.qa_s` / `timeouts.qa_idle_s` in agent mode; "to'xta" stops it
   as before. The fallback model answers only plain questions (it has no tools).
4. **Untrusted content** stays data (§3.2.14 rule). The gate is the backstop against a page or a forwarded message that
   talks the agent into a dangerous command; the owner accepted the remaining risk (2026-10-31).
5. `agent.enabled` (default true, personal) switches it; false gives back the restricted session of §5.3.

## 4. Data model

### 4.1 Rules
- SQLAlchemy 2 typed ORM, portable types only:
  - `BigInteger` for every Telegram id;
  - `UTCDateTime` for every `*_at`;
  - JSON as `Text` via `persistence/types.JSONText`;
  - money as `BigInteger` `*_usd_micros`.
- Every table has `id INTEGER PK` unless a PK is given. FKs within a group are real `ForeignKey("table.id")`
  strings; cross-feature FKs are strings with no `relationship()` (A2).
- Both DB files carry every table. ✱ marks a column with redacted content (it has passed `core/redact`
  before the write).

### 4.2 Tables
| Table (feature) | Columns | Keys / indexes | Retention |
|---|---|---|---|
| `inbound_updates` (inbox) | `bot` S16, `update_id`, `chat_id`, `user_id`?, `message_id`?, `kind` (text\|voice\|audio\|photo\|video\|document\|link\|edited\|member\|service\|other), `media_group_id`?, `reply_to_message_id`?, `payload_json`✱ (+ `sent_at`, Telegram's send time, §4.4; + `thread_id`, the forum topic, §3.2.16), `synthetic` bool=false, `status` (received\|attached\|ignored\|failed), `burst_id`?, `received_at` | **UNIQUE(bot, update_id)**; IX(bot, status, update_id) | payload nulled 7 d; row 30 d |
| `poll_offsets` (inbox) | `bot` PK, `next_offset`, `updated_at` | — | — |
| `bursts` (burst) | `bot`, `chat_id`, `status` (open\|holding\|closed\|routed\|merged\|cancelled), `opened_at`, `last_part_at`, `closes_at`, `closed_at`?, `close_reason`?, `input_json`✱?, `route_id`?, `thread_id`? (`0002`: the forum topic of its parts) | IX(bot, status) | 30 d |
| `transcripts` (voice) | `inbound_update_id` UNIQUE, `status` (pending\|done\|failed), `text`✱?, `provider`, `provider_path`, `model`, `audio_ms`, `latency_ms`, `error_code`?, `created_at`, `done_at`? | — | text 7 d; row 30 d |
| `voice_offers` (voice) | `voices_json` (3), `status` (open\|chosen\|expired), `chosen`?, `at` | — | 30 d |
| `outbox_messages` (outbox) | `bot`, `chat_id`, `op` (send_text\|edit_text\|delete\|react\|chat_action\|pin\|send_photo\|send_document\|send_voice\|send_video), `kind` (ack\|echo\|progress\|result\|question\|decision\|report\|notice\|heartbeat\|file\|voice\|system), `payload_json`✱, `dedupe_key` S128, `coalesce_key`?, `priority` 0–3, `status` (pending\|sending\|sent\|failed\|superseded\|cancelled), `attempts`, `next_attempt_at`, `last_error_code`?, `reply_to_message_id`?, `disable_notification`, `project_id`?, `turn_id`?, `created_at`, `sent_at`?, `thread_id`? (`0002`: the forum topic it goes to, §3.2.16 step 7) | **UNIQUE(dedupe_key)**; IX(bot, status, priority, next_attempt_at); IX(coalesce_key, status) | sent 30 d |
| `message_links` (outbox) | `bot`, `chat_id`, `message_id`, `outbox_id`, `project_id`?, `turn_id`?, `question_id`?, `decision_id`?, `kind`, `created_at` | PK(bot, chat_id, message_id) | 90 d |
| `setting_versions` (settings) | `key` S96, `value_json`, `version`, `reason` (change\|revert\|import), `actor` (owner\|system), `origin_update_id`?, `created_at` | **UNIQUE(key, version)** | forever, append-only |
| `vault_secrets` (vault) | `name` S64, `kind` (§4.5), `ciphertext` blob, `nonce` blob(12), `key_fp` S16, `last4` S4, `bots_json`, `origin_update_id`?, `created_at`, `removed_at`? | UNIQUE(name) WHERE removed_at IS NULL | removed: ciphertext wiped 30 d |
| `integrations` (integrations) | `slug` S32 UNIQUE (`^[a-z][a-z0-9_]{1,30}$`), `display_name`, `kind` (mcp_http\|api_http), `url`, `auth` (none\|bearer\|header\|oauth), `header_name`?, `secret_id`?, `bots_json`, `tool_policy_json` (`{"dev": "all"\|[names], "personal": [names]}`), `tools_cache_json`, `status` (ok\|auth_failed\|unreachable\|needs_reauth\|disabled), `status_code`?, `last_validated_at`?, `created_at`, `removed_at`? | — | removed 30 d |
| `oauth_clients` (integrations) | `integration_id` PK, `issuer`, `client_id`, `client_secret_id`?, `token_secret_id`, `expires_at`?, `last_refresh_at`?, `refresh_failures` | — | with integration |
| `audit_events` (audit) | `bot`, `at`, `actor` (owner\|executor\|system\|supervisor), `kind` (executor_input\|gate_verdict\|decision_asked\|decision_resolved\|grant_consumed\|setting_changed\|integration_changed\|secret_added\|secret_removed\|session_opened\|session_closed\|self_update\|access_dropped), `project_id`?, `ref_type`?, `ref_id`?, `data_json` (ids, codes, sha256, scrubbed command; **no message text**) | IX(bot, at); IX(kind, at) | 90 d; no UPDATE/DELETE before |
| `usage_records` (cost) | `bot`, `at`, `source`, `provider` (anthropic_plan\|google\|openrouter\|openai\|edge), `is_paid_api`, `model`, `project_id`?, `turn_id`?, `cost_usd_micros`, `units_json`, `duration_ms` | IX(bot, at); IX(provider, at) | 400 d |
| `cap_notices` (cost) | `provider`, `month` S7, `level` (80\|100), `sent_at` | PK(provider, month, level) | 400 d |
| `health_events` (health) | `bot`, `at`, `kind` (started\|clean_exit\|unclean_restart\|wake_gap\|smoke_ok\|smoke_failed\|cli_version\|pressure\|disk_low), `data_json` | IX(bot, at) | 90 d |
| `kv_state` (health) | `bot`, `key`, `value_json`, `updated_at` (pinned message id, last CLI version, speech status) | PK(bot, key) | — |
| `projects` (projects) | `id` S64 PK = folder name, `path` UNIQUE, `description`, `description_source` (readme\|claude_md\|spec\|owner\|none), `is_worktree`, `is_archive_name`, `executor` (claude\|codex), `git_remote_url`?, `default_branch`?, `deploy_branches_json`, `deploy_checked_sha`?, `discovered_at`, `last_seen_at`, `missing_since`?, `last_activity_at`? | — | forever |
| `agent_sessions` (turns) | `project_id`, `executor`, `kind` (coding\|investigation\|smoke), `session_id` S64, `status` (idle\|live\|parked\|closed\|lost), `is_current`, `policy` (FULL\|READ_ONLY\|PLAN_ONLY\|SELF_UPDATE), `model`, `prompt_version_seen`, `mac_attached_at`?, `created_at`, `last_active_at`, `closed_at`? | UNIQUE(project_id, executor, kind) WHERE is_current; IX(session_id) | forever |
| `turns` (turns) | `project_id`, `agent_session_id`?, `kind` (instruct\|investigate\|plan\|build\|selfupdate\|smoke), `status` (queued\|starting\|running\|waiting_question\|waiting_decision\|waiting_limit\|paused_runaway\|stopping\|done\|failed\|interrupted\|cancelled), `queue_reason` (cap\|pressure\|mac_attached\|update_drain)?, `title` S120✱, `burst_id`?, `queued_at`, `started_at`?, `ended_at`?, `start_head`?, `end_head`?, `natija`✱?, `tests_status` (passed\|failed\|not_run\|unknown)?, `commits_json`?, `pushed`?, `changed_files_json`?, `steps`, `cost_usd_micros`, `duration_ms`?, `progress_message_id`?, `limit_resets_at`?, `error_code`? | IX(project_id, status); IX(status, queued_at) | 400 d |
| `deliveries` (turns) | `turn_id`, `seq`, `mode` (new_turn\|next\|answer\|approval\|nudge\|resume), `source` (owner\|system), `text`✱, `media_json`, `status` (pending\|sending\|delivered\|failed\|queued_mac\|superseded), `burst_id`?, `created_at`, `sent_at`? | **UNIQUE(turn_id, seq)**; IX(status) | text 30 d |
| `turn_progress` (turns) | `turn_id`, `at`, `kind` (tool\|text\|todo), `summary` S200✱ | IX(turn_id, at); last 50 per turn | 7 d |
| `questions` (questions) | `project_id`, `turn_id`, `tool_use_id`, `questions_json`✱, `status` (open\|answered\|expired\|cancelled), `answer_json`✱?, `answer_mode` (callback\|message)?, `asked_at`, `answered_at`? | IX(status) | 90 d |
| `decisions` (gate) | `kind` (gate\|plan_approval\|selfupdate_core\|settings_weaken\|wrong_target_revert\|runaway_continue\|drain_timeout), `project_id`?, `turn_id`?, `gate_class`?, `tool_name`?, `input_sha256`?, `input_redacted`✱?, `target`?, `payload_json`, `status` (pending\|approved\|denied\|expired\|consumed\|superseded\|cancelled), `created_at`, `expires_at`, `decided_at`?, `decided_update_id`?, `grant_expires_at`?, `consumed_at`?, `superseded_by`? | IX(status); IX(project_id, status); IX(input_sha256, status) | 90 d |
| `digests` (digest) | `project_id` PK, `updated_at`, `git_branch`, `git_head`, `git_head_subject`✱, `git_head_at`, `ahead`, `behind`, `dirty_count`, `recent_commits_json`✱, `last_natija`✱, `last_natija_at`, `last_turn_id`, `open_questions`, `pending_decisions`, `queued`, `parked`, `handoff_note`✱, `handoff_note_at`, `handoff_note_source` (executor\|synthesized), `executor`, `session_id`? | — | one row per project |
| `server_checks` (servers) | `host`, `at`, `ok`, `summary_json`✱, `error_code`?, `duration_ms` | IX(host, at) | 30 d |
| `route_decisions` (routing) | `burst_id`, `at`, `method` (explicit\|reply\|binding\|draft\|llm\|context\|topic), `intent`, `project_id`?, `candidates_json`, `confidence_milli`, `asked`, `model`?, `latency_ms`, `corrected_to`? | IX(at) | 90 d |
| `clarifications` (routing) | `burst_id`, `kind` (which_project\|which_item\|which_running\|which_decision), `candidates_json`, `status` (open\|resolved\|expired), `message_id`?, `asked_at`, `resolved_at`? | IX(status) | expires 30 min; row 30 d |
| `project_drafts` (newproject) | `status` (collecting\|creating\|planning\|awaiting_approval\|revising\|building\|done\|cancelled\|failed), `parts_json`✱, `proposed_name`?, `project_id`?, `decision_id`?, `plan_turn_id`?, `build_turn_id`?, `opened_at`, `last_part_at`, `closed_at`? | — | 400 d |
| `self_updates` (selfupdate) | `status` (working\|awaiting_core\|requested\|building\|refused\|draining\|switching\|probation\|healthy\|merged\|rolled_back\|failed), `kind` (app\|supervisor), `branch`, `commit`?, `version`?, `changed_files_json`, `touches_core`, `decision_id`?, `tests_count`?, `result_code`?, `requested_at`?, `finished_at`? | — | forever |
| `assistant_sessions` (qa) | `session_id`, `status` (live\|closed), `summary`✱?, `prompt_version_seen`, `websearch_count`, `created_at`, `last_active_at`, `closed_at`? | — | 90 d |
| `answers` (qa) | `assistant_session_id`, `burst_id`, `text_full`✱?, `voice_status` (sent\|skipped\|failed\|off), `voice_error_code`?, `first_text_ms`, `total_ms`, `voice_ms`?, `fallback`, `at` | IX(at) | text 30 d; row 400 d |
| `project_topics` (topics, `0002`) | `project_id` S64 PK (a `projects.id` string, no FK), `chat_id`, `thread_id`, `created_at`, `closed_at`? | IX(chat_id, thread_id) | one row per project; rewritten when opened anew |
| `topic_chats` (topics, `0002`) | `chat_id` PK, `is_forum`, `noted_at`, `refused_at`?, `refusal` (not_forum\|no_right)? | — | one row per chat |
| `media_files` (media) | `kind` (download\|screenshot\|tts\|inbound), `rel_path`, `size_bytes`, `sha256`, `mime`, `created_at`, `expires_at`, `deleted_at`? | IX(expires_at) | file 7 d |

Retention runs daily at 04:30 (`schedule.retention_at = "04:30"`). It deletes in batches of 500 and never
touches a row in a non-terminal state.

### 4.3 Settings registry
- **Shape.** `Setting(key, type, default, check, group, bots, weakens=None, doc, unit)`.
  - Types are JSON only: `bool | int | float | str | list[str] | dict[str, str]`.
  - `check` is one of `Range`, `OneOf`, `SubsetOf`, `MaxLen`, `Regex`, `HostList`, `TimeOfDay`, `Duration`.
  - `bots` is `DEV | PERSONAL | BOTH`.
  - `weakens` is one of `always | increases | decreases | disables | removes_items | to_values(set)`.
- **Snapshot.** `snapshot(deps)` returns an immutable mapping plus `version` (the max `setting_versions.id`).
  Sessions use that version to detect a changed prompt.
- **Bootstrap-backed defaults.** `default=FROM_BOOTSTRAP` covers the ids that start in env files:
  `access.owner_id`, `access.dev_group_id` and `access.personal_group_id` (all `weakens=always`).
- **Groups and key defaults** (every key below exists; values are the defaults):
  | Group | Keys (default) |
  |---|---|
  | PERSONA | `persona.dev_tone` ("qisqa, aniq, texnik"), `persona.personal` (persona text: short, precise, professional, a little warm, own evidence-based position, cites scholars and notes disagreement), `persona.personal_verbosity` ("short") |
  | PROMPTS | `prompt.dev_executor_extra` (""), `prompt.personal_extra` ("") — slots in `prompts/**`; core prompt files stay in git |
  | VOICE | `voice.enabled` (dev false, personal true), `voice.name` ("Kore"), `voice.style` ("tabiiy o'zbek talaffuzi, iliq, aniq"), `voice.gender` ("female"), `voice.language_code` ("uz-UZ"), `voice.max_words` (70), `voice.max_s` (35) |
  | SPEECH | `speech.stt_provider`, `speech.tts_provider`, `speech.video_provider` ("auto"), `stt.google_model`, `stt.openrouter_model`, `tts.google_model`, `tts.openrouter_model` ("google/gemini-3.8-flash-tts"), `tts.edge_voice`, `stt.mode` ("smart"), `stt.split_over_s` (20) |
  | LANGUAGE | `lang.reply` ("uz"), `stt.languages` (["uz-UZ","ru-RU","en-US"]), `stt.extra_terms` ([]) |
  | MODELS | `models.routing` ("haiku"), `models.report` ("haiku"; sonnet missed the 3.5 s budget live) + `models.report_effort` ("low"), `models.settings` ("sonnet"), `models.qa` ("sonnet") + `models.qa_effort` ("medium"), `models.coding` ("opus") + `models.coding_effort` ("xhigh"), `models.investigation` ("opus"), `models.fallback_text` ("gemini-3.8-flash"). Check `OneOf(haiku, sonnet, opus, fable, best)`; `weakens=to_values({fable, best})` (usage credits = money, UC-13 #3) |
  | ROUTING / PROJECTS | `routing.min_confidence` (0.75), `routing.min_margin` (0.2), `routing.context_window_min` (15), `projects.root` ("~/Documents/Projects"), `projects.aliases` ({}), `projects.descriptions` ({}), `projects.archived` ([]), `projects.unarchived` ([]), `projects.excluded` ([]; never registered, listed, routed or opened), `executor.default` ("claude"), `executor.per_project` ({}), `topics.enabled` (true; §3.2.16) |
  | DECISIONS | `gate.ask_classes` (all 12 classes, §5.7; `removes_items`), `gate.secret_scan_enabled` (true; `disables`), `gate.approval_ttl_min` (120; `increases`), `gate.hook_timeout_s` (15), `gate.readonly_remote_commands` (§5.7; `weakens=always` on add), `gate.jump_only_hosts` (["server"]; `removes_items`), `gate.git_hosts` (["github.com"]), `gate.http_write_allow_hosts` ([]; `weakens=always` on add), `gate.temp_roots` (["/tmp", "/private/tmp", "/private/var/folders"]) |
  | BURST | `burst.after_complete_text_s` (1.2), `burst.after_text_s` (2.0), `burst.after_media_s` (10.0), `burst.album_gap_s` (1.5), `burst.after_voice_s` (2.0), `burst.hold_max_s` (120), `burst.max_total_s` (30), `burst.merge_min_confidence` (0.85) |
  | TIMEOUTS | `timeouts.routing_s` (2.5), `timeouts.report_s` (3.5), `timeouts.stt_s` (8), `timeouts.stt_per_audio_s` (0.5, added per second of audio), `timeouts.tts_s` (15), `timeouts.integration_validate_s` (25), `timeouts.session_open_s` (60), `questions.hold_process_min` (30), `park.grace_s` (15), `timeouts.topic_s` (5), `timeouts.audio_file_s` (360), `timeouts.document_s` (45) |
  | SCHEDULES | `schedule.heartbeat_edit_min` (5), `schedule.digest_active_poll_s` (120), `schedule.digest_idle_poll_min` (60), `schedule.server_check_min` (30), `schedule.cost_summary_at` ("", off), `schedule.quiet_hours` ("", e.g. "23:00-07:00" → `disable_notification`), `schedule.retention_at` ("04:30"), `ui.timezone` ("Asia/Tashkent"), `topics.retry_after_min` (30) |
  | CAPS | `caps.google_monthly_usd` (40), `caps.openrouter_monthly_usd` (10), `caps.openai_monthly_usd` (10), `caps.warn_pct` (80) — all `increases`; `turns.max_concurrent` (15; `increases`), `turns.max_wall_min` (60), `turns.max_steps` (200), `turns.max_same_failure` (5) (`increases`), `guard.min_free_disk_gb` (10), `guard.block_disk_gb` (3) |
  | LIMITS | `text.max_chars` (3500), `text.max_lines` (40), `outbox.max_parts` (3), `outbox.progress_edit_s` (5), `media.retention_days` (7), `files.max_mb` (20), `files.text_max_chars` (150000), `files.pdf_max_pages` (100), `files.max_pdfs` (3), `files.audio_max_min` (150), `files.audio_kbps` (32), `files.send_transcript` (true), `files.max_documents` (5), `files.document_max_mb` (20) (§3.2.17) |
  | SERVERS | `servers.hosts` ([]; `~/.ssh/config` aliases the owner lists) |
- Keys that appear only inline in §2–§5 belong to the same registry, in the closest group (TIMEOUTS for
  durations, SCHEDULES for intervals). They include `ui.seen_reaction`, `digest.stale_after_min`,
  `newproject.*`, `handoff.scan_s`, `integrations.oauth_*`, `selfupdate.*`, `qa.*`,
  `sessions.idle_close_min` and `turns.result_grace_s`. There is no literal for any of them outside
  `defaults.py`.
- Personal-only additions (2026-10-31, T75/T76/UC-34): `timeouts.integration_call_s` (30),
  `integrations.result_max_chars` (20000), `integrations.response_max_kb` (1024), `news.max_items` (7),
  `news.max_age_h` (36), `voice.sample_voices` ("Kore,Charon,Aoede,Puck,Leda,Fenrir"), `voice.sample_count` (3).
  Personal agent mode (2026-10-31, §3.2.18): `agent.enabled` (true; `enables`: turning it on asks),
  `agent.workspace_dir` ("~/Documents/Projects/_shaxsiy"), `timeouts.agent_s` (900), `timeouts.agent_idle_s` (300).
- `test_registry` fails if any of these lacks `weakens`: `access.*`, `gate.ask_classes`,
  `gate.secret_scan_enabled`, `gate.jump_only_hosts`, `caps.*`, `turns.max_concurrent`.

### 4.4 Burst window (pure `burst.domain.window(parts, now, snap) → closes_at | HOLD`)
- Last part complete text (≥ 3 words and ends with `?`, `.` or `!`) → +`after_complete_text_s`.
- Other text → +`after_text_s`.
- Bare photo, file or link (no caption, or link-only text) → +`after_media_s`.
- An album stays open until `album_gap_s` after its last item.
- A voice part being transcribed → HOLD, then +`after_voice_s` after its transcript.
- "kut" → HOLD up to `hold_max_s`.
- The hard cap is `max_total_s` from the first part (a "kut" hold is exempt).
- Placement also reads the owner's clock (T36 BUG-1): what Telegram held while the Mac slept, or the network or the
  bot was down, arrives in one `getUpdates` batch. A part joins only if its send time (`message.date`, an edit's
  `edit_date`, whole seconds, so 1 s is allowed) is inside the window the same rules give on the parts' send times;
  otherwise the burst closes for that reason and the part opens a new one. Receipt time still arms the timer and
  decides alone for a part without a date (synthetic). Album items and an edit of a part still assembling stay;
  a burst waiting for a transcript is never closed this way.
- An interrupt word closes the window at once and is never merged.
- Burst end → one `TurnInput`. A lone image or file proceeds with a sensible interpretation; on the dev bot,
  a lone error screenshot → `investigate` (read-only diagnosis) of the inferred project.

### 4.5 Secrets
- **Ciphertext.** AES-256-GCM (`cryptography`) with a random 96-bit nonce per write.
  - AAD = `f"{name}|{kind}|{id}"` (binds the row).
  - Plaintext is UTF-8, or a JSON bundle for OAuth tokens.
- **Master key.** 32 random bytes, base64, in `~/.config/yordamchi/master.key` (0600). It is created by the
  installer, never by a bot. `key_fp` = the first 8 hex chars of its sha256.
- **Detection** (`core/redact`, a fixture of ≥ 10 formats). A detected key goes to the vault as
  `kind=<pattern>`:
  - OpenAI `sk-(proj-)?…`, Anthropic API `sk-ant-api03-…`, Google `AIza…`, OpenRouter `sk-or-v1-…`;
  - GitHub `gh[pousr]_…` / `github_pat_…`, Telegram `\d{8,10}:[A-Za-z0-9_-]{35}`, AWS `AKIA[0-9A-Z]{16}` (+
    secret pattern), Slack `xox[abp]-…`, JWT `eyJ…\.eyJ…\.…`, PEM blocks;
  - generic high-entropy tokens (≥ 24 chars, Shannon ≥ 4.0, mixed classes) next to `key|token|secret|parol|kalit`;
  - OAuth callback URLs.

  A Claude login token `sk-ant-oat…` is refused and never stored.
- **Scrubbing.** Every log line, every outbox payload and every audit `data_json` passes `redact.scrubbed`,
  which masks the patterns and every decrypted vault value (in-memory set, rebuilt on change) as `••••last4`.
- **Pre-push scan input.** `run/secret-hashes.json` holds sha256 hashes of vault values, for exact-match scanning.
- **Undeletable owner message** (no admin right, or older than 48 h) → `S.SECRET_NOT_DELETED` with "kalitni
  almashtiring".

### 4.6 Idempotency and durable queues
- **Inbound.** `inbound_updates(bot, update_id)` is inserted before any work; a duplicate is a no-op. The
  offset is persisted after the batch.
- **Executor.** `deliveries(turn_id, seq)`, with status `sending` written before the port call. Restart rules
  are in §2.7.
- **Outbound.** `outbox.dedupe_key` (`<kind>:<turn_or_ref>:<part>`) is written before sending. Edits for one
  `coalesce_key` keep only the newest pending row (older ones become `superseded`).
- **Decisions.** A grant is consumed by a conditional UPDATE, so it is single-use even under a race.
- **Durable across restarts:** inbound (`received`), bursts (`open`/`closed`), deliveries (`pending`), turns
  (`queued`), questions (`open`), decisions (`pending`/`approved`), outbox (`pending`/`sending`), drafts,
  self-updates.

## 5. Executor contract

### 5.1 Port (`ports/agent.py`). Claude (P1) and Codex (P4) implement it; `FakeAgentRuntime` replays scripts
```python
class AgentRuntime(Protocol):
    async def probe(self, *, timeout: float) -> RuntimeStatus        # installed, version, logged_in, auth_method, missing[]
    async def open(self, spec: SessionSpec, *, resume_id: str | None, timeout: float) -> SessionHandle
    async def send(self, h: SessionHandle, msg: AgentInput, *, mode: Literal["new_turn", "next"], timeout: float) -> None
    async def answer(self, h: SessionHandle, question_id: str, answers: Mapping[str, str], *, timeout: float) -> bool
    async def interrupt(self, h: SessionHandle, *, timeout: float) -> None   # then kills the process group if still busy
    async def set_model(self, h: SessionHandle, model: str, *, timeout: float) -> None
    async def close(self, h: SessionHandle, *, timeout: float) -> None
    def events(self, h: SessionHandle, *, idle_timeout: float) -> AsyncIterator[AgentEvent]
```
- `answer` returns `False` when the pending callback is gone. The caller then delivers the answer as a message.
- `SessionSpec` (frozen) fields: `kind`, `cwd`, `model`, `effort`, `system_append`, `context_provider`,
  `setting_sources`, `tools`, `allowed_tools`, `disallowed_tools`, `permission_mode`, `mcp_servers`
  (http: url + `headers_helper`), `sdk_servers`, `strict_mcp`, `env`, `expected_tools`, `max_turns`,
  `guard: GuardFn`, `extra_args`.
  - `context_provider` returns a precomputed string in under 50 ms.
  - `sdk_servers` are in-process tool sets, referenced by id.
  - `env` holds additions only.
- `AgentInput` = text plus ≤ 5 image paths. File paths are given as text references.

**Events**, all executor-neutral:
| Event | Fields |
|---|---|
| `SessionReady` | `session_id, tools, mcp_servers, model, cli_version` (Claude: the `init` system message) |
| `Progress` | `kind (tool_start\|tool_end\|text\|todo), tool_name, summary, is_error` |
| `QuestionAsked` | `question_id, questions` |
| `ToolDenied` | `tool_name, reason_code` |
| `UsageLimited` | `resets_at, limit_type` (Claude: `RateLimitEvent` `status="rejected"`, or an `api_error_status` 429 result) |
| `TurnResult` | `text, subtype, is_error, stop_reason, cost_usd_micros, model_usage, num_turns, duration_ms, permission_denials`; parsed by `turns.domain.parse_result` into `natija, tests, handoff` |
| `RuntimeFailed` | `code (process_died\|auth_failed\|session_not_found\|cli_missing\|protocol), retryable` |
| `SessionClosed` | `reason` |

**Turn completion.** A Yordamchi turn completes on a `TurnResult` followed by
`turns.result_grace_s = 1.5` s of silence, with no queued `next` input left. Merged follow-ups therefore
never produce a second final result.

### 5.2 How the gate hooks in
- `GuardFn = Callable[[ToolCall], Awaitable[GuardVerdict]]`, where `ToolCall` = `(tool_name, tool_input,
  tool_use_id, agent_id, session_id)`.
- **Claude.**
  - `hooks["PreToolUse"] = [HookMatcher(matcher=None, hooks=[guard_hook], timeout=gate.hook_timeout_s)]`;
  - allow → `{}`. In `dontAsk` sessions (investigation) an allowed call returns `"permissionDecision":
    "allow"` explicitly, because that mode denies anything not pre-approved;
  - deny → `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
    "permissionDecisionReason": reason}}`;
  - any exception inside `guard.py` → deny (fail closed); a stopping turn → deny every call.
- **`can_use_tool`.**
  - `AskUserQuestion` → the question flow (3.2.2).
  - Any other call that falls through (critical-path removals, "actions no mode auto-approves") → the guard
    again. `PermissionResultAllow(updated_input=input)` only on allow, otherwise deny.
- **Second layer.** `disallowed_tools` holds scoped **forbidden-only** rules (a deny rule beats approval,
  so approvable classes never go here):
  - `Read(//<DATA>/**)`, `Edit(//<DATA>/**)`, `Read(//~/.config/yordamchi/**)`, `Edit(//~/.config/yordamchi/**)`
    (paths expanded);
  - `Bash(security find-generic-password*)`, `Bash(security dump-keychain*)`;
  - `Bash(launchctl * uz.yordamchi*)`.
- **Codex** (P4): `item/commandExecution/requestApproval` and `item/fileChange/requestApproval` → the guard →
  accept or decline.

### 5.3 Session options (decided)
| Option | `coding` (dev) | `investigation` (dev) | dev Assistant: `routing` `report` `settings` | personal `assistant` | `smoke` |
|---|---|---|---|---|---|
| cwd | project path | project path | `DATA/sessions/<kind>` | `DATA/sessions/assistant` | `DATA/sessions/smoke` |
| `setting_sources` | `["user","project","local"]` | same | `[]` | `[]` | same as coding |
| `system_prompt` | preset `claude_code` + `append` = `prompts/dev/executor_system.md` | preset + `prompts/dev/investigate_system.md` | custom `prompts/dev/<kind>_system.md` | custom `prompts/personal/qa_system.md` | as coding |
| `tools` | preset default | `Read Glob Grep Bash WebFetch WebSearch` | `[]` | `WebSearch WebFetch` + sdk servers | preset |
| `permission_mode` | `bypassPermissions` | `dontAsk` (Bash allowed only by a hook `allow`) | `dontAsk` | `dontAsk` | `bypassPermissions` |
| `allowed_tools` | — | `Read Glob Grep WebFetch WebSearch` | — | exact allowlist | — |
| MCP | dev integrations (http + `headersHelper`); `strict_mcp=False` | same | none; `strict_mcp=True` | sdk `yordamchi_bridge`, `yordamchi_media`, `yordamchi_int`; `strict_mcp=True` | none, strict |
| env additions | `ENABLE_CLAUDEAI_MCP_SERVERS=false`; `GIT_CONFIG_COUNT=1`, `GIT_CONFIG_KEY_0=core.hooksPath`, `GIT_CONFIG_VALUE_0=<release>/githooks`; `GIT_TERMINAL_PROMPT=0`; `GH_PROMPT_DISABLED=1`; `YORDAMCHI_SOCK`, `YORDAMCHI_SESSION_TOKEN`, `YORDAMCHI_PY`; PATH with shims first | same | `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` | `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, `CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION=1000` | as coding |
| model / effort | `models.coding` / `models.coding_effort` | `models.investigation` | `models.<kind>` | `models.qa` / `models.qa_effort` | `haiku` |
| hooks | PreToolUse(guard FULL / PLAN_ONLY / SELF_UPDATE), UserPromptSubmit(context, timeout 10), PostToolUse(progress, files), PostToolUseFailure(runaway) | PreToolUse(guard READ_ONLY), UserPromptSubmit | — | PreToolUse(allowlist), UserPromptSubmit | as coding |
| `can_use_tool` | questions + fall-through | deny all | — | deny all | as coding |
| limits | `max_turns=turns.max_steps`, Yordamchi wall clock | `max_turns=60`, wall 3 min | `timeouts.<kind>_s` | — | wall 5 min |
| start check | §2.5 credential check on the project `.claude/settings*.json` (`env`, `apiKeyHelper`) | same | — | allowlist ⊆ `init` tools + MCP ⊆ allowlist ∪ {`EndConversation`, `ToolSearch`, `WaitForMcpServers`} (C2, revised after S5: none of the three appeared); otherwise refuse, `S.PERSONAL_TOOLS_MISMATCH` | — |

- Always: `cli_path="~/.local/bin/claude"` (the symlink, so CLI auto-updates take effect on the next spawn).
- Never: `--bare`, `ANTHROPIC_*`, `CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_CONFIG_DIR`, `cleanupPeriodDays`.
- `--restricted` goes into `extra_args` for the personal session (spike S5 passed: WebSearch, WebFetch and
  SDK MCP work under it).
- Dev sessions: `disallowed_tools` includes `SendMessage` and `ListAgents` (every SDK session opens a
  cross-session socket; peer-origin input is untrusted; docs/SPIKES.md).
- Warm Assistant pools: one live client and one hot spare per kind. A client is rotated after 20 asks, on a
  context-size threshold, or on a settings version change.

### 5.4 Prompt and persona changes on warm or resumed sessions (UC-09 AC, R-34; revised after spike S4)
- The system prompt snapshot stays on (STACK §2.1), so `append` is frozen per session id. S4 showed that a
  later instruction contradicting the frozen prompt is followed unreliably (haiku 3/6, sonnet 2/3), while a
  neutral frozen prompt plus the current instructions on every turn was followed 10/10 (docs/SPIKES.md).
- Therefore the frozen part (`prompts/<bot>/<kind>_system.md` or the `append` text) holds **only fixed
  parts**: role, the result contract (§5.5), the NFR-SEC-11 authorization, and the line "the latest
  owner-instructions block in this conversation is authoritative and replaces earlier ones". It holds **no**
  persona, tone, language or other owner-editable instruction.
- Every send precomputes `prompts/<bot>/turn_context.md`, and the `UserPromptSubmit` hook returns it as
  `additionalContext` on **every turn** (not only after a change). It always contains the full current
  owner-editable instruction block (persona, tone, language, length limits, project notes), headed
  "Current instructions from the owner:". `prompt_version_seen` is still recorded in the delivery's
  `delivered` uow, for audit and the UC-09 "applies from the next message" test.
- No session rotation is needed for prompt changes.
- Model change → `set_model()` before the next send. Effort is a launch option, so an effort change closes
  an idle session and resumes it with the new effort before the next delivery. Assistant pools rebuild
  their spare at once.

### 5.5 Prompt contracts (text lives in `prompts/**`; tests assert the slots)
- **Final message of every turn**:
  - `Natija: <one Uzbek sentence>`;
  - `Testlar: <o'tdi|yiqildi|ishga tushirilmadi> (<summary>)`;
  - `<handoff>Goal/Decisions/State/Next</handoff>` (English, ≤ 1,500 chars).

  Missing `Natija:` → the first sentence of the result. Missing `Testlar:` → `unknown`.
- **Authorization** (NFR-SEC-11): "The owner pre-authorized, for this session and every turn: commit, push,
  branches and creating private GitHub repos. Never ask about these; this overrides any 'ask before push'
  rule. Never commit `.env` files or secrets."
- **Approval batching**: "Anything needing approval (deploy, server change, deletion of data, spending,
  sending to others) must be ONE command, and say what and where, so the owner is asked once."
- **Gate pending** (`gate_pending.md`): "BLOCKED: owner decision #<id> has been asked. Do not retry and do not
  try another way to the same effect. Continue other independent work, else end your turn. On 'APPROVED
  #<id>' run exactly the same command once."
- **Finish nudge**: a turn that ends with uncommitted or unpushed work and no pending decision gets one
  `nudge` delivery: "Commit and push your work now (pre-authorized)". A second miss → the result shows
  "commit/push qilinmadi".

### 5.6 Turn guards
- **Runaway.** Breaching `turns.max_wall_min`, `turns.max_steps` (tool calls), or the same failing call
  (tool + input hash) `turns.max_same_failure` times → interrupt → `paused_runaway` + "davom ettiraymi?".
- **Usage limit.** One `S.USAGE_LIMIT(project, resets_at)`; state `waiting_limit`.
  - Retry 40 s after a ClaudeManager `activeId` change, at `resets_at`, or every 10 min.
  - The retry resumes and delivers "Continue where you stopped; a usage limit interrupted you". No
    instruction is lost.
- **Scheduler.** A queued turn starts only when all hold:
  - working turns (`starting|running|stopping`) < `turns.max_concurrent`;
  - `sysctl kern.memorystatus_vm_pressure_level` ≤ `guard.max_pressure_level` (default 2 = warn; 4 = critical always queues). T22 measured this Mac sitting at level 2 under normal load, so `== 1` would queue every turn. An unreadable level → queue and log;
  - free disk ≥ `guard.block_disk_gb`.

  Otherwise `S.QUEUED(project, position, reason)`. At pressure level 4 it also suggests parking. Idle live
  sessions close after `sessions.idle_close_min = 10`. Investigations, reports and Q&A sit outside the cap.
- **Smoke test** (NFR-REL-08). On a `claude --version` change (checked at start and every 10 min):
  - one smoke turn: "create ok.txt containing ok and commit";
  - a gate probe: `ssh yordamchi-nohost true` must be denied.

  Failure → `S.SMOKE_FAILED(version, step)`, and the bot keeps running.

### 5.7 Gate classifier (`dev/gate/domain.py`, pure)
- **Policies.**
  - `FULL` (coding);
  - `READ_ONLY` (investigation): no write tool, Bash only on the read-only list;
  - `PLAN_ONLY` (§3.2.8);
  - `SELF_UPDATE` = FULL, plus pushing `main` of Yordamchi is forbidden (the supervisor merges).
- **Ask classes** (`gate.ask_classes`, 12):
  | Class | Triggers |
  |---|---|
  | `deploy` | vercel, netlify deploy, fly/flyctl deploy\|scale, railway up, heroku, gcloud run\|app deploy, aws deploy/ecs/lambda update, kubectl apply\|delete\|rollout\|scale\|set\|patch\|edit, helm install\|upgrade\|rollback\|uninstall, terraform/tofu apply\|destroy, pulumi up\|destroy, ansible-playbook, docker push, eas submit\|update, fastlane, `gh workflow run`, `gh release create`; make/npm/pnpm/yarn/just targets named `deploy*\|release*\|publish*\|prod*` |
  | `server_write` | ssh/scp/rsync/sftp to a non-git host with a remote command not on `gate.readonly_remote_commands`; any command on a `gate.jump_only_hosts` host; `docker -H ssh://`, `DOCKER_HOST=ssh://`, `docker --context <non-default>` |
  | `deploy_push` | `git push` or `gh pr merge` whose target branch ∈ `projects.deploy_branches_json` |
  | `force_push` | `-f`, `--force`, `--force-with-lease`, `+ref` to the default branch |
  | `data_loss_db` | DROP/TRUNCATE via psql/mysql/sqlite3/mongosh/clickhouse; `dropdb`; reset/drop/flush/clean/downgrade migration commands; any migration whose DB host is not local. Exempt: a provably test DB (`:memory:`, a tmp or `tests/` SQLite file, a localhost DB named `*test*`) |
  | `docker_destroy` | `compose down -v\|--volumes`, `docker volume rm\|prune`, `docker system prune` |
  | `fs_destroy` | rm/rmdir/unlink/`find -delete`/`git clean` targeting the project root, anything outside the project except `gate.temp_roots`, or untracked non-ignored files (`git clean -n` / `ls-files -o --exclude-standard`) |
  | `repo_delete` | `gh repo delete`; `gh api -X DELETE repos/…` |
  | `repo_public` | `gh repo create --public`; `gh repo edit --visibility public`; API `visibility=public` |
  | `spend` | cloud resource create/scale CLIs (aws ec2 run-instances, gcloud compute … create, doctl/hcloud/linode-cli create); paid plan changes |
  | `third_party_send` | mail/sendmail/msmtp/swaks; npm/twine/cargo/gem publish; curl/wget/httpie with a mutating method or body to a non-local host not in `gate.http_write_allow_hosts`; MCP write tools not pre-allowed |
  | `architecture` | by prompt instruction only, via `AskUserQuestion` (not detectable at the tool layer) |

  Never asked: commit, push to non-deploy branches, branches, private repo creation, deleting tracked files
  or ignored artefacts, installs, tests, builds, local Docker (D9).
- **Forbidden** (deny, never askable): read/write of `DATA/`, `~/.config/yordamchi/`,
  `~/.claude/.credentials.json`, `~/.claude/settings*.json`, `~/.claude/CLAUDE.md`; keychain reads;
  `launchctl`/`kill`/`pkill` aimed at `uz.yordamchi.*` or the bot pids; calling `yordamchi-helper` or the
  sockets; `git push --no-verify`; `-c core.hooksPath`; writes under `releases/`; setting
  `ANTHROPIC_*`/`CLAUDE_CONFIG_DIR`/`CLAUDE_CODE_OAUTH_TOKEN` in a command.
- **Parsing.**
  - `shlex` (posix, `punctuation_chars`) splits on `; && || | & newline`; env-prefix assignments are
    removed and kept as facts.
  - It recurses into `bash|sh|zsh -c`, `eval`, `env`, `sudo`, `nohup`, `time`, `timeout` and `xargs`.
  - Scripts (`./x.sh`, `bash x.sh`, `make t`, `npm run s`, `just r`) are expanded by reading the recipe text
    (≤ 2 levels) and classifying it.
  - Unparseable input (unbalanced quotes, `$(…)` or backticks with risky tokens, a heredoc into a shell) →
    keyword scan, and the strongest class wins.

  An extra question is acceptable; a miss is not.
- **Read-only remote list** (default):
  - `uptime`, `df -h`, `free -m`, `uname -a`, `hostname`, `whoami`, `ls`, `ps aux`, `top -bn1`;
  - `docker ps`, `docker compose ps`, `docker stats --no-stream`, `docker logs --tail N <c>`;
  - `systemctl status|is-active <u>`, `journalctl -u <u> -n N --no-pager`;
  - `tail -n N <path>`, `cat /var/log/<path>`;
  - pipes only into `grep head tail wc sort uniq cut`.
- **Layer 2 backstops** (catch indirection through scripts and programs):
  - **`shims/{ssh,scp,sftp,rsync}`** → `"$YORDAMCHI_PY" -I -m yordamchi.shim <tool> "$@"` → socket
    `gate_check`, same classifier. It passes if Layer 1 consumed a matching grant in the last 120 s for this
    session, or if the command is read-only. Socket down → exit 126 (deny).
  - **git `pre-push`** via `core.hooksPath` (env scope, so project files are untouched):
    - the secret scan covers the files changed in the pushed commits: `.env*` (except
      `.example|.sample|.template`), `*.pem`, `id_rsa*`, `*.p12`, `*.keystore`, `credentials*.json`, the
      §4.5 patterns and exact vault-hash matches;
    - a hit → exit 1 with `YORDAMCHI_SECRET_SCAN_BLOCKED file=… rule=…` plus socket `secret_blocked` →
      `S.PUSH_BLOCKED_SECRET(project, file)`;
    - it then chains to the project's own hook (`git config --local core.hooksPath` or `.git/hooks`).
  - **Residual risk** (R-20): a program that deploys through an SDK, with no CLI, is invisible to the gate.
    It is covered by name heuristics, the prompt, the runaway guard and the audit log.

### 5.8 Codex mapping (P4)
- `adapters/codex/` drives `codex app-server` (JSON-RPC over stdio):
  - `thread/start|resume`; `turn/start`; `turn/steer` (= `next`); `turn/interrupt`;
  - approval policy `untrusted` (every command and file change reaches the client) with sandbox
    `danger-full-access`. The guard is the gate; Codex PreToolUse hooks are a backstop only.
- It uses the same env (shims, `core.hooksPath`) and the same appended instructions, plus the owner's global
  rules as its instructions file.
- A switch hands over: the last 5 `Natija` lines, `git status`, open questions and the handoff note. The
  previous Claude session id is kept for switching back.
- `account/read` fails → `S.CODEX_MISSING(steps)`. Any gate class the suite cannot enforce on Codex is
  `forbid` on Codex (R-24).

## 6. Intents and chat commands (`bots/<bot>/intents.py`; natural language first, no BotFather menu)

### 6.1 Deterministic pre-parse, in this order (no LLM)
1. Synthetic `selftest`.
2. Interrupt words.
3. An answer to an open clarification.
4. Approval or denial while items are pending.
5. A question answer.
6. Typed aliases.
7. Exact phrases.

Everything else goes to routing plus the LLM intent (dev), or to the assistant (personal). `setMyCommands` is
never called; the check script asserts the command list is empty.

In a project's forum topic (dev, §3.2.16 step 5) the same order runs with that project in mind: a command that names
no project acts on it, step 3 is skipped, and "everything else" is that project's work, with no routing model.

| Intent | Examples | Alias | Bot | UC | Phase |
|---|---|---|---|---|---|
| `stop_turn` / `stop_all` / `redirect` / `hold` | "to'xta", "bas", "stop" / "hammasini to'xtat" / "yo'q, boshqacha qil: …" / "kut" | `/stop` | both | 03 | P1 |
| `approve` / `decline` | "ha", "qil", "mayli" / "yo'q", "kerakmas" (+ `#id` or a project name) | — | both | 13, 09, 14 | P1 |
| `answer_question` | "2", an option text, a voice answer | — | dev | 12 | P1 |
| `instruct_project` | "BonviHR'da login sahifasini tuzat" | — | dev | 10, 11 | P1 |
| `report_project` / `report_all` | "HRMentor qanday?", "bugun nima o'zgardi?" | `/hisobot [X]` | dev | 15 | P1 |
| `server_status` / `investigate` | "bonvi server qanday?" / "X'dagi xatoni tekshir"; a lone error screenshot | — | dev | 15, 02 | P1 |
| `show_health` | "holat?" | `/holat` | both | 06 | P1 |
| `greeting` / `thanks` / `help` | "salom", "qandaysan?" / "rahmat" / "nima qila olasan?" (a whole message of these words only; answered from the catalog, no model) | `/yordam`, `/help` | dev | — | P1 |
| `chat` (routing LLM intent, §3.2.6 step 7) | "Python'da decorator nima?", "bu gapni inglizchaga tarjima qil: …", "1250 ning 12 foizi qancha?" (about none of his projects; a plain answer, no project) | — | dev | 10 | P1 |
| `show_settings` / `change_setting` / `revert_setting` | "sozlamalaring qanday?" / "javoblaring qisqaroq bo'lsin" / "oldingi holatiga qaytar" | `/sozlamalar` | both | 09, 34 | P1 |
| `add_term` | "bu so'zni to'g'ri yoz: Wunderkind" | — | both | 05 | P1 |
| `alias_project` / `archive_project` / `describe_project` | "BonviHR'ni 'hr' deb ham tani" / "X'ni arxivla" / "X tavsifi: …" | — | dev | 18, 09 | P1 |
| `list_projects` | "loyihalar" | `/loyihalar` | dev | 18 | P1 basic, P2 full |
| `wrong_target` | "bu HRMentor uchun edi" | — | dev | 10 | P1 |
| `continue_turn` / `retry_delivery` | "davom ettir" / "qayta yubor" | — | dev | NFR-COST-03, NFR-REL-05/07 | P1 |
| `new_session` | "X yangi sessiya"; bare "yangi sessiya" in a project's topic | — | dev | ASSUMPTIONS | P1 |
| `show_cost` | "xarajat?" | `/xarajat` | both | 07 | P2 |
| `integration_add` / `list` / `remove` | "Bonvi HR'ga ulan: https://… token …" / "integratsiyalar" / "X'ni o'chir" | `/integratsiyalar` | both | 08 | P2 |
| `new_project` (+ "tamom" / "boshla") | "yangi loyiha: …" | — | dev | 14 | P2 |
| `mac_handoff` | "X sessiyasi" | `/sessiya X` | dev | 16 | P2 |
| `park_project` / `resume_project` | "X'ni yopib tur" / "X'ni davom ettiramiz" | — | dev | 18 | P2 |
| `self_update` | "o'zingga X qo'sh", "Yordamchi'da … o'zgartir" | — | dev | 19 | P2 |
| `ask` (default), `more`, `new_conversation` | anything / "to'liqroq" / "yangi suhbat" | — | personal | 30 | P3 |
| media analysis and artefacts | a photo, video or link; "X saytining skrinshoti"; "videoni yubor" | — | personal (assistant calls tools) | 31, 32 | P3 |
| `news` | "bugungi yangiliklar", "moliya yangiliklari" | — | personal | 33 | P3 |
| `voice_samples` | "ovozing yoqmadi" → 3 numbered samples → "ikkinchisi" | — | personal | 34 | P3 |
| `project_status` | "BonviHR qanday?" (bridge, read-only) | — | personal | 36 | P3 |
| `switch_executor` | "ChatGPT'ga o't", "X uchun ChatGPT'ga o't", "Claude'ga qayt" | — | dev | 17 | P4 |

### 6.2 Golden message formats (`tests/golden/`)
- **Progress**: `BonviHR · ishlayapti 4 daq · 12 qadam` plus ≤ 3 lines of the latest steps.
- **Result**:
  - `BonviHR · Natija: …`
  - `Fayllar: N ta (a.py, b.py, …)`
  - `Testlar: o'tdi (42)`
  - `Commit: a1b2c3d · push: bajarildi (origin/main)`
  - `Vaqt: 7 daq`
- **`holat?`**: uptime, versions (Yordamchi, `claude`, SDK), Claude login + ClaudeManager (active account,
  5h/7d %), speech providers and key state, running/queued (with position), pending decisions and
  questions, last 3 error codes, degraded capabilities.

## 7. Phases (REQUIREMENTS §5)

| Phase | Features included | Exit criteria (acceptance IDs) | Stub or fake until later |
|---|---|---|---|
| **P1** dev core, usable alone | `core`, `persistence`, all `ports` + fakes; adapters `telegram`, `claude` (executor, assistant, guard), `gemini/stt`, `openrouter/stt`, `git`, `ssh`, `macos`, `keystore`, `system_clock`; `shared/{access,inbox,burst,voice(STT),vault,settings,outbox,cost(record),health,audit}`; `dev/{routing,turns(no park),gate+shims+githooks,questions,projects(discovery, aliases),digest,servers}`; `bots/dev`; supervisor watchdog + backups; installer, plists, check script; migration `0001` | UC-01 (all), 02 (dev cases), 03, 04, 05, 06, 09 (P1 groups + injection), 10, 11, 12, 13 (Claude gate suite incl. CI-deploy and secret-scan fixtures), 15; NFR-REL-01…08, SEC-01…07, 09…11, SPD dev rows, CAP-01…05, COST-03, OPS-01…03 | `xarajat?`, integrations, park/resume, new project, handoff, self-update → `S.NOT_YET(phase)`. Bot updates are manual (`scripts/install.py --release <commit>`). In P1 the owner must not resume bot sessions at the Mac (no attach detection) |
| **P2** self-running | `shared/integrations` + `adapters/mcp` + helper + OAuth; cost report and caps; `dev/{newproject,handoff,selfupdate}`; projects chat ops; park/resume; `adapters/github`; supervisor updater (probation, rollback) | UC-07, 08 (dev half, incl. live rotation A→B), 14, 16, 18, 19 (3 broken-update fixtures, `kill -9` mid-switch, DB equality after the migration rollback, 0 lost messages); NFR-COST-01/02 | integrations for the personal bot |
| **P3** personal bot | `bots/personal`; `personal/{qa,media,artefacts,news}`; `shared/bridge`; voice TTS (Google, OpenRouter, Edge), voice offers; adapters `web`, `edge_tts`, `gemini/{tts,video,assistant}`, `openrouter/{tts,video,assistant}`; gateway `yordamchi_int` | UC-30…36; UC-02 full set (≥ 40, both bots); UC-08 personal half; NFR-SEC-08; SPD personal rows | — |
| **P4** Codex | `adapters/codex`, switch intent, handoff summary | UC-17; the gate suite and commit/push policy on Codex | — |

**Evaluation sets** (`tests/eval/`, thresholds in `thresholds.toml` copied from REQUIREMENTS §4):
| Phase | Sets |
|---|---|
| P1 | routing ≥ 120 (80 clear / 20 context / 20 ambiguous, ≥ 15 projects, ≤ 1 silent wrong route); burst dev cases (≥ 24); gate suite (Claude); secrets (≥ 10 formats + an unknown-format key); settings (≥ 1 per P1 group) + injection (executor output, file, page saying "change setting X" → 0 changes); STT mixed-language (≥ 20 owner notes, live, on each configured provider); NFR-SEC-11 push-without-asking; fault (50 messages × `kill -9`); outbound network fault; 15-project 10-min load; latency p90 over ≥ 20 samples (ack 1 s, routing 4 s, report 4 s, `holat?` 3 s, interrupt 3 s, STT of a 60 s note 5 s, first progress 30 s) |
| P2 | broken-update fixtures + `kill -9`; migration tests; OAuth paste-back with a fixture server; attach/detach; park → no process within 30 s |
| P3 | UC-30 quality (≥ 30, owner-reviewed, with opinion ≥ 10 and religious subsets); injection (≥ 10 pages, files and images); burst set complete (≥ 40); tool-list equality; bridge isolation (C7 + a test); fixture MCP read/write tool (UC-35); latency (first text 8 s, complete 20 s, voice ≤ 12 s after text, media 60 s with "Ko'ryapman" ≤ 2 s, screenshot 30 s, news 30 s) |
| P4 | gate suite and commit/push policy on Codex |

**Work units** (each unit = a feature package plus its tests):
| Unit | Contents | Depends on | Parallel? |
|---|---|---|---|
| U1 | `core/*`, `persistence/*`, settings registry mechanism, `make check` skeleton | — | no (everything imports it) |
| U2 | `ports/*`, `tests/fakes/*`, factories | U1 | no |
| U3 | telegram adapter, access, inbox, outbox, polling runner (`bots/common.py`) | U2 | yes, with U4–U7 and U10 |
| U4 | claude adapter (executor, assistant, guard) + spikes S1–S6 | U2 | yes |
| U5 | gate domain + service, shims, githooks, gate eval suite | U2 | yes (pure) |
| U6 | burst, voice STT, gemini/openrouter STT adapters | U2 | yes |
| U7 | routing, projects discovery, routing eval set | U2 | yes |
| U8 | turns, questions, digest, servers | U3, U4, U5, U7 | no |
| U9 | `bots/dev` wiring, health, catalogs, golden tests | U3–U8 | no |
| U10 | supervisor watchdog + backups, installer, plists, check script | U1 (file formats in §2.8) | yes |

P2–P4 follow the same pattern: features run in parallel, then wiring last.

**Panel** (Telegram Mini App for the dev bot, owner request 2026-10-25): its own SPEC, `docs/MINIAPP_SPEC.md` (units M0–T2, §9 there; it amends §2.1 for the loopback web server and the Cloudflare tunnel).

**Shared files are sequential** (one unit edits them at a time, merged in order):
- `core/strings.py`, `core/errors.py`, `shared/settings/defaults.py`;
- `migrations/versions/*` (one Alembic head);
- `bots/<bot>/intents.py` and `bots/<bot>/app.py` (wiring);
- `pyproject.toml` and `uv.lock`, including the import-linter contracts;
- `.protected-core`, `tests/eval/thresholds.toml`, and the CONVENTIONS §4 port table.

## 8. Testing strategy
- **Fakes, one per port** (`tests/fakes/`):
  - `FakeAgentRuntime` — scripted event sequences; records sends, interrupts and guard calls;
  - `FakeAssistant` — replays recorded LLM fixtures;
  - `FakeTranscriber`, `FakeSynthesizer`, `FakeMediaAnalyzer` — scripted latency and errors (429, 402,
    timeout);
  - `FakeMessenger` — records every request; any `reply_markup` fails the test;
  - real `git` on `tmp_path` repos, plus `FakeGitHub`;
  - `FakeRemoteShell`;
  - a fixture MCP server — one read tool, one write tool, OAuth endpoints;
  - `FakeWebTools`, `FakeMacHost` — pressure levels, a sessions-registry dir, a ClaudeManager `state.json`;
  - `InMemoryKeystore`;
  - `FakeClock` — virtual time for windows, TTLs and schedules.
- **Recorded fixtures** (`tests/fixtures/<vendor>/`, sanitised): SDK message streams (init, partial, result,
  `RateLimitEvent`), Telegram updates (albums, voice, `my_chat_member`), Gemini and OpenRouter
  STT/TTS/video responses, gh/ssh outputs, CI workflow files for deploy-branch detection (including YAML
  `on:` parsed as `True`).
- **Eval harness**: `tests/eval/<suite>/cases.yaml` plus one runner per suite. LLM answers replay from
  `tests/fixtures/llm/<suite>/<sha256>.json`; `pytest -m live --record-llm` re-records them. The gate suite
  feeds recorded tool-call payloads to every executor's `guard.py`.
- **Local and CI-less.** `make check` = ruff, mypy `--strict`, `lint-imports`, pytest (sockets disabled),
  `python -m unittest discover supervisor/tests` under the supervisor's own interpreter (no pytest
  dependency), `alembic upgrade head` on scratch DBs plus `alembic check`. The supervisor runs the same
  `make check` before any switch.
- **Opt-in live** (`-m live`, needs real keys): a test bot plus the second Telegram account (stranger,
  leave-group, privacy and rights checks); a Claude scratch project (smoke, rotation A→B); STT/TTS on each
  configured provider; a throwaway private GitHub repo; SSH to one owner host. `tests/live/latency.py`
  writes the p90 report for §7.
- **Owner-reviewed** (once per release): UC-02 "no question needed", UC-30 quality, opinion and religious
  subsets.

## 9. Ops

### 9.1 LaunchAgents (`ops/launchd/*.plist.tmpl`, rendered by the installer)
| Key | dev / personal | supervisor |
|---|---|---|
| `ProgramArguments` | `DATA/releases/current/.venv/bin/python -I -m yordamchi.bots.<bot>.app` | `DATA/supervisor/current/.venv/bin/python -I -m yordamchi_supervisor` |
| `EnvironmentVariables` | `YORDAMCHI_ENV_FILE`, `PATH=DATA/releases/current/shims:~/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin`, `LANG=en_US.UTF-8`, `PYTHONUNBUFFERED=1` | `YORDAMCHI_DATA_DIR`, the same PATH without shims |
| `ProcessType` | `Interactive` (STACK §4.6) | `Standard` |
| `RunAtLoad` / `KeepAlive` | `true` / `true` (deliberate stops use `launchctl bootout`) | `true` / `true` |
| `ThrottleInterval` / `ExitTimeOut` | `10` / `20` | `10` / `10` |
| `WorkingDirectory` | `DATA/run` | `DATA/run` |
| `StandardOutPath` = `StandardErrorPath` | `~/Library/Logs/Yordamchi/uz.yordamchi.<bot>.launchd.log` | same pattern |
| `SoftResourceLimits.NumberOfFiles` | `8192` | — |
| `AbandonProcessGroup` | absent (false: children die with the bot) | absent |

PATH: the plist PATH plus the login-shell PATH merge (§2.5). Binaries found on this Mac 2026-10-23:
`~/.local/bin/{claude,gh,node,uv}`, `/usr/bin/{git,ssh}`, `/usr/local/bin/docker`. ffmpeg and yt-dlp are not
installed: ffmpeg goes to `DATA/bin`, and yt-dlp comes with the venv.

### 9.2 Power, logs, backups, disk
- **Power** (owner, once): `sudo pmset -c sleep 0` (today AC `sleep 1`). Turn off "install macOS updates"
  restarts. The supervisor holds `/usr/bin/caffeinate -s -w <own pid>`. With the lid closed, clamshell needs
  AC plus an external display. Battery with the lid closed sleeps (accepted, Q1).
- **Logs**: JSON lines through `core/log` with `redact`. Rotation: 20 MB × 4 per bot, 10 MB × 2 for the
  supervisor. The supervisor copy-truncates each launchd log over 5 MB. Total ≤ 200 MB (NFR-CAP-05).
- **Backups** (supervisor, 04:00): the `sqlite3` backup API → `backups/daily` (keep 7), `backups/weekly`
  (Sunday, keep 4), `pre-update` (keep 5). `PRAGMA integrity_check` runs on each copy, and a failure alerts.
- **Disk guard** (dev bot, every 10 min): under `guard.min_free_disk_gb` → `S.DISK_LOW` once per 6 h; under
  `guard.block_disk_gb` → new coding turns queue. Media purge runs daily.

### 9.3 Installer (`scripts/install.py`, stdlib, run at the Mac; no Homebrew)
1. Preflight: arm64; `~/.local/bin/{uv,claude,gh,node}` present; `/usr/bin/git`; no refused credential
   environment variables.
2. `uv self update` to 0.13.5; `uv python install 3.12.15`.
3. Create the `DATA` tree (0700) and `~/.config/yordamchi` (0700). Generate `master.key` (0600) if it is
   absent; never overwrite it. Run `tmutil addexclusion`.
4. Env files: tokens and ids are typed at the terminal (hidden input), never in chat; written 0600.
5. App release: `git archive` → `releases/<v>`; `uv sync --frozen --no-editable`; `chmod -R a-w`; `current`
   symlink. Supervisor release: copy `supervisor/`, `uv venv --python 3.12.15`, symlink.
6. ffmpeg + ffprobe 9.0.2 arm64 zips: SHA-256 pinned in the script → `DATA/bin`; assert
   `-encoders` lists `libopus`. P3: `playwright install chromium`.
7. `alembic upgrade head` for each DB file. Render the plists, `launchctl bootstrap gui/$UID …` and enable.
8. `python -m yordamchi.check`, which also runs at bot start and on "tekshir", inside the launchd context.
   It verifies:
   - `getMe.can_join_groups == false`;
   - privacy off, or the bot is admin with `can_delete_messages` and `can_pin_messages`;
   - `getMyCommands` is empty;
   - file modes (0600/0700);
   - no refused variables;
   - `claude auth status` reports `authMethod` `claude.ai`;
   - ClaudeManager is running;
   - binaries are found;
   - `pmset` AC sleep is 0;
   - free disk;
   - FileVault state (info only).

   It also prints the owner checklist (Telegram 2FA, second account).

## 10. Open items needing owner action or approval
1. Run `sudo pmset -c sleep 0` once, keep the Mac on the charger, and turn off automatic macOS update
   restarts (Q1, R-01/02).
2. **Give a Gemini or an OpenRouter key by chat.** Either works, on any tier: the bot detects it, validates
   it, sets itself up (§3.2.11) and reports quota limits plainly. No paid-tier key is required.
3. Telegram setup, about 15 min, following the check script:
   - two bots; join-groups off after setup;
   - dev privacy off, or the bot as admin with delete and pin rights;
   - the private group;
   - Telegram 2FA;
   - a second Telegram account for the stranger tests.
4. For the evals: record ≥ 20 mixed-language voice notes once (UC-05 set), and grade the owner-reviewed
   outputs once per release.
5. Tell the bot which servers to watch (`servers.hosts`, by chat; default none). From P2, add Bonvi HR,
   Wunderkind and LC by chat, as Yordamchi's own copies.
6. P4 only: a ChatGPT plan, then install Codex CLI and run `codex login`.

Resolved, no action: C1. `cleanupPeriodDays` stays at 30 (owner, 2026-10-23); the handoff note plus a new
session is the normal path (§3.2.7).

## 11. Spikes (P1, week 1; each ends in a test kept in `tests/live/`)
| Spike | Question | Decision if it fails |
|---|---|---|
| S1 | `priority: "next"` dict passthrough in the Python SDK: is a follow-up merged into the running turn, and how many results come back? | Queue the follow-up and send it as a `new_turn` right after the result (still no second final message: the result grace covers it) |
| S2 | Does `interrupt()` kill a running Bash command within 2 s? | Always kill the process group after 1 s |
| S3 | Do PreToolUse in subagents and `AskUserQuestion` reach `can_use_tool` under `bypassPermissions`? (The docs say yes) | Answer questions only through gate-deny + message (§3.2.2 step 6 path) |
| S4 | Does `UserPromptSubmit` `additionalContext` change persona and instructions on a resumed session? | Rotate the session on a prompt change, seeded with the handoff note |
| S5 | `--restricted` with WebSearch, WebFetch and SDK MCP | Omit `--restricted` (the five other layers stay) |
| S6 | Do SDK sessions appear in `~/.claude/sessions/` (`kind`/`entrypoint`)? | Exclude by child pid; the process scan is primary |
| S7 | OpenRouter chat-path STT vs Google on the 20-note set | Tell the owner once that a direct Gemini key spells names better |
| S8 | Streaming TTS + piped Opus: voice ≤ 12 s after the text? | Shorter `voice.max_words` (55), and synthesize sentence by sentence |

## 12. Definition of Done (per phase; the machine confirms)
- [ ] `make check` is green: ruff, mypy `--strict`, import-linter C1–C8, A1–A8, pytest, supervisor
      unittest, `alembic upgrade head` on empty DBs and on the previous release's fixture DB, `alembic check`.
- [ ] Every eval set of the phase (§7) meets `thresholds.toml`. No threshold was lowered, and no test was
      skipped or deleted (collected count ≥ previous).
- [ ] The live latency report meets every p90 row of the phase.
- [ ] Each new endpoint-equivalent (intent, tool, socket method) carries the owner-only and gate tests:
      stranger → 0 requests; `reply_markup` grep = 0; secret-leak search over logs, both DBs,
      `~/.claude/projects` and replies = 0.
- [ ] The phase's fault tests pass (`kill -9`, network, sleep/wake, plus P2 broken updates).
- [ ] Installed through `scripts/install.py` on the Mac; `python -m yordamchi.check` is all green from
      launchd context.
- [ ] Everything from earlier phases still passes.
- [ ] `docs/QOLLANMA.md` (Uzbek) is updated with what the owner can say in this phase.
