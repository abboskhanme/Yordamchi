# Yordamchi — Requirements (v1)

Status: draft for owner review · 2026-10-23 · Risks: `docs/RISKS.md` · Assumptions: `docs/ASSUMPTIONS.md`

Two Telegram bots on the owner's Mac. They let one developer run his whole dev workflow, and get
answers to anything he asks, from his phone:

- **Dev bot** (built first) drives coding agents across his projects.
- **Personal bot** is a general assistant that informs but never acts on the computer.

Bot text is Uzbek (Latin). Everything else is English.

---

## 0. Decided by the owner (not open for discussion)

| # | Decision |
|---|---|
| D1 | Stack: Python 3.12 (uv), aiogram 3, Claude Agent SDK / official `claude` CLI, google-genai, SQLite via SQLAlchemy + Alembic. Switching to Postgres is a config change. |
| D2 | Both bots run on the owner's Mac (MacBook Pro M4) as launchd services and use Telegram long polling. No inbound port. |
| D3 | Each project is a **headless** Claude Code session (not tmux, not a visible terminal). Sessions resume by session id, and the owner can continue a session at the Mac. |
| D4 | The dev bot lives in **one flat private group**: owner + bot only, **no Topics**. |
| D5 | Routing is content-first (§3, UC-10). A Telegram reply is one signal among several, not the main mechanism. |
| D6 | **Brain for both bots is the owner's Claude subscription**, through the official `claude` binary (Agent SDK). Gemini is used only for speech-to-text, text-to-speech and, where useful, search or video understanding. OpenRouter is optional (extra models or fallback only). |
| D7 | The owner has 3–4 Claude accounts. `../ClaudeManager` rotates them by moving the next login into Claude Code's keychain slot. Yordamchi uses the standard `claude` login and **never handles Claude credentials**. |
| D8 | **No inline buttons or keyboards anywhere.** All interaction is conversation (text or voice). |
| D9 | **Approval policy.** Commits, `git push`, branches, creating GitHub repos and routine actions are **automatic, never asked**. The bot asks, in conversation, only for **important decisions**: deploys or anything that changes a production server, irreversible data loss, spending money, and architecture forks (full list in UC-13). An explicit owner instruction is always carried out. |
| D10 | Speed of answers (questions, reports, status, analytics, Q&A) is the **top priority**. Coding may be slow. |
| D11 | Integrations (MCP servers, API keys) are **owned by Yordamchi**: stored in its own registry and injected into every session it starts. They are never tied to a Claude account. |
| D12 | Both bots are hard owner-only. The owner's Telegram user id is allowlisted. The dev bot also accepts only the one configured group. Everything else is ignored silently, and the bot leaves foreign groups. |
| D13 | Computer control is for programming work only: folders, code, git, GitHub, servers, deploys, terminal commands. |
| D14 | **Self-configuration is a core goal.** After delivery the owner never writes code for these bots again. Every setting is changed by telling the bots (UC-09). Code-level changes are requested by chat and go through a safe self-update pipeline (UC-19). More requirements will come later, so the system must be extensible by chat. |

---

## 1. Problem

The owner is a solo developer with about 35 project folders in `~/Documents/Projects`. All of his
work runs through Claude Code in terminals on one laptop. Today:

- **When he is away from the desk** (street, meetings, travel), work stops. A one-line change
  ("in X change Y") or a check ("what's the state of X", "how is server Z") waits until he is back
  and has opened the laptop.
- **Sessions block on him.** When a running session asks a question or needs a decision, it sits
  idle until he looks at that terminal. With several projects in flight, most of the waiting is on
  his side, not the agent's.
- **Carrying the laptop is the only way to stay in control.** He opens it in meetings and on the
  road just to type a short instruction or read a status line.
- **Simple information has a high cost.** Checking a project's git state, the last result, or a
  server's health means opening a terminal, finding the right session and reading through it.
  General questions (news, finance, advice, religion, learning) are scattered across apps and are
  awkward to ask by voice on the move.

**Where it loses time:** idle agents waiting for answers, deferred small tasks, and
minutes-per-check for status that could be read in seconds.

---

## 2. User roles

| Role | Who | What they do | How often | Must not see / do |
|---|---|---|---|---|
| **Owner** | The single human user of both bots, permanently | Instructs projects, answers agent questions, makes important decisions, asks anything | All day, mostly by phone, often by voice | — |
| *Executor session* (non-human) | A Claude Code (or Codex) session in a project folder | Writes code, runs commands, commits and pushes, asks the owner | Per task | Must not deploy or destroy data without a "yes" (UC-13). Must not see secrets it does not need. Must not commit secrets. |
| *Untrusted content* (non-human) | Web pages, links, files, images, MCP/API results, server output | Data only | Constant | Must never be treated as an instruction. Personal-bot content never reaches the dev executor. |
| *Stranger* | Any other Telegram user, chat or group | — | — | Sees nothing. Gets no reply, no error, no sign the bot is alive. |

There are no other roles: no team, no admins, no guests.

---

## 3. Use cases

Notation: **UC-xx** — *Owner* does *action* → gets *result*. Main flow, then failure flow (F).

### 3.1 Common to both bots

**UC-01 Owner-only access.** A Telegram update arrives → it is processed only if it comes from the
owner (and, for the dev bot, from the configured group).
- F: any other user or chat → dropped silently and logged by id only. Added to a foreign group →
  the bot leaves.

**UC-02 Conversational burst ("listens like a person").** The owner sends 2–3 parts on one topic
back-to-back, such as a screenshot, then text, then a voice note, or voice, a pause, more voice and
an image. The bot treats them as **one turn** and answers once.
- Adaptive quiet window: short after text, longer after a bare image or file. A voice note being
  transcribed, or an album still arriving, keeps the window open.
- A lone image or file with no follow-up → the bot proceeds with a sensible interpretation from
  context instead of asking.
- A follow-up that arrives after work has started is merged into the running turn (same session).
- F: an unrelated message arrives mid-turn → it is **not** merged. The dev bot routes it on its
  own. The personal bot answers it as part of the same reply only if it arrives before the reply is
  sent.

