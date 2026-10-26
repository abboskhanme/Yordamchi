"""topics_threads: the dev group's forum topics (D4 revised 2026-10-26; SPEC §3.2.16, §4.2).

Expand-only (CONVENTIONS §8): two new tables and two nullable columns, nothing else. The release before it runs on
this schema unchanged: it never reads the new tables, and its rows read NULL (General) in the new columns.
- `project_topics`: the topic the bot opened for each project, found back by (chat, thread).
- `topic_chats`: whether a chat is a forum, and Telegram's latest refusal to open a topic there.
- `bursts.thread_id`, `outbox_messages.thread_id`: the topic a burst was written in, and the one a message goes to.

Revision ID: 0002
Create Date: 2026-10-26 12:43:27.985207
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_topics",
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("thread_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("project_id", name=op.f("pk_project_topics")),
    )
    with op.batch_alter_table("project_topics", schema=None) as batch_op:
        batch_op.create_index("ix_project_topics_chat_id_thread_id", ["chat_id", "thread_id"], unique=False)

    op.create_table(
        "topic_chats",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("is_forum", sa.Boolean(), nullable=False),
        sa.Column("noted_at", sa.DateTime(), nullable=False),
        sa.Column("refused_at", sa.DateTime(), nullable=True),
        sa.Column("refusal", sa.String(length=16), nullable=True),
        sa.PrimaryKeyConstraint("chat_id", name=op.f("pk_topic_chats")),
    )
    with op.batch_alter_table("bursts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("thread_id", sa.BigInteger(), nullable=True))

    with op.batch_alter_table("outbox_messages", schema=None) as batch_op:
        batch_op.add_column(sa.Column("thread_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    # Rollback restores the pre-update DB copy, not downgrade() (CONVENTIONS §8).
    with op.batch_alter_table("outbox_messages", schema=None) as batch_op:
        batch_op.drop_column("thread_id")

    with op.batch_alter_table("bursts", schema=None) as batch_op:
        batch_op.drop_column("thread_id")

    op.drop_table("topic_chats")
    with op.batch_alter_table("project_topics", schema=None) as batch_op:
        batch_op.drop_index("ix_project_topics_chat_id_thread_id")

    op.drop_table("project_topics")
