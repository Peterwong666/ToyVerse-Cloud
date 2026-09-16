"""幂等键处理。

用途
----
防止客户端重复提交造成重复业务（重复绑定设备、重复下单、重复执行分配单）。
遗留 MVP 已实现这一语义，本项目将其抽为通用能力。

契约
----
客户端在请求头携带 ``Idempotency-Key: <任意唯一串>``，服务端行为：

============  ==========================================
场景          行为
============  ==========================================
首次请求      执行业务，成功后存下响应快照
重复请求      直接返回首次的响应快照（状态码与响应体一致）
请求进行中    返回 ``IDEMPOTENCY_CONFLICT``
同键不同体    返回 ``IDEMPOTENCY_CONFLICT``（防止键被复用）
============  ==========================================

典型用法::

    replay = await find_completed(session, key, scope)
    if replay is not None:
        return replay

    await reserve(session, key=key, scope=scope, tenant_id=..., user_id=..., request_hash=...)
    result = await do_business()
    await finalize(session, key=key, scope=scope, status=200, body=result)
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import idempotency_conflict
from app.core.ids import new_id
from app.db.base import utcnow
from app.models.audit import IdempotencyKey

#: 请求头名称
IDEMPOTENCY_HEADER = "idempotency-key"

#: 幂等记录保留时长
DEFAULT_TTL = timedelta(hours=24)

STATUS_PENDING = "PENDING"
STATUS_COMPLETED = "COMPLETED"


def request_fingerprint(payload: Any) -> str:
    """计算请求体摘要，用于检测同一幂等键被用于不同请求体。"""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_scope(endpoint: str, tenant_id: str | None) -> str:
    """构造幂等作用域：同一端点在同一个租户内互斥。"""
    return f"{endpoint}::tenant={tenant_id or 'platform'}"


async def find_completed(session: AsyncSession, key: str, scope: str) -> dict[str, Any] | None:
    """查找已完成的幂等记录。

    Returns:
        首次请求的响应体快照；未找到返回 ``None``。
    """
    stmt = select(IdempotencyKey).where(
        IdempotencyKey.key == key,
        IdempotencyKey.scope == scope,
        IdempotencyKey.status == STATUS_COMPLETED,
    )
    record = (await session.execute(stmt)).scalar_one_or_none()
    if record is None or record.response_body is None:
        return None
    return dict(record.response_body)


async def reserve(
    session: AsyncSession,
    *,
    key: str,
    scope: str,
    tenant_id: str | None = None,
    user_id: str | None = None,
    request_hash: str | None = None,
) -> None:
    """占位：写入一条 ``PENDING`` 记录，表示该幂等键正在处理中。

    并发场景下由唯一约束 ``uq_idempotency_scope_key`` 保证只有一个请求能占位成功，
    其余请求会收到 :class:`~app.core.errors.ErrorCode.IDEMPOTENCY_CONFLICT`。

    Raises:
        AppException: 该幂等键已被占用（进行中或已用不同请求体提交过）。
    """
    existing_stmt = select(IdempotencyKey).where(
        IdempotencyKey.key == key, IdempotencyKey.scope == scope
    )
    existing = (await session.execute(existing_stmt)).scalar_one_or_none()

    if existing is not None:
        if existing.status == STATUS_PENDING:
            raise idempotency_conflict("相同请求正在处理中，请稍后重试")
        if (
            request_hash is not None
            and existing.request_hash is not None
            and existing.request_hash != request_hash
        ):
            raise idempotency_conflict("该幂等键已用于不同的请求体，已拒绝")
        # 已完成且请求体一致：由调用方通过 find_completed 返回快照，此处不应到达
        raise idempotency_conflict("该请求已处理，请勿重复提交")

    session.add(
        IdempotencyKey(
            id=new_id("audit_log"),
            key=key,
            scope=scope,
            tenant_id=tenant_id,
            user_id=user_id,
            status=STATUS_PENDING,
            request_hash=request_hash,
            expires_at=utcnow() + DEFAULT_TTL,
        )
    )
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise idempotency_conflict("相同请求正在处理中，请稍后重试") from exc


async def finalize(
    session: AsyncSession,
    *,
    key: str,
    scope: str,
    status: int,
    body: dict[str, Any] | None,
) -> None:
    """标记幂等键处理完成，存下响应快照。"""
    stmt = select(IdempotencyKey).where(
        IdempotencyKey.key == key, IdempotencyKey.scope == scope
    )
    record = (await session.execute(stmt)).scalar_one_or_none()
    if record is None:
        return
    record.status = STATUS_COMPLETED
    record.response_status = status
    record.response_body = body
    await session.flush()


async def release(session: AsyncSession, *, key: str, scope: str) -> None:
    """业务失败时释放占位，使客户端可以用同一幂等键重试。"""
    stmt = select(IdempotencyKey).where(
        IdempotencyKey.key == key, IdempotencyKey.scope == scope
    )
    record = (await session.execute(stmt)).scalar_one_or_none()
    if record is not None and record.status == STATUS_PENDING:
        await session.delete(record)
        await session.flush()
