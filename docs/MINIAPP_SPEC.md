# Yordamchi — Panel SPEC (a Telegram Mini App for the dev bot)

Status: for owner approval · 2026-10-25 · `plan-architect`. Inputs: the owner's request of 2026-10-25 (§0.1); `SPEC.md`
§2, §3.2, §4, §5.2–§5.7, §6, §7; `CONVENTIONS.md`; `STACK.md`; `REQUIREMENTS.md` (D2, D8, D12, UC-01, UC-13); `DEPLOY.md`;
the code on 2026-10-25 (`bots/dev/{app,runtime,pipeline,telegram,intents,control_socket}.py`, `dev/{turns,questions,
gate}`, `shared/{access,inbox,burst,outbox,health}`, `adapters/claude/stream.py`, `scripts/install.py`, `bots/cli/check.py`).
This document is the single source of truth for the panel. Where it differs from another document it says so in §0.4,
and it wins for the panel. Every `setting.key = X` is a default in `shared/settings/defaults.py` (§6). Paths without a
prefix are under `src/yordamchi/`.

## 0. Owner summary and settled decisions

### 0.1 The request (owner, 2026-10-25, translated)
"A small mini app. At the bottom, one input line for general things. If a project session is open, it gets its own
card on screen; several sessions sit one under another. Each card has its own chat input under it, so when I write
separately nothing gets mixed up. Each card can look like a computer terminal, showing the process." He chose a
Cloudflare named tunnel on his own domain to reach it from his phone. The bots stay launchd agents on his MacBook.

### 0.2 What gets built (approval point)
- A page that opens inside Telegram, from the dev bot's profile ("Open App") or a `t.me` link. Only you get in.
- One card per project that is working now or worked in the last 12 hours, one under another. Each card is a dark
  terminal: your instruction, every step the agent takes (the command or the file), its text, short results, then the
  result. The header shows the state and a running clock.
- Under each card, its own input. What you type there goes to that project only and is never mixed with anything else.
  `to'xta`, `davom ettir` and `yangi sessiya` work there as typed words, and an agent's question can be answered there.
- At the bottom, one general input that behaves exactly like writing in the group (routing, reports, `holat?`, chat),
  with the bot's general replies shown above it.
- Important decisions (deploy, data loss, money, sending to others) are still asked in the group and answered only
  there. The panel shows them and says "answer in the group".
- The group does not change: the same messages, the same plain progress line. The panel is off until you switch it on,
  and `panelni o'chir` in the group switches it off at once.
- What you do once (about 30 minutes): a Cloudflare account with your domain on it, five terminal commands for the
  tunnel, one BotFather setting, one message in the group. Optional and recommended: Cloudflare Access (an email code
  once a month).

### 0.3 Decisions recorded
Owner's (2026-10-25), not re-opened here:

| # | Decision | Specified in |
|---|---|---|
| P1 | The panel backend runs inside the dev bot process as `core.aio.forever` loops: an `aiohttp` web server (3.14.5, already pinned with aiogram) bound to `127.0.0.1` only | §3.1, §3.2 |
| P2 | Frontend: React 18 + Vite + TypeScript + Tailwind | §3.2, §7.2 |
| P3 | Every API and stream call carries Telegram `initData`, verified server-side (HMAC with the dev bot token, `auth_date` freshness, `OWNER_ID` only; others 401 with only the id logged), then a short-lived session token. Every write is an `OwnerOrigin` minted by `shared/access` | §3.3 |
| P4 | The general input behaves exactly like a group message (same inbox, burst, pre-parse, routing, chat). A card's input goes to its project directly, routing skipped: a follow-up to its working turn, or a new turn. No new execution path. Every important decision still asks in the group | §3.4, §3.7 |
| P5 | Cards: one per project with a live or recent session; a terminal view of steps, text and results (the group keeps `ui.progress_steps = 0`); collapse, state, clock; stop and continue typed into the card; history = the last lines of `turn_progress` plus a live tail | §2, §3.5 |
| P6 | Opening through BotFather's Main Mini App and a `t.me` link. Never an inline keyboard button, never `setChatMenuButton` | §2.6 |
| P7 | Exposure through a Cloudflare named tunnel (`cloudflared tunnel run`) as a fourth LaunchAgent installed by `scripts/install.py --enable-panel`. The credentials JSON stays in `~/.cloudflared`, never in the repo, an env file or a log. The hostname is a setting | §7 |
| P8 | D8 is about Telegram keyboards and menus; controls inside the Mini App's own page are allowed | §0.4 |

Decided here by the architect, with the reason in the section named:

| # | Decision | Specified in |
|---|---|---|
| A1 | The live stream is a **WebSocket** (`/api/stream`) authenticated by its first frame; writes are HTTPS `POST` | §3.5 |
| A2 | Progress reaches the panel by **push** where the data is born in memory (executor steps, sent bot messages) and by a **2 s poll** where the truth is DB state written in many places (card states, new owner deliveries) | §3.6 |
| A3 | Agent questions can be answered from a card. No decision (any `decisions` row) can be approved or declined from the panel; `dev/gate` enforces it | §3.7 |
| A4 | No new table, column or migration | §5 |
| A5 | The panel's own UI text lives in `panel/src/i18n/uz.json`; everything sent to Telegram stays in `core/strings.py` | §2.7 |
| A6 | No new port and no new adapter: the HTTP server is an inbound edge in `bots/dev/panel/`, as `bots/dev/telegram.py` and `bots/dev/control_socket.py` are | §3.2 |
| A7 | The built frontend is committed as `panel/dist/`; neither the installer nor the supervisor runs node | §3.2, §7.2 |
| A8 | `panel.mode = "off"` by default; switching it on weakens a safeguard and asks once in the group | §6 |

### 0.4 Amendments to settled documents
Their text is left as it is; for the panel, this section governs.
- **D2 ("no inbound port") and SPEC §2.1 ("no web server, no inbound port, no cloud relay").** With the panel
  installed there is one web server, inside the dev bot, bound to `127.0.0.1`, and one relay, Cloudflare, which
  `cloudflared` reaches over outbound connections only (QUIC or HTTP/2 to port 7844). Nothing listens on a LAN or
  internet interface. Without `--enable-panel` nothing changes.
- **REQUIREMENTS §5 ("NOT in this version: a web dashboard or admin UI … no cloud relay").** The owner asked for the
  panel on 2026-10-25. It is a conversation surface and a live view, with no settings or admin screen: a settings request
  typed into the general input is a chat message, handled as UC-09 is today.
- **D8.** Read as Telegram keyboards, inline buttons and menus (P8). The bot still never sends `reply_markup` and never
  calls `setChatMenuButton` or `setMyCommands`. The panel's code also never uses Telegram's own client buttons
  (`MainButton`, `SecondaryButton`, `BottomButton`, `SettingsButton`, `BackButton`), only plain controls on its own page
  (§8.5).
- **UC-13 and SPEC §3.2.3 step 5.** A decision is approved or declined only by an owner message in the group. A panel
  message that would bind to a decision changes nothing and gets `S.PANEL_DECISION_IN_GROUP` (§3.7).
- **CONVENTIONS**: §1 (the layout gains `panel/` and `bots/dev/panel/`), §2 (the C5 `aiohttp` line; new rule A10), §3 (a
  panel UI text row), §10 ("there is no frontend": the panel is the one frontend). Unit M0 edits them (§9).

### 0.5 Reference implementations (copy their shape)
| New thing | Copy from |
|---|---|
| `dev/panel/` (domain + service; in-memory state built and wired in `bots/dev/runtime.py`) | `shared/inbox` (the reference feature) and `dev/turns/service.py::TurnsState` with its hooks `questions` and `turn_ended` |
| `bots/dev/panel/server.py` (translate, one service call, hand off) | `bots/dev/telegram.py` (`intake.accepted(origin, await inbox.accept_update(…))`) and `bots/dev/control_socket.py` (a server loop under `aio.forever`) |
| `PanelPass` and its inventory test | `shared/access/domain.py::require_owner_origin` and `tests/unit/shared/access/test_origin_required.py` |
| `--enable-panel`, the `cloudflared` pin, the tunnel plist, "enabled when its plist is installed" | `--enable-personal`, `FFMPEG_PINS`, `ops/launchd/uz.yordamchi.supervisor.plist.tmpl`, `bots/cli/check.py::personal_enabled` |

## 1. Goals, non-goals and scope

### 1.1 Goals
1. From the phone, inside Telegram, see every project that is working or recently worked, each as a live terminal of
   its session.
2. Write to one project without routing and without mixing; write a general message exactly as in the group.
3. Keep every safeguard: owner only (D12, UC-01), an `OwnerOrigin` on every write, the gate unchanged, important
   decisions answered in the group, nothing secret on the wire (NFR-SEC-03), the group's plain line unchanged.
4. Add no execution path, no table and no dependency. The group stays the system of record.

### 1.2 Non-goals in v1
- Voice notes, photos, files or albums from the panel. Say them in the group.
- Approving or declining any decision from the panel.
- A settings, projects, cost or health screen. `holat?` and `xarajat?` stay chat; type them into the general input.
- Showing what the owner typed in Telegram in the general feed. It shows the panel's own inputs and the bot's replies.
- File diffs, file viewers, downloads, and history older than §3.5 gives.
- The personal bot.
- Notifications from the panel, and sending while offline.
- Tunnel monitoring by the bot (the check script covers it, §7.4), and verifying Cloudflare Access JWTs at the origin.

### 1.3 v1 and later
| v1 (this SPEC) | Later (each its own self-update or SPEC) |
|---|---|
| Cards with the terminal trace; typed card commands; question answers; the general input and feed; Main Mini App + link; the Cloudflare tunnel; check lines | voice and photos from the panel · Edit diffs · paging to older lines · Telegram-typed owner messages in the feed · `startapp=<project>` links in result messages · a tunnel watchdog and alert · an Access JWT check · quieter group replies to panel messages · a stop control in the card header |

## 2. UX

### 2.1 Screen layout (phone portrait, about 40 columns)
Main screen: two working cards, one waiting for the owner, one finished, the general dock at the bottom.
```
┌──────────────────────────────────────┐
│ Yordamchi · panel          ● onlayn  │  sticky header: title, connection dot
│ ◆ 1 ta savol kutyapti            ↓   │  attention chip (only when something waits)
├──────────────────────────────────────┤
│╭ BonviHR ────── ● ishlayapti  4:12 ─╮│  card header: project, state chip, clock
││ $ login sahifasini tuzat           ││  the instruction (prompt line)
││ ▸ Read   src/pages/Login.tsx       ││  a tool step: tool + file
││   120 qator o'qildi                ││  its result, short and dim
││ ▸ Edit   src/pages/Login.tsx       ││
││   o'zgartirildi                    ││
││ ▸ Bash   npm test -- login         ││  a tool step: tool + command
││   … yuqorida 41 qator              ││
││   Tests: 3 passed                  ││  the tail of the output
││ Login formasidagi xato tuzatildi,  ││  the agent's text
││ endi commit qilaman.               ││
││ ▌                                  ││  live cursor while running
│├────────────────────────────────────┤│
││ BonviHR'ga yozing…            [ ➤ ]││  the card's own input
│╰────────────────────────────────────╯│
│╭ HRMentor ─── ◆ sizni kutyapti 0:40 ╮│  amber border
││ ? Savol: Qaysi bazani ishlatay?    ││
││   1) PostgreSQL   2) SQLite        ││
││ HRMentor'ga yozing…           [ ➤ ]││
│╰────────────────────────────────────╯│
│╭ OvozYoz ───────── ✓ tugadi   7:02 ─╮│  collapsed: one summary line
││ ✓ Natija: README yangilandi        ││
│╰────────────────────────────────────╯│
├──────────────────────────────────────┤
│ Umumiy ▴                             │  general feed, last 3 items (tap: expand)
│                         siz: holat?  │
│ bot: Ishlayapti: BonviHR (4 daq)…    │
│ Umumiy xabar yozing…          [ ➤ ]  │  the general input, above the safe area
└──────────────────────────────────────┘
```
General feed expanded (a bottom sheet, at most 60 % of the height):
```
├──────────────────────────────────────┤
│ Umumiy ▾                         ✕   │
│ 14:01                   siz: holat?  │
│ 14:01 bot: Onlayn 3 soat · claude    │
│       2.1.309 · ishlayapti: BonviHR  │
│ 14:03       siz: decorator nima o'zi?│
│ 14:03 bot: Decorator — funksiyani    │
│       o'rab, xatti-harakatini…       │
│ 14:05   siz: BonviHR'da testlarni    │
│              ishga tushir            │
│ 14:05 bot: → BonviHR                 │
│ Umumiy xabar yozing…          [ ➤ ]  │
└──────────────────────────────────────┘
```

### 2.2 Card states
The state comes from the card's primary turn (§3.5). Chips are text plus a glyph; colours are the terminal palette of
§2.3.

| State | Turn statuses | Chip | Border | Clock | Collapsed line | Default | Input |
|---|---|---|---|---|---|---|---|
| `running` | `starting`, `running`, `stopping` (sub `stopping`: "to'xtatilmoqda") | ● ishlayapti (green, pulsing) | none | live, from `started_at` | the newest line | expanded | shown |
| `waiting_you` | `waiting_question`, `waiting_decision` | ◆ sizni kutyapti (amber) | amber | live, from `waiting_since` | the question, or "Muhim qaror #N — guruhda javob bering" | expanded | shown |
| `paused` | `paused_runaway` (sub `runaway`), `interrupted` (sub `interrupted`), `waiting_limit` (sub `limit`: "limit HH:MM gacha") | ‖ to'xtatilgan (grey) | none | fixed: how long it ran | the sub label, e.g. "uzildi — «davom ettir» deb yozing" | expanded | shown |
| `queued` | `queued` (sub `queue_<reason>`, plus "N-o'rin") | ⧗ navbatda (grey) | none | live, from `queued_at` | the reason | collapsed | when expanded |
| `done` | `done`, `cancelled` (sub `cancelled`: "bekor qilindi") | ✓ tugadi (green) | none | fixed: `duration_ms` | the `Natija:` line | collapsed | when expanded |
| `failed` | `failed` | ✗ xato (red) | red | fixed | the last notice line | collapsed | when expanded |

- **Order.** Cards keep a stable order: by `since` (when the card appeared), oldest at the top, newest just above the
  general dock. A state change never moves a card. At most `panel.max_cards = 12`; over that, the ended cards with the
  oldest `since` leave first, and a working or waiting card never leaves.
- **Collapse.** A tap on the header toggles it. The owner's choice is kept in `localStorage`
  (`yordamchi.panel.cards` → `{project_id: {turn_id, expanded}}`) until the card's primary turn changes; then the
  default above applies again. Nothing else is stored in the browser: no token, no text.
- **Attention chip.** It counts the `waiting_you` cards. A tap scrolls to the first one.
- **Clock.** `m:ss` under an hour, `h:mm:ss` above, ticking in the browser from the server's `clock` field (§4.3)
  corrected by the server time offset.

### 2.3 The terminal inside a card
Dark whatever the Telegram theme: background `#0d1117`, text `#c9d1d9`, dim `#8b949e`, cyan `#79c0ff`, green `#3fb950`,
amber `#d29922`, red `#f85149`. Font: `ui-monospace, SFMono-Regular, Menlo, monospace`, 13 px. An expanded body is at
most 45 % of the viewport high and scrolls inside. It sticks to the bottom while the owner is at the bottom; when he
scrolls up it stops following and shows a "↓ yangi qatorlar" pill.

| Line kind | Looks like | Source |
|---|---|---|
| `prompt` | green `$ ` + the owner's text (≤ 3 lines, then `…`) | an owner delivery of the turn (Telegram or panel) |
| `tool` | cyan tool label, white argument; a subagent's step is indented `↳` | executor step start |
| `output` | dim, indented; an error is red with `✗` | executor step end, per the table below |
| `text` | plain light text, wrapped | the agent's text |
| `todo` | amber `☐ 2/5 · Running the tests` | `TodoWrite` |
| `result` | green block (the golden result message) | the bot's result message |
| `question` | amber `? ` block | the bot's question message |
| `decision` | amber `! ` block (the question as the group got it) with a state chip; while pending, the hint "guruhda javob bering (ha / yo'q)" | the `decisions` row (`dev/gate` posts decisions straight to the Messenger, not through the outbox, §3.6) |
| `notice` | grey `· ` block | the bot's other messages about the project |
| `system` | grey `── yangi vazifa · 14:05 ──` | the start of a new turn on the card |

Tool output shown under a step (server-side rule, `dev/panel/domain.py::output_lines`; `N` = `panel.tool_output_lines`
= 12):

| Tool | Tool line | Output when it worked | Output when it failed |
|---|---|---|---|
| `Bash` | the command, ≤ 3 lines | the **last** N lines, after "… yuqorida K qator" when cut | the last N lines, red |
| `Read` | the file path | "K qator o'qildi" (count only, never content) | the first N lines, red |
| `Edit`, `Write`, `NotebookEdit` | the file path | "o'zgartirildi" | the first N lines, red |
| `Grep`, `Glob` | the pattern | "K ta natija", then the first min(5, N) lines | red, first N lines |
| `WebFetch`, `WebSearch` | the URL or query | "bajarildi" | the first line, red |
| `Task`, `Agent` | the description | "bajarildi" | the first line, red |
| `mcp__<server>__<tool>` | `<server> · <tool>` (no arguments) | `panel.mcp_output = "hidden"`: "natija yashirilgan (K belgi)"; `"shown"`: as `Bash` | hidden: "xato"; shown: as `Bash` |
| any other | the tool name and its summary | "bajarildi" | the first line, red |

Every visual line is cut at `panel.line_max_chars = 400`; a text block at `panel.text_max_chars = 2000`.

