# Yordamchi — Week-1 spikes S1–S6 (SPEC §11, TASKS T03–T06)

Run 2026-10-23/24 on the owner's Mac against the real `~/.local/bin/claude` **2.1.309** with `claude-agent-sdk`
**0.2.173** (Python 3.12.15, `cli_path` always set, keychain login, `CLAUDECODE`/`CLAUDE_CODE_*` scrubbed, no
credential variables). Every session ran in a fresh scratch dir; no orphan `claude` process was left.
Models: `haiku` (Haiku 5.5) unless stated; S4 was repeated on `sonnet`.

Each spike has a live test in `tests/live/test_spike_s<n>_*.py` (`pytest -m live tests/live`). The tests read the
raw stream (`client._query.receive_messages()`), because the SDK parser drops some message types (see N1).
Last run: 14 live tests (S1 4, S2 3, S3 3, S4 2, S5 2, S6 1; about 4 min, haiku). 13 passed on the full run. The
one miss was a haiku compliance slip in an S1 content check, with the merge itself correct; that check was
hardened, and S1, S3 and S4 then passed on rerun. Model-graded assertions use tags or single words to stay stable.
The default `pytest` run deselects all 14.

| Spike | Question | Result | Decision |
|---|---|---|---|
| S1 | `priority:"next"` merges a follow-up into the running turn? | **Pass** | Use `next` for burst follow-ups. `now` is not a safe redirect (below) |
| S2 | `interrupt()` kills a running Bash command within 2 s? | **Pass** (0.055 s; 1.54 s if SIGTERM is ignored) | Keep the 1 s process-group kill only as a backstop, aimed at the tool's group |
| S3 | PreToolUse in subagents, hook deny, `AskUserQuestion` → `can_use_tool` under bypass? | **Pass** (subagent `AskUserQuestion` is unavailable, as documented) | As designed; questions come only from the main thread |
| S4 | `UserPromptSubmit` `additionalContext` changes persona on a resumed session? | **Pass only with a design change**. An update that conflicts with the frozen system prompt is unreliable | No rotation. No owner-changeable instruction (persona, language, style) goes in the system prompt; every turn sends the current block as `additionalContext` (10/10) |
| S5 | `--restricted` keeps WebSearch, WebFetch, SDK MCP? Exact `init` tool list? | **Pass** | Add `--restricted` to the personal session. Relax C2 equality to a subset rule |
| S6 | SDK sessions in `~/.claude/sessions/*.json` (`kind`/`entrypoint`)? | **Pass** | Identify own sessions by `entrypoint == "sdk-py"` plus pid ancestry; `kind` is useless |

## S1 — follow-up priority on the streaming input

**Method.** `ClaudeSDKClient`, `tools=["Bash"]`, bypass. Prompt: run `sleep 8 && echo first-done`. 1.5 s after the
Bash `tool_use` arrives, a second user dict (with a `uuid`) goes through `client.query(<async iterable>)`. The SDK
passes unknown dict fields through unchanged.

| Variant (sleep) | Event sequence (s since connect) | Results |
|---|---|---|
| `priority:"next"` (8 s) | 2.8 tool_use · 4.3 sent → `command_lifecycle queued` · 10.9 tool_result · 10.9 `started` · 12.4 text "…first-done… PINEAPPLE" · 12.5 `completed` · 12.5 result `success/completed`, `num_turns=2` | **1**, follow-up obeyed |
| no `priority` field (8 s) | identical to `next` | **1** |
| `priority:"later"` (8 s) | result 1 at 11.5 ("first-done"), then a new turn: `started`, `system/init` again, result 2 at 12.6 ("…PINEAPPLE") | 2 |
| `"now"` + `origin {"kind":"human"}` (20 s) | 4.5 sent · 6.2 `task_updated {is_backgrounded:true}` · 7.1 tool_result "Command was moved to the background…" · 7.1 result 1 `success`, `stop_reason=tool_use`, `terminal_reason=aborted_tools`, `result=""` · 7.1 `started`, `init` · 8.8 result 2 "REDIRECTED" · **23.1** background shell finishes → `task_notification` → CLI-started turn → result 3 ("The background sleep finished…") | **3** |
| `"now"`, no origin (20 s) | the Bash call was **not** stopped: tool_result "first-done" at 22.9 (full 20 s) · result 1 `aborted_tools`, empty · result 2 "REDIRECTED" at 24.2 | 2 |

