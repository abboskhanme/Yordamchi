# Yordamchi — Conventions

**Binding from the first line of code.** `plan-conventions` · 2026-10-23 · Inputs: `REQUIREMENTS.md`, `RISKS.md`,
`ASSUMPTIONS.md`; stack fixed by the owner (`STACK.md`). Sibling precedents are cited where they are reused.

Every rule names its check: **ruff** · **mypy** `--strict` · **C1–C8** (import-linter in `pyproject.toml`) · **A1–A8**
(`tests/architecture/test_rules.py`, ast) · **test:** (suite) · **review**. Python 3.12, line length 120. `make check` =
ruff check + format check + mypy + `lint-imports` + pytest + supervisor tests + `alembic upgrade head` on a scratch DB.

## 0. How much structure — the call
Not CRUD: two bots and a supervisor share one core, ten external systems are faked for the acceptance evals, and the
gate, routing, burst and settings logic are real rules.

| Force | Structure | What breaks without it |
|---|---|---|
| Gate, routing, burst, approvals, settings, splitting have eval targets | pure `domain.py` per feature | the 120-case routing set and the gate suite need a clock, a DB and a model |
| Telegram, Claude, Codex, Gemini, git, GitHub, SSH, MCP, web, macOS | `ports/` Protocols + one adapter per vendor | no offline test; the Codex swap (UC-17) touches every caller |
| The personal process holds no executor, git or SSH handle (UC-36) | `shared/` · `dev/` · `personal/` + contracts | "no executor handle" is checkable only by reading code |
| SQL next to hour-long executor calls | `store.py` functions per feature (no classes, no Protocols) | "no port call inside a write transaction" (A3) has nothing to check |
| The self-updater survives a broken app (UC-19, R-32) | `supervisor/`: separate program, stdlib only | a broken update takes its own safety net down |
| Behaviour is data (D14, NFR-OPS-03) | one settings registry, one defaults file | literals scatter; "applies from the next message" fails |

**Not built:** repository classes or Protocols (tests use real SQLite; SQLAlchemy already makes Postgres a config
change, D1) · DTOs per layer (ORM rows stay in service/store; `domain` gets frozen dataclasses only as pure inputs) ·
a DI framework (one typed `Deps` per bot, built in `app.py`) · an event bus (durable work is a DB row) · per-feature
`errors.py`/`strings.py`/settings files · FastAPI. A Protocol missing from §4's table is a review failure.

## 1. Layout
```
Yordamchi/
├── pyproject.toml  uv.lock  Makefile    ruff · mypy · pytest · import-linter config · `make check`
├── .protected-core                      globs whose change asks the owner once (UC-19, Q5) — §12
├── config/{dev,personal}.env.example    bootstrap keys only (§12)
├── migrations/                          Alembic: env.py (render_as_batch=True, discovers */models.py), versions/
├── src/yordamchi/
│   ├── core/          kernel: errors strings text log redact aio retry proc config paths money timefmt
│   ├── ports/         one module per external system: Protocols + their value types
│   ├── adapters/      per vendor: claude codex gemini edge_tts telegram git github ssh mcp web keystore macos
│   │                  system_clock
│   ├── persistence/   base types engine uow upsert
│   ├── prompts/       <bot>/<feature>_<purpose>.md + __init__.py (render)
│   ├── shared/        deps.py + access inbox burst voice vault settings outbox cost health integrations audit bridge
│   ├── dev/           deps.py + routing turns gate questions projects digest servers newproject handoff selfupdate
│   ├── personal/      deps.py + qa media artefacts news
│   └── bots/          common.py (OwnerOnly middleware, guarded(), polling runner)
│       ├── dev/       app.py (composition root) · telegram.py (aiogram routers) · intents.py (chat → feature)
│       └── personal/  the same three files
├── supervisor/        self-updater + watchdog: own pyproject (dependencies = []), own venv, own tests
└── tests/             architecture/ unit/<group>/<feature>/ integration/ contract/ eval/<suite>/ fault/ migrations/
                       fakes/<port>.py factories.py fixtures/<vendor>/ golden/ conftest.py
```
Feature names are the expected set. The architect may add or rename features, never change their shape (§4).

