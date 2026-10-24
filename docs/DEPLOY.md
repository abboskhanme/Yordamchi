# Yordamchi — Deploy, operate, roll back

How Yordamchi is installed on the owner's MacBook, how it is updated and rolled back, where to look when something
is wrong, and how to rotate the bot tokens. Sources: SPEC §2.2 (layout), §2.5 (startup), §2.8 (heartbeat and
watchdog), §9 (ops), §12 (DoD); STACK §5 (runtime dependencies).

Everything runs as the owner's user. There is no server, no inbound port, no Docker and no Homebrew.

```
DATA = ~/Library/Application Support/Yordamchi        (0700)
LOGS = ~/Library/Logs/Yordamchi
KEY  = ~/.config/yordamchi/master.key                 (0600, dir 0700, excluded from Time Machine)
UID  = $(id -u)
```

## 0. Rules that apply to every section

- **The real install, an update and a rollback touch the live bots. Do them only with the owner's go-ahead.**
  Agents run `--dry-run` only.
- Run the installer with the uv-managed Python: `~/.local/bin/python3.12 -I scripts/install.py …`.
- Never paste a token into a chat (Telegram, Claude, anywhere). Tokens are typed at the installer's hidden prompt.
- Never edit files inside `DATA/releases/*`: they are read-only on purpose. A change is a new commit and a new
  release.
- Before any migration, a backup is taken (the installer does it in `backups/pre-update/install-…`). A rollback
  restores that copy; there are no `alembic downgrade` rollbacks (CONVENTIONS §8).
- `make check` must be green on the commit you install (the supervisor applies the same rule to self-updates).

## 1. First install (T43)

Preconditions, all owner actions:

| What | Why | Status today |
|---|---|---|
| X01 Telegram setup: dev privacy off, the private supergroup with the dev bot as admin (delete + pin), join-groups off afterwards, Telegram 2FA | the check script verifies it | partial |
| Both bot tokens **rotated** in BotFather (the old ones were pasted into a transcript) | §6 below | todo |
| X05 `sudo pmset -c sleep 0`, on the charger, automatic macOS update restarts off | the Mac must not sleep on AC | todo |
| ffmpeg 9.0.2 URLs and SHA-256 recorded in STACK §5 and copied into `FFMPEG_PINS` in `scripts/install.py` | the installer refuses to guess a checksum | todo (or install with `--skip-ffmpeg`) |
| T32 heartbeat writer and T34 `bots/dev/app.py` merged | without them the watchdog restarts the dev bot every ~2 min | todo |

Steps:

1. In the working copy, on the commit to install, run `uv sync && make check`. It must end with
   `make check: all stages green`.
2. Dry run (safe, any time):
   ```sh
   ~/.local/bin/python3.12 -I scripts/install.py --dry-run --release <commit>
   ```
   It builds the whole tree under a new temporary HOME (release, venv with `uv sync --offline`, both DBs migrated,
   the rendered plists) and prints a summary. It never calls `launchctl` or `tmutil`, never downloads, never asks
   for a token and never reads the real env files. Every line should be `done`, `unchanged` or `dry-run`; a
   `failed` line names what to fix.
3. Real install:
   ```sh
   ~/.local/bin/python3.12 -I scripts/install.py --release <commit>      # add --skip-ffmpeg if not pinned yet
   ```
   It asks you to type `install`, then, in order: preflight → uv 0.13.5 and Python 3.12.15 → the DATA tree (0700)
   → `master.key` (created once, never overwritten) → env files → `git archive <commit>` into
   `DATA/releases/<YYYY.MM.DD-sha7>/` with its own `.venv`, made read-only → the supervisor release → ffmpeg →
   DB backup → `alembic upgrade head` on `db/dev.db` and `db/personal.db` → `current` symlinks → plists →
   `launchctl bootstrap` → the check script.
   - Env files: a value already in `dev.env` / `personal.env` is **never** overwritten. Only empty values are asked
     for. Tokens are read hidden. `OWNER_ID` and `DEV_GROUP_ID` may be left empty and filled in later (§1.1).
   - `DEV_GROUP_ID` must be a `-100…` supergroup id. A basic group changes its id when Telegram upgrades it, and the
     bot would then leave it as a foreign group.
   - `uz.yordamchi.personal` is rendered into `DATA/launchd/` only. It is installed and loaded with
     `--enable-personal` at P3.
4. Run the check from the installed release and fix every red (`XATO`) line:
   ```sh
   "$DATA/releases/current/.venv/bin/python" -I -m yordamchi.bots.cli.check
   ```
   (`python -m yordamchi.check` once its entry point is wired; see `docs/PENDING_WIRING.md`.)

Rollback of a first install: §4.3 (stop and remove the jobs). Nothing outside DATA, the key, the logs and the three
plists is created.

### 1.1 Finding OWNER_ID and DEV_GROUP_ID

1. Stop the dev bot if it is running (`launchctl bootout gui/$UID/uz.yordamchi.dev`), or `getUpdates` answers
   "conflict".
