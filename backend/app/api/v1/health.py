"""健康检查端点。

供负载均衡、容器编排与监控系统使用，不需认证。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.core.config import settings
from app.db.base import utcnow
from app.db.session import check_connection, database_backend_name
from app.schemas.auth import HealthResponse

router = APIRouter(tags=["健康检查"])

#: 应用版本，与 pyproject.toml 保持同步
APP_VERSION = "0.1.0"


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="存活探针",
    description="进程存活即返回 UP，不检查下游依赖。",
)
async def health() -> HealthResponse:
    """存活探针。"""
    return HealthResponse(
        status="UP",
        app=settings.APP_NAME,
        version=APP_VERSION,
        environment=settings.APP_ENV,
        database=database_backend_name(),
        time=utcnow(),
    )


@router.get(
    "/health/ready",
    response_model=HealthResponse,
    summary="就绪探针",
    description="探测数据库连通性；不可用时返回 DOWN 且状态码 503。",
    responses={503: {"description": "依赖不可用"}},
)
async def readiness() -> HealthResponse:
    """就绪探针。"""
    from fastapi import HTTPException, status

    db_ok = await check_connection()
    if not db_ok:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "INTERNAL_ERROR",
                "message": "数据库不可用",
                "database": database_backend_name(),
            },
        )
    return HealthResponse(
        status="UP",
        app=settings.APP_NAME,
        version=APP_VERSION,
        environment=settings.APP_ENV,
        database=database_backend_name(),
        time=utcnow(),
    )
