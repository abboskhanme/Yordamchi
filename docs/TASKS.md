# Tasks

Source: `docs/SPEC.md` (approved 2026-10-23). `plan-manager` · 2026-10-23. Paths without a prefix are under `src/yordamchi/`.

**Next action:** (1) Owner, today: X01–X04 and X07 (Telegram setup, second account, speech keys, 20 voice notes,
scratch repo). (2) Orchestrator: start spikes T03–T06 now (no deps), and T07 once T01 lands. Tell the T01 agent that its
done criterion includes the pre-carved shared-file blocks and every §4.3 P1 default (rule R2).

**Status:** done 29 (T01–T28 + T17b, T18b, ffmpeg pin) · in progress 4 (T29, T30, T32, T33 + security fixes) · blocked on owner 1 (T38) · todo: the rest.

**Critical path (P1):** T01 → T02 → T08 → T17 → T20 → T24 → {T29 T30 T31 T32} → T34 → T36 → T42 → T43 → T44 → T47.
Joins it: T03 → T10 → T24 (spikes gate the executor) and T07 → T18 → T24 (gate). Day-one owner items on it:
X01 (needed by T39, T43), X04 (T38 → T47).

---

## Rules for this plan

- **R1 Concurrency.** At most 5 parallel agents at a time, plus one sequential-lane agent. A later wave starts as slots
  free, in listed order. A task with no shared-file edits (marked `—` under Shared) may take any free slot early.
- **R2 Shared files.**
  - *Locked.* Only the named task edits these. Any later change is its own sequential task.
    `pyproject.toml`, `uv.lock` and the import-linter contracts with every P1 pin and every P1 C5 line: T01 (T02 if a
    test dep is needed). `tests/eval/thresholds.toml` with every P1 row, `.protected-core`, `migrations/env.py`,
    `tests/conftest.py`, `Makefile`: T01. CONVENTIONS §4 port table: T02. `bots/common.py`: T14.
    `bots/dev/{app,telegram,intents}.py`: T34. `migrations/versions/*`: merge train only.
    `docs/ASSUMPTIONS.md`: merge train only (agents put decisions in their hand-off).
  - *Block files:* `core/strings.py` (STR), `core/errors.py` (ERR), `shared/settings/defaults.py` (DEF),
    `tests/factories.py` (FAC). T01/T02 pre-carve one `# --- <feature> ---` block per P1 feature. T01 also seeds every
    §4.3 P1 key, so most tasks never touch DEF. A parallel task writes only inside its own block, in its own git
    worktree.
- **R3 Merge train (MT): a sequential lane run by the orchestrator. Only MT writes `main`.** Finished tasks merge one
  at a time, FIFO by completion. Each step:
  1. rebase;
  2. regenerate `0001`;
  3. `make check`;
  4. merge;
  5. append the spike or assumption lines.

  A red step goes back to the task.
- **R4 Baseline done (every task):**
  - merged through MT, `make check` green on `main`;
  - tests per CONVENTIONS §11: every domain function, every public service (happy path + one failure), adapter error
    mapping, port contract (fake + live);
  - every table ships its §4.2 retention function;
  - no test skipped or weakened.

  "Done when" lists only the extras.
- **R5 Leaves the machine / touches real state:** X07 (repo), T39 (live Telegram/GitHub), T43 (launchd on the Mac).
  The owner says go first.

## Migration plan

- **P1 ships exactly one revision: `migrations/versions/0001_initial.py`, P1 tables only.** It is generated, never
  hand-edited and never hand-merged.
- **Feature tasks** add `models.py` only. Unit tests use `create_all`. A task may regenerate `0001` in its worktree to
  get `make check` green. That copy is thrown away at MT.
- **MT step 2:**
  1. delete `0001`;
  2. `alembic revision --autogenerate --rev-id 0001 -m initial` against an empty scratch DB;
  3. read it: only `create_table`/`create_index`;
  4. `alembic upgrade head` on a scratch `dev.db` and `personal.db`;
  5. `alembic check`.

  On a conflict, regenerate; never resolve by hand. Parallel units never collide, because none of them commits a
  revision.
- **Why it is safe:** no real DB holds `0001` until T43. **T43 freezes it** (first real `upgrade head`). T40 saves
  `tests/migrations/fixtures/p1_release.db`, the "previous release" DB for P2 DoD.
