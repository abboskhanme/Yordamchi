"""`vault_secrets`: every secret, as AES-256-GCM ciphertext only (SPEC §4.2, §4.5; NFR-SEC-02).

A row never holds a plaintext. `ciphertext` and `nonce` are NULL only in two cases: inside the unit of work that
creates the row (the AAD needs the row id, so the row is inserted, then sealed), and after retention wiped a removed
secret (`removed_at` older than 30 days). The name, kind and last four characters stay for the audit trail.
"""

from datetime import datetime

from sqlalchemy import BigInteger, Index, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from yordamchi.core.json_value import JsonValue
from yordamchi.persistence.base import Base
from yordamchi.persistence.types import JSONText


class VaultSecret(Base):
    __tablename__ = "vault_secrets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))
    ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    nonce: Mapped[bytes | None] = mapped_column(LargeBinary(12))
    key_fp: Mapped[str] = mapped_column(String(16))
    last4: Mapped[str] = mapped_column(String(4))
    bots_json: Mapped[JsonValue] = mapped_column(JSONText, nullable=False)  # ["dev"], ["dev", "personal"]
    origin_update_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime]
    removed_at: Mapped[datetime | None]


# UNIQUE(name) WHERE removed_at IS NULL: a removed name can be used again.
Index(
    "uq_vault_secrets_name_active",
    VaultSecret.name,
    unique=True,
    sqlite_where=VaultSecret.removed_at.is_(None),
    postgresql_where=VaultSecret.removed_at.is_(None),
)
