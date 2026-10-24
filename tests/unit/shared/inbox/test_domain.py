"""inbox.domain: origin check, kinds and status, the secret scan and its markers, the payload round trip, the ack
plan and reply, the polling offset and the retention cutoffs (SPEC §3.2.1, §4.2, §4.5, §4.6; UC-04)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from tests.factories import CLAUDE_LOGIN_TOKEN, FAKE_KEYS, make_update, origin_of
from yordamchi.core import redact
from yordamchi.core import strings as S
from yordamchi.core.errors import VaultKeyMissing
from yordamchi.core.paths import BotName
from yordamchi.shared.inbox import domain
from yordamchi.shared.inbox.domain import Attachment, Content, SecretScan, UpdateKind, UpdateStatus
from yordamchi.shared.vault.domain import NewSecret, SecretRef

GOOGLE = FAKE_KEYS["google_api_key"]
GITHUB = FAKE_KEYS["github_token"]
GOOGLE_REF = SecretRef(id=3, name="google_api_key", kind="google_api_key", last4=GOOGLE[-4:], bots=(BotName.DEV,))
NOW = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)


# --- origin, kind, status -------------------------------------------------------------------------------------------
def test_the_origin_must_prove_this_very_update() -> None:
    update = make_update(7)
    domain.require_matching_origin(origin_of(update), update, BotName.DEV)
    for wrong in (
        origin_of(replace(update, update_id=8)),
        origin_of(replace(update, chat_id=-100999)),
        replace(origin_of(update), user_id=1),
        replace(origin_of(update), bot=BotName.PERSONAL),
    ):
        with pytest.raises(ValueError, match="does not match"):
            domain.require_matching_origin(wrong, update, BotName.DEV)
    with pytest.raises(ValueError, match="does not match"):
        domain.require_matching_origin(origin_of(update), update, BotName.PERSONAL)


def test_a_text_of_nothing_but_links_waits_like_media() -> None:
    assert domain.kind_for_text("https://example.com/a  http://x.uz/b\n") is UpdateKind.LINK
    assert domain.kind_for_text("shu saytni ko'r: https://example.com") is UpdateKind.TEXT


def test_the_initial_status_follows_what_the_update_can_still_become() -> None:
    assert domain.initial_status(make_update(), vault_failed=False) is UpdateStatus.RECEIVED
    assert domain.initial_status(make_update(), vault_failed=True) is UpdateStatus.FAILED
    member = make_update(text=None, kind=UpdateKind.MEMBER)
    assert domain.initial_status(member, vault_failed=False) is UpdateStatus.IGNORED
    assert {UpdateStatus.ATTACHED, UpdateStatus.IGNORED, UpdateStatus.FAILED} == domain.TERMINAL


def test_content_never_shows_in_a_repr() -> None:
    update = make_update(text=f"kalit: {GOOGLE}", caption="maxfiy izoh")
    assert GOOGLE not in repr(update)
    assert "maxfiy" not in repr(update)
    assert GOOGLE not in repr(Content(text=GOOGLE))


# --- the scan -------------------------------------------------------------------------------------------------------
def test_the_scan_finds_keys_in_text_and_caption_in_order() -> None:
    scan = domain.scan_secrets(make_update(text=f"a {GOOGLE} b {GITHUB}", caption=f"c {GOOGLE}"))
    assert [hit.kind for hit in scan.text] == ["google_api_key", "github_token"]
    assert [hit.kind for hit in scan.caption] == ["google_api_key"]
    assert scan.found_key
    assert scan.to_store() == (NewSecret("google_api_key", GOOGLE), NewSecret("github_token", GITHUB))


def test_a_claude_login_token_is_found_but_never_offered_to_the_vault() -> None:
    scan = domain.scan_secrets(make_update(text=f"token: {CLAUDE_LOGIN_TOKEN}"))
    assert (scan.found_key, scan.refused, scan.to_store()) == (True, 1, ())


def test_a_token_like_part_of_a_bot_url_is_masked_but_is_not_a_key() -> None:
    update = make_update(text="xato: https://api.telegram.org/botWEIRD-token-shape/getMe")
    scan = domain.scan_secrets(update)
    assert [(hit.kind, hit.vaultable) for hit in scan.text] == [("telegram_bot_url", False)]
    assert (scan.found_key, scan.to_store()) == (False, ())
    text = domain.redact_text(update.text, scan.text, {})
    assert text is not None
    assert "WEIRD-token-shape" not in text


def test_a_plain_message_has_nothing_to_scan() -> None:
    scan = domain.scan_secrets(make_update(text="BonviHR qanday?", caption=None))
    assert scan == SecretScan()
    assert (scan.found_key, scan.refused, scan.to_store()) == (False, 0, ())


# --- markers and content --------------------------------------------------------------------------------------------
def test_each_hit_is_replaced_by_its_marker_in_place() -> None:
    text = f"g {GOOGLE} c {CLAUDE_LOGIN_TOKEN} h {GITHUB} ."
    hits = domain.scan_secrets(make_update(text=text)).text
    redacted = domain.redact_text(text, hits, {GOOGLE: GOOGLE_REF})  # the GitHub key was not stored
    assert redacted == (f"g [kalit: google_api_key {redact.MASK}{GOOGLE[-4:]}] c [rad etildi] h [kalit saqlanmadi] .")
    assert domain.redact_text(None, (), {}) is None


def test_redacted_content_carries_the_attachment_and_the_vault_ids() -> None:
    photo = Attachment(file_id="AgAD-file", mime="image/jpeg", size_bytes=1024, file_name="ekran.jpg")
    update = make_update(text=None, caption=f"kalit: {GOOGLE}", attachment=photo)
    content = domain.redacted_content(update, domain.scan_secrets(update), {GOOGLE: GOOGLE_REF})
    assert content == Content(
        text=None, caption=f"kalit: [kalit: google_api_key {GOOGLE_REF.masked}]", attachment=photo, secret_ids=(3,)
    )


def test_the_payload_round_trips_and_every_string_is_scrubbed() -> None:
    content = Content(
        text="salom",
        caption="izoh",
        attachment=Attachment(file_id="f1", mime="audio/ogg", size_bytes=10, duration_s=12, file_name="ovoz.ogg"),
        secret_ids=(1, 4),
    )
    assert domain.content_from_json(domain.payload_json(content, lambda text: text)) == content
    scrubbed = domain.payload_json(content, str.upper)
    assert (scrubbed["text"], scrubbed["caption"]) == ("SALOM", "IZOH")
    assert scrubbed["attachment"] == {
        "FILE_ID": "F1",
        "MIME": "AUDIO/OGG",
        "SIZE_BYTES": 10,
        "DURATION_S": 12,
        "FILE_NAME": "OVOZ.OGG",
    }


def test_a_nulled_or_malformed_payload_reads_as_empty_content() -> None:
    assert domain.content_from_json(None) == Content()
    assert domain.content_from_json(["not", "an", "object"]) == Content()
    odd = domain.content_from_json({"text": 5, "attachment": {"mime": "x"}, "secret_ids": ["a", 2]})
    assert odd == Content(text=None, attachment=None, secret_ids=(2,))


# --- the ack --------------------------------------------------------------------------------------------------------
def test_a_plain_message_gets_the_seen_reaction_and_typing() -> None:
    update = make_update(text="BonviHR qanday?")
    plan = domain.plan_ack(
        update, domain.scan_secrets(update), status=UpdateStatus.RECEIVED, stored=(), vault_error=None
    )
    assert (plan.chat_id, plan.message_id) == (update.chat_id, update.message_id)
    assert (plan.delete_message, plan.react, plan.typing, plan.notices, plan.replies) == (False, True, True, (), False)
    assert domain.ack_reply(plan, deleted=False) is None


def test_a_message_holding_a_key_is_deleted_and_answered_with_its_mask() -> None:
    update = make_update(text=f"{GOOGLE} {GOOGLE}")
    plan = domain.plan_ack(
        update,
        domain.scan_secrets(update),
        status=UpdateStatus.RECEIVED,
        stored=(GOOGLE_REF, GOOGLE_REF),
        vault_error=None,
    )
    assert (plan.delete_message, plan.react, plan.typing, plan.replies) == (True, False, True, True)
    assert plan.notices == (S.SECRETS_STORED(keys=f"google_api_key {GOOGLE_REF.masked}"),)  # once, not twice
    assert domain.ack_reply(plan, deleted=True) == plan.notices[0]
    reply = domain.ack_reply(plan, deleted=False)
    assert reply == plan.notices[0] + "\n" + S.SECRET_NOT_DELETED()
    assert GOOGLE not in reply


def test_a_refused_token_is_deleted_and_explained() -> None:
    update = make_update(text=CLAUDE_LOGIN_TOKEN)
    plan = domain.plan_ack(
        update, domain.scan_secrets(update), status=UpdateStatus.RECEIVED, stored=(), vault_error=None
    )
    assert plan.delete_message
    assert plan.notices == (S.CLAUDE_TOKEN_REFUSED(),)


def test_a_key_the_vault_could_not_take_is_still_deleted_and_the_owner_told_why() -> None:
    update = make_update(text=GOOGLE)
    plan = domain.plan_ack(
        update, domain.scan_secrets(update), status=UpdateStatus.FAILED, stored=(), vault_error=VaultKeyMissing()
    )
    assert (plan.delete_message, plan.react, plan.typing) == (True, False, False)
    assert plan.notices == (S.ERR_VAULT_KEY_MISSING(),)


def test_a_membership_update_gets_no_ack() -> None:
    update = make_update(text=None, kind=UpdateKind.MEMBER)
    plan = domain.plan_ack(
        update, domain.scan_secrets(update), status=UpdateStatus.IGNORED, stored=(), vault_error=None
    )
    assert (plan.delete_message, plan.react, plan.typing, plan.replies) == (False, False, False, False)


def test_a_synthetic_update_only_shows_typing() -> None:
    update = make_update(text="selftest", synthetic=True)
    plan = domain.plan_ack(
        update, domain.scan_secrets(update), status=UpdateStatus.RECEIVED, stored=(), vault_error=None
    )
    assert (plan.delete_message, plan.react, plan.typing) == (False, False, True)


# --- offset and retention -------------------------------------------------------------------------------------------
def test_the_next_offset_is_past_the_batch_and_never_moves_back() -> None:
    assert domain.next_offset(0, [5, 3, 9]) == 10
    assert domain.next_offset(10, [4]) == 10
    assert domain.next_offset(10, []) == 10


def test_payloads_are_kept_7_days_and_rows_30() -> None:
    assert domain.payload_cutoff(NOW) == NOW - timedelta(days=7)
    assert domain.row_cutoff(NOW) == NOW - timedelta(days=30)