## 2. Layers and imports
```
bots ─► dev | personal ─► shared ─► persistence | prompts ─► ports ─► core
bots ─► adapters ─────────────────────────────────────────► ports ─► core
inside a feature:  service ─► store ─► models ─► domain
```
| ID | Contract (import-linter; `confined` is MegaPlus Platform's `backend/lint/import_contracts.py`) | Why |
|---|---|---|
| C1 | `layers`: `bots` › `dev \| personal \| adapters` › `shared` › `persistence \| prompts` › `ports` › `core` | arrows point down; `\|` siblings are independent |
| C2 | `layers`, containers `shared.*` `dev.*` `personal.*`: `(service)` › `(store)` › `(models)` › `(domain)` | a store never calls a service |
| C3 | `forbidden`: `adapters` ↛ `shared`, `persistence`, `prompts`, `sqlalchemy` | adapters translate; no policy, no DB |
| C4 | `protected`: `adapters` imported only by `bots` | features see ports, never vendors |
| C5 | `confined`: `aiogram` → `bots`, `adapters.telegram` · `claude_agent_sdk` → `adapters.claude` · `google` → `adapters.gemini` · `sqlalchemy` → `persistence`, `*.*.store`, `*.*.models` · each other SDK → its adapter | one home per vendor; a new SDK adds its line in the same commit |
| C6 | `forbidden`: `*.*.domain` ↛ `sqlalchemy`, `aiogram`, every SDK, `persistence`, `adapters`, `bots`, `*.*.service`, `*.*.store`, `*.*.models` | pure means replayable |
| C7 | `independence`: `bots.dev`, `bots.personal` · `forbidden`, indirect included: `bots.personal` ↛ `dev`, `adapters.claude.executor`, `adapters.codex`, `adapters.ssh`, `adapters.git`, `adapters.github` | UC-36 as a test, not a promise |
| C8 | `protected`: `core.retry` imported only by `adapters` | retries live in adapters (§7) |

| ID | ast rule — what import-linter cannot express (pattern: BonviCall `server/tests/test_layering.py`) |
|---|---|
| A1 | `domain.py` imports none of `asyncio os subprocess socket shutil time logging`, uses `PurePosixPath` (not `Path`), never calls `open()` |
| A2 | a feature imports another feature's `service`/`domain`, never its `store`/`models`; cross-feature FKs are strings (`ForeignKey("projects.id")`), no cross-feature `relationship()` |
| A3 | a service's first parameter is `deps` (opens its own `uow()`, may call ports) or `s` (joins the caller's transaction, DB only). Inside `async with deps.db.uow() as s:` only `store.*`, `s.*` and `<feature>.service.*(s, …)` are awaited, never a port: SQLite has one writer, and a turn runs for minutes |
| A4 | every `async def` on a port Protocol has a keyword-only `timeout: float` (async iterators: `idle_timeout`) |
| A5 | `Msg(` only in `core/strings.py`; `BotText(` only in `core/text.py` and `core/redact.py`; `OwnerOrigin(` only in `shared/access/` |
| A6 | nothing under `supervisor/` imports `yordamchi` |
| A7 | each handler in `bots/*/telegram.py` awaits exactly one service call |
| A8 | log events are snake_case literals; `system_prompt=` is never a literal; `asyncio.sleep(n > 0)` only in `core/aio.py`, `core/retry.py`, `adapters/system_clock.py`; every `.protected-core` glob matches a file |

The personal bot reads dev data only through `shared/bridge`, on a read-only engine (`?mode=ro`). Nothing else crosses.

