# Yordamchi

Two owner-only Telegram bots that run on the owner's MacBook as launchd agents:

- the **dev bot** turns messages and voice notes into work for headless Claude Code sessions, one per project, and
  asks only about important decisions (deploy, server changes, data loss, money, sending to others);
- the **personal bot** answers anything with text first and a short Uzbek voice summary after it.

A stdlib-only **supervisor** (`supervisor/`, its own uv project) is the watchdog and the self-updater.

## Develop

Python 3.12.15 and uv 0.13.5; no Homebrew.

```sh
uv sync            # the pinned set from uv.lock
make check         # every gate: ruff, mypy --strict, import-linter C1–C8, pytest (A1–A9, network closed),
                   # supervisor unittest, alembic upgrade head + alembic check on fresh dev.db and personal.db
make fix           # ruff --fix + format
```

Nothing runs from this working copy: installed bots run from
`~/Library/Application Support/Yordamchi/releases/current`. Bootstrap env files live there too (templates in
`config/*.env.example`); never commit them.

## Read first

- `CLAUDE.md` — project rules.
- `docs/SPEC.md` — architecture, data model, executor contract, phases and Definition of Done.
- `docs/CONVENTIONS.md` — layering, import contracts, AST rules, the feature recipe.
- `docs/STACK.md` — pinned versions. `docs/TASKS.md` — what is next.
- `.claude/skills/new-module/SKILL.md` — how to add a feature or an adapter.
