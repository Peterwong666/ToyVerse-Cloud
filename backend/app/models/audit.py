"""审计、事件与幂等相关的模型。

这三张表是**基础设施性质**的横切能力，不属于任何业务领域：

* :class:`AuditLog` —— 操作审计，所有关键动作落库，含 ``traceId`` 便于串联
* :class:`OutboxEvent` —— 事务性发件箱，保证「业务写入」与「事件发布」原子性
* :class:`IdempotencyKey` —— 幂等键，防止重复提交（来自遗留 MVP 的幂等实践）
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base, TimestampMixin, UTCDateTime


class AuditLog(Base, TimestampMixin):
    """操作审计日志。

    记录「谁、在哪个租户、对什么资源、做了什么、结果如何、traceId 是什么」。
    遗留 MVP 已证明这套结构在排查越权与线上问题时非常有效。
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("idx_audit_logs_tenant_created", "tenant_id", "created_at"),
        Index("idx_audit_logs_action_created", "action", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(36), index=True, doc="所属租户（平台操作为空）")
    actor_id: Mapped[str | None] = mapped_column(String(36), index=True, doc="操作者用户 ID")
    actor_account: Mapped[str | None] = mapped_column(String(64), doc="操作者账号")
    actor_role: Mapped[str | None] = mapped_column(String(64), doc="操作者角色")

    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resource_type: Mapped[str | None] = mapped_column(String(64), index=True)
    resource_id: Mapped[str | None] = mapped_column(String(64), index=True)
    summary: Mapped[str | None] = mapped_column(String(256), doc="人类可读的操作摘要")

    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, doc="结构化附加信息")
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    client_ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    success: Mapped[bool] = mapped_column(default=True, doc="操作是否成功")

    def __repr__(self) -> str:
        return f"<AuditLog {self.action} {self.resource_type}:{self.resource_id}>"


class OutboxEvent(Base, TimestampMixin):
    """事务性发件箱事件。

    业务事务内只写这张表；由后台投递器读取未发布事件并推送到消息队列。
    这样避免了「业务已提交但事件丢失」的不一致问题。
    """

    __tablename__ = "outbox_events"
    __table_args__ = (
        Index("idx_outbox_events_published", "published_at"),
        Index("idx_outbox_events_aggregate", "aggregate_type", "aggregate_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(String(36), index=True)

    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    trace_id: Mapped[str | None] = mapped_column(String(64))

    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime, doc="投递时间，为空表示待投递")
    attempts: Mapped[int] = mapped_column(default=0, doc="投递尝试次数")
    last_error: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<OutboxEvent {self.event_type} {self.aggregate_type}:{self.aggregate_id}>"


class IdempotencyKey(Base, TimestampMixin):
    """幂等键记录。

    客户端在请求头携带 ``Idempotency-Key``，服务端：

    * 首次请求：执行业务，成功后把响应快照存入本表
    * 重复请求（已完成）：直接返回快照，不重复执行业务
    * 重复请求（进行中）：返回 ``IDEMPOTENCY_CONFLICT``，提示稍后重试
    """

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint("scope", "key", name="uq_idempotency_scope_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), nullable=False, index=True, doc="客户端提供的幂等键")
    scope: Mapped[str] = mapped_column(
        String(128), nullable=False, doc="作用域，通常为「端点路径 + 租户 ID」"
    )
    tenant_id: Mapped[str | None] = mapped_column(String(36), index=True)
    user_id: Mapped[str | None] = mapped_column(String(36))

    #: PENDING / COMPLETED
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING", index=True)
    response_status: Mapped[int | None] = mapped_column(doc="首次请求的 HTTP 状态码")
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSON, doc="首次请求的响应快照")
    request_hash: Mapped[str | None] = mapped_column(
        String(64), doc="请求体摘要，用于检测同一幂等键被用于不同请求体"
    )

    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)

    def __repr__(self) -> str:
        return f"<IdempotencyKey {self.scope}:{self.key} status={self.status}>"