- **P2+:** append-only `000N_<feature>_<change>.py`, expand-only (CONVENTIONS §8).
  - A task authors its revision in its worktree.
  - At MT it is re-pointed (`down_revision` = current head) and renumbered.
  - Two heads cannot pass, because `alembic upgrade head` fails on multiple heads.

## Gap assignments (SPEC §7 units omit these; assigned here)

| Gap | Assigned to |
|---|---|
| `shared/{audit,vault,cost(record)}` + `adapters/keystore` | T08 |
| `shared/settings` service | T21 |
| `adapters/git` | T17 |
| `adapters/{ssh,macos}` | T22 |
| Control socket + retention scheduler | T34 |
| S7 | T38 |
| S8 | P3 T71 |
| U8 split | T24 first, then T29–T32 |
| U9 split | T32 health ∥, T34 wiring, T35 golden |

Shared codes: STR ERR DEF FAC (block files) · MIG (`0001` regenerated at MT) · WIRE (`bots/dev/*`) · COMMON
(`bots/common.py`) · `—` = none.

---

## Phase 0 — Day one, external [owner; all start 2026-10-23, they wait in parallel with everything]

| ID | Item | Unblocks | Status |
|---|---|---|---|
| X01 | Telegram: create two bots, the dev private group, dev privacy off (or admin with delete + pin), join-groups off after adding, Telegram 2FA (SPEC §10.3) | T39, T43 | **DONE** 2026-10-24: dev `@OrvionXBot` (privacy off, join-groups off, admin with delete+pin in supergroup `OrvionX Projects` -1004347183120), personal `@OrvionX_AI_Bot`; OWNER_ID and DEV_GROUP_ID are in the 0600 env files. **Before T43:** rotate both tokens (they were pasted into a chat transcript); T43 takes the new ones through hidden input. Optional: personal bot join-groups off. |
| X02 | Second Telegram account for the stranger tests | T39 | **dropped**: the owner has none (2026-10-23). Stranger, leave and privacy are covered offline only (recording aiogram session + fakes, UC-01 offline AC); T39 omits the live stranger case |
| X03 | Gemini key **and** OpenRouter key (S7 compares both; one key = S7 falls back to its default). Given to `pytest -m live` as env vars, never committed; by chat after T43 | T38 | todo |
| X04 | Record ≥ 20 mixed-language voice notes (UC-05 set) | T38 | todo |
| X05 | `sudo pmset -c sleep 0`, charger, macOS auto-restart off | T43 | todo |
| X06 | List `servers.hosts` (`~/.ssh/config` aliases) + one host for the live SSH test | T31 live, T39 | todo |
| X07 | OK to create a throwaway private GitHub repo (live push test, NFR-SEC-11) | T39 | **done**: approved 2026-10-23 for `abboskhanme/yordamchi-smoke` (private, test commits only) |

## Phase 1 — Skeleton [sequential]

| ID | Task | Owner | Deps | Par | Creates | Shared | AC | Done when | Status |
|---|---|---|---|---|---|---|---|---|---|
| T01 | U1 core + persistence + settings registry + `make check` | build-backend | — | no | `core/{errors,strings,text,log,redact,aio,retry,proc,config,paths,money,timefmt}.py`, `persistence/{base,types,engine,uow,upsert}.py`, `shared/settings/{registry,defaults}.py`, `prompts/__init__.py`, `migrations/env.py`, `tests/architecture/test_rules.py`, `tests/conftest.py`, `config/*.env.example`, `.gitignore`, Makefile | creates PYP, THR, PROT, STR/ERR/DEF blocks | NFR-OPS-02/03, NFR-SEC-03 (§4.5 patterns), NFR-SEC-04 (refuse-list, `clean_env`), NFR-LANG-01, UC-09 (no literals) | every stage of `make check` runs green on the skeleton. C1–C8 and A1–A8 active. `test_registry` green over all §4.3 P1 keys. Blocks pre-carved | **DONE** 2026-10-24 (`make check` green, 469 + 4 tests) |
| T02 | U2 ports + fakes + factories | build-backend | T01 | no | `ports/*.py` (incl. `media.py`), `tests/fakes/*.py` (SPEC §8 list), `tests/factories.py`, `adapters/system_clock.py`, `{shared,dev,personal}/deps.py`, `tests/contract/test_<port>.py` (fake side) | FAC (create), PORTS | A4, UC-12 (`FakeMessenger` fails on `reply_markup`), UC-36 (`PersonalDeps` has no executor, git or SSH) | every port has a fake + contract skeleton; mypy strict | **DONE** 2026-10-24 (`make check` green, 469 + 4 tests) |

