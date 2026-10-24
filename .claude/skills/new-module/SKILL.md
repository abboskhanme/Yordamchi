---
name: new-module
description: Recipe for adding a feature package or a vendor adapter to Yordamchi in the exact shape docs/CONVENTIONS.md §4 requires. Use whenever a work unit from docs/SPEC.md §7 (or a self-update) adds a feature, a use case, a port, or an adapter.
---

# New feature / adapter recipe (Yordamchi)

Before you start:
- Read `docs/CONVENTIONS.md` §2–§5 and the feature's section of `docs/SPEC.md` (§3.1 map, §3.2 flow, §4.2 tables).
- Copy the closest existing feature package.
- Do the steps in order.
- Run `make check` at the end. It must be green.

## 1. Feature package: `src/yordamchi/<shared|dev|personal>/<feature>/`
Create only the files below. Leave out a file only when it would be empty.
```
__init__.py   empty
domain.py     frozen dataclasses + pure functions; every input is an argument (incl. `now`, setting values)
service.py    async def <verb>_<noun>(deps: <Group>Deps, …)  or  (s: Session, …)
store.py      async def get_/find_/list_/add_/set_/mark_<noun>(s: Session, …); never commits
models.py     the tables this feature owns (plural snake_case tables, singular PascalCase classes)
```

### Rules
- **`domain.py` stays pure.** It must not import asyncio, os, subprocess, socket, time, logging, sqlalchemy, any SDK, or another feature's store/models. It does no `open()` and uses `PurePosixPath` (C6, A1).
- **No port calls inside a write transaction.** Inside `async with deps.db.uow() as s:` await only `store.*`, `s.*` and `<feature>.service.*(s, …)` (A3).
- **Another feature: use its `service`/`domain`, never its `store`/`models`.** Cross-feature foreign keys are strings, and there is no `relationship()` (A2).
- **Read settings once per turn** with `snap = await settings.snapshot(deps)`. Never cache a setting in a constructor or a global.
- **Every external await is bounded.** Pass `timeout=snap[...]`.
- **Owner text:** only `S.<ENTRY>(...)` from `core/strings.py`, or `redact.scrubbed()` for model output. Never a literal, never `reply_markup`.
- **Errors:** raise a `DomainError` subclass with a code from `core/errors.py`. Services never retry.

## 2. Shared files (one writer at a time; SPEC §7)
1. `shared/settings/defaults.py`: one `Setting(...)` per behaviour value. A safeguard needs `weakens=`.
2. `core/strings.py`: entries under `# --- <feature> ---`, in Uzbek (Latin). Each message starts with the project name when it is about a project.
3. `core/errors.py`: new codes. Every code needs its `S.ERRORS` message (`test_error_catalog`).
4. `prompts/<bot>/<feature>_<purpose>.md`: only if the feature calls a model. English, `{{slot}}` placeholders, no logic.
5. `bots/<bot>/intents.py`: one row if the owner reaches the feature by chat. Handlers await exactly one service call (A7).
6. `bots/<bot>/app.py`: wiring only.
7. `pyproject.toml`: a new SDK gets its confinement line (C5) in the same commit.

## 3. Migration (only if `models.py` changed)
```
uv run alembic revision --autogenerate -m "<feature>_<change>"
```
1. **Read the generated file.** Delete any spurious `drop_*` or `alter_column` on tables you did not touch.
2. Keep it expand-only: new tables, nullable columns, indexes.
3. `tests/migrations` must pass: `upgrade head` on an empty DB and on the previous release fixture, plus `alembic check`.

## 4. Tests
- `tests/unit/<group>/<feature>/test_domain.py`: every rule, on virtual time (`FakeClock`).
- `tests/unit/<group>/<feature>/test_service.py`: the happy path plus each failure listed for the use case in REQUIREMENTS. Use fakes from `tests/fakes/` and a real SQLite file in `tmp_path`.
- Eval cases go in `tests/eval/<suite>/cases.yaml` if the feature touches routing, burst, gate, secrets, settings or injection.
- Never lower `tests/eval/thresholds.toml`.

## 5. New adapter: `src/yordamchi/adapters/<vendor>/<capability>.py`
- **The class:** name it `<Vendor><Port>`. This is the only file that imports the SDK. It translates every vendor exception into an `ExternalError` subclass and retries only `retryable` errors via `core/retry.py`.
- **The constructor** takes bootstrap values only (keys, paths, binaries). Behaviour values arrive per call.
- **Fake:** `tests/fakes/<port>.py`, only if the port is new.
- **Integration test:** `tests/integration/test_<vendor>.py`, on sanitised recordings in `tests/fixtures/<vendor>/`.
- **Contract test:** `tests/contract/test_<port>.py` runs against both the fake and the real adapter. The real one is marked `live`.
- **Wiring:** one line in `bots/<bot>/app.py`, plus one C5 confinement line in `pyproject.toml`.

## 6. Done
- `make check` is green.
- The collected test count did not drop.
- `docs/QOLLANMA.md` (Uzbek) says what the owner can now say to the bot, if the feature is owner-facing.
- One-line decisions are appended to `docs/ASSUMPTIONS.md`.