### 2.4 Inputs
| Behaviour | General input | Card input |
|---|---|---|
| Goes to | the dev group's intake, as a group message (§3.4) | the card's project (§3.4) |
| Merges with | other messages inside the burst window, exactly as in the group | nothing: every message stands alone |
| Understands | everything the group understands (SPEC §6) | the card vocabulary below; anything else is an answer or work |
| Enter key | desktop and web (`platform` not `ios`/`android`): sends, Shift+Enter is a new line; iOS and Android: a new line, the ➤ control sends | same |
| Size | auto-grows to 5 lines; ≤ `panel.max_input_chars` = 4,000, a counter shows from 90 % | same |
| After ➤ | an optimistic bubble "yuborilmoqda…"; the input clears | an optimistic `$` line "yuborilmoqda…"; the card expands and scrolls to the bottom |
| Accepted (202) | the bubble shows the stored text from the response (a pasted key reads `[kalit: … ••••abcd]`) | the optimistic line stays until the turn's delivery line with the same text arrives, then it is replaced |
| Failed | "Yuborilmadi — qayta urinish uchun bosing"; a tap resends with the same `client_id`, so it counts once | same |
| Unsent text | while any input holds text, Telegram asks before closing (`enableClosingConfirmation`) | same |

**Card vocabulary** (`dev/panel/domain.py::card_command`). The whole message must be the phrase, after the same
normalisation routing uses (lower case, apostrophe variants → `'`, spaces collapsed, a final `.` or `!` dropped). The
stop, redirect and hold words are `shared/burst/domain.py::interrupt_of`, so they have one definition. `P` is the card's
project. The rows apply **in this order**, which mirrors the group's pre-parse (interrupts first, then answers to open
items, then commands, SPEC §6.1). So `davom ettir` typed while P's agent asks a question is the answer to it, as in the
group.

| # | Typed | Action | Reply (sent to the group, mirrored into the card) |
|---|---|---|---|
| 1 | `kut` | ignored (nothing is delivered; a card message never merges, so there is nothing to wait for) | none |
| 2 | `to'xta`, `stop`, `bas`, `hammasini to'xtat` | `turns.stop_turn(P)`: this project only | `S.STOPPED` or `S.STOP_NOTHING` |
| 3 | `yo'q, boshqacha qil: X` | `turns.redirect_turn(P, X)` | the new turn's lines |
| 4 | an approval or denial word (± `#id`) while a decision of P is pending | nothing changes | `S.PANEL_DECISION_IN_GROUP_PROJECT` |
| 5 | anything, while an agent question of P is open | the answer to P's oldest open question (a number picks the option) | as today (`dev/questions`) |
| 6 | `davom ettir`, `davom`, `continue` | `turns.continue_turn(P)` | `S.TURN_CONTINUED` or `S.TURN_NOTHING_TO_CONTINUE` |
| 7 | `qayta yubor` | `turns.retry_delivery(P)` | as today |
| 8 | `yangi sessiya` | `turns.new_session(P)` | as today |
| 9 | anything else | work for P: a `next` follow-up into its working coding turn, else a new turn | progress lines, then the result |

The card input's placeholder is `{project}ga yozing…`; a hint row under an empty input reads
"to'xta · davom ettir · yangi sessiya".

### 2.5 Empty, error and connection states
| Situation | Screen |
|---|---|
| Loading | header plus three grey skeleton cards |
| No cards | "Hozir ochiq sessiya yo'q. Pastdagi qatorga yozing — masalan «BonviHR'da login sahifasini tuzat»." |
| Empty feed | "Bu yerda umumiy xabarlaringiz va botning javoblari ko'rinadi." |
| Opened outside Telegram (no `initData`) | full screen: "Bu panel faqat Telegram ichida ochiladi." |
| `auth_failed`, `origin_rejected` (a stranger gets the same) | full screen: "Kirish rad etildi." |
| `init_data_expired`, `session_expired`, `session_revoked` and a fresh session impossible | full screen: "Panelni yopib, qayta oching." |
| `panel_off` | full screen: "Panel o'chirilgan. Guruhda `panelni yoq` deb yozing." |
| Stream down, the server reachable again soon | banner: "Aloqa uzildi — qayta ulanmoqda…"; the cards grey out with "Ma'lumot HH:MM dagi holat"; inputs stay usable |
| HTTP 502/503/504 (the bot is restarting or the Mac is unreachable through the tunnel) | banner: "Bot javob bermayapti. Qayta ulanmoqda…" |
| No network | banner: "Internet yo'q yoki Mac o'chiq. Telegram guruhidan foydalaning." |
| `updating` on send | the bubble: "Bot yangilanmoqda. Xabar yuborilmadi — birozdan keyin qayta yuboring." |
| `rate_limited`, `auth_locked` | "Juda tez. Bir oz kutib, qayta urinib ko'ring." / "Kirish vaqtincha yopilgan. 10 daqiqadan keyin urinib ko'ring." |
| `too_long` | "Xabar juda uzun ({max} belgigacha)." |
| `project_unknown` | "Bunday loyiha yo'q." |

### 2.6 Opening the panel
- **Main Mini App** (the owner sets it once in BotFather, §7.1): Telegram shows "Open App" on the dev bot's profile, and
  `https://t.me/<bot_username>?startapp` opens it from any chat, the dev group included.
- **Direct link (optional):** after BotFather's `/newapp` with the short name `panel`, `https://t.me/<bot_username>/panel`;
  then set `panel.app_name = "panel"` by chat, and the bot's link uses it.
- **By chat:** `panel` (or `/panel`) in the group answers `S.PANEL_LINK` with the link. A pinned text link is acceptable:
  it is a text message, not a keyboard or a menu. The bot never pins it itself; the owner may pin it by hand.
- **Never:** an inline or reply keyboard, `setChatMenuButton` (the check asserts the default menu button), or
  `setMyCommands`.
- **`start_param`:** if it names a card's project, the client scrolls to that card and expands it. v1 never generates
  such links.
- **Telegram calls the client makes** (only in `panel/src/telegram.ts`, §8.5): `ready()`, `expand()`,
  `disableVerticalSwipes()` (cards scroll inside), `enableClosingConfirmation()` / `disableClosingConfirmation()`,
  `setHeaderColor`, `setBackgroundColor`, `HapticFeedback.impactOccurred("light")` on send, `onEvent`/`offEvent` for
  `themeChanged`, `viewportChanged`, `safeAreaChanged`, `contentSafeAreaChanged`; reads `initData`, `platform`, `version`,
  `colorScheme`, `themeParams`, `safeAreaInset`, `contentSafeAreaInset`, `viewportStableHeight`,
  `initDataUnsafe.start_param` (focus only, never trusted); `isVersionAtLeast` guards each call.

### 2.7 UI strings: where they live
- **Telegram text** (anything the bot sends because of the panel) goes in `core/strings.py` under `# --- panel ---`,
  as `Msg` entries (A5, `BotText`).
- **The panel's own chrome** (labels, placeholders, chips, empty and error states) goes in `panel/src/i18n/uz.json`.
  Why: the rule "bot text only from `core/strings.py`" exists so that everything the Messenger sends is catalogued and
  typed as `BotText`; the panel's chrome never goes through the Messenger, and a TypeScript build cannot import Python.
  The server never sends a UI sentence: it sends codes (`state`, `sub`, `error.code`) and content that already is bot
  text (mirrored outbox messages) or scrubbed agent output. A Python test keeps every wire code in step with `uz.json`
  (§8.5).

`core/strings.py`, block `# --- panel ---`:

| Entry | Text | Sent when |
|---|---|---|
| `PANEL_ECHO` | `Paneldan: «{text}»` | `panel.echo_to_group` is true and the owner used the general input |
| `PANEL_ECHO_PROJECT` | `**{project}** · paneldan: «{text}»` | the same, for a card input |
| `PANEL_DECISION_IN_GROUP` | `Muhim qaror #{decision_id}ga paneldan javob berib bo'lmaydi — shu guruhda yozing: \`ha\` yoki \`yo'q\`.` | a general panel message bound to a decision (§3.7) |
| `PANEL_DECISION_IN_GROUP_PROJECT` | `**{project}** · muhim qaror #{decision_id}ga paneldan javob berib bo'lmaydi — shu guruhda yozing: \`ha\` yoki \`yo'q\`.` | a card message bound to a decision of its project |
| `PANEL_LINK` | `**Panel:** {link}` | `panel` in chat, the panel on and its hostname set |
| `PANEL_LINK_PROFILE` | `**Panel:** bot profilidagi «Open App» orqali oching.` | the same, when the bot's username could not be read |
| `PANEL_OFF` | `Panel o'chirilgan. Yoqish: \`panelni yoq\` (bir marta tasdiqlaysiz).` | `panel` in chat while `panel.mode = off` |
| `PANEL_NO_HOST` | `Panel manzili sozlanmagan. Yozing: \`sozlama: panel manzili <host>\`.` | `panel` or `panelni yoq` while `panel.hostname` is empty |
| `PANEL_NOT_INSTALLED` | `Panel bu Mac'da o'rnatilmagan (\`scripts/install.py --enable-panel\`).` | `panel` without `PANEL_PORT` in `dev.env` |
| `PANEL_AUTH_LOCKED` | `Panelga {count} marta noto'g'ri kirishga urinildi — yangi kirishlar {minutes} daqiqaga yopildi. Bu siz bo'lmasangiz: \`panelni o'chir\` va bot tokenini almashtiring.` | the auth lock trips (§3.8 S12), once per lock |
| `HEALTH_PANEL_ON` | `Panel: yoqilgan · {hostname} · ochiq oynalar: {streams}` | a `holat?` line |
| `HEALTH_PANEL_DOWN` | `Panel: yoqilgan, lekin server ishlamayapti ({reason})` | a `holat?` line; `reason` is `port band` or `xato` |
| `HEALTH_PANEL_OFF` | `Panel: o'chirilgan` | a `holat?` line (only when `PANEL_PORT` is set) |

`panel/src/i18n/uz.json` (flat keys; `{x}` placeholders; `t(key, params)` is typed by `keyof`):
```json
{
  "app.title": "Yordamchi", "app.subtitle": "panel",
  "conn.online": "onlayn", "conn.connecting": "ulanmoqda…", "conn.offline": "aloqa yo'q",
  "conn.stale": "Ma'lumot {time} dagi holat", "conn.lost": "Aloqa uzildi — qayta ulanmoqda…",
  "attention.waiting": "{n} ta savol kutyapti",
  "card.state.running": "ishlayapti", "card.state.waiting_you": "sizni kutyapti", "card.state.paused": "to'xtatilgan",
  "card.state.queued": "navbatda", "card.state.done": "tugadi", "card.state.failed": "xato",
  "card.sub.stopping": "to'xtatilmoqda", "card.sub.limit": "limit {time} gacha",
  "card.sub.runaway": "uzoq ishladi — «davom ettir» deb yozing", "card.sub.interrupted": "uzildi — «davom ettir» deb yozing",
  "card.sub.cancelled": "bekor qilindi", "card.sub.queue_cap": "bir vaqtda ishlash limiti",
  "card.sub.queue_pressure": "xotira band", "card.sub.queue_disk": "disk to'lgan", "card.sub.queue_mac_attached": "Mac'da ochiq",
  "card.sub.queue_update_drain": "yangilanish kutilmoqda", "card.sub.queue_busy": "oldingi vazifa tugashini kutyapti",
  "card.queue_position": "{n}-o'rin", "card.steps": "{n} qadam", "card.new_task": "yangi vazifa · {time}",
  "card.decision_hint": "Muhim qaror #{id} — guruhda javob bering (ha / yo'q)",
  "decision.state.pending": "kutilmoqda", "decision.state.approved": "tasdiqlandi", "decision.state.denied": "rad etildi",
  "decision.state.expired": "muddati o'tdi", "decision.state.consumed": "bajarildi",
  "decision.state.superseded": "almashtirildi", "decision.state.cancelled": "bekor qilindi",
  "card.input.placeholder": "{project}ga yozing…", "card.input.hint": "to'xta · davom ettir · yangi sessiya",
  "card.new_lines": "↓ yangi qatorlar", "card.expand": "Ochish", "card.collapse": "Yig'ish",
  "line.output.read": "{n} qator o'qildi", "line.output.edited": "o'zgartirildi", "line.output.found": "{n} ta natija",
  "line.output.hidden": "natija yashirilgan ({n} belgi)", "line.output.cut": "… yuqorida {n} qator",
  "line.output.more": "… yana {n} qator", "line.output.ok": "bajarildi", "line.output.error": "xato",
  "general.title": "Umumiy", "general.placeholder": "Umumiy xabar yozing…", "general.you": "siz", "general.bot": "bot",
  "general.empty": "Bu yerda umumiy xabarlaringiz va botning javoblari ko'rinadi.",
  "empty.cards": "Hozir ochiq sessiya yo'q. Pastdagi qatorga yozing — masalan «BonviHR'da login sahifasini tuzat».",
  "send": "Yuborish", "sending": "yuborilmoqda…", "send_failed": "Yuborilmadi — qayta urinish uchun bosing",
  "time.just_now": "hozirgina", "time.minutes": "{n} daq", "time.hours": "{n} soat",
  "errors.not_telegram": "Bu panel faqat Telegram ichida ochiladi.",
  "errors.auth_failed": "Kirish rad etildi.", "errors.origin_rejected": "Kirish rad etildi.",
  "errors.init_data_expired": "Panelni yopib, qayta oching.", "errors.session_invalid": "Panelni yopib, qayta oching.",
  "errors.session_expired": "Panelni yopib, qayta oching.", "errors.session_revoked": "Panelni yopib, qayta oching.",
  "errors.panel_off": "Panel o'chirilgan. Guruhda «panelni yoq» deb yozing.",
  "errors.not_found": "Topilmadi.", "errors.project_unknown": "Bunday loyiha yo'q.",
  "errors.updating": "Bot yangilanmoqda. Xabar yuborilmadi — birozdan keyin qayta yuboring.",
  "errors.too_long": "Xabar juda uzun ({max} belgigacha).", "errors.bad_request": "So'rov noto'g'ri.",
  "errors.rate_limited": "Juda tez. Bir oz kutib, qayta urinib ko'ring.",
  "errors.auth_locked": "Kirish vaqtincha yopilgan. 10 daqiqadan keyin urinib ko'ring.",
  "errors.server": "Bot javob bermayapti. Qayta ulanmoqda…",
  "errors.offline": "Internet yo'q yoki Mac o'chiq. Telegram guruhidan foydalaning."
}
```

## 3. Architecture

### 3.1 Process and network shape
```
 Owner's phone ── Telegram app ── Mini App webview: https://<panel.hostname>/
                                        │ HTTPS + WSS
                                        ▼
                     Cloudflare edge (TLS ends here; optional Access, §7.7)
                                        ▲ outbound only: QUIC or HTTP/2 to :7844
 MacBook ───────────────────────────────┼──────────────────────────────────────────────────
   uz.yordamchi.tunnel   DATA/bin/cloudflared tunnel … run      (fourth LaunchAgent, §7.3)
                                        │ http://127.0.0.1:PANEL_PORT  (loopback only)
   uz.yordamchi.dev      aiohttp server (bots/dev/panel) ── dev/panel ── turns · questions · gate
                         inbox · burst · outbox · settings · access (unchanged paths)
                         Telegram long polling, the group, the gate: as today
```
- The aiohttp server binds `127.0.0.1:<PANEL_PORT>` (bootstrap value, §6.2). It never binds `0.0.0.0` or `::`.
- The server runs only in normal mode: never in probation (SPEC §3.2.12 step 6) and never in `--dry-run`.
- Loops added to SPEC §2.5 step 7, each under `core.aio.forever`, started only when `PANEL_PORT` is set:
  `panel_http` (the server), `panel_traces` (executor steps), `panel_sent` (sent bot messages), `panel_poll` (card
  states, deliveries, ticks, expiry).
- Shutdown (SPEC §2.6): the panel loops are cancelled with the others. Open streams get `bye{reason: "shutdown"}` and
  close code 1001; aiohttp gets `shutdown_timeout = 2` s for requests in flight.

### 3.2 Module layout and layers
```
panel/                                   frontend (React 18, Vite 8, TS 7, Tailwind 4); built output committed
  package.json  package-lock.json  index.html  vite.config.ts  tsconfig.json  vendor.json
  src/  main.tsx  App.tsx  telegram.ts  api.ts  stream.ts  store.ts  types.ts  markdown.ts  time.ts  styles.css
        i18n/{uz.json,index.ts}
        components/{Header,CardList,Card,Terminal,TermLine,CardInput,GeneralDock,GeneralFeed,GeneralInput,Screen}.tsx
  public/vendor/telegram-web-app.js      Telegram's script, vendored, SHA-256 pinned in vendor.json
  test/  *.test.ts(x)  fixtures/wire/*.json   (the wire fixtures are shared with the Python tests, §8.5)
  dist/  index.html  assets/*  vendor/telegram-web-app.js  build.json      (committed; §7.2)
src/yordamchi/
  dev/panel/        __init__.py  domain.py  service.py           the feature (no store.py, no models.py: no tables)
  bots/dev/panel/   __init__.py  server.py  security.py           the inbound HTTP/WS edge (security.py: protected core)
scripts/panel_dist.py                                             stdlib: writes and verifies panel/dist/build.json
ops/launchd/uz.yordamchi.tunnel.plist.tmpl  ops/cloudflared/config.yml.tmpl   (both protected core)
```

