# AI feature: chat in the dev group

Owner, 2026-10-24: "nimadir so'rasam faqat projectni ichidan qidirishga harakat qilyapti", "guruhda ham project uchun
mo'ljallangan bo'lsa ham, men bilan sodda yordamchi bot kabi gaplashishi kerak". Before this, every message that was
not a pre-parsed command was forced into a project intent: a general question became "X · Tekshiryapman" (a read-only
turn inside the active project) or a report.

## What it does

Two model calls, one job each:

1. **Routing** (existing, `dev/routing`, haiku, structured output) gets one more intent, `chat`: a message about none of
   his projects (small talk, general knowledge, a programming or tech concept, advice, translation or writing help,
   arithmetic). A `chat` decision has no project (`Outcome.NO_PROJECT`), no turn and no "→ X" line.
2. **The chat answer** (new, `dev/chat`, the warm `chat` Assistant) answers it in a few plain lines, as a reply to his
   message. No tools, no project data, no report shape.

Code: `src/yordamchi/dev/routing/domain.py` (`Intent.CHAT`, `_chats`, `_with_project`), `src/yordamchi/dev/chat/`
(`domain.py`, `service.py`), `src/yordamchi/bots/dev/pipeline.py` (`_chat`), `src/yordamchi/bots/dev/runtime.py`
(warm-up). Prompts: `src/yordamchi/prompts/dev/routing_system.md` (one intent bullet, one word in step 6),
`chat_system.md` (fixed parts), `chat_context.md` (per-ask block).

## Prompt strategy

- Routing: `chat` is described as one more intent with examples and counter-examples, no global "first decide" step
  (T34b showed such a step made haiku drop context follow-ups). The examples in the prompt are deliberately not the
  eval's texts.
- Chat answer: the frozen system prompt holds the role, the no-tools honesty rules, the input contract and the
  Telegram Markdown subset. Persona (`persona.dev_chat`) and reply language (`lang.reply`, or his language when he
  writes Russian or English) travel with every ask (SPEC §5.4, spike S4), so a change applies to the next answer.
- Input is one JSON object: his words in order (`voice`, `forwarded` flags) and his local time. Every string is
  scrubbed of secrets before it reaches the model; the answer is scrubbed again before it reaches him.

## Schema and validation

- Routing: the existing `OUTPUT_SCHEMA`, whose `intent` enum now includes `chat` (`LLM_INTENTS`). The CLI validates the
  structured output; `parse_llm_route` rejects an unknown intent (→ the no-LLM fallback).
- Chat answer: free text by design (it is a conversation). `answer_text` strips a copied `[chat]` marker, cuts to
  3,500 characters (one Telegram message) at a sentence, and closes a cut code fence. Empty → the failure line.

## Guardrails (in code, not in the prompt)

- A named project or a reply to a project's message is never chat: it goes to that project as `report_project`.
- Chat only for a burst of words; a photo, file or link keeps the project routes.
- It must be sure (`routing.min_confidence`), or have no project in mind with nothing active; else today's route.
- A family word ("Wunderkind…") or several named projects keep chat out.
- Chat never steers a running turn (`with_follow_up` only merges `instruct_project`).
- The chat Assistant has no tools (`tools=[]`, `strict_mcp_config`, `ENABLE_CLAUDEAI_MCP_SERVERS=false`); the
  PreToolUse guard denies everything. It cannot change a setting, approve a decision or start work.
- Bounded by `timeouts.chat_s` (30 s); no retry in the service. Any failure → `S.CHAT_FAILED`, one line, never a report.
- Forwarded text is material: the prompt says so, and the eval has an injection case.

## Settings

`models.chat` = sonnet · `models.chat_effort` = low · `timeouts.chat_s` = 30 · `persona.dev_chat` (prompt slot) ·
`assistant.warm_clients.chat` = 1 (one warm client, started at boot and after a settings change).

## Evaluation

- Routing: `tests/eval/routing/cases.yaml`, category `chat` (30 cases: Uzbek, Russian, English, mixed; text and voice
  spellings; some while a project is active or running) and 13 `near_miss` cases in their UC-10 categories. Floor in
  the runner: ≥ 90 % of chat cases answered as chat, 0 near misses answered as chat; UC-10's floors unchanged.
  Re-record: `uv run pytest -m live --record-llm tests/eval/routing`.