2. Write the bot a private message, and in the dev group write a message that mentions it (`@OrvionXBot salom`).
3. Run `"$DATA/releases/current/.venv/bin/python" -I -m yordamchi.bots.cli.check --discover-ids`. It calls
   `getUpdates` once without an offset (nothing is confirmed, so the bot still receives those messages later),
   prints the candidate ids and offers to write the empty ones into `dev.env` (and `OWNER_ID` into
   `personal.env`). An id already in the file is never replaced.
4. Start the bot again: `launchctl bootstrap gui/$UID ~/Library/LaunchAgents/uz.yordamchi.dev.plist`.

## 2. What runs where

| Job | Program | Plist | Logs |
|---|---|---|---|
| `uz.yordamchi.dev` | `DATA/releases/current/.venv/bin/python -I -m yordamchi.bots.dev.app` | `~/Library/LaunchAgents/uz.yordamchi.dev.plist` | `LOGS/dev.jsonl` (+ `.1`–`.3`), `LOGS/uz.yordamchi.dev.launchd.log` |
| `uz.yordamchi.personal` (P3) | `… -m yordamchi.bots.personal.app` | staged in `DATA/launchd/` until P3 | `LOGS/personal.jsonl`, `LOGS/uz.yordamchi.personal.launchd.log` |
| `uz.yordamchi.supervisor` | `DATA/supervisor/current/.venv/bin/python -I -m yordamchi_supervisor` | `~/Library/LaunchAgents/uz.yordamchi.supervisor.plist` | `LOGS/supervisor.jsonl` (+ `.1`), `LOGS/uz.yordamchi.supervisor.launchd.log` |

Plist facts (SPEC §9.1): `KeepAlive` and `RunAtLoad` true, `ThrottleInterval` 10, `ExitTimeOut` 20 for the bots
and 10 for the supervisor, `AbandonProcessGroup` false (children die with the bot), `WorkingDirectory` `DATA/run`.
`EnvironmentVariables`: `YORDAMCHI_DATA_DIR`, for the bots also `YORDAMCHI_ENV_FILE`, `PATH`
(bots: `DATA/releases/current/shims` first; supervisor: no shims), `LANG`, `PYTHONUNBUFFERED`.

The templates are `ops/launchd/*.plist.tmpl`; the installer renders them from the release it installs. Never edit
a plist in `~/Library/LaunchAgents` by hand: the next install replaces it.

## 3. Update (until the supervisor's self-update, T57, takes over)

1. On the new commit: `uv sync && make check` (green), then the dry run (§1 step 2).
2. Owner go-ahead.
3. `~/.local/bin/python3.12 -I scripts/install.py --release <new commit>`.
   The installer stops the loaded bots (`bootout`), copies each DB to
   `DATA/backups/pre-update/install-<version>-<timestamp>/` (sqlite3 backup API, `integrity_check` must be `ok`),
   runs `alembic upgrade head` from the **new** release, and only then switches `current`. A failure before the
   switch leaves `current` where it was and brings the stopped bots back on the old release.
4. The summary ends with a **Rollback** block naming the previous `releases/current` and `supervisor/current`.
   Keep it until the new version has run for a day.
5. Run the check (§1 step 4) and say "holat?" in the dev group.

Re-running the installer on the same commit is safe: a built release is reused, the key and env values are kept,
and the jobs are restarted with `kickstart -k`.

## 4. Rollback

### 4.1 Back to the previous release (code only)

```sh
cd "$HOME/Library/Application Support/Yordamchi"
ls releases/                                  # the previous version is the one the installer summary named
ln -sfn <previous-version> releases/.current.tmp && mv -fh releases/.current.tmp releases/current
launchctl kickstart -k gui/$UID/uz.yordamchi.dev
```
Supervisor: the same with `supervisor/current -> releases/<previous-version>` and
`launchctl kickstart -k gui/$UID/uz.yordamchi.supervisor`. Migrations are expand-only (CONVENTIONS §8), so the
previous release runs on the newer schema. If it refuses with `STARTUP_SCHEMA_MISMATCH`, also restore the DB (§4.2).

### 4.2 Back to the pre-update database

Use the copy the installer made right before migrating (`backups/pre-update/install-<version>-<timestamp>/`), then
follow §5 with that directory instead of a daily one. Everything the bot wrote after that copy (messages, settings,
decisions) is lost, so roll the code back first (§4.1) and restore the DB only when the old code needs it.

### 4.3 Stop, or remove, the installed jobs

```sh
launchctl bootout gui/$UID/uz.yordamchi.dev          # stop; KeepAlive does not restart a booted-out job
launchctl bootout gui/$UID/uz.yordamchi.supervisor
rm ~/Library/LaunchAgents/uz.yordamchi.{dev,supervisor}.plist   # only to uninstall; otherwise login reloads them
```
Start again: `launchctl bootstrap gui/$UID ~/Library/LaunchAgents/uz.yordamchi.<x>.plist`. The supervisor leaves a
booted-out bot alone (it acts only on loaded jobs).

