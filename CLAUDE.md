# Yordamchi — project rules

Two owner-only Telegram bots that run on the owner's MacBook as launchd agents:
- the **dev bot** drives headless Claude Code sessions, one per project;
- the **personal bot** is an assistant whose brain is Claude, with Gemini used for voice.

A stdlib-only **supervisor** acts as watchdog and self-updater. These rules override the global
`~/.claude/CLAUDE.md` where they conflict.

## Read first
- `docs/SPEC.md`: architecture, data model, executor contract, phases, DoD (§12).
- `docs/CONVENTIONS.md`: layering, import contracts C1–C8, ast rules A1–A8, feature recipe. These are binding.
- `docs/REQUIREMENTS.md`: owner decisions D1–D14 (final) and the acceptance criteria IDs.
- `docs/STACK.md`: pinned versions and SDK mechanisms. `docs/TASKS.md`: what to do next.
- `docs/RISKS.md` and `docs/ASSUMPTIONS.md`. Log your own calls in ASSUMPTIONS as one line each.

## Owner decisions that code must never drift from
- Strangers get nothing. Only `OWNER_ID` is served, and the dev bot also accepts only `DEV_GROUP_ID`.
  Anything else is dropped silently, and the bot leaves foreign groups.
- No inline buttons, reply keyboards or BotFather command menu, ever: `reply_markup` in `src/` → 0.
  All interaction is conversation.
- In the projects the bot works on, commits, `git push` and private GitHub repos happen automatically.
  It asks the owner, in chat, only about important decisions (SPEC §5.7: deploy or prod, data loss,
  money, sending to others).
- Dev bot: one private forum group with Topics (owner, 2026-10-26; D4 revised): **General** for talk, reports and
  routing, and one topic per project that the bot opens itself on the project's first work. A message in a project
  topic goes to that project; in General, routing is content-first and a reply is only one signal.
- The brain of both bots is the owner's Claude subscription through the official `claude` binary.
  Gemini or OpenRouter only does STT, TTS and optional search or video.
- Never touch Claude credentials. ClaudeManager rotates the accounts. Never set
  `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_AUTH_TOKEN` or `CLAUDE_CONFIG_DIR`,
  and never use `--bare`.
- Integrations belong to Yordamchi (its vault, injected per session) and are never tied to a Claude
  account.
- Do not change the owner's `~/.claude/settings.json` (`cleanupPeriodDays` stays 30).
- Behaviour lives in settings (`shared/settings/defaults.py`), not constants. Prompts live in
  `src/yordamchi/prompts/**`. Bot text is Uzbek and comes only from `core/strings.py`.

## Working in this repo
- `make check` must be green before anything is called done: ruff, mypy `--strict`, import-linter,
  architecture rules, pytest with the network closed, supervisor tests, and alembic upgrade/check.
  Never delete, skip or weaken a test, an eval case or `tests/eval/thresholds.toml`.
- New feature or adapter: follow `.claude/skills/new-module/SKILL.md`.
- Shared files are edited by one unit at a time (SPEC §7): `core/strings.py`, `core/errors.py`,
  `shared/settings/defaults.py`, `migrations/versions/*`, `bots/*/intents.py`, `bots/*/app.py`,
  `pyproject.toml` and `uv.lock`.
- Migrations: after `alembic revision --autogenerate`, read the file and delete spurious drops.
  Migrations are expand-only (CONVENTIONS §8).
- Files listed in `.protected-core` are the safety net. Change them only on an explicit task, with
  tests.
- Tests never hit the network. Real-service tests are `-m live` and opt-in.
- Nothing runs from this working copy. The installed bots run from
  `~/Library/Application Support/Yordamchi/releases/current`.
- Never commit `*.env`, DB files, `master.key` or any secret. Pushing to a remote needs the owner's
  OK (there is no remote yet).
