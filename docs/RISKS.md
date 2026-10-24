# Yordamchi — Risks

2026-10-23 · Requirements: `docs/REQUIREMENTS.md` · L = likelihood, I = impact (H / M / L)

## Top 5
1. **R-01 Mac asleep or offline** — the bots stop exactly when the owner is away.
2. **R-10 Wrong-project routing** without replies.
3. **R-20 Destructive or production action slips past the gate.** This now also covers auto-push
   triggering CI deploys and a bare "ha" approving the wrong thing.
4. **R-30 Self-update bricks the bot.** This includes R-32, a broken self-updater, and R-33, a
   migration that cannot be rolled back. It matters more now that every future change is made by
   chat.
5. **R-05 Resource exhaustion** (16 GB RAM, ~31 GB free disk) with many concurrent sessions.

## 1. Host: power, sleep, network

| ID | What could happen | L | I | Mitigation |
|---|---|---|---|---|
| R-01 | The Mac is a laptop. With the lid closed on battery it sleeps regardless of `caffeinate` (it is on battery right now). Wi-Fi drops. Both bots go silent while the owner is away, and he cannot tell why. | H | H | Q1: Mac stays plugged in, set never to sleep on power (`pmset -c sleep 0`, lid open or clamshell). The owner already uses `caffeinate`. Pinned "Onlayn: HH:MM" heartbeat (UC-06). Sleep/wake recovery and gap report (NFR-REL-04). Off-Mac relay deferred to v2. |
| R-02 | FileVault is **on** (verified). After any reboot (macOS update, panic, power loss) nothing runs until the owner logs in at the Mac. | M | H | Turn off automatic macOS update restarts. Planned restarts use `sudo fdesetup authrestart`. Documented as accepted in NFR-REL-02. |
| R-03 | The Mac is offline for more than 24 h: Telegram discards undelivered updates, and those messages are lost silently. | L | M | Documented limit (scope). On return the bot reports the offline gap so the owner can resend. |
| R-04 | Silent failure modes seen in the owner's Hermes bots on this Mac: long polling dies without exiting, and network retries produce duplicate sends. | M | H | Watchdog process with heartbeat staleness check (NFR-REL-01). Persisted outbox with dedupe (NFR-REL-06). Fault-injection tests. |
| R-05 | 16 GB RAM, 10 cores and about 31 GB free disk. 10–15 `claude` processes plus builds, tests and Docker swap or fill the disk, and everything slows or crashes. | H | H | Cap on concurrent working turns (Q2: owner chose 10–15, default 15), plus a memory/disk-pressure guard that queues new turns before the Mac swaps; the owner parks projects by chat (UC-18). Idle sessions hold no process. Disk warning at < 10 GB, media cache purge, log rotation (NFR-CAP-05). Free disk space before starting. |
| R-06 | launchd starts with a minimal PATH, so `claude`, `git`, `gh`, `node` and `ssh-agent` are not found. Nested-session environment variables (`CLAUDECODE`, `CLAUDE_CODE_SESSION_ID`…) leak in from a dev shell and break transcripts. Lessons from AgentsRoom. | H | M | Explicit PATH (`~/.local/bin` first) plus login-shell PATH resolution. Strip nested-session variables from the child environment. The check script runs from launchd context. |

## 2. Claude subscription, terms and the CLI