## Phase 2 — Spikes + pure gate [parallel, max 5; no shared files; runs alongside Phase 1]

Spikes run in a scratch uv env pinned to SDK 0.2.173 / CLI 2.1.309 and land in `tests/live/` via MT. Each one ends in
a kept live test and a decision line (pass, or the SPEC §11 fallback).

| ID | Task | Owner | Deps | Par | Creates | Shared | AC | Done when | Status |
|---|---|---|---|---|---|---|---|---|---|
| T03 | S1 `priority:"next"` merge + S2 `interrupt()` kills Bash ≤ 2 s | build-integration | — | yes | `tests/live/test_spike_s1_next.py`, `test_spike_s2_interrupt.py` | — | UC-02 (steer), UC-03 (shell killed) | decision recorded for T10/T24 | **DONE** 2026-10-24 (docs/SPIKES.md) |
| T04 | S3 subagent PreToolUse + `AskUserQuestion` reach `can_use_tool` under bypass | build-integration | — | yes | `tests/live/test_spike_s3_hooks.py` | — | NFR-SEC-06, UC-12 | decision recorded for T11/T29 | **DONE** 2026-10-24 (docs/SPIKES.md) |
| T05 | S4 `UserPromptSubmit` `additionalContext` on a resumed session | build-integration | — | yes | `tests/live/test_spike_s4_resume.py` | — | UC-09 (persona on warm/resumed) | decision recorded for T12/T24 | **DONE** 2026-10-24 (docs/SPIKES.md) |
| T06 | S5 `--restricted` with WebSearch/WebFetch/SDK MCP + S6 SDK sessions in `~/.claude/sessions/` | build-integration | — | yes | `tests/live/test_spike_s5_restricted.py`, `test_spike_s6_sessions.py` | — | NFR-SEC-08 (P3 input), UC-16 (P2 input) | decisions recorded for P3 T77 / P2 T54 | **DONE** 2026-10-24 (docs/SPIKES.md) |
| T07 | U5a gate domain: §5.7 classifier, approval binding, grants, READ_ONLY policy | build-backend | T01 | yes | `dev/gate/domain.py`, `tests/unit/dev/gate/test_domain.py` | — | UC-13, NFR-SEC-06, NFR-SEC-11 (commit/push/branch/private repo free), UC-15 (investigation write denied) | every class and exemption has a case; pure (C6, A1) | **DONE** 2026-10-24 (MT 792367b; 575 gate cases) |

## Phase 3 — Foundations [sequential; the reference feature]

| ID | Task | Owner | Deps | Par | Creates | Shared | AC | Done when | Status |
|---|---|---|---|---|---|---|---|---|---|
| T08 | `shared/inbox` **reference implementation** (built first), then `shared/audit`, `shared/vault` + `adapters/keystore/file.py`, `shared/cost` (record only) | build-backend | T02 | no | `shared/{inbox,audit,vault,cost}/{domain,service,store,models}.py`, `adapters/keystore/file.py` | STR ERR DEF FAC MIG | UC-04 (extract before persist, delete ≤ 5 s, masked reply, ciphertext only, `sk-ant-oat` refused), NFR-SEC-02, NFR-SEC-03 (masking set, `secret-hashes.json`), NFR-SEC-10, NFR-REL-05 (inbound idempotency), SPD ack ≤ 1 s | inbox shape signed off by qa-review as the copy-from template **before Phase 5 starts** | **DONE** 2026-10-24 (MT f99717a, 750 tests) |

## Phase 4 — Adapters [parallel, max 5; no shared files; runs alongside Phase 3]

