"""基础设施横切能力：audit_logs / outbox_events / idempotency_keys

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-16

这三张表不属于任何业务领域，是横切关注点：

* ``audit_logs`` —— 操作审计。记录「谁 / 哪个租户 / 什么资源 / 什么动作 /
  结果 / traceId」，是排查越权与线上问题的第一手资料
* ``outbox_events`` —— 事务性发件箱。业务事务内只写本表，
  后台投递器负责推送，避免「业务已提交但事件丢失」的不一致
* ``idempotency_keys`` —— 幂等键。防止重复提交造成重复业务
  （重复绑定、重复下单、重复执行分配单）
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.base  # noqa: F401 - 自定义类型 UTCDateTime 需要此导入

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """升级。"""
    # ------------------------------------------------------------
    # 审计日志
    # ------------------------------------------------------------
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=True),
        sa.Column("actor_id", sa.String(length=36), nullable=True),
        sa.Column("actor_account", sa.String(length=64), nullable=True),
        sa.Column("actor_role", sa.String(length=64), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=True),
        sa.Column("resource_id", sa.String(length=64), nullable=True),
        sa.Column("summary", sa.String(length=256), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("client_ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            app.db.base.UTCDateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            app.db.base.UTCDateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_logs")),
    )
    with op.batch_alter_table("audit_logs", schema=None) as batch_op:
        # 复合索引服务于「按租户/动作 + 时间倒序」的审计查询
        batch_op.create_index(
            "idx_audit_logs_tenant_created", ["tenant_id", "created_at"], unique=False
        )
        batch_op.create_index(
            "idx_audit_logs_action_created", ["action", "created_at"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_audit_logs_action"), ["action"], unique=False)
        batch_op.create_index(batch_op.f("ix_audit_logs_actor_id"), ["actor_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_audit_logs_resource_id"), ["resource_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_audit_logs_resource_type"), ["resource_type"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_audit_logs_tenant_id"), ["tenant_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_audit_logs_trace_id"), ["trace_id"], unique=False)

    # ------------------------------------------------------------
    # 事务性发件箱
    # ------------------------------------------------------------
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("published_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            app.db.base.UTCDateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            app.db.base.UTCDateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox_events")),
    )
    with op.batch_alter_table("outbox_events", schema=None) as batch_op:
        # 投递器扫描「未发布事件」走此索引
        batch_op.create_index("idx_outbox_events_published", ["published_at"], unique=False)
        batch_op.create_index(
            "idx_outbox_events_aggregate", ["aggregate_type", "aggregate_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_outbox_events_event_type"), ["event_type"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_outbox_events_tenant_id"), ["tenant_id"], unique=False)

    # ------------------------------------------------------------
    # 幂等键
    # ------------------------------------------------------------
    op.create_table(
        "idempotency_keys",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("scope", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=True),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.JSON(), nullable=True),
        sa.Column("request_hash", sa.String(length=64), nullable=True),
        sa.Column("expires_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column(
            "created_at",
            app.db.base.UTCDateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            app.db.base.UTCDateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_idempotency_keys")),
        # 唯一约束是并发幂等的关键：只有一个请求能占位成功
        sa.UniqueConstraint("scope", "key", name="uq_idempotency_scope_key"),
    )
    with op.batch_alter_table("idempotency_keys", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_idempotency_keys_expires_at"), ["expires_at"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_idempotency_keys_key"), ["key"], unique=False)
        batch_op.create_index(batch_op.f("ix_idempotency_keys_status"), ["status"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_idempotency_keys_tenant_id"), ["tenant_id"], unique=False
        )


def downgrade() -> None:
    """回滚。"""
    with op.batch_alter_table("idempotency_keys", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_idempotency_keys_tenant_id"))
        batch_op.drop_index(batch_op.f("ix_idempotency_keys_status"))
        batch_op.drop_index(batch_op.f("ix_idempotency_keys_key"))
        batch_op.drop_index(batch_op.f("ix_idempotency_keys_expires_at"))
    op.drop_table("idempotency_keys")

    with op.batch_alter_table("outbox_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_outbox_events_tenant_id"))
        batch_op.drop_index(batch_op.f("ix_outbox_events_event_type"))
        batch_op.drop_index("idx_outbox_events_aggregate")
        batch_op.drop_index("idx_outbox_events_published")
    op.drop_table("outbox_events")

    with op.batch_alter_table("audit_logs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_audit_logs_trace_id"))
        batch_op.drop_index(batch_op.f("ix_audit_logs_tenant_id"))
        batch_op.drop_index(batch_op.f("ix_audit_logs_resource_type"))
        batch_op.drop_index(batch_op.f("ix_audit_logs_resource_id"))
        batch_op.drop_index(batch_op.f("ix_audit_logs_actor_id"))
        batch_op.drop_index(batch_op.f("ix_audit_logs_action"))
        batch_op.drop_index("idx_audit_logs_action_created")
        batch_op.drop_index("idx_audit_logs_tenant_created")
    op.drop_table("audit_logs")
