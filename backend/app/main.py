"""FastAPI 应用入口。

职责
----
* 创建应用实例，装配中间件（traceId、CORS）
* 注册全局异常处理器，保证**任何**异常路径的响应形状统一
* 挂载 API 路由与四端前端静态资源
* 管理启动/关闭生命周期（日志、安全校验、演示数据）

设计约定：业务逻辑不写在这里，只做装配。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.v1.router import api_router
from app.core.config import FRONTEND_ROOT, InsecureConfigurationError, settings
from app.core.errors import AppException, ErrorCode
from app.core.logging import (
    TRACE_ID_HEADER,
    TraceIdMiddleware,
    get_logger,
    new_trace_id,
    setup_logging,
)
from app.db.session import database_backend_name, dispose_engine
from app.realtime.ws_chat import router as ws_router

logger = get_logger(__name__)

APP_VERSION = "0.1.0"

API_PREFIX = "/api/v1"

#: 四端前端入口（零构建静态资源）
FRONTEND_ENTRIES = ("platform", "merchant", "factory", "miniapp")

#: 不参与静态缓存的路径前缀（API 与文档由各自逻辑处理）
_DYNAMIC_PREFIXES = ("/api/", "/docs", "/redoc", "/openapi.json")


class FrontendCacheControlMiddleware:
    """为前端静态资源补充 ``Cache-Control: no-cache``。

    为什么需要
    ----------
    本项目的前端是**零构建**方案，文件名不含内容哈希（``shell.js`` 而不是
    ``shell.a1b2c3.js``）。若允许浏览器强缓存，发版后用户会持续加载旧代码，
    出现「改了代码但页面没变」的困惑。

    ``no-cache`` 并非「不缓存」，而是「每次回源校验」：配合 StaticFiles 自动
    生成的 ``ETag`` / ``Last-Modified``，内容未变时返回 304，开销极小，
    但能保证代码即时生效。

    （生产环境若要启用强缓存，应改用带内容哈希的文件名 —— 见 docs/01。）
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].startswith(_DYNAMIC_PREFIXES):
            await self.app(scope, receive, send)
            return

        async def send_with_cache_control(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if "cache-control" not in headers:
                    headers["cache-control"] = "no-cache, must-revalidate"
            await send(message)

        await self.app(scope, receive, send_with_cache_control)


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用启动与关闭。"""
    # ---- 启动 ----
    logger.info("=" * 68)
    logger.info("%s v%s 正在启动", settings.APP_NAME, APP_VERSION)
    logger.info("运行环境：%s", settings.APP_ENV)
    logger.info("数据库：%s", database_backend_name())
    logger.info("AI 默认供应商：%s", settings.AI_DEFAULT_PROVIDER)

    # 安全校验：不通过直接拒绝启动（对历史硬编码弱口令问题的架构性防范）
    try:
        settings.validate_security()
    except InsecureConfigurationError as exc:
        logger.error("%s", exc)
        raise

    logger.info("配置安全校验通过")
    logger.info("API 文档：http://%s:%s/docs", settings.HOST, settings.PORT)
    logger.info("=" * 68)

    # 演示数据（幂等，可安全重复执行）
    if settings.SEED_DEMO_DATA:
        from app.db.seed import seed_demo_data

        try:
            await seed_demo_data()
        except Exception:
            logger.exception("写入演示数据失败（不影响服务启动）")

    try:
        yield
    finally:
        # ---- 关闭 ----
        await dispose_engine()
        logger.info("%s 已停止", settings.APP_NAME)


# ---------------------------------------------------------------------------
# 异常处理
# ---------------------------------------------------------------------------


def _error_response(
    request: Request, *, status_code: int, code: str, message: str, details: object = None
) -> JSONResponse:
    """构造统一错误响应。"""
    trace_id = getattr(request.state, "trace_id", None) or new_trace_id()
    body: dict[str, object] = {"code": code, "message": message, "traceId": trace_id}
    if details is not None:
        body["details"] = details
    return JSONResponse(status_code=status_code, content=body, headers={TRACE_ID_HEADER: trace_id})


async def _handle_app_exception(request: Request, exc: Exception) -> JSONResponse:
    """业务异常。"""
    assert isinstance(exc, AppException)
    if exc.status_code >= 500:
        logger.error("业务异常 %s：%s", exc.code, exc.message)
    else:
        logger.info("业务异常 %s：%s", exc.code, exc.message)
    return _error_response(
        request,
        status_code=exc.status_code,
        code=str(exc.code),
        message=exc.message,
        details=exc.details,
    )


async def _handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    """Pydantic 请求校验失败。"""
    assert isinstance(exc, RequestValidationError)
    fields = [
        {
            "field": ".".join(str(part) for part in error.get("loc", []) if part != "body"),
            "message": error.get("msg", ""),
            "type": error.get("type", ""),
        }
        for error in exc.errors()
    ]
    logger.info("请求校验失败：%s", fields)
    return _error_response(
        request,
        status_code=400,
        code=str(ErrorCode.VALIDATION_ERROR),
        message="请求参数校验不通过",
        details=fields,
    )


async def _handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    """Starlette/FastAPI 内置 HTTP 异常，统一为项目错误形状。"""
    assert isinstance(exc, StarletteHTTPException)
    mapping = {
        401: ErrorCode.UNAUTHENTICATED,
        403: ErrorCode.PERMISSION_DENIED,
        404: ErrorCode.RESOURCE_NOT_FOUND,
        405: ErrorCode.VALIDATION_ERROR,
        409: ErrorCode.IDEMPOTENCY_CONFLICT,
    }
    code = mapping.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
    detail = exc.detail
    message = detail if isinstance(detail, str) else str(code)
    details = detail if not isinstance(detail, str) else None
    return _error_response(
        request,
        status_code=exc.status_code,
        code=str(code),
        message=message,
        details=details,
    )


async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    """兜底异常：记录完整堆栈，对外只暴露 traceId。"""
    logger.exception("未捕获异常：%s", exc)
    return _error_response(
        request,
        status_code=500,
        code=str(ErrorCode.INTERNAL_ERROR),
        message="服务内部错误，请稍后重试或联系管理员并提供 traceId",
    )


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    """创建并装配 FastAPI 应用。"""
    # 日志必须最先配置，否则启动阶段（含安全校验失败）的输出会丢失
    setup_logging(level=settings.LOG_LEVEL, as_json=settings.LOG_JSON)

    app = FastAPI(
        title=settings.APP_NAME,
        version=APP_VERSION,
        description=(
            "多租户 AI 智能玩具 SaaS 平台 API。\n\n"
            "覆盖客户开通、产品配置、下单、设备生成、工厂烧录、终端激活与 AI 运营的完整闭环。\n\n"
            f"**统一错误响应**：`{{code, message, traceId, details?}}`，"
            f"可通过响应头 `{TRACE_ID_HEADER}` 关联日志。"
        ),
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    # ---- 中间件（注意：后添加的先执行，traceId 需最外层） ----
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[TRACE_ID_HEADER],
    )
    app.add_middleware(TraceIdMiddleware)
    app.add_middleware(FrontendCacheControlMiddleware)

    # ---- 异常处理 ----
    app.add_exception_handler(AppException, _handle_app_exception)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(Exception, _handle_unexpected)

    # ---- API 路由 ----
    app.include_router(api_router, prefix=API_PREFIX)

    # ---- WebSocket 路由（P8） ----
    # WS 不走 /api/v1 前缀体系：它没有 status code / 响应模型 / OpenAPI 文档，
    # 混进 api_router 只会让 HTTP 契约与帧契约搅在一起。模块自带完整路径
    # （/ws/miniapp/chat），因此这里不加 prefix——与上面的挂载方式风格一致。
    app.include_router(ws_router)

    # ---- 前端静态资源 ----
    _mount_frontend(app)

    return app


def _mount_frontend(app: FastAPI) -> None:
    """挂载四端前端静态资源。

    前端为零构建 ES Module，直接由后端托管，无需 Node 构建步骤。
    """
    if not FRONTEND_ROOT.exists():
        logger.warning("前端目录不存在，跳过静态资源挂载：%s", FRONTEND_ROOT)
        return

    # 四端入口
    for entry in FRONTEND_ENTRIES:
        entry_dir = FRONTEND_ROOT / entry
        if entry_dir.exists():
            app.mount(f"/{entry}", StaticFiles(directory=entry_dir, html=True), name=entry)

    # 共享资源（shared/ 与 pages/）
    for shared_dir in ("shared", "pages"):
        target = FRONTEND_ROOT / shared_dir
        if target.exists():
            app.mount(f"/{shared_dir}", StaticFiles(directory=target), name=shared_dir)

    # 根路径与 /login 都提供统一登录页
    if (FRONTEND_ROOT / "index.html").exists():
        from fastapi.responses import FileResponse

        login_page = FRONTEND_ROOT / "index.html"

        @app.get("/", include_in_schema=False)
        async def root() -> FileResponse:
            """登录页。"""
            return FileResponse(login_page)

        @app.get("/login", include_in_schema=False)
        async def login_page_route() -> FileResponse:
            """登录页（显式路径，便于跳转时统一指向 /login）。"""
            return FileResponse(login_page)

        logger.info("已挂载登录页：/ 与 /login")
    else:
        logger.warning("未找到前端登录页，根路径返回服务信息")

        @app.get("/", include_in_schema=False)
        async def root_info() -> JSONResponse:
            return JSONResponse(
                content={
                    "app": settings.APP_NAME,
                    "version": APP_VERSION,
                    "docs": "/docs",
                    "api": API_PREFIX,
                }
            )


#: 应用实例（供 uvicorn 引用：``uvicorn app.main:app``）
app = create_app()
