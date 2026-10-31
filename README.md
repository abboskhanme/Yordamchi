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

## Windows: personal bot only

Other people can run just the personal bot on Windows, from a terminal (no supervisor, no service, no dev bot). It
updates itself from GitHub every 3 days.

Before: [Git for Windows](https://git-scm.com/download/win), [uv](https://docs.astral.sh/uv/), the Claude Code
native installer (`claude.exe` in `%USERPROFILE%\.local\bin`; run `claude` once and log in), and `ffmpeg` on `PATH`
for voice notes. In BotFather create a bot, then `/setjoingroups` → Disable.

```bat
git clone https://github.com/abboskhanme/Yordamchi.git
cd Yordamchi
uv sync --frozen
uv run python scripts\windows\setup.py
scripts\windows\run.cmd
```

- `setup.py` asks for the bot token and your Telegram id, keeps the token encrypted with Windows DPAPI (a copied file
  is useless elsewhere) and creates the database in `%LOCALAPPDATA%\Yordamchi`. Re-running it keeps the encryption key.
- `run.cmd` starts the bot, restarts it after a crash and every 3 days pulls `origin/main` (fast-forward only, never over
  local changes), runs `uv sync` and the migrations, and restarts on the new code; a failed update is rolled back.
  Stop it with Ctrl+C. Log: `%LOCALAPPDATA%\Yordamchi\logs\runner.log`.
- Send the Gemini (or OpenRouter) key to the bot in chat for voice. Details: `docs/DEPLOY.md` §1.3.

## Read first

- `CLAUDE.md` — project rules.
- `docs/SPEC.md` — architecture, data model, executor contract, phases and Definition of Done.
- `docs/CONVENTIONS.md` — layering, import contracts, AST rules, the feature recipe.
- `docs/STACK.md` — pinned versions. `docs/TASKS.md` — what is next.
- `.claude/skills/new-module/SKILL.md` — how to add a feature or an adapter.