**Answers.** Merged: yes, for `next` and for no field (the follow-up is read once the running tool finishes; one
`ResultMessage`). `now`+human does redirect within ~4 s, but it leaves the shell running in the background, emits an
empty aborted result first, and the backgrounded command later triggers an unsolicited extra turn and result.
`now` without origin waits for the tool and then splits the turn.

**Decision.** S1 passes, so the SPEC §11 fallback is not needed. Burst follow-ups use `next` (send it explicitly;
do not rely on the default). For "yo'q, boshqacha…" use `interrupt()` (S2) followed by a `new_turn`, not `now`.

## S2 — `interrupt()` while Bash runs

**Method.** The command writes its own pid to `pid.txt` and sleeps 30 s in Python (`python3 -c …time.sleep(30)`).
2 s after `task_started` the interrupt control request is sent; `os.kill(pid, 0)` is polled every 20–50 ms.

| Variant | Command's process gone after | Notes |
|---|---|---|
| plain | **0.055 s** (shell and python) | result `error_during_execution`, `is_error=true`, `stop_reason=tool_use`, `terminal_reason=aborted_tools`; `task_notification status=stopped`; user text `[Request interrupted by user for tool use]` |
| SIGTERM/SIGINT/SIGHUP ignored | shell 0.056 s, python **1.536 s** | the CLI escalates to SIGKILL after ~1.5 s |
| `cmd1 & cmd2` (background child in the same command) | 0.054 s, both | |
| `cancel_queued:true` with a queued follow-up | 0.06 s | receipt `{"still_queued": [], "cancelled": ["<uuid>"]}`; lifecycle `queued → cancelled`, never `started` |

The interrupt control request answers in 2–9 ms with the receipt `{"still_queued": [...]}`.

**Process groups.** The CLI shares the caller's process group (the SDK spawns it without `setsid`:
`cli_pgid == bot pgid`). The Bash tool's shell (`/bin/zsh -c source ~/.claude/shell-snapshots/…`) leads its own
group (`pgid == shell pid`).

**Decision.** Pass. T10's "kill the process group if still busy" must **never** call `killpg(getpgid(cli_pid))`:
that kills the bot itself. Either spawn the CLI in its own group (a custom transport or wrapper), or kill the
descendants' groups found from the CLI pid. Keep it as a backstop after 2 s, not 1 s: the CLI's own SIGKILL
escalation lands at ~1.5 s. Residual: a command that calls `setsid`/daemonizes escapes both.

## S3 — hooks and questions under `bypassPermissions`

**Method.** `tools=["Bash","Agent","AskUserQuestion"]`, bypass, `PreToolUse` hook with `matcher=None` that denies
Bash calls containing a marker, plus a `can_use_tool` that answers questions.

- **Main-thread deny holds.** tool_result `is_error=true`, text `PreToolUse:Bash hook error: SPIKE_GATE_DENIED`.
  The file was not created. `ResultMessage.permission_denials` lists the call. Hook input has
  `permission_mode="bypassPermissions"` and no `agent_id`.
- **Subagent tool calls hit the hook.** Hook input carries `agent_id="a78187…"` and `agent_type="general-purpose"`.
  The subagent's first command ran; the denied one did not (file absent). The denial shows in the parent result's
  `permission_denials`.
- **`AskUserQuestion` reaches `can_use_tool`.** The hook sees it first (allow), then `can_use_tool` is called. It
  was the only `can_use_tool` call: bypass auto-approves everything else. Returning
  `PermissionResultAllow(updated_input={**input, "answers": {question: label}})` produced the tool result
  `Your questions have been answered: "Which color do you prefer?"="Blue"`, and the final answer was "Blue".
- **Inside a subagent `AskUserQuestion` is unavailable.** The subagent reported "AskUserQuestion isn't available";
  this matches the docs.
- **SDK warning.** With `can_use_tool` and `bypassPermissions` together, the SDK emits `CanUseToolShadowedWarning` on
  every connect ("can_use_tool will not be invoked …"). The warning is wrong for `AskUserQuestion`, which still
  reaches the callback. Under the repo's `filterwarnings = error` it fails tests, and in the bot it is log noise:
  T11 filters exactly this warning class.