Never delete `master.key`: the vault in the DBs cannot be decrypted without it.

## 5. Restore a database from a backup

Backups (supervisor, daily from 04:00 Asia/Tashkent; SPEC §9.2, `supervisor/…/backups.py`):
`DATA/backups/daily/<YYYY-MM-DD>/{dev,personal}.db` (keep 7), `weekly/<date>/` (Sunday, keep 4),
`pre-update/<id>/` (keep 5). Every copy passed `PRAGMA integrity_check`; a failed copy is kept as `<name>.db.failed`
and alerted.

Restore `dev.db` (the same for `personal.db` with its own label):
```sh
cd "$HOME/Library/Application Support/Yordamchi"
launchctl bootout gui/$UID/uz.yordamchi.dev                      # 1. stop the only writer
cp db/dev.db db/dev.db.before-restore                            # 2. keep what is there now
sqlite3 backups/daily/<YYYY-MM-DD>/dev.db 'PRAGMA integrity_check'   # 3. must print: ok
cp backups/daily/<YYYY-MM-DD>/dev.db db/dev.db                   # 4. copy the backup over
rm -f db/dev.db-wal db/dev.db-shm                                # 5. drop the old WAL, or it is replayed on top
chmod 600 db/dev.db
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/uz.yordamchi.dev.plist   # 6. start it
```
Then run the check and "holat?". The restore is tested: `supervisor/tests/test_backups.py::Restore` runs exactly
these steps on a scratch copy and compares every row. Practise it once on a copy (`cp -R DATA/db /tmp/restore-test`)
after T43, so the first real restore is not the first one.

## 6. Rotate the bot tokens

Do this before T43 (the current tokens were pasted into a chat transcript) and whenever a token may have leaked.

1. BotFather → `/revoke` → choose the bot. The old token stops working at once; the bot goes silent until step 2.
2. On the Mac:
   ```sh
   ~/.local/bin/python3.12 -I scripts/install.py --env-only --rotate-tokens
   ```
   It asks for each existing token with hidden input, refuses the old value and anything that is not a BotFather
   token, replaces only the token lines (every other line, comment and id stays), keeps the files 0600, and restarts
   the loaded bots with `kickstart -k`. The supervisor reads `dev.env` at send time, so it needs no restart.
3. Run the check: `getMe` must answer for each bot.

Inside a full install, `--rotate-tokens` does the same at the env step.

## 7. How the owner checks each thing (observability)

| Question | Where to look |
|---|---|
| Is everything set up right? | the check script (§1 step 4): green `OK`, red `XATO`, neutral `ESLATMA`; exit code 1 on any red |
| Is the bot alive? | the pinned "Onlayn: HH:MM" message in the dev group (edited every 5 min); "holat?" in chat |
| Are the jobs loaded and running? | `launchctl print gui/$UID/uz.yordamchi.dev` (`state = running`, `pid`, `runs`) |
| Heartbeats | `DATA/run/{supervisor,dev,personal}.heartbeat.json`; the supervisor writes every 15 s, a bot after every poll. The check calls the supervisor's stale after 60 s and a bot's after 120 s |
| Did it crash or restart? | the supervisor posts to the dev group: "…ni qayta ishga tushirdim: <sabab>", a crash-loop alert (≥ 3 starts in 10 min, at most once per 30 min, with the last line of `run/<bot>.lasterror`), and failed restarts. The bot itself posts "Qayta ishga tushdim…" after an unclean exit |
| Errors | `LOGS/<process>.jsonl` (JSON lines, scrubbed, no message content). `grep '"level": "error"' LOGS/dev.jsonl \| tail`. Startup refusals land in `DATA/run/<bot>.lasterror` and `LOGS/uz.yordamchi.<bot>.launchd.log` |
| Disk | the dev bot warns below `guard.min_free_disk_gb` (once per 6 h) and queues coding turns below `guard.block_disk_gb`; the supervisor alerts when a backup is skipped for lack of space; the check prints free space |
| Backups | `ls DATA/backups/daily/` (one dir per day); a failed or skipped backup is alerted in the dev group; restore: §5 |
| Power | the check's "Quvvat" line (`pmset` AC sleep must be 0); after a sleep the bot posts "Mac uxlagan edi …" |

Log rotation (NFR-CAP-05, ≤ 200 MB total): bots 20 MB × 4, supervisor 10 MB × 2, launchd logs copy-truncated by
the supervisor above 5 MB (`.1` keeps the last 1 MB).

## 8. Housekeeping

- Old releases stay until removed by hand (they are the rollback targets). Keep at least the last two:
  ```sh
  cd "$HOME/Library/Application Support/Yordamchi/releases"
  chmod -R u+w <old-version> && rm -rf <old-version>      # never the one `current` points at
  ```
- `DATA/launchd/*.plist` are the rendered copies of the last install, for diffing against `~/Library/LaunchAgents`.
- `uv` builds the release venvs with `--link-mode clone`, so `chmod -R a-w` never reaches into uv's cache.
