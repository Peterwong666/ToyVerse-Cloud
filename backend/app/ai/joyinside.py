"""京东云 JoyInside（Wi-Fi 设备云）适配器骨架。

与集贤的核心差异：**不支持平台侧 OTA**
--------------------------------------
JoyInside 方案（ESP32-S3）的固件升级由端侧完成，平台无法推送。
本适配器用两种方式把这一事实表达清楚：

1. ``capabilities`` 仍然声明 ``device``（生成 / 激活 / 停用是有的），
   但 :meth:`JoyInsideProvider.push_ota` **明确抛错**并说明原因，
   而不是静默返回成功——P9 的验收项「Wi-Fi 产品推送被拒绝」就依赖此处。
2. 路由层据 :data:`OTA_SUPPORTED` 在界面上显示「不支持（端侧升级）」，
   避免运营以为平台漏做了功能。

Wi-Fi 激活为什么需要 MAC
------------------------
Wi-Fi 设备没有 IMEI，平台识别它靠「SN + MAC」；而二维码里还带了签名，
因此激活前必须校验三者一致（P8 落地）。这里保留 ``extra`` 透传 MAC 等字段，
是为了让 P8 不必修改适配器签名。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.ai.base import (
    CAP_DEVICE,
    BaseHttpProvider,
    DeviceActionResult,
    DeviceProvisionResult,
    HttpRetryConfig,
)
from app.core.config import settings
from app.models.enums import AiProviderKind, CloudVendor

#: 注册键
PROVIDER_CODE = "joyinside"

#: 平台侧是否支持 OTA（与 ``catalog.CloudProvider.ota_support`` 的语义对齐）
OTA_SUPPORTED = False

#: 端点路径（联调时按京东云官方文档校对）
PATH_DEVICE_CREATE = "/openapi/v1/device/create"
PATH_DEVICE_ACTIVATE = "/openapi/v1/device/activate"
PATH_DEVICE_DEACTIVATE = "/openapi/v1/device/deactivate"
PATH_DEVICE_QUERY = "/openapi/v1/device/query"


class JoyInsideProvider(BaseHttpProvider):
    """京东云 JoyInside Wi-Fi 设备云适配器。"""

    code = PROVIDER_CODE
    name = "京东云 JoyInside（Wi-Fi）"
    kind = AiProviderKind.DEVICE_CLOUD
    vendor = str(CloudVendor.JOYINSIDE)
    capabilities = frozenset({CAP_DEVICE})
    description = "Wi-Fi 方案设备云：设备生成 / 激活 / 停用；不支持平台侧 OTA"

    def build_headers(self) -> dict[str, str]:
        """京东云以 ``tenant_id`` + ``access_key`` 标识调用方。"""
        return {
            "X-Tenant-Id": self.credentials.get("tenant_id", ""),
            "X-Access-Key": self.credentials.get("access_key", ""),
            "X-App-Id": self.credentials.get("app_id", ""),
        }

    # ---------------- 字段映射 ----------------

    @staticmethod
    def map_provision_response(payload: dict[str, Any]) -> DeviceProvisionResult:
        """映射生成响应。

        与集贤不同，JoyInside 返回的是 ``result.devices[]``，
        字段名差异正说明「字段映射必须留在适配器里」这一设计取向。
        """
        ok = str(payload.get("code", "0")) in {"0", "200", "SUCCESS"}
        result = payload.get("result") or {}
        items = result.get("devices") if isinstance(result, dict) else None
        ids: list[str] = []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("deviceNo"):
                    ids.append(str(item["deviceNo"]))
        return DeviceProvisionResult(
            success=ok and bool(ids),
            message=str(payload.get("message") or ("生成成功" if ok else "生成失败")),
            vendor_device_ids=ids,
        )

    # ---------------- 设备侧 ----------------

    async def provision_devices(
        self,
        *,
        product_code: str,
        count: int,
        metadata: dict[str, Any] | None = None,
    ) -> DeviceProvisionResult:
        """在 JoyInside 云端批量生成设备。"""
        payload = await self.request_json(
            "POST",
            PATH_DEVICE_CREATE,
            json_body={
                "productCode": product_code,
                "count": count,
                "networkType": "WIFI",
                **(metadata or {}),
            },
        )
        return self.map_provision_response(payload)

    async def activate_device(
        self, *, device_id: str, sn: str, extra: dict[str, Any] | None = None
    ) -> DeviceActionResult:
        """激活 Wi-Fi 设备。

        ``extra`` 需携带 ``mac`` 与二维码签名 ``qrSign``（由 P8 校验后传入）；
        本层不重复校验，避免把业务规则下沉到适配器。
        """
        payload = await self.request_json(
            "POST",
            PATH_DEVICE_ACTIVATE,
            json_body={"deviceNo": device_id, "sn": sn, **(extra or {})},
        )
        ok = str(payload.get("code", "0")) in {"0", "200", "SUCCESS"}
        return DeviceActionResult(
            success=ok,
            message=str(payload.get("message") or ("激活成功" if ok else "激活失败")),
            device_id=device_id,
            activated=ok,
        )

    async def deactivate_device(self, *, device_id: str, reason: str | None = None) -> DeviceActionResult:
        """停用设备。"""
        payload = await self.request_json(
            "POST", PATH_DEVICE_DEACTIVATE, json_body={"deviceNo": device_id, "reason": reason or ""}
        )
        ok = str(payload.get("code", "0")) in {"0", "200", "SUCCESS"}
        return DeviceActionResult(
            success=ok,
            message=str(payload.get("message") or ("停用成功" if ok else "停用失败")),
            device_id=device_id,
            activated=False,
        )

    async def push_ota(
        self, *, device_ids: Sequence[str], firmware_version: str, firmware_url: str
    ) -> DeviceActionResult:
        """拒绝 OTA 推送。

        Raises:
            AppException: ``VENDOR_UNAVAILABLE``，``details.reason=OTA_NOT_SUPPORTED``。
        """
        raise self._call_failed(
            "京东云 JoyInside（Wi-Fi）方案不支持平台侧 OTA，固件升级需在设备端完成",
            detail={"reason": "OTA_NOT_SUPPORTED", "firmwareVersion": firmware_version},
        )


def build_provider() -> JoyInsideProvider:
    """按 ``settings`` 构造 JoyInside 适配器。"""
    return JoyInsideProvider(
        credentials=settings.ai_provider_credentials["joyinside"],
        retry=HttpRetryConfig(
            timeout_seconds=float(settings.JOYINSIDE_TIMEOUT_SECONDS),
            max_retries=2,
        ),
    )


__all__ = [
    "OTA_SUPPORTED",
    "PATH_DEVICE_ACTIVATE",
    "PATH_DEVICE_CREATE",
    "PATH_DEVICE_DEACTIVATE",
    "PATH_DEVICE_QUERY",
    "PROVIDER_CODE",
    "JoyInsideProvider",
    "build_provider",
]
