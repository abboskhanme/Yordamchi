# Yordamchi — Stack

Status: **Owner-chosen 2026-10-23 (D1–D3, D6); verified against official docs 2026-10-23.**
Two conflicts found, each with a one-line fix (§3). Requirements: `docs/REQUIREMENTS.md` · Risks: `docs/RISKS.md`

## 1. Decision (owner's, unchanged)

Python 3.12 (uv) · aiogram 3, long polling · Claude Agent SDK driving the installed official `claude`
(one headless streaming session per project, resume by id) · google-genai for STT, TTS and optional
search/video · SQLite (WAL) via SQLAlchemy 2 async + Alembic · bots, watchdog and self-updater as user
LaunchAgents on the MacBook Pro M4 (macOS 26.6.1, 16 GB). No inbound port, no Homebrew.

The requirements left four choices open. They are decided here, with reasons in §2 and §2.2:
- the installed `claude` via `cli_path`, not the SDK's bundled copy;
- the master key in a 0600 file, not the Keychain;
- a static ffmpeg binary, not PyAV;
- MCP OAuth tokens reach sessions only through `headersHelper`, never on argv.

## 2. Components

| Component | Pin (exists 2026-10-23) | Serves | How / why | Source |
|---|---|---|---|---|
| Python | 3.12.15 via uv 0.13.5; `requires-python = "==3.12.*"` | D1 | Branch is security-only; EOL 2028-10. arm64 build exists (python-build-standalone 20261021). Installed today: 3.12.14 and uv 0.12.9. | [release cycle](https://peps.python.org/api/release-cycle.json), [uv](https://pypi.org/project/uv/0.13.5/) |
| claude-agent-sdk | 0.2.173 (2026-10-22; bundles CLI 2.1.309) | D3, D6, UC-02/03/11–16 | One `ClaudeSDKClient` streaming session per project. Mechanisms in §2.1. | [PyPI](https://pypi.org/project/claude-agent-sdk/0.2.173/), [CHANGELOG](https://github.com/anthropics/claude-agent-sdk-python/blob/main/CHANGELOG.md) |
| `claude` CLI | installed `~/.local/bin/claude` 2.1.309 (auto-updates); set `cli_path` explicitly | D3, NFR-SEC-05, NFR-REL-08 | The SDK picks its bundled binary first, then PATH, then `~/.local/bin`. Pointing `cli_path` at the installed binary means the bot and the Mac (`claude --resume <id>`) share one binary, one transcript format and one version to smoke-test. The bundled copy is not used. Signed `com.anthropic.claude-code`. | [SDK transport source](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/_internal/transport/subprocess_cli.py) |
| aiogram | 3.31.0 (2026-08-26; Bot API 10.3) | UC-01–06, D2, D8 | Long polling.<br>• `setMessageReaction`: one reaction per message.<br>• `sendChatAction`: lasts ≤ 5 s, so repeat it.<br>• `deleteMessage`: only < 48 h; private chat: incoming OK; group: needs admin.<br>• `leaveChat`.<br>• `sendMessageDraft`: private chats only, 30 s ephemeral, then `sendMessage`; streams personal-bot text.<br>• Albums arrive as separate updates with `media_group_id`; the burst assembler joins them.<br>• Download ≤ 20 MB, upload ≤ 50 MB.<br>• `sendVoice` accepts OGG/Opus, MP3, M4A.<br>Caps pydantic < 2.14. | [Bot API](https://core.telegram.org/bots/api), [PyPI](https://pypi.org/project/aiogram/3.31.0/) |
| google-genai | 2.33.0 | UC-05, UC-30/31/33/34 | **STT** `gemini-3.5-transcribe-live` (Live API, `client.aio.live.connect`, 16 kHz PCM in, owner decision 2026-10-25) by default; `gemini-3.5-transcribe` (Interactions API), same as OvozYoz, when set. `transcription_config`: `language_codes` [uz-UZ, ru-RU, en-US], `custom_vocabulary` ≤ 1,000 terms, `mode: "smart"`. Accepts `audio/ogg`, so Telegram voice goes in as-is. uz-UZ is listed.<br>**TTS** `gemini-3.8-flash-tts` (130+ languages incl. "Northern Uzbek"). 30 prebuilt voices with no gender metadata. Gender and accent come from `client.voices.list(gender=, accent=, language_code=)` or Voice design. Output is WAV/PCM 24 kHz mono, so it needs encoding.<br>**Search/video** `gemini-3.8-flash`. `google_search` tool: $14 per 1k queries after 5k free per month. YouTube URL as a video part (public only, preview, free). Inline media ≤ 20 MB. | [transcribe](https://ai.google.dev/gemini-api/docs/transcribe), [TTS](https://ai.google.dev/gemini-api/docs/speech-generation), [grounding](https://ai.google.dev/gemini-api/docs/google-search), [video](https://ai.google.dev/gemini-api/docs/video-understanding) |
| SQLAlchemy[asyncio] · aiosqlite · Alembic | 2.1.6 · 0.22.1 · 1.20.1 | D1, NFR-REL-02/05/06, UC-09 | WAL + `busy_timeout` + `synchronous=NORMAL`. Alembic `render_as_batch=True`, because SQLite cannot ALTER most columns. Portable types only, so moving to Postgres stays a URL + driver change. Personal bot reads the digest over a `mode=ro` connection. | [SQLAlchemy](https://pypi.org/project/SQLAlchemy/2.1.6/), [aiosqlite](https://pypi.org/project/aiosqlite/0.22.1/), [Alembic](https://pypi.org/project/alembic/1.20.1/) |
| cryptography | 50.0.2 | UC-04, NFR-SEC-02 | AES-256-GCM per secret; master key in a file (§2.2). | [PyPI](https://pypi.org/project/cryptography/50.0.2/) |
| mcp | 2.3.0 | UC-08, R-43 | Validates an integration (connect + `list_tools`).<br>OAuth from the phone via `OAuthClientProvider`:<br>• `redirect_handler` receives the full auth URL; Yordamchi sends it to Telegram.<br>• The owner's phone fails to load `http://localhost:<port>/callback?...`; he pastes that URL into chat and `callback_handler` returns code/state.<br>• PKCE and refresh are built in; `TokenStorage` points at the encrypted store.<br>This is the same paste-back flow `claude mcp login --no-browser` documents. | [OAuth clients](https://py.sdk.modelcontextprotocol.io/client/oauth-clients/), [PyPI](https://pypi.org/project/mcp/2.3.0/) |
| yt-dlp[default] | 2026.10.21 (+ yt-dlp-ejs 0.8.0) | UC-32 | YouTube needs yt-dlp-ejs plus a JS runtime. Only deno is enabled by default, so enable the existing Node (`js_runtimes`: node at `~/.local/bin/node`). Needs ffmpeg/ffprobe to merge and re-encode to ≤ 50 MB. | [README §dependencies](https://github.com/yt-dlp/yt-dlp#dependencies) |
| ffmpeg + ffprobe | 9.0.2 static arm64, martin-riedl.de (signed zip, SHA-256 per file, opus enabled) | NFR-CAP-04, UC-30/32 | Encodes PCM to OGG/Opus voice; yt-dlp uses it for video. Voice-only fallback: the built-in `afconvert` to M4A, which `sendVoice` accepts. | [builds](https://ffmpeg.martin-riedl.de/) |
| Playwright | 1.64.1 + Chromium | UC-32 | Viewport and full-page PNG. A new browser context per request gives a clean profile. | [PyPI](https://pypi.org/project/playwright/1.64.1/) |
| pydantic (transitive) + stdlib `tomllib` | 2.13.5 | NFR-OPS-03, UC-09 | Settings schema and validation; one TOML config file. No extra config library. | resolved lock |
| pytest · pytest-asyncio | 9.1.1 · 1.4.0 | NFR-OPS-02 | Gate, burst, routing, leak, fault and self-update suites. | [pytest](https://pypi.org/project/pytest/9.1.1/) |
| Codex CLI (P4) | `@openai/codex` 0.164.0 (2026-10-23); re-pin at P4 | UC-17 | Drive `codex app-server` (JSON-RPC over stdio) from Python:<br>• `thread/resume`, `turn/steer`, `turn/interrupt`.<br>• Commands reach the client as `item/commandExecution/requestApproval` (command, cwd), so the same gate answers accept/decline.<br>• Login: `account/login/start` with `chatgptDeviceCode`.<br>• Simpler fallback: `codex exec --json` / `codex exec resume <id>`.<br>• Codex PreToolUse hooks are deny-only and fail open, so they are a backstop only. | [app-server](https://learn.chatgpt.com/docs/app-server), [hooks](https://learn.chatgpt.com/docs/hooks), [exec](https://learn.chatgpt.com/docs/non-interactive-mode) |

Machine check (2026-10-23): `uv pip compile` resolves this pinned set together for Python 3.12 on macOS arm64,
68 packages in all.

### 2.1 Claude Agent SDK — requirement → mechanism (SDK 0.2.173 / CLI 2.1.309)

| Need | Mechanism |
|---|---|
| Burst follow-up merged mid-turn (UC-02) | Send the user-message dict with `priority: "next"` (the default). Claude reads it in the same turn once the running tools finish, and close messages can merge into one turn. The field is documented in the [TS reference](https://code.claude.com/docs/en/agent-sdk/typescript); the Python client passes dict fields through. Confirm in a spike. |
| Redirect, "yo'q, boshqacha…" (UC-03) | `priority: "now"` + `origin: {"kind": "human"}` (CLI ≥ 2.1.286). Running shell, subagent and MCP work moves to the background, and the message is read in the same turn. |
| "to'xta" (UC-03) | `interrupt()`. Python has no `cancel_queued` (TS does). So Yordamchi keeps follow-ups in its own queue and forwards only what it merges. Background tasks need `stop_task()`. |
| Resume / handoff (D3, UC-16/18) | `resume=<id>`; `session_id=<uuid>` pre-assigns ids. The owner runs `claude --resume <id>`, which searches every project; `--continue` skips SDK sessions by design. |
| Model per path (NFR-SPD-02) | `model` alias + `effort` per session; `set_model()` switches a warm session.<br>Today: `haiku` = Haiku 5.5, `sonnet` = Sonnet 5.5, `opus` = Opus 5.5 (the owner's default, xhigh).<br>`fable`/`best` (Fable 5.1) "can bill to usage credits" on a plan, so switching to it is a spending decision (UC-13 #3).<br>Store aliases, not model ids. ([model config](https://code.claude.com/docs/en/model-config)) |
| Important-decision gate (UC-13, NFR-SEC-06) | `PreToolUse` hook callbacks: "hooks run before every other step, and a hook deny applies even in `bypassPermissions` mode". Scoped `disallowed_tools` add a second layer, denied in every mode. `can_use_tool` never sees auto-approved calls, so it is never the gate. Design rule: §4.1. ([permissions](https://code.claude.com/docs/en/agent-sdk/permissions)) |
| Agent asks the owner (UC-12) | `AskUserQuestion` arrives in `can_use_tool`: 1–4 questions × 2–4 options, free text allowed, may stay pending indefinitely. ([user input](https://code.claude.com/docs/en/agent-sdk/user-input)) |
| Commit/push authorization (NFR-SEC-11) | `system_prompt={"type": "preset", "preset": "claude_code", "append": …}`. CLAUDE.md is injected as context that "overrides default behavior". So also send the authorization as per-turn context (`UserPromptSubmit` → `additionalContext`), and prove it with the NFR-SEC-11 eval. |
| Dev sessions use the owner's rules, agents, skills | `setting_sources=["user", "project", "local"]`, always explicit: the default flipped between SDK versions. |
| Personal bot isolation (NFR-SEC-08) | • `setting_sources=[]`: no CLAUDE.md, agents, skills, hooks, plugins.<br>• `tools=["WebSearch", "WebFetch"]` and `strict_mcp_config=True`.<br>• env `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`: auto memory loads regardless of setting sources.<br>• An empty private `cwd`.<br>• An allowlist `PreToolUse` hook.<br>• Compare the `init` tool and MCP list against the allowlist (C2).<br>Optional extra layer: `--restricted` (CLI ≥ 2.1.248) via `extra_args`.<br>**Never `--bare`**: it never reads OAuth or the keychain. ([features](https://code.claude.com/docs/en/agent-sdk/claude-code-features)) |
| D11 integrations, no account connectors (NFR-SEC-07) | `mcp_servers` per session:<br>• Remote HTTP servers use `headersHelper`. It runs at connect and reconnect, re-runs once on 401/403, and times out after 10 s; it asks Yordamchi's token service for the header.<br>• Yordamchi's own tools (bridge, media) run as in-process SDK servers.<br>Account connectors:<br>• Dev sessions set `ENABLE_CLAUDEAI_MCP_SERVERS=false`, which keeps project `.mcp.json` and plugins.<br>• The personal bot uses `strict_mcp_config=True`, which also drops connectors.<br>([MCP](https://code.claude.com/docs/en/mcp)) |
| Persona / prompt change "from the next message" (UC-09, R-34) | The snapshot is on by default, so a changed `append` is ignored on resume until compaction. Documented fix: send the change in the conversation (next message, or `UserPromptSubmit` → `additionalContext`). Keep the snapshot on; the docs say "Keep recording on in production", and turning it off loses the prompt cache and earlier thinking. ([system prompts](https://code.claude.com/docs/en/agent-sdk/modifying-system-prompts)) |
| ClaudeManager rotation (D7, NFR-SEC-04) | The SDK inherits the environment and the default config dir, so it uses the Keychain item `Claude Code-credentials`, the slot ClaudeManager rewrites. Running sessions re-read it in about 30 s. ([authentication](https://code.claude.com/docs/en/authentication)) |
| Cost, runaway guard (UC-07, NFR-COST-03) | `ResultMessage.total_cost_usd`, `usage`, `model_usage`, `num_turns`, `duration_ms`. Limits: `max_turns`, `max_budget_usd`, plus Yordamchi's own wall clock. |

**Subscription terms.** The [support article](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)
was last updated 2026-10-07:
- The SDK and `claude -p` still draw from plan limits; the June 2026 billing change is paused.
- Max and Team plans now also get monthly API credits that cover the SDK and `claude -p`.

The [legal page](https://code.claude.com/docs/en/legal-and-compliance) still:
- ties plan limits to "ordinary, individual usage";
- forbids routing plan credentials "on behalf of their users";
- forbids tools that "collect, store, or intermediate" credentials.

So Yordamchi stays owner-only, uses the unmodified binary and never handles credentials (R-08 unchanged).

### 2.2 Choices the requirements left open

- **Master key in a 0600 file, not the Keychain.**
  - Location: `~/.config/yordamchi/master.key` (dir 0700, excluded from Time Machine). The DB lives
    elsewhere, under `~/Library/Application Support/Yordamchi/`.
  - Why not the Keychain: the LaunchAgent runs in the owner's session, so the login Keychain is unlocked,
    but its ACL binds to the reading binary. uv's CPython is ad-hoc signed (`codesign`: `Signature=adhoc`,
    no Team ID). Every Python patch or venv rebuild would raise an "allow access" dialog that nobody can
    click while the owner is away, and the bots would lose their secrets. AgentsRoom made the same call.
  - Cost: any process running as the owner can read the file. That is equally true of an "allow all"
    Keychain item. FileVault protects the disk at rest.
- **Static ffmpeg, not PyAV.** yt-dlp calls the ffmpeg/ffprobe executables, which PyAV cannot provide, so
  one binary serves both voice and video. Yordamchi does not reuse the copy in `~/.hermes/tools`
  (same vendor, 9.0.1), to stay independent of Hermes.
- **News (UC-33): Claude WebSearch/WebFetch first.** Gemini grounding is only the fallback: it is billed
  per query after 5k per month, and its terms govern how Search Suggestions are displayed.

## 3. Conflicts found

**C1 — Transcripts are deleted after 30 days, but parked projects must resume by session id (UC-18, D3, NFR-CAP-01).**
- Evidence: [data usage](https://code.claude.com/docs/en/data-usage) says "Claude Code clients store
  session transcripts locally … under `~/.claude/projects/` for 30 days by default … Adjust the period
  with `cleanupPeriodDays`."
- The owner's `~/.claude/settings.json` does not set it.
- Claude Code clients run the sweep themselves (see `~/.claude/.last-cleanup`), including his
  interactive sessions with their own settings. A per-session `--settings` value is therefore not a
  reliable guard.
- **Fix:** add one line, `"cleanupPeriodDays": 365`, to `~/.claude/settings.json`. It is the owner's
  global file, so he says yes first.
- Fallback in code: if a transcript is missing, start a new session seeded with the digest's
  "where we stopped" note, and tell the owner.

**C2 — NFR-SEC-08 requires an exact tool list, but some tools cannot be removed.**
- Evidence: `EndConversation` cannot be removed: "neither `--disallowedTools` nor a `--tools` list can
  remove it" while any other tool remains. It only ends the conversation, and PreToolUse does not run
  for it.
- `ToolSearch` or `WaitForMcpServers` also appear, depending on the MCP tool-search mode
  ([tools reference](https://code.claude.com/docs/en/tools-reference)).
- **Fix:** name these harness tools explicitly in the allowlist; the equality check stays strict.

## 4. Constraints the architect must design around

1. **The gate never waits inside a hook.**
   - A timed-out PreToolUse hook does not block ("don't count on a stalled hook to act as a gate";
     the default timeout is 600 s).
   - `defer` survives restarts, but it "only works when Claude makes a single tool call in the turn".
     Otherwise the call goes through the normal permission flow.
   - So the gate is: an instant deterministic `deny`, a pending decision stored in Yordamchi's DB, and,
     after "ha", a single-use allow for that exact command and host. This survives a reboot
     (NFR-REL-02). ([hooks](https://code.claude.com/docs/en/hooks))
2. **Secrets on argv.** A dict passed as `mcp_servers` is serialized inline into `--mcp-config` on the
   command line, where `ps` shows it. Tokens go only through `headersHelper`, or through a path to a
   0600 file.
3. **The bot token leaks through URLs.** aiohttp/aiogram errors and tracebacks contain
   `api.telegram.org/bot<token>/…`. The scrubber must also cover stderr, `sys.excepthook` and the
   asyncio exception handler, not only logging.
4. **Environment.**
   - The SDK strips only `CLAUDECODE`, and Python `env=` merges on top of the inherited environment, so
     it cannot unset anything. Scrub `os.environ` at startup.
   - Refuse to start if any credential source outranks the keychain login:
     - `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`;
     - `ANTHROPIC_PROFILE`, `ANTHROPIC_FEDERATION_RULE_ID`;
     - `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, `CLAUDE_CODE_USE_FOUNDRY`;
     - an `apiKeyHelper`;
     - a non-default `CLAUDE_CONFIG_DIR`;
     - `ANTHROPIC_BASE_URL` (a proxy, NFR-SEC-05).
   - This list is wider than the one in NFR-SEC-04.
   - Positive check: `claude auth status` reports `authMethod: "claude.ai"`.
5. **Memory (R-05, Q2).** Measured today:
   - five interactive `claude` processes use 155–471 MB RSS each;
   - swap already stands at 4.5 of 6 GB.

   The memory-pressure guard is required, not a nice-to-have.
6. **launchd.**
   - Set `ProcessType=Interactive`. If it is unset, launchd applies "light resource limits … throttling
     its CPU usage and I/O bandwidth" (`man launchd.plist`), and speed is D10.
   - When a job exits, launchd kills its whole process group. A bot crash therefore ends its `claude`
     children, which matches NFR-REL-07.
7. **Self-update.**
   - Run the bots from immutable release dirs, never from the working copy the executor edits.
   - Each release is a commit with its own `.venv`, built by `uv sync --frozen`; a `current` symlink
     selects the live one.
   - The updater has its own pinned Python and venv, using only the stdlib plus `cryptography`
     (to read the report token). It restarts the bots with `launchctl kickstart -k`.
   - It snapshots the DB with the `sqlite3` backup API.
   - Its runtime is never upgraded in the same update as the bots (R-32).
8. **WebSearch cap.** WebSearch has a documented per-session cap of 200. Raise
   `CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION`, and keep the personal-session rotation.
9. **Privacy mode.** Turning privacy mode off applies only after the bot is removed and re-added to the
   group ([features](https://core.telegram.org/bots/features#privacy-mode)). Admin bots receive every
   message anyway.
10. **Unmeasured latency targets.**
    - STT of a 60 s note ≤ 5 s: OvozYoz measured 2.5 s for a 6 s clip. If it misses the target, split at
      silences and transcribe in parallel, as OvozYoz does.
    - Voice ≤ 12 s after text: use streaming TTS (`stream: true`) and encode as the audio arrives.

## 5. Runtime dependencies to install on the Mac (no Homebrew)

**Already present** (the check script verifies them):
- `claude` 2.1.309, auto-updating; ClaudeManager running.
- `gh`, logged in as `abboskhanme`.
- Node 24.21.0 (`~/.local/bin`); yt-dlp uses it.
- `/usr/bin/git` (Xcode CLT).
- `afconvert` (built into macOS).

**To install:**
1. `~/.local/bin/uv self update` (to 0.13.5), then `uv python install 3.12.15`.
2. In the repo: `uv sync --frozen`. The lockfile carries the §2 pins.
3. `uv run playwright install chromium` (installs to `~/Library/Caches/ms-playwright`).
4. ffmpeg + ffprobe 9.0.2 macOS arm64 release zips from ffmpeg.martin-riedl.de:
   - verify each against its published SHA-256. The pins (copied into `FFMPEG_PINS` in `scripts/install.py`):

     | Binary | URL | SHA-256 |
     |---|---|---|
     | ffmpeg | `https://ffmpeg.martin-riedl.de/download/macos/arm64/1789931890_9.0.2/ffmpeg.zip` | `c8ed4c4e6978a03c485edbfe4e0a5dc2380f8a30bba5150531b31b094492d924` |
     | ffprobe | `https://ffmpeg.martin-riedl.de/download/macos/arm64/1789931890_9.0.2/ffprobe.zip` | `fcbe839537485eaee7a7a8bc5cbc0f90d53617e80943e8a5b2e31cb851197ea6` |

     Source: the publisher's `ffmpeg.zip.sha256` and `ffprobe.zip.sha256` next to each zip, linked from the
     release list on https://ffmpeg.martin-riedl.de/. Checked 2026-10-24: one download of each zip hashed to exactly
     these values. Each zip holds one file, a Mach-O arm64 binary signed "Developer ID Application: Martin Riedl
     (KU3N25YGLU)". The publisher's `codecs.txt` and `versions.txt` for this build list the `libopus` encoder
     (opus 1.6.1).
     A new version means new pins here first, then in `FFMPEG_PINS`.
   - unpack into Yordamchi's own `bin/` outside the repo;
   - check that `ffmpeg -hide_banner -encoders` lists `libopus`.
5. `mkdir -m 700 ~/.config/yordamchi` (the key file is created at first run with mode 0600), then
   `tmutil addexclusion ~/.config/yordamchi`.
6. Power settings (`sudo` needed once):
   - Run `sudo pmset -c sleep 0`. Today the AC setting is `sleep 1`, and the Mac stays awake only
     because of running assertions.
   - The watchdog also holds `caffeinate -s` (no sudo needed; "valid only when system is running on AC
     power").
   - Lid closed: Apple's clamshell mode needs AC plus an external display (and keyboard/mouse);
     otherwise the Mac sleeps.
   - `sudo pmset -a disablesleep 1` is undocumented (it is not in `man pmset` on 26.6.1). Test it once
     if wanted.
   - Battery with the lid closed always sleeps; this is accepted (Q1).
   - Turn off automatic macOS update restarts.
7. FileVault (nothing to install):
   - After any reboot, nothing runs until the owner logs in.
   - `sudo fdesetup authrestart` only bypasses the pre-boot unlock. The user session, and with it the
     `claude` keychain login, still needs a login.
   - This is accepted (R-02); the heartbeat makes the outage visible.
8. A new paid-tier Gemini key for Yordamchi with a spend cap. The AIAgents key is dead since 2026-10-17
   (its `docs/VOICE.md`).
9. P4 only: `npm install -g @openai/codex@<pinned>`, or the standalone arm64 binary; then `codex login`
   (device-code flow available).

## 6. Rejected alternatives

- **TypeScript (grammY + TS SDK).**
  - Owner decision D1.
  - The Python voice code (OvozYoz, AI) and yt-dlp fit as-is.
  - Honest cost: the TS SDK is ahead (typed `priority`, `cancel_queued`, SessionStart/End hooks).
- **PostgreSQL.**
  - Under 1 GB a year, one host. SQLite needs no server under launchd, and the self-update snapshot is
    just the `sqlite3` backup API.
  - Switching later is a URL plus asyncpg, as long as the SQL stays portable.
- **tmux/PTY driving.**
  - The owner chose headless (D3).
  - Lunexa (AgentsRoom SPEC §2–3) needed screen parsing and timing hacks (60 ms Enter, 500 ms menu
    delay) that break with every CLI UI change.
  - The SDK gives typed messages, hooks, `can_use_tool` and `interrupt()`.
- **Telegram Topics.** Owner rejected (D4).
- **Claude Code Channels / Remote Control.**
  - One session each, with no content routing, burst assembly, Uzbek voice, approval policy or
    new-project flow.
  - Remote Control is the Claude app, not Telegram.
  - Both stay as manual fallbacks.
- **Claude Code background agents (`claude --bg`, agent view).** A terminal UI for dispatching sessions,
  with no programmatic streaming input, hooks or permission callback for a bot.
- **Hermes Agent for the personal bot.** Honestly, it would cover much of UC-30/31/33 out of the box
  (Telegram, Gemini voice, web). We still build because:
  1. In AIAgents it runs on provider API keys, not the subscription through the official binary (D6).
  2. The shared core (burst assembly, settings in data, secret store, outbox, scrubber) is built for the
     dev bot anyway.
  3. The Uzbek STT path uses OvozYoz's hints plus the registry vocabulary.
  4. D11 needs the registry and a tool allowlist checked at start (NFR-SEC-08).
  5. UC-09 and UC-19 work by chat for both bots.
  6. Silent polling and duplicate sends were seen in the owner's Hermes bots (R-04).
- **OpenRouter as the primary brain.** D6: per-token billing and a third party in the data path. It stays
  as an optional source of extra models or a fallback.
- **PyAV, imageio-ffmpeg, Hermes' ffmpeg.** See §2.2. imageio-ffmpeg also has no ffprobe, and its last
  release was 2025-01.
- **Keychain via `keyring`.** See §2.2.
- **SDK-bundled CLI.** See §2.

## 7. Costs accepted

- The bots live and die with the Mac's power, lid and login (R-01, R-02). An off-Mac relay is v2.
- The installed CLI auto-updates under the running bots. The NFR-REL-08 smoke test is the guard, and
  self-update bumps the SDK pin.
- Python 3.12 reaches EOL in 2028-10. Moving to 3.13 or 3.14 before then is a self-update.

## 8. Re-check on every release

- The SDK-with-plan support article and the legal page: billing and individual use (R-08).
- `claude --version` against the SDK's bundled CLI version (CHANGELOG). Run the smoke test. Re-check:
  - `--system-prompt-snapshot`, `strict_mcp_config` and `defer` semantics;
  - which hook events Python supports;
  - `priority` and `cancel_queued` in Python.
- Model aliases, and whether Fable draws on plan limits or usage credits.
- `cleanupPeriodDays` is still set (C1).
- Gemini:
  - model ids `gemini-3.5-transcribe`, `gemini-3.8-flash-tts`, `gemini-3.8-flash`;
  - the Uzbek rows and the voice list;
  - the grounding price and the YouTube preview status.
- The Bot API version against aiogram (`sendMessageDraft`, reactions, limits).
- Update yt-dlp weekly; re-check yt-dlp-ejs and its JS runtime rules. Also check the ffmpeg SHA-256 and
  the Playwright browser revision.
- The MCP spec: the 2026-07-28 revision deprecates dynamic client registration in favour of Client ID
  Metadata Documents, which need an HTTPS URL. Check each OAuth server.
- At P4: the Codex CLI version and the app-server approval protocol.