| ID | What could happen | L | I | Mitigation |
|---|---|---|---|---|
| R-07 | Usage limits are hit with many parallel sessions. | L | M | 3–4 accounts are rotated by ClaudeManager (D7). Yordamchi uses the **standard keychain login**, so rotation applies. On a limit: one notice, then automatic continuation (UC-11). "holat?" warns if ClaudeManager is not running. |
| R-08 | Terms of use. Anthropic: OAuth is for "ordinary use of Claude Code" by the purchaser, the Agent SDK with a subscription is for "individual" use, and tools may not "collect, store, or intermediate" Claude credentials. The owner is the sole user of his own plans, which fits personal use. Policy is in flux: a June 2026 change to `claude -p` / SDK billing was announced, then paused; as of 2026-10-07 SDK and `claude -p` still draw from plan limits, and Max adds monthly API credits. Using several accounts to extend limits may conflict with the terms (owner accepted that risk in ClaudeManager). | M | H | Only the official, unmodified `claude` binary. Never proxy, intercept or rewrite traffic. Never touch Claude credentials (NFR-SEC-04/05). Owner-only use. Re-check the support article ("Use the Claude Agent SDK with your plan") at each release. The swappable executor (UC-17) limits lock-in. |
| R-09 | Isolating the personal bot through a separate `CLAUDE_CONFIG_DIR` silently moves it to a **different keychain slot** (`Claude Code-credentials-<hash>`, see the ClaudeManager SPEC). It then escapes rotation, or has no login at all. `CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_API_KEY` in the environment bypass rotation too. | M | H | NFR-SEC-04 forbids both. The isolation mechanism is chosen by the architect without changing the config dir. Rotation test: account A → B, the next turn runs on B. |
| R-11 | The `claude` CLI auto-updates. Stream format, session registry (`~/.claude/sessions/*.json`) or flags change, and progress, Mac-attach detection or results break. | M | M | Smoke self-test on version change (NFR-REL-08). Parse defensively and prefer documented SDK messages and hooks. Treat internal files as best-effort, with a process-scan fallback. |
| R-12 | Account-bound config leaks in: claude.ai connectors (the owner has Bonvi HR, Markaz, Wunderkind, Higgsfield, Miro… on an account) differ per rotated account, so behaviour changes after rotation or an integration silently disappears. | H | M | D11 / UC-08: integrations live in Yordamchi's registry and are injected per session. NFR-SEC-07: do not rely on account connectors (architect decides how to exclude them). Rotation test in UC-08. |
| R-13 | Headless Claude is too slow for chat: cold start, loading CLAUDE.md, MCP servers and plugins. The owner's top priority (speed) fails. | M | H | Warm persistent sessions for the Q&A path, a faster model tier, the digest for reports, text before voice (NFR-SPD). Measure against the latency table before P3 ships. |

## 3. Routing, conversation, approvals

| ID | What could happen | L | I | Mitigation |
|---|---|---|---|---|
| R-10 | Without replies, a message goes to the wrong project. Similar names make it likely: HRMentor vs HRMentor-wt-agent-api, BonviCall vs "BonviCall Clone Archive", MegaPlus ERP vs MegaPlus Platform, six Wunderkind* projects. | M | H | Content-first routing with several signals. Ask only when confidence is low, naming the candidates. **Always name the target** in the first reply. Archive and worktree folders are excluded unless named. Routing eval with a silent-wrong-route ceiling (UC-10). "bu X uchun edi" stops and moves the work, with revert offered. |
| R-14 | Burst window **too short**: an image and its explanation become two turns, and the bot answers a lone screenshot prematurely or asks questions. | M | M | Adaptive window: longer after bare media, held open by an album or a voice note being transcribed, "kut" extends it. Late follow-ups merge into the running turn (UC-02). Tune from logs. |
| R-15 | Burst window **too long**: every answer feels slow. | M | M | Short window after a complete text question (≤ 1.5 s added, UC-02 AC). "typing…" within 1 s. Windows are configurable. |
| R-16 | An **unrelated** message gets merged into a running task and corrupts its context. | M | M | Merge only when routing gives the same project with high confidence. Otherwise route separately (UC-02 "unrelated" subset: 0 merges). |
| R-17 | STT mishears an Uzbek, English or Russian mixed instruction or a project name, and the wrong instruction runs. | M | M | One-line transcript echo before execution (UC-05). Fast interrupt (UC-03). Important actions still need a "yes". Project aliases help recognition. |
| R-18 | Conversational approval binds wrongly: a casual "ha" meant for a question approves a deploy, or "ha" lands on the wrong project. | M | H | "ha" approves only when it replies to the decision, names it, or is the only one pending. Otherwise the bot asks which (UC-13). Approval is bound to the exact command and host and is single-use. It expires after 2 h. |
| R-19 | Global `~/.claude/CLAUDE.md` says "ask before push". Sessions either **stall** waiting for a push approval nobody sees, or, in the reverse case, push secrets. | H | M | Durable per-session owner authorization (NFR-SEC-11). Eval fails if any session asks about push. Deterministic pre-push secret scan blocks `.env` and keys (UC-13 AC). |

## 4. Destructive actions, injection, secrets

