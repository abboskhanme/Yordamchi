"""Tables of `shared/integrations` (SPEC §4.2): `integrations` and `oauth_clients`.

- `integrations` is Yordamchi's own registry (D11). A static token or API key lives in the vault (`secret_id` →
  `vault_secrets.id`, a string FK by A2); the row holds no secret. `slug` is unique among active rows only, so a
  removed integration (kept 30 days for a revert) never blocks adding it again.
- `oauth_clients` holds one sealed OAuth grant per integration: AES-256-GCM through the `Keystore` port, AAD =
  `oauth_clients|<integration_id>|<slug>`. It is sealed here rather than as a `vault_secrets` row because a refresh
  rewrites it without an owner message, and the vault only stores what an owner's message brought (`OwnerOrigin`).
  `ciphertext` and `nonce` are NULL only inside the unit of work that creates the row (the AAD needs its id).
"""

from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from yordamchi.core.json_value import JsonValue
from yordamchi.persistence.base import Base
from yordamchi.persistence.types import JSONText


class Integration(Base):
    __tablename__ = "integrations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(32))
    display_name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))  # mcp_http | api_http
    url: Mapped[str] = mapped_column(String(500))
    description: Mapped[str] = mapped_column(String(200), default="")  # what an API key is for (UC-08)
    auth: Mapped[str] = mapped_column(String(16))  # none | bearer | header | oauth
    header_name: Mapped[str | None] = mapped_column(String(64))
    secret_id: Mapped[int | None] = mapped_column(Integer)  # vault_secrets.id (another feature's table: no FK, A2)
    bots_json: Mapped[JsonValue] = mapped_column(JSONText, nullable=False)  # ["dev"], ["dev", "personal"]
    tool_policy_json: Mapped[JsonValue] = mapped_column(JSONText, nullable=False)  # {"dev": [...]|"all", ...}
    tools_cache_json: Mapped[JsonValue] = mapped_column(JSONText, nullable=False)  # [{"name", "read_only"}]
    status: Mapped[str] = mapped_column(String(16))  # ok | auth_failed | unreachable | needs_reauth | disabled
    status_code: Mapped[str | None] = mapped_column(String(48))  # the error code behind a failing status
    last_validated_at: Mapped[datetime | None]
    created_at: Mapped[datetime]
    removed_at: Mapped[datetime | None]


# UNIQUE(slug) WHERE removed_at IS NULL: a removed slug can be used again.
Index(
    "uq_integrations_slug_active",
    Integration.slug,
    unique=True,
    sqlite_where=Integration.removed_at.is_(None),
    postgresql_where=Integration.removed_at.is_(None),
)


class OAuthClient(Base):
    __tablename__ = "oauth_clients"

    integration_id: Mapped[int] = mapped_column(ForeignKey("integrations.id"), primary_key=True)
    issuer: Mapped[str | None] = mapped_column(String(200))
    client_id: Mapped[str] = mapped_column(String(200))
    ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    nonce: Mapped[bytes | None] = mapped_column(LargeBinary(12))
    key_fp: Mapped[str] = mapped_column(String(16))
    expires_at: Mapped[datetime | None]
    last_refresh_at: Mapped[datetime | None]
    refresh_failures: Mapped[int] = mapped_column(Integer, default=0)
    reauth_told_at: Mapped[datetime | None]  # S.OAUTH_REAUTH went out; cleared by a successful sign-in
