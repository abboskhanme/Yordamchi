# Yordamchi — Deploy, operate, roll back

How Yordamchi is installed on the owner's MacBook, how it is updated and rolled back, where to look when something
is wrong, where the secrets are and how to rotate them. Sources: SPEC §2.2 (layout), §2.5 (startup), §2.8 (heartbeat
and watchdog), §9 (ops), §12 (DoD); STACK §5 (runtime dependencies); the owner's requirement of 2026-10-24 (a copied
file must never be usable: ASSUMPTIONS).

Everything runs as the owner's user. There is no server, no inbound port, no Docker and no Homebrew.

```
DATA = ~/Library/Application Support/Yordamchi        (0700)
LOGS = ~/Library/Logs/Yordamchi
KC   = the macOS login Keychain, service uz.yordamchi: dev-bot-token, personal-bot-token, vault-master-key
UID  = $(id -u)
```

## 0. Rules that apply to every section

- **The real install, an update and a rollback touch the live bots. Do them only with the owner's go-ahead.**
  Agents run `--dry-run` only.
- Run the installer with the uv-managed Python: `~/.local/bin/python3.12 -I scripts/install.py …`.
- Never paste a token into a chat (Telegram, Claude, anywhere). Tokens are typed at the installer's hidden prompt.
- **No secret sits in any file**: not the env files, the DBs, the backups, the logs or the tests. Bot tokens and the
  vault master key are in the login Keychain (`KC`); the env files hold only references such as
  `DEV_BOT_TOKEN=@keychain:uz.yordamchi/dev-bot-token`, and the DBs only ciphertext. A bot refuses to start on a
  plaintext token, and the check script is red on one (§9).
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
   → the vault master key in the Keychain (created once, never overwritten; a legacy `master.key` is imported,
   verified and deleted, §9.2) → env files (tokens into the Keychain, references into the files)
   → `git archive <commit>` into `DATA/releases/<YYYY.MM.DD-sha7>/` with its own `.venv`, made read-only → the supervisor release → ffmpeg →
   DB backup → `alembic upgrade head` on `db/dev.db` and `db/personal.db` → `current` symlinks → plists →
   `launchctl bootstrap` → the check script.
   - Env files: a value already in `dev.env` / `personal.env` is **never** overwritten. Only empty values are asked
     for. Tokens are read hidden and written to the Keychain through `security -i` on stdin (never on a command
     line), read back to verify, and the env file gets the reference. A plaintext token still in a file is moved
     into the Keychain and the step ends `warn`: it sat in a file, so rotate it (§6). `OWNER_ID` and
     `DEV_GROUP_ID` may be left empty and filled in later (§1.1).
   - `DEV_GROUP_ID` must be a `-100…` supergroup id. A basic group changes its id when Telegram upgrades it, and the
     bot would then leave it as a foreign group.
   - `uz.yordamchi.personal` is rendered into `DATA/launchd/` only. It is installed and loaded with
     `--enable-personal` at P3.
4. Run the check from the installed release and fix every red (`XATO`) line:
   ```sh
   "$DATA/releases/current/.venv/bin/python" -I -m yordamchi.bots.cli.check
   ```
   (`python -m yordamchi.check` once its entry point is wired; see `docs/PENDING_WIRING.md`.)

Rollback of a first install: §4.3 (stop and remove the jobs). Nothing outside DATA, the three Keychain items, the
logs and the three plists is created (`~/.config/yordamchi` stays as an empty 0700 dir).

### 1.0 Enabling the personal bot (P3, T77)

```sh
~/.local/bin/python3.12 -I scripts/install.py --dry-run --enable-personal     # first: temp HOME, nothing loaded
~/.local/bin/python3.12 -I scripts/install.py --release <commit> --enable-personal
"$DATA/releases/current/.venv/bin/python" -m playwright install chromium   # once: screenshots (SPEC §9.3 step 6)
```
- The installer asks for the personal bot token (hidden) if `personal.env` has none, puts it in the Keychain
  (`personal-bot-token`), copies `uz.yordamchi.personal.plist` to `~/Library/LaunchAgents` and loads it. From then
  on the check script requires it loaded and its heartbeat fresh, like the dev bot's.
- At every start the bot asks Claude one short throwaway question with the exact tool list of an answer
  (NFR-SEC-08). A session whose tools are not the allowlist refuses the start (exit 78, `run/personal.lasterror`,
  one message to the owner per 6 h); that refusal is remembered in `run/personal.tools-refused.json` per release and
  `claude` version, so launchd's restarts ask no model until a new release, a `claude` update or 6 h.
- Speech keys are per bot: paste a Gemini or OpenRouter key into the personal DM once (QOLLANMA §11).

### 1.1 Finding OWNER_ID and DEV_GROUP_ID