- **`permission_denials` is incomplete for subagents.** It carried the subagent's denial when the subagent ran in
  the foreground. It did not when the model started the subagent in the background. The hook is the reliable
  source of `ToolDenied`.
- **Two side effects:**
  - The model sometimes runs the subagent **in the background** (`task_started is_backgrounded=true`, tool_result
    "Async agent launched successfully"). The main turn then ends with a result while the subagent still runs.
    Its completion starts a second, CLI-initiated turn with its own result.
  - After one gate denial, haiku refused to route the same command through a subagent. Good for the gate, but it
    means a test needs a fresh session per case.

**Decision.** Pass. T11 implements as designed. Questions only ever come from the main thread. The §3.2.2 step 6
path (gate-deny + message) is still needed, for a subagent that wants to ask.

## S4 — persona change on a resumed session

**Method.** A custom `system_prompt`, one turn, close, then `resume=<id>` with a `UserPromptSubmit` hook returning
`additionalContext`. The hook fired on resume. Its input keys: `cwd, hook_event_name, permission_mode, prompt,
prompt_id, session_id, transcript_path`. The session id was unchanged. Answers carry tags ([EN]/[FR]/[DE]), so the
checks are mechanical. A pass means "[FR] and no [EN]" on the first resumed turn.

| Case (resumed turn) | haiku | sonnet |
|---|---|---|
| Neutral context, no conflict ("code word ZEBRA-42, end every answer with it") | 4/4 | 1/1 |
| System prompt "Always answer in English"; hook context "Updated instructions…: only French, tag [FR]" | 3/5 | 1/1 |
| System prompt "English, tag [EN]", plus a clause that an "Updated instructions from the owner" reminder replaces it; update via the hook | 3/6 (an earlier, looser wording got 4/4) | 2/3 |
| Same, but the update is prepended to the user message (no hook) | 3/6 | **0/3** |
| Same, update in both the hook and the message | 6/6 | 1/3 |
| **Neutral frozen system prompt** (only "each turn carries the owner's current instructions in a system reminder; the latest wins"). Turn 1 context: English [EN]. Resumed turn context: French [FR] | **6/6** | **4/4** |
| Resume with a changed `system_prompt` (German), snapshot on (default) | ignored (English) | — |
| Resume with `{"type":"custom","prompt":GERMAN,"snapshot":False}` | took effect ("Gras ist meistens grün…") | — |

**Answers.** `additionalContext` does reach the model on a resumed session. When it contradicts the recorded
system prompt, the system prompt often wins. Sonnet is the most resistant, and it ignores a contradicting user
message entirely. A changed `system_prompt` on resume is ignored (the snapshot), exactly as documented.

**Decision.** No rotation: the §11 fallback is not needed, but SPEC §5.4 must change.
- The frozen system prompt (the custom prompt, or the `append` text) holds only fixed parts: the role, the result
  contract, the authorization, and how to read the per-turn instructions.
- Every owner-changeable instruction (persona, language, tone, assistant instructions) moves out of it. It is sent
  **on every turn** in `turn_context.md` through `additionalContext`, not only when `prompt_version` changes.
- The prompt says "the latest owner-instructions reminder is authoritative".

`snapshot: False` exists (SDK ≥ 0.2.153); it stays off per STACK.

## S5 — `--restricted`, personal-style session

**Method.** `setting_sources=[]`, `tools=["WebSearch","WebFetch"]`,
`allowed_tools=[WebSearch, WebFetch, mcp__ybridge__ping]`, `dontAsk`, `strict_mcp_config=True`, the SDK server
`ybridge` with one tool `ping`, an allowlist PreToolUse hook, and `extra_args={"restricted": None}`. The argv
contained `--restricted`. The flag exists in 2.1.309; `claude --help` describes it.

- **With `--restricted`, all three tools work.** WebSearch returned "3.16 million", WebFetch of example.com returned
  "Example Domain", and `ping` returned `pong:abc123`. `permission_denials` was empty every time. The same held
  without the flag.
- **`init` with and without the flag:** `tools = ["WebFetch","WebSearch","mcp__ybridge__ping"]`,
  `mcp_servers = [{"name":"ybridge","status":"connected","source":"sdk"}]`. **None** of `EndConversation`,
  `ToolSearch`, `WaitForMcpServers` appeared.
