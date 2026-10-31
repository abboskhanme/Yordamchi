"""Upstream updates (SPEC §3.2.12 "Upstream updates"): an installation follows the GitHub `main` it was cloned from.

Every `CHECK_EVERY` (3 days) the `upstream` job fetches `origin main` into the working copy (`paths.repo`) and, when
the running release is strictly behind the fetched commit, queues an ordinary self-update for the updater
(`updates/<id>/request.json`, `source: "upstream"`). The updater then does what it does for a chat request: build from
`git archive`, `make check` offline, drain, snapshot, migrate, switch, probation, rollback. Only finishing differs: no
push, and the local `main` is fast-forwarded only when that loses nothing (`Updater._fast_forward_upstream`).

| situation | what happens |
|---|---|
| not due, or an update in progress | nothing (the check waits until the update is over) |
| no `origin` remote / no git repo | logged, next check in `CHECK_EVERY` |
| fetch fails (offline, DNS, auth) | logged (never alerted), retried after `RETRY_AFTER` |
| running release == `origin/main` | nothing |
| running release strictly behind | one request per side that changed (see R-32 below) |
| local `main` or the running release ahead of / diverged from `origin/main` | logged, nothing (local work wins) |
| no local `main` (a clone of another branch, a detached checkout) | `main` is created at `origin/main`, then as above |
| a side was already tried on this commit and is still behind (refused, rolled back) | held until GitHub moves on |

R-32 (never the app and the supervisor in one update): the app goes first and ignores `supervisor/**`; once it runs
the fetched commit, the next tick queues `kind=supervisor`, which ignores everything else. A failed app half holds the
supervisor half, so a new supervisor never drives an app it was not tested with.

The outcome of a queued request is told to the owner once, by this job (`S.UPSTREAM_*`), because the dev bot follows
only the updates it requested itself. A rollback the updater already alerted is not told again.

Platform-neutral on purpose (owner, 2026-10-31): only git (argv lists, no shell), file I/O and the state and updates
helpers. No launchd, no macOS tool.
"""

import re
import secrets
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Final

from yordamchi_supervisor import releases as rel
from yordamchi_supervisor import strings as S
from yordamchi_supervisor.log import JsonLog
from yordamchi_supervisor.paths import SupervisorPaths
from yordamchi_supervisor.proc import CommandRunner, tool_env
from yordamchi_supervisor.state import StateFile, parse_instant
from yordamchi_supervisor.telegram import Alerter
from yordamchi_supervisor.updater import STATE_SECTION as UPDATE_SECTION
from yordamchi_supervisor.updater import UPSTREAM, UPSTREAM_REF, read_status, write_json_atomic
from yordamchi_supervisor.updates import scan

INTERVAL: Final = timedelta(minutes=1)  # the job's cadence; a tick that is not due reads one file
CHECK_EVERY: Final = timedelta(days=3)
RETRY_AFTER: Final = timedelta(hours=1)  # after a failed fetch
TIMEOUT_FETCH_S: Final = 120.0
STATE_SECTION: Final = "upstream"
REMOTE: Final = "origin"
MAIN: Final = "main"
KINDS: Final = ("app", "supervisor")  # the order R-32 splits a mixed range in
# Never a prompt: no terminal credential question, ssh in batch mode (a passphrase-protected key fails instead).
FETCH_ENV: Final = {"GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -o BatchMode=yes"}
_SHA: Final = re.compile(r"[0-9a-f]{40}")


def _sha(value: object) -> str | None:
    return value if isinstance(value, str) and _SHA.fullmatch(value) else None


def is_mine(kind: str, path: str) -> bool:
    """A changed path the `kind` side of an update carries."""
    return path.startswith("supervisor/") == (kind == "supervisor")


