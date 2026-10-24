# Telegram fixtures

**Synthetic, not recorded.** Every file here was written by hand to match the object shapes and error
answers in the official Bot API reference (<https://core.telegram.org/bots/api>, Bot API 10.3, read on
2026-10-24). No real token, chat, user or file id appears: ids are made up (`OWNER` 100000001, dev group
-1001234567890, a foreign group -1009876543210, the bot 7000000001), and `file_id`s only imitate the real
prefixes. Re-record from a test bot (`pytest -m live`) once X01 is done, and keep the README's claim true.

## `updates/` — `Update` objects as `getUpdates` returns them

| File | What it covers |
|---|---|
| `album.json` | three photo updates sharing one `media_group_id`; only the first has a caption (UC-02 albums) |
| `voice.json` | a 14 s voice note, `audio/ogg`, with `file_size` (STT input) |
| `photo.json` | one photo with three `PhotoSize`s and a caption |
| `document.json` | a 1.2 MB PDF |
| `document_too_large.json` | a 25 MiB document: over the 20 MB getFile limit (NFR-CAP-04) |
| `my_chat_member_added_to_foreign_group.json` | a stranger adds the bot to another group: `left` → `member` (UC-01 leave) |
| `my_chat_member_promoted_in_dev_group.json` | the owner makes the bot admin in the dev group with `can_delete_messages` (UC-01 setup check) |
| `my_chat_member_kicked.json` | the bot is removed: `member` → `kicked` |

## `responses/` — one HTTP answer each: `{"status": <HTTP status>, "body": <Bot API response>}`

`raw` instead of `body` is a non-JSON answer (a proxy's HTML page). The bodies follow "Making requests":
`ok`, `error_code`, `description`, and `parameters` (`retry_after`, `migrate_to_chat_id`) from
`ResponseParameters`. The descriptions are the texts Telegram is known to send; the docs call them unstable,
so the adapter matches only their well-known prefixes.

| File | Status | Adapter outcome |
|---|---|---|
| `send_message_ok.json`, `get_file_voice.json` | 200 | success |
| `too_many_requests.json` | 429, `retry_after: 7` | `RateLimited(retry_after=7)`, honoured (NFR-CAP-02) |
| `too_many_requests_no_parameters.json` | 429, no `parameters` | `RateLimited`, `retry_after` read from the text |
| `request_entity_too_large.json` | 413 | outbound file too large, not retried |
| `file_is_too_big.json` | 400 | inbound file too large (getFile over 20 MB) |
| `message_not_modified.json` | 400 | an unchanged edit counts as done |
| `message_cant_be_deleted.json`, `message_to_delete_not_found.json` | 400 | `delete()` returns False |
| `cant_parse_entities.json`, `reaction_invalid.json` | 400 | refused, not retried |
| `chat_migrated.json` | 400, `migrate_to_chat_id` | refused, not retried |
| `unauthorized.json` | 401 | `AuthFailed` |
| `forbidden_bot_kicked.json`, `forbidden_not_member.json` | 403 | refused; `leave()` treats them as already left |
| `conflict.json` | 409 | refused (a second poller) |
| `internal_server_error.json`, `bad_gateway.json` | 5xx | `ProviderUnavailable`, retried |
| `proxy_html_502.json` | 502, HTML | `ProviderUnavailable`; retried, except a send (its delivery is unknown: the outbox decides) |