## 3. Where things live
| Concern | Single home | Check |
|---|---|---|
| Rules: gate classes, routing signals, burst windows, approval binding, setting validation, splitting | `<group>/<feature>/domain.py`; every input is an argument, including `now` and setting values | C6, A1 |
| Use cases, transactions | `service.py`; `persistence/uow.py` is the only code that commits | grep `\.commit(` → `uow.py` |
| SQL | `store.py`, `persistence/upsert.py` | C5, ruff S608 |
| Prompt text | `prompts/**/*.md` (defaults, in git) + `prompt.*` settings (owner edits, versioned in the DB) | A8; grep `You are` in `src/**/*.py` → 0 |
| Behaviour values (all of UC-09) | `shared/settings/defaults.py`, the one defaults file | review (UC-09 AC), `test_registry` |
| Bootstrap values | `core/config.py`, one class per process | TID251 `os.environ`, `os.getenv` |
| Bot-facing Uzbek text | `core/strings.py`, imported as `S` (OvozYoz `strings.py` pattern) | mypy `BotText`, A5 |
| Error classes and codes | `core/errors.py` | `test_error_catalog` |
| Time | `deps.clock.now()` (UTC-aware), `.sleep()`; display via `core/timefmt.py` | TID251 `datetime.now`, ruff DTZ |
| Money | `Decimal` in code, `int` micro-USD in the DB (`*_usd_micros`); SDK floats are converted in the adapter | review |
| Subprocesses · background tasks | `core/proc.py` · `core/aio.py` (`spawn`, `forever`, `raise_if_cancelling`) | TID251 `subprocess`, `asyncio.create_subprocess_*`, `asyncio.create_task`; RUF006 |
| Key patterns, masking (logs and every outbound) | `core/redact.py` | test:secrets |
| Owner-only edge | `bots/common.py` `OwnerOnly`, first outer middleware on `dp.update`: drops silently, logs ids only; policy in `shared/access/domain.py` | test:stranger |
| Tool-call gate | `dev/gate/domain.py` decides (pure, no model); each executor's `adapters/<vendor>/guard.py` calls `SessionSpec.guard` before every tool call; error or timeout → deny | test:gate, per executor |

## 4. Feature recipe and adapter recipe
A feature is one package. A self-update that adds a feature creates exactly these, omitting only empty ones:
```
src/yordamchi/<shared|dev|personal>/<feature>/
  __init__.py   empty; import from the submodule
  domain.py     frozen dataclasses + pure functions
  service.py    public API: async def <verb>_<noun>(deps: <Group>Deps, …) or (s: Session, …)
  store.py      async def get_/find_/list_/add_/set_/mark_<noun>(s: Session, …); never commits
  models.py     the tables this feature owns
+ entries in shared/settings/defaults.py · core/strings.py (under `# --- <feature> ---`) · core/errors.py
+ prompts/<bot>/<feature>_<purpose>.md               if it calls a model
+ bots/<bot>/intents.py                              one row, if the owner reaches it by chat
+ migrations/versions/<rev>_<feature>_<change>.py    if models.py changed
+ tests/unit/<group>/<feature>/test_domain.py, test_service.py (+ eval cases for routing, burst, gate, secrets, settings)
```
`<Group>Deps` (`<group>/deps.py`) is a frozen dataclass of ports, `db` and `clock`, built only in `bots/<bot>/app.py`;
`PersonalDeps` has no executor, git or SSH field. Handlers turn aiogram objects into domain inputs, persist and hand off
(A7): no SQL, no branching, no awaiting a model, STT or a send. Order comes from `update_id`, not handler scheduling.

| Port (`ports/`) | Production adapter (`adapters/`) | Second implementation — why the Protocol exists |
|---|---|---|
| `AgentRuntime`: project sessions (start/resume, send, steer, interrupt, events) | `claude/executor.py` (Agent SDK; guard as a `PreToolUse` hook) | `codex/` (UC-17); `FakeAgentRuntime` |
| `Assistant`: warm Q&A session with an allowlisted toolset | `claude/assistant.py` | `gemini/assistant.py`, labelled "zaxira model" (Q4) |
| `Transcriber` · `Synthesizer` | `gemini/stt.py` · `gemini/tts.py` | fake with scripted latency · `edge_tts/` (R-40) |
| `Messenger`: send, edit, delete, react, pin, leave, download | `telegram/` (aiogram `Bot`) | `FakeMessenger` |
| `Git` · `GitHub` | `git/` · `github/` (CLIs) | real git on `tmp_path` repos · fake |
| `RemoteShell` · `McpClient` | `ssh/` (OpenSSH, `~/.ssh/config`) · `mcp/` | fake · fixture MCP server (UC-35) |
| `WebTools` (screenshot, download) · `MacHost` (memory/disk, Mac attach, ClaudeManager) | `web/` · `macos/` | fakes |
| `Keystore` · `Clock` | `keystore/` (Keychain or 0600 file: the architect decides) · `system_clock.py` | in-memory · `FakeClock` (virtual time) |

A new adapter is `adapters/<vendor>/<capability>.py` holding `<Vendor><Port>` (`GeminiTranscriber`; `GitCli` when the
vendor is the port): the only file importing that SDK, translating every vendor exception into `ExternalError`. Add
`tests/fakes/<port>.py` (new port only), `tests/integration/test_<vendor>.py` on payloads recorded in
`tests/fixtures/<vendor>/`, `tests/contract/test_<port>.py` (fake and real; real is `live`), one wiring line in `app.py`
and one C5 line. Constructors take bootstrap values only (keys, paths, binaries); behaviour values arrive per call, so
a setting change applies to the next message:
```python
class Transcriber(Protocol):                                                  # ports/speech.py
    async def transcribe(self, audio: bytes, *, mime: str, languages: Sequence[str],
                         vocabulary: Sequence[str], model: str, timeout: float) -> Transcript: ...