- Chat answers: `tests/eval/chat/cases.yaml` (31 cases), mechanical checks (an answer came, language, length, no
  report shape or project name, Markdown subset, sums and facts as a floor, the injection canary, no claimed work).
  Re-record: `uv run pytest -m live --record-llm tests/eval/chat`.
- Last result: see "Last run" below.

## Last run

Not shipped. Two live routing passes (haiku, effort low, claude 2.1.310, 213 cases) on 2026-10-24:

| Bucket | Before (HEAD, 170 cases) | Pass 1 | Pass 2 (final prompt) | Floor |
|---|---|---|---|---|
| explicit + content-clear | 112/115 = 97.4 % | 121/123 = 98.4 % | 122/123 = 99.2 % | ≥ 97 % |
| context follow-ups | 25/26 = 96.2 %, wrong 0 | 30/31 = 96.8 %, wrong 0 | 29/31 = 93.5 %, wrong 0 | ≥ 90 %, wrong 0 |
| genuinely ambiguous | 28/29 = 96.6 % | 27/29 = 93.1 % | 27/29 = 93.1 % | ≥ 90 % |
| silent wrong (UC-10) | 1 | **2** | **2** | ≤ 1 |
| chat → chat | (no intent) | 30/30 | 30/30 | ≥ 90 % |
| near misses → chat | (no intent) | 0/13 | 0/13 | 0 |

Every silent wrong is a confident pick between two projects, never chat: before `tugmalar rangini ko'k qil` → TezTel;
pass 1 that one and `Xitoydan ehtiyot qism …` → ImportAgents; pass 2 `ovozli xabar matnga noto'g'ri o'girilyapti` →
Yordamchi and `Dubaydan kelgan tovarlar …` → TezTel. No case outside the chat bucket was labelled chat in either pass.
Routing latency: pass 1 p50 2,193 ms, p90 3,079 ms; pass 2 p50 1,970 ms, p90 3,014 ms.

Chat answers (one live pass, sonnet, effort low, warm client, 31 cases): p50 3,431 ms, p90 7,466 ms, max 10,283 ms
for the whole `answer_chat`; none over `timeouts.chat_s`. Mean tokens per answer: input 80, cache read 4,335, cache
write 652, output 268; the CLI's notional cost ≈ 5,500 micro-USD (0.0055 USD) per answer, not billed (the owner's
plan). At 30 chat questions a day that is about 0.17 USD/day notional. The live checker passed 27/31; the 4 misses were
the checker's own false positives (a `#` comment and HTML inside code blocks, a two-line sum too short for word
markers), fixed with a not-blind test for each; the recorded answers replay 31/31.

## Operations

- Logs: `route_decided` (intent `chat`, reason `chat`), `chat_answered` (burst id, answered, provider, error code,
  chars, duration), `chat_answer_failed`, `chat_answer_empty`, `chat_warm_failed`. Never the question or the answer.
- Usage: one `usage_records` row per answer, source `chat`, provider `anthropic_plan` (the owner's subscription; the
  CLI's cost figure is notional, `is_paid_api` false).
- Degraded path: Claude unavailable or out of limit → the failure line within `timeouts.chat_s`; the routing LLM down
  → SPEC §3.2.6's fallback (the one active project, else a question), as before.

## Known failure modes

- A chat follow-up ("misol keltir" after a chat answer) is not a routing signal: a chat route has no project, so it
  adds no `recent` activity, and the follow-up routes like any message with no subject of its own.
- A photo or a link with a general question ("bu rasmda nima yozilgan?") is not chat (no media in the chat ask).
- No live data: exchange rates, news and weather are answered from what the model knows, with that caveat.
- "bu X uchun edi" after a chat answer cannot move it: a chat route has no project to correct.
- A restart while an answer is being written loses that answer (the burst is already recorded as routed).
- The warm client keeps up to 20 asks of conversation before it rotates, so an earlier chat colours a later answer
  (live: the voice "pitonda dekorator degani nima" right after the typed one got "Yana so'rading, …"). Follow-ups
  inside chat work by accident, until a rotation.
- The register drifts between "sen" and "siz" from answer to answer; `persona.dev_chat` can pin it once the owner
  says which he wants.
- "menga oddiy landing sahifa qilib ber" (no project named) gets the code of a page in chat, not a project.
