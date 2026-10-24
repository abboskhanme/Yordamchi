"""vault.domain: names, row binding, references, peer visibility, the hash file and the wipe cutoff (SPEC §4.5)."""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from tests.factories import FAKE_KEYS
from yordamchi.core import redact
from yordamchi.core.paths import BotName
from yordamchi.shared.vault import domain
from yordamchi.shared.vault.domain import NewSecret, SecretRef


def test_the_first_secret_of_a_kind_is_named_after_it_and_later_ones_are_numbered() -> None:
    assert domain.next_free_name("google_api_key", set()) == "google_api_key"
    assert domain.next_free_name("google_api_key", {"google_api_key"}) == "google_api_key_2"
    assert domain.next_free_name("google_api_key", {"google_api_key", "google_api_key_2"}) == "google_api_key_3"
    assert domain.next_free_name("github_token", {"google_api_key"}) == "github_token"


def test_the_aad_binds_name_kind_and_row_id() -> None:
    assert domain.secret_aad(name="google_api_key_2", kind="google_api_key", secret_id=7) == (
        b"google_api_key_2|google_api_key|7"
    )


def test_last4_follows_the_redaction_rule_for_each_kind() -> None:
    assert domain.last4_of(FAKE_KEYS["google_api_key"], "google_api_key") == FAKE_KEYS["google_api_key"][-4:]
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
    assert domain.last4_of(pem, "pem_private_key") == ""  # "----" says nothing
    assert domain.last4_of("short", "generic_secret") == ""  # too short to show a tail


def test_a_reference_shows_only_the_mask() -> None:
    ref = SecretRef(id=1, name="google_api_key", kind="google_api_key", last4="p6Q", bots=(BotName.DEV,))
    assert ref.masked == redact.MASK + "p6Q"
    assert SecretRef(id=2, name="pem", kind="pem_private_key", last4="", bots=()).masked == redact.MASK


def test_a_new_secret_needs_a_known_shape_of_kind_and_a_value() -> None:
    assert NewSecret(kind="google_api_key", value="x").kind == "google_api_key"
    with pytest.raises(ValueError, match="kind"):
        NewSecret(kind="Bad Kind", value="x")
    with pytest.raises(ValueError, match="empty"):
        NewSecret(kind="google_api_key", value="")
    assert "AIza" not in repr(NewSecret(kind="google_api_key", value=FAKE_KEYS["google_api_key"]))


def test_bots_json_reads_known_bots_only() -> None:
    assert domain.bots_from_json(["dev", "personal"]) == (BotName.DEV, BotName.PERSONAL)
    assert domain.bots_from_json(["dev", "future_bot", 3]) == (BotName.DEV,)
    assert domain.bots_from_json("dev") == ()


def test_a_peer_secret_is_visible_only_to_the_bots_it_is_assigned_to() -> None:
    assert domain.visible_to((BotName.DEV, BotName.PERSONAL), BotName.PERSONAL)
    assert not domain.visible_to((BotName.DEV,), BotName.PERSONAL)


def test_the_hash_document_is_sorted_unique_sha256_hex() -> None:
    values = ["b-value", "a-value", "b-value"]
    expected = sorted({hashlib.sha256(value.encode()).hexdigest() for value in values})
    assert domain.hashes_document(values) == {"sha256": expected}
    assert domain.sha256_hex("a-value") == hashlib.sha256(b"a-value").hexdigest()
    assert domain.hashes_document([]) == {"sha256": []}


def test_a_removed_ciphertext_is_wiped_after_30_days() -> None:
    now = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
    assert domain.wipe_cutoff(now) == now - timedelta(days=30)
