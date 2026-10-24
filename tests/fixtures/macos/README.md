# macOS fixtures

Used by `tests/integration/test_macos.py` and `tests/contract/test_mac_host.py` through
`tests/integration/macos_harness.py`. They were recorded on the owner's Mac (macOS 26, arm64, 16 GB RAM) on
2026-10-24 and sanitized. Session ids, project paths, session names, the user name, emails, account ids,
display/org names, the token fingerprint, `oauthAccount` and ClaudeManager's event history are replaced. Pids,
start times, CLI versions, entrypoints, statuses, utilizations and timestamps are kept as recorded. No real key,
token or session id appears.

## Recorded (sanitized)

| File | Command or source | Notes |
|---|---|---|
| `sysctl_pressure_level.txt` | `/usr/sbin/sysctl -n kern.memorystatus_vm_pressure_level` | `2` (warning). In the same session, also at level 2, `memory_pressure` reported 47 % free and `vm.swapusage` 3.4 of 4 GB used: the level does not follow from the free percentage, so the adapter reads it and never derives it |
| `ps.txt` | `env -i PATH=… LC_ALL=C TZ=UTC /bin/ps -axww -o pid=,ppid=,lstart=,command=` | 25 of 457 lines kept: launchd, Terminal and its login shells, five interactive `claude` sessions, ClaudeManager, Chrome with `claude --chrome-native-host`, and the spike's SDK session (72118) under the python that drove it. `lstart` in UTC has the same form as the registry's `procStart` |
| `sessions/*.json` | `~/.claude/sessions/<pid>.json` (CLI 2.1.292–2.1.310) | the six records for the sessions in `ps.txt`; `72118.json` (`entrypoint: sdk-py`) was rebuilt from the fields printed while it ran, because a clean SDK disconnect removes the file (spike S6). The CLI's `<pid>.<sha256>.key` files are not here, because `*.key` never enters the repo (`.gitignore`); `macos_harness.py` writes a placeholder next to each record |
| `claude_manager/state.json` | `~/Library/Application Support/uz.claudemanager.desktop/state.json` | three accounts, the first active; structure, statuses, utilizations and `resetsAt` formats (chrono, microseconds, `Z`) as recorded |

## Synthetic

| File | What it covers |
|---|---|
| `ps_edge.txt` + `sessions_edge/` | the S6 rules with a bot at pid 7000: its SDK child and grandchild (excluded by ancestry), an SDK orphan of an earlier run (`sdk-py`), the owner resuming the bot's session, sessions named only on the command line (`-r`, `--session-id=`), a stale record on a reused pid, records without `procStart` or with a changed format, malformed and id-less records, a record naming another pid, VS Code (`claude-vscode`), a dead pid's file, `claude --continue`, a python running a file called `claude`, `claude-manager`, a zombie and a line that is not `ps` output |

Memory-pressure edge values, oversized or unreadable files and ClaudeManager field drift are built inside the tests.
