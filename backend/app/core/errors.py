"""统一错误码与异常体系。

设计约定
--------
* 所有业务异常继承 :class:`AppException`，携带项目定义的 :class:`ErrorCode`。
* 所有错误响应体形状统一为 ``{"code", "message", "traceId", "details"}``。
* HTTP 状态码与错误码一一对应，便于前端统一处理。

这套错误码合并自三个遗留项目的错误词汇表，是各端与前端约定的唯一契约。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """项目统一错误码。"""

    # ---- 认证与授权 ----
    UNAUTHENTICATED = "UNAUTHENTICATED"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    ACCOUNT_DISABLED = "ACCOUNT_DISABLED"
    ACCOUNT_LOCKED = "ACCOUNT_LOCKED"
    PASSWORD_CHANGE_REQUIRED = "PASSWORD_CHANGE_REQUIRED"
    PERMISSION_DENIED = "PERMISSION_DENIED"

    # ---- 租户 ----
    TENANT_DISABLED = "TENANT_DISABLED"
    TENANT_CODE_EXISTS = "TENANT_CODE_EXISTS"

    # ---- 校验与通用资源 ----
    VALIDATION_ERROR = "VALIDATION_ERROR"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    CASCADE_CONFLICT = "CASCADE_CONFLICT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"

    # ---- 目录域 ----
    # 约定：目录域内「编码重复」一律归类为**冲突**（409）而非校验失败（400）——
    # 请求本身合法，只是与既有资源撞了唯一键；客户端应提示「换一个编码」，
    # 而不是让用户以为表单填错了。每个主要资源一个码，便于前端精确定位字段。
    PRODUCT_CODE_EXISTS = "PRODUCT_CODE_EXISTS"
    #: 同一份文件被用于「另一个订单/租户」的批次导入。
    #: 不能静默复用旧批次——那会把设备悄悄挂到旧订单上。
    BATCH_FILE_CONFLICT = "BATCH_FILE_CONFLICT"
    CLOUD_CODE_EXISTS = "CLOUD_CODE_EXISTS"
    TEMPLATE_CODE_EXISTS = "TEMPLATE_CODE_EXISTS"
    PRODUCT_NOT_AUTHORIZED = "PRODUCT_NOT_AUTHORIZED"

    # ---- 设备与订单 ----
    DEVICE_NOT_AVAILABLE = "DEVICE_NOT_AVAILABLE"
    DEVICE_NOT_FOUND = "DEVICE_NOT_FOUND"
    DEVICE_ALREADY_BOUND = "DEVICE_ALREADY_BOUND"
    DEVICE_FROZEN = "DEVICE_FROZEN"
    DEVICE_NOT_IN_TENANT = "DEVICE_NOT_IN_TENANT"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"

    # ---- 激活与二维码 ----
    QR_INVALID = "QR_INVALID"
    QR_EXPIRED = "QR_EXPIRED"
    BIND_FAILED = "BIND_FAILED"

    # ---- 外部依赖 ----
    VENDOR_UNAVAILABLE = "VENDOR_UNAVAILABLE"

    # ---- 兜底 ----
    INTERNAL_ERROR = "INTERNAL_ERROR"


#: 错误码 → HTTP 状态码
_STATUS_MAP: dict[ErrorCode, int] = {
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.INVALID_CREDENTIALS: 401,
    ErrorCode.ACCOUNT_DISABLED: 403,
    ErrorCode.ACCOUNT_LOCKED: 403,
    ErrorCode.PASSWORD_CHANGE_REQUIRED: 403,
    ErrorCode.PERMISSION_DENIED: 403,
    ErrorCode.TENANT_DISABLED: 403,
    ErrorCode.TENANT_CODE_EXISTS: 409,
    ErrorCode.VALIDATION_ERROR: 400,
    ErrorCode.RESOURCE_NOT_FOUND: 404,
    ErrorCode.CASCADE_CONFLICT: 409,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.PRODUCT_CODE_EXISTS: 409,
    ErrorCode.BATCH_FILE_CONFLICT: 409,
    ErrorCode.CLOUD_CODE_EXISTS: 409,
    ErrorCode.TEMPLATE_CODE_EXISTS: 409,
    ErrorCode.PRODUCT_NOT_AUTHORIZED: 409,
    ErrorCode.DEVICE_NOT_AVAILABLE: 409,
    ErrorCode.DEVICE_NOT_FOUND: 404,
    ErrorCode.DEVICE_ALREADY_BOUND: 409,
    ErrorCode.DEVICE_FROZEN: 409,
    ErrorCode.DEVICE_NOT_IN_TENANT: 403,
    ErrorCode.INVALID_STATE_TRANSITION: 409,
    ErrorCode.QR_INVALID: 404,
    ErrorCode.QR_EXPIRED: 409,
    ErrorCode.BIND_FAILED: 409,
    ErrorCode.VENDOR_UNAVAILABLE: 503,
    ErrorCode.INTERNAL_ERROR: 500,
}

#: 错误码 → 默认中文提示
_DEFAULT_MESSAGE: dict[ErrorCode, str] = {
    ErrorCode.UNAUTHENTICATED: "未登录或登录态已失效，请重新登录",
    ErrorCode.INVALID_CREDENTIALS: "账号或密码错误",
    ErrorCode.ACCOUNT_DISABLED: "账号已被禁用，请联系管理员",
    ErrorCode.ACCOUNT_LOCKED: "账号因多次登录失败已被锁定，请稍后再试",
    ErrorCode.PASSWORD_CHANGE_REQUIRED: "首次登录需修改初始密码",
    ErrorCode.PERMISSION_DENIED: "无权限执行该操作",
    ErrorCode.TENANT_DISABLED: "所属租户已被禁用，请联系平台管理员",
    ErrorCode.TENANT_CODE_EXISTS: "租户编码已存在",
    ErrorCode.VALIDATION_ERROR: "请求参数校验不通过",
    ErrorCode.RESOURCE_NOT_FOUND: "资源不存在",
    ErrorCode.CASCADE_CONFLICT: "存在关联数据，无法删除",
    ErrorCode.IDEMPOTENCY_CONFLICT: "重复请求，且原请求尚未完成",
    ErrorCode.PRODUCT_CODE_EXISTS: "产品编码已存在",
    ErrorCode.BATCH_FILE_CONFLICT: "该文件已用于其它订单的批次，请修改文件内容或更换订单",
    ErrorCode.CLOUD_CODE_EXISTS: "云服务商编码已存在",
    ErrorCode.TEMPLATE_CODE_EXISTS: "产品模板编码已存在",
    ErrorCode.PRODUCT_NOT_AUTHORIZED: "该产品未授权给此租户",
    ErrorCode.DEVICE_NOT_AVAILABLE: "设备当前状态不可用",
    ErrorCode.DEVICE_NOT_FOUND: "设备不存在",
    ErrorCode.DEVICE_ALREADY_BOUND: "设备已被绑定",
    ErrorCode.DEVICE_FROZEN: "设备已冻结，操作被拒绝",
    ErrorCode.DEVICE_NOT_IN_TENANT: "设备不属于当前租户",
    ErrorCode.INVALID_STATE_TRANSITION: "当前状态不允许该操作",
    ErrorCode.QR_INVALID: "二维码无效或不存在",
    ErrorCode.QR_EXPIRED: "确认令牌已过期，请重新扫码",
    ErrorCode.BIND_FAILED: "设备绑定失败",
    ErrorCode.VENDOR_UNAVAILABLE: "供应商协议尚未配置，该能力已安全禁用",
    ErrorCode.INTERNAL_ERROR: "服务内部错误",
}


class AppException(Exception):
    """业务异常基类。

    Args:
        code: 项目统一错误码。
        message: 面向用户的提示；留空时使用错误码的默认中文提示。
        details: 附加的结构化信息（如字段级校验失败详情）。
        status_code: 覆盖默认 HTTP 状态码（一般无需传）。
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str | None = None,
        *,
        details: Any = None,
        status_code: int | None = None,
    ) -> None:
        self.code = code
        self.message = message or _DEFAULT_MESSAGE.get(code, str(code))
        self.details = details
        self.status_code = status_code or _STATUS_MAP.get(code, 500)
        super().__init__(self.message)

    def to_payload(self, trace_id: str | None = None) -> dict[str, Any]:
        """构造统一错误响应体。"""
        payload: dict[str, Any] = {
            "code": str(self.code),
            "message": self.message,
            "traceId": trace_id,
        }
        if self.details is not None:
            payload["details"] = self.details
        return payload

    def __repr__(self) -> str:
        return f"<AppException {self.code} status={self.status_code} message={self.message!r}>"