| ID | What could happen | L | I | Mitigation |
|---|---|---|---|---|
| R-20 | A runaway or confused agent runs a destructive or production command: `rm -rf`, `docker compose down -v`, `DROP`, force push, remote deploy. Or an auto-push to `main` triggers a **CI deploy**, which is a deploy without a "yes". Touching 1C on the Bonvi Windows host would affect real users. | M | H | Deterministic tool-layer gate, the same for Claude and Codex (UC-13, NFR-SEC-06). Push to a deploy-triggering branch counts as a deploy. Server writes need a "yes", and the Windows host is jump-only (Q3). `BONVI_SERVER.md` rules load through the owner's global rules. Runaway guard (NFR-COST-03). Audit log. |
| R-21 | Prompt injection. A web page, file, image, MCP result or server log tells the model to act. On the personal bot it tries to reach the dev executor. On the dev bot it tries to exfiltrate data or run commands. | M | H | The personal bot has no path to the dev bot (UC-36), and its tool allowlist is verified at start (NFR-SEC-08). Read-only integrations by default (UC-35). Dev-side risky actions are gated regardless of what the model believes. Injection fixtures (UC-31). |
| R-22 | Secrets leak into Telegram history (cloud chats, not end-to-end encrypted), logs, transcripts or LLM inputs. Examples: a pasted key, an agent `cat`-ing `.env` into a progress message, a server log line. | M | H | Key detection before any LLM sees the message, deletion of the owner's message, masked confirmation (UC-04). Outbound and log scrubber over all stored secrets and key patterns (NFR-SEC-03). Leak test across logs, DB and transcripts. Recommend rotating any key pasted before deletion was confirmed. |
| R-23 | Telegram history in the dev group holds server addresses, project details and errors. A compromised Telegram account exposes them. | L | M | Accepted. Group stays private with no invite link. Owner enables Telegram 2FA (day one). |
| R-24 | Codex lacks Claude's hook and permission surface, so the gate or commit policy behaves differently when the executor is swapped. | M | H | UC-17 AC: the same gate suite must pass on Codex before it ships (P4). If a gate cannot be enforced, Codex is not offered for that action class. |

## 5. Self-configuration and self-update

| ID | What could happen | L | I | Mitigation |
|---|---|---|---|---|
| R-30 | A self-update breaks startup, polling, the gate or the allowlist, and the bot cannot fix itself because it is down. | M | H | Branch, then tests, then switch, then health check, then **automatic rollback** to the last good version within 2 min (UC-19). The self-updater is a separate program with its own runtime and does not import bot code. Three broken-update fixtures plus a `kill -9` mid-switch test. Protected-core changes ask once (Q5). |
| R-31 | The restart kills the session doing the self-update, or running coding turns in other projects, so work is lost. | M | M | Switch only after the turn completes. Other turns finish first (up to 30 min, then the bot asks). Session ids persist and results are reported after the restart. |
| R-32 | The self-updater itself is broken (by a protected-core change, or a dependency update of its own runtime), so there is no safety net. | L | H | Self-updater changes are protected core (asked once) and have their own tests. Its runtime is pinned and never upgraded in the same update as the bots. The watchdog alerts. Manual recovery steps are documented for the Mac. |
| R-33 | A migration in a self-update is not reversible, and the code rollback leaves a DB the old code cannot read. | M | H | Pre-update DB copy, restored on rollback. Migrations must be backward-compatible across one version (failing-migration fixture in UC-19). Polling pauses during the switch, so no messages are written to a DB that may be rolled back. |
| R-34 | Persona or system-prompt changes "do not take" because Claude Code snapshots the system prompt per conversation (`--system-prompt-snapshot`, default on) and reuses it on resume. | H | M | UC-09 AC requires the change on the next message in a warm or resumed session. The architect picks the mechanism (new session with carried summary, snapshot off, or settings sent per turn). |
| R-35 | A settings change by chat breaks behaviour (burst window 0, empty important-decision list, a mis-parsed value) or arrives through injection. | M | H | Validation and ranges. A one-line restatement of every change. Safeguard-weakening changes ask once. Only the owner's own messages can change settings. Versioned with "oldingi holatiga qaytar" (UC-09). |

## 6. External services and Telegram

