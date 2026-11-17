"""add post annotations"""
import sqlalchemy as sa
from alembic import op

revision = "012_post_annotations"
down_revision = "011_agent_moderation_audit"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "post_annotations",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("post_slug", sa.Text(), nullable=False),
        sa.Column("block_kind", sa.Text(), nullable=False),
        sa.Column("block_index", sa.Integer(), nullable=False),
        sa.Column("exact", sa.Text(), nullable=False),
        sa.Column("prefix", sa.Text(), nullable=False, server_default=""),
        sa.Column("suffix", sa.Text(), nullable=False, server_default=""),
        sa.Column("note_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("post_slug ~ '^[a-z0-9-]{1,100}$'", name="post_annotations_slug_check"),
        sa.CheckConstraint("block_kind IN ('heading','paragraph','list_item','blockquote_paragraph','table_cell')", name="post_annotations_kind_check"),
        sa.CheckConstraint("block_index >= 0", name="post_annotations_index_check"),
        sa.CheckConstraint("length(exact) BETWEEN 1 AND 2000", name="post_annotations_exact_check"),
        sa.CheckConstraint("length(prefix) <= 64 AND length(suffix) <= 64", name="post_annotations_context_check"),
        sa.CheckConstraint("length(trim(note_text)) BETWEEN 1 AND 2000", name="post_annotations_note_check"),
    )
    op.create_index("ix_post_annotations_slug_id", "post_annotations", ["post_slug", "id"])


def downgrade():
    op.drop_index("ix_post_annotations_slug_id", table_name="post_annotations")
    op.drop_table("post_annotations")