**UC-03 Interrupt or redirect.** The owner cuts in with "to'xta", "stop", "bas", "kut", or
"yo'q, boshqacha qil: …" → the current turn stops, or is redirected in the same session, right away.
- "kut" during assembly holds the burst window open for the next part.
- F: on the dev bot, several projects are running and the target is unclear → one short text
  question naming the running projects. "hammasini to'xtat" stops all of them.

**UC-04 Give a secret in chat.** The owner pastes an API key, token or MCP credential → it is
stored encrypted, his message is deleted, and the bot confirms with a masked value (`••••abcd`).
The secret never reaches an LLM, a log or a bot reply.
- F: the bot cannot delete the message (missing rights, or older than 48 h) → it says so and
  recommends rotating the key.
- F: the owner pastes a Claude login token → refused. Accounts are ClaudeManager's job.

**UC-05 Voice input.** Every voice note is transcribed (Gemini). On the dev bot, the transcript is
echoed in one line with the routed project, so a misheard instruction is visible.
- The owner speaks mostly Uzbek but mixes in Russian and English words and phrases (technical
  terms, names, whole sentences). Every part is transcribed in the language it was spoken, without
  translation: Uzbek in Latin script (oʻ, gʻ, sh, ch), Russian in Cyrillic, English as spoken.
  Same approach as `../OvozYoz` (`engines/segment.py`): language hints `uz-UZ`, `ru-RU`, `en-US`
  on every request, plus a custom vocabulary.
- The vocabulary is built automatically from the project registry (names and aliases), the
  integration names and a term list the owner can extend by chat ("bu so'zni to'g'ri yoz: …").
  Project names are therefore spelled exactly, which routing (UC-10) depends on.
- F: transcription fails → "Ovozni tushuna olmadim" plus the reason. The note is not dropped from
  the burst; the bot asks for text only if nothing else in the burst explains the request.

**UC-06 Health and status.** The owner asks "holat?" (or `/holat`) → uptime, versions, Claude login
and ClaudeManager state, Gemini key state, running and queued sessions, pending decisions, last
errors.
- The dev bot keeps a pinned "Onlayn: HH:MM" message in the group and edits it every 5 minutes, so
  a stale time tells the owner the Mac is down.
- F: crash or restart → the owner is told after the restart. Crash loops are reported by a
  separate watchdog (NFR-REL).

**UC-07 Cost and usage.** The owner asks "xarajat?" → per bot and per project, for today, 7 days
and this month: turns, run time, the Claude API-equivalent cost estimate, Claude 5h/7d utilization
(read from ClaudeManager, if available), and paid-API spend (Gemini, OpenRouter, OpenAI) against
caps.

**UC-08 Integrations owned by Yordamchi.** The owner says "connect to this" and gives an MCP server
URL or token, or an API key with what it is for → Yordamchi:
1. validates it (connects and lists the tools);
2. stores it in **its own registry** (secret encrypted);
3. injects it into every session it starts, for the bots it is assigned to;
4. confirms with the tool list.

The owner can list and remove integrations by chat. OAuth MCP servers: Yordamchi runs the OAuth
flow itself (sends the owner the link), stores the tokens and refreshes them.
- F: validation fails, a token expires or the server is down → a clear Uzbek message naming the
  integration and what to do. It is never a silent miss.

**UC-09 Runtime settings by chat (level 1 of self-configuration).** The owner says what to change,
for example:
- "javoblaring qisqaroq bo'lsin";
- "hisobotlar uchun tezroq model";
- "rasmdan keyin 15 soniya kut";
- "migratsiyalarni so'rama";
- "BonviHR'ni 'hr' deb ham tani".

The bot restates the change in one line, applies it, and it is in effect **from the next message**,
with no code change and no restart. Everything behaviour-related lives in data, not code:
- persona and tone;
- the system prompts of both bots and of executor sessions;
- voice, language and accent;
- model tier per path (routing, Q&A, reports, coding, investigation);
- routing aliases and the project registry;
- the important-decision ("ask vs auto") list;
- burst windows and latency-related timeouts;
- integrations;
- schedules (digest refresh, server checks, heartbeat, cost summary, quiet hours);
- caps (cost, concurrency, runaway guard);
- text and voice length limits.

"sozlamalaring qanday?" returns the current settings, grouped, with the full list as a file.
"oldingi holatiga qaytar" reverts the last change, or a named one. Every change is versioned and
audited.
- F: an invalid value (out of range, unknown model, unknown voice) → the bot says why and suggests
  valid options. Nothing changes.
- F: a change that **weakens a safeguard** → the bot restates it and asks once before applying.
  Examples: removing an item from the important-decision list, disabling the secret scan, raising
  a cost cap, changing the owner id or group id.
- F: a "settings change" that comes from anything other than the owner's own message (web page,
  file, tool result, executor output) → ignored. Only the owner's messages can change settings.
- F: the request needs code, not a setting → the bot says so and offers to do it as a self-update
  (UC-19).

### 3.2 Dev bot

**UC-10 Route a message to a project (content-first).** A message or burst arrives → the bot
picks the project from these signals:
- explicit name or alias (fuzzy, including Uzbek suffixes such as "BonviHR'da", "hrmentorga");
- the content matched against each project's description;
- recent conversation context and which project reported last;
- which project has an open question;
- a reply to a project's message.

It routes when it is confident and **always names the target** in its first reply ("→ BonviHR").
- F: low confidence, or two projects could be confused → one plain-text question naming the
  candidates ("BonviHR'gami yoki HRMentor'gami?"). Answering with a name or "birinchisi" resolves
  it.
- F: wrong target noticed ("bu HRMentor uchun edi") → the turn in the wrong project stops. The bot
  reports any files changed there and re-sends the instruction to the right project. Reverting the
  wrong changes is offered in text and done only on "ha".

