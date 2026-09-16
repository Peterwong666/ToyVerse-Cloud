"""集贤（4G 设备云）适配器骨架。

定位
----
集贤是 **4G 方案**的设备云：玩具通过 ML307N 模组走 4G 直接联网，
设备侧能力（生成 / 激活 / 停用 / OTA）由集贤云承担，因此本适配器
只声明 ``device`` 能力，对话能力由模型类供应商（火山 / 百度）负责。

与京东 JoyInside 的关键差异
---------------------------
========================  ====================  ==========================
维度                      集贤（4G）             京东 JoyInside（Wi-Fi）
========================  ====================  ==========================
激活路径                  开机 → 4G 上线 → 调用   配网 → 上报 SN/MAC → 校验
                          激活接口                二维码 SN → 调用激活接口
平台侧 OTA                **支持**                不支持（仅端侧升级）
激活标识                  设备 SN + IMEI          SN + MAC + 二维码签名
========================  ====================  ==========================

运维含义：4G 设备的激活是「设备主动上报」，平台无法预知上线时刻；
Wi-Fi 设备则依赖用户扫码，因此才需要二维码签名校验（P8 落地）。

待联调校对
----------
本文件中的端点路径、字段名与签名算法均为**按平台 API 惯例给出的骨架**，
尚未与集贤官方文档逐字校对。已知需要在联调时确认的点：

1. 签名算法（当前为 ``HMAC-SHA256(规范化参数, secret_key)`` 占位实现）
2. OTA 推送是「按分批任务」还是「按设备列表」
3. 激活接口是否需要携带 ``vendor_id`` / ``app_id`` 之外的额外凭证
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
PROVIDER_CODE = "jixian"

#: 端点路径（联调时按官方文档校对）
PATH_GENERATE = "/openapi/device/generate"
PATH_ACTIVATE = "/openapi/device/activate"
PATH_DEACTIVATE = "/openapi/device/deactivate"
PATH_OTA_PUSH = "/openapi/device/ota/push"


class JixianProvider(BaseHttpProvider):
    """集贤 4G 设备云适配器。"""

    code = PROVIDER_CODE
    name = "集贤（4G 设备云）"
    kind = AiProviderKind.DEVICE_CLOUD
    vendor = str(CloudVendor.JIXIAN)
    capabilities = frozenset({CAP_DEVICE})
    description = "4G 方案设备云：设备生成 / 激活 / 停用 / OTA 推送"

    def build_headers(self) -> dict[str, str]:
        """集贤以 ``access_key`` 标识调用方（签名在查询参数里）。"""
        return {
            "X-Access-Key": self.credentials.get("access_key", ""),
            "X-Vendor-Id": self.credentials.get("vendor_id", ""),
            "X-App-Id": self.credentials.get("app_id", ""),
        }

    # ---------------- 字段映射 ----------------

    @staticmethod
    def map_provision_response(payload: dict[str, Any]) -> DeviceProvisionResult:
        """把集贤的生成响应映射为统一 DTO。

        ``code``/``msg`` + ``data.list[].deviceId`` 是这类国产云平台最常见的
        响应形状；用 ``.get`` 链式取值为的是在字段缺失时**降级为失败**而不是抛异常——
        生成设备的失败必须被显式记录，不能静默当作成功。
        """
        ok = str(payload.get("code", "0")) in {"0", "200", "SUCCESS"}
        data = payload.get("data") or {}
        items = data.get("list") if isinstance(data, dict) else None
        ids: list[str] = []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("deviceId"):
                    ids.append(str(item["deviceId"]))
        return DeviceProvisionResult(
            success=ok and bool(ids),
            message=str(payload.get("msg") or ("生成成功" if ok else "生成失败")),
            vendor_device_ids=ids,
            simulated=False,
            raw=None,  # 原始响应由调用方按需记录，避免把密钥回显进审计
        )

    # ---------------- 设备侧 ----------------

    async def provision_devices(
        self,
        *,
        product_code: str,
        count: int,
        metadata: dict[str, Any] | None = None,
    ) -> DeviceProvisionResult:
        """在集贤云端批量生成设备。"""
        payload = await self.request_json(
            "POST",
            PATH_GENERATE,
            json_body={
                "productCode": product_code,
                "count": count,
                "networkType": "4G",
                **(metadata or {}),
            },
        )
        return self.map_provision_response(payload)

    async def activate_device(
        self, *, device_id: str, sn: str, extra: dict[str, Any] | None = None
    ) -> DeviceActionResult:
        """激活 4G 设备（设备开机上线后由平台调用）。"""
        payload = await self.request_json(
            "POST",
            PATH_ACTIVATE,
            json_body={"deviceId": device_id, "sn": sn, **(extra or {})},
        )
        ok = str(payload.get("code", "0")) in {"0", "200", "SUCCESS"}
        return DeviceActionResult(
            success=ok,
            message=str(payload.get("msg") or ("激活成功" if ok else "激活失败")),
            device_id=device_id,
            activated=ok,
        )

    async def deactivate_device(self, *, device_id: str, reason: str | None = None) -> DeviceActionResult:
        """停用设备（解绑 / 冻结 / 报废时调用）。"""
        payload = await self.request_json(
            "POST", PATH_DEACTIVATE, json_body={"deviceId": device_id, "reason": reason or ""}
        )
        ok = str(payload.get("code", "0")) in {"0", "200", "SUCCESS"}
        return DeviceActionResult(
            success=ok,
            message=str(payload.get("msg") or ("停用成功" if ok else "停用失败")),
            device_id=device_id,
            activated=False,
        )

    async def push_ota(
        self, *, device_ids: Sequence[str], firmware_version: str, firmware_url: str
    ) -> DeviceActionResult:
        """推送 OTA（4G 方案支持平台侧推送）。"""
        payload = await self.request_json(
            "POST",
            PATH_OTA_PUSH,
            json_body={
                "deviceIds": list(device_ids),
                "firmwareVersion": firmware_version,
                "firmwareUrl": firmware_url,
            },
        )
        ok = str(payload.get("code", "0")) in {"0", "200", "SUCCESS"}
        data = payload.get("data") or {}
        task_id = str(data.get("taskId")) if isinstance(data, dict) and data.get("taskId") else None
        return DeviceActionResult(
            success=ok,
            message=str(payload.get("msg") or ("推送已受理" if ok else "推送失败")),
            task_id=task_id,
        )

    # ---------------- 不支持的能力 ----------------
    #
    # 集贤只声明了 ``device`` 能力，因此不实现对话与语音方法：调用方
    # （:mod:`app.api.v1.ai`）会先按 ``capabilities`` 拦截并返回
    # ``VENDOR_UNAVAILABLE``。之所以不在适配器里覆盖这些方法，是为了避免
    # 「用错误的方法签名骗过类型检查」——能力缺口应由能力声明表达，
    # 而不是靠运行时抛异常表达。


def build_provider() -> JixianProvider:
    """按 ``settings`` 构造集贤适配器。"""
    return JixianProvider(
        credentials=settings.ai_provider_credentials["jixian"],
        retry=HttpRetryConfig(
            timeout_seconds=float(settings.JIXIAN_TIMEOUT_SECONDS),
            max_retries=2,
        ),
    )


__all__ = [
    "PATH_ACTIVATE",
    "PATH_DEACTIVATE",
    "PATH_GENERATE",
    "PATH_OTA_PUSH",
    "PROVIDER_CODE",
    "JixianProvider",
    "build_provider",
]