**Responsibilities.**
| Module | Holds | Never |
|---|---|---|
| `dev/panel/domain.py` (pure, C6, A1) | card mapping and order, line building (§2.3 tables), timeline merge, the card vocabulary, panel update-id allocation, session expiry and token-bucket arithmetic, wire JSON shapes (`card_json`, `line_json`, `feed_json`, `frame_json`), `PanelError` and its HTTP status, the constants of §6.3 | I/O, time reads, sockets, aiohttp |
| `dev/panel/service.py` | `PanelState` (memory: sessions, rings, subscribers, rate state; methods `subscribe`, `unsubscribe`, `status`); `new_state`, `open_session`, `resolve_session`, `read_state`, `submit_message`, `panel_health`, `panel_link`; the loop bodies `run_traces`, `run_sent`, `run_poll` (§3.10) | SQL (it calls other features' services), HTTP |
| `bots/dev/panel/server.py` | the aiohttp `Application`, routes, static files, the three JSON handlers (one service call each, A10), the WebSocket pump, `serve_panel` (the `panel_http` loop body) | policy, SQL, a model call |
| `bots/dev/panel/security.py` | the middlewares, in this order: headers → host/mode → origin → content → session → rate (§3.8) | anything else |

**Layers and imports** (CONVENTIONS §2):
- `bots.dev.panel` → `dev.panel` → `dev.turns`, `dev.questions`, `dev.gate`, `dev.projects` services and domains →
  `shared.*`. C1 holds as is.
- The only contract change: **C5-aiohttp** gains `yordamchi.bots.dev.panel.**` as an allowed importer.
- `dev/panel` is under `yordamchi.dev`, so C7 already keeps it out of the personal bot.
- A2 holds: the panel calls `turns.service`, `questions.service`, `gate.service`, `inbox.service`, `outbox.service` and
  `settings.service`, never a store.

**Ports involved.** No new port.
- `Messenger` is used through the outbox (echoes, refusals, replies), as today.
- `AgentRuntime` is used through `dev/turns` (card work, stops, continues), as today.
- `Clock` and `settings.snapshot` are used everywhere.
- The aiohttp *server* is an inbound edge of the bots layer, the role `bots/dev/telegram.py` plays for aiogram, so it
  needs no Protocol. The panel makes no outbound call to an external system: the tunnel is checked by the check script
  (§7.4), not by the bot.

### 3.3 Identity: `initData` → `PanelPass` → `OwnerOrigin`
All of it lives in `shared/access` (protected core; this SPEC is the explicit task that changes it).

**1. Secret key.** `access.domain.panel_secret_key(bot_token) = HMAC_SHA256(key=b"WebAppData", msg=bot_token)`. It is
computed once in `bots/dev/app.py::build` from `DevConfig.bot_token`. `PanelState` keeps only these 32 bytes, never the
token. The test vector for the factory token is `7304d4c18c945997bea841be606197d87a111ea1c2f4efd2c9c3678107aab318`.

**2. `verify_init_data(raw, *, secret_key, now, max_age_s) -> VerifiedLaunch | LaunchRefused`** (pure), in this order:
1. More than 4,096 bytes → `TOO_LONG`.
2. `urllib.parse.parse_qsl(raw, keep_blank_values=True, strict_parsing=True)`. An error or a duplicate key → `MALFORMED`.
3. `hash` must be 64 lowercase hex, else `MALFORMED`; `auth_date` must be a decimal integer, else `MALFORMED`.
4. Data-check string: every received pair except `hash`, **`signature` included**, sorted by key, joined as `key=value`
   with `\n`. This is Telegram's first-party rule ("all received fields"), and aiogram's `check_webapp_signature` does
   the same; vector V3 (§8.1) proves `signature` is covered.
5. `hmac.compare_digest(HMAC_SHA256(secret_key, dcs).hexdigest(), hash)`, else `BAD_HASH`.
6. `auth_date − now > 60 s` → `FROM_FUTURE`; `now − auth_date > max_age_s` (`panel.init_data_max_age_s` = 3,600) →
   `EXPIRED`. Exactly at the limit is accepted.
7. `user` must be a JSON object whose `id` is an integer (not a bool), else `NO_USER`.
8. Result: `VerifiedLaunch(user_id, auth_date, launch_id=hash)`.

The third-party Ed25519 `signature` check is not used: the bot holds the token, and HMAC is Telegram's method for the
bot's own backend.

**3. `admit_panel(launch, policy, *, now) -> PanelPass | Drop`** (pure). `policy.bot` must be `DEV` (else `ValueError`).
A user other than `policy.owner_id` → `Drop(STRANGER)`. Otherwise
`PanelPass(bot=DEV, user_id, launch_id, auth_date, issued_at=now)`. Only `shared/access` calls `PanelPass(` (A5 is
extended). `require_panel_pass(obj)` raises `TypeError` for anything that is not a minted `PanelPass`, exactly as
`require_owner_origin` does.

**4. Sessions** (`dev/panel/service.py::open_session`), for `POST /api/session`:
- Order: the per-IP rate (`panel.sessions_per_minute` = 10, key = the `CF-Connecting-IP` header or `"local"`) → the auth
  lock → `access.service.admit_panel_launch` → at most 3 sessions per `launch_id` → at most `panel.max_sessions` = 4
  (the least recently seen session is evicted, and its streams get `bye{revoked}`).
- Token: 32 random bytes, urlsafe base64 (43 characters), returned once. `PanelState` keeps `sha256(token)` →
  `PanelSession(pass, created_at, last_seen_at, hostname, owner_id)`.
- Idle limit `panel.session_idle_min` = 30; absolute limit `panel.session_max_h` = 12. Any authenticated request or
  client frame refreshes `last_seen_at`.
- A session is revoked when `panel.mode` becomes `off`, `panel.hostname` changes, or `access.owner_id` changes. It is
  checked on every request and every tick.
- Audit: one `audit_events` row, kind `panel_session` (new `AuditKind`), for `opened`, `refused`, `locked` and
  `revoked`, with `{event, user_id, reason}` and no text.
- Logs: `panel_session_opened(session=<first 8 hex of the token hash>, user_id)`, and
  `panel_auth_dropped(reason, user_id)`, where `user_id` is only the verified id of a valid launch (a stranger) and
  `None` otherwise. Nothing else about a stranger is logged.

**5. Minting the write origin: `access.domain.mint_panel_origin(panel_pass, policy, *, update_id) -> OwnerOrigin | Drop`.**
- It calls `require_panel_pass` first; then `policy.bot` must be `DEV` and `channel_of_update_id(update_id)` must be
  `PANEL` (otherwise `ValueError`).
- `panel_pass.user_id != policy.owner_id` (the owner id changed since the session opened) → `Drop(STRANGER)`.
- Otherwise `OwnerOrigin(bot=DEV, update_id, user_id, chat_id=policy.home_chat_id, channel=Channel.PANEL)`.
- The service wrapper `access.service.panel_origin(deps, panel_pass, *, update_id) -> OwnerOrigin | None` reads today's
  policy from the settings snapshot.

**6. The channel.** `OwnerOrigin` and `Arrival` gain `channel: Channel = Channel.TELEGRAM` (`TELEGRAM | PANEL`).
- `channel_of_update_id(update_id)` is the one definition of the range: `update_id ≤ −1` is `PANEL`, anything else is
  `TELEGRAM` (Telegram's update ids are positive).
- `admit()` drops an arrival whose channel disagrees with its id (`Drop(NOT_SERVED)`), and mints with
  `channel = arrival.channel`.
- `inbox.service.origin_of_update` builds its `Arrival` with `channel_of_update_id(row.update_id)`, so a burst routed
  later, or after a restart, carries the panel channel into `dev/questions`, `dev/gate` and `dev/turns`.
- Every existing `OwnerOrigin` construction keeps working (the default applies).

**7. Inventory tests** (CONVENTIONS §5, A5):
- `test_origin_required.py`: its AST inventory gains `yordamchi.dev.questions.service.answer_card` (the only new public
  function taking `origin: OwnerOrigin`), with a lookalike refusal case.
- `test_panel_pass_required.py` (new, same pattern): every public function or method in `src/` with a parameter
  annotated `PanelPass` raises `TypeError` for a lookalike before anything is read or written. Today's list:
  `access.domain.mint_panel_origin`, `access.service.panel_origin`, `panel.service.read_state`,
  `panel.service.submit_message`, `PanelState.subscribe`.

### 3.4 Write path
**The general input** (`POST /api/messages`, `project_id: null`) and **a card input** (`project_id: "P"`) share steps
1–4:
1. `security.py`: Host, mode, Origin, content type, session → `request["panel_pass"]`, then the rate
   (`panel.messages_per_minute` = 30 per session).
2. The handler (A10, one service call):
   `accepted = await panel.submit_message(deps, state, request["panel_pass"], message)`. A `PanelRefusal` becomes the
   JSON error of §4.5. On success the handler calls `intake.accepted(accepted.origin, accepted.accepted)` when both are
   set (a duplicate has neither), exactly as a Telegram handler does (it wakes the burst loop, and checks a speech key
   the text held), and answers 202.
3. `submit_message`:
   1. `require_panel_pass`.
   2. Validate: text not empty after `strip()` and ≤ `panel.max_input_chars`; for a card, `projects.get_project(P)`
      exists and is not hidden.
   3. `paths.drain_flag` exists → `updating` (a self-update is draining; writes would be lost on a rollback restore).
   4. Under `state.write_lock`, deduplicate: `inbox.find_panel_update(deps, client_id, since_id=id_floor(now − 10 min))`.
      If found, return it with `duplicate: true` and its stored text.
   5. `update_id = domain.next_update_id(now, state.last_update_id)` (§5.2).
   6. `origin = await access.panel_origin(deps, pass, update_id=update_id)`; `None` → `session_revoked`.
   7. `accepted = await inbox.accept_update(deps, origin, IncomingUpdate(bot=DEV, update_id, chat_id=origin.chat_id,
      kind=kind_for_text(text), user_id=origin.user_id, message_id=None, text=text,
      panel=PanelMark(client_id, card=P or None), sent_at=now))`. This is the same intake as a Telegram update: secrets
      vaulted and masked before any write, the redacted payload stored, the ack queued.
   8. `state.last_update_id = update_id`. A general input also publishes a `feed` frame (id `u<|update_id|>`), so other
      open devices see it.
4. The burst loop attaches the update. A general part takes the ordinary window (SPEC §4.4). A card part stands alone
   and closes at once (`burst.domain.place`: a part whose `content.panel.card` is set → `Into.ALONE`,
   `close_alone = CloseReason.PANEL_CARD`, and the assembling burst is left alone). A card burst whose text is a stop or
   redirect word sorts first in `in_route_order`, like an interrupt.

**5a. General:** the pipeline handles the burst exactly as a group burst: interrupts, early commands, clarifications,
answers, commands, corrections, routing, chat (SPEC §6.1). The only difference is §3.7.

**5b. Card:** `Pipeline.handle` takes the card branch right after its synthetic check and before interrupts:
`if item.panel_card is not None: await self._card(item); return True`. `_card`, in this order (the rows of §2.4;
P = `item.panel_card`; text = `item.own_text`; "cancel" = `burst.cancel_burst`, and if that returns False another pass
had the burst, so stop):
1. `origin = await inbox.origin_of_update(deps, item.parts[-1].update_id)`. If it is `None` or not `Channel.PANEL`,
   cancel and log `panel_card_without_origin`.
2. `cmd = panel_domain.card_command(text)`:
   - `HOLD` → cancel;
   - `STOP` → cancel, `turns.stop_turn(P)`, reply its text if any;
   - `REDIRECT(x)` → cancel, `turns.redirect_turn(TurnRequest(project_id=P, text=x, burst_id=item.burst_id))`.
3. `pending = [p for p in await gate.pending_items(deps) if p.kind == "decision" and p.project_id == P]`. If
   `gate.domain.bind_approval(text, reply_to=None, pending=pending, mentioned_projects=(P,), names=(P,))` returns
   `Bound` or `Ambiguous` → cancel and reply `S.PANEL_DECISION_IN_GROUP_PROJECT(project=P, decision_id=…)` (for
   `Ambiguous`, the first of its ids). Nothing changes.
4. `await questions.answer_card(deps, turns_state, origin, item, P)` is not `None` → done (it bound the answer to P's
   oldest open question and recorded the burst as routed, method `binding`).
5. `cmd` is `CONTINUE`, `RETRY` or `NEW_SESSION` → cancel and reply with what `turns.continue_turn(P)`,
   `turns.retry_delivery(P)` or `turns.new_session(P)` returns.
6. `await turns.submit_card_turn(deps, turns_state, item, P)`, in one unit of work, as `route_turn` does:
   - `routing.record_route` with `RouteDecision(outcome=MERGE if a coding turn of P is working else ROUTE,
     method=Method.PANEL, intent=INSTRUCT, project_id=P, confidence=1.0, candidates=((P, 1.0),),
     reason="panel_card")`;
   - `turns.submit(request_from_input(item, P, kind=INSTRUCT, merge=True, route_id=…))`: `next` into the working coding
     turn, else a new turn that queues behind any active one;
   - `_work_accepted`.

   No "→ P" ack is sent: the card is the project.

Replies (stops, continues, questions, results) go to the group as today and come back to the card through the sent
queue (§3.6).

**Echo (`panel.echo_to_group`, default `false`).** When true, `inbox.accept_update` queues, in the same unit of work as
the update:
- `S.PANEL_ECHO(text)` or `S.PANEL_ECHO_PROJECT(project, text)`, the redacted text cut at 300 characters;
- kind `echo`, priority 0 (so it precedes the reply), `disable_notification=True`, ref `panel-echo-<|update_id|>`.

The default is off: the T37 load test showed the group's 20 messages a minute are the bottleneck, and every echo would
spend one.

### 3.5 Read path and stream path
**WebSocket, not SSE**, because:
1. Browsers cannot set headers on `EventSource`. The token would go in the URL (and so into Cloudflare's and the
   browser's logs) or in a cookie (which brings CSRF back). A WebSocket authenticates in its first frame instead.
2. aiohttp's `WebSocketResponse(heartbeat=…)` is first class and runs in tests over a `UnixSite`, which the closed
   network allows.
3. Every reconnect takes a fresh snapshot (the state is small), so SSE's `Last-Event-ID` replay buys nothing.
4. Writes stay on `POST`, for idempotency and HTTP status codes.

Cloudflare closes idle WebSockets after 100 s; the server sends a `tick` every 15 s and aiohttp pings every 20 s.

**Snapshot** (`GET /api/state` → `panel.read_state`), with `seq` read **first** and `now` the server time:
1. `rows = turns.list_card_turns(deps, since=now − panel.recent_h)`: every turn that is queued or active, or ended at or
   after `since`, of a non-hidden project, with its queue position.
2. Per project, the **primary turn** is the latest active turn of a coding kind (`instruct`, `plan`, `build`,
   `selfupdate`); else the latest active turn; else the latest turn. The card's `since` is the earliest `queued_at`
   among the project's rows. Trim to `panel.max_cards` (§2.2).
3. Lines per card, merged by `(at, id)`, the newest `panel.history_lines` = 60 kept:
   - `prompt` lines: `turns.list_owner_deliveries(deps, turn_ids=…)` (deliveries with `source = owner`);
   - executor lines, per turn T: the `turn_progress` rows of T older than the first ring line of T
     (`turns.list_turn_progress(deps, turn_ids, per_turn=60)`, mapped by `progress_line_of`), then the ring lines of T;
   - bot lines: `outbox.list_recent_sent(deps, chat_id=<group>, since=min(card since, now − 24 h), limit=500)` with
     `about.project_id = P` and kind in {`result`, `question`, `notice`, `report`, `system`, `file`};
   - `decision` lines: `gate.list_card_decisions(deps, since=card since)` rows of P, id `g<decision_id>`, with their
     `state`;
   - a `system` separator at each turn's `queued_at` after the first.
4. Feed, the newest `panel.feed_items` = 30 since `now − 24 h`:
   - the panel's general inputs (`inbox.list_panel_updates`, `card` unset);
   - plus sent bot messages to the group of kind in {`ack`, `echo`, `report`, `notice`, `question`, `result`, `system`,
     `file`} whose `project_id` is null, or whose kind is `echo` (routing's "→ X"), or whose project has no card;
   - plus decisions with no project, or whose project has no card (`gate.list_card_decisions`, id `g<decision_id>`);
   - minus refs `panel-echo-*`, and minus kinds `progress` and `heartbeat` everywhere.
5. `attention` = the `waiting_you` projects and the count of `gate.pending_items`.
6. Every string passes `redact.scrubbed` here (the one panel choke point, §3.8 S13), though each was scrubbed when
   stored.

**Stream hub** (`PanelState` + three loop bodies):
- `run_traces(deps, state, turns_state.traces)`:
  - for each `TurnTrace`: skip hidden projects;
  - build its line(s) with the snapshot's limits (one `settings.snapshot` per drained batch);
  - append to `rings[project]`, a `deque(maxlen=panel.ring_lines = 300)`; record the turn's first ring time;
  - publish `line`.
- `run_sent(deps, state, sent_queue)`: for each `SentNote` to the dev group: a note about a project that has a card,
  of a kind in step 3's set, becomes a `line` for that card; any other note that step 4's rules admit becomes a `feed`
  item; the rest (`progress`, `heartbeat`, `panel-echo-*`) is dropped.
- `run_poll(deps, state)`:
  - every `panel.state_poll_s` = 2 s while at least one stream is open, else every 15 s for expiry only;
  - recompute the cards without lines; publish `card` for new or changed ones and `card_removed` for gone ones; publish
    a `system` line when a card's primary turn changes;
  - read `turns.list_owner_deliveries(deps, after_id=cursor, limit=100)` and publish `prompt` lines;
  - read `gate.list_card_decisions(deps, since=now − panel.recent_h)` and publish a `line` (or `feed` item) for each
    decision that is new or whose `state` changed; a frame with a known id replaces that line (§4.4);
  - every 15 s publish `tick`, expire and revoke sessions (§3.3), and prune the rings of projects without a card for
    more than `panel.recent_h`.
- **Publishing**: `seq += 1`, then each subscriber's `asyncio.Queue(maxsize=256)` takes it with `put_nowait`. A full
  queue marks that subscriber `overflowed`; its pump clears the queue, sends `resync`, and the client re-snapshots.
- **Subscribing**: `PanelState.subscribe(pass) → Subscription | PanelRefusal`, at most 3 per session and 8 in all.

**Client sequence** (`stream.ts`, `store.ts`):
1. `POST /api/session`.
2. Open the WebSocket and send `auth`; receive `ready{epoch, seq}`.
3. `GET /api/state`, while buffering frames.
4. Drop buffered frames with `seq` ≤ `snapshot.seq`; apply the rest. Lines and feed items are keyed by `id`: a known
   id replaces the old entry (only decisions ever change), so a frame that repeats the snapshot is harmless.
5. Reconnect with backoff 1, 2, 4, 8, 16, 30 s (±20 % jitter), always back to step 2. A new `epoch` (the bot restarted)
   or a `resync` triggers step 3 again.
6. A close code 4401 triggers step 1 again with the same `initData`; if that answers `init_data_expired`, show "Panelni
   yopib, qayta oching". No frame for 40 s means the stream is dead: close it and reconnect.
7. A `ping` every 20 s while `document.visibilityState` is `visible`.

### 3.6 How progress reaches the stream: push or poll
| Data | Mechanism | Latency | Coupling | Why |
|---|---|---|---|---|
| Executor steps (tool, command or file, text, todo, results) | **push**: `TurnsState.traces`, an `asyncio.Queue(maxsize=2000)` that `dev/turns/service.py::_on_progress` fills with `put_nowait` (a full queue increments `traces_dropped` and drops the trace; `except asyncio.QueueFull` only) | < 100 ms | one optional queue on `TurnsState`, created only when the panel is configured; no panel code runs inside the turn pump | the detail exists only in memory: `turn_progress` keeps 200-character summaries and no output, so polling it cannot show results |
| Bot messages to the group | **push**: `SenderState.sent`, an `asyncio.Queue(maxsize=500)` that `shared/outbox/service.py::_succeeded` fills after the `sent` commit for message ops | < 100 ms after Telegram accepted it | one optional `sent=` parameter on `run_sender` | exactly the rows that were sent, in send order; an id-cursor poll misses a row sent out of id order (priorities, parking) |
| Card states (status, clock basis, attention) | **poll**, every 2 s while a stream is open | ≤ 2 s | none: existing reads | status changes are written in about 20 places across `turns`, `questions` and `gate`; a hook at each would be invasive |
| New owner deliveries (`prompt` lines) | **poll** by an id cursor, with the card states | ≤ 2 s | none | SQLite's single writer allocates delivery ids in commit order, so a cursor misses nothing |
| Decisions (asked, then approved, declined or expired) | **poll** `gate.list_card_decisions`, with the card states | ≤ 2 s | one read function in `dev/gate` | `dev/gate` posts its questions and replies straight to the Messenger (`gate.service._tell`), not through the outbox, so the sent queue never sees them |
| History when the panel opens | **read** (the snapshot) | — | new read functions | the durable truth: `turn_progress`, `deliveries`, `outbox_messages`, `inbound_updates` |

Messages that bypass the outbox are not mirrored: `dev/gate`'s replies ("tasdiqlandi", `S.WHICH_DECISION`),
`guarded()` error notices, startup notices and the supervisor's alerts. They stay in the group; decisions themselves are
shown from their table.

**`TurnTrace`** (`dev/turns/domain.py`): `project_id, turn_id, session_kind, at, kind (tool_start | tool_end | text |
todo), tool_name, tool_use_id, summary, detail, is_error, in_subagent`. `summary` and `detail` are already
`redact.scrubbed` when queued.

The richer detail needs **`ports/agent.py::Progress`** to gain `tool_use_id: str | None = None`, `detail: str = ""`
and `in_subagent: bool = False` (defaults, so `FakeAgentRuntime` and Codex are untouched). `adapters/claude/stream.py`
fills them:
- `tool_start`: `detail` = the full value of the summary key (`command`, `file_path`, `pattern` and `path` for
  `Grep`, `url`, `query`, `description`), all lines, cut at 2,000 characters; empty for `mcp__*` and other tools;
- `tool_end`: `detail` = the `tool_result` content as text (text blocks joined; an image block → `[image]`); when it is
  longer than 4,000 characters, the first 2,000 and the last 2,000 joined by a line `…`;
- `text`: `detail` = the text (2,000 characters);
- `in_subagent` = the message carried a `parent_tool_use_id`.

### 3.7 Important decisions stay in the group
- **`dev/gate/service.py::answer_decision`** (protected core). After `domain.bind_approval`, if the binding is `Bound`
  or `Ambiguous` and `origin.channel is Channel.PANEL`:
  - log `decision_panel_refused(decision_id)`;
  - return `DecisionAnswer(AnswerKind.PANEL_REFUSED, decision_id, project_id, reply=S.PANEL_DECISION_IN_GROUP(
    decision_id=…))` (for `Ambiguous`, the first of its ids), **without** sending it: the gate's own `_tell` goes
    straight to the Messenger and would never reach the panel.

  No row changes and no grant is made. `dev/questions/service.py::_decided` records the burst as routed (method
  `binding`, reason `panel_refused`) and queues `answer.reply` through the outbox (kind `notice`, about the decision's
  project) in the same unit of work, so the group and the panel both get it.
- **`dev/turns/service.py::answer_revert`** (the wrong-target revert offer, a `decisions` row) does the same for a
  `PANEL` origin: `RevertOutcome.PANEL_REFUSED`, with the reply `S.PANEL_DECISION_IN_GROUP`.
- **The card path** never reaches the gate with an approval: step 3 of §3.4 5b detects it first.
- **Covered** by these two checks: every `decisions` kind (`gate`, `plan_approval`, `selfupdate_core`,
  `settings_weaken`, `wrong_target_revert`, `runaway_continue`, `drain_timeout`), whichever input the text came from.
- **Not decisions**, so allowed from the panel: agent questions (`AskUserQuestion`, including the "architecture" class
  that is asked as a question); `davom ettir` after a runaway pause or a restart (it is `continue_turn`, not a decision);
  non-weakening settings changes; and switching the panel **off**. A weakening change (`panelni yoq`, raising a cap)
  creates a decision, and that decision is answered in the group.
- **Why:** the group is fed by `getUpdates` over TLS from Telegram, idempotent per update id and tied to the owner's
  Telegram account with 2FA. The panel is a bearer session reachable from the internet. Then even a leaked bot token
  (R-53) or a replayed `initData` can never approve a deploy, data loss, spending or a third-party send.

### 3.8 Security controls (the `qa-security` checklist)
| # | Threat | Control | Proved by |
|---|---|---|---|
| S1 | Forged `initData` | HMAC per §3.3 step 2; `compare_digest`; strict parse (no duplicate keys, ≤ 4 KB); the panel holds the derived key, never the token | vectors V1–V4 and the tamper cases (§8.1) |
| S2 | A captured `initData` replayed | ≤ 1 h old (`panel.init_data_max_age_s`), ≤ 60 s in the future; ≤ 3 sessions per launch; never in a URL, a log or browser storage; sessions idle 30 min, absolute 12 h; optional Access (§7.7); a replay still cannot approve a decision (S9) | service tests: expired, from the future, a fourth session refused |
| S3 | Strangers | a user other than `access.owner_id` → 401 `auth_failed`, the same body as a bad hash; the log keeps `user_id` and the reason only; no inbox row | integration: a stranger → 401, 0 rows, the log line has the id and nothing else |
| S4 | A stolen session token | 256 random bits, in memory only (the server keeps its sha256), the `Authorization` header only (never a cookie, URL or log); revoked on `panel.mode` off or a `panel.hostname` or `access.owner_id` change; at most 4 sessions | service tests |
| S5 | CSRF | no cookie authentication at all; every `POST` and the WebSocket upgrade need `Origin = https://<panel.hostname>`; `POST` needs `Content-Type: application/json`; `Sec-Fetch-Site`, when sent, must be `same-origin` | integration: a wrong or missing Origin → 403 |
| S6 | CORS | the page and the API are one origin; no `Access-Control-*` header is ever sent; `OPTIONS` → 403 | integration |
| S7 | XSS | React text nodes only; the Markdown subset (`**bold**`, `` `code` ``, `• `) is rendered by building elements, never `dangerouslySetInnerHTML` (grep test); CSP (S8) | vitest; architecture grep |
| S8 | CSP and framing | every response carries the headers of §4.1; the Telegram script is vendored, so `script-src 'self'` holds; `frame-ancestors https://web.telegram.org` for Telegram Web | integration (headers); e2e: 0 CSP violations |
| S9 | A panel compromise approving a deploy, data loss, spending or a send | §3.7 | regression: a panel "ha" with a pending deploy → still pending; the group's "ha" approves it |
| S10 | DNS rebinding, a spoofed Host | `Host` must equal `panel.hostname` (only `/healthz` also answers to `127.0.0.1:<port>` and `localhost:<port>`) | integration |
| S11 | WebSocket hijacking | Origin checked before the upgrade; the `auth` frame within 5 s; at most 3 streams per session and 8 in all; client frames ≤ 4 KB | integration |
| S12 | Brute force and abuse | 10 sessions/min per IP; 30 messages/min per session; bodies ≤ 16 KB; 20 failed logins (`panel.auth_failures_to_lock`) within 10 min lock new sessions for 10 min (open sessions keep working) and send `S.PANEL_AUTH_LOCKED` once | service tests |
| S13 | Secrets on the wire | every string passes `redact.scrubbed` at the hub (snapshot and frames) after being scrubbed when stored; MCP output hidden by default; `Read` content never shown | leak sweep (§8.2) |
| S14 | Tunnel credentials stolen (a rogue connector would receive panel traffic) | `~/.cloudflared/**` is forbidden to the executor: `<home>/.cloudflared` joins the gate's `forbidden_roots` (protected core) and `forbidden_tool_rules` adds `Read(//<home>/.cloudflared/**)` and `Edit(//<home>/.cloudflared/**)`; the file is 0600; the check script; `cert.pem` removed after setup | gate eval cases (`cat`, `cp`, `Read` of `~/.cloudflared/x.json` → forbidden) |
| S15 | A local process (the executor) calling the loopback port | no loopback trust: the same authentication as from the internet | integration: no token → 401 |
| S16 | Fingerprinting | no `Server` header; the page title is "Panel"; aiohttp's access log is off | integration |
| S17 | The panel hurting the dev bot | bounded queues that drop rather than block (the turn pump and the sender never wait); every loop under `aio.forever`; the kill switch `panelni o'chir` needs no model | fault tests (§8.2) |

### 3.9 Failure behaviour
| Situation | The owner sees | The bot does |
|---|---|---|
| Tunnel down, or cloudflared not connected | when opening: Cloudflare's error page (1033 or 502). Already open: "Aloqa uzildi — qayta ulanmoqda…", the last state greyed with its time, sends failing with a retry | nothing different: the group works; `tekshir` shows the red tunnel line |
| The Mac asleep or offline | the same | — |
| The dev bot restarting | the banner; reconnects; a new session from the same `initData` if it is ≤ 1 h old, else "Panelni yopib, qayta oching" | sessions and rings are memory only; cards are rebuilt from the DB; detailed lines from before the restart are gone, `turn_progress` lines remain |
| A self-update draining or in probation | sends get 409 "Bot yangilanmoqda…"; reading works until the switch | writes refused while `run/flags/drain.json` exists; the panel does not run in probation |
| `panel.mode` off | "Panel o'chirilgan…" | every request 404 except `/healthz`; streams get `bye{disabled}` within one tick (15 s) |
| `PANEL_PORT` in use | the panel does not open (502 at Cloudflare) | `holat?`: "Panel: yoqilgan, lekin server ishlamayapti (port band)"; `forever` retries with backoff |
| A slow phone | a short re-sync, no gap | that subscriber's queue is dropped and it gets `resync` |

### 3.10 Contracts between units (new public types and functions)
Signatures and fields are binding, because units build them in parallel; bodies belong to the owning unit (§9). `…`
means the existing parameters, unchanged. Every new field of an existing dataclass has a default, so existing callers
compile as they are.
```python
# shared/access/domain.py (U1)
class Channel(StrEnum): TELEGRAM = "telegram"; PANEL = "panel"
OwnerOrigin(bot, update_id, user_id, chat_id, channel: Channel = Channel.TELEGRAM)
Arrival(…, channel: Channel = Channel.TELEGRAM)
def channel_of_update_id(update_id: int) -> Channel                      # ≤ -1 → PANEL
def panel_secret_key(bot_token: str) -> bytes
class LaunchProblem(StrEnum): MALFORMED = "malformed"; TOO_LONG = "too_long"; BAD_HASH = "bad_hash"
                              EXPIRED = "expired"; FROM_FUTURE = "from_future"; NO_USER = "no_user"
@dataclass(frozen=True) class VerifiedLaunch: user_id: int; auth_date: datetime; launch_id: str  # repr=False
@dataclass(frozen=True) class LaunchRefused: problem: LaunchProblem
def verify_init_data(raw: str, *, secret_key: bytes, now: datetime, max_age_s: int) -> VerifiedLaunch | LaunchRefused
@dataclass(frozen=True) class PanelPass: bot: BotName; user_id: int; launch_id: str; auth_date: datetime; issued_at: datetime
def require_panel_pass(panel_pass: object) -> PanelPass                 # TypeError unless minted here
def admit_panel(launch: VerifiedLaunch, policy: AccessPolicy, *, now: datetime) -> PanelPass | Drop
def mint_panel_origin(panel_pass: PanelPass, policy: AccessPolicy, *, update_id: int) -> OwnerOrigin | Drop
# shared/access/service.py (U1)
async def admit_panel_launch(deps: SharedDeps, init_data: str, *, secret_key: bytes) -> PanelPass | LaunchRefused | Drop
async def panel_origin(deps: SharedDeps, panel_pass: PanelPass, *, update_id: int) -> OwnerOrigin | None
# shared/audit/domain.py (U1)
AuditKind.PANEL_SESSION = "panel_session"

# shared/inbox (U3)
@dataclass(frozen=True) class PanelMark: client_id: str; card: str | None = None   # card = the project of a card input
IncomingUpdate(…, panel: PanelMark | None = None); Content(…, panel: PanelMark | None = None)
Accepted(…, text: str | None = None)                                    # the stored (redacted) text
async def find_panel_update(deps, client_id: str, *, since_id: int) -> StoredUpdate | None
async def list_panel_updates(deps, *, since_id: int, limit: int) -> tuple[StoredUpdate, ...]   # oldest first
async def last_panel_update_id(deps) -> int | None                     # the largest negative update_id
# shared/burst/domain.py (U3)
CloseReason.PANEL_CARD = "panel_card"; TurnInput.panel_card -> str | None   # property: the card's project, else None
# shared/outbox (U3)
SENT_QUEUE_MAX = 500
@dataclass(frozen=True) class SentNote: outbox_id: int; chat_id: int; op: Op; kind: Kind; dedupe_key: str
    about: About; text: str | None; file_name: str | None; sent_at: datetime; message_id: int | None
    # text: the row's stored text (send_text), or the caption of a file op
async def run_sender(deps, *, config: SenderConfig = DEFAULT_CONFIG, sent: asyncio.Queue[SentNote] | None = None) -> None
async def list_recent_sent(deps, *, chat_id: int, since: datetime, limit: int) -> tuple[SentNote, ...]  # sent, message ops
# shared/health (U3)
@dataclass(frozen=True) class PanelHealth: mode: str; hostname: str; listening: bool; streams: int; problem: str | None
type PanelProbe = Callable[[], Awaitable[PanelHealth]]
async def show_health(deps, state, *, work: WorkProbe | None = None, panel: PanelProbe | None = None) -> BotText

# ports/agent.py (U4)
Progress(kind, tool_name, summary, is_error=False, tool_use_id: str | None = None, detail: str = "", in_subagent: bool = False)
# dev/turns/domain.py (U4); `ProgressKind` below is the port's literal, `turns.domain.ProgressKind` the stored enum
TRACE_QUEUE_MAX = 2000
@dataclass(frozen=True) class TurnTrace: project_id: str; turn_id: int; session_kind: SessionKind; at: datetime
    kind: ports.agent.ProgressKind; tool_name: str | None; tool_use_id: str | None; summary: str; detail: str
    is_error: bool; in_subagent: bool
@dataclass(frozen=True) class CardTurn: turn_id: int; project_id: str; kind: TurnKind; status: TurnStatus; title: str
    queued_at: datetime; started_at: datetime | None; ended_at: datetime | None; waiting_since: datetime | None
    limit_resets_at: datetime | None; queue_reason: str | None; queue_position: int | None; steps: int
    natija: str | None; tests: str | None; error_code: str | None; duration_ms: int | None
@dataclass(frozen=True) class OwnerDelivery: delivery_id: int; turn_id: int; project_id: str; mode: DeliveryMode
    text: str | None; at: datetime
@dataclass(frozen=True) class ProgressRow: progress_id: int; turn_id: int; at: datetime; kind: ProgressKind; summary: str
def forbidden_tool_rules(*, data_dir: str, config_dir: str, home: str) -> tuple[str, ...]   # + ~/.cloudflared
# dev/turns/service.py (U4)
TurnsState.traces: asyncio.Queue[TurnTrace] | None = None; TurnsState.traces_dropped: int = 0
async def list_card_turns(deps, *, since: datetime) -> tuple[CardTurn, ...]
async def list_owner_deliveries(deps, *, turn_ids: Collection[int] = (), after_id: int | None = None,
                                limit: int) -> tuple[OwnerDelivery, ...]   # source=owner; by turn ids, or after an id
async def list_turn_progress(deps, turn_ids: Collection[int], *, per_turn: int) -> tuple[ProgressRow, ...]
async def submit_card_turn(deps, state: TurnsState, item: TurnInput, project_id: str) -> RoutedTurn
RevertOutcome.PANEL_REFUSED = "panel_refused"
# dev/routing/domain.py (U4)
Method.PANEL = "panel"

# dev/gate/service.py (U2)
AnswerKind.PANEL_REFUSED = "panel_refused"                             # reply set, not sent (§3.7)
@dataclass(frozen=True) class DecisionView: decision_id: int; project_id: str | None; turn_id: int | None; kind: str
    state: str; text: BotText; created_at: datetime; decided_at: datetime | None
    # state: decisions.status; text: the question exactly as `_question(row)` posted it (scrubbed)
async def list_card_decisions(deps, *, since: datetime) -> tuple[DecisionView, ...]   # created at or after since, oldest first
# dev/questions/service.py (U2)
async def answer_card(deps, state: TurnsState, origin: OwnerOrigin, item: TurnInput, project_id: str) -> OwnerAnswer | None

# dev/panel/domain.py (U5)
class PanelError(StrEnum): …                                            # §4.5; HTTP_STATUS: Mapping[PanelError, int]
@dataclass(frozen=True) class PanelRefusal: code: PanelError; retry_after_s: int | None = None; limit: int | None = None
@dataclass(frozen=True) class PanelMessage: client_id: str; text: str; project_id: str | None
class CardCommandKind(StrEnum): HOLD = "hold"; STOP = "stop"; REDIRECT = "redirect"
                                CONTINUE = "continue"; RETRY = "retry"; NEW_SESSION = "new_session"
@dataclass(frozen=True) class CardCommand: kind: CardCommandKind; argument: str = ""
def card_command(text: str) -> CardCommand | None
def next_update_id(now: datetime, last: int | None) -> int;  def id_floor(at: datetime) -> int
# dev/panel/service.py (U5)
@dataclass(frozen=True) class SessionOpened: token: str; expires_at: datetime; idle_timeout_s: int
    server_time: datetime; epoch: str; version: str                    # token: repr=False
@dataclass(frozen=True) class MessageAccepted: update_id: int; duplicate: bool; text: str; at: datetime
    origin: OwnerOrigin | None; accepted: Accepted | None              # None for a duplicate: skip intake.accepted
class PanelState:  # slots dataclass; built by new_state
    bot_username: str | None                                           # set by serve_panel after getMe
    def subscribe(self, panel_pass: PanelPass) -> Subscription | PanelRefusal
    def unsubscribe(self, subscription: Subscription) -> None
    def status(self) -> dict[str, JsonValue]                           # cached by run_poll; for the control socket
def new_state(deps: DevDeps, *, secret_key: bytes, port: int, dist: Path, version: str) -> PanelState
async def open_session(deps, state, init_data: str, *, client_ip: str | None) -> SessionOpened | PanelRefusal
async def resolve_session(deps, state, token: str) -> PanelPass | PanelRefusal
async def read_state(deps, state, panel_pass: PanelPass) -> JsonObject
async def submit_message(deps, state, panel_pass: PanelPass, message: PanelMessage) -> MessageAccepted | PanelRefusal
async def run_traces(deps, state, traces: asyncio.Queue[TurnTrace]) -> None
async def run_sent(deps, state, sent: asyncio.Queue[SentNote]) -> None
async def run_poll(deps, state) -> None
async def panel_health(deps, state) -> PanelHealth
async def panel_link(deps, state) -> BotText                           # S.PANEL_LINK / _PROFILE / _OFF / _NO_HOST

# bots/dev/panel/server.py (U6)
def build_web_app(deps: DevDeps, state: PanelState, intake: Intake, *, scheme: Literal["https", "http"] = "https") -> web.Application
async def serve_panel(deps: DevDeps, state: PanelState, intake: Intake, *, bot: Bot | None,
                      scheme: Literal["https", "http"] = "https") -> None   # the panel_http loop body
# bots/dev (U7)
@dataclass(frozen=True) class PanelConfig: port: int; secret_key: bytes; dist: Path   # runtime.py; secret_key repr=False
runtime.build_app(…, panel: PanelConfig | None = None)
Intent.SHOW_PANEL = "show_panel"   # "panel", "/panel", "panel havolasi"; EARLY
Intent.PANEL_ON = "panel_on"       # "panelni yoq", "/panel on"; EARLY, owner-only; hostname empty → S.PANEL_NO_HOST
Intent.PANEL_OFF = "panel_off"     # "panelni o'chir", "/panel off"; EARLY, owner-only; applies at once
# control socket `status`: + "panel": PanelState.status() or null
```

## 4. API contract

### 4.1 Conventions
- **Base.** `https://<panel.hostname>` in production; the same app on `http://127.0.0.1:<PANEL_PORT>` locally. JSON is
  UTF-8 with `snake_case` keys. Every instant is UTC, RFC 3339 with a `Z` and milliseconds.
- **Authentication.** `POST /api/session` takes the `initData`; everything else under `/api/` takes
  `Authorization: Bearer <token>`; the stream takes `{"type": "auth", "token": …}` as its first frame.
- **Headers on every response** (`security.py`):
  - `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self' wss://<hostname>; manifest-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors https://web.telegram.org`;
  - `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
    `Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=(), usb=()`,
    `Cross-Origin-Opener-Policy: same-origin`, `Cross-Origin-Resource-Policy: same-origin`,
    `Strict-Transport-Security: max-age=31536000`;
  - no `Server` header;
  - `Cache-Control`: `/api/*` `no-store`; `/` `no-cache`; `/assets/*` `public, max-age=31536000, immutable`;
    `/vendor/*` `no-cache`.

  The CSP's scheme follows the server's `scheme` (`wss` in production; `ws` only in the e2e seam, §8.4).
- **Limits.** Request body ≤ 16,384 bytes (aiohttp `client_max_size`); `init_data` ≤ 4,096 bytes; `text` ≤
  `panel.max_input_chars`; client frames ≤ 4,096 bytes.
- **Errors.** `{"error": {"code": <PanelError>, "retry_after_s"?: int, "max"?: int}}`, with a `Retry-After` header on 429.

### 4.2 Endpoints
| Method and path | Auth | Request | Success | Errors |
|---|---|---|---|---|
| `GET /` | none | — | 200 `index.html` (no data in it) | 404 `panel_off` (also for a Host mismatch) |
| `GET /assets/<file>`, `GET /vendor/<file>` | none | — | 200, files from `panel/dist` only (`follow_symlinks=False`) | 404 |
| `GET /healthz` | none | — | 200 `{"ok": true}` (whatever the mode) | — |
| `POST /api/session` | the `initData` in the body | `SessionRequest` | 201 `SessionResponse` | 400 `bad_request` · 401 `auth_failed`, `init_data_expired` · 403 `origin_rejected` · 404 `panel_off` · 429 `rate_limited`, `auth_locked` |
| `GET /api/state` | Bearer | — | 200 `Snapshot` | 401 `session_invalid`, `session_expired`, `session_revoked` · 404 `panel_off` · 429 |
| `POST /api/messages` | Bearer | `MessageRequest` | 202 `MessageResponse` | 400 · 401 · 403 · 404 `panel_off`, `project_unknown` · 409 `updating` · 413 `too_long` · 429 |
| `GET /api/stream` (WebSocket) | the first frame | client frames (§4.4) | 101, then server frames | 403 before the upgrade (Origin, mode, Host); close codes (§4.4) |
| `OPTIONS *` | — | — | — | 403 |

### 4.3 JSON schemas
One document; every endpoint and frame refers to its `$defs`.
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "yordamchi/panel/v1",
  "$defs": {
    "Instant": {"type": "string", "format": "date-time"},
    "ProjectId": {"type": "string", "minLength": 1, "maxLength": 64},
    "PanelError": {"enum": ["bad_request", "auth_failed", "init_data_expired", "session_invalid", "session_expired",
      "session_revoked", "origin_rejected", "panel_off", "not_found", "project_unknown", "updating", "too_long",
      "rate_limited", "auth_locked"]},
    "Error": {"type": "object", "additionalProperties": false, "required": ["error"], "properties": {
      "error": {"type": "object", "additionalProperties": false, "required": ["code"], "properties": {
        "code": {"$ref": "#/$defs/PanelError"},
        "retry_after_s": {"type": "integer", "minimum": 1},
        "max": {"type": "integer", "minimum": 1}}}}},
    "SessionRequest": {"type": "object", "additionalProperties": false, "required": ["init_data"], "properties": {
      "init_data": {"type": "string", "minLength": 1, "maxLength": 4096}}},
    "SessionResponse": {"type": "object", "additionalProperties": false,
      "required": ["token", "expires_at", "idle_timeout_s", "server_time", "epoch", "version"], "properties": {
        "token": {"type": "string", "pattern": "^[A-Za-z0-9_-]{43}$"},
        "expires_at": {"$ref": "#/$defs/Instant"}, "idle_timeout_s": {"type": "integer", "minimum": 60},
        "server_time": {"$ref": "#/$defs/Instant"}, "epoch": {"type": "string", "pattern": "^[0-9a-f]{8}$"},
        "version": {"type": "string"}}},
    "MessageRequest": {"type": "object", "additionalProperties": false, "required": ["client_id", "text", "project_id"],
      "properties": {
        "client_id": {"type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"},
        "text": {"type": "string", "minLength": 1, "maxLength": 4096},
        "project_id": {"oneOf": [{"$ref": "#/$defs/ProjectId"}, {"type": "null"}]}}},
    "MessageResponse": {"type": "object", "additionalProperties": false,
      "required": ["update_id", "duplicate", "text", "accepted_at"], "properties": {
        "update_id": {"type": "integer", "maximum": -1}, "duplicate": {"type": "boolean"},
        "text": {"type": "string"}, "accepted_at": {"$ref": "#/$defs/Instant"}}},
    "CardState": {"enum": ["running", "waiting_you", "paused", "queued", "done", "failed"]},
    "CardSub": {"enum": ["stopping", "limit", "runaway", "interrupted", "cancelled", "queue_cap", "queue_pressure",
      "queue_disk", "queue_mac_attached", "queue_update_drain", "queue_busy", null]},
    "TurnStatus": {"enum": ["queued", "starting", "running", "waiting_question", "waiting_decision", "waiting_limit",
      "paused_runaway", "stopping", "done", "failed", "interrupted", "cancelled"]},
    "TurnKind": {"enum": ["instruct", "investigate", "plan", "build", "selfupdate"]},
    "Clock": {"type": "object", "additionalProperties": false, "required": ["mode", "from", "seconds"], "properties": {
      "mode": {"enum": ["live", "fixed", "none"]},
      "from": {"oneOf": [{"$ref": "#/$defs/Instant"}, {"type": "null"}]},
      "seconds": {"oneOf": [{"type": "integer", "minimum": 0}, {"type": "null"}]}}},
    "LineKind": {"enum": ["prompt", "tool", "output", "text", "todo", "result", "question", "decision", "notice", "system"]},
    "DecisionState": {"enum": ["pending", "approved", "denied", "expired", "consumed", "superseded", "cancelled", null]},
    "Line": {"type": "object", "additionalProperties": false,
      "required": ["id", "at", "turn_id", "kind", "tool", "text", "error", "sub", "state"], "properties": {
        "id": {"type": "string", "pattern": "^(t\\d+|p\\d+|d\\d+|o\\d+|g\\d+|s\\d+)$"},
        "at": {"$ref": "#/$defs/Instant"},
        "turn_id": {"oneOf": [{"type": "integer"}, {"type": "null"}]},
        "kind": {"$ref": "#/$defs/LineKind"},
        "tool": {"oneOf": [{"type": "string", "maxLength": 120}, {"type": "null"}]},
        "text": {"type": "string", "maxLength": 8000},
        "error": {"type": "boolean"}, "sub": {"type": "boolean"},
        "state": {"$ref": "#/$defs/DecisionState"}}},
    "Attention": {"type": "object", "additionalProperties": false, "required": ["questions", "decisions"], "properties": {
      "questions": {"type": "integer", "minimum": 0},
      "decisions": {"type": "array", "items": {"type": "integer"}}}},
    "Card": {"type": "object", "additionalProperties": false,
      "required": ["project_id", "state", "sub", "title", "turn_id", "turn_kind", "turn_status", "steps", "clock",
        "natija", "tests", "queue_position", "attention", "since"], "properties": {
        "project_id": {"$ref": "#/$defs/ProjectId"}, "state": {"$ref": "#/$defs/CardState"},
        "sub": {"$ref": "#/$defs/CardSub"}, "title": {"type": "string", "maxLength": 120},
        "turn_id": {"type": "integer"}, "turn_kind": {"$ref": "#/$defs/TurnKind"},
        "turn_status": {"$ref": "#/$defs/TurnStatus"}, "steps": {"type": "integer", "minimum": 0},
        "clock": {"$ref": "#/$defs/Clock"},
        "natija": {"oneOf": [{"type": "string"}, {"type": "null"}]},
        "tests": {"enum": ["passed", "failed", "not_run", "unknown", null]},
        "queue_position": {"oneOf": [{"type": "integer", "minimum": 1}, {"type": "null"}]},
        "attention": {"$ref": "#/$defs/Attention"}, "since": {"$ref": "#/$defs/Instant"},
        "lines": {"type": "array", "items": {"$ref": "#/$defs/Line"}}}},
    "FeedItem": {"type": "object", "additionalProperties": false,
      "required": ["id", "at", "from", "kind", "project_id", "text", "state"], "properties": {
        "id": {"type": "string", "pattern": "^(u\\d+|o\\d+|g\\d+)$"}, "at": {"$ref": "#/$defs/Instant"},
        "from": {"enum": ["owner", "bot"]},
        "kind": {"enum": ["input", "ack", "echo", "report", "notice", "question", "decision", "result", "system", "file"]},
        "project_id": {"oneOf": [{"$ref": "#/$defs/ProjectId"}, {"type": "null"}]},
        "text": {"type": "string", "maxLength": 8000},
        "state": {"$ref": "#/$defs/DecisionState"}}},
    "Snapshot": {"type": "object", "additionalProperties": false,
      "required": ["epoch", "seq", "server_time", "version", "panel", "limits", "attention", "cards", "feed"],
      "properties": {
        "epoch": {"type": "string", "pattern": "^[0-9a-f]{8}$"}, "seq": {"type": "integer", "minimum": 0},
        "server_time": {"$ref": "#/$defs/Instant"}, "version": {"type": "string"},
        "panel": {"type": "object", "additionalProperties": false, "required": ["hostname", "link"], "properties": {
          "hostname": {"type": "string"}, "link": {"oneOf": [{"type": "string", "format": "uri"}, {"type": "null"}]}}},
        "limits": {"type": "object", "additionalProperties": false, "required": ["max_input_chars"], "properties": {
          "max_input_chars": {"type": "integer"}}},
        "attention": {"type": "object", "additionalProperties": false, "required": ["waiting_you", "decisions_pending"],
          "properties": {"waiting_you": {"type": "array", "items": {"$ref": "#/$defs/ProjectId"}},
            "decisions_pending": {"type": "integer", "minimum": 0}}},
        "cards": {"type": "array", "items": {"$ref": "#/$defs/Card"}},
        "feed": {"type": "array", "items": {"$ref": "#/$defs/FeedItem"}}}}
  }
}
```
- In the snapshot every `Card` carries `lines`; in a `card` frame it never does.
- **Line ids:** `t<seq>` an executor line (unique per process `epoch`); `p<turn_progress.id>`; `d<deliveries.id>`;
  `o<outbox_messages.id>`; `g<decisions.id>`; `s<turn_id>` a separator. **Feed ids:** `u<|update_id|>` a panel input;
  `o<id>` a bot message; `g<id>` a decision.
- **`state`** is set only on `decision` lines and items (the `decisions.status`); it is `null` everywhere else. A
  decision's line is the only one whose content changes: a later frame with the same id replaces it.
- **`Clock`:** `running` → `live` from `started_at`; `waiting_you` → `live` from `waiting_since`; `queued` → `live` from
  `queued_at`; `paused` → `fixed`, the seconds it ran; `done`, `failed` → `fixed`, `duration_ms / 1000` (else
  `ended_at − started_at`); no start yet → `none`.
- **`Card.sub`** is set from the status and `queue_reason` as §2.2 says; the client turns it into text through `uz.json`.

### 4.4 Stream protocol
Client → server (JSON text frames):

| Frame | Fields | Rule |
|---|---|---|
| `auth` | `{"type": "auth", "token": str}` | the first frame, within 5 s, else close 4408 |
| `ping` | `{"type": "ping"}` | every 20 s while the page is visible; refreshes the session's idle timer |

Server → client. Every frame has `"type"`; every frame but `bye` has `"seq"` (an integer increasing per process):

| Frame | Fields | When |
|---|---|---|
| `ready` | `epoch`, `seq`, `server_time` | right after a successful `auth` |
| `line` | `seq`, `project_id`, `line: Line` | an executor step, a bot message about a carded project, a `prompt`, a decision (new or changed state), a `system` separator. A `line` whose id the card already has replaces it |
| `card` | `seq`, `card: Card` (no `lines`) | a card appeared or changed |
| `card_removed` | `seq`, `project_id` | a card left (§2.2) |
| `feed` | `seq`, `item: FeedItem` | a panel general input, a general bot message, or a decision with no carded project (same replace rule) |
| `tick` | `seq`, `server_time` | every 15 s |
| `resync` | `seq`, `reason: "overflow" \| "restart"` | the subscriber fell behind; the client re-snapshots |
| `bye` | `reason: "disabled" \| "revoked" \| "expired" \| "shutdown"` | just before the server closes the stream |

Close codes:
- `1001` shutdown (reconnect);
- `4400` a malformed or oversized client frame;
- `4401` the token is invalid, expired or revoked (open a new session);
- `4403` Origin or Host rejected, or the panel is off;
- `4408` no `auth` frame within 5 s;
- `4429` too many streams.

### 4.5 Error codes and statuses
`PanelError` lives in `dev/panel/domain.py`. A refusal is a return value (`PanelRefusal(code, retry_after_s, max)`), not a
`YordamchiError`: it never reaches the owner as Telegram text, so it has no `S.ERRORS` entry. Its Uzbek text is
`uz.json` `errors.<code>`.

| Code | HTTP | Meaning |
|---|---|---|
| `bad_request` | 400 | malformed JSON, a wrong content type, a bad `client_id`, empty text |
| `auth_failed` | 401 | a bad hash, malformed `initData`, a stranger, a launch reused more than 3 times (one code for all, so nothing is revealed) |
| `init_data_expired` | 401 | the `initData` is older than `panel.init_data_max_age_s`: close and reopen the panel |
| `session_invalid` / `session_expired` / `session_revoked` | 401 | no such token / past its idle or absolute limit / revoked (§3.3) |
| `origin_rejected` | 403 | a wrong or missing `Origin`, or `Sec-Fetch-Site` |
| `panel_off` | 404 | `panel.mode = off`, `panel.hostname` empty, or a Host mismatch |
| `not_found` | 404 | an unknown path |
| `project_unknown` | 404 | the card's project is not registered, or is hidden |
| `updating` | 409 | a self-update is draining |
| `too_long` | 413 | `text` over `panel.max_input_chars` (`max` is set) |
| `rate_limited` / `auth_locked` | 429 | a bucket is empty / failed logins locked new sessions (`retry_after_s` is set) |

## 5. Data model

### 5.1 No new table, no new column, no migration
`0001` is frozen since T43, and nothing here needs a revision: `alembic check` stays clean. These are new **values** in
existing columns and keys:

| Where | New value | Fits |
|---|---|---|
| `inbound_updates.update_id` (`BigInteger`) | panel updates are negative (§5.2) | signed 64-bit; UNIQUE(bot, update_id) unchanged |
| `inbound_updates.payload_json` | `"panel": {"client_id": "<uuid>", "card": "<project>" \| null}`, only on panel rows (written by `inbox.domain.payload_json`, read by `content_from_json`, as `forward` is) | JSON text |
| `bursts.input_json` | each part's `content` carries the same `panel` key (`burst.domain._part_json` stores `inbox.payload_json(content)`), so a card burst survives a restart | JSON text |
| `bursts.close_reason` (`String(16)`) | `panel_card` | 10 characters |
| `route_decisions.method` (`String(16)`) | `panel` | 5 characters |
| `audit_events.kind` (`String(32)`) | `panel_session` | 13 characters |
| `outbox_messages.dedupe_key` | refs `panel-echo-<n>` (kind `echo`) | as today |
| `setting_versions` | rows for the `panel.*` keys of §6.1 | an older release ignores unknown keys (`settings.domain.resolve` iterates the registry), so a rollback is safe |

Retention: panel rows follow the existing rules (`inbound_updates` payload 7 d, rows 30 d; `audit_events` 90 d). The
panel keeps nothing on disk of its own: sessions, rings and subscribers are memory only.

### 5.2 Panel update ids
`dev/panel/domain.py`:
- `next_update_id(now, last) = max(encode(now), last + 1)` when `last` is set, else `encode(now)`;
  `encode(t) = −(2**62 − unix_ms(t) × 1024)`.
- `id_floor(t) = encode(t)`, the lower bound of a time window.
- `last` starts from `inbox.last_panel_update_id(deps)` (the largest negative `update_id`) on the first write after a
  start.

Properties (tested):
- always ≤ −1, so `channel_of_update_id` says `PANEL` until the year 140,000;
- strictly increasing within a process and across restarts, even after the clock steps back;
- newer ids sort after older ones, so two quick panel messages attach to a burst in the order they were sent;
- `find_panel_update` is a range query on the UNIQUE(bot, update_id) index.

Accepted quirk: all panel ids sort before all Telegram ids. In the rare pass where a panel part and a Telegram part are
both `received` at once, the panel part attaches first.

**Idempotency.** A retried `POST` carries the same `client_id`. Within 10 minutes `submit_message` finds the stored row
by its `panel.client_id` and returns `duplicate: true`, so a message is processed exactly once even across a bot
restart. A card burst is routed once (`record_route`, `submit` keyed by `burst_id`), as every burst is.

## 6. Settings, bootstrap values and constants

### 6.1 Settings (`shared/settings/defaults.py`, block `# --- panel ---`; `bots=DEV` for all)
| Key (constant) | Type | Default | Check | Group | `weakens` | Doc |
|---|---|---|---|---|---|---|
| `panel.mode` (`PANEL_MODE`) | str | `"off"` | `OneOf("off", "on")` | ACCESS | `to_values({"on"})` | "on" serves the Telegram Mini App panel on `panel.hostname`; "off" refuses every panel request (the kill switch) |
| `panel.hostname` (`PANEL_HOSTNAME`) | str | `""` | `Regex(r"(?:(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63})?", description="hostname, masalan panel.example.uz")` (a full match, so `""` passes) | ACCESS | `always` | the public hostname the panel is served on; every request must carry it |
| `panel.app_name` (`PANEL_APP_NAME`) | str | `""` | `Regex(r"(?:[A-Za-z0-9_]{3,30})?", description="BotFather qisqa nomi, masalan panel")` | LIMITS | — | the BotFather direct-link short name; empty: the Main Mini App link `t.me/<bot>?startapp` |
| `panel.init_data_max_age_s` | int | 3600 | `Range(60, 86400)` | ACCESS | `increases` | the oldest Telegram launch data that may open a session |
| `panel.session_idle_min` | int | 30 | `Range(5, 240)` | ACCESS | `increases` | a panel session ends after this long without a request or a ping |
| `panel.session_max_h` | int | 12 | `Range(1, 24)` | ACCESS | `increases` | a panel session ends this long after it opened |
| `panel.max_sessions` | int | 4 | `Range(1, 10)` | ACCESS | `increases` | open panel sessions at once; the least recently used is closed |
| `panel.sessions_per_minute` | int | 10 | `Range(1, 60)` | ACCESS | `increases` | new sessions per minute from one IP |
| `panel.messages_per_minute` | int | 30 | `Range(1, 120)` | ACCESS | `increases` | messages per minute from one session |
| `panel.auth_failures_to_lock` | int | 20 | `Range(5, 200)` | ACCESS | `increases` | failed logins within 10 minutes that close new logins for 10 minutes and tell the owner |
| `panel.mcp_output` | str | `"hidden"` | `OneOf("hidden", "shown")` | ACCESS | `to_values({"shown"})` | whether integration (MCP) tool results appear in the panel; they pass through Cloudflare |
| `panel.echo_to_group` | bool | false | — | LIMITS | — | also post the owner's panel messages into the group as "Paneldan: «…»" |
| `panel.tool_output_lines` | int | 12 | `Range(0, 50)` | LIMITS | — | result lines under a tool step; 0: none |
| `panel.line_max_chars` | int | 400 | `Range(80, 2000)` | LIMITS | — | the longest one-line terminal entry |
| `panel.text_max_chars` | int | 2000 | `Range(200, 8000)` | LIMITS | — | the longest text block (agent text, a bot message) |
| `panel.max_input_chars` | int | 4000 | `Range(100, 4096)` | LIMITS | — | the longest message the panel accepts |
| `panel.history_lines` | int | 60 | `Range(10, 300)` | LIMITS | — | terminal lines a card shows when the panel opens |
| `panel.ring_lines` | int | 300 | `Range(50, 2000)` | LIMITS | — | detailed lines kept in memory per project |
| `panel.recent_h` | int | 12 | `Range(1, 72)` | LIMITS | — | a project keeps its card this long after its last turn ended |
| `panel.max_cards` | int | 12 | `Range(1, 30)` | LIMITS | — | the most cards at once (working or waiting cards never leave) |
| `panel.feed_items` | int | 30 | `Range(5, 100)` | LIMITS | — | general feed items when the panel opens |
| `panel.state_poll_s` | float | 2.0 | `Range(0.5, 10)` | SCHEDULES | — | how often card states are re-read while a panel is connected |

`test_registry`'s list of safeguards that must declare `weakens` gains `panel.mode`, `panel.hostname`,
`panel.init_data_max_age_s`, `panel.session_*`, `panel.max_sessions`, `panel.sessions_per_minute`,
`panel.messages_per_minute`, `panel.auth_failures_to_lock` and `panel.mcp_output`. Switching the panel on by chat
(`panelni yoq` → `panel.mode = on`, or "sozlama: panel manzili X" → `panel.hostname`) therefore asks once in the group;
one request may set both, and one `settings_weaken` decision then carries both items. `panelni o'chir` applies at once.

### 6.2 Bootstrap values (`dev.env`; `core/config.py`)
| Key | Value | Read by |
|---|---|---|
| `PANEL_PORT` | an integer 1024–65535; written as `47831` by `--enable-panel` if absent | the dev bot (`DevConfig.panel_port: int \| None`: no key → no panel loops); the tunnel config; the check |
| `PANEL_METRICS_PORT` | an integer 1024–65535, ≠ `PANEL_PORT`; written as `47832` if absent | the tunnel plist (`--metrics`); the check |

Both join `DEV_KEYS`, so they are known, not "ignored". A malformed value → `ConfigInvalid` (the bot refuses to start,
SPEC §2.5 step 3). An older release that does not know them reports them as ignored keys, never fatal.

### 6.3 Mechanism constants (`dev/panel/domain.py` unless named; not owner behaviour, like `bots/common.PollConfig`)
| Constant | Value |
|---|---|
| `WS_AUTH_TIMEOUT_S` · `WS_HEARTBEAT_S` · `WS_MAX_FRAME_BYTES` · `TICK_S` | 5 · 20 · 4,096 · 15 |
| `MAX_BODY_BYTES` · `SUBSCRIBER_QUEUE` · `MAX_STREAMS_PER_SESSION` · `MAX_STREAMS` · `SESSIONS_PER_LAUNCH` | 16,384 · 256 · 3 · 8 · 3 |
| `DEDUPE_WINDOW` · `AUTH_WINDOW` · `AUTH_LOCK_FOR` · `FEED_WINDOW` · `ECHO_MAX_CHARS` | 10 min · 10 min · 10 min · 24 h · 300 |
| `ID_BASE` · `ID_SEQ` | 2**62 · 1,024 |
| `access.domain`: `INIT_DATA_MAX_BYTES` · `INIT_DATA_FUTURE_SKEW_S` · `WEBAPP_KEY_LABEL` | 4,096 · 60 · `b"WebAppData"` |
| `turns.domain.TRACE_QUEUE_MAX` · `outbox.domain.SENT_QUEUE_MAX` | 2,000 · 500 |
| `adapters/claude/stream.py`: `DETAIL_MAX_CHARS` · `RESULT_MAX_CHARS` | 2,000 · 4,000 (2,000 head + 2,000 tail) |
| client (`panel/src`): reconnect backoff · stream silence · ping | 1→30 s ±20 % · 40 s · 20 s |

## 7. Install and ops

### 7.1 What the owner does once
1. **Cloudflare account.** Sign up at `dash.cloudflare.com` (free plan) and turn on two-factor login.
2. **Domain.** Websites → Add a site → the free plan → change the domain's nameservers at its registrar to the two
   Cloudflare gives, and wait for "Active". (No domain yet: owner question Q1.)
3. **Install with the panel** (§7.2), which puts `cloudflared` in `DATA/bin`:
   `~/.local/bin/python3.12 -I scripts/install.py --release <commit> --enable-panel`.
   This first run stops at the tunnel step, BLOCKED, and prints steps 4–7.
4. **Log in, once:** `"$DATA/bin/cloudflared" tunnel login`. A browser opens; pick the domain. It writes
   `~/.cloudflared/cert.pem`.
5. **Create the tunnel:** `"$DATA/bin/cloudflared" tunnel create yordamchi`. It prints the tunnel UUID and writes
   `~/.cloudflared/<UUID>.json`. Then `chmod 600 ~/.cloudflared/<UUID>.json`.
6. **Route DNS:** `"$DATA/bin/cloudflared" tunnel route dns yordamchi panel.<domain>`. It creates the proxied CNAME
   `panel.<domain>` → `<UUID>.cfargotunnel.com`. Use a first-level name: Universal SSL covers `*.<domain>` only.
7. **Put `cert.pem` away:** move `~/.cloudflared/cert.pem` out (a password manager, or delete it; `tunnel login` makes a
   new one). It can create tunnels and DNS records for the whole zone, and running the tunnel does not need it.
8. **Install again:** `scripts/install.py --release <commit> --enable-panel` (add `--panel-tunnel <UUID>` if
   `~/.cloudflared` holds more than one tunnel). It renders the config and loads `uz.yordamchi.tunnel`.
9. **BotFather:** `/mybots` → the dev bot → Bot Settings → Configure Mini App → Enable Mini App → send
   `https://panel.<domain>/`. Labels may differ slightly; the result to check is the "Open App" button on the bot's
   profile. Never use "Configure Menu Button" (D8). Optional: `/newapp` with the short name `panel`, then
   `sozlama: panel.app_name panel`.
10. **In the dev group:** `sozlama: panelni yoq, manzili panel.<domain>` → one question → `ha`.
11. **Check:** `"$DATA/releases/current/.venv/bin/python" -I -m yordamchi.check` → the Panel section is green; then
    `panel` in the group, open the link, and type `holat?` in the general input.
12. **Optional, recommended:** Cloudflare Access (§7.7).

### 7.2 Installer: `--enable-panel`, `--disable-panel`, `--panel-tunnel <UUID>`
New options on `scripts/install.py` (stdlib, as today). With `--enable-panel`, after the ffmpeg step:
1. **Preflight.** The `cloudflared` pin must be set (`CLOUDFLARED_PIN`; `None` → refuse, as for ffmpeg).
2. **cloudflared.**
   - Download `CLOUDFLARED_PIN.url` and check the tgz's SHA-256.
   - Extract exactly one member, `cloudflared`, with `tarfile` `filter="data"`, and check the binary's SHA-256.
   - `codesign --verify --strict` must pass, and `codesign -dv` must show `TeamIdentifier=68WVV388M8`.
   - Write `DATA/bin/cloudflared` (0700) and the marker `DATA/bin/.cloudflared-2026.10.3.json`.
   - A matching marker → `unchanged`. A dry run → a `dry-run` line, no download.
3. **Ports.** Add `PANEL_PORT=47831` and `PANEL_METRICS_PORT=47832` to `dev.env` if absent (an existing value is never
   overwritten). Each must be a valid integer, the two must differ, and each must be free (a test bind on `127.0.0.1`)
   or held by our own processes.
4. **Tunnel credentials.**
   - `--panel-tunnel <UUID>`, else the single `~/.cloudflared/<uuid>.json` there.
   - None, or more than one → `BLOCKED`, with the commands of §7.1 steps 4–7, and the plist is not installed.
   - The file must parse as JSON with `TunnelID == <UUID>`. Only `TunnelID` is read: `TunnelSecret` is never read into
     a variable that is printed, logged or copied.
   - A mode wider than 0600 is fixed to 0600 and reported `warn`.
5. **Config.** Render `DATA/tunnel/config.yml` (0600, dir 0700) from `ops/cloudflared/config.yml.tmpl`.
6. **Plist.** Render `uz.yordamchi.tunnel.plist` into `DATA/launchd/` always, and into `~/Library/LaunchAgents` only
   with `--enable-panel` and a rendered config. Then `bootstrap`, or `kickstart -k` when unchanged and loaded (the
   existing launchd step, with `bootout` waited for).
7. **The check** runs as today and includes the Panel section.

Other options:
- `--disable-panel`: `bootout` the tunnel job and remove its plist from `~/Library/LaunchAgents`. The binary, the config,
  `dev.env` and the DNS record stay.
- Without either flag: an installed tunnel plist is re-rendered and kept (never silently disabled); none is installed.

**Frontend build.** The release is `git archive <commit>`, so it holds the committed `panel/dist/`. The bot serves
`<release>/panel/dist` (read-only after `chmod -R a-w`). Node never runs in the installer or the supervisor:
- the developer, or the agent of a self-update, runs `make panel` in the working copy: `~/.local/bin/node` 24.21.0
  (STACK §5), `npm ci --ignore-scripts`, typecheck, vitest, `vite build`, then `scripts/panel_dist.py write`;
- `make check` verifies `panel/dist/build.json` against the sources with stdlib Python (§8.5). A self-update that
  changes `panel/` without rebuilding fails `make check`, and the supervisor refuses it;
- `prompts/dev/selfupdate.md` gains one sentence: "If you changed anything under `panel/`, run `make panel` and commit
  `panel/dist`."

### 7.3 cloudflared: pin, config and plist
**Pin** (recorded in STACK §5 and `scripts/install.py`; verified 2026-10-25 by downloading the asset, checking GitHub's
asset digest and `codesign`):

| Item | Value |
|---|---|
| Version | `2026.10.3` (released 2026-10-21) |
| URL | `https://github.com/cloudflare/cloudflared/releases/download/2026.10.3/cloudflared-darwin-arm64.tgz` |
| tgz SHA-256 | `1165d14b6f6360a3b8c283d7d1100024d6ec7cd9bc9e32519e96150645f3ec55` (19,841,881 bytes; equals GitHub's asset digest) |
| binary SHA-256 | `f249139e0541fa589c987397d867cc16c83ab1236d31eb6782f2e4552ee9cf80` (Mach-O arm64) |
| Signature | `Developer ID Application: Cloudflare Inc. (68WVV388M8)` |

Caution: the release notes list `f249139e…` against the **tgz** name, but it is the hash of the binary inside it. The
installer checks both values in their right places.

**`ops/cloudflared/config.yml.tmpl`** (protected core), rendered to `DATA/tunnel/config.yml`:
```yaml
# Rendered by scripts/install.py from ops/cloudflared/config.yml.tmpl (docs/MINIAPP_SPEC.md §7.3). Do not edit.
tunnel: {{TUNNEL_ID}}
credentials-file: {{HOME}}/.cloudflared/{{TUNNEL_ID}}.json
no-autoupdate: true
ingress:
  - service: http://127.0.0.1:{{PANEL_PORT}}
    originRequest:
      connectTimeout: 5s
      noHappyEyeballs: true
```
A single catch-all rule. Only hostnames the owner routes to this tunnel reach it, and the app rejects any `Host` but
`panel.hostname` (S10). So the hostname has one source of truth, the setting, and the installer never needs it. The
original `Host` header is forwarded (no `httpHostHeader`).

**`ops/launchd/uz.yordamchi.tunnel.plist.tmpl`** (protected core), in the shape of the supervisor's plist:

| Key | Value |
|---|---|
| `Label` | `uz.yordamchi.tunnel` |
| `ProgramArguments` | `{{DATA}}/bin/cloudflared`, `tunnel`, `--config`, `{{DATA}}/tunnel/config.yml`, `--metrics`, `127.0.0.1:{{PANEL_METRICS_PORT}}`, `--loglevel`, `warn`, `--transport-loglevel`, `warn`, `--no-autoupdate`, `--grace-period`, `5s`, `run` |
| `EnvironmentVariables` | `HOME={{HOME}}`, `PATH=/usr/bin:/bin:/usr/sbin:/sbin` |
| `WorkingDirectory` | `{{DATA}}/run` |
| `ProcessType` | `Standard` |
| `RunAtLoad` / `KeepAlive` | `true` / `true` |
| `ThrottleInterval` / `ExitTimeOut` | `10` / `10` |
| `StandardOutPath` = `StandardErrorPath` | `{{LOGS}}/uz.yordamchi.tunnel.launchd.log` |
| `AbandonProcessGroup` | absent (false) |

The render values gain `PANEL_METRICS_PORT` (read from `dev.env`); `TUNNEL_ID` and `PANEL_PORT` are for the config
only.

**Log size (NFR-CAP-05).** `warn` levels keep the log small, and the supervisor's `logtrim.py` copy-truncates
`uz.yordamchi.tunnel.launchd.log` over 5 MB, like the bots' launchd logs. That is unit O3: a supervisor-only change
(R-32), protected core, and it does not touch the watchdog's process list.

`core/paths.py` gains `TUNNEL_LABEL`, `tunnel_dir` (`DATA/tunnel`), `tunnel_config`, `cloudflared` (`DATA/bin/cloudflared`),
`cloudflared_home` (`~/.cloudflared`) and `tunnel_launchd_log`; `data_dirs` gains `tunnel/`.

### 7.4 `python -m yordamchi.check`: the Panel section
It is checked when `~/Library/LaunchAgents/uz.yordamchi.tunnel.plist` exists (`panel_enabled(paths)`, like
`personal_enabled`); otherwise one neutral line, "Panel o'rnatilmagan". It is stdlib-only (A9). It reads `dev.env`, and
the control socket's `status`, which gains a `panel` object
`{"mode", "hostname", "port", "listening", "sessions", "streams"}`.

| # | Check | OK | XATO or ESLATMA, and the hint |
|---|---|---|---|
| 1 | `PANEL_PORT`, `PANEL_METRICS_PORT` | "Panel portlari: 47831, 47832" | XATO "dev.env: PANEL_PORT/PANEL_METRICS_PORT noto'g'ri yoki bir xil" |
| 2 | the binary | "cloudflared 2026.10.3 (imzo: Cloudflare 68WVV388M8)", from the install marker, the binary's SHA-256 and `codesign -dv` | XATO "DATA/bin/cloudflared yo'q yoki imzosi boshqa" → `scripts/install.py --enable-panel` |
| 3 | the config | "Tunnel sozlamasi: DATA/tunnel/config.yml (0600)" | XATO on missing or wrong mode |
| 4 | credentials | "Tunnel kaliti: ~/.cloudflared/<UUID>.json (0600)" | XATO missing → §7.1 steps 4–6; XATO wider than 0600 → `chmod 600 …` |
| 5 | `cert.pem` | — | ESLATMA "~/.cloudflared/cert.pem bor — tunnel yaratilgach kerak emas; xavfsiz joyga oling" |
| 6 | the job | "uz.yordamchi.tunnel ishlayapti" (`launchctl print`: `state = running`) | XATO "yuklanmagan" → `launchctl bootstrap gui/$UID ~/Library/LaunchAgents/uz.yordamchi.tunnel.plist` |
| 7 | tunnel ready | "Cloudflare bilan aloqa bor" (`GET http://127.0.0.1:<metrics>/ready` → 200 within 3 s) | XATO "cloudflared Cloudflare'ga ulanmagan" → the internet, `LOGS/uz.yordamchi.tunnel.launchd.log` |
| 8 | local server | "Panel serveri 127.0.0.1:<port> javob beryapti" (`/healthz` 200) | XATO, when the dev bot is running but the server does not answer → `holat?`, `panel_bind_failed` in `LOGS/dev.jsonl` |
| 9 | mode and hostname (socket) | "Panel yoqilgan: <hostname>" | ESLATMA "Panel o'chirilgan (panel.mode = off)"; ESLATMA "panel.hostname bo'sh" |
| 10 | public reachability | "https://<hostname>/healthz → 200"; or a 302/403 to `*.cloudflareaccess.com` → OK plus "Cloudflare Access yoqilgan" | XATO with the status code; ESLATMA "Cloudflare Access yoqilmagan (tavsiya etiladi)" when it answered 200 |
| 11 | the menu button | "Menyu tugmasi standart" (`getChatMenuButton` without `chat_id` → `default` or `commands`) | XATO when it is `web_app` (D8) → BotFather: "Configure Menu Button" → reset |
| 12 | BotFather | — | ESLATMA "BotFather → Main Mini App manzili https://<hostname>/ bo'lsin (Bot API buni ko'rsatmaydi)" |

### 7.5 DEPLOY.md additions (unit O2 adds this as a new section "10. Panel (Telegram Mini App)")
- **What runs.** The table of §2 gains the row:
  `uz.yordamchi.tunnel` | `DATA/bin/cloudflared tunnel --config DATA/tunnel/config.yml … run` |
  `~/Library/LaunchAgents/uz.yordamchi.tunnel.plist` (with `--enable-panel`) | `LOGS/uz.yordamchi.tunnel.launchd.log`.
  §0's "There is no server, no inbound port" gains: "except, with the panel, the dev bot's loopback web server and the
  outbound tunnel (MINIAPP_SPEC §3.1)".
- **First setup:** MINIAPP_SPEC §7.1, verbatim.
- **Secrets table (§9):** a row `~/.cloudflared/<UUID>.json` | the tunnel credentials (runs this tunnel only) |
  read by cloudflared | written by `cloudflared tunnel create`. It stays in `~/.cloudflared`, mode 0600, never in the
  repo, an env file, a backup or a log. `cert.pem` is not kept there after setup.
- **Rotate the tunnel credentials** (suspected leak):
  `cloudflared tunnel login` → `cloudflared tunnel delete -f yordamchi` → `create yordamchi` →
  `route dns yordamchi <hostname>` (`--overwrite-dns`) → `scripts/install.py --enable-panel --panel-tunnel <new UUID>`
  → remove `cert.pem`.
- **Rotate the bot token** (§6): it now also changes the panel's HMAC key. Open panel sessions end at the bot's restart;
  reopen the panel.
- **Observe:** §7 gains rows: "Is the panel reachable?" → the check's Panel section; `holat?`'s panel line;
  `launchctl print gui/$UID/uz.yordamchi.tunnel`; `curl -s http://127.0.0.1:47832/ready`.
- **Rollback:** MINIAPP_SPEC §7.8.

### 7.6 QOLLANMA.md additions (Uzbek; unit O2), a new section "12. Panel (Mini App)"
- How to open it: the bot's profile → "Open App", or `panel` in the group (the link; you can pin it yourself).
- What you see: one card per project, the terminal lines, the states (ishlayapti, sizni kutyapti, to'xtatilgan,
  navbatda, tugadi), the clock, collapsing.
- Writing in a card: it goes only to that project. `to'xta`, `davom ettir`, `yangi sessiya`, `qayta yubor`,
  `yo'q, boshqacha qil: …`; answering an agent's question by number or text.
- The general line: like writing in the group (`holat?`, `loyihalar`, "BonviHR'da …", any question).
- Important decisions are answered only in the group (`ha` / `yo'q`); the panel shows them.
- Switching off and on: `panelni o'chir` (at once), `panelni yoq` (one question). `panel manzili …`. "Panel
  xabarlarini guruhga ham yoz" → `panel.echo_to_group`.
- If it does not open: "Panelni yopib, qayta oching"; `tekshir`.

### 7.7 Cloudflare Access (optional second layer)
**Recommendation: turn it on** (owner question Q2). The panel can instruct coding agents, and a forged `initData` needs
only the bot token. A token pasted into a transcript once already (DEPLOY §6) shows that tokens leak. With Access, a
leaked token alone does not open the panel.

Cost:
- a Zero Trust signup on the free plan (up to 50 users; Cloudflare may ask for a payment method even for $0);
- an email code inside the Telegram window once per session duration.

Setup (about 10 minutes):
1. Zero Trust → Settings → Authentication → One-time PIN.
2. Access → Applications → Add → Self-hosted → `panel.<domain>`, path empty → policy "Allow", Include: Emails = the
   owner's address → session duration 1 month.
3. A second application for `panel.<domain>/healthz` with a **Bypass** policy (Everyone), so check line 10 keeps working.

The app needs no change: the `CF_Authorization` cookie rides on same-origin requests and on the WebSocket upgrade. If
Telegram's webview loses cookies on some client, Access asks for the code again; that is the cost to accept or switch
off.

### 7.8 Kill switch and rollback, fastest first
1. `panelni o'chir` in the group: `panel.mode = off`. Every request answers 404 except `/healthz`; sessions are
   revoked; streams close within 15 s. No restart and no model are needed.
2. `launchctl bootout gui/$UID/uz.yordamchi.tunnel`: the hostname shows Cloudflare's error page; the bot is untouched.
3. `scripts/install.py --release <commit> --disable-panel`: the job and its plist go away (binary, config and DNS
   stay).
4. Code: DEPLOY §4.1 (point `releases/current` back). The panel is additive: there is no migration and nothing to
   restore; an older release ignores `PANEL_PORT` (reported as an ignored key) and the `panel.*` rows.
5. Remove for good: steps 2–3, then `cloudflared tunnel delete yordamchi` and delete the DNS record in the dashboard;
   BotFather → Configure Mini App → Disable Mini App.

### 7.9 Logs and observability
- **Events** (snake_case, ids and codes only, never content, tokens or `initData`):
  - `panel_started(port)`, `panel_bind_failed(port, error_type)`;
  - `panel_session_opened(session, user_id)`, `panel_session_closed(session, reason)`;
  - `panel_auth_dropped(reason, user_id)`, `panel_auth_locked(failures)`;
  - `panel_request(route, status, duration_ms)`;
  - `panel_message_accepted(update_id, card, duplicate)`;
  - `panel_stream_opened(session)`, `panel_stream_closed(session, code)`;
  - `panel_resync(reason)`, `panel_trace_dropped(count)`, `panel_card_without_origin(burst_id)`,
    `decision_panel_refused(decision_id)`.
- `session` is the first 8 hex of the token's sha256.
- aiohttp's access log is off (`AppRunner(access_log=None)`), and its error logger goes through `core.log` and
  `core/redact`.
- **`holat?`** gains one line from `panel.service.panel_health(deps, state)` (only when `PANEL_PORT` is set): `HEALTH_PANEL_ON`,
  `HEALTH_PANEL_DOWN` or `HEALTH_PANEL_OFF`. This goes through a new optional `panel=` probe on
  `shared/health/service.py::show_health`, and `PanelHealth` is added to `shared/health/domain.py`. The golden `holat`
  output changes, with a reviewed diff.

## 8. Testing

### 8.1 Unit tests (`tests/unit/…`, network closed, `FakeClock`)
**`shared/access/test_panel_auth.py`**: `verify_init_data`, `admit_panel`, `mint_panel_origin`, `channel_of_update_id`,
`admit()`'s channel check. The pinned vectors:
- **V1** (published by the tma.js docs, `docs.telegram-mini-apps.com/packages/tma-js-init-data-node/validating`; verified
  2026-10-25). Token `5768337691:AAH5YkoiEuPk8-FZa32hStHTqXiLPtAEhx8`, `now` = 1662771658, `max_age_s` 3600 → valid,
  `user_id` 279058397:
  ```
  query_id=AAHdF6IQAAAAAN0XohDhrOrc&user=%7B%22id%22%3A279058397%2C%22first_name%22%3A%22Vladislav%22%2C%22last_name%22%3A%22Kibenko%22%2C%22username%22%3A%22vdkfrost%22%2C%22language_code%22%3A%22ru%22%2C%22is_premium%22%3Atrue%7D&auth_date=1662771648&hash=c501b71e775f74ce10e377dea85a7ea24ecd640b223ea86dfe453e0eaed2e2b2
  ```
  The same with `auth_date=1662771649` → `BAD_HASH`.
- **V2** (computed 2026-10-25 with `tests/factories.DEV_BOT_TOKEN` = `123456789:AAFakeDevTokenForTestsOnly_01234567`;
  secret key `7304d4c1…b318`). `now` = 1792918830 (2026-10-25T09:00:30Z) → valid, `user_id` 424242
  (`factories.OWNER_ID`) → `admit_panel` → a `PanelPass`:
  ```
  auth_date=1792918800&chat_instance=-8134722200314281151&chat_type=sender&signature=c2lnbmF0dXJlLWZvci10ZXN0cy1vbmx5LW5vdC1yZWFsLWVkMjU1MTk&user=%7B%22id%22%3A424242%2C%22first_name%22%3A%22Abbos%22%2C%22language_code%22%3A%22uz%22%2C%22allows_write_to_pm%22%3Atrue%7D&hash=cac10e3b549047963d931639ec04bcb2ed11ce7ef536f8fb43575dd0127b3a17
  ```
- **V3**: V2 without its `signature` pair, the hash kept → `BAD_HASH` (proves `signature` is inside the check string).
- **V4**: a stranger, user 777000, correctly signed → `verify` valid, `admit_panel` → `Drop(STRANGER)`:
  ```
  auth_date=1792918800&chat_instance=-8134722200314281151&chat_type=sender&signature=c2lnbmF0dXJlLWZvci10ZXN0cy1vbmx5LW5vdC1yZWFsLWVkMjU1MTk&user=%7B%22id%22%3A777000%2C%22first_name%22%3A%22Begona%22%7D&hash=ad59a5681c42b60f1791dcd54bbe7b50dc2b2577496768e6b7c3d50c922826d2
  ```
- V2 checked with the token's last character changed → `BAD_HASH`.
- **Boundaries:** `now = auth_date + 3600` valid, `+ 3601` `EXPIRED`; `auth_date = now + 60` valid, `+ 61`
  `FROM_FUTURE`.
- **Malformed:** a duplicate key, a missing `hash`, an uppercase `hash`, a non-integer `auth_date`, a missing `user`,
  `user` not JSON, `"id": "424242"` (a string), `"id": true`, 4,097 bytes, a bad percent-escape.
- **Origin minting:** a lookalike pass → `TypeError`; `update_id = 5` → `ValueError`; the owner id changed since the
  pass → `Drop`; the minted origin has `channel = PANEL` and `chat_id = dev_group_id`.
- **`admit()`:** a `TELEGRAM` arrival with a negative id → `Drop(NOT_SERVED)`.

Further unit tests:
- **`shared/access/test_panel_pass_required.py`**: the inventory (§3.3 step 7).
- **`test_origin_required.py`**: gains `answer_card`.
- **`dev/panel/test_domain.py`** (pure):
  - card state, sub and clock for every `TurnStatus`; the primary turn and order; the `max_cards` trim;
  - every row of the §2.3 output table;
  - the card vocabulary (each phrase, with apostrophe variants and a final punctuation mark, and near misses such as
    "davom et va testlarni yoz" → `None`);
  - `next_update_id` (negative, monotonic, a clock step back, ids that sort by time);
  - session expiry arithmetic, token buckets;
  - every wire shape against `panel/test/fixtures/wire/*.json` (golden, `--update-golden`).
- **`dev/panel/test_service.py`** (fakes plus a real SQLite in `tmp_path`):
  - `submit_message`: a general input writes an inbox row with a negative id and the `panel` mark; a card input sets
    `card`; a repeated `client_id` → the same `update_id`, `duplicate`; the drain flag → `updating`; `too_long`;
    `project_unknown`; the panel off;
  - the echo row only with `panel.echo_to_group`;
  - sessions: open, idle and absolute expiry, eviction at `max_sessions`, a fourth session per launch, revocation on
    mode, hostname and owner id; the auth lock with exactly one `S.PANEL_AUTH_LOCKED`;
  - the hub: trace → ring → `line` frame; the scrub (below); overflow → `resync`; a card poll diff → `card` and
    `card_removed`; a sent note → `line` or `feed`, panel echoes skipped; a decision asked, then approved in the group →
    one `g<id>` line, then the same id with `state: "approved"`;
  - `read_state`: `turn_progress` lines before the first ring line, ring lines after.
- **`shared/burst`**: a card part stands alone, closes at once, never closes the assembling burst, and survives a
  restart (`parts_json` round trip); card stop words sort first.
- **`shared/inbox`**: the `panel` payload round trip; `origin_of_update` gives `PANEL`; the echo is in the same unit of
  work; `find_panel_update` by `client_id` inside the window only.
- **`shared/outbox`**: notes only for message ops, after the commit; a full queue drops and sending goes on.
- **`dev/turns`**:
  - a trace is queued with `detail` and `in_subagent`; a full queue → `traces_dropped`, and the turn goes on;
  - `submit_card_turn` writes the route and the delivery in one unit of work (a merge when working, else a new turn);
  - `answer_revert` refuses `PANEL`;
  - `forbidden_tool_rules` covers `~/.cloudflared`.
- **`dev/gate`**:
  - a `PANEL` origin is refused for every decision kind, nothing changes, the reply `S.PANEL_DECISION_IN_GROUP` is
    returned and **not** sent by the gate (the Messenger fake records no call), and `dev/questions` queues it in the
    outbox;
  - `list_card_decisions` returns each decision once with its current state and the exact question text;
  - **the S9 regression**: a panel "ha" with a pending deploy leaves the decision pending, and a group "ha" then
    approves it;
  - gate eval cases for `~/.cloudflared`.
- **`dev/questions`**: `answer_card` binds only P's questions (a "2" in BonviHR's card never answers HRMentor's), records
  the burst, refuses a lookalike origin.
- **`adapters/claude`**: `stream.py` fills `detail`, `tool_use_id` and `in_subagent` from recorded fixtures under
  `tests/fixtures/claude/streams/` (Bash with a long output → head and tail; Read; an MCP call; a subagent step).
- **`bots/dev`**:
  - pipeline: the card branch on fakes (each vocabulary row, a question answer, a decision refusal, work as a merge or
    a new turn);
  - pipeline: `panel`, `panelni yoq` (asks once), `panelni o'chir` (applies at once, needs an origin);
  - the panel loops absent in probation and when `PANEL_PORT` is unset.
- **ops**: `test_install.py` (the `--enable-panel` dry run: the pin, ports, the config and plist renders, no `launchctl`,
  no download; `--disable-panel`; `TunnelSecret` never in the output); the check's Panel section with a fake runner and
  urllib; the supervisor's `test_logtrim` with the tunnel log.

### 8.2 Integration tests (offline, in `make check`)
`tests/integration/test_panel_http.py` runs the real aiohttp app, with `security.py`, on a `web.UnixSite` in `tmp_path`.
Unix sockets are allowed by `--allow-unix-socket`. The client is aiohttp with `UnixConnector`, `Host: panel.test`,
`panel.hostname = panel.test`, and the deps are fakes (`FakeMessenger`, `FakeAgentRuntime`). It covers:
- every row of §4.2 and §4.5, and the headers of §4.1 on every response;
- S3 (a stranger: 401, 0 inbox rows, a log line with only the id);
- S5, S6, S10 and S11 (Origin, Host, OPTIONS, the WebSocket Origin, the auth timeout, the stream cap);
- a scripted turn whose steps stream as `line` frames in order;
- a card message delivered as `next` to the working turn;
- a general "holat?" whose reply arrives as a `feed` frame;
- `panel.mode` off mid-stream → `bye{disabled}`.

**Leak sweep** (`tests/fault/test_panel_leaks.py`):
- a vault value and a key-shaped string are placed in a tool's command and output, an agent text, a delivery, a bot
  message and a gated command (so in a decision's question);
- the snapshot, every frame, every response body and the captured logs are searched;
- the result must be 0 occurrences (NFR-SEC-03).

**Fault tests:**
- `kill -9` between a panel write's commit and its response, then a retry with the same `client_id` → exactly one
  delivery;
- a trace flood (10,000 steps) never blocks the turn pump;
- a stalled subscriber gets `resync`, and the others are unaffected.

### 8.3 Frontend (vitest 5 + jsdom + Testing Library; run by `make panel`)
- **`store.test.ts`**: snapshot then frames, `seq` filtering, dedupe by id, `resync` and a new `epoch`, optimistic items
  replaced, line caps — fed from `panel/test/fixtures/wire/*.json`.
- **`stream.test.ts`** (a fake WebSocket): `auth` first; backoff steps; the 40 s silence watchdog; close codes → states;
  `ping` only while visible.
- **`api.test.ts`** (a mocked `fetch`): error codes → states; retry with the same `client_id`.
- **`markdown.test.ts`**: the subset, and `<img src=x onerror=…>` stays text.
- **`i18n.test.ts`**: every key used exists (also enforced by `keyof`), with no unused keys.
- **Components:** collapse and expand with `localStorage`; the chip per state; Enter on desktop vs. mobile; the counter
  at 90 %; the empty and error screens; the attention chip scrolls; the decision hint.

### 8.4 End to end (opt-in Playwright; Python Playwright 1.64.1 is already pinned, Chromium already installed for P3)
`tests/e2e/test_panel_browser.py`, marker `e2e` (registered in `pyproject.toml`; `addopts` becomes
`-m "not live and not e2e"`); `@pytest.mark.enable_socket` with `@pytest.mark.allow_hosts(["127.0.0.1"])` (pytest-socket),
so only loopback opens:
- the server on `127.0.0.1:<random>` with `scheme="http"`, a test seam of `serve_panel` (production always passes
  `"https"`, and the CSP then names `ws:` instead of `wss:`);
- Chromium with `--host-resolver-rules="MAP panel.test 127.0.0.1:<port>"`, opening
  `http://panel.test/#tgWebAppData=<urlencoded initData>&tgWebAppVersion=8.0&tgWebAppPlatform=tdesktop`, with the
  `initData` signed at test time by the factory helper. So the real vendored Telegram script parses it.

Scenarios:
1. Auth, then a card streams a scripted turn.
2. Typing in the card → `FakeAgentRuntime` receives `next`.
3. A general `holat?` → the feed.
4. A stranger → "Kirish rad etildi.".
5. The panel off → its screen.
6. The server stopped and started → the banner, then recovered.
7. A vault value never appears in the DOM.
8. 0 `securitypolicyviolation` events and 0 console errors.

The owner's phone check, once, is in the DoD (§11).

### 8.5 Architecture and contract tests (in `make check`, stdlib, no node)
- **A5** extended: `PanelPass(` only in `shared/access/`.
- **A10** (new): every JSON handler in `bots/dev/panel/server.py` (`_session`, `_state`, `_messages`) awaits exactly one
  `panel.service` call.
- **`test_panel_dist.py`**: `scripts/panel_dist.py verify` passes.
  - The source hash is sha256 over sorted `(path, sha256)` of `panel/index.html`, `package.json`,
    `package-lock.json`, `vite.config.ts`, `tsconfig.json`, `vendor.json`, `src/**`, `public/**`; it must equal
    `build.json.source_sha256`.
  - Every `dist` file must match `build.json.files`.
  - `public/vendor/telegram-web-app.js` must match `vendor.json`'s SHA-256.
- **`test_panel_frontend.py`**:
  - only `panel/src/telegram.ts` mentions `Telegram`, and it uses only the members listed in §2.6;
  - no file in `panel/src/**` or `panel/dist/assets/*.js` contains `MainButton`, `SecondaryButton`, `BottomButton`,
    `SettingsButton`, `BackButton`, `sendData`, `switchInlineQuery`, `openTelegramLink`, `requestWriteAccess`,
    `requestContact`, `shareMessage`, `shareToStory`, `openInvoice`, `downloadFile` or `dangerouslySetInnerHTML` (the
    vendored script is exempt);
  - `uz.json` has `errors.<code>` for every `PanelError`, `card.state.<s>` for every `CardState`, `card.sub.<s>` for
    every `CardSub` and `decision.state.<s>` for every `DecisionState`.
- The existing `reply_markup` grep (0 hits) is unchanged; `getChatMenuButton` is the check's line 11.

## 9. Work breakdown

### 9.1 Units
Estimates are focused agent-days, tests included (R4 baseline). Every unit ends with `make check` green.

| Unit | Contents | Files it owns | Depends on | Parallel? | Estimate |
|---|---|---|---|---|---|
| **M0** contracts (sequential, first) | pre-carve every shared edit of this SPEC | `core/strings.py` (block `panel`: §2.7), `shared/settings/defaults.py` (block `panel`: §6.1, plus the `test_registry` list), `core/config.py` (§6.2), `core/paths.py` (§7.3), `pyproject.toml` (C5 line, `e2e` marker, `addopts`), `.gitignore` (`!/panel/dist/`, `panel/node_modules/`), `.protected-core` (`# pending:` lines for §9.3's new files), `Makefile` (`panel`), `tests/factories.py` (empty `# --- panel ---` block), `docs/CONVENTIONS.md` (§0.4), `tests/architecture/test_rules.py` (A5 `PanelPass`; A10, which passes while `bots/dev/panel/server.py` does not exist yet), `panel/test/fixtures/wire/*.json` (by hand from §4.3) | — | no | 0.5 |
| **U1** identity | §3.3 | `shared/access/{domain,service}.py`, `shared/audit/domain.py` (`PANEL_SESSION`), the `tests/factories.py` panel block (`sign_init_data`, `make_panel_pass`), `tests/unit/shared/access/test_panel_auth.py`, `test_panel_pass_required.py` | M0 | yes | 1.0 |
| **U2** decisions stay in the group | §3.7 gate and questions; S14 gate side; `list_card_decisions` | `dev/gate/{domain,service}.py`, `dev/questions/service.py` (`answer_card`, `_decided`), their tests, `tests/eval/gate/` cases, the `test_origin_required.py` entry | U1 | yes, with U3, U4 | 0.75 |
| **U3** intake plumbing | the `panel` mark, the echo, `origin_of_update` channel, panel reads; burst `PANEL_CARD`; the outbox sent queue and `list_recent_sent`; `PanelHealth` and the `show_health` probe | `shared/inbox/{domain,service,store}.py`, `shared/burst/domain.py`, `shared/outbox/{domain,service,store}.py`, `shared/health/{domain,service}.py`, their tests | U1 | yes, with U2, U4 | 1.0 |
| **U4** executor trace and turn reads | `Progress` fields; `stream.py` detail; `TurnTrace`, `traces`; `list_card_turns`, `list_owner_deliveries`, `list_turn_progress`, `submit_card_turn`; `answer_revert` PANEL; `forbidden_tool_rules` `~/.cloudflared`; `Method.PANEL` | `ports/agent.py`, `adapters/claude/stream.py`, `dev/turns/{domain,service}.py`, `dev/routing/domain.py`, `tests/fixtures/claude/streams/`, their tests | U1 | yes, with U2, U3 | 1.25 |
| **U5** panel feature | `dev/panel` domain and service (§3.3 sessions, §3.4 submit, §3.5 snapshot and hub) | `dev/panel/{__init__,domain,service}.py`, `tests/unit/dev/panel/*`, the factories panel block (after U1) | U1, U3, U4 (the domain part may start after M0) | partly | 2.0 |
| **U6** HTTP edge | §3.2 server, §3.8 middlewares, §4 | `bots/dev/panel/{__init__,server,security}.py`, `tests/integration/test_panel_http.py`; promotes its `.protected-core` line | U5 | no | 1.25 |
| **U7** wiring (sequential) | composition, loops, pipeline card branch, intents, socket status, `holat?` line | `bots/dev/{app,runtime,pipeline,intents,control_socket}.py`, `prompts/dev/selfupdate.md`, `tests/golden/holat*`, `tests/unit/bots/dev/test_panel_*` | U2, U3, U4, U5, U6 | no | 1.0 |
| **F1** frontend | §2 and §8.3; `make panel`; the committed `dist` | `panel/**` (it consumes `panel/test/fixtures/wire/*`, owned by M0/U5), `scripts/panel_dist.py`, `tests/architecture/test_panel_{dist,frontend}.py` | M0 | yes, all along | 2.5 |
| **O1** installer and tunnel | §7.2, §7.3 | `scripts/install.py`, `ops/launchd/uz.yordamchi.tunnel.plist.tmpl`, `ops/cloudflared/config.yml.tmpl`, `tests/unit/ops/test_install.py`, `docs/STACK.md` §5 (the pin row); promotes its `.protected-core` lines | M0 | yes | 1.0 |
| **O2** check and docs | §7.4–§7.6 | `bots/cli/check.py`, its tests, `docs/DEPLOY.md`, `docs/QOLLANMA.md` | O1; U7 for the socket fields and the docs | the check part yes, the docs after U7 | 0.75 |
| **O3** supervisor log trim | §7.3 log size | `supervisor/src/yordamchi_supervisor/logtrim.py`, `supervisor/tests/test_logtrim.py` | — | yes (its own commit; R-32) | 0.25 |
| **T1** end to end and leak sweep | §8.2 leak and fault tests, §8.4 | `tests/e2e/*`, `tests/fault/test_panel_leaks.py` | U7, F1 | no | 1.0 |
| **T2** review | `qa-review` (CONVENTIONS), `qa-security` (§3.8 S1–S17, each with its proof), `test-migration` (`alembic check` clean, no revision) | — (fixes go back to the owning unit) | T1 | no | 0.5 + fixes |

Total: about 14.75 agent-days. Critical path: M0 → U1 → U4 → U5 → U6 → U7 → T1 → T2, about 8.5 days; with U5's domain
part started early, about 8. F1, O1 and O3 run beside it from the start, and O2's check part after O1.

### 9.2 Shared files (one writer at a time, merged in order by the merge train)
| File | Writer(s), in order |
|---|---|
| `core/strings.py`, `shared/settings/defaults.py` | M0 only. A later change is its own sequential step |
| `core/errors.py` | untouched (panel refusals are values, §4.5) |
| `migrations/versions/*` | untouched (§5) |
| `bots/dev/intents.py`, `bots/dev/app.py` | U7 only |
| `pyproject.toml` | M0 only. `uv.lock` is untouched: no new Python dependency |
| `.protected-core` | M0 (pending lines) → U6 promotes `src/yordamchi/bots/dev/panel/security.py` → O1 promotes the two templates |
| `tests/factories.py` (block file) | M0 (carves the block) → U1 → U5 |
| `Makefile`, `.gitignore`, `docs/CONVENTIONS.md`, `tests/architecture/test_rules.py` | M0 only |
| `tests/unit/shared/access/test_origin_required.py` | U2 only |
| `docs/SPEC.md` §7 | this task (the one cross-reference line) |
| `docs/ASSUMPTIONS.md` | the merge train only (§12) |
| CONVENTIONS §4 port table, `tests/eval/thresholds.toml` | untouched |

### 9.3 Protected-core changes (this SPEC, once approved, is the explicit task each one needs; each ships with tests)
- `src/yordamchi/shared/access/**` (U1): the channel, `initData` verification, `PanelPass`, origin minting.
- `src/yordamchi/dev/gate/**` (U2): the `PANEL_REFUSED` rule; `~/.cloudflared` forbidden.
- `src/yordamchi/bots/dev/panel/security.py` (U6, new, added to the list).
- `ops/launchd/uz.yordamchi.tunnel.plist.tmpl` and `ops/cloudflared/config.yml.tmpl` (O1, new, added). Why: a changed
  tunnel target could expose the control socket (`cloudflared` can proxy `unix:` paths).
- `supervisor/**` (O3): its own commit, never together with app changes (R-32).
- `.protected-core` itself (M0, U6, O1).

## 10. Risks and owner questions

### 10.1 Risks (continuing RISKS.md's numbering; the merge train copies them there)
| ID | Risk | L / I | Mitigation |
|---|---|---|---|
| R-53 | A leaked bot token now also forges `initData`, so it opens the panel and can instruct agents | L / H | Access (Q2); decisions group-only (§3.7); the token lives in the Keychain; the auth lock notice; DEPLOY §6 rotation ends every session |
| R-54 | The tunnel credentials JSON is stolen and a rogue connector receives part of the panel traffic | L / H | the executor is forbidden `~/.cloudflared` (S14); 0600; check lines 4–5; rotation in §7.5 |
| R-55 | Cloudflare sees panel traffic in clear (TLS ends at its edge): code, outputs, integration data | M / M | MCP output hidden by default; `Read` content never shown; scrubbed; the same trust class as Telegram, which already sees the group |
| R-56 | A panel bug slows or breaks the dev bot | L / H | bounded queues that drop; no panel code in the turn pump or the sender; loops under `forever`; the kill switch needs no model; fault tests |
| R-57 | Telegram webview differences (the iOS keyboard over the sticky input, a reload with old `initData`, lost cookies under Access) | M / L | `viewportStableHeight` and safe areas; the reopen message; the owner's phone check in the DoD |
| R-58 | Strangers who find the hostname see that a web app exists there (the shell loads; `/api` answers 401) | M / L | the shell holds no data and says nothing about Yordamchi; Access hides even that |

### 10.2 Owner questions (at most three; if he says nothing, the default stands)
1. **Q1 — Domain.** Do you already have a domain on Cloudflare, or one you can move there, and which name should the
   panel use?
   Default: `panel.<your domain>` on a domain you already own. If you have none, nothing is bought (registering one at
   Cloudflare costs about $10 a year and needs your go-ahead), and the panel ships switched off (`panel.mode = off`)
   until you name a hostname.
2. **Q2 — Cloudflare Access.** Add Access as a second lock (an email code inside Telegram once a month)?
   Recommended: yes (R-53). Default if you are silent: the panel goes live without it, with every other layer in place,
   and the check shows an ESLATMA line until it is on (§7.7).
3. **Q3 — Decisions.** In v1, important decisions (deploy, data loss, money, sending to others) are answered only in the
   group, never from the panel; the panel shows them. Is that right?
   Default: yes, group only (§3.7).

## 11. Definition of Done (the machine confirms)
- [ ] `make check` is green: ruff, mypy `--strict`, import-linter C1–C8 with the new C5 line, A1–A10, pytest with the
      network closed (unit, integration over `UnixSite`, the leak sweep, the fault tests, both inventories, dist
      freshness, the frontend allowlist), the supervisor's tests, `alembic upgrade head` plus `alembic check` with **no
      new revision**.
- [ ] `make panel` is green: `tsc --noEmit`, vitest, `vite build`, and `build.json` rewritten with no diff in git.
- [ ] `pytest -m e2e tests/e2e` is green on Chromium, with 0 CSP violations.
- [ ] The collected test count did not drop; no test, eval case or threshold was weakened or skipped.
- [ ] Owner-only: a stranger's `initData` → 401, 0 rows, a log with the id only; a lookalike `PanelPass` or
      `OwnerOrigin` → `TypeError` before anything is written, for every inventory entry.
- [ ] The S9 regression passes: a panel "ha" never approves; the group's "ha" still does.
- [ ] Leak sweep = 0 across frames, responses and logs; `reply_markup` grep = 0; check line 11 green (default menu
      button).
- [ ] `qa-security` closed S1–S17, each with its proof named in §3.8; `qa-review` has no open findings.
- [ ] Installed with `scripts/install.py --enable-panel` on the Mac (owner's go-ahead), and the check's Panel section is
      all green from the launchd context (line 10 is 200, or a redirect when Access is on).
- [ ] The owner, once, on his phone: opened the panel from the profile and from the link; saw a running card stream;
      sent a general `holat?`; sent a card instruction; stopped a turn with `to'xta` in its card; got the group refusal
      for a panel `ha` on a test decision. The orchestrator logs the result in TASKS.
- [ ] `docs/DEPLOY.md` §10, `docs/QOLLANMA.md` §12 and `docs/STACK.md` §5 (the cloudflared pin) are written; the merge
      train has appended §12's lines to ASSUMPTIONS and §10.1's rows to RISKS.
- [ ] Everything from earlier phases still passes (P1 DoD, SPEC §12).

## 12. Decisions to append to ASSUMPTIONS.md (by the merge train)
- [2026-10-25] Panel stream transport → a WebSocket with first-frame auth, writes on POST → `EventSource` cannot send
  headers, so the token would sit in URLs or in a CSRF-prone cookie; every reconnect re-snapshots, so SSE's replay buys
  nothing.
- [2026-10-25] Panel progress delivery → push for executor steps (`TurnsState.traces`) and sent messages
  (`SenderState.sent`), a 2 s poll for card states and deliveries → the detail exists only in memory, and status
  changes are written in about 20 places.
- [2026-10-25] Panel and decisions → no `decisions` row can be approved or declined from the panel; `dev/gate` refuses a
  `Channel.PANEL` origin → a leaked token or a replayed initData must never approve a deploy.
- [2026-10-25] Panel identity → `OwnerOrigin.channel`; panel update ids are negative, time-ordered
  (`−(2**62 − ms × 1024)`); `PanelPass` minted only by `shared/access` → no migration, one definition of the channel,
  and restart-safe dedupe by `client_id`.
- [2026-10-25] Panel frontend → a committed `panel/dist` verified by a stdlib test in `make check` → the supervisor and
  the installer stay node-free and offline.
- [2026-10-25] Panel echo into the group → off by default (`panel.echo_to_group`) → the group's 20 messages a minute
  belong to answers (T37).
- [2026-10-25] Panel tool output → local tools shown (Bash tail, counts for Read and Grep, never file content); MCP
  output hidden by default (`panel.mcp_output`) → panel traffic passes through Cloudflare in clear.
- [2026-10-25] Tunnel ingress → one catch-all to `127.0.0.1:PANEL_PORT`, and the app enforces `Host = panel.hostname`
  → the hostname has one source of truth, the setting.
- [2026-10-25] cloudflared pin → 2026.10.3: tgz `1165d14b…`, binary `f249139e…`, Team ID 68WVV388M8 → the release
  notes print the binary's hash against the tgz's name.