1. Stop the dev bot if it is running (`launchctl bootout gui/$UID/uz.yordamchi.dev`), or `getUpdates` answers
   "conflict".
2. Write the bot a private message, and in the dev group write a message that mentions it (`@OrvionXBot salom`).
3. Run `"$DATA/releases/current/.venv/bin/python" -I -m yordamchi.bots.cli.check --discover-ids`. It calls
   `getUpdates` once without an offset (nothing is confirmed, so the bot still receives those messages later),
   prints the candidate ids and offers to write the empty ones into `dev.env` (and `OWNER_ID` into
   `personal.env`). An id already in the file is never replaced.
4. Start the bot again: `launchctl bootstrap gui/$UID ~/Library/LaunchAgents/uz.yordamchi.dev.plist`.

### 1.2 Installing from a GitHub clone, and upstream updates

- **Any folder.** Clone anywhere and run `scripts/install.py` from that clone (or pass `--repo`). The installer writes
  that checkout into the supervisor plist as `YORDAMCHI_REPO_DIR`; without it the supervisor assumes
  `~/Documents/Projects/Yordamchi`. Moving the clone later means running the installer again from the new place.
- **Updates every 3 days.** The supervisor fetches `origin main` into the clone and, when the running release is behind,
  runs an ordinary self-update (build, `make check` offline, drain, switch, probation, rollback): the bots first, the
  supervisor in a second update. It never pushes. It moves the local `main` only as a fast-forward on a clean copy;
  local commits GitHub does not have stop it (local work wins). Offline is fine: it retries after an hour, silently.
