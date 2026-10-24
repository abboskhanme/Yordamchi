# Claude fixtures

**Recorded, then sanitised.** `streams/*.jsonl` (except `synthetic_*`) were captured on 2026-10-24 from the real
`~/.local/bin/claude` **2.1.310** with `claude-agent-sdk` 0.2.173 and `haiku`, through the raw stream
(`client._query.receive_messages()`, which still carries `command_lifecycle`). Sessions ran in fresh scratch dirs with
`setting_sources=[]`, `bypassPermissions`, `CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS=1` and
`ENABLE_CLAUDEAI_MCP_SERVERS=false`.

Sanitising: the home dir became `/Users/owner`, scratch dirs `/tmp/yordamchi-fixture/project`, task dirs
`/tmp/claude-501/fixture`, the account e-mail `owner@example.com`; thinking signatures are `fixture-signature`; the
initialize response keeps only `pid`, `claude_code_version`, `current_permission_mode`, `session_state` and
`capabilities`; `init` lists of skills, agents, plugins and slash commands are cut to three entries. Session ids,
message uuids and tool-use ids are the recorded random ones.

One edit to a recording: in `interrupt.jsonl` the `receipt` record was moved one line up, before the
`command_lifecycle cancelled` frame, because the recorder logged the receipt when its await returned, after the frame
the CLI had already sent. The replay pauses at `receipt` until the interrupt is requested, so it must come first.

Line format: `{"t": <seconds since connect>, "kind": ..., "data": ...}`; `tests/fakes/claude_client.py` replays it.

| kind | data |
|---|---|
| `raw` | one stream-json message |
| `hook` | `{"input": <hook input>, "tool_use_id", "output": <what the recorder's hook returned>}` |
| `perm` | `{"tool_name", "input", "tool_use_id", "agent_id"}`: a `can_use_tool` call |
| `sent` | a user message the recorder wrote (`priority` as sent) |
| `receipt` | the interrupt control response |
| `init_response` | the initialize control response (trimmed) |
| `error`, `stderr` | what a failed connect raised and printed |

## `streams/`

| File | Scenario | What it pins |
|---|---|---|
| `simple.jsonl` | two plain turns | no message before the first query; `init` on every turn (N3); `command_lifecycle` queued/started/completed; `session_state_changed` running → idle; `rate_limit_event` with `unifiedWindows`; cumulative `total_cost_usd` / `modelUsage` |
| `tools.jsonl` | default tool set, two Bash calls, one failing | `tool_use` / `tool_result` (`is_error`), `ToolSearch`, the default `init` tool list without `SendMessage`/`ListAgents` |
| `next_merge.jsonl` | a `priority:"next"` follow-up during a Bash call | merged: one result, follow-up `queued` → `started` inside the turn (S1) |
| `interrupt.jsonl` | interrupt with `cancel_queued` while Bash runs, one queued input, then a new turn | receipt `cancelled`; result `error_during_execution` / `aborted_tools` / `result: null` (N5); a normal turn after |
| `hook_deny.jsonl` | PreToolUse deny of a Bash call | tool_result `PreToolUse:Bash hook error: …`; `permission_denials` (N6) |
| `question.jsonl` | `AskUserQuestion` under bypass | hook first, then `can_use_tool`; `requires_action`; the answered tool result (S3, UC-12) |
| `background_bash.jsonl` | `run_in_background` Bash | `task_started.is_backgrounded`, `background_tasks_changed`; result + idle, then a CLI-started turn with its own result (N2) |
| `background_agent.jsonl` | a background subagent | the state stays `running` after the first result; subagent `tool_use` with `parent_tool_use_id`; hook input with `agent_id`; the second result then idle (N2, S3) |
| `now_redirect.jsonl` | `priority:"now"` + `origin: human` (never used by Yordamchi) | `task_updated.patch.is_backgrounded`, the empty `aborted_tools` result, the redirect result, a late CLI-started turn (S1) |
| `set_model.jsonl` | `set_model("sonnet")` between turns | the `<local-command-stdout>` user echo (string content); the next `init` reports the new model |
| `resume_missing.jsonl` | `resume=<unknown id>` | connect fails: `ResultError` "No conversation found with session ID" |
| `assistant_report.jsonl` | the assistant pool (T12), `report` kind: two asks on one warm client, `effort=low`, a different owner-instructions block on each | custom system prompt, `tools: []`, `dontAsk`; the `UserPromptSubmit` hook on every turn with its `additionalContext`; per-turn cost from session totals |
| `assistant_routing.jsonl` | the assistant pool, `routing` kind with an `output_format` JSON schema: two asks | `init.tools == ["StructuredOutput"]`; the `PreToolUse` hook for `StructuredOutput` (explicit allow under `dontAsk`); `result.structured_output`, `stop_reason: tool_use`, `num_turns: 2` |

## Synthetic (`synthetic_*.jsonl`), written by hand in the recorded shapes

| File | Why synthetic |
|---|---|
| `synthetic_usage_limited.jsonl` | a `rejected` rate limit (UC-11) cannot be triggered on demand; shapes from N12 and the SDK's `RateLimitInfo` |
| `synthetic_auth_failed.jsonl` | an expired login (401 result) |
| `synthetic_todo.jsonl` | `TodoWrite` is not in the 2.1.310 default tool set; also a `conversation_reset` |

## `tool_calls/` (T25, the gate eval)

One `PreToolUse` hook input per tool, the payload `GuardBridge.pre_tool_use` receives. `tests/eval/gate/` replays
them with the case's own `tool_input`, `cwd` and `tool_use_id`; recorded paths under `/tmp/yordamchi-fixture/project`
and `/Users/owner` are mapped onto the eval's project and home.

| File | Source |
|---|---|
| `bash.json` | recorded: `hook_deny.jsonl`, main thread |
| `bash_subagent.json` | recorded: `background_agent.jsonl`, the subagent's call (`agent_id`, `agent_type`); its envelope is also grafted onto any case with `agent: true` |
| `bash_background.json` | recorded: `background_bash.jsonl` (`run_in_background`) |
| `agent.json` | recorded: `background_agent.jsonl`, the `Agent` tool starting a background subagent |
| `ask_user_question.json` | recorded: `question.jsonl` |
| `tool_search.json` | recorded: `tools.jsonl` |
| `edit.json` `write.json` `multi_edit.json` `read.json` `grep.json` `glob.json` `notebook_edit.json` `web_fetch.json` `todo_write.json` `task.json` `mcp.json` | synthetic: `bash.json`'s recorded envelope with the tool name and input shape of the CLI 2.1.310 tool schema (`Task` is the pre-2.1 name of `Agent`; `mcp.json` is `mcp__github__create_issue`). None of these tools ran in the T10/T11 recordings |

## `probe/`

`version.txt` is `claude --version`; `auth_status_claude_ai.json` is `claude auth status` (sanitised). The logged-out
and API-key variants are synthetic.

The two `assistant_*` recordings were captured on 2026-10-24 (CLI 2.1.310, haiku) through `ClaudeAssistant` itself,
with a client factory that tees the raw stream, the hooks and the sent messages into this format; session cwds became
`/tmp/yordamchi-fixture/sessions/<kind>` (also inside the transcript path slug). Their prompts and instructions are test
data, not product prompts.

Re-record with the live suite (`pytest -m live tests/integration/test_claude_* tests/contract/test_agent_runtime.py`)
after a CLI or SDK upgrade, and re-check docs/SPIKES.md N1–N12.