| ID | Task | Owner | Deps | Par | Creates | Shared | AC | Done when | Status |
|---|---|---|---|---|---|---|---|---|---|
| T09 | U3a Telegram adapter (`TelegramMessenger`) | build-integration | T02 | yes | `adapters/telegram/*.py`, `tests/integration/test_telegram.py`, `tests/contract/test_messenger.py`, `tests/fixtures/telegram/` (albums, voice, `my_chat_member`) | — | UC-12 (any `reply_markup` fails the recording session), NFR-CAP-02 (`retry_after`), NFR-CAP-04 (20 MB in, 50 MB out) | contract green on fake + recorded payloads | **DONE** 2026-10-24 (MT 717834f, `make check` green, 527 tests) |
| T10 | U4a Claude executor (`ClaudeAgentRuntime`, §5.1/§5.3) | build-integration | T02, T03 | yes | `adapters/claude/executor.py`, `tests/integration/test_claude_executor.py`, `tests/contract/test_agent_runtime.py`, `tests/fixtures/claude/` | — | UC-03 (interrupt + pgkill per S2), UC-02 (steer per S1), UC-11 (`UsageLimited`), NFR-SEC-04/05/07 (`cli_path`, clean env, connectors off), NFR-REL-08 (probe) | all §5.1 events mapped from recorded streams; micro-USD | **DONE** 2026-10-24 (MT, 976 tests; live 10/10) |
| T11 | U4c guard hook bridge (PreToolUse + `can_use_tool` → `SessionSpec.guard`) | build-integration | T02, T04 | yes | `adapters/claude/guard.py`, `tests/integration/test_claude_guard.py` | — | NFR-SEC-06, UC-13 (error/timeout → deny), UC-12 (question capture per S3), UC-03 (deny-all while stopping) | fail-closed tests incl. `gate.hook_timeout_s` | **DONE** 2026-10-24 (MT, 976 tests; live 10/10) |
| T12 | U4b assistant pool (warm `routing`/`report`/`settings` clients + hot spare, rotation) | build-integration | T02, T05 | yes | `adapters/claude/assistant.py`, `tests/integration/test_claude_assistant.py`, `tests/contract/test_assistant.py` | — | NFR-SPD-02, UC-09 (prompt version → next ask per S4), UC-10/UC-15 timeouts | rotation after 20 asks / settings version | **DONE** 2026-10-24 (MT; live warm routing p90 1.56 s, report p90 1.45 s) |
| T13 | U6c STT adapters (Gemini direct, OpenRouter chat path) | build-integration | T02 | yes | `adapters/gemini/stt.py`, `adapters/openrouter/stt.py`, `tests/integration/test_{gemini,openrouter}.py`, `tests/contract/test_transcriber.py`, `tests/fixtures/{gemini,openrouter}/` | — | UC-05 (hints, vocabulary, parallel segments), §3.2.11 key validation (429/402/timeout mapping) | both pass the same contract | **DONE** 2026-10-24 (MT, 617 tests green) |

## Phase 5 — Features A [parallel, max 5; block files only, via MT]

| ID | Task | Owner | Deps | Par | Creates | Shared | AC | Done when | Status |
|---|---|---|---|---|---|---|---|---|---|
| T14 | U3b `shared/access` + `bots/common.py` (OwnerOnly, `guarded()`, polling runner) | build-backend | T08 | yes | `shared/access/*`, `bots/common.py` | STR ERR FAC COMMON | UC-01 (drop, leave ≤ 10 s, owner-elsewhere drop), NFR-SEC-01, NFR-REL-03 (1→30 s backoff), A5 (`OwnerOrigin`) | `feed_update` test: stranger → 0 requests, 0 inbox rows | **DONE** 2026-10-24 (MT; owner-only feed_update suite, 1200 tests) |
| T15 | U3c `shared/outbox` (durable queue, split/escape/`.md`, coalescing, rate limits) | build-backend | T08 | yes | `shared/outbox/*` (`outbox_messages`, `message_links`) | STR ERR DEF FAC MIG | NFR-REL-06, NFR-CAP-02/03, UC-11 (≤ 1 edit / 5 s), UC-12, NFR-SEC-03 (scrub before send) | split/coalesce domain cases | **DONE** 2026-10-24 (MT 72db73f; 2100+ tests) |
| T16 | U6a `shared/burst` (§4.4 window, album, voice HOLD, "kut", hard cap, follow-up merge decision) | build-backend | T08 | yes | `shared/burst/*` (`bursts`) | STR ERR FAC MIG | UC-02 (dev cases), UC-03 (interrupt word closes, never merged) | FakeClock virtual-time tests | **DONE** 2026-10-24 (MT 718b4c9; 32 burst eval cases seeded) |
| T17 | U7a `dev/projects` discovery, aliases, deploy-branch detection + `adapters/git` | build-backend | T08 | yes | `dev/projects/*` (`projects`), `adapters/git/*.py`, `tests/integration/test_git.py`, `tests/fixtures/workflows/` (incl. `on:` → `True`) | STR ERR FAC MIG | UC-13 (CI deploy; unparseable → every branch deploys), UC-10 prereq (registry, aliases, archived), NFR-CAP-01 (≥ 40 projects) | real git on `tmp_path` | **DONE** 2026-10-24 (MT e0af49f; follow-up T17b in PENDING_WIRING) |
| T18 | U5b gate service (decisions, grants, approvals) + shims + githooks | build-backend | T07, T08 | yes | `dev/gate/{service,store,models}.py` (`decisions`), `shims/{ssh,scp,sftp,rsync}`, `githooks/pre-push`, `bots/cli/{shim,githook}.py` | STR ERR DEF FAC MIG | UC-13 (one question, 0 runs before "ha", exact-action binding, single-use grant, pre-push secret block names the file), NFR-SEC-06, NFR-SEC-10, NFR-SEC-02 | shims/hook fail closed without the socket | **DONE** 2026-10-24 (MT 5ae2f6e; shims/githooks protected) |