- **See it.** The owner gets one Telegram line per outcome ("Yordamchi GitHub'dan avtomatik yangilandi …" or "…
  o'rnatilmadi: …"). Details: `grep '"upstream_' LOGS/supervisor.jsonl`; the next check time is `upstream.next_at` in
  `DATA/supervisor/state.json`; each update has its `DATA/updates/upstream-*/status.json`.
- **Stop it.** `git -C <clone> remote rename origin github` (or remove `origin`); the job then logs
  `upstream_no_origin` and does nothing. Rename it back to resume.
- **Chat self-update** (`o'zingga … qo'sh`) still finds the copy through the project registry: the clone must be a
  folder named `Yordamchi` under `projects.root` (default `~/Documents/Projects`; `sozlama: projects.root <folder>`).

## 2. What runs where

| Job | Program | Plist | Logs |
|---|---|---|---|
| `uz.yordamchi.dev` | `DATA/releases/current/.venv/bin/python -I -m yordamchi.bots.dev.app` | `~/Library/LaunchAgents/uz.yordamchi.dev.plist` | `LOGS/dev.jsonl` (+ `.1`–`.3`), `LOGS/uz.yordamchi.dev.launchd.log` |
| `uz.yordamchi.personal` (P3) | `… -m yordamchi.bots.personal.app` | `~/Library/LaunchAgents/` with `--enable-personal` (§1.0), else staged in `DATA/launchd/` | `LOGS/personal.jsonl`, `LOGS/uz.yordamchi.personal.launchd.log` |
| `uz.yordamchi.supervisor` | `DATA/supervisor/current/.venv/bin/python -I -m yordamchi_supervisor` | `~/Library/LaunchAgents/uz.yordamchi.supervisor.plist` | `LOGS/supervisor.jsonl` (+ `.1`), `LOGS/uz.yordamchi.supervisor.launchd.log` |

Plist facts (SPEC §9.1): `KeepAlive` and `RunAtLoad` true, `ThrottleInterval` 10, `ExitTimeOut` 20 for the bots
and 10 for the supervisor, `AbandonProcessGroup` false (children die with the bot), `WorkingDirectory` `DATA/run`.
`EnvironmentVariables`: `YORDAMCHI_DATA_DIR`, for the bots also `YORDAMCHI_ENV_FILE`, for the supervisor also
`YORDAMCHI_REPO_DIR` (the clone, §1.2), `PATH`
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

Re-running the installer on the same commit is safe: a built release is reused, the Keychain items and env values
are kept, and the jobs are restarted with `kickstart -k`.

### 3.1 The forum-topics release (`0002`, D4 revised 2026-10-26)

- **Schema.** The installer runs `alembic upgrade head`, which applies `0002 topics_threads`: two new tables
  (`project_topics`, `topic_chats`) and two nullable columns (`bursts.thread_id`, `outbox_messages.thread_id`).
  Expand-only: the previous release runs on it unchanged, so the code rollback (§4.1) needs no DB restore.
- **Nothing changes until the owner acts.** With Topics off the bot is the flat group it was: no topic is opened,
  no request carries a thread. The owner's one-time steps are in `docs/QOLLANMA.md` §2a: Topics on, the bot's
  "Manage topics" right, then any message in the group.
- **Check.** `python -m yordamchi.check` shows `dev guruh: Topics yoqilgan (is_forum)` (or an info line while it is
  off) and, with Topics on, `dev guruh: bot mavzu ocha oladi (can_manage_topics)`; the second is red without the
  right (the work then stays in General and the owner is told once in the group).
- **Smoke test after the owner's steps.** In General: "BonviHR'da README'ni tekshir" → a "BonviHR" topic appears with
  the echo and the live line in it, and General shows one "→ BonviHR · mavzusida davom etadi" line. In that topic:
  "to'xta" stops BonviHR only.
- **Switch off without a release:** `sozlama: topics.enabled false` in the group (from the next message).

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

Never delete the Keychain item `uz.yordamchi/vault-master-key`: the vault in the DBs (and in every backup) cannot be
decrypted without it. To uninstall completely, and only then:
`security delete-generic-password -s uz.yordamchi -a <dev-bot-token|personal-bot-token|vault-master-key>`.

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
A restored DB opens with the same Keychain master key; a DB copied to another Mac (or another user) holds only
ciphertext it cannot open there. Then run the check and "holat?". The restore is tested:
`supervisor/tests/test_backups.py::Restore` runs exactly
these steps on a scratch copy and compares every row. Practise it once on a copy (`cp -R DATA/db /tmp/restore-test`)
after T43, so the first real restore is not the first one.

## 6. Rotate the bot tokens

Do this before T43 (the current tokens were pasted into a chat transcript), whenever a token may have leaked, and
whenever a bot refuses to start with "… ochiq holda yozilgan" (a plaintext token was found in an env file).

1. BotFather → `/revoke` → choose the bot. The old token stops working at once; the bot goes silent until step 2.
2. On the Mac:
   ```sh
   ~/.local/bin/python3.12 -I scripts/install.py --env-only --rotate-tokens
   ```
   It asks for each existing token with hidden input, refuses the old value (read from the Keychain) and anything
   that is not a BotFather token, **updates the Keychain item** (`security -i` on stdin, then read back), and leaves
   the env files as they are: they hold only the reference. A plaintext token line still in a file is replaced by
   its reference. The files stay 0600 and the loaded bots are restarted with `kickstart -k`. The supervisor reads
   the token from the Keychain at send time, so it needs no restart.
3. Run the check: the Keychain lines and `getMe` must be green for each bot.

Inside a full install, `--rotate-tokens` does the same at the env step.

## 7. How the owner checks each thing (observability)

| Question | Where to look |
|---|---|
| Is everything set up right? | the check script (§1 step 4): green `OK`, red `XATO`, neutral `ESLATMA`; exit code 1 on any red |
| Is the bot alive? | the pinned "Onlayn: HH:MM" message in the dev group (edited every 5 min); "holat?" in chat |
| Are the jobs loaded and running? | `launchctl print gui/$UID/uz.yordamchi.dev` (`state = running`, `pid`, `runs`) |
| Heartbeats | `DATA/run/{supervisor,dev,personal}.heartbeat.json`; the supervisor writes every 15 s, a bot after every poll. The check calls the supervisor's stale after 60 s and a bot's after 120 s |
| Did it crash or restart? | the supervisor posts to the dev group: "…ni qayta ishga tushirdim: <sabab>", a crash-loop alert (≥ 3 starts in 10 min, at most once per 30 min, with the last line of `run/<bot>.lasterror`), and failed restarts. The bot itself posts "Qayta ishga tushdim…" after an unclean exit |
| Is it cut off from Telegram? | after 10 min offline a banner on the Mac: "… daqiqadan beri Telegram'ga ulana olmayapti" (once per outage; macOS shows it as from Script Editor, so allow its notifications). `consecutive_poll_failures` in `run/<bot>.heartbeat.json`. If `curl https://api.telegram.org` fails while `dig api.telegram.org` answers, the macOS resolver is stuck: `sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder`. The bots renew their connection every 5 min, so no restart is needed |
| Errors | `LOGS/<process>.jsonl` (JSON lines, scrubbed, no message content). `grep '"level": "error"' LOGS/dev.jsonl \| tail`. Startup refusals land in `DATA/run/<bot>.lasterror` and `LOGS/uz.yordamchi.<bot>.launchd.log` |
| Disk | the dev bot warns below `guard.min_free_disk_gb` (once per 6 h) and queues coding turns below `guard.block_disk_gb`; the supervisor alerts when a backup is skipped for lack of space; the check prints free space |
| Backups | `ls DATA/backups/daily/` (one dir per day); a failed or skipped backup is alerted in the dev group; restore: §5 |
| Power | the check's "Quvvat" line (`pmset` AC sleep must be 0); after a sleep the bot posts "Mac uxlagan edi …" |
| Are the secrets safe? | the check's "Maxfiy qiymatlar (Keychain)" section: no plaintext in any env file, every referenced item present, the master key in the Keychain, no `master.key` file left (§9) |

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

## 9. Secrets: where they are and what to do when something is off

**Where.** Everything that could be used if copied lives in the owner's login Keychain, as generic passwords of the
service `uz.yordamchi`, each created with `-T /usr/bin/security` so a launchd job reads it without a prompt:

| Item (account) | What | Read by | Written by |
|---|---|---|---|
| `dev-bot-token` | the dev bot token | the dev bot, the supervisor (alerts), the check script | installer |
| `personal-bot-token` | the personal bot token | the personal bot, the check script | installer |
| `vault-master-key` | 32 random bytes, base64: AES-256-GCM key of the vault and the OAuth grants | both bots | installer (or a bot importing a legacy `master.key`) |
| `google-api-key`, `openrouter-api-key` | keys for the opt-in live tests only (`tests/integration/stt_harness.live_key`) | `pytest -m live` | the owner, by hand (below) |

Files hold no usable secret: env files hold `@keychain:` references, the DBs and their backups hold ciphertext,
`run/secret-hashes.json` holds one-way sha256 hashes for the pre-push scan, logs are scrubbed. The Keychain is
encrypted with the login password and is not part of a file copy or of Time Machine. The cost: the vault in a backup
opens only on this Mac's login Keychain. If the Keychain item is lost, the stored API keys and integration logins are
lost with it; send the keys again by chat and reconnect the integrations.

**Look, without showing a value.** `security find-generic-password -s uz.yordamchi -a dev-bot-token` prints the
item's attributes, never its value (that needs `-w`; do not run `-w` where a screen is shared or recorded). The check
script does the same for every item.

**A bot refuses to start.** The reason is in `DATA/run/<bot>.lasterror` and the launchd log, in Uzbek:
- "… faylida … ochiq holda yozilgan": a plaintext secret in the env file → §6 (rotate; the installer moves it into
  the Keychain and leaves only the reference).
- "macOS Keychain'da «…» yozuvi topilmadi": the item is gone → §6 (`--env-only --rotate-tokens` stores a new one).
- "macOS Keychain'dan «…» o'qilmadi": the Keychain is locked or `security` did not answer → log in at the Mac (after
  a reboot nothing runs until the owner logs in anyway, R-02), then run the check. If the check is still red and the
  item was edited by hand in Keychain Access, its access list may now prompt: store it again with the installer.
- "«…» bu botniki emas": an env file points at another bot's item (the personal bot may never read the dev token,
  UC-36) → fix the reference, e.g. `PERSONAL_BOT_TOKEN=@keychain:uz.yordamchi/personal-bot-token`.

