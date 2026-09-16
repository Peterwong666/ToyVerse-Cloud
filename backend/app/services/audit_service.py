"""审计日志服务。

所有关键动作都应调用 :func:`record` 落库。审计记录携带 ``traceId``，
可与日志、错误响应互相串联，是排查越权与线上问题的第一手资料。
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import AuthContext
from app.core.ids import new_id
from app.core.logging import get_logger, get_trace_id
from app.models.audit import AuditLog
from app.models.enums import AuditAction

logger = get_logger(__name__)


def _client_ip(request: Request | None) -> str | None:
    """提取客户端 IP（优先取反代透传的真实 IP）。"""
    if request is None:
        return None
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else None


async def record(
    session: AsyncSession,
    *,
    action: AuditAction | str,
    tenant_id: str | None = None,
    actor: AuthContext | None = None,
    actor_account: str | None = None,
    actor_role: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    summary: str | None = None,
    detail: dict[str, Any] | None = None,
    success: bool = True,
    request: Request | None = None,
) -> AuditLog:
    """写入一条审计记录。

    Args:
        session: 数据库会话（与业务事务共用，保证「业务成功则审计必在」）。
        action: 动作类型。
        tenant_id: 所属租户；平台级操作留空。
        actor: 认证上下文；登录失败等场景可为空。
        actor_account: 无认证上下文时显式指定操作者账号（如登录失败）。
        actor_role: 同上，显式指定角色。
        resource_type: 资源类型（如 ``device``、``order``）。
        resource_id: 资源 ID。
        summary: 人类可读摘要，直接展示给运维。
        detail: 结构化附加信息。
        success: 操作是否成功。
        request: 用于提取 IP 与 User-Agent。

    Returns:
        已加入会话的审计记录（尚未提交，由调用方决定事务边界）。
    """
    log = AuditLog(
        id=new_id("audit_log"),
        tenant_id=tenant_id or (actor.tenant_id if actor else None),
        actor_id=actor.user_id if actor else None,
        actor_account=actor.account if actor else actor_account,
        actor_role=actor.role_code if actor else actor_role,
        action=str(action),
        resource_type=resource_type,
        resource_id=resource_id,
        summary=summary,
        detail=detail,
        trace_id=get_trace_id(),
        client_ip=_client_ip(request),
        user_agent=(request.headers.get("user-agent", "")[:512] if request else None),
        success=success,
    )
    session.add(log)

    # 敏感动作同时输出到日志，便于实时告警
    if not success or action in {AuditAction.PERMISSION_DENIED, AuditAction.LOGIN_FAILED}:
        logger.warning(
            "审计｜%s 账号=%s 资源=%s:%s 结果=%s 摘要=%s",
            action,
            log.actor_account or "-",
            resource_type or "-",
            resource_id or "-",
            "成功" if success else "失败",
            summary or "-",
        )
    else:
        logger.info(
            "审计｜%s 账号=%s 资源=%s:%s",
            action,
            log.actor_account or "-",
            resource_type or "-",
            resource_id or "-",
        )

    return log


async def record_failure(
    session: AsyncSession,
    *,
    action: AuditAction | str,
    summary: str,
    detail: dict[str, Any] | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    actor: AuthContext | None = None,
    request: Request | None = None,
) -> AuditLog:
    """写入一条失败审计。

    ``resource_type`` / ``resource_id`` 必须能传进来：失败审计若不带资源标识，
    运维就只能靠摘要文字去猜是哪一单/哪台设备出了问题（P4 阶段实测到的缺口）。
    """
    return await record(
        session,
        action=action,
        actor=actor,
        resource_type=resource_type,
        resource_id=resource_id,
        summary=summary,
        detail=detail,
        success=False,
        request=request,
    )