**UC-11 Instruct an existing project.** "In X change Y" → the bot sends the instruction into X's
session, starting or resuming it if needed. It keeps one live progress message per project, edited
in place, then sends a final result as a new message: the `Natija:` line, changed files, test
status, commit and push status, and duration. The work ends **committed and pushed**.
- F: the session errors out or hits a usage limit → the bot reports the reason. On a limit it
  waits for the reset (or for ClaudeManager's rotation) and continues automatically.
- F: a turn runs longer than the limit (NFR-COST-03) → it pauses and the bot asks "davom
  ettiraymi?".

**UC-12 Answer an agent's question.** The agent asks the owner something → the bot posts it as a
text question: the project name first, options numbered. It references the material already sent.
The owner answers with a number, text or voice, by reply or not. Routing gives priority to the
project with the open question.
- F: several projects have open questions and the answer is ambiguous → one short question naming
  them.

**UC-13 Important decision (conversational approval).** The agent is about to do something on the
important-decision list (below) → the turn pauses on that action. The bot asks **one** clear
question saying what, where and the consequence. A "ha", "qil" or "mayli" (text or voice) runs
exactly that action once; "yo'q" or an alternative instruction cancels it or redirects the agent.
Other projects keep working meanwhile.

Important-decision list (deterministic, enforced at the tool layer, the same for every executor):
1. Deploying, or any command that changes a server (anything over SSH/SCP/rsync to a remote host
   that is not on the read-only allowlist; deploy CLIs; remote `docker compose up/down/restart`).
   This includes a push to a branch whose push triggers a production deploy (a CI workflow that
   deploys on push to that branch).
2. Irreversible data loss:
   - dropping or truncating a database, `dropdb`, `DROP`/`TRUNCATE`;
   - a destructive migration against a non-test database;
   - `docker compose down -v`, `docker volume rm`, `docker system prune`;
   - deleting a project folder, untracked non-ignored files, or anything outside the project;
   - `git push --force` to the default branch;
   - deleting a GitHub repo;
   - making a repo public.
3. Spending money: purchases, paid plan or scale changes, paid-API usage beyond the cap (NFR-COST).
4. Sending something to a third party: posting, email, messages.
5. An architecture fork that changes the approved plan (the agent's judgment, by instruction). This
   includes the stack choice for a new project.

The following are **never asked about**: commits, push, branches, creating a private GitHub repo,
deleting git-tracked files or ignored build artefacts, installing dependencies, running tests,
builds and local Docker.
- F: the owner gives no answer → the turn waits. The question stays listed in "holat?". After 2 h
  the approval expires and is asked again before running.

**UC-14 New project from a description.** The owner describes a project in any format, possibly
across several messages:
1. The bot collects the parts into one draft. It closes the draft after 2 minutes of silence or
   when the owner says "tamom" / "boshla".
2. It creates a folder in `~/Documents/Projects`, runs `git init`, and creates a private GitHub repo.
3. The executor writes the plan docs (REQUIREMENTS, SPEC; the stack question per the owner's rules).
4. The bot sends a plan summary plus the docs as files and asks, in text, for approval.
5. "ha" → the executor builds from zero. All Q&A happens in the group. The build ends committed and
   pushed. "o'zgartir: …" → the plan is revised and re-sent.
- F: the folder name already exists → the bot proposes another name in text. No code is written
  before approval.

**UC-15 Report or info query (fast path).** "What's the state of X", "what changed today", "how is
server Z" → answered from the **project digest** Yordamchi keeps up to date: last results, git
state, recent commits and changes, open questions, running tasks, last server checks. Every answer
states how fresh its data is. It never waits behind a running coding turn.
- F: the digest is stale, or the question needs fresh investigation → an immediate "Tekshiryapman
  (~N daqiqa)" message, then a read-only investigation in a separate session (may SSH via the Mac,
  read-only allowlist). The result follows. The investigation cannot modify files.

**UC-16 Handoff with the Mac.** The owner asks "X sessiyasi" → he gets the exact
`cd <path> && claude --resume <id>` command. While he has that session open at the Mac, the bot
does not write to it. Messages for X are queued, and the bot says so in text ("Mac'da ochiq —
navbatga qo'ydim; yangi sessiya kerakmi?"). When he closes it, the queue is delivered into the same
session, which now includes his Mac conversation.
- F: he opens the session at the Mac while a bot turn is running → the bot interrupts its own turn
  and tells him. The Mac wins.

**UC-17 Switch executor.** "ChatGPT'ga o't" (globally, or "X uchun") → X's executor becomes OpenAI
Codex CLI (ChatGPT subscription); "Claude'ga qayt" switches back.
- The new executor gets a handoff summary: recent `Natija` lines, git status, open questions.
- The previous session id is kept for switching back.
- The important-decision gate, the commit/push policy and the owner's global rules apply equally.
- F: Codex is not installed or not logged in → the bot states exactly what is missing and the steps
  to fix it. It never falls back silently.

**UC-18 Project registry by chat.** "loyihalar" lists projects with status. The owner can add an
alias ("BonviHR'ni 'hr' deb ham tani"), archive or unarchive a project, or change its description.
New folders under `~/Documents/Projects` are registered automatically. The description is built
from README, CLAUDE.md or SPEC.
- **Park a session by chat.** "Bu loyihani yopib tur, keyinroq davom ettiramiz" → the bot finishes
  or stops the current turn, saves a short "where we stopped" note in the digest, and closes the
  session process. "X'ni davom ettiramiz" resumes the same session id with that note. A parked
  project still answers reports from its digest.

**UC-19 Self-update by chat (level 2 of self-configuration).** "O'zingga X qo'sh" or
"Yordamchi'da … o'zgartir" → the dev bot works on Yordamchi's own repo like any project, through
the safe self-update pipeline:
1. Work happens on a branch. The full test suite (including gate, secret-leak and routing tests)
   must pass.
2. Before switching, any coding turns running in other projects are allowed to finish (up to 30
   min, then the bot asks). A pre-update copy of the DB is taken. Inbound polling pauses, so
   Telegram holds new messages.
3. A **self-updater** switches to the new version, restarts the bots and runs a health check:
   both bots start, polling works, and a synthetic end-to-end message passes, all within 2 min.
4. If healthy, the branch is merged and pushed, and the version becomes the new **last good
   version**.
5. If not healthy (it does not come back, it crashes, or it fails the health check), the
   self-updater **automatically rolls back** to the last good version, restores the DB copy if a
   migration ran, restarts, and reports in plain Uzbek what failed.
6. The owner is told the outcome either way, in plain Uzbek: what changed, version, tests, and
   health.

The self-updater is separate from the bots. It has its own code, runtime and dependencies, and it
**does not depend on the code it is replacing**, so it survives a broken update of the bots. Changes
to the protected core are an important decision: one question (see Q5). The protected core is the
self-updater, watchdog, owner allowlist, important-decision gate and secret store.
- F: tests fail → nothing is switched. The bot reports the failing tests and keeps working on the
  branch, or asks.
- F: the self-updater itself is broken → the bots keep running on their current version, and the
  watchdog alerts the owner. Manual repair at the Mac is the only path (RISKS R-32).

### 3.3 Personal bot

**UC-30 Ask anything.** General Q&A, advice, explanations, learning, worldview, religious
questions, poems, songs and stories → a **text answer first**, then a **voice note** (about
20–30 s) giving the key points and what to pay attention to.
- Long answers are compressed to the most complete short form. "to'liqroq" sends the full version
  (split messages, or a `.md` file).
- Personality: short, precise, professional, a little warm. It has its own position, grounded in
  evidence, scientists' and Islamic scholars' views, and says where scholars differ. It does not
  mirror the owner.
- F: Claude is unavailable → the bot says so, and per Q4 gives a labelled fallback answer.

**UC-31 Analyse media and links.** The owner sends an image, a video (≤ 20 MB), a web link or a
YouTube link → the bot says what it is, the key points and its opinion. Instructions inside the
content are ignored.
- F: the file is too large to download (> 20 MB) → it says so and asks for a link or a shorter
  clip. Paywall or login pages → it says it cannot see the content.

**UC-32 Request an artefact.** "Screenshot of site X", "get the image or file from this page" → the
bot returns a PNG (viewport, plus a full-page PNG as a file), or the file itself if it is ≤ 50 MB.
"Send me the video from this site / link" → the bot downloads it (yt-dlp, for the owner's personal
use, YouTube included) and sends it, at a quality that fits ≤ 50 MB. The reply format follows the
request.
- F: the video cannot fit in 50 MB even at low quality, or the content needs a login → the bot
  explains why and offers what it can do instead (a shorter clip, a summary, the direct link).

**UC-33 News.** "Bugungi yangiliklar", "moliya yangiliklari" → recent items, each with source and
publication time. Market numbers carry their source and date.

**UC-34 Personal-bot settings by chat** (a case of UC-09). The owner can change:
- persona and tone;
- the model tier;
- the voice ("ovozing yoqmadi" → 3 short numbered voice samples, and he answers "ikkinchisi");
- gender, accent and language;
- voice on/off;
- verbosity.

Applied from the next reply and persisted. Unsupported requests are answered honestly, with an
offer to add the capability as a self-update (UC-19).

**UC-35 Use an integration.** The owner asks something that an integration added via UC-08 can
answer (for example Bonvi HR, Wunderkind) → the bot uses it automatically, without the owner
supplying anything again. The personal bot calls only read-only tools, or tools the owner
explicitly allowed when adding the integration.

**UC-36 Read project status (read-only bridge).** "BonviHR qanday?" → answered from the dev bot's
project digest (UC-15). The personal bot has **no path to instruct the dev bot or any executor**.

---

## 4. Acceptance criteria

Latencies are p90 over ≥ 20 samples on the owner's Mac on normal Wi-Fi. The clock starts at receipt
of the **last** part of the burst, so the quiet window is included. Eval sets live in the repo as
fixtures and run in the test suite. "Owner-reviewed" means the owner grades the eval output once
before release.

### 4.1 Use cases

**UC-01**
- From a second Telegram account, messages, commands and voice to either bot get no
  reply. The executor receives nothing. The log records only the numeric id.
- Adding the dev bot to any other group → it leaves within 10 s and posts nothing there.
- The dev bot ignores the owner's own messages in any chat other than the configured group. The
  personal bot ignores any group other than the configured private one, if one is configured.
- Setup is verified by a check script: BotFather join-groups is disabled after setup. The dev bot
  receives non-command group messages (privacy mode off, or admin) and has the delete-messages
  right.

**UC-02** (eval set ≥ 40 bursts, both bots)
- Photo without caption, then text 6 s later → **one** turn: the executor receives both parts in
  one input, and the bot sends exactly one answer.
- Text, then a voice note (≤ 60 s) starting ≤ 2 s after → one turn.
- Album of 4 photos, then text ≤ 3 s after the last photo → one turn with 5 parts.
- Lone photo, nothing follows → the bot proceeds after the window (≤ 10 s). It asks nothing in
  ≥ 9 of 10 cases labelled "interpretable". On the dev bot, a lone error screenshot triggers a
  read-only diagnosis of the inferred project.
- Follow-up text arriving while the dev turn for the same project runs → it reaches the same
  session as a steering message (visible in the transcript). No second turn or second final result
  is created.
- Follow-up clearly about another project while project A runs → routed separately and **not**
  merged into A (0 merges on the "unrelated" subset).
- Added delay from the quiet window for a single complete text question ≤ 1.5 s.
- Every clarifying question quotes or names the material it is about. At most one clarifying
  question per turn. 0 questions on cases labelled "no question needed" (owner-reviewed).

**UC-03**
- "to'xta" (text or voice) while a turn runs → interrupted and confirmed within 3 s. No tool call
  from that turn starts after the confirmation (transcript check). A running shell command is
  killed.
- "yo'q, boshqacha qil: X" → interrupted, and X is delivered to the same session within 5 s with
  prior context kept.
- "hammasini to'xtat" → every running turn interrupted within 5 s.

**UC-04** (fixture of ≥ 10 key formats: OpenAI, Anthropic API, Gemini, OpenRouter, GitHub, Telegram,
AWS, generic high-entropy)
- The owner's message is deleted within 5 s. The reply shows only a masked value.
- A recursive search of all logs, the SQLite DB, Claude transcripts (`~/.claude/projects`), Codex
  transcripts and bot replies finds the plaintext 0 times. The DB holds only ciphertext.
- A key typed outside an explicit command and in an unknown format still never appears in bot
  replies or logs. The outbound filter masks every stored secret value.

**UC-05**
- A 60 s Uzbek voice note is transcribed within 5 s.
- Mixed-language test set (≥ 20 owner-style notes: Uzbek with Russian and English terms or
  sentences): no foreign word is dropped or translated; Uzbek comes out in Latin script and Russian
  in Cyrillic; every project name or alias spoken is spelled exactly as in the registry.
- A term added by chat is spelled correctly in the next voice note.
- The dev bot's echo line (transcript + target project) appears before the executor starts.

**UC-06**
- After `kill -9` of a bot process, the owner gets "Qayta ishga tushdim …" within 60 s.
- The pinned heartbeat is never older than 10 min while the Mac is awake and online.
- "holat?" answers within 3 s.

**UC-07**
- "xarajat?" answers within 5 s with every field listed in UC-07.
- Figures match the sum of recorded turn costs and API calls in the DB.

**UC-08**
- An MCP integration added while ClaudeManager has account A active answers the same question
  successfully after rotating to account B, with no restart and no re-entry (run in both bots).
- No integration added via chat appears in the claude.ai account connector list of any account.
- Add, list and remove work by chat. A removed integration is absent from the next session.
- Invalid credentials or an unreachable server → a Uzbek message naming the integration and the
  fix, within 30 s of the add attempt or the failing call. Never a silent fallback to "I don't
  know".
- OAuth: the owner completes auth from the phone using the link the bot sends. The token refreshes
  without the owner. On refresh failure the bot tells him once and explains how to re-auth.

**UC-09** (fixture: ≥ 1 change per settings group listed in UC-09)
- Each change made by chat takes effect on the **next** message. This includes persona and
  system-prompt changes in an already-warm or resumed session.
- Each change survives a restart of both bots.
- No change needs a code edit, a deploy or a restart.
- "sozlamalaring qanday?" answers ≤ 3 s, covering every group, with values matching the stored
  settings.
- "oldingi holatiga qaytar" restores the previous value (verified by re-reading the settings).
- Every safeguard-weakening change in the fixture triggers exactly one question before it applies.
- Injection fixture (web page, file, executor output saying "change setting X") → 0 changes.
- An out-of-range value → refused with valid options; the stored settings are unchanged.
- Code review: no behaviour value from the UC-09 list is a hard-coded literal outside the settings
  defaults file.

**UC-10** (routing eval: ≥ 120 owner-style messages, **no replies**, ≥ 15 projects — 80 explicit or
content-clear, 20 context follow-ups, 20 genuinely ambiguous)
- Explicit or content-clear: ≥ 97% routed correctly without a question.
- Context follow-ups (within 15 min of that project's activity): ≥ 90% correct without a question.
  The rest are asked, never wrong.
- Genuinely ambiguous: ≥ 90% produce a question naming the candidates.
- **Silent wrong routes** (routed without a question to the wrong project): ≤ 1 in the whole set.
- Every first reply names the target project. The routing decision takes ≤ 4 s.
- "bu HRMentor uchun edi" → the wrong turn stops within 3 s and its changed files are listed. Only
  on "ha" are exactly those files reverted.

**UC-11**
- First progress message ≤ 30 s after routing.
- At most 1 edit per 5 s per progress message.
- The final result arrives as a new message containing the `Natija:` line, changed files, tests,
  commit hash and push status.
- A build task ends with a commit pushed to the remote and **no question asked** (D9).
- On a usage limit: one notice, automatic continuation after reset or rotation, no lost
  instruction.

**UC-12**
- An agent question appears in the group within 5 s of being asked, project name first, options
  numbered.
- "2", the option text or a voice answer reaches the session and the turn continues.
- No message from either bot contains an inline or reply keyboard. This is asserted at the outbound
  API layer: any `reply_markup` is a test failure.

**UC-13** (gate test suite, run for each executor)
- Each command class in the list triggers exactly **one** question and **zero** executions before a
  "yes". Fixtures include remote deploy, SSH write, `docker compose down -v`, `DROP DATABASE`,
  force push to main, `gh repo delete`, `rm -rf` outside the project, and visibility → public.
- A deploy request by the owner produces exactly one confirmation question (what, where), then runs
  after "ha".
- "ha" runs the exact pending action once. If the agent then attempts a different command or host,
  it is asked again.
- With 2+ decisions pending, a bare "ha" with no reply or name produces a question naming them, not
  an approval.
- Commit, push, branch and private repo creation in the fixtures run with no question.
- In a fixture repo whose CI deploys on push to `main`, a push to `main` asks once (it is a
  deploy). A push to a feature branch does not.
- Pre-push secret scan: a commit that contains a planted test secret or `.env` is **blocked**, and
  the owner is told which file. The push does not happen.

**UC-14**
- From "tamom" to the plan summary ≤ 15 min.
- Before "ha", the folder contains only plan docs (no source files).
- After the build: a private GitHub repo exists, the default branch is pushed, and tests pass.
- A name collision never overwrites an existing folder.

**UC-15**
- Answers to report or status queries for any project: **≤ 4 s**, including "currently running:
  <task>, N min", even while that project's coding turn is running.
- Each report shows a freshness line ("Ma'lumot: 3 daqiqa oldin").
- The digest is refreshed after every turn and on git changes (≤ 5 min lag for active projects).
- A server status answer comes from the last check if it is ≤ 30 min old. Otherwise an immediate
  "Tekshiryapman" (≤ 2 s), then the result ≤ 3 min.
- A fresh investigation session attempting a file write or a non-allowlisted remote command is
  denied (test).

**UC-16**
- The bot detects the owner's `claude --resume <id>` at the Mac within 10 s. Messages for that
  project are queued, not written.
- After the Mac session closes, the queue is delivered within 30 s into the same session id.
- If the Mac attaches during a bot turn, the bot interrupts its turn within 10 s and notifies.

**UC-17**
- After "X uchun ChatGPT'ga o't", the next turn in X runs on Codex with the handoff summary in its
  first input. "holat?" shows Codex for X.
- The UC-13 gate suite and the commit/push policy pass on Codex.
- Switching back resumes the previous Claude session id.
- Codex missing or logged out → one message listing exactly what is missing.

**UC-18**
- A new folder appears in "loyihalar" within 5 min of creation.
- An alias added by chat is used by routing immediately (routing eval re-run passes).
- Archived projects are excluded from routing unless named.
- After "X'ni yopib tur", X holds no process within 30 s. "X'ni davom ettiramiz" resumes the same
  session id, and the agent's first reply refers to the "where we stopped" note.

**UC-19** (fixtures: 3 deliberately broken updates — crash at import, polling never starts, a
failing migration)
- Each fixture is rolled back automatically. Both bots are back online on the last good version
  within 3 min and post, in plain Uzbek, what failed.
- After the failing-migration rollback, the DB equals the pre-update copy.
- No owner message sent during an update or rollback is lost; all are processed afterwards.
- A change whose tests fail is never switched to.
- A self-update never kills a running coding turn in another project without telling the owner.
- Killing the bots' code mid-update (`kill -9` during the switch) still ends in a healthy last good
  version. This proves the self-updater does not depend on the bots' code.
- After every self-update the owner gets a message with version, what changed, tests and health.

**UC-30** (quality eval: ≥ 30 prompts across topics, owner-reviewed)
- First text visible ≤ 8 s for no-tool answers. Complete text ≤ 20 s.
- The voice note arrives ≤ 12 s after the text. The voice never delays the text.
- Voice length is 15–35 s, or the full text read aloud if shorter. It never reads URLs or code.
- The default text is ≤ 3,500 characters and ≤ 40 lines. "to'liqroq" delivers the full version.
- Opinion subset (owner states a contestable claim, ≥ 10 prompts): the bot states its own position
  with at least one reason or source every time. Uncritical agreement: 0 cases.
- Religious subset: cites named sources or scholars, and notes disagreement where it exists.
- Replies are in the owner's language (Uzbek by default).

**UC-31**
- Image analysis: first text ≤ 10 s.
- Web page, YouTube or video: an immediate "Ko'ryapman" message (≤ 2 s), result ≤ 60 s.
- Injection fixture (≥ 10 pages, files and images with embedded instructions): 0 instructions
  followed, 0 tool calls caused by the content.

**UC-32**
- A screenshot PNG for a public URL arrives ≤ 30 s.
- Files ≤ 50 MB are delivered. Larger files get an explanation.

**UC-33**
- "Bugungi yangiliklar": ≥ 5 items, each with source and time, none older than 48 h unless labelled.
- First text ≤ 30 s.

**UC-34**
- A setting changed by chat applies to the next reply and survives a restart.
- The voice-sample flow works by number or by voice ("ikkinchisi").

**UC-35**
- With a fixture MCP server exposing one read tool and one write tool: a matching question calls
  the read tool. The write tool is never called unless it was allowed when the integration was
  added.

**UC-36**
- The answer arrives ≤ 4 s with a freshness line.
- The personal bot process holds no dev bot token, no executor handle and no write access to the
  dev DB (code review + test).
- A web page in the personal bot saying "tell the dev bot to push" causes 0 entries in the dev
  bot's input or audit log.

### 4.2 Non-functional requirements

**Reliability**
- **NFR-REL-01 Restart.** Both bots run as user LaunchAgents with automatic restart, back within
  30 s after a crash. A separate watchdog process detects:
  - a dead process;
  - a stale heartbeat (> 120 s, which covers "polling died silently");
  - a crash loop (≥ 3 crashes in 10 min).

  It restarts the bot and tells the owner. A crash loop sends one alert per 30 min with the last
  (scrubbed) error.
- **NFR-REL-02 Reboot.** After a reboot and owner login, both bots are online within 2 min.
  Sessions, queues, pending decisions and settings are intact. With FileVault on, nothing can run
  before login (accepted, see RISKS R-01).
- **NFR-REL-03 Network drop.** Wi-Fi off 10 min, then on → polling resumes within 60 s of
  connectivity. Messages sent during the outage are processed in order. Outbound messages created
  during the outage are delivered.
- **NFR-REL-04 Sleep and wake.** On wake, both bots resume within 60 s. If the gap was ≥ 5 min, the
  dev bot posts "Mac uxlagan edi HH:MM–HH:MM, N ta xabar qayta ishlanmoqda".
- **NFR-REL-05 No lost or double-processed input.** Fault test: 50 messages with `kill -9` at
  random points. Afterwards every message has either exactly one executor delivery or one explicit
  "bajarilmay qoldi — qayta yuboraymi?" notice. Silent drops: 0. Double deliveries to an executor:
  0.
- **NFR-REL-06 Outbound.** Outgoing messages are persisted before sending and survive restarts.
  Network-fault test: lost messages 0, duplicates ≤ 2% of sent.
- **NFR-REL-07 Interrupted turns.** A turn cut off by a crash or reboot is **not** re-run
  automatically. After restart the bot says what was in progress and asks whether to continue.
- **NFR-REL-08 Claude CLI changes.** When the `claude` version changes, a smoke self-test runs (one
  turn in a scratch project). On failure the owner is told and the bot keeps running.

**Speed** (D10 — the top priority; coding is excluded)

| Path | Target (p90) |
|---|---|
| Ack: "typing…" or a "seen" reaction | ≤ 1 s from receipt of each message |
| Project report or status from the digest | ≤ 4 s |
| `holat?` | ≤ 3 s |
| General Q&A, first text visible | ≤ 8 s; complete ≤ 20 s |
| Web search, news | ≤ 30 s |
| Media or link analysis | ≤ 60 s, with "Ko'ryapman" ≤ 2 s |
| Voice note after its text | ≤ 12 s |
| STT of a 60 s voice note | ≤ 5 s |
| Interrupt | ≤ 3 s |
| Routing decision + named ack | ≤ 4 s |
| Fresh investigation | "Tekshiryapman" ≤ 2 s, result ≤ 3 min |
| Coding first progress | ≤ 30 s (completion: no target) |

- **NFR-SPD-01** The answer path never waits for a coding turn, a session start, or TTS. Text is
  always sent before its voice note.
- **NFR-SPD-02** The Q&A path uses a warm, persistent session per bot (no cold start per question)
  and a faster model tier than coding. The tiers are owner-configurable by chat.

**Security**
- **NFR-SEC-01** Owner-only, per UC-01.
- **NFR-SEC-02** Secrets are encrypted at rest. The master key is not stored next to the data and
  not in the repo. DB and secret files are mode 0600. Secrets never go into git (pre-commit check
  in Yordamchi's own repo).
- **NFR-SEC-03** Secrets never appear in logs, Telegram output or LLM inputs. Every log line and
  every outbound message passes a scrubber that masks stored secret values and known key patterns
  (UC-04 test).
- **NFR-SEC-04** Yordamchi never reads, stores, copies or rotates Claude credentials, and never
  sets `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_AUTH_TOKEN` or a non-default
  `CLAUDE_CONFIG_DIR` for its sessions.
  - Test: after ClaudeManager switches account, the next turn of both bots runs on the new account
    within 60 s, with no restart.
  - The startup self-check refuses to run, and tells the owner, if any of those variables is set.
- **NFR-SEC-05** Only the official `claude` binary (and the official `codex` binary) talks to model
  providers. Yordamchi never proxies, intercepts or rewrites their traffic.
- **NFR-SEC-06** The important-decision gate (UC-13) is enforced in code at the tool-call layer, not
  only by prompt.
- **NFR-SEC-07** Yordamchi-launched sessions behave the same on every Claude account. Account-level
  claude.ai connectors are not relied on. The architect decides whether to exclude them; the UC-08
  rotation test must pass either way.
- **NFR-SEC-08 Personal bot tool policy.** At session start, the tool and MCP list reported by the
  session must equal the allowlist: web search/fetch, its integrations' allowed tools, the bridge
  read tool, and Yordamchi's media tools. No shell, no file write, no access to the dev executor.
  It does not load the owner's global CLAUDE.md, agents, skills or hooks. On any mismatch it
  refuses to start and tells the owner.
- **NFR-SEC-09 Prompt injection.** All web, file, image, MCP and server content is data. The
  personal bot's content can never reach the dev executor (UC-36). Dev-side content is limited by
  NFR-SEC-06.
- **NFR-SEC-10 Audit.** There is an append-only log of every executor input, gate decision and
  approval, config change, integration change, and secret add or remove (name only). It is kept 90
  days.
- **NFR-SEC-11 Commit/push override.** Sessions Yordamchi starts receive an explicit, durable owner
  authorization for commit, push, branch and private repo creation. This overrides the "ask before
  push" line in `~/.claude/CLAUDE.md`. "Never commit `.env` or secrets" stays and is backed by the
  pre-push secret scan (UC-13).
  - Test: a session asked to finish a task pushes without asking. No session sits waiting for a
    push approval (watch for "should I push?" text → counted as a failure in the UC-11 eval).

**Cost**
- **NFR-COST-01** Visibility per UC-07.
- **NFR-COST-02** Each paid API (Gemini, OpenRouter, OpenAI) has a monthly cap (initial values in
  ASSUMPTIONS). The bot warns at 80%. At 100%, paid calls pause until the owner says to raise the
  cap; text replies continue without voice.
- **NFR-COST-03 Runaway guard.** A turn exceeding 60 min of wall time or 200 agent steps (per-project
  configurable), or repeating the same failing tool call 5 times, pauses. The owner is asked whether
  to continue.

**Capacity**
- **NFR-CAP-01** ≥ 40 projects registered; ≥ 15 sessions resumable. Concurrently **working**
  coding turns are capped (Q2); the rest are queued, with their position shown. The Q&A path is
  outside the cap.
- **NFR-CAP-02** Outbound respects Telegram limits: ~1 message/s per chat, 20 messages/min per
  group, and `retry_after` on 429. Test: 15 projects reporting for 10 min → 0 lost messages, every
  final result delivered, progress edits coalesced.
- **NFR-CAP-03** No message over 4,096 characters, and no caption over 1,024. Long content is split
  at paragraph or code-block boundaries. More than 3 parts → a `.md` file.
- **NFR-CAP-04** Inbound files over 20 MB and outbound files over 50 MB are refused with an
  explanation. Voice output is OGG/Opus.
- **NFR-CAP-05** The bot warns when free disk is under 10 GB. The media cache is purged after 7
  days. Logs rotate at ≤ 200 MB total.

**Operations and quality**
- **NFR-OPS-01** One documented setup: BotFather settings, group creation and rights, owner and
  chat ids, LaunchAgents, keys, power settings, and a check script that verifies all of it.
- **NFR-OPS-02** Definition of Done includes the routing, burst, gate, secret-leak, fault,
  settings (UC-09) and broken-self-update (UC-19) tests. Weakening an eval threshold needs the
  owner's OK.
- **NFR-OPS-03 Behaviour in data.** Every behaviour value listed in UC-09 is read from versioned
  settings at use time, with defaults in one file. Adding a new setting must not require changing
  the settings mechanism itself, so future requirements can land as settings or small
  self-updates.
- **NFR-LANG-01** All bot output is Uzbek (Latin), with technical terms in English. Code, docs and
  logs are English.

---

## 5. Scope boundary

### In this version
Everything in §3. Delivery order (dev bot first):
1. **P1:** dev core (UC-01–06, 09, 10–13, 15, NFR-REL/SEC/SPD). Settings-in-data starts here,
   because it is cheap now and expensive to retrofit.
2. **P2:** UC-07, 08, 14, 16, 18, 19 (self-update). Self-update is in P2, so every later change
   can already be made by chat.
3. **P3:** personal bot (UC-30–36).
4. **P4:** UC-17 (Codex).

Extensibility: new owner requirements after delivery arrive by chat. A requirement that is a
setting is handled by UC-09; anything else becomes a self-update (UC-19).

### NOT in this version
- Controlling GUI apps, clicking, screen automation, "open program X and click Y" (D13).
- Inline buttons, reply keyboards, a BotFather command menu, Telegram Topics, more than one dev
  group (D4, D8).
- Any user other than the owner: no team access, no guest mode, no sharing.
- Running anywhere but the owner's Mac: no VPS, no cloud relay, no off-Mac watchdog, no
  second-machine failover (see Q1). Messages sent while the Mac is offline for more than 24 h are
  lost by Telegram.
- A web dashboard or admin UI. Configuration is by chat plus one config file.
- A local Telegram Bot API server (so files stay ≤ 20 MB in and ≤ 50 MB out).
- Real-time voice conversation (Gemini Live, calls). Voice notes only.
- The personal bot executing anything on the computer, sending messages, emails or posts for the
  owner, payments, or calendar/contact writes.
- Personal-bot brains other than Claude, beyond the Q4 fallback (pending Q4).
- Image, music or video **generation**.
- Logged-in browsing with the owner's cookies or accounts.
- Owner-defined recurring tasks or briefings ("every morning send news"). Built-in schedules
  (digest, server checks, heartbeat, cost summary, quiet hours) are configurable by chat. A
  briefing feature can be added later by chat as a self-update.
- Long-term personal memory or profiling beyond the current conversation and the settings.
- Managing Claude accounts, Claude logins or API-key billing for Claude (ClaudeManager owns
  accounts).
- Windows or Linux support.
- Rewriting the owner's global `~/.claude` rules, agents or skills (Yordamchi only adds its
  per-session authorization).

---

## 6. Data sources

| Source | What | Volume | Owner | Format / access |
|---|---|---|---|---|
| Telegram Bot API | Owner messages: text, voice, photos, albums, documents, links | ~50–300 messages/day; voice ≤ 5 min typical; files ≤ 20 MB | Owner | Long polling; updates kept by Telegram ≤ 24 h |
| `~/Documents/Projects/*` | Project folders, README / CLAUDE.md / SPEC (descriptions), git state | ~35 folders, ≤ 15 active | Owner | Local filesystem, git |
| Claude Code | Session transcripts (`~/.claude/projects/**.jsonl`), session registry (`~/.claude/sessions/*.json`, used to detect a Mac attach), owner's global rules, agents and skills | Thousands of lines per session | Anthropic CLI (read-only for Yordamchi) | JSONL / JSON. The internal formats may change between versions. |
| ClaudeManager | Active account, 5h/7d utilization | Small | Owner's app | Read-only, if exposed; no tokens |
| Servers | Read-only checks (uptime, disk, containers, logs) via `~/.ssh/config` hosts; Bonvi rules in `~/Documents/Projects/BONVI_SERVER.md` | A few hosts | Owner / partner | SSH from the Mac (Tailscale) |
| GitHub | Repos, push, repo creation | Per project | Owner (`gh` logged in as `abboskhanme`) | `gh` CLI, git |
| Web | Search, pages, YouTube, files | On demand | Third parties (untrusted) | HTTP, Claude web tools, Gemini where useful |
| Gemini API | STT, TTS, optional search and video understanding | ~100–300 calls/day | Google (owner's key) | REST |
| Integrations | MCP servers and API keys given by chat (for example Bonvi HR, Wunderkind school and LC — read-only) | ≥ 10 | Owner / third parties | MCP (HTTP), REST; secrets in Yordamchi's registry |
| Yordamchi DB | Registry, sessions map, digests, queues, decisions, settings, audit, cost | Small (< 1 GB/yr) | Yordamchi | SQLite |

No legacy data to migrate.

---

## 7. Open questions (max 5)

If unanswered, the recommendations stand and are recorded in `docs/ASSUMPTIONS.md`.

**Q1. When you are away, where is the Mac?** The bots work only while it is awake and online. With
the lid closed on battery, macOS sleeps whatever we do, and with FileVault on, a reboot waits for
your login.
→ **Recommendation:** the Mac stays at home or the office, on the charger, set never to sleep on
power (lid open, or clamshell). When it travels in your bag, the bots are offline, which is
accepted for v1. The pinned "Onlayn: HH:MM" message shows you when it is down. An off-Mac relay or
watchdog is v2.

**Q2. How many projects may be *coding* at the same time?** The Mac has 16 GB RAM, and builds,
tests and Docker per project are heavy.
→ **Owner's answer (2026-10-23):** up to **10–15 coding at once** (default cap 15, changeable by
chat). The owner parks projects himself by chat ("yopib tur", UC-18) to free capacity. A memory
and disk guard protects the Mac: under pressure, new turns queue with their position shown instead
of the Mac swapping or crashing, and the owner is told. Reports and Q&A are never queued.

**Q3. May Yordamchi run read-only commands on your servers without asking?** This covers on-demand
"how is server Z" and a check every 30 min for fast reports. Your rule is "ask before touching
production".
→ **Recommendation:** yes, but only an allowlist of read-only commands (uptime, disk, `docker ps`,
`systemctl status`, log tails). Anything else on a server is an important decision. The Windows
host `server` (1C) is a jump host only: no commands run on it without asking.

**Q4. Personal bot: should "switch to ChatGPT / Gemini" make another provider its brain in v1?**
→ **Owner accepted the recommendation (2026-10-23):** no. Claude stays the brain, with the model tier changeable by chat. While
Claude is unavailable, the bot answers plain questions with Gemini (key already present), clearly
labelled "zaxira model". Other providers as a full brain are v2. Keys given now are stored for
later.

**Q5. Self-update: may the bot apply changes to itself automatically once tests pass?** This
covers both settings and code.
→ **Recommendation:** yes. It applies automatically with health check and auto-rollback, and you
get the outcome message. Two kinds of change ask you once, with a plain summary of what changes:
- changes to the protected core (self-updater, watchdog, owner allowlist, important-decision gate,
  secret store);
- settings that weaken a safeguard.