- **The flag is really active:**
  - `--restricted` with `bypassPermissions` exits 1: `Error: bypassPermissions not supported in restricted mode`.
  - `--restricted` with the default tool set drops Bash and WebFetch. What remains is `Task, CronDelete, CronList,
    DesignSync, Edit, EnterWorktree, ExitWorktree, Glob, Grep, ListAgents, NotebookEdit, PushNotification, Read,
    ReportFindings, ScheduleWakeup, SendMessage, Skill, TaskStop, ToolSearch, WebSearch, Write`. So `ToolSearch`
    appears only with large tool sets.
- **`init` still lists bundled items with `setting_sources=[]`:** 19 skills (`deep-research`, `design`, …), the
  agents `claude, Explore, general-purpose, Plan, statusline-setup`, and 3 builtin plugins. The owner's
  `~/.claude/skills` are not loaded. The `Skill` and `Agent` tools are absent, so none of these can be called.

**Decision.** Pass, so `--restricted` goes into the personal session's `extra_args`; the session must never use
bypass. The C2 start check must become `allowlist ⊆ init.tools ⊆ allowlist ∪ {EndConversation, ToolSearch,
WaitForMcpServers}`. The strict equality in STACK C2 would refuse a correct session, because the harness tools are
absent here. Check `mcp_servers` names and status too, and ignore `skills`/`agents`/`plugins`.

## S6 — SDK sessions in `~/.claude/sessions/`

**Method.** Read-only. Open a session, read `<cli_pid>.json`, disconnect, read again. Separately, SIGKILL the CLI.

An SDK session's record (the owner's terminal sessions are identical except `"entrypoint": "cli"`):
```json
{"pid": 58896, "sessionId": "8dbccfd0-…", "cwd": "<session cwd>", "startedAt": 1792814116211,
 "procStart": "Sat Oct 24 03:55:16 2026", "version": "2.1.309", "peerProtocol": 1,
 "peerFeatures": ["notify_idle", "reply_across_default_dirs", "artifact_yield"],
 "kind": "interactive", "entrypoint": "sdk-py", "pidDomain": "darwin",
 "messagingSocketPath": "/tmp/cc-socks/58896.sock", "name": "s6-a86f4a-d5", "nameSource": "derived",
 "status": "idle|busy|waiting", "updatedAt": …, "statusUpdatedAt": …}
```
- It appears at connect, before the first query, together with a `<pid>.<sha>.key` file (0600). `sessionId`
  equals the `init` `session_id`. `status` moves `idle → busy`.
- A clean disconnect removes both files.
- After SIGKILL both files stay (stale). The next `claude` start sweeps them, as the next spike session showed.

**Decision.** Pass. `kind` is `"interactive"` for both, so it cannot be used. Mac-attach detection (T54) must:
- treat as the owner's: records with `entrypoint == "cli"` whose `pid` is alive and whose `procStart` matches;
- treat as Yordamchi's: `entrypoint == "sdk-py"`, or a pid that descends from a bot. Check the pid; never trust a
  stale file.

The process scan stays primary, as SPEC planned. Yordamchi may also set its own `CLAUDE_CODE_ENTRYPOINT` through
`options.env` (the SDK lets `env` override `sdk-py`) to tag its sessions. That is untested.

## Notes for T10–T12

- **N1. The SDK parser drops `command_lifecycle`.** The CLI advertises `msg_lifecycle_v1` and emits
  `{"type":"command_lifecycle","command_uuid":<your uuid>,"state":"queued|started|completed|cancelled"}` for every
  user message sent with a `uuid`. `parse_message` returns `None` for it, so `receive_messages()` never shows it.
  T10 should read the raw stream (`client._query.receive_messages()`, private) to learn when a queued input was
  merged. Re-check on SDK upgrades.
- **N2. Turn completion: "TurnResult + 1.5 s silence" is not enough.** Three cases end a turn and later start a new,
  CLI-initiated one with its own result, 2–15 s later:
  - a backgrounded subagent (S3);
  - a shell backgrounded by `now` (S1);
  - any `run_in_background` Bash.

  T10 must track background tasks: `task_started.is_backgrounded`, `task_updated.patch.is_backgrounded`,
  `background_tasks_changed`, `task_notification`. Either complete the turn only when none is outstanding, or deliver
  the late result as a follow-up. Results with `terminal_reason="aborted_tools"` and empty text are never delivered.