class Upstream:
    def __init__(
        self,
        paths: SupervisorPaths,
        *,
        runner: CommandRunner,
        alerter: Alerter,
        log: JsonLog,
        state: StateFile,
    ) -> None:
        self._paths = paths
        self._runner = runner
        self._alerter = alerter
        self._log = log
        self._state = state

    # --- the job --------------------------------------------------------------------------------------------------
    def tick(self, now: datetime) -> None:
        section = self._state.section(STATE_SECTION)
        before = dict(section)
        self._report(section)
        due = self._due(section, now)
        if (due or section.get("pending") is True) and not self._busy(now):
            if due:
                self._check(section, now)
            if section.get("pending") is True:
                self._queue_next(section, now)
        if section != before:
            self._state.save_section(STATE_SECTION, section)

    @staticmethod
    def _due(section: Mapping[str, object], now: datetime) -> bool:
        at = parse_instant(section.get("next_at"))
        # A clock set backwards must not push the next check further than one period away.
        return at is None or now >= at or at - now > CHECK_EVERY

    def _busy(self, now: datetime) -> bool:
        if self._state.section(UPDATE_SECTION).get("journal") is not None:
            return True
        return scan(self._paths.updates, now).active is not None

    # --- git ------------------------------------------------------------------------------------------------------
    def _git(self, *args: str, timeout: float = rel.TIMEOUT_GIT_S, env: Mapping[str, str] | None = None) -> str | None:
        result = self._runner.run(
            [rel.GIT, "-C", str(self._paths.repo), *args], timeout=timeout, env=tool_env(self._paths.home, env)
        )
        return result.text() if result.ok else None

    def _commit(self, ref: str) -> str | None:
        return _sha(self._git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"))

    def _is_ancestor(self, older: str, newer: str) -> bool:
        return self._git("merge-base", "--is-ancestor", older, newer) is not None

    # --- check: fetch origin main ---------------------------------------------------------------------------------
    def _check(self, section: dict[str, object], now: datetime) -> None:
        if self._git("remote", "get-url", REMOTE) is None:
            section["next_at"] = (now + CHECK_EVERY).isoformat()
            self._log.info("upstream_no_origin", repo=str(self._paths.repo))
            return
        fetched = self._git(
            "fetch",
            "--quiet",
            "--no-tags",
            REMOTE,
            f"+refs/heads/{MAIN}:{UPSTREAM_REF}",
            timeout=TIMEOUT_FETCH_S,
            env=FETCH_ENV,
        )
        target = self._commit(UPSTREAM_REF) if fetched is not None else None
        if target is None:
            failures = section.get("failures")
            count = (failures if isinstance(failures, int) and not isinstance(failures, bool) else 0) + 1
            section["failures"] = count
            section["next_at"] = (now + RETRY_AFTER).isoformat()
            self._log.warning("upstream_fetch_failed", failures=count)  # never alerted: an offline Mac is normal
            return
        section["failures"] = 0
        section["checked_at"] = now.isoformat()
        section["next_at"] = (now + CHECK_EVERY).isoformat()
        section["target"] = target
        section["pending"] = True
        self._log.info("upstream_fetched", commit=target[:12])

    # --- decide and queue -----------------------------------------------------------------------------------------
    def running_commit(self, kind: str) -> str | None:
        """The commit the running `kind` release was built from: its marker, else the updater's `last_good`."""
        app = kind == "app"
        link = self._paths.current_release if app else self._paths.supervisor_current
        root = self._paths.releases if app else self._paths.supervisor_releases
        name = rel.link_target(link)
        marked = rel.marker_commit(root / name) if name else None
        if marked:
            return marked
        good = self._state.section(UPDATE_SECTION).get("last_good")
        entry = good.get(kind) if isinstance(good, dict) else None
        return _sha(entry.get("commit")) if isinstance(entry, dict) else None

    def _needed(self, target: str) -> list[str] | str:
        """The sides behind `target`, in R-32 order; or why nothing may be queued."""
        main = self._commit(f"refs/heads/{MAIN}") or self._create_main(target)
        if main is None:
            return "no_local_main"
        if main != target and not self._is_ancestor(main, target):
            return "local_ahead"  # the owner's local `main` has commits GitHub does not: their work wins
        needed: list[str] = []
        for kind in KINDS:
            base = self.running_commit(kind)
            if base is None:
                return f"no_base_{kind}"
            if base == target:
                continue
            if not self._is_ancestor(base, target):
                return f"running_ahead_{kind}"  # a release from local work GitHub does not have
            changed = rel.changed_paths(self._runner, self._paths.repo, base, target, env=tool_env(self._paths.home))
            if changed is None:
                return "git_failed"
            if any(is_mine(kind, path) for path in changed):
                needed.append(kind)
        return needed

    def _create_main(self, target: str) -> str | None:
        """A clone without a local `main` (`clone -b <other>`, a detached checkout) gets one at the fetched commit, so
        local work stays comparable and the updater can fast-forward it later. `update-ref` with an empty old value
        only creates: an existing branch is never moved, and neither HEAD nor the working tree is touched."""
        if self._git("update-ref", f"refs/heads/{MAIN}", target, "") is None:
            return None
        self._log.info("upstream_main_created", commit=target[:12])
        return target

    def _queue_next(self, section: dict[str, object], now: datetime) -> None:
        target = _sha(section.get("target"))
        if target is None:
            section["pending"] = False
            return
        needed = self._needed(target)
        if isinstance(needed, str) or not needed:
            section["pending"] = False
            self._log.info("upstream_nothing_to_do", reason=needed or "up_to_date", commit=target[:12])
            return
        kind = needed[0]
        tried = section.get("tried")
        table = {str(k): v for k, v in tried.items()} if isinstance(tried, dict) else {}
        if table.get(kind) == target:
            section["pending"] = False  # it failed on this commit; a newer GitHub commit is tried again
            self._log.warning("upstream_held", kind=kind, commit=target[:12])
            return
        update_id = self._write_request(kind, target, now)
        table[kind] = target
        section["tried"] = table
        section["queued"] = update_id
        self._log.info("upstream_queued", update_id=update_id, kind=kind, commit=target[:12], waiting=needed[1:])

    def _write_request(self, kind: str, target: str, now: datetime) -> str:
        update_id = f"upstream-{kind}-{target[:7]}-{now:%Y%m%d%H%M%S}"
        if self._paths.update_dir(update_id).exists():
            update_id = f"{update_id}-{secrets.token_hex(2)}"
        body: dict[str, object] = {
            "id": update_id,
            "kind": kind,
            "commit": target,
            "branch": f"selfupdate/upstream-{target[:12]}",
            "source": UPSTREAM,
            "requested_at": now.isoformat(),
        }
        write_json_atomic(self._paths.update_dir(update_id) / "request.json", body)
        return update_id

    # --- tell the owner -------------------------------------------------------------------------------------------
    def _report(self, section: dict[str, object]) -> None:
        queued = section.get("queued")
        if not isinstance(queued, str) or not queued:
            return
        directory = self._paths.update_dir(queued)
        status = read_status(directory)
        if status is None and directory.is_dir():
            return  # not picked up yet
        if status is not None and status.get("final") is not True:
            return
        section.pop("queued", None)
        if status is None:
            self._log.warning("upstream_request_vanished", update_id=queued)
            return
        text = self.outcome_text(status)
        if text is not None:
            self._alerter.send("upstream_update", text)
        self._log.info("upstream_update_done", update_id=queued, status=status.get("status"), told=text is not None)

    @staticmethod
    def outcome_text(status: Mapping[str, object]) -> str | None:
        outcome = status.get("status")
        version = status.get("version")
        code = status.get("result_code")
        version_text = version if isinstance(version, str) and version else "?"
        reason = S.update_reason(code if isinstance(code, str) and code else "unknown")
        if outcome in ("merged", "healthy"):
            part = S.UPSTREAM_PARTS.get(str(status.get("kind")), S.UPSTREAM_PARTS["app"])
            text = S.UPSTREAM_UPDATED(part=part(), version=version_text)
            return text + (S.UPSTREAM_MAIN_LEFT(reason=reason) if outcome == "healthy" else "")
        if status.get("alerted") is True:
            return None  # the updater already told the owner about the rollback
        return S.UPSTREAM_NOT_APPLIED(version=version_text, reason=reason)