| ID | What could happen | L | I | Mitigation |
|---|---|---|---|---|
| R-40 | Gemini Uzbek STT/TTS quality: mixed-language terms and project names are misheard, the TTS accent sounds unnatural, preview model names change or get deprecated. The owner's projects already use `gemini-3.5-transcribe-live`, Live `gemini-3.8-live` and TTS `gemini-3.8-flash-tts` (AIAgents). | M | M | Model ids in config, not code. Vocabulary hints with project names. Voice choice by chat (UC-34). Fallback TTS documented in AIAgents `docs/VOICE.md`: Edge `uz-UZ-MadinaNeural` / `SardorNeural` (free, unofficial), OpenRouter `gpt-audio-mini`. If TTS fails, the text is still sent. Use a paid-tier Gemini key: free-tier data may be used for training. |
| R-41 | Telegram limits break output or drop messages. Text max 4,096, caption 1,024. Bot download max 20 MB, upload 50 MB. ~1 message/s per chat, 20/min per group, 429 floods with 15 projects reporting. Voice bubbles need OGG/Opus, and `ffmpeg` is **not installed**. | H | M | Outbound queue with rate limiting and `retry_after`, coalesced progress edits, splitting and `.md` fallback, size checks with explanations (NFR-CAP). OGG/Opus encoder (ffmpeg static binary or PyAV) is a day-one item. |
| R-42 | Bot privacy mode is on, so the dev bot never receives the owner's plain (non-reply, non-command) group messages, and content routing cannot work. | H (if missed) | H | Setup check: privacy mode off, or bot is admin. Delete-messages right. Join-groups disabled after setup (UC-01 AC). |
| R-43 | OAuth MCP from the phone: the provider redirects to `localhost` on the phone, which fails, so the integration cannot be completed away from the Mac. | M | M | Architect picks a flow that works from the phone (device code, or paste the final redirect URL into chat). UC-08 AC requires completing it from the phone. |
| R-44 | Paid API spend runs away (Gemini TTS on every reply, video analysis, OpenRouter). | L | M | Monthly caps, 80% warning, pause at 100% (NFR-COST-02). Separate keys for Yordamchi with provider-side limits. |
| R-45 | Stale project digest gives a confidently wrong report. | M | M | Every report states its data age. Refresh after each turn and on git change. Server checks older than 30 min are refreshed before answering (UC-15). |
| R-46 | The owner opens a session at the Mac while the bot writes to it, and the transcript is corrupted or conflicts. | M | M | Attach detection, queueing, and the bot yields its own turn (UC-16). Session registry format risk: see R-11. |
| R-47 | Auto-created GitHub repos land under the wrong account or go public. | L | M | Private by default, under the `gh` account `abboskhanme` (assumption). Making a repo public is an important decision. |
| R-48 | Mixed-language voice notes are mis-transcribed: Russian or English terms are dropped or translated, or a project name is misspelled and routes wrongly. | M | M | `uz-UZ`/`ru-RU`/`en-US` hints on every request, as in OvozYoz. The vocabulary is built from the registry plus owner-added terms. The dev bot echoes the transcript and target before acting. A mixed-language test set is part of acceptance (UC-05). |
| R-49 | Video downloads (yt-dlp) break when sites change, or conflict with a platform's terms (YouTube). | M | L | Pin and auto-update yt-dlp. On failure, fall back to a summary or the direct link. Personal use only, on the owner's explicit request; the owner accepts the terms risk. |

## 7. Start on day one (blocks the build if late)

- [ ] Two bots in BotFather (dev and personal). Tokens go to the secret store, never to chat
      history or git. Disable join-groups after setup. Dev bot: privacy mode off.
- [ ] Private Telegram group (owner + dev bot), no invite link. Dev bot is admin with
      delete-messages right. Record the owner user id and group chat id. Turn on Telegram 2FA.
- [ ] A second Telegram account for the stranger and negative tests.
- [ ] Gemini API key on the **paid** tier, with a spend cap. OpenRouter key optional.
- [ ] Claude: confirm ClaudeManager is running, and that no `ANTHROPIC_API_KEY` /
      `CLAUDE_CODE_OAUTH_TOKEN` is set in the launchd context. `claude` 2.1.309 at
      `~/.local/bin/claude`.
- [ ] uv-managed Python 3.12 (system Python is 3.9.6). An OGG/Opus encoder (no ffmpeg and no
      Homebrew on this Mac).
- [ ] Power (Q1): `pmset` on charger, auto-update restarts off, decide where the Mac lives.
- [ ] Free disk space (about 31 GB free now).
- [ ] P4 only: ChatGPT plan with Codex. Install the official Codex CLI (Node is at
      `~/.local/bin/node`) and run `codex login`.
- [ ] Integration endpoints and tokens for the owner's read-only MCP servers (Bonvi HR,
      Wunderkind school, LC). Today they are claude.ai account connectors; Yordamchi needs its
      own copies.