- **N3. `system/init` repeats at the start of every turn**, not only at connect. Emit `SessionReady` once and treat
  later inits as updates. Do not re-run the C2 check on each init as if the session were new.
- **N4. `interrupt()` in Python returns `None` and discards the receipt.** `_query._send_control_request(
  {"subtype":"interrupt","cancel_queued":True})` returns `{"still_queued":[…],"cancelled":[…]}` on 2.1.309; it is
  private API. `init.capabilities` lists `interrupt_receipt_v1, interrupt_cancel_queued_v1, interrupt_send_now_v1,
  interrupt_requeue_taken_v1, msg_lifecycle_v1, …`. Feature-detect from these.
- **N5. Interrupted results.** `stop` gives `subtype=error_during_execution`, `is_error=true`,
  `terminal_reason=aborted_tools`. Map it to a stopped turn, not `RuntimeFailed`.
- **N6. Hook denials.** They appear in `ResultMessage.permission_denials` (`tool_name, tool_use_id, tool_input`), but
  not for background subagents. The tool_result text is `PreToolUse:<Tool> hook error: <reason>`. Emit `ToolDenied`
  from the guard bridge itself, the moment it returns deny; the result field is a cross-check only.
- **N7. Account connectors.** Without `ENABLE_CLAUDEAI_MCP_SERVERS=false` and without strict MCP, `init` lists 11
  `claude.ai *` servers (`source:"claudeai"`, status `pending`/`needs-auth`). With the variable, `mcp_servers` is `[]`.
  `strict_mcp_config=True` also drops them. So NFR-SEC-07 holds through either control.
- **N8. Cross-session messaging.** Every SDK session opens a peer socket (`/tmp/cc-socks/<pid>.sock`,
  `peerFeatures`), and the default tool set includes `SendMessage`/`ListAgents`. The owner's sessions on this Mac
  can therefore message bot sessions (`origin.kind="peer"`). It is not a hole for the personal session, whose tools
  exclude these, but treat peer-origin input as untrusted (qa-security). Dev sessions could add `SendMessage` and
  `ListAgents` to `disallowed_tools`. `claude --help` shows no flag to turn the socket off.
- **N9. Env leakage.** Inside a Claude Code session the parent exports `CLAUDE_CODE_SESSION_ID`,
  `CLAUDE_CODE_MESSAGING_SOCKET`, `CLAUDE_CODE_MESSAGING_TOKEN`, `CLAUDE_CODE_CHILD_SESSION`, `CLAUDE_PID` and
  `CLAUDE_EFFORT`. The SDK passes all of them on except `CLAUDECODE`. `core.proc.scrub_environ` covers
  `CLAUDE_CODE_*`. `CLAUDE_PID` and `CLAUDE_EFFORT` are not covered; add them to the scrub list. The SDK always sets
  `CLAUDE_CODE_ENTRYPOINT=sdk-py`.
- **N10. The CLI's sleep guard blocks `sleep N && …`.** On 2.1.309 a foreground `sleep 30 && echo done` is refused
  ("Blocked: sleep 30 followed by …"); `sleep 8` and `sleep 20` ran. Smoke tests and fixtures must not depend on
  long `sleep` chains.
- **N11. Bash uses the owner's shell environment.** It runs `/bin/zsh -c source ~/.claude/shell-snapshots/…`, even
  with `setting_sources=[]`. The gate's PATH shims must survive that snapshot. Check in T18: the snapshot may put
  the owner's PATH first.
- **N12. Rate limits.** `rate_limit_event` arrives every turn: `status`, `resetsAt`, `rateLimitType="five_hour"`,
  `utilization`, and `overageStatus="rejected"` with `overageDisabledReason="out_of_credits"`. Map it to
  `UsageLimited` when `status="rejected"`.
- **N13. Install.** The PyPI wheel is 116 MB because it bundles a CLI that Yordamchi never uses (`cli_path` is
  always set). On this Mac's link it downloaded at about 50 KB/s, and a concurrent `uv` held the cache lock for
  more than 5 minutes. Expect a slow first `uv sync` on the Mac.