## Phase 6 — Features B [parallel, max 5]

| ID | Task | Owner | Deps | Par | Creates | Shared | AC | Done when | Status |
|---|---|---|---|---|---|---|---|---|---|
| T19 | U6b `shared/voice` STT service + §3.2.11 key detection/provider choice | build-backend | T08, T13, T16 | yes | `shared/voice/*` (`transcripts`) | STR ERR DEF FAC MIG | UC-05 (5 s, mixed language, chat-added term next note), §3.2.11 | burst HOLD ↔ transcript wired with fakes | **DONE** 2026-10-24 (MT 5c01361) |
| T20 | U7b `dev/routing` (§3.2.6 signals, LLM route, clarification, wrong target) | build-backend | T08, T17 | yes | `dev/routing/*` (`route_decisions`, `clarifications`), `prompts/dev/routing_system.md` | STR ERR DEF FAC MIG | UC-10 (one question naming candidates, first reply names project, ≤ 4 s, wrong-target), UC-02 (unrelated not merged) | replay fixtures for LLM path | **DONE** 2026-10-24 (MT; incl. T17b; 48 routing cases seeded) |
| T21 | `shared/settings` service (change/revert/list, weakening, NL interpretation) | build-backend | T14, T18 | yes | `shared/settings/{domain,service,store,models}.py` (`setting_versions`), `prompts/dev/settings_system.md` | STR ERR DEF FAC MIG | UC-09 (P1 groups: next message, survives restart, list ≤ 3 s, revert, weakening → one decision, out-of-range refused, `OwnerOrigin` required, PERSONAL key on dev refused), NFR-OPS-03 | every P1 group has a service test | **DONE** 2026-10-24 (MT c7091cd) |
| T22 | `adapters/ssh` + `adapters/macos` (pressure, disk, ClaudeManager `state.json`, process scan per S6) | build-backend | T02 | yes | `adapters/ssh/*.py`, `adapters/macos/host.py`, `tests/integration/test_{ssh,macos}.py` | — | UC-15 (read-only checks), NFR-CAP-01/05, UC-06 (ClaudeManager in `holat?`) | contract green on fixtures | **DONE** 2026-10-24 (MT, 873 tests) |
| T23 | U10a supervisor: watchdog, `caffeinate`, backups, log trim | ship-devops | T01 | yes | `supervisor/` (own `pyproject.toml`, `dependencies=[]`, own `strings.py`, `tests/`) | — | NFR-REL-01 (dead, stale > 120 s, crash loop ≥ 3/10 min, 1 alert/30 min), NFR-REL-02, NFR-CAP-05, §9.2 backups + `integrity_check` | `unittest` green under pinned 3.12.15; A6 | **DONE** 2026-10-24 (MT, supervisor 86 tests) |

## Phase 7 — Turns + evals [parallel, max 5]