# ---------------------------------------------------------------------------
# 便捷构造器
#
# 用法：raise not_found("租户不存在")
# 相比 AppException(ErrorCode.RESOURCE_NOT_FOUND, ...) 更简洁，
# 且在代码中一眼能看出语义。
# ---------------------------------------------------------------------------


def unauthenticated(message: str | None = None) -> AppException:
    return AppException(ErrorCode.UNAUTHENTICATED, message)


def invalid_credentials(message: str | None = None) -> AppException:
    return AppException(ErrorCode.INVALID_CREDENTIALS, message)


def account_disabled(message: str | None = None) -> AppException:
    return AppException(ErrorCode.ACCOUNT_DISABLED, message)


def account_locked(message: str | None = None) -> AppException:
    return AppException(ErrorCode.ACCOUNT_LOCKED, message)


def password_change_required(message: str | None = None) -> AppException:
    return AppException(ErrorCode.PASSWORD_CHANGE_REQUIRED, message)


def permission_denied(message: str | None = None, *, required: str | None = None) -> AppException:
    details = {"requiredPermission": required} if required else None
    return AppException(ErrorCode.PERMISSION_DENIED, message, details=details)


def tenant_disabled(message: str | None = None) -> AppException:
    return AppException(ErrorCode.TENANT_DISABLED, message)