**The vault is degraded** ("Kalitlar ombori ochilmadi"): the bot runs, but it cannot store or open keys until the
master key can be read. Log in and run the check; it names what is missing.

### 9.1 Add a live-test key without a file

```sh
read -rs KEY    # paste the key, then Enter; nothing is echoed
printf 'add-generic-password -U -s uz.yordamchi -a openrouter-api-key -T /usr/bin/security -w "%s"\n' "$KEY" \
  | /usr/bin/security -i
unset KEY
```
`printf` is a shell builtin, so the key is never on a process command line. A shell variable
`YORDAMCHI_LIVE_OPENROUTER_API_KEY` still wins over the item when it is set.

### 9.2 The legacy `master.key` file

Until 2026-10-24 the master key was `~/.config/yordamchi/master.key`. It is moved into the Keychain by the next
install, or by the first bot start that needs the vault: written to `vault-master-key`, read back, compared byte for
byte, and only then deleted. The check is red while the file exists. A file readable by others (`chmod 600` it first)
or one that is not base64 of 32 bytes is never imported and never deleted.

**Conflict** (the installer stops with "holds a different key", or the bot logs `master_key_conflict`): the
Keychain already has a different key than the file. Nothing was written or deleted. Find out which key the vault
rows were sealed with, without printing either key:
```sh
cd "$HOME/Library/Application Support/Yordamchi"
sqlite3 -readonly db/dev.db 'select distinct key_fp from vault_secrets'                    # the rows' key
base64 -d < ~/.config/yordamchi/master.key | shasum -a 256 | cut -c1-8                     # the file's key
security find-generic-password -s uz.yordamchi -a vault-master-key -w | base64 -d | shasum -a 256 | cut -c1-8
```
If the rows show one fingerprint and it is the file's, delete the Keychain item
(`security delete-generic-password -s uz.yordamchi -a vault-master-key`) and run the installer: it imports the file
and deletes it only once the Keychain holds the same key. If it is the Keychain's, delete the file. If the rows show
two fingerprints, or neither matches, delete nothing: each key still opens some rows.