| ID | Task | Owner | Deps | Par | Creates | Shared | AC | Done when | Status |
|---|---|---|---|---|---|---|---|---|---|
| T24 | U8a `dev/turns`: sessions, scheduler (cap/pressure/queue), deliveries, progress, result, runaway, usage-limit wait (no park) | build-backend | T09–T12, T15, T16, T18, T20 | yes | `dev/turns/*` (`agent_sessions`, `turns`, `deliveries`, `turn_progress`), `prompts/dev/{executor_system,turn_context}.md` | STR ERR DEF FAC MIG | UC-11 (all), UC-03 (stop ≤ 3 s, redirect ≤ 5 s, stop all ≤ 5 s), UC-02 (steer), UC-10 (wrong-target stop, files, revert on "ha"), NFR-COST-03, NFR-CAP-01, NFR-REL-05/07, NFR-SEC-11, UC-09 (`prompt_version_seen`) | FakeAgentRuntime scripts cover every turn state | **DONE** 2026-10-24 (MT 02a4404; 122 turn tests) |
| T25 | U5c gate eval suite (Claude) incl. CI-deploy + secret-scan fixtures | test-backend | T07, T11, T18 | yes | `tests/eval/gate/`, `tests/fixtures/claude/tool_calls/` | — | UC-13 (all bullets), NFR-SEC-06, NFR-OPS-02 | meets `thresholds.toml` | **DONE** 2026-10-24 (298 cases; 9 gate bugs found and fixed; PEM-in-source and guard-cwd fixed in acff334, 0 xfails) |
| T26 | U7c routing eval ≥ 120 (80/20/20, ≥ 15 projects) | test-backend | T12, T20 | yes | `tests/eval/routing/`, `tests/fixtures/llm/routing/` | — | UC-10 (≥ 97 % / ≥ 90 % / ≥ 90 %, ≤ 1 silent wrong) | meets thresholds; fixtures recorded live | **DONE** 2026-10-24 (170 live cases; clear 97.4 %, ctx 96.2 %, amb 96.6 %, silent wrong 1 — margins thin) |
| T27 | burst dev eval (≥ 24) + secrets eval (≥ 10 formats + unknown) | test-backend | T08, T16 | yes | `tests/eval/{burst,secrets}/` | — | UC-02 (dev), UC-04 | meets thresholds | **DONE** 2026-10-24 (78 burst + 27 secrets cases; found and fixed redact B1/B2) |
| T28 | U10b installer, plists, check script | ship-devops | T01, T23 | yes | `scripts/install.py`, `ops/launchd/*.plist.tmpl`, `bots/cli/check.py` (core-only imports) | — | NFR-OPS-01, UC-01 (join-groups off, privacy/admin rights, empty commands), NFR-SEC-02 (modes, `master.key`, `tmutil`), NFR-SEC-04, NFR-REL-02 | dry run against a temp HOME passes (no `launchctl`) | **DONE** 2026-10-24 (MT df6fd8c; ffmpeg pin missing, see PENDING_WIRING) |

## Phase 8 — Turn satellites [parallel, max 5]

| ID | Task | Owner | Deps | Par | Creates | Shared | AC | Done when | Status |
|---|---|---|---|---|---|---|---|---|---|
| T29 | U8b `dev/questions` | build-backend | T11, T24 | yes | `dev/questions/*` (`questions`) | STR ERR FAC MIG | UC-12 (≤ 5 s, project first, numbered, "2"/text/voice answer) | `answer()` False → message path | in progress (2026-10-24) |
| T30 | U8c `dev/digest` + report answers + handoff note | build-backend | T12, T17, T24 | yes | `dev/digest/*` (`digests`), `prompts/dev/report_system.md` | STR ERR DEF FAC MIG | UC-15 (≤ 4 s incl. "running X, N min", freshness line, refresh ≤ 5 min) | report path never waits on a turn | in progress (2026-10-24) |
| T31 | U8d `dev/servers` (30-min checks, investigation) | build-backend | T22, T24 | yes | `dev/servers/*` (`server_checks`), `prompts/dev/investigate_system.md` | STR ERR DEF FAC MIG | UC-15 (≤ 30 min cache, "Tekshiryapman" ≤ 2 s, ≤ 3 min, write denied) | FakeRemoteShell suite | todo |
| T32 | U9a `shared/health` (`holat?`, heartbeat file + pin, wake gap, self-checks, CLI smoke, disk guard, media purge) | build-backend | T10, T15, T22, T24 | yes | `shared/health/*` (`health_events`, `kv_state`) | STR ERR DEF FAC MIG | UC-06, NFR-REL-04, NFR-REL-08, NFR-CAP-05, §2.8 | `holat?` ≤ 3 s on fakes | in progress (2026-10-24) |
| T33 | settings (≥ 1 per P1 group) + injection eval | test-backend | T21, T24 | yes | `tests/eval/{settings,injection}/` | — | UC-09 fixture (weakening → 1 question, injection → 0 changes), NFR-SEC-09 | meets thresholds | in progress (2026-10-24; executor-output part waits for T24) |

## Phase 9 — Wiring [sequential]