def tenant_code_exists(message: str | None = None) -> AppException:
    return AppException(ErrorCode.TENANT_CODE_EXISTS, message)


def validation_error(message: str | None = None, *, details: Any = None) -> AppException:
    return AppException(ErrorCode.VALIDATION_ERROR, message, details=details)


def not_found(message: str | None = None) -> AppException:
    return AppException(ErrorCode.RESOURCE_NOT_FOUND, message)


def cascade_conflict(message: str | None = None, *, details: Any = None) -> AppException:
    return AppException(ErrorCode.CASCADE_CONFLICT, message, details=details)


def idempotency_conflict(message: str | None = None) -> AppException:
    return AppException(ErrorCode.IDEMPOTENCY_CONFLICT, message)


def batch_file_conflict(message: str | None = None, *, details: Any = None) -> AppException:
    return AppException(ErrorCode.BATCH_FILE_CONFLICT, message, details=details)


def product_code_exists(message: str | None = None, *, details: Any = None) -> AppException:
    return AppException(ErrorCode.PRODUCT_CODE_EXISTS, message, details=details)


def cloud_code_exists(message: str | None = None, *, details: Any = None) -> AppException:
    return AppException(ErrorCode.CLOUD_CODE_EXISTS, message, details=details)


def template_code_exists(message: str | None = None, *, details: Any = None) -> AppException:
    return AppException(ErrorCode.TEMPLATE_CODE_EXISTS, message, details=details)


def product_not_authorized(message: str | None = None, *, details: Any = None) -> AppException:
    return AppException(ErrorCode.PRODUCT_NOT_AUTHORIZED, message, details=details)


def device_not_available(message: str | None = None) -> AppException:
    return AppException(ErrorCode.DEVICE_NOT_AVAILABLE, message)


def device_not_found(message: str | None = None) -> AppException:
    return AppException(ErrorCode.DEVICE_NOT_FOUND, message)


def device_already_bound(message: str | None = None) -> AppException:
    return AppException(ErrorCode.DEVICE_ALREADY_BOUND, message)


def device_frozen(message: str | None = None) -> AppException:
    return AppException(ErrorCode.DEVICE_FROZEN, message)


def device_not_in_tenant(message: str | None = None) -> AppException:
    return AppException(ErrorCode.DEVICE_NOT_IN_TENANT, message)


def invalid_state_transition(
    message: str | None = None, *, current: str | None = None, target: str | None = None
) -> AppException:
    details = {"current": current, "target": target} if current or target else None
    return AppException(ErrorCode.INVALID_STATE_TRANSITION, message, details=details)


def qr_invalid(message: str | None = None) -> AppException:
    return AppException(ErrorCode.QR_INVALID, message)


def qr_expired(message: str | None = None) -> AppException:
    return AppException(ErrorCode.QR_EXPIRED, message)


def bind_failed(message: str | None = None, *, details: Any = None, ) -> AppException:
    return AppException(ErrorCode.BIND_FAILED, message, details=details)


def vendor_unavailable(message: str | None = None, *, vendor: str | None = None) -> AppException:
    details = {"vendor": vendor} if vendor else None
    return AppException(ErrorCode.VENDOR_UNAVAILABLE, message, details=details)


def internal_error(message: str | None = None) -> AppException:
    return AppException(ErrorCode.INTERNAL_ERROR, message)