# shared/voice/service.py: every value comes from this turn's settings snapshot
text = await deps.transcriber.transcribe(part.audio, mime=part.mime, languages=snap[STT_LANGUAGES],
                                         vocabulary=terms, model=snap[STT_MODEL], timeout=snap[STT_TIMEOUT_S])
```

## 5. Settings and prompts
```python
BURST_AFTER_MEDIA_S = Setting("burst.after_media_s", float, 10.0, check=Range(2, 60), group=Group.BURST,
                              bots=BOTH, doc="Quiet window after a bare photo, file or link (UC-02).")
GATE_ASK_CLASSES = Setting("gate.ask_classes", list[str], GATE_CLASSES, check=SubsetOf(GATE_CLASSES),
                           group=Group.DECISIONS, bots=DEV, weakens=removes_items, doc="UC-13 classes that ask.")
```
- `shared/settings` adds `registry.py` (the mechanism) and `defaults.py` (every `Setting`). Values are JSON only
  (`bool int float str list[str] dict[str, str]`; enums are `str` + `OneOf`): a new setting is one declaration and
  never a mechanism change (NFR-OPS-03).
- `weakens` is a pure `(old, new) -> bool` (`always`, `increases`, `decreases`, `disables`, `removes_items`).
  `test_registry` fails if a default fails its own check, or if a safeguard has no `weakens`: owner/group ids, the
  gate list, the secret scan, cost caps, the concurrency cap.
- Read at use time with `snap = await settings.snapshot(deps)`, once per turn or job; never cached in a global or a
  constructor.
- A change is a new `setting_versions` row (append-only; revert = a new row with the old value) and needs an
  `OwnerOrigin`, which only `shared/access` mints from a verified owner update. Executor output, tool results and web
  content can never change a setting or approve a decision (A5, UC-09 F, R-18).
- Prompts are English Markdown with `{{slot}}` placeholders and no logic; persona, tone and length limits are slots
  filled from settings. `prompts.render(name, **slots)` raises on a missing or unused slot.

## 6. Async
- One event loop per process: `asyncio.run` only in `bots/*/app.py` and the supervisor's main (TID251). Nothing blocks
  the loop (ruff `ASYNC`); sync libraries (PyAV, yt-dlp) run via `asyncio.to_thread` inside their adapter.
- Per-turn concurrency uses `asyncio.TaskGroup`. Long-lived loops (polling, outbox, scheduler, heartbeat) run under
  `core.aio.forever(name, fn)`, which logs, backs off and restarts the body; siblings that must outlive each other never
  share a TaskGroup. Every external await is bounded (A4): the service passes a setting, the adapter applies it.
- Subprocesses only via `core/proc.py`: an argv list, never a shell (ruff S602, S605), `start_new_session=True`, env
  from `clean_env()` (strips `CLAUDECODE`, `CLAUDE_CODE_*`; `~/.local/bin` first in PATH: R-06). A timeout or cancel
  kills the process group (TERM, grace, KILL).
- `app.py` starts with `os.umask(0o077)` and sanitises `os.environ`, which the Agent SDK merges into the `claude`
  child. It refuses to start if `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_AUTH_TOKEN` or
  `CLAUDE_CONFIG_DIR` is set (NFR-SEC-04).
- `CancelledError` is never swallowed: no bare `except` (E722), no `except BaseException`; inside a loop's
  `except Exception`, call `raise_if_cancelling()` first (OvozYoz `engines/base.py`).
- An interrupt is confirmed only after the turn's guard denies new tool calls; `session.interrupt()` and the
  process-group kill follow (UC-03). Windows, expiries and schedules use `deps.clock`, so tests run on virtual time.

## 7. Errors and logging
- One hierarchy in `core/errors.py`: `YordamchiError(code, **params)` → `DomainError` (expected: `SettingOutOfRange`,
  `ProjectAmbiguous`, …) and `ExternalError(retryable)`, raised only by adapters (`ProviderUnavailable`,
  `RateLimited(retry_after)`, `AuthFailed`, `CallTimedOut`, `NotInstalled`). `code` is stable snake_case and keys its
  Uzbek `Msg` in `S.ERRORS` (AgentsRoom rule); `test_error_catalog` fails on a code without a message.
- Adapters translate vendor exceptions (as OvozYoz `describe_error` does) and retry only `retryable` errors via
  `core/retry.py`: backoff with jitter, honours `retry_after`, stays inside the call's `timeout`. Services never retry;
  outbox redelivery and the usage-limit wait are durable state, not retry loops.
- `except Exception` without re-raise exists in three places only (BLE001 per-file allowance): `bots/common.guarded()`
  around every turn and job, `core.aio.forever()`, `adapters/*/guard.py` (fail closed: deny). A `YordamchiError` →
  warning, and the owner gets `S.ERRORS[code](**params)`; anything else → error with a scrubbed traceback, and the owner
  gets `S.ERR_INTERNAL(incident=…)`. aiogram `@dp.errors()` logs and returns `True` (Wunderkind `bot/main.py`).
- The owner sees Uzbek: project name first, what happened, what to do. Never a traceback, class name or provider text.
- Logs: JSON lines in `~/Library/Logs/Yordamchi/<process>.jsonl`, rotated, ≤ 200 MB in total, e.g.
  `log.info("turn_finished", project=…, turn_id=…, duration_ms=…)`. Log ids, codes, counts, durations, versions —
  **never content** (message text, transcripts, prompts, model output, tool I/O, env values); that is what keeps an
  unknown-format key out of logs (UC-04). Strangers: numeric ids only. Every record passes `core/redact.py`.

## 8. Persistence
- SQLAlchemy 2 typed ORM (`Mapped[]`, `mapped_column`); `persistence/base.py` sets `MetaData(naming_convention=…)`,
  which SQLite batch migrations need. Prefer no `relationship()`; if needed, `lazy="raise"`.
- `engine.py` sets `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000` on connect and uses
  `async_sessionmaker(expire_on_commit=False)`; DB files are 0600. `persistence/types.UTCDateTime` rejects naive
  datetimes on write and returns UTC-aware ones; columns end in `_at`.
- Idempotency keys are written before acting, via `upsert.insert_or_ignore()` (dialect-dispatched, so D1 holds):
  `inbound_updates (bot, update_id)` UNIQUE, processed only if the insert happened; `deliveries (turn_id, seq)`;
  `outbox_messages.dedupe_key` (NFR-REL-05/06).
- Migrations: `alembic revision --autogenerate -m "<feature>_<change>"`, then **read it** and delete spurious
  `drop_*`/`alter_column` on tables you did not touch. Expand-only across one version (R-33): new tables, nullable
  columns, indexes; rename or drop one release after the code stopped using it. Rollback restores the pre-update DB
  copy, not `downgrade()`. test:migrations: `upgrade head` on an empty DB and on the previous release's fixture DB,
  plus `alembic check`.

## 9. Naming
| Thing | Pattern | Example |
|---|---|---|
| Package, module | singular snake_case; never a stdlib name (ruff A005) | `dev/routing/`, `shared/vault/` (not `secrets`) |
| Table · ORM class | plural snake_case · singular PascalCase | `inbound_updates` · `InboundUpdate` |
| Column | `<entity>_id`, `*_at`, `is_*`, `*_usd_micros`, `*_ms` | `turn_id`, `created_at`, `is_archived` |
| Domain type | frozen dataclass, a noun | `BurstPart`, `RouteDecision`, `GateVerdict` |
| Service · store function | `verb_noun` · `get_/find_/list_/add_/set_/mark_<noun>` | `route_burst` · `find_project_by_alias` |
| Port · adapter · fake | capability · `<Vendor><Port>` · `Fake<Port>` | `Transcriber` · `GeminiTranscriber` · `FakeTranscriber` |
| Setting key · constant | `<group>.<name>_<unit>` · UPPER_SNAKE | `burst.after_media_s` · `BURST_AFTER_MEDIA_S` |
| Error code · catalog entry | `<area>_<problem>` · `<FEATURE>_<WHAT>` | `stt_failed` · `ROUTING_WHICH_PROJECT` |
| Prompt · log event | `prompts/<bot>/<feature>_<purpose>.md` · snake_case, past tense | `prompts/dev/routing_classify.md` · `update_dropped` |
| Intent · test | `verb_noun` · `test_<behaviour>` | `show_health` · `test_lone_photo_proceeds_after_window` |

## 10. Chat output (the UI; there is no frontend)
- `Messenger` accepts only `BotText` (mypy), made by a catalog entry (`S.HEALTH_ONLINE(time=…)`) or by
  `redact.scrubbed(text)` for model output and transcripts. The outbox scrubs again before sending.
- No `reply_markup`, keyboard or command menu (D8): grep `reply_markup` in `src/` → 0; the Telegram adapter's
  recording-session test fails on any request carrying one (UC-12). HTML escaping, code blocks, the 4,096/1,024 split
  and the `.md` fallback live only in `shared/outbox/domain.py`. Every project message starts with the project name.

## 11. Testing
| Kind | Where | Rules |
|---|---|---|
| Unit | `tests/unit/<group>/<feature>/` | fakes + a real SQLite file in `tmp_path` (`create_all`); data from `tests/factories.py` (`await make_project(s, name="BonviHR")`); no shared mutable fixtures |
| Integration (offline) | `tests/integration/`, `tests/contract/` | adapters on recorded, sanitised payloads (`tests/fixtures/<vendor>/`); real git on tmp repos; both dispatchers via `Dispatcher.feed_update` with a recording aiogram session (stranger → 0 requests, 0 inbox rows); each port's contract suite runs against the fake and the real adapter |
| Eval | `tests/eval/{routing,burst,gate,secrets,settings,injection}/` | `cases.yaml` + one runner; thresholds in `tests/eval/thresholds.toml`, copied from REQUIREMENTS §4. Model answers replay from `tests/fixtures/llm/<suite>/<sha256(prompt_version+input)>.json`; a stale key fails, `pytest -m live --record-llm` re-records. The gate suite drives every executor's `guard.py` with recorded tool-call payloads |
| Fault · migrations · architecture · supervisor | `tests/fault/` · `tests/migrations/` · `tests/architecture/` · `supervisor/tests/` | `kill -9`, restart, network faults (NFR-REL-05/06) · §8 · §2 · the three broken-update fixtures and `kill -9` mid-switch (UC-19) |

- The network is closed (pytest-socket `--disable-socket --allow-unix-socket`, as in MegaPlus). `live` tests (real
  Telegram, Claude, Gemini, GitHub, SSH) are opt-in: `addopts` has `-m "not live"`, and `conftest.py` re-enables
  sockets only for them. `asyncio_mode = "auto"`, `--strict-markers`, `xfail_strict = true`, `filterwarnings = ["error"]`.
- Must have a test: every domain function; every public service function (happy path + one failure); every port
  (contract); every adapter error mapping; every `Setting` (auto-parametrized); every error code ↔ `Msg`; every
  migration; every bug fix (a regression test named after the symptom). Golden output (`tests/golden/*.txt`: final
  result, `holat`, settings list) changes only with `--update-golden` and a reviewed diff.
- Never delete, skip or weaken a test or a threshold to get green; no `assert True`; `skip`/`xfail` need a reason.
  `thresholds.toml` is protected core, and a self-update whose collected test count drops is refused.

## 12. Config, secrets, supervisor
- `.env` is bootstrap only, one per process, outside the repo: `~/Library/Application Support/Yordamchi/{dev,personal}.env`
  (0600) with the bot token, owner id, group id and paths. Two files, because the personal process must not hold the
  dev token (UC-36). Templates: `config/*.env.example`; `.gitignore` covers `*.env`.
- Everything else is a DB setting (§5); API keys and MCP tokens go to the vault (`shared/vault`, ciphertext only), and
  the master key is never in the data dir or the repo (NFR-SEC-02). The DB, media cache and env files stay in the data
  dir, so an executor working on Yordamchi's own repo (UC-19) cannot read them (R-22).
- `.protected-core` starts as: `supervisor/**`, `src/yordamchi/bots/common.py`, `src/yordamchi/shared/{access,vault}/**`,
  `src/yordamchi/dev/gate/**`, `src/yordamchi/adapters/*/guard.py`, `src/yordamchi/core/redact.py`,
  `tests/eval/thresholds.toml`, `.protected-core`.
- `supervisor/` is its own uv project, **not** a workspace member: pinned Python, `dependencies = []` (stdlib `sqlite3`
  for the DB copy, `urllib` for Telegram, `subprocess` for git and `launchctl`). It imports nothing from `yordamchi`
  (A6) and keeps its own `supervisor/strings.py`, the one exception to the single catalog. An update touches
  `supervisor/` or the app, never both (R-32).

## 13. Forbidden
| Forbidden | Check |
|---|---|
| `Any` outside `adapters/`; a bare `# type: ignore` or `# noqa` | ruff ANN401, PGH003, PGH004 |
| bare `except`, `except BaseException`, `except Exception` outside §7's three places, `except: pass`, `suppress(Exception)` | ruff E722, BLE001, S110 |
| SQL built from strings; `sqlalchemy.text` outside `migrations/`; logic or SQL in `bots/` | ruff S608, TID251, A7, C5 |
| `float` for money; naive datetimes; `datetime.now()` outside `adapters/system_clock.py` | review, ruff DTZ, TID251 |
| `print`, `logging.basicConfig`, f-strings in log calls, content in logs | ruff T201, TID251, G004, A8, review |
| `time.sleep`, `requests`, sync subprocess in async code; `shell=True`; `create_task` outside `core/aio.py`; module-level clients, engines or `Bot()` | ruff ASYNC, S602, S605, TID251, review |
| Inline prompts; a string literal sent to the owner; `reply_markup` | A8, mypy `BotText`, grep |
| Setting or passing `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CONFIG_DIR` | grep → only `core/proc.py`'s refuse-list and its test |
| `supervisor/` importing `yordamchi`; committing a `.env` file or a secret; `assert True`; weakened tests | A6, pre-commit secret scan, review, test-count check |