| ID | Task | Owner | Deps | Par | Creates | Shared | AC | Done when | Status |
|---|---|---|---|---|---|---|---|---|---|
| T34 | U9b `bots/dev` composition root, routers, intents (§6.1 order), control socket (`gate_check`, `secret_blocked`, `status`), startup §2.5, shutdown §2.6, recovery §2.7, scheduler + retention; P2 intents → `S.NOT_YET(phase)` | build-backend | T09–T24, T29–T32 | no | `bots/dev/{app,telegram,intents}.py` | WIRE STR ERR DEF MIG (final) | UC-01 (end to end), UC-06 (restart notice ≤ 60 s), NFR-REL-02/05/07, NFR-SEC-04 (exit 78), NFR-SPD-01, A7 | scripted conversation passes on fakes | todo |
| T35 | U9c golden tests + catalog completeness | test-backend | T34 | no | `tests/golden/{progress,result,holat,settings_list}.txt` + tests | STR (fixes) | UC-11 format, UC-06, UC-09 list, NFR-LANG-01, `test_error_catalog` | goldens reviewed | todo |

## Phase 10 — System tests [parallel, max 5]

| ID | Task | Owner | Deps | Par | Creates | Shared | AC | Done when | Status |
|---|---|---|---|---|---|---|---|---|---|
| T36 | Fault suite: 50 × `kill -9`, outbound network fault, sleep/wake, restart notice | test-backend | T34 | yes | `tests/fault/` | — | NFR-REL-03/04/05/06/07, UC-06 | 0 silent drops, 0 double deliveries, dup ≤ 2 % | todo |
| T37 | Load: 15 projects × 10 min | test-backend | T34 | yes | `tests/fault/test_load.py` | — | NFR-CAP-01/02 | 0 lost, every result delivered | todo |
| T38 | STT mixed-language live eval + S7 | test-backend | T13, T19, X03, X04 | yes | `tests/eval/stt/`, `tests/live/test_spike_s7.py` | — | UC-05 (each configured provider) | meets thresholds; S7 decision | **BLOCKED** (X03, X04) |
| T39 | Live e2e P1: stranger/leave/privacy, push without asking, account switch, CLI smoke, live interrupt | test-e2e | T34, X01, X02, X07 | yes | `tests/live/` | — | UC-01, NFR-SEC-11, NFR-SEC-04 (next turn on new account ≤ 60 s), NFR-REL-08, UC-03 | all green on the test bot | **BLOCKED** (X01, X02, X07) |
| T40 | Migration tests: `0001` on empty `dev.db` + `personal.db`, `alembic check`, one head; save `p1_release.db` | test-migration | T34 | yes | `tests/migrations/`, `tests/migrations/fixtures/p1_release.db` | — | SPEC §12 alembic rows | green | todo |

## Phase 11 — QA [parallel, 2]

| ID | Task | Owner | Deps | Par | Creates | Shared | AC | Done when | Status |
|---|---|---|---|---|---|---|---|---|---|
| T41 | Code review vs CONVENTIONS (layers, no literals, A/C rules) | qa-review | T35 | yes | — (fixes via MT) | — | UC-09 (review clause), CONVENTIONS §13 | no open findings | todo |
| T42 | Security review: leak search (logs, both DBs, `~/.claude/projects`, replies) = 0, `reply_markup` grep = 0, owner-only per endpoint-equivalent | qa-security | T35, T36 | yes | — (fixes via MT) | — | NFR-SEC-01…07, 09…11, UC-04 | no open findings | todo |

## Phase 12 — Release P1 [sequential]

| ID | Task | Owner | Deps | Par | Creates | Shared | AC | Done when | Status |
|---|---|---|---|---|---|---|---|---|---|
| T43 | Install on the Mac via `scripts/install.py` (owner types tokens; owner go-ahead) — **freezes `0001`** | ship-devops | T28, T40, T41, T42, X01, X05 | no | DATA tree, releases, plists | — | NFR-OPS-01, SPEC §12 install row | `python -m yordamchi.check` all green from launchd | **BLOCKED** (X01, X05) |
| T44 | Live latency p90 report (≥ 20 samples per row) | test-backend | T43 | no | `tests/live/latency.py` | — | SPD dev rows: ack 1 s, routing 4 s, report 4 s, `holat?` 3 s, interrupt 3 s, STT 5 s, first progress 30 s | every row meets p90 | todo |
| T45 | Owner grades UC-02 "no question needed" subset | owner | T27, T43 | no | — | — | UC-02 | graded pass | todo |
| T46 | `docs/QOLLANMA.md` (Uzbek) for P1 | ship-docs | T34 | yes | `docs/QOLLANMA.md` | — | SPEC §12 docs row | covers every P1 intent | todo |
| T47 | P1 DoD gate (`/check-done`, SPEC §12) | orchestrator | T36–T46 | no | — | — | P1 exit list (SPEC §7) | every §12 box machine-confirmed | todo |

