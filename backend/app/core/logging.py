"""日志与请求追踪（traceId）。

设计
----
* 每个请求/WebSocket 连接分配一个 ``traceId``，写入日志与响应头 ``x-trace-id``。
* ``traceId`` 通过 :mod:`contextvars` 传递，日志格式化器自动带出，
  业务代码无需手动透传。
* 客户端可通过请求头 ``x-trace-id`` 自带追踪 ID（便于跨服务关联）。
* 这套机制源自遗留 MVP 的 traceId 实践，使错误响应、日志与审计记录可以互相串联。
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from starlette.datastructures import MutableHeaders

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

TRACE_ID_HEADER = "x-trace-id"

#: 当前请求的 traceId（未在请求上下文中时为 None）
_trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)

#: 当前请求所属租户（用于日志检索；商户端请求会填充）
_tenant_id_var: ContextVar[str | None] = ContextVar("tenant_id", default=None)


# ---------------------------------------------------------------------------
# traceId 上下文
# ---------------------------------------------------------------------------


def get_trace_id() -> str | None:
    """获取当前请求的 traceId。"""
    return _trace_id_var.get()


def set_trace_id(trace_id: str | None) -> None:
    """设置当前请求的 traceId（供中间件与后台任务使用）。"""
    _trace_id_var.set(trace_id)


def get_tenant_id() -> str | None:
    """获取当前请求的租户 ID。"""
    return _tenant_id_var.get()


def set_tenant_id(tenant_id: str | None) -> None:
    """设置当前请求的租户 ID。"""
    _tenant_id_var.set(tenant_id)


def new_trace_id() -> str:
    """生成一个新的 traceId。"""
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# 日志格式化
# ---------------------------------------------------------------------------


class _ContextFilter(logging.Filter):
    """把 traceId 与 tenantId 注入每条日志记录。"""

    def filter(self, record: logging.LogRecord) -> bool:
        # LogRecord 未声明这两个字段，用 setattr 动态附加
        setattr(record, "trace_id", _trace_id_var.get() or "-")  # noqa: B010
        setattr(record, "tenant_id", _tenant_id_var.get() or "-")  # noqa: B010
        return True


class _PlainFormatter(logging.Formatter):
    """开发环境可读格式。"""

    def format(self, record: logging.LogRecord) -> str:
        return (
            f"{self.formatTime(record, '%Y-%m-%d %H:%M:%S')} "
            f"[{record.levelname:<7}] "
            f"trace={getattr(record, 'trace_id', '-')} "
            f"tenant={getattr(record, 'tenant_id', '-')} "
            f"{record.name}: {record.getMessage()}"
        )


class _JsonFormatter(logging.Formatter):
    """生产环境结构化格式（便于采集与检索）。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "traceId": getattr(record, "trace_id", "-"),
            "tenantId": getattr(record, "tenant_id", "-"),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(*, level: str = "INFO", as_json: bool = False) -> None:
    """配置根 logger。应在应用启动时调用一次。"""
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_ContextFilter())
    handler.setFormatter(_JsonFormatter() if as_json else _PlainFormatter())
    root.addHandler(handler)

    # 降低三方库噪音
    for noisy in ("uvicorn.access", "sqlalchemy.engine.Engine", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # 有 traceId 时可开启 SQL 日志排查
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """获取 logger。"""
    return logging.getLogger(name)


# ---------------------------------------------------------------------------
# 中间件
# ---------------------------------------------------------------------------


class TraceIdMiddleware:
    """为每个 HTTP 请求 / WebSocket 连接注入 traceId。

    使用纯 ASGI 实现（而非 ``BaseHTTPMiddleware``），
    以避免对 WebSocket 与流式响应造成干扰。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        # 优先复用客户端传入的 traceId，便于跨服务关联
        trace_id = ""
        for raw_name, raw_value in scope.get("headers") or []:
            if raw_name.decode("latin-1").lower() == TRACE_ID_HEADER:
                trace_id = raw_value.decode("latin-1").strip()
                break
        if not trace_id or len(trace_id) > 128:
            trace_id = new_trace_id()

        token = _trace_id_var.set(trace_id)
        tenant_token = _tenant_id_var.set(None)
        # 供路由与依赖注入读取
        scope.setdefault("state", {})
        scope["state"]["trace_id"] = trace_id

        started = time.perf_counter()

        async def send_with_trace(message: Message) -> None:
            # WebSocket 的 accept 也能带 headers，但帧协议不同，这里只处理 HTTP
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers[TRACE_ID_HEADER] = trace_id
                scope["state"]["status_code"] = message["status"]
                elapsed_ms = (time.perf_counter() - started) * 1000
                scope["state"]["elapsed_ms"] = round(elapsed_ms, 2)
            await send(message)

        logger = logging.getLogger("toyverse.access")
        try:
            await self.app(scope, receive, send_with_trace)
        finally:
            if scope["type"] == "http":
                elapsed_ms = scope["state"].get("elapsed_ms")
                if elapsed_ms is not None:
                    logger.info(
                        "%s %s -> %s (%.2fms)",
                        scope.get("method", "-"),
                        scope.get("path", "-"),
                        scope["state"].get("status_code", "-"),
                        elapsed_ms,
                    )
            _tenant_id_var.reset(tenant_token)
            _trace_id_var.reset(token)