---

## P2 — Self-running (coarse; new revisions `0002+` per the migration plan)

- [ ] T50 `shared/integrations` + `adapters/mcp` + `bin/yordamchi-helper` + socket `headers` + OAuth paste-back — build-integration — deps: T47 — UC-08 dev half (rotation A→B)
- [ ] T51 cost report + caps 80 %/100 % (`xarajat?`) — build-backend — deps: T47 — UC-07, NFR-COST-01/02
- [ ] T52 `adapters/github` — build-integration — deps: T47
- [ ] T53 `dev/newproject` (draft → plan → "ha" → build) — build-backend — deps: T52 — UC-14
- [ ] T54 `dev/handoff` (Mac attach, S6 decision) — build-backend — deps: T47, T06 — UC-16
- [ ] T55 park/resume + projects chat ops — build-backend — deps: T47 — UC-18
- [ ] T56 `dev/selfupdate` (branch work → `updates/<id>/request.json`) — build-backend — deps: T47 — UC-19
- [ ] T57 supervisor updater (build, test, drain, switch, probation, rollback, self-switch) + socket `selftest` — ship-devops — deps: T47 — UC-19
- [ ] T58 P2 wiring [sequential]: intents replace `S.NOT_YET`, `app.py` — build-backend — deps: T50–T57
- [ ] T59 P2 evals: 3 broken updates, `kill -9` mid-switch, DB equality, 0 lost; OAuth fixture server; attach/detach; park → no process ≤ 30 s; live rotation — test-backend + test-e2e — deps: T58
- [ ] T60 test-migration on `p1_release.db`, qa-security, install, QOLLANMA, DoD — deps: T59

Waves: {T50 T51 T52 T54 T55} → {T53 T56 T57} → T58 → T59 → T60.

## P3 — Personal bot (coarse)

- [ ] T70 `shared/bridge` (ro engine) + C7 isolation test — build-backend — deps: T47 — UC-36
- [ ] T71 TTS adapters (Gemini, OpenRouter, Edge) + voice TTS service + voice offers + S8 — build-integration — deps: T47 — UC-30 voice, UC-34
- [ ] T72 `personal/qa` + Gemini/OpenRouter assistant ("zaxira model") — build-backend — deps: T47 — UC-30
- [ ] T73 `personal/media` + `MediaAnalyzer` video adapters — build-integration — deps: T47 — UC-31
- [ ] T74 `personal/artefacts` + `adapters/web` (Playwright, download, yt-dlp) — build-integration — deps: T47 — UC-32
- [ ] T75 `personal/news` — build-backend — deps: T72 — UC-33
- [ ] T76 gateway `yordamchi_int` + personal integrations half — build-integration — deps: T50, T70 — UC-35, UC-08 personal
- [ ] T77 `bots/personal` wiring + tool-list equality refusal (S5 decision) [sequential] — build-backend — deps: T70–T76 — NFR-SEC-08
- [ ] T78 P3 evals: UC-30 quality ≥ 30 (owner-reviewed), injection ≥ 10, burst full ≥ 40 both bots, bridge isolation, fixture MCP — test-backend — deps: T77
- [ ] T79 P3 latency, qa-security, install personal plist, QOLLANMA, DoD — deps: T78

Waves: {T70 T71 T72 T73 T74} → {T75 T76} → T77 → T78 → T79.

## P4 — Codex (coarse)

- [ ] T90 `adapters/codex` (app-server runtime + guard) — build-integration — deps: T79, X08 — UC-17
- [ ] T91 `switch_executor` intent + handoff summary + switch back to the Claude session — build-backend — deps: T90 — UC-17
- [ ] T92 gate suite + commit/push policy on Codex — test-backend — deps: T90 — UC-13 on Codex
- [ ] T93 wiring, install, QOLLANMA, DoD — deps: T91, T92

## Blocked

- T38 — waiting on X03 (Gemini + OpenRouter keys) and X04 (20 voice notes); asked 2026-10-23
- T39 — X01 done; X02 dropped, X07 approved (2026-10-24); waits only on T34
- T43 — X01 done; waiting on X05 (`pmset`), the token rotation and the ffmpeg pin
- X08 (P4) — ChatGPT plan + `codex login` (money: owner decision). Ask at P3 start, so it does not gate T90
